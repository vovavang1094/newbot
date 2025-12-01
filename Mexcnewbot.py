import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
import random
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, CommandHandler
from fastapi import FastAPI
from contextlib import asynccontextmanager
import uvicorn
from datetime import datetime

# ====================== НАСТРОЙКИ ======================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID", 0))

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

DAILY_VOLUME_LIMIT = 500_000  # USDT — максимальный дневной объём
MAX_MARKET_CAP = 80_000_000  # USDT — максимальная рыночная капитализация
MIN_PREV_VOLUME = 1000  # Минимальный предыдущий объём на 1m
MIN_CURRENT_VOLUME = 2500  # Минимальный текущий объём для алерта на 1m

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Глобальные переменные
tracked_symbols = set()
sent_alerts = {}

# Глобальные переменные для управления задачами
scanner_task = None
application = None
bot_instance = None

# Списки для фильтрации
STOCK_SYMBOLS = {
    # Акции (stock symbols)
    'AAPL', 'GOOGL', 'AMZN', 'MSFT', 'TSLA', 'META', 'NVDA', 'NFLX', 
    'AMD', 'INTC', 'IBM', 'ORCL', 'CSCO', 'ADBE', 'PYPL', 'CRM',
    # Индексы и фонды
    'SPY', 'QQQ', 'DIA', 'IWM', 'VOO', 'IVV', 'VTI', 'VUG',
    # Крипто-акции и токенизированные активы
    'MSTR', 'COIN', 'RIOT', 'MAR', 'HUT', 'BITF', 'CLSK'
}

# ====================== MEXC API ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
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


