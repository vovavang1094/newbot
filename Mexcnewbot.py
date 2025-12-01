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
from datetime import datetime

# Импортируем базу данных
from database import db

# ====================== НАСТРОЙКИ ======================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID", 0))

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

DAILY_VOLUME_LIMIT = 500_000
MIN_PREV_VOLUME = 1000
MIN_CURRENT_VOLUME = 2500

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

async def get_market_cap_coingecko(symbol: str) -> float:
    """Получаем рыночную капитализацию через CoinGecko API"""
    try:
        clean_symbol = symbol.replace("USDT", "").lower()
        
        async with aiohttp.ClientSession() as session:
            # Сначала ищем ID монеты
            async with session.get(
                f"https://api.coingecko.com/api/v3/search?query={clean_symbol}",
                timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if data.get("coins") and len(data["coins"]) > 0:
                        # Берем первую найденную монету
                        coin_id = data["coins"][0]["id"]
                        
                        # Получаем детальную информацию
                        async with session.get(
                            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
                            timeout=10
                        ) as detail_response:
                            if detail_response.status == 200:
                                coin_data = await detail_response.json()
                                market_cap = coin_data.get("market_data", {}).get("market_cap", {}).get("usd", 0)
                                return float(market_cap)
        
        return 0
    except Exception as e:
        logger.debug(f"Ошибка получения кап из CoinGecko для {symbol}: {e}")
        return 0

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
        
        # 6. Пробуем получить рыночную капитализацию
        market_cap = await get_market_cap(symbol)
        MAX_MARKET_CAP = 80_000  # USDT
        
        if market_cap > MAX_MARKET_CAP and market_cap > 0:
            logger.debug(f"Пропускаем {symbol}: капитализация {market_cap:,.0f} > {MAX_MARKET_CAP:,}")
            return False
        
        logger.debug(f"✓ {symbol}: объём {daily_volume:,.0f}, кап {market_cap:,.0f}")
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
            
            try:
                await bot_instance.send_message(
                    chat_id=MY_USER_ID,
                    text=f"✅ <b>Сканер запущен</b>\n\n"
                         f"<b>Отслеживается:</b> {len(tracked_symbols)} пар\n"
                         f"<b>В блэк-листе:</b> {len(blacklist)} монет\n"
                         f"<b>Уведомления отключены:</b> {len(paused_alerts)} монет\n\n"
                         f"<b>Фильтры:</b>\n"
                         f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
                         f"• Исключены акции\n\n"
                         f"<b>Примеры:</b>\n{', '.join(sample[:8])}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление: {e}")
        
        return True
        
    except Exception as e:
        logger.error(f"Критическая ошибка при загрузке символов: {e}")
        return False


# ====================== ОБНОВЛЕННЫЕ ФУНКЦИИ УПРАВЛЕНИЯ ======================
async def load_data_from_db():
    """Загружаем данные из базы"""
    global blacklist, paused_alerts
    
    try:
        blacklist = await db.get_blacklist()
        paused_alerts = await db.get_paused_alerts()
        logger.info(f"Загружено из БД: blacklist={len(blacklist)}, paused={len(paused_alerts)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки из БД: {e}")
        blacklist = set()
        paused_alerts = set()


async def toggle_pause_symbol(query, symbol: str):
    """Включить/выключить уведомления для монеты"""
    try:
        if symbol in paused_alerts:
            paused_alerts.remove(symbol)
            await db.remove_paused_alert(symbol)
            action = "включены"
        else:
            paused_alerts.add(symbol)
            await db.add_paused_alert(symbol)
            action = "отключены"
        
        await query.edit_message_text(
            f"✅ Уведомления для <b>{symbol}</b> {action}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка переключения паузы: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при изменении настроек",
            parse_mode="HTML"
        )


async def add_to_blacklist(query, symbol: str):
    """Добавить монету в блэк-лист"""
    try:
        if symbol in blacklist:
            await query.edit_message_text(
                f"ℹ️ <b>{symbol}</b> уже в блэк-листе",
                parse_mode="HTML"
            )
            return
        
        blacklist.add(symbol)
        await db.add_to_blacklist(symbol)
        
        # Удаляем из отслеживаемых
        if symbol in tracked_symbols:
            tracked_symbols.remove(symbol)
        
        # Удаляем из пауз
        if symbol in paused_alerts:
            paused_alerts.remove(symbol)
            await db.remove_paused_alert(symbol)
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✅ <b>{symbol}</b> добавлен в блэк-лист\n\n"
            f"Монета исключена из отслеживания",
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Ошибка добавления в блэк-лист: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при добавлении в блэк-лист",
            parse_mode="HTML"
        )


