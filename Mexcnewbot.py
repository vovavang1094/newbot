import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
import random
from dotenv import load_dotenv
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes, CommandHandler, CallbackQueryHandler
from fastapi import FastAPI
from contextlib import asynccontextmanager
import uvicorn
from datetime import datetime, timedelta
import html

# ====================== НАСТРОЙКИ ======================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID", 0))

# ПРОВЕРКА ОБЯЗАТЕЛЬНЫХ ПЕРЕМЕННЫХ
if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "ваш_токен_бота":
    print("❌ ОШИБКА: TELEGRAM_TOKEN не установлен!")
    print("Пожалуйста, установите переменную окружения TELEGRAM_TOKEN на Render")
    exit(1)

if not MY_USER_ID:
    print("❌ ОШИБКА: MY_USER_ID не установлен!")
    print("Пожалуйста, установите переменную окружения MY_USER_ID на Render")
    exit(1)

MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY", "")

DAILY_VOLUME_LIMIT = 500_000
MIN_PREV_VOLUME = 1000
MIN_CURRENT_VOLUME = 2200
MIN_PRICE = 0.0001
MAX_PRICE = 100

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Глобальные переменные
tracked_symbols = set()
sent_alerts = {}
blacklist = set()
paused_alerts = set()
alert_history = []

# Глобальные переменные для управления задачами
scanner_task = None
application = None
bot_instance = None

# Списки для фильтрации
STOCK_KEYWORDS = ['STOCK', 'ETF', 'SHARES', 'INDEX', 'FUND', 'BASKET', 'TOKENIZED']
STOCK_SYMBOLS = {
    'AAPL', 'GOOGL', 'AMZN', 'MSFT', 'TSLA', 'META', 'NVDA', 'NFLX', 
    'AMD', 'INTC', 'IBM', 'ORCL', 'CSCO', 'ADBE', 'PYPL', 'CRM',
    'SPY', 'QQQ', 'DIA', 'IWM', 'VOO', 'IVV', 'VTI', 'VUG',
    'MSTR', 'COIN', 'RIOT', 'MAR', 'HUT', 'BITF', 'CLSK'
}

# ====================== MEXC API ФУНКЦИИ ======================
def generate_signature(params: str) -> str:
    """Генерация подписи для MEXC API"""
    return hmac.new(
        MEXC_SECRET_KEY.encode() if MEXC_SECRET_KEY else b"",
        params.encode(),
        hashlib.sha256
    ).hexdigest()


