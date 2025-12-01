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

MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY", "")

DAILY_VOLUME_LIMIT = 500_000
MIN_PREV_VOLUME = 1000      # Объем за предыдущие 5 минут
MIN_CURRENT_VOLUME = 4000   # Объем за текущие 5 минут
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


async def get_5m_kline_data(symbol: str):
    """Получаем данные за последние 10 свечей на 5-минутном таймфрейме (50 минут)"""
    api_symbol = symbol.replace("USDT", "_USDT")
    timestamp = str(int(time.time() * 1000))
    
    query_string = f"symbol={api_symbol}&interval=Min5&limit=10"
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
                    "interval": "Min5",
                    "limit": 10
                },
                headers=headers,
                timeout=10
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    if data.get("success") and "data" in data:
                        kline_data = data["data"]
                        
                        if len(kline_data.get("close", [])) >= 2:
                            # Суммируем объем за последние 5 минут (текущая свеча)
                            curr_volume = int(float(kline_data["amount"][-1]))
                            curr_close = float(kline_data["close"][-1])
                            
                            # Суммируем объем за предыдущие 5 минут (предыдущая свеча)
                            prev_volume = int(float(kline_data["amount"][-2]))
                            prev_close = float(kline_data["close"][-2])
                            
                            return {
                                "prev_volume": prev_volume,
                                "curr_volume": curr_volume,
                                "prev_price": prev_close,
                                "curr_price": curr_close,
                                "symbol": symbol
                            }
                
    except Exception as e:
        logger.debug(f"Ошибка 5m данных для {symbol}: {str(e)[:100]}")
    
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
            data = await get_5m_kline_data(symbol)
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
            logger.info(f"   Примеры: {', '.join(sample)}")
            
            # Отправляем уведомление без HTML
            try:
                if bot_instance:
                    await bot_instance.send_message(
                        chat_id=MY_USER_ID,
                        text=f"✅ Сканер запущен\n\n"
                             f"Отслеживается: {len(tracked_symbols)} пар\n"
                             f"В блэк-листе: {len(blacklist)} монет\n"
                             f"Уведомления отключены: {len(paused_alerts)} монет\n\n"
                             f"Фильтры:\n"
                             f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
                             f"• Пред. 5 мин < {MIN_PREV_VOLUME} USDT\n"
                             f"• Тек. 5 мин > {MIN_CURRENT_VOLUME} USDT\n"
                             f"• Цена: {MIN_PRICE:.4f} - {MAX_PRICE:.2f} USDT\n"
                             f"• Исключены акции\n\n"
                             f"Примеры:\n{', '.join(sample[:8])}"
                    )
                    logger.info("✅ Стартовое сообщение отправлено")
                else:
                    logger.error("❌ bot_instance не инициализирован для отправки стартового сообщения")
            except Exception as e:
                logger.error(f"❌ Не удалось отправить стартовое уведомление: {e}")
        
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