async def remove_from_blacklist(query, symbol: str):
    """Удалить монету из блэк-листа"""
    try:
        if symbol not in blacklist:
            await query.edit_message_text(
                f"ℹ️ <b>{symbol}</b> нет в блэк-листе",
                parse_mode="HTML"
            )
            return
        
        blacklist.remove(symbol)
        await db.remove_from_blacklist(symbol)
        
        await query.edit_message_text(
            f"✅ <b>{symbol}</b> удален из блэк-листа\n\n"
            f"Монета будет проверена при следующем обновлении списка",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка удаления из блэк-листа: {e}")
        await query.edit_message_text(
            f"❌ Ошибка при удалении из блэк-листа",
            parse_mode="HTML"
        )


# ====================== ОБНОВЛЕННЫЙ СКАНЕР ======================
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
                        
                        # Сохраняем алерт в базу
                        try:
                            await db.save_alert(
                                symbol, prev_vol, curr_vol, 
                                prev_price, curr_price,
                                volume_change_pct, price_change_pct
                            )
                        except Exception as e:
                            logger.error(f"Ошибка сохранения алерта в БД: {e}")
                        
                        # Создаем клавиатуру
                        keyboard = [
                            [
                                InlineKeyboardButton("🔕 Выключить увед.", callback_data=f"pause_{symbol}"),
                                InlineKeyboardButton("🚫 В блэк-лист", callback_data=f"blacklist_{symbol}")
                            ]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        message = (
                            f"<b>⚡ {symbol}</b>\n"
                            f"Объём: {prev_vol:,} → <b>{curr_vol:,}</b> USDT\n"
                            f"Изменение: <b>{volume_change_pct:+.0f}%</b>\n"
                            f"Цена: <b>{price_change_pct:+.2f}%</b>\n"
                            f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>📊</a>"
                        )
                        
                        try:
                            await bot_instance.send_message(
                                chat_id=MY_USER_ID,
                                text=message,
                                parse_mode="HTML",
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
    
    await update.message.reply_text(
        f"<b>📊 MEXC Volume Scanner</b>\n\n"
        f"<b>Статус:</b> ✅ Активен\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>В блэк-листе:</b> {len(blacklist)} монет\n"
        f"<b>Уведомления отключены:</b> {len(paused_alerts)} монет\n\n"
        f"<i>Выберите действие:</i>",
        parse_mode="HTML",
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
    
    await query.edit_message_text(
        f"<b>📊 MEXC Volume Scanner</b>\n\n"
        f"<b>Статус:</b> ✅ Активен\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>В блэк-листе:</b> {len(blacklist)} монет\n"
        f"<b>Уведомления отключены:</b> {len(paused_alerts)} монет\n\n"
        f"<i>Выберите действие:</i>",
        parse_mode="HTML",
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
        f"<b>📋 Отслеживаемые пары</b>\n\n"
        f"Всего: {len(tracked_symbols)} пар\n\n"
        f"{symbols_text}\n\n"
        f"<i>Показано {min(20, len(symbols_list))} из {len(symbols_list)}</i>",
        parse_mode="HTML",
        reply_markup=reply_markup
    )


async def show_blacklist_menu(query):
    """Показать меню блэк-листа"""
    if not blacklist:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"<b>🚫 Блэк-лист</b>\n\n"
            f"В блэк-листе нет монет",
            parse_mode="HTML",
            reply_markup=reply_markup
        )
        return
    
    blacklist_list = sorted(list(blacklist))
    blacklist_text = "\n".join([f"• {symbol}" for symbol in blacklist_list[:15]])
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"<b>🚫 Блэк-лист</b>\n\n"
        f"Всего: {len(blacklist)} монет\n\n"
        f"{blacklist_text}\n\n"
        f"<i>Показано {min(15, len(blacklist_list))} из {len(blacklist_list)}</i>",
        parse_mode="HTML",
        reply_markup=reply_markup
    )


async def show_paused_menu(query):
    """Показать меню отключенных уведомлений"""
    if not paused_alerts:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"<b>🔕 Отключенные уведомления</b>\n\n"
            f"Нет отключенных уведомлений",
            parse_mode="HTML",
            reply_markup=reply_markup
        )
        return
    
    paused_list = sorted(list(paused_alerts))
    paused_text = "\n".join([f"• {symbol}" for symbol in paused_list[:15]])
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"<b>🔕 Отключенные уведомления</b>\n\n"
        f"Всего: {len(paused_alerts)} монет\n\n"
        f"{paused_text}\n\n"
        f"<i>Показано {min(15, len(paused_list))} из {len(paused_list)}</i>",
        parse_mode="HTML",
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
            f"В блэк-листе: {len(blacklist)} монет",
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text("❌ Ошибка обновления")


async def stats_db_query(query):
    """Статистика через callback"""
    try:
        recent_alerts = await db.get_recent_alerts(24)
        
        alert_count = len(recent_alerts)
        unique_symbols = len(set([alert['symbol'] for alert in recent_alerts]))
        
        # Находим самые активные монеты
        symbol_counts = {}
        for alert in recent_alerts:
            symbol = alert['symbol']
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        
        top_symbols = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        stats_text = f"<b>📊 Статистика за 24ч</b>\n\n"
        stats_text += f"<b>Всего алертов:</b> {alert_count}\n"
        stats_text += f"<b>Уникальных пар:</b> {unique_symbols}\n"
        stats_text += f"<b>В блэк-листе:</b> {len(blacklist)}\n"
        stats_text += f"<b>Пауз уведомлений:</b> {len(paused_alerts)}\n\n"
        
        if top_symbols:
            stats_text += "<b>Топ-5 активных пар:</b>\n"
            for symbol, count in top_symbols:
                stats_text += f"• {symbol}: {count} алертов\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(stats_text, parse_mode="HTML", reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await query.edit_message_text("❌ Ошибка получения статистики")


# ====================== НОВЫЕ КОМАНДЫ ======================
async def stats_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Расширенная статистика из базы данных"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    try:
        recent_alerts = await db.get_recent_alerts(24)
        
        alert_count = len(recent_alerts)
        unique_symbols = len(set([alert['symbol'] for alert in recent_alerts]))
        
        # Находим самые активные монеты
        symbol_counts = {}
        for alert in recent_alerts:
            symbol = alert['symbol']
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        
        top_symbols = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        stats_text = f"<b>📊 Статистика за 24ч</b>\n\n"
        stats_text += f"<b>Всего алертов:</b> {alert_count}\n"
        stats_text += f"<b>Уникальных пар:</b> {unique_symbols}\n"
        stats_text += f"<b>В блэк-листе:</b> {len(blacklist)}\n"
        stats_text += f"<b>Пауз уведомлений:</b> {len(paused_alerts)}\n\n"
        
        if top_symbols:
            stats_text += "<b>Топ-5 активных пар:</b>\n"
            for symbol, count in top_symbols:
                stats_text += f"• {symbol}: {count} алертов\n"
        
        await update.message.reply_text(stats_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await update.message.reply_text("❌ Ошибка получения статистики")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """История алертов для конкретной монеты"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    if not context.args:
        await update.message.reply_text(
            "Использование: /history <символ>\n"
            "Пример: /history BTCUSDT"
        )
        return
    
    symbol = context.args[0].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    
    try:
        all_alerts = await db.get_recent_alerts(24)
        symbol_alerts = [a for a in all_alerts if a['symbol'] == symbol][:10]
        
        if not symbol_alerts:
            await update.message.reply_text(f"ℹ️ За последние 24 часа не было алертов для {symbol}")
            return
        
        history_text = f"<b>📈 История алертов: {symbol}</b>\n\n"
        
        for i, alert in enumerate(symbol_alerts, 1):
            time_str = alert['created_at'].strftime("%H:%M")
            history_text += (
                f"{i}. <b>{time_str}</b>\n"
                f"   Объём: {alert['prev_volume']:,}→{alert['curr_volume']:,} "
                f"(<b>{alert['volume_change_pct']:+.0f}%</b>)\n"
                f"   Цена: <b>{alert['price_change_pct']:+.2f}%</b>\n\n"
            )
        
        await update.message.reply_text(history_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка получения истории: {e}")
        await update.message.reply_text("❌ Ошибка получения истории")


async def run_telegram_polling():
    """Запуск Telegram polling"""
    try:
        await application.initialize()
        await application.start()
        logger.info("Telegram бот готов к работе")
        await application.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Ошибка запуска Telegram бота: {e}")


# ====================== ОБНОВЛЕННЫЙ ЗАПУСК ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner_task, application, bot_instance
    
    logger.info("=== Запуск MEXC Volume Scanner с PostgreSQL ===")
    
    # Подключаемся к базе данных
    try:
        await db.connect()
        logger.info("✅ Подключение к базе данных установлено")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к базе данных: {e}")
        # Можно продолжить без базы данных, но с ограничениями
    
    # Загружаем данные из базы
    await load_data_from_db()
    
    bot_instance = Bot(token=TELEGRAM_TOKEN)
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_db))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    await load_and_filter_symbols()
    
    scanner_task = asyncio.create_task(volume_spike_scanner())
    logger.info("Сканер запущен в фоне")
    
    if TELEGRAM_TOKEN and MY_USER_ID:
        asyncio.create_task(run_telegram_polling())
    
    yield
    
    logger.info("=== Остановка приложения ===")
    
    if scanner_task:
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass
    
    # Закрываем соединение с базой
    await db.close()
    
    if application:
        await application.shutdown()
        await application.stop()


# ====================== FASTAPI ======================
app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "service": "MEXC Volume Scanner с PostgreSQL",
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

