async def get_market_cap(symbol: str) -> float:
    """Получаем рыночную капитализацию токена"""
    try:
        # Для получения данных о капитализации нужен отдельный API запрос
        # Используем спотовый API для получения информации о монете
        clean_symbol = symbol.replace("USDT", "")
        
        async with aiohttp.ClientSession() as session:
            # Используем CoinGecko API (бесплатный, без ключа)
            async with session.get(
                f"https://api.coingecko.com/api/v3/coins/{clean_symbol.lower()}",
                timeout=10
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    market_cap = data.get("market_data", {}).get("market_cap", {}).get("usd", 0)
                    return float(market_cap)
                
    except Exception as e:
        logger.debug(f"Не удалось получить капитализацию для {symbol}: {str(e)[:100]}")
    
    # Если не получили данные, возвращаем 0
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


# ====================== ФИЛЬТРАЦИЯ СИМВОЛОВ ======================
def filter_stock_symbols(symbols: list) -> list:
    """Фильтруем акции и подобные символы"""
    filtered = []
    
    for symbol in symbols:
        clean_symbol = symbol.replace("USDT", "")
        
        # Пропускаем если это акция
        if clean_symbol in STOCK_SYMBOLS:
            logger.debug(f"Пропускаем акцию: {symbol}")
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
        # 1. Проверяем что это не акция
        clean_symbol = symbol.replace("USDT", "")
        if clean_symbol in STOCK_SYMBOLS:
            logger.debug(f"Пропускаем акцию: {symbol}")
            return False
        
        # 2. Проверяем что нет цифр в символе
        if any(char.isdigit() for char in clean_symbol):
            logger.debug(f"Пропускаем символ с цифрами: {symbol}")
            return False
        
        # 3. Проверяем 1D объём
        daily_volume = await get_1d_volume(symbol)
        if daily_volume > DAILY_VOLUME_LIMIT:
            logger.debug(f"Пропускаем {symbol}: объём {daily_volume:,.0f} > {DAILY_VOLUME_LIMIT:,}")
            return False
        
        # 4. Проверяем рыночную капитализацию (если возможно)
        market_cap = await get_market_cap(symbol)
        if market_cap > MAX_MARKET_CAP and market_cap > 0:
            logger.debug(f"Пропускаем {symbol}: капитализация {market_cap:,.0f} > {MAX_MARKET_CAP:,}")
            return False
        
        # 5. Проверяем цену токена (дополнительный фильтр)
        # Если цена меньше 0.0001, вероятно это очень рискованный токен
        try:
            data = await get_1m_kline_data(symbol)
            if data and data["curr_price"] < 0.0001:
                logger.debug(f"Пропускаем {symbol}: цена слишком низкая {data['curr_price']:.8f}")
                return False
        except:
            pass
        
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
        batch_size = 10  # Уменьшил размер батча для стабильности
        
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
            
            # Пауза между батчами чтобы не перегружать API
            if i + batch_size < total_symbols:
                await asyncio.sleep(2)
        
        tracked_symbols = set(low_volume_symbols)
        
        logger.info(f"✅ ФИЛЬТРАЦИЯ ЗАВЕРШЕНА!")
        logger.info(f"   Всего символов: {len(all_symbols)}")
        logger.info(f"   После фильтра акций: {len(filtered_symbols)}")
        logger.info(f"   После всех фильтров: {len(tracked_symbols)}")
        logger.info(f"   Условия: 1D объём < {DAILY_VOLUME_LIMIT:,} USDT, Кап < {MAX_MARKET_CAP:,} USDT")
        
        if tracked_symbols:
            sample = list(tracked_symbols)[:15]
            logger.info(f"   Примеры: {', '.join(sample)}")
            
            try:
                await bot_instance.send_message(
                    chat_id=MY_USER_ID,
                    text=f"✅ <b>Сканер запущен с фильтрами</b>\n\n"
                         f"<b>Отслеживается:</b> {len(tracked_symbols)} пар\n\n"
                         f"<b>Фильтры:</b>\n"
                         f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
                         f"• Рыночная кап < {MAX_MARKET_CAP:,} USDT\n"
                         f"• Исключены акции\n\n"
                         f"<b>Примеры пар:</b>\n{', '.join(sample[:8])}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление: {e}")
        else:
            logger.warning("Нет пар, соответствующих фильтрам!")
            try:
                await bot_instance.send_message(
                    chat_id=MY_USER_ID,
                    text="⚠️ <b>ВНИМАНИЕ</b>\n\n"
                         "Сканер запущен, но не найдено пар, соответствующих фильтрам!\n"
                         "Возможно, фильтры слишком строгие.",
                    parse_mode="HTML"
                )
            except:
                pass
        
        return True
        
    except Exception as e:
        logger.error(f"Критическая ошибка при загрузке символов: {e}")
        return False


# ====================== УЛУЧШЕННЫЙ СКАНЕР ======================
async def volume_spike_scanner():
    """Улучшенный сканер с дополнительными фильтрами"""
    logger.info(f"🚀 Улучшенный сканер запущен! Отслеживаю {len(tracked_symbols)} пар")
    
    if len(tracked_symbols) == 0:
        logger.warning("Нет пар для отслеживания!")
        return
    
    iteration = 0
    alert_count_last_hour = 0
    last_hour_check = time.time()
    
    while True:
        try:
            current_minute = datetime.now().strftime("%Y%m%d%H%M")
            iteration += 1
            
            # Логируем статус каждые 15 итераций
            if iteration % 15 == 1:
                time_since_last_hour = time.time() - last_hour_check
                if time_since_last_hour > 3600:
                    alert_count_last_hour = 0
                    last_hour_check = time.time()
                
                logger.info(f"Итерация {iteration}. Пар: {len(tracked_symbols)}. Алертов за час: {alert_count_last_hour}")
            
            # Обновляем список символов каждые 8 часов
            if iteration % 960 == 0:
                logger.info("🔄 Обновляю список символов (каждые 8 часов)...")
                await load_and_filter_symbols()
                continue
            
            symbols_list = list(tracked_symbols)
            if not symbols_list:
                await asyncio.sleep(60)
                continue
            
            # Ограничиваем количество проверок
            max_per_iteration = min(80, len(symbols_list))
            random.shuffle(symbols_list)
            
            # Дополнительный фильтр: если в последний час было много алертов, уменьшаем проверки
            if alert_count_last_hour > 50:
                max_per_iteration = max(20, max_per_iteration // 2)
                logger.info(f"Много алертов ({alert_count_last_hour}), уменьшаю проверки до {max_per_iteration}")
            
            checked_count = 0
            alerts_in_iteration = 0
            
            for symbol in symbols_list[:max_per_iteration]:
                try:
                    data = await get_1m_kline_data(symbol)
                    if not data:
                        continue
                    
                    prev_vol = data["prev_volume"]
                    curr_vol = data["curr_volume"]
                    prev_price = data["prev_price"]
                    curr_price = data["curr_price"]
                    
                    # ДОПОЛНИТЕЛЬНЫЕ ФИЛЬТРЫ:
                    # 1. Пропускаем если цена слишком волатильна (большие свечи)
                    if prev_price > 0:
                        price_change = abs(curr_price - prev_price) / prev_price
                        if price_change > 0.2:  # Более 20% изменения за 1 минуту
                            logger.debug(f"Пропускаем {symbol}: слишком волатильно ({price_change:.1%})")
                            continue
                    
                    # 2. Пропускаем если текущий объём очень большой (возможно манипуляции)
                    if curr_vol > 50_000:  # Более 50k USDT за 1 минуту
                        logger.debug(f"Пропускаем {symbol}: слишком большой объём {curr_vol:,}")
                        continue
                    
                    # Основное условие
                    if prev_vol < MIN_PREV_VOLUME and curr_vol > MIN_CURRENT_VOLUME:
                        alert_id = f"{symbol}_{current_minute}"
                        
                        if alert_id in sent_alerts:
                            continue
                        
                        volume_change_pct = ((curr_vol - prev_vol) / max(prev_vol, 1)) * 100
                        if prev_price > 0:
                            price_change_pct = ((curr_price - prev_price) / prev_price) * 100
                        else:
                            price_change_pct = 0
                        
                        # ЕЩЁ ФИЛЬТР: пропускаем если изменение объёма слишком маленькое
                        if volume_change_pct < 50:  # Менее 50% роста
                            logger.debug(f"Пропускаем {symbol}: рост объёма всего {volume_change_pct:.0f}%")
                            continue
                        
                        # ЕЩЁ ФИЛЬТР: пропускаем если цена падает при росте объёма (возможно дистибуция)
                        if price_change_pct < -3 and volume_change_pct > 100:
                            logger.debug(f"Пропускаем {symbol}: цена падает при росте объёма")
                            continue
                        
                        # Форматируем сообщение
                        if price_change_pct >= 5:
                            price_emoji = "🚀"
                        elif price_change_pct >= 2:
                            price_emoji = "📈"
                        elif price_change_pct <= -5:
                            price_emoji = "💥"
                        elif price_change_pct <= -2:
                            price_emoji = "📉"
                        else:
                            price_emoji = "➡️"
                        
                        # Короткое сообщение чтобы не спамить
                        message = (
                            f"<b>⚡ {symbol}</b>\n"
                            f"Объём: {prev_vol:,} → <b>{curr_vol:,}</b> USDT\n"
                            f"Изменение: <b>{volume_change_pct:+.0f}%</b>\n"
                            f"Цена: {price_emoji} <b>{price_change_pct:+.2f}%</b>\n"
                            f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>📊</a>"
                        )
                        
                        try:
                            await bot_instance.send_message(
                                chat_id=MY_USER_ID,
                                text=message,
                                parse_mode="HTML",
                                disable_web_page_preview=True
                            )
                            
                            sent_alerts[alert_id] = time.time()
                            alert_count_last_hour += 1
                            alerts_in_iteration += 1
                            
                            logger.info(f"🚨 {symbol} | {prev_vol:,}→{curr_vol:,} (+{volume_change_pct:.0f}%)")
                            
                        except Exception as e:
                            logger.error(f"Ошибка отправки: {e}")
                    
                    checked_count += 1
                            
                except Exception as e:
                    logger.debug(f"Ошибка обработки {symbol}: {str(e)[:100]}")
                    continue
            
            # Очищаем старые алерты
            current_time = time.time()
            expired = [k for k, v in sent_alerts.items() if current_time - v > 7200]  # 2 часа
            for exp in expired:
                sent_alerts.pop(exp, None)
            
            # Сбрасываем счётчик алертов каждый час
            if current_time - last_hour_check > 3600:
                alert_count_last_hour = 0
                last_hour_check = current_time
            
            # Регулируем задержку в зависимости от количества алертов
            if alerts_in_iteration > 5:
                sleep_time = 60  # Если много алертов, ждём дольше
                logger.info(f"Много алертов ({alerts_in_iteration}), увеличиваю паузу до {sleep_time} сек")
            else:
                sleep_time = 35  # Обычная пауза
            
            await asyncio.sleep(sleep_time)
            
        except asyncio.CancelledError:
            logger.info("Сканер остановлен")
            break
        except Exception as e:
            logger.error(f"Ошибка в сканере: {e}")
            await asyncio.sleep(60)


# ====================== TELEGRAM КОМАНДЫ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return
    
    filters_text = (
        f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"• Рыночная кап < {MAX_MARKET_CAP:,} USDT\n"
        f"• Исключены акции и токены с цифрами\n"
        f"• Цена > 0.0001 USDT\n"
        f"• Объём роста > 50%\n"
        f"• Волатильность < 20% за 1m"
    )
    
    await update.message.reply_text(
        f"<b>📊 MEXC Volume Scanner v2</b>\n\n"
        f"<b>Статус:</b> ✅ Активен\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>Алертов за 2ч:</b> {len([v for v in sent_alerts.values() if time.time() - v < 7200])}\n\n"
        f"<b>Фильтры:</b>\n{filters_text}\n\n"
        f"<i>Команды: /stats, /list, /refresh</i>",
        parse_mode="HTML"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        return
    
    recent_alerts = [v for v in sent_alerts.values() if time.time() - v < 7200]
    
    await update.message.reply_text(
        f"<b>📈 Статистика v2</b>\n\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>Алертов за 2ч:</b> {len(recent_alerts)}\n"
        f"<b>Всего алертов:</b> {len(sent_alerts)}\n"
        f"<b>Время:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"<b>Фильтры активны:</b>\n"
        f"• Акции исключены\n"
        f"• Кап < {MAX_MARKET_CAP:,} USDT\n"
        f"• 1D объём < {DAILY_VOLUME_LIMIT:,} USDT",
        parse_mode="HTML"
    )


async def list_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        return
    
    if not tracked_symbols:
        await update.message.reply_text("ℹ️ Нет отслеживаемых пар")
        return
    
    symbols_list = sorted(list(tracked_symbols))
    chunks = [symbols_list[i:i+25] for i in range(0, len(symbols_list), 25)]
    
    for i, chunk in enumerate(chunks):
        await update.message.reply_text(
            f"<b>📋 Отслеживаемые пары ({i+1}/{len(chunks)})</b>\n\n" +
            "\n".join(f"• {symbol}" for symbol in chunk),
            parse_mode="HTML"
        )


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        return
    
    await update.message.reply_text("🔄 Обновляю список пар с фильтрами...")
    
    success = await load_and_filter_symbols()
    
    if success:
        await update.message.reply_text(
            f"✅ Обновлено!\n"
            f"Отслеживается: {len(tracked_symbols)} пар\n"
            f"Фильтры: 1D объём < {DAILY_VOLUME_LIMIT:,} USDT, Кап < {MAX_MARKET_CAP:,} USDT",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Ошибка обновления")


# ====================== ОСТАЛЬНОЙ КОД БЕЗ ИЗМЕНЕНИЙ ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner_task, application, bot_instance
    
    logger.info("=== Запуск MEXC Volume Scanner v2 ===")
    
    bot_instance = Bot(token=TELEGRAM_TOKEN)
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("list", list_symbols))
    application.add_handler(CommandHandler("refresh", refresh))
    
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
    
    if application:
        await application.shutdown()
        await application.stop()


async def run_telegram_polling():
    try:
        await application.initialize()
        await application.start()
        logger.info("Telegram бот готов к работе")
        await application.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Ошибка запуска Telegram бота: {e}")


app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "service": "MEXC Volume Scanner v2",
        "status": "active",
        "timestamp": datetime.now().isoformat(),
        "tracked_pairs": len(tracked_symbols),
        "filters": {
            "daily_volume_limit": DAILY_VOLUME_LIMIT,
            "max_market_cap": MAX_MARKET_CAP,
            "exclude_stocks": True
        },
        "recent_alerts": len([v for v in sent_alerts.values() if time.time() - v < 7200])
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "Mexcnewbot:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )

