# ====================== СКАНЕР (5-минутные интервалы) ======================
async def volume_spike_scanner():
    """Сканируем все низковольюмные пары на всплески объёма на 5m"""
    logger.info(f"🚀 Сканер запущен! Отслеживаю {len(tracked_symbols)} пар")
    logger.info(f"Ваш USER_ID: {MY_USER_ID}")
    logger.info(f"Условия: Пред. 5 мин < {MIN_PREV_VOLUME}, Тек. 5 мин > {MIN_CURRENT_VOLUME}")
    
    if len(tracked_symbols) == 0:
        logger.warning("Нет пар для отслеживания!")
        return
    
    iteration = 0
    
    while True:
        try:
            current_5min = datetime.now().strftime("%Y%m%d%H%M")[:11] + str(int(datetime.now().minute / 5) * 5).zfill(2)
            iteration += 1
            
            if iteration % 5 == 1:
                logger.info(f"Итерация {iteration}. Пар: {len(tracked_symbols)}. Алертов за сессию: {len(sent_alerts)}")
            
            # Обновляем список символов каждые 6 часов
            if iteration % 432 == 0:  # Каждые 6 часов (при проверке каждые 50 секунд)
                logger.info("🔄 Обновляю список символов (каждые 6 часов)...")
                await load_and_filter_symbols()
                continue
            
            symbols_list = list(tracked_symbols)
            if not symbols_list:
                logger.warning("Нет символов для сканирования")
                await asyncio.sleep(60)
                continue
            
            # Сканируем все символы
            max_per_iteration = len(symbols_list)
            random.shuffle(symbols_list)
            
            for symbol in symbols_list[:max_per_iteration]:
                try:
                    # Пропускаем если уведомления отключены
                    if symbol in paused_alerts:
                        continue
                    
                    data = await get_5m_kline_data(symbol)
                    if not data:
                        continue
                    
                    prev_vol = data["prev_volume"]
                    curr_vol = data["curr_volume"]
                    prev_price = data["prev_price"]
                    curr_price = data["curr_price"]
                    
                    # Проверяем условие всплеска за 5 минут
                    if prev_vol < MIN_PREV_VOLUME and curr_vol > MIN_CURRENT_VOLUME:
                        alert_id = f"{symbol}_{current_5min}"
                        
                        if alert_id in sent_alerts:
                            logger.debug(f"Алерт {symbol} уже отправлен в этой 5-минутке")
                            continue
                        
                        volume_change_pct = ((curr_vol - prev_vol) / max(prev_vol, 1)) * 100
                        if prev_price > 0:
                            price_change_pct = ((curr_price - prev_price) / prev_price) * 100
                        else:
                            price_change_pct = 0
                        
                        # ВСЕ УСЛОВИЯ ВЫПОЛНЕНЫ - ОТПРАВЛЯЕМ АЛЕРТ
                        logger.info(f"🚨 АЛЕРТ НАЙДЕН: {symbol}")
                        logger.info(f"   Пред. 5 мин: {prev_vol:,} USDT ( < {MIN_PREV_VOLUME})")
                        logger.info(f"   Тек. 5 мин: {curr_vol:,} USDT ( > {MIN_CURRENT_VOLUME})")
                        logger.info(f"   Изменение: +{volume_change_pct:.0f}%")
                        logger.info(f"   Изменение цены: {price_change_pct:+.2f}%")
                        
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
                            f"⚡ 5-МИНУТНЫЙ АЛЕРТ: {symbol}\n"
                            f"Объём за 5 мин: {prev_vol:,} → {curr_vol:,} USDT\n"
                            f"Изменение: {volume_change_pct:+.0f}%\n"
                            f"Цена: {price_change_pct:+.2f}%\n"
                            f"https://www.mexc.com/futures/{symbol[:-4]}_USDT"
                        )
                        
                        try:
                            # Логируем попытку отправки
                            logger.info(f"📤 Пытаюсь отправить алерт {symbol}")
                            logger.info(f"   Chat ID: {MY_USER_ID}")
                            
                            # Основной способ: Создаем нового бота для отправки
                            temp_bot = Bot(token=TELEGRAM_TOKEN)
                            
                            # Отправляем сообщение
                            result = await temp_bot.send_message(
                                chat_id=MY_USER_ID,
                                text=message,
                                disable_web_page_preview=True,
                                reply_markup=reply_markup
                            )
                            
                            logger.info(f"✅ АЛЕРТ УСПЕШНО ОТПРАВЛЕН: {symbol}")
                            logger.info(f"   Message ID: {result.message_id}")
                            sent_alerts[alert_id] = time.time()
                            
                        except Exception as e:
                            logger.error(f"❌ ОШИБКА ОТПРАВКИ АЛЕРТА {symbol}:")
                            logger.error(f"   Тип ошибки: {type(e).__name__}")
                            logger.error(f"   Сообщение: {str(e)}")
                            logger.error(f"   Chat ID: {MY_USER_ID}")
                            
                            # Пробуем упрощенное сообщение без кнопок
                            try:
                                logger.info(f"   Пробую упрощенную отправку...")
                                temp_bot = Bot(token=TELEGRAM_TOKEN)
                                simple_msg = f"⚡ {symbol} | 5 мин: {prev_vol:,}→{curr_vol:,} (+{volume_change_pct:.0f}%)"
                                await temp_bot.send_message(
                                    chat_id=MY_USER_ID,
                                    text=simple_msg,
                                    disable_web_page_preview=True
                                )
                                logger.info(f"✅ Упрощенный алерт отправлен: {symbol}")
                                sent_alerts[alert_id] = time.time()
                            except Exception as e2:
                                logger.error(f"❌ Ошибка упрощенной отправки: {e2}")
                            
                except Exception as e:
                    logger.error(f"Ошибка обработки {symbol}: {str(e)}")
                    continue
            
            # Очищаем старые алерты
            current_time = time.time()
            expired = [k for k, v in sent_alerts.items() if current_time - v > 7200]
            for exp in expired:
                sent_alerts.pop(exp, None)
            
            await asyncio.sleep(50)  # Проверяем каждые 50 секунд (чуть меньше 5 минут)
            
        except asyncio.CancelledError:
            logger.info("Сканер остановлен")
            break
        except Exception as e:
            logger.error(f"Ошибка в сканере: {e}")
            await asyncio.sleep(60)


# ====================== TELEGRAM КОМАНДЫ И КНОПКИ ======================
async def safe_reply(update: Update, text: str):
    """Безопасная отправка сообщения"""
    try:
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(text)
        elif bot_instance:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text=text
            )
        else:
            logger.error(f"Не удалось отправить сообщение: {text}")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        await safe_reply(update, "🚫 Доступ запрещён")
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
        "📊 MEXC 5-MIN Volume Scanner\n\n"
        f"Статус: ✅ Активен\n"
        f"Отслеживаемых пар: {len(tracked_symbols)}\n"
        f"В блэк-листе: {len(blacklist)} монет\n"
        f"Уведомления отключены: {len(paused_alerts)} монет\n\n"
        f"Фильтры:\n"
        f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"• Пред. 5 мин < {MIN_PREV_VOLUME} USDT\n"
        f"• Тек. 5 мин > {MIN_CURRENT_VOLUME} USDT\n"
        f"• Цена: {MIN_PRICE:.4f} - {MAX_PRICE:.2f} USDT\n\n"
        f"Выберите действие:"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif bot_instance:
        await bot_instance.send_message(
            chat_id=MY_USER_ID,
            text=text,
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
        "📊 MEXC 5-MIN Volume Scanner\n\n"
        f"Статус: ✅ Активен\n"
        f"Отслеживаемых пар: {len(tracked_symbols)}\n"
        f"В блэк-листе: {len(blacklist)} монет\n"
        f"Уведомления отключены: {len(paused_alerts)} монет\n\n"
        f"Фильтры:\n"
        f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"• Пред. 5 мин < {MIN_PREV_VOLUME} USDT\n"
        f"• Тек. 5 мин > {MIN_CURRENT_VOLUME} USDT\n"
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


# ====================== ОТЛАДОЧНЫЕ КОМАНДЫ ======================
async def env_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка переменных окружения"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    check_text = (
        f"🔍 Проверка переменных окружения:\n\n"
        f"TELEGRAM_TOKEN: {'УСТАНОВЛЕН' if TELEGRAM_TOKEN and TELEGRAM_TOKEN != 'ваш_токен_бота' else '❌ НЕ УСТАНОВЛЕН'}\n"
        f"MY_USER_ID: {MY_USER_ID}\n"
        f"MEXC_API_KEY: {'УСТАНОВЛЕН' if MEXC_API_KEY else 'НЕ УСТАНОВЛЕН'}\n"
        f"MEXC_SECRET_KEY: {'УСТАНОВЛЕН' if MEXC_SECRET_KEY else 'НЕ УСТАНОВЛЕН'}\n\n"
        f"Текущий user_id: {update.effective_user.id}\n"
        f"Совпадает с MY_USER_ID: {'✅ ДА' if update.effective_user.id == MY_USER_ID else '❌ НЕТ'}\n\n"
        f"Параметры сканера:\n"
        f"MIN_PREV_VOLUME (пред. 5 мин): {MIN_PREV_VOLUME}\n"
        f"MIN_CURRENT_VOLUME (тек. 5 мин): {MIN_CURRENT_VOLUME}\n"
        f"DAILY_VOLUME_LIMIT: {DAILY_VOLUME_LIMIT:,}"
    )
    
    if update.message:
        await update.message.reply_text(check_text)
    elif bot_instance:
        await bot_instance.send_message(chat_id=MY_USER_ID, text=check_text)


async def test_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Протестировать конкретный символ"""
    if update.effective_user.id != MY_USER_ID:
        # Если нет сообщения, отправляем через бота
        if bot_instance:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text="🚫 Доступ запрещён"
            )
        return
    
    if not context.args:
        # Проверяем, есть ли message для ответа
        if update.message:
            await update.message.reply_text("Укажите символ: /test BTCUSDT")
        elif bot_instance:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text="Укажите символ: /test BTCUSDT"
            )
        return
    
    symbol = context.args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    
    # Отправляем сообщение о начале теста
    if update.message:
        await update.message.reply_text(f"Тестирую {symbol}...")
    elif bot_instance:
        await bot_instance.send_message(
            chat_id=MY_USER_ID,
            text=f"Тестирую {symbol}..."
        )
    
    try:
        # Проверяем 1D объем
        daily_volume = await get_1d_volume(symbol)
        
        # Проверяем 5m данные
        data = await get_5m_kline_data(symbol)
        
        message = f"📊 {symbol} (5-минутный интервал)\n\n"
        
        if data:
            message += (
                f"5m данные:\n"
                f"• Пред. 5 мин объем: {data['prev_volume']:,}\n"
                f"• Тек. 5 мин объем: {data['curr_volume']:,}\n"
                f"• Пред. цена: {data['prev_price']:.8f}\n"
                f"• Тек. цена: {data['curr_price']:.8f}\n\n"
            )
            
            # Проверяем условия
            conditions = []
            
            if data['prev_volume'] < MIN_PREV_VOLUME:
                conditions.append(f"✓ Пред. 5 мин < {MIN_PREV_VOLUME}")
            else:
                conditions.append(f"✗ Пред. 5 мин > {MIN_PREV_VOLUME}")
                
            if data['curr_volume'] > MIN_CURRENT_VOLUME:
                conditions.append(f"✓ Тек. 5 мин > {MIN_CURRENT_VOLUME}")
            else:
                conditions.append(f"✗ Тек. 5 мин < {MIN_CURRENT_VOLUME}")
                
            volume_change_pct = ((data['curr_volume'] - data['prev_volume']) / max(data['prev_volume'], 1)) * 100
            conditions.append(f"Изменение: {volume_change_pct:.0f}%")
            
            message += "Условия для алерта:\n" + "\n".join(f"• {c}" for c in conditions)
        else:
            message += "❌ Нет 5m данных\n"
        
        message += f"\n1D объем: {daily_volume:,.0f} USDT"
        
        if daily_volume > DAILY_VOLUME_LIMIT:
            message += f" ( > {DAILY_VOLUME_LIMIT:,} - ПРОПУСК)"
        else:
            message += f" ( < {DAILY_VOLUME_LIMIT:,} - ОК)"
        
        # Отправляем результат
        if update.message:
            await update.message.reply_text(message)
        elif bot_instance:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text=message
            )
        
    except Exception as e:
        error_msg = f"Ошибка: {str(e)}"
        if update.message:
            await update.message.reply_text(error_msg)
        elif bot_instance:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text=error_msg
            )


async def send_test_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправить тестовый алерт прямо сейчас"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    test_symbol = "HIPPOUSDT" if not context.args else context.args[0].upper()
    
    try:
        # Создаем тестовый алерт
        message = (
            f"⚡ ТЕСТОВЫЙ АЛЕРТ (5-минутный): {test_symbol}\n"
            f"Объём за 5 мин: 61 → 6,438 USDT\n"
            f"Изменение: +10454%\n"
            f"Цена: -0.10%\n"
            f"https://www.mexc.com/futures/{test_symbol[:-4]}_USDT"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("🔕 Выключить увед.", callback_data=f"pause_{test_symbol}"),
                InlineKeyboardButton("🚫 В блэк-лист", callback_data=f"blacklist_{test_symbol}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Пробуем все способы отправки
        methods = []
        
        # Способ 1: через reply
        if update.message:
            try:
                await update.message.reply_text(message, reply_markup=reply_markup, disable_web_page_preview=True)
                methods.append("reply_text")
            except Exception as e:
                logger.error(f"Ошибка reply_text: {e}")
        
        # Способ 2: через создание нового бота
        try:
            temp_bot = Bot(token=TELEGRAM_TOKEN)
            await temp_bot.send_message(
                chat_id=MY_USER_ID,
                text=message,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            methods.append("новый бот")
        except Exception as e:
            logger.error(f"Ошибка нового бота: {e}")
        
        # Способ 3: через bot_instance
        if bot_instance:
            try:
                await bot_instance.send_message(
                    chat_id=MY_USER_ID,
                    text=message,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
                methods.append("bot_instance")
            except Exception as e:
                logger.error(f"Ошибка bot_instance: {e}")
        
        # Отправляем отчет
        report = f"Тестовый алерт отправлен для {test_symbol}\nИспользованные методы: {', '.join(methods) if methods else 'ни один не сработал'}"
        
        if update.message:
            await update.message.reply_text(report)
        elif bot_instance:
            await bot_instance.send_message(chat_id=MY_USER_ID, text=report)
        
    except Exception as e:
        logger.error(f"Ошибка в send_test_alert: {e}")


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для отладки"""
    if update.effective_user.id != MY_USER_ID:
        if bot_instance:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text="🚫 Попытка доступа от постороннего пользователя"
            )
        return
    
    # Получаем статистику по текущим символам
    sample_symbols = list(tracked_symbols)[:5] if tracked_symbols else []
    
    debug_info = (
        f"🔧 Отладка 5-минутного сканера\n\n"
        f"Всего пар: {len(tracked_symbols)}\n"
        f"В блэк-листе: {len(blacklist)}\n"
        f"Паузы: {len(paused_alerts)}\n"
        f"Алертов за сессию: {len(sent_alerts)}\n\n"
        f"Примеры пар ({len(sample_symbols)}):\n"
    )
    
    # Проверяем несколько символов
    for symbol in sample_symbols:
        try:
            data = await get_5m_kline_data(symbol)
            if data:
                debug_info += f"• {symbol}: {data['prev_volume']:,} → {data['curr_volume']:,} USDT за 5 мин\n"
            else:
                debug_info += f"• {symbol}: нет данных\n"
        except:
            debug_info += f"• {symbol}: ошибка\n"
    
    debug_info += f"\nФильтры:\n"
    debug_info += f"MIN_PREV_VOLUME (пред. 5 мин): {MIN_PREV_VOLUME}\n"
    debug_info += f"MIN_CURRENT_VOLUME (тек. 5 мин): {MIN_CURRENT_VOLUME}\n"
    debug_info += f"DAILY_VOLUME_LIMIT: {DAILY_VOLUME_LIMIT:,}\n"
    debug_info += f"MY_USER_ID: {MY_USER_ID}\n"
    
    # Отправляем сообщение
    if update.message:
        await update.message.reply_text(debug_info)
    elif bot_instance:
        await bot_instance.send_message(
            chat_id=MY_USER_ID,
            text=debug_info
        )
    else:
        logger.error("Не удалось отправить debug сообщение - нет доступных методов")


async def test_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тест отправки сообщения ботом"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    test_message = "🤖 Тестовое сообщение от бота\nВремя: " + datetime.now().strftime("%H:%M:%S")
    
    # Способ 1: через reply
    try:
        await update.message.reply_text("Тест 1: reply_text")
        logger.info("✅ Тест 1: reply_text - успешно")
    except Exception as e:
        logger.error(f"❌ Тест 1: reply_text - ошибка: {e}")
    
    # Способ 2: через новый бот
    try:
        temp_bot = Bot(token=TELEGRAM_TOKEN)
        await temp_bot.send_message(
            chat_id=MY_USER_ID,
            text="Тест 2: через новый бот"
        )
        logger.info("✅ Тест 2: через новый бот - успешно")
    except Exception as e:
        logger.error(f"❌ Тест 2: через новый бот - ошибка: {e}")
    
    # Способ 3: через bot_instance
    if bot_instance:
        try:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text="Тест 3: через bot_instance"
            )
            logger.info("✅ Тест 3: через bot_instance - успешно")
        except Exception as e:
            logger.error(f"❌ Тест 3: через bot_instance - ошибка: {e}")
    else:
        logger.error("❌ bot_instance не инициализирован")
    
    await update.message.reply_text("✅ Тесты отправки завершены. Проверьте логи.")


async def force_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительно отправить алерт для символа прямо сейчас"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    if not context.args:
        await update.message.reply_text("Укажите символ: /forcealert CHFUSDT")
        return
    
    symbol = context.args[0].upper()
    
    try:
        # Получаем данные
        data = await get_5m_kline_data(symbol)
        if not data:
            await update.message.reply_text(f"❌ Нет данных для {symbol}")
            return
        
        prev_vol = data["prev_volume"]
        curr_vol = data["curr_volume"]
        prev_price = data["prev_price"]
        curr_price = data["curr_price"]
        
        volume_change_pct = ((curr_vol - prev_vol) / max(prev_vol, 1)) * 100
        if prev_price > 0:
            price_change_pct = ((curr_price - prev_price) / prev_price) * 100
        else:
            price_change_pct = 0
        
        # Создаем алерт как в сканере
        alert_id = f"{symbol}_force_{datetime.now().strftime('%Y%m%d%H%M')}"
        
        keyboard = [
            [
                InlineKeyboardButton("🔕 Выключить увед.", callback_data=f"pause_{symbol}"),
                InlineKeyboardButton("🚫 В блэк-лист", callback_data=f"blacklist_{symbol}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = (
            f"⚡ ПРИНУДИТЕЛЬНЫЙ АЛЕРТ: {symbol}\n"
            f"Объём за 5 мин: {prev_vol:,} → {curr_vol:,} USDT\n"
            f"Изменение: {volume_change_pct:+.0f}%\n"
            f"Цена: {price_change_pct:+.2f}%\n"
            f"https://www.mexc.com/futures/{symbol[:-4]}_USDT"
        )
        
        # Отправляем
        temp_bot = Bot(token=TELEGRAM_TOKEN)
        result = await temp_bot.send_message(
            chat_id=MY_USER_ID,
            text=message,
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )
        
        await update.message.reply_text(f"✅ Принудительный алерт отправлен для {symbol}\nMessage ID: {result.message_id}")
        
        # Сохраняем в историю
        await save_alert_to_history(
            symbol, prev_vol, curr_vol, 
            prev_price, curr_price,
            volume_change_pct, price_change_pct
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка в force_alert: {e}")


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
    
    logger.info("=== Запуск MEXC 5-MIN Volume Scanner ===")
    
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
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("test", test_symbol))
    application.add_handler(CommandHandler("env", env_check))
    application.add_handler(CommandHandler("testalert", send_test_alert))
    application.add_handler(CommandHandler("testbot", test_bot))
    application.add_handler(CommandHandler("forcealert", force_alert))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Загружаем и фильтруем символы
    await load_and_filter_symbols()
    
    # Запускаем сканер
    scanner_task = asyncio.create_task(volume_spike_scanner())
    logger.info("✅ 5-минутный сканер запущен")
    
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
        "service": "MEXC 5-MIN Volume Scanner",
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
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Запуск сервера на порту {port}")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        reload=False
    )


