async def get_all_futures_symbols():
    """Получаем ВСЕ символы фьючерсов с MEXC"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://contract.mexc.com/api/v1/contract/detail",
                timeout=15
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Ошибка получения символов: {resp.status}")
                    return []
                
                data = await resp.json()
                if not data.get("success"):
                    logger.error(f"API error: {data}")
                    return []
                
                symbols_data = data.get("data", [])
                all_symbols = []
                
                for s in symbols_data:
                    symbol_name = s.get("symbol", "")
                    if symbol_name.endswith("_USDT"):
                        formatted = symbol_name.replace("_USDT", "USDT")
                        all_symbols.append(formatted)
                
                logger.info(f"Найдено {len(all_symbols)} USDT фьючерсов")
                return all_symbols
                
    except Exception as e:
        logger.error(f"Ошибка получения всех символов: {e}")
        return []


async def get_1d_volume(symbol: str) -> float:
    """Получаем объём за 1 день (24 часа) для символа"""
    api_symbol = symbol.replace("USDT", "_USDT")
    timestamp = str(int(time.time() * 1000))
    
    query_string = f"symbol={api_symbol}&interval=Day1&limit=1"
    signature = generate_signature(query_string)
    
    headers = {
        "ApiKey": MEXC_API_KEY if MEXC_API_KEY else "",
        "Request-Time": timestamp,
        "Signature": signature
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{api_symbol}",
                params={
                    "symbol": api_symbol,
                    "interval": "Day1",
                    "limit": 1
                },
                headers=headers,
                timeout=10
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    if data.get("success") and "data" in data:
                        kline_data = data["data"]
                        if "amount" in kline_data and len(kline_data["amount"]) > 0:
                            volume = float(kline_data["amount"][0])
                            return volume
                
                return 0
                
    except Exception as e:
        logger.debug(f"Ошибка получения 1D объёма для {symbol}: {str(e)[:100]}")
        return 0


async def get_1m_kline_data(symbol: str):
    """Получаем данные за последние 2 свечи на 1-минутном таймфрейме"""
    api_symbol = symbol.replace("USDT", "_USDT")
    timestamp = str(int(time.time() * 1000))
    
    query_string = f"symbol={api_symbol}&interval=Min1&limit=2"
    signature = generate_signature(query_string)
    
    headers = {
        "ApiKey": MEXC_API_KEY if MEXC_API_KEY else "",
        "Request-Time": timestamp,
        "Signature": signature
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{api_symbol}",
                params={
                    "symbol": api_symbol,
                    "interval": "Min1",
                    "limit": 2
                },
                headers=headers,
                timeout=10
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    if data.get("success") and "data" in data:
                        kline_data = data["data"]
                        
                        if len(kline_data.get("close", [])) >= 2:
                            prev_volume = int(float(kline_data["amount"][0]))
                            prev_close = float(kline_data["close"][0])
                            curr_volume = int(float(kline_data["amount"][1]))
                            curr_close = float(kline_data["close"][1])
                            
                            return {
                                "prev_volume": prev_volume,
                                "curr_volume": curr_volume,
                                "prev_price": prev_close,
                                "curr_price": curr_close,
                                "symbol": symbol
                            }
                
    except Exception as e:
        logger.debug(f"Ошибка 1m данных для {symbol}: {str(e)[:100]}")
    
    return None


def filter_stock_symbols(symbols: list) -> list:
    """Фильтруем акции и подобные символы"""
    filtered = []
    
    for symbol in symbols:
        clean_symbol = symbol.replace("USDT", "")
        
        # Пропускаем если это известная акция
        if clean_symbol in STOCK_SYMBOLS:
            logger.debug(f"Пропускаем известную акцию: {symbol}")
            continue
        
        # Пропускаем если содержит ключевые слова акций
        if any(keyword in symbol.upper() for keyword in STOCK_KEYWORDS):
            logger.debug(f"Пропускаем символ с ключевым словом: {symbol}")
            continue
        
        # Пропускаем если содержит цифры (например, токенизированные акции)
        if any(char.isdigit() for char in clean_symbol):
            logger.debug(f"Пропускаем символ с цифрами: {symbol}")
            continue
        
        filtered.append(symbol)
    
    logger.info(f"После фильтрации акций: {len(filtered)} из {len(symbols)}")
    return filtered


async def check_symbol_conditions(symbol: str) -> bool:
    """Проверяем условия для символа"""
    try:
        # 1. Проверяем блэк-лист
        if symbol in blacklist:
            logger.debug(f"Пропускаем {symbol}: в блэк-листе")
            return False
        
        # 2. Проверяем что это не акция
        clean_symbol = symbol.replace("USDT", "")
        if clean_symbol in STOCK_SYMBOLS:
            logger.debug(f"Пропускаем акцию: {symbol}")
            return False
        
        # 3. Проверяем что нет ключевых слов акций
        if any(keyword in symbol.upper() for keyword in STOCK_KEYWORDS):
            logger.debug(f"Пропускаем символ с ключевым словом: {symbol}")
            return False
        
        # 4. Проверяем что нет цифр в символе
        if any(char.isdigit() for char in clean_symbol):
            logger.debug(f"Пропускаем символ с цифрами: {symbol}")
            return False
        
        # 5. Проверяем 1D объём
        daily_volume = await get_1d_volume(symbol)
        if daily_volume > DAILY_VOLUME_LIMIT:
            logger.debug(f"Пропускаем {symbol}: объём {daily_volume:,.0f} > {DAILY_VOLUME_LIMIT:,}")
            return False
        
        # 6. Проверяем цену токена
        try:
            data = await get_1m_kline_data(symbol)
            if data:
                current_price = data["curr_price"]
                
                # Фильтр по цене
                if current_price < MIN_PRICE:
                    logger.debug(f"Пропускаем {symbol}: цена слишком низкая {current_price:.8f}")
                    return False
                elif current_price > MAX_PRICE:
                    logger.debug(f"Пропускаем {symbol}: цена слишком высокая {current_price:.4f}")
                    return False
        except Exception as e:
            logger.debug(f"Ошибка проверки цены для {symbol}: {e}")
            return False
        
        logger.debug(f"✓ {symbol}: объём {daily_volume:,.0f}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка проверки {symbol}: {e}")
        return False


# ====================== ЗАГРУЗКА И ФИЛЬТРАЦИЯ СИМВОЛОВ ======================
async def load_and_filter_symbols():
    """Загружаем и фильтруем символы по всем условиям"""
    global tracked_symbols
    
    logger.info("Начинаю загрузку и фильтрацию символов...")
    
    try:
        all_symbols = await get_all_futures_symbols()
        if not all_symbols:
            logger.error("Не удалось получить символы фьючерсов")
            return False
        
        logger.info(f"Получено {len(all_symbols)} символов. Начинаю фильтрацию...")
        
        # 1. Фильтруем акции
        filtered_symbols = filter_stock_symbols(all_symbols)
        
        # 2. Проверяем остальные условия для каждого символа
        low_volume_symbols = []
        total_symbols = len(filtered_symbols)
        batch_size = 20
        
        for i in range(0, total_symbols, batch_size):
            batch = filtered_symbols[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_symbols + batch_size - 1) // batch_size
            
            logger.info(f"Проверяю батч {batch_num}/{total_batches} ({len(batch)} символов)")
            
            tasks = []
            for symbol in batch:
                task = asyncio.create_task(check_symbol_conditions(symbol))
                tasks.append((symbol, task))
            
            for symbol, task in tasks:
                try:
                    if await task:
                        low_volume_symbols.append(symbol)
                        
                except Exception as e:
                    logger.error(f"Ошибка проверки {symbol}: {e}")
            
            # Пауза между батчами
            if i + batch_size < total_symbols:
                await asyncio.sleep(1)
        
        tracked_symbols = set(low_volume_symbols)
        
        logger.info(f"✅ ФИЛЬТРАЦИЯ ЗАВЕРШЕНА!")
        logger.info(f"   Всего символов: {len(all_symbols)}")
        logger.info(f"   После фильтра акций: {len(filtered_symbols)}")
        logger.info(f"   После всех фильтров: {len(tracked_symbols)}")
        logger.info(f"   В блэк-листе: {len(blacklist)}")
        
        if tracked_symbols:
            sample = list(tracked_symbols)[:15]
            
            # Отправляем уведомление без HTML
            try:
                await bot_instance.send_message(
                    chat_id=MY_USER_ID,
                    text=f"✅ Сканер запущен\n\n"
                         f"Отслеживается: {len(tracked_symbols)} пар\n"
                         f"В блэк-листе: {len(blacklist)} монет\n"
                         f"Уведомления отключены: {len(paused_alerts)} монет\n\n"
                         f"Фильтры:\n"
                         f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
                         f"• Цена: {MIN_PRICE:.4f} - {MAX_PRICE:.2f} USDT\n"
                         f"• Исключены акции\n\n"
                         f"Примеры:\n{', '.join(sample[:8])}"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление: {e}")
        
        return True
        
    except Exception as e:
        logger.error(f"Критическая ошибка при загрузке символов: {e}")
        return False


# ====================== ФУНКЦИИ УПРАВЛЕНИЯ ДАННЫМИ ======================
async def load_data_from_db():
    """Загружаем данные (упрощенная версия в памяти)"""
    global blacklist, paused_alerts, alert_history
    logger.info("Данные загружены из памяти")
    return True


async def save_alert_to_history(symbol: str, prev_volume: int, curr_volume: int, 
                               prev_price: float, curr_price: float, 
                               volume_change_pct: float, price_change_pct: float):
    """Сохраняем алерт в историю"""
    alert = {
        'symbol': symbol,
        'prev_volume': prev_volume,
        'curr_volume': curr_volume,
        'prev_price': prev_price,
        'curr_price': curr_price,
        'volume_change_pct': volume_change_pct,
        'price_change_pct': price_change_pct,
        'created_at': datetime.now()
    }
    alert_history.append(alert)
    # Храним только последние 1000 алертов
    if len(alert_history) > 1000:
        alert_history = alert_history[-1000:]


def get_recent_alerts(hours: int = 24):
    """Получить недавние алерты"""
    cutoff_time = datetime.now() - timedelta(hours=hours)
    return [alert for alert in alert_history if alert['created_at'] > cutoff_time]


async def toggle_pause_symbol(query, symbol: str):
    """Включить/выключить уведомления для монеты"""
    try:
        if symbol in paused_alerts:
            paused_alerts.remove(symbol)
            action = "включены"
        else:
            paused_alerts.add(symbol)
            action = "отключены"
        
        await query.edit_message_text(
            f"✅ Уведомления для {symbol} {action}"
        )
    except Exception as e:
        logger.error(f"Ошибка переключения паузы: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при изменении настроек"
        )


async def add_to_blacklist(query, symbol: str):
    """Добавить монету в блэк-лист"""
    try:
        if symbol in blacklist:
            await query.edit_message_text(
                f"ℹ️ {symbol} уже в блэк-листе"
            )
            return
        
        blacklist.add(symbol)
        
        # Удаляем из отслеживаемых
        if symbol in tracked_symbols:
            tracked_symbols.remove(symbol)
        
        # Удаляем из пауз
        if symbol in paused_alerts:
            paused_alerts.remove(symbol)
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✅ {symbol} добавлен в блэк-лист\n\n"
            f"Монета исключена из отслеживания",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Ошибка добавления в блэк-лист: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при добавлении в блэк-лист"
        )


async def remove_from_blacklist(query, symbol: str):
    """Удалить монету из блэк-листа"""
    try:
        if symbol not in blacklist:
            await query.edit_message_text(
                f"ℹ️ {symbol} нет в блэк-листе"
            )
            return
        
        blacklist.remove(symbol)
        
        await query.edit_message_text(
            f"✅ {symbol} удален из блэк-листа\n\n"
            f"Монета будет проверена при следующем обновлении списка"
        )
    except Exception as e:
        logger.error(f"Ошибка удаления из блэк-листа: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при удалении из блэк-листа"
        )


# ====================== СКАНЕР ======================
async def volume_spike_scanner():
    """Сканируем все низковольюмные пары на всплески объёма на 1m"""
    logger.info(f"🚀 Сканер запущен! Отслеживаю {len(tracked_symbols)} пар")
    
    if len(tracked_symbols) == 0:
        logger.warning("Нет пар для отслеживания!")
        return
    
    iteration = 0
    
    while True:
        try:
            current_minute = datetime.now().strftime("%Y%m%d%H%M")
            iteration += 1
            
            if iteration % 10 == 1:
                logger.info(f"Итерация {iteration}. Пар: {len(tracked_symbols)}")
            
            # Обновляем список символов каждые 6 часов
            if iteration % 720 == 0:
                logger.info("🔄 Обновляю список символов (каждые 6 часов)...")
                await load_and_filter_symbols()
                continue
            
            symbols_list = list(tracked_symbols)
            if not symbols_list:
                await asyncio.sleep(60)
                continue
            
            max_per_iteration = min(80, len(symbols_list))
            random.shuffle(symbols_list)
            
            for symbol in symbols_list[:max_per_iteration]:
                try:
                    # Пропускаем если уведомления отключены
                    if symbol in paused_alerts:
                        continue
                    
                    data = await get_1m_kline_data(symbol)
                    if not data:
                        continue
                    
                    prev_vol = data["prev_volume"]
                    curr_vol = data["curr_volume"]
                    prev_price = data["prev_price"]
                    curr_price = data["curr_price"]
                    
                    # Проверяем условие всплеска
                    if prev_vol < MIN_PREV_VOLUME and curr_vol > MIN_CURRENT_VOLUME:
                        alert_id = f"{symbol}_{current_minute}"
                        
                        if alert_id in sent_alerts:
                            continue
                        
                        volume_change_pct = ((curr_vol - prev_vol) / max(prev_vol, 1)) * 100
                        if prev_price > 0:
                            price_change_pct = ((curr_price - prev_price) / prev_price) * 100
                        else:
                            price_change_pct = 0
                        
                        if volume_change_pct < 50:
                            continue
                        
                        # Сохраняем алерт в историю
                        await save_alert_to_history(
                            symbol, prev_vol, curr_vol, 
                            prev_price, curr_price,
                            volume_change_pct, price_change_pct
                        )
                        
                        # Создаем клавиатуру
                        keyboard = [
                            [
                                InlineKeyboardButton("🔕 Выключить увед.", callback_data=f"pause_{symbol}"),
                                InlineKeyboardButton("🚫 В блэк-лист", callback_data=f"blacklist_{symbol}")
                            ]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        # Сообщение без HTML тегов
                        message = (
                            f"⚡ {symbol}\n"
                            f"Объём: {prev_vol:,} → {curr_vol:,} USDT\n"
                            f"Изменение: {volume_change_pct:+.0f}%\n"
                            f"Цена: {price_change_pct:+.2f}%\n"
                            f"https://www.mexc.com/futures/{symbol[:-4]}_USDT"
                        )
                        
                        try:
                            await bot_instance.send_message(
                                chat_id=MY_USER_ID,
                                text=message,
                                disable_web_page_preview=True,
                                reply_markup=reply_markup
                            )
                            
                            sent_alerts[alert_id] = time.time()
                            logger.info(f"🚨 {symbol} | {prev_vol:,}→{curr_vol:,} (+{volume_change_pct:.0f}%)")
                            
                        except Exception as e:
                            logger.error(f"Ошибка отправки: {e}")
                            
                except Exception as e:
                    logger.debug(f"Ошибка обработки {symbol}: {str(e)[:100]}")
                    continue
            
            # Очищаем старые алерты
            current_time = time.time()
            expired = [k for k, v in sent_alerts.items() if current_time - v > 7200]
            for exp in expired:
                sent_alerts.pop(exp, None)
            
            await asyncio.sleep(35)
            
        except asyncio.CancelledError:
            logger.info("Сканер остановлен")
            break
        except Exception as e:
            logger.error(f"Ошибка в сканере: {e}")
            await asyncio.sleep(60)


# ====================== TELEGRAM КОМАНДЫ И КНОПКИ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return
    
    keyboard = [
        [InlineKeyboardButton("📋 Список пар", callback_data="list_symbols")],
        [InlineKeyboardButton("🚫 Блэк-лист", callback_data="blacklist_menu")],
        [InlineKeyboardButton("🔕 Паузы", callback_data="paused_menu")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "📊 MEXC Volume Scanner\n\n"
        f"Статус: ✅ Активен\n"
        f"Отслеживаемых пар: {len(tracked_symbols)}\n"
        f"В блэк-листе: {len(blacklist)} монет\n"
        f"Уведомления отключены: {len(paused_alerts)} монет\n\n"
        f"Фильтры:\n"
        f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"• Цена: {MIN_PRICE:.4f} - {MAX_PRICE:.2f} USDT\n\n"
        f"Выберите действие:"
    )
    
    await update.message.reply_text(
        text,
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id != MY_USER_ID:
        return
    
    data = query.data
    
    if data == "list_symbols":
        await show_symbols_list(query)
    
    elif data == "blacklist_menu":
        await show_blacklist_menu(query)
    
    elif data == "paused_menu":
        await show_paused_menu(query)
    
    elif data == "stats":
        await stats_db_query(query)
    
    elif data == "refresh":
        await refresh_symbols(query)
    
    elif data.startswith("pause_"):
        symbol = data.replace("pause_", "")
        await toggle_pause_symbol(query, symbol)
    
    elif data.startswith("blacklist_"):
        symbol = data.replace("blacklist_", "")
        await add_to_blacklist(query, symbol)
    
    elif data.startswith("remove_blacklist_"):
        symbol = data.replace("remove_blacklist_", "")
        await remove_from_blacklist(query, symbol)
    
    elif data == "back":
        await start_callback(query)


async def start_callback(query):
    """Обработчик команды start для callback"""
    keyboard = [
        [InlineKeyboardButton("📋 Список пар", callback_data="list_symbols")],
        [InlineKeyboardButton("🚫 Блэк-лист", callback_data="blacklist_menu")],
        [InlineKeyboardButton("🔕 Паузы", callback_data="paused_menu")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "📊 MEXC Volume Scanner\n\n"
        f"Статус: ✅ Активен\n"
        f"Отслеживаемых пар: {len(tracked_symbols)}\n"
        f"В блэк-листе: {len(blacklist)} монет\n"
        f"Уведомления отключены: {len(paused_alerts)} монет\n\n"
        f"Фильтры:\n"
        f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"• Цена: {MIN_PRICE:.4f} - {MAX_PRICE:.2f} USDT\n\n"
        f"Выберите действие:"
    )
    
    await query.edit_message_text(
        text,
        reply_markup=reply_markup
    )


async def show_symbols_list(query):
    """Показать список отслеживаемых пар"""
    if not tracked_symbols:
        await query.edit_message_text("ℹ️ Нет отслеживаемых пар")
        return
    
    symbols_list = sorted(list(tracked_symbols))
    
    # Показываем первые 20 символов
    symbols_text = "\n".join([f"• {symbol}" for symbol in symbols_list[:20]])
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"📋 Отслеживаемые пары\n\n"
        f"Всего: {len(tracked_symbols)} пар\n\n"
        f"{symbols_text}\n\n"
        f"Показано {min(20, len(symbols_list))} из {len(symbols_list)}",
        reply_markup=reply_markup
    )


async def show_blacklist_menu(query):
    """Показать меню блэк-листа"""
    if not blacklist:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🚫 Блэк-лист\n\n"
            f"В блэк-листе нет монет",
            reply_markup=reply_markup
        )
        return
    
    blacklist_list = sorted(list(blacklist))
    blacklist_text = "\n".join([f"• {symbol}" for symbol in blacklist_list[:15]])
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🚫 Блэк-лист\n\n"
        f"Всего: {len(blacklist)} монет\n\n"
        f"{blacklist_text}\n\n"
        f"Показано {min(15, len(blacklist_list))} из {len(blacklist_list)}",
        reply_markup=reply_markup
    )


async def show_paused_menu(query):
    """Показать меню отключенных уведомлений"""
    if not paused_alerts:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🔕 Отключенные уведомления\n\n"
            f"Нет отключенных уведомлений",
            reply_markup=reply_markup
        )
        return
    
    paused_list = sorted(list(paused_alerts))
    paused_text = "\n".join([f"• {symbol}" for symbol in paused_list[:15]])
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🔕 Отключенные уведомления\n\n"
        f"Всего: {len(paused_alerts)} монет\n\n"
        f"{paused_text}\n\n"
        f"Показано {min(15, len(paused_list))} из {len(paused_list)}",
        reply_markup=reply_markup
    )


async def refresh_symbols(query):
    """Обновить список пар"""
    await query.edit_message_text("🔄 Обновляю список пар...")
    
    success = await load_and_filter_symbols()
    
    if success:
        await query.edit_message_text(
            f"✅ Обновлено!\n"
            f"Отслеживается: {len(tracked_symbols)} пар\n"
            f"В блэк-листе: {len(blacklist)} монет"
        )
    else:
        await query.edit_message_text("❌ Ошибка обновления")


async def stats_db_query(query):
    """Статистика через callback"""
    try:
        recent_alerts = get_recent_alerts(24)
        
        alert_count = len(recent_alerts)
        unique_symbols = len(set([alert['symbol'] for alert in recent_alerts]))
        
        # Находим самые активные монеты
        symbol_counts = {}
        for alert in recent_alerts:
            symbol = alert['symbol']
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        
        top_symbols = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        stats_text = "📊 Статистика за 24ч\n\n"
        stats_text += f"Всего алертов: {alert_count}\n"
        stats_text += f"Уникальных пар: {unique_symbols}\n"
        stats_text += f"В блэк-листе: {len(blacklist)}\n"
        stats_text += f"Пауз уведомлений: {len(paused_alerts)}\n\n"
        
        if top_symbols:
            stats_text += "Топ-5 активных пар:\n"
            for symbol, count in top_symbols:
                stats_text += f"• {symbol}: {count} алертов\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(stats_text, reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await query.edit_message_text("❌ Ошибка получения статистики")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда статистики"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    try:
        recent_alerts = get_recent_alerts(24)
        
        alert_count = len(recent_alerts)
        unique_symbols = len(set([alert['symbol'] for alert in recent_alerts]))
        
        stats_text = (
            "📊 Статистика за 24ч\n\n"
            f"Всего алертов: {alert_count}\n"
            f"Уникальных пар: {unique_symbols}\n"
            f"Отслеживаемых пар: {len(tracked_symbols)}\n"
            f"В блэк-листе: {len(blacklist)}\n"
            f"Пауз уведомлений: {len(paused_alerts)}\n\n"
            f"Время: {datetime.now().strftime('%H:%M:%S')}"
        )
        
        await update.message.reply_text(stats_text)
        
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await update.message.reply_text("❌ Ошибка получения статистики")


async def run_telegram_polling():
    """Запуск Telegram polling"""
    try:
        await application.initialize()
        await application.start()
        logger.info("Telegram бот готов к работе")
        await application.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Ошибка запуска Telegram бота: {e}")


# ====================== ЗАПУСК ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner_task, application, bot_instance
    
    logger.info("=== Запуск MEXC Volume Scanner ===")
    
    # Проверка токена
    logger.info(f"TELEGRAM_TOKEN: {'*' * len(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else 'НЕ УСТАНОВЛЕН'}")
    logger.info(f"MY_USER_ID: {MY_USER_ID}")
    
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "ваш_токен_бота":
        logger.error("❌ TELEGRAM_TOKEN не установлен!")
        raise ValueError("TELEGRAM_TOKEN не установлен")
    
    if not MY_USER_ID:
        logger.error("❌ MY_USER_ID не установлен!")
        raise ValueError("MY_USER_ID не установлен")
    
    # Создаем экземпляр бота
    try:
        bot_instance = Bot(token=TELEGRAM_TOKEN)
        logger.info("✅ Telegram бот создан успешно")
    except Exception as e:
        logger.error(f"❌ Ошибка создания бота: {e}")
        raise
    
    # Загружаем данные
    await load_data_from_db()
    
    # Инициализируем Telegram приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Загружаем и фильтруем символы
    await load_and_filter_symbols()
    
    # Запускаем сканер
    scanner_task = asyncio.create_task(volume_spike_scanner())
    logger.info("✅ Сканер запущен")
    
    # Запускаем Telegram polling
    asyncio.create_task(run_telegram_polling())
    
    yield
    
    logger.info("=== Остановка приложения ===")
    
    if scanner_task:
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass
    
    if application:
        await application.shutdown()
        await application.stop()


# ====================== FASTAPI ======================
app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "service": "MEXC Volume Scanner",
        "status": "active",
        "timestamp": datetime.now().isoformat(),
        "tracked_pairs": len(tracked_symbols),
        "blacklist_count": len(blacklist),
        "paused_count": len(paused_alerts),
        "recent_alerts": len([v for v in sent_alerts.values() if time.time() - v < 7200])
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "Mexcnewbot:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )























