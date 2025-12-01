import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
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

DAILY_VOLUME_LIMIT = 2_000_000  # USDT — максимальный дневной объём
MIN_PREV_VOLUME = 1000  # Минимальный предыдущий объём
MIN_CURRENT_VOLUME = 2000  # Минимальный текущий объём для алерта

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Глобальные переменные
tracked_symbols = set()  # Символы для отслеживания (объём < 2M USDT)
sent_alerts = {}  # Для предотвращения дублирования алертов

# Глобальные переменные для управления задачами
scanner_task = None
application = None
bot_instance = None

# ====================== MEXC API ======================
async def load_low_volume_symbols():
    """Загружаем ВСЕ символы фьючерсов и фильтруем по дневному объёму < 2M USDT"""
    global tracked_symbols
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://contract.mexc.com/api/v1/contract/detail", timeout=15) as resp:
                if resp.status != 200:
                    logger.error(f"API вернул {resp.status}")
                    return False
                
                data = await resp.json()
                if not data.get("success"):
                    logger.error("API не success")
                    return False

                symbols_data = data["data"]
                
                # Собираем ВСЕ пары с дневным объёмом < 2M USDT
                new_tracked = set()
                for s in symbols_data:
                    symbol_name = s["symbol"]
                    if symbol_name.endswith("_USDT") and s.get("state") == 1:
                        daily_volume = float(s.get("volume24h", 0))
                        if daily_volume <= DAILY_VOLUME_LIMIT:
                            # Преобразуем формат: BTC_USDT -> BTCUSDT
                            formatted_symbol = symbol_name.replace("_USDT", "USDT")
                            new_tracked.add(formatted_symbol)
                
                tracked_symbols = new_tracked
                logger.info(f"Загружено и отслеживается: {len(tracked_symbols)} пар (объём < {DAILY_VOLUME_LIMIT:,} USDT/день)")
                return True
                
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
        return False


async def get_1m_kline_data(symbol: str):
    """Получаем данные за последние 2 свечи на 1-минутном таймфрейме"""
    # Преобразуем формат: BTCUSDT -> BTC_USDT для API MEXC
    api_symbol = symbol.replace("USDT", "_USDT")
    timestamp = str(int(time.time() * 1000))
    
    # Создаем подпись для API
    query_string = f"symbol={api_symbol}&interval=Min1&limit=2"
    signature = hmac.new(
        MEXC_SECRET_KEY.encode(), 
        query_string.encode(), 
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        "ApiKey": MEXC_API_KEY,
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
                    if data.get("success") and len(data["data"]["close"]) >= 2:
                        kline_data = data["data"]
                        
                        # Предыдущая свеча (индекс 0)
                        prev_volume = int(float(kline_data["amount"][0]))
                        prev_close = float(kline_data["close"][0])
                        
                        # Текущая свеча (индекс 1)
                        curr_volume = int(float(kline_data["amount"][1]))
                        curr_close = float(kline_data["close"][1])
                        
                        # Также получаем текущий максимум и минимум для контекста
                        curr_high = float(kline_data["high"][1])
                        curr_low = float(kline_data["low"][1])
                        
                        return {
                            "prev_volume": prev_volume,
                            "curr_volume": curr_volume,
                            "prev_price": prev_close,
                            "curr_price": curr_close,
                            "curr_high": curr_high,
                            "curr_low": curr_low,
                            "symbol": symbol
                        }
                        
    except asyncio.TimeoutError:
        logger.warning(f"Таймаут при получении данных для {symbol}")
    except Exception as e:
        logger.error(f"Ошибка получения данных для {symbol}: {e}")
    
    return None


# ====================== СКАНЕР ВСПЛЕСКОВ ОБЪЁМА ======================
async def scan_all_low_volume_pairs():
    """Сканируем ВСЕ низковольюмные пары на всплески объёма"""
    logger.info(f"Сканер запущен - отслеживаю ВСЕ пары с объёмом < {DAILY_VOLUME_LIMIT:,} USDT/день")
    
    while True:
        try:
            current_minute = datetime.now().strftime("%Y%m%d%H%M")
            
            # Для каждой отслеживаемой пары
            for symbol in list(tracked_symbols):
                try:
                    # Получаем данные 1м свечи
                    data = await get_1m_kline_data(symbol)
                    if not data:
                        continue
                    
                    prev_vol = data["prev_volume"]
                    curr_vol = data["curr_volume"]
                    prev_price = data["prev_price"]
                    curr_price = data["curr_price"]
                    
                    # Проверяем условие всплеска
                    if prev_vol < MIN_PREV_VOLUME and curr_vol > MIN_CURRENT_VOLUME:
                        # Уникальный ID алерта для предотвращения дублирования
                        alert_id = f"{symbol}_{current_minute}"
                        
                        # Проверяем, не отправляли ли уже алерт для этой пары в эту минуту
                        if alert_id in sent_alerts:
                            continue
                        
                        # Рассчитываем изменения
                        volume_change_pct = ((curr_vol - prev_vol) / max(prev_vol, 1)) * 100
                        price_change_pct = ((curr_price - prev_price) / max(prev_price, 0.00000001)) * 100 if prev_price > 0 else 0
                        
                        # Определяем эмодзи
                        if price_change_pct >= 3:
                            price_emoji = "🚀"
                        elif price_change_pct >= 1:
                            price_emoji = "📈"
                        elif price_change_pct <= -3:
                            price_emoji = "💥"
                        elif price_change_pct <= -1:
                            price_emoji = "📉"
                        else:
                            price_emoji = "➡️"
                        
                        # Форматируем цену в зависимости от её величины
                        if curr_price >= 1:
                            price_format = f"{curr_price:.4f}"
                        elif curr_price >= 0.01:
                            price_format = f"{curr_price:.6f}"
                        else:
                            price_format = f"{curr_price:.8f}"
                        
                        # Формируем сообщение
                        message = (
                            f"<b>⚡ ВСПЛЕСК ОБЪЁМА 1M</b>\n\n"
                            f"<b>Пара:</b> {symbol}\n"
                            f"<b>Время:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
                            f"<b>Объём 1M:</b>\n"
                            f"• Предыдущий: {prev_vol:,} USDT\n"
                            f"• Текущий: <b>{curr_vol:,} USDT</b>\n"
                            f"• Изменение: <b>{volume_change_pct:+.0f}%</b>\n\n"
                            f"<b>Цена:</b>\n"
                            f"• Было: {prev_price:.8f}\n"
                            f"• Стало: <b>{price_format}</b>\n"
                            f"• Изменение: {price_emoji} <b>{price_change_pct:+.2f}%</b>\n\n"
                            f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>📊 Открыть фьючерс</a> | "
                            f"<a href='https://www.mexc.com/exchange/{symbol[:-4]}_USDT'>💎 Спот</a>"
                        )
                        
                        # Отправляем алерт
                        try:
                            await bot_instance.send_message(
                                chat_id=MY_USER_ID,
                                text=message,
                                parse_mode="HTML",
                                disable_web_page_preview=True
                            )
                            
                            # Запоминаем отправленный алерт
                            sent_alerts[alert_id] = time.time()
                            logger.info(f"АЛЕРТ: {symbol} | Объём: {prev_vol:,}→{curr_vol:,} ({volume_change_pct:+.0f}%) | Цена: {price_change_pct:+.2f}%")
                            
                        except Exception as e:
                            logger.error(f"Ошибка отправки сообщения: {e}")
                            
                except Exception as e:
                    logger.error(f"Ошибка при обработке {symbol}: {e}")
                    continue
            
            # Очищаем старые алерты (старше 1 часа)
            current_time = time.time()
            expired_alerts = [alert_id for alert_id, alert_time in sent_alerts.items() 
                            if current_time - alert_time > 3600]
            for expired in expired_alerts:
                sent_alerts.pop(expired, None)
            
            # Обновляем список символов каждые 30 минут
            if int(time.time()) % 1800 < 30:  # Каждые 30 минут
                await load_low_volume_symbols()
            
            # Ждем 58 секунд до следующей проверки
            await asyncio.sleep(58)
            
        except asyncio.CancelledError:
            logger.info("Сканер остановлен")
            break
        except Exception as e:
            logger.error(f"Критическая ошибка в сканере: {e}")
            await asyncio.sleep(60)


# ====================== TELEGRAM КОМАНДЫ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if update.effective_user.id != MY_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return
    
    await update.message.reply_text(
        f"<b>📊 MEXC Volume Scanner</b>\n\n"
        f"<b>Отслеживаю ВСЕ пары с дневным объёмом < {DAILY_VOLUME_LIMIT:,} USDT</b>\n"
        f"<b>Таймфрейм:</b> 1 минута\n"
        f"<b>Условие алерта:</b> Объём 1M < {MIN_PREV_VOLUME:,} → > {MIN_CURRENT_VOLUME:,} USDT\n\n"
        f"<b>Статус:</b> ✅ Активен\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>Алертов сегодня:</b> {len(sent_alerts)}\n\n"
        f"<i>Бот работает 24/7 на Render</i>",
        parse_mode="HTML"
    )


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновить список отслеживаемых пар"""
    if update.effective_user.id != MY_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return
    
    await update.message.reply_text("🔄 Обновляю список пар...")
    success = await load_low_volume_symbols()
    
    if success:
        await update.message.reply_text(
            f"✅ Обновлено!\n"
            f"Отслеживается: {len(tracked_symbols)} пар\n"
            f"Условие: дневной объём < {DAILY_VOLUME_LIMIT:,} USDT",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Ошибка обновления")


# ====================== УПРАВЛЕНИЕ ЖИЗНЕННЫМ ЦИКЛОМ ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    global scanner_task, application, bot_instance
    
    logger.info("Запуск MEXC Volume Scanner...")
    
    # Создаем экземпляр бота для отправки сообщений
    bot_instance = Bot(token=TELEGRAM_TOKEN)
    
    # Инициализируем Telegram приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refresh", refresh))
    
    # Загружаем символы для отслеживания
    if await load_low_volume_symbols():
        # Запускаем сканер
        scanner_task = asyncio.create_task(scan_all_low_volume_pairs())
        logger.info("Сканер запущен")
    else:
        logger.error("Не удалось загрузить символы для отслеживания")
    
    # Запускаем Telegram бота в фоне
    if TELEGRAM_TOKEN and MY_USER_ID:
        asyncio.create_task(run_telegram_polling())
    
    yield
    
    # Останавливаем приложение
    logger.info("Остановка приложения...")
    
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
    """Запуск Telegram polling в фоне"""
    try:
        await application.initialize()
        await application.start()
        logger.info("Telegram бот запущен")
        await application.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Ошибка запуска Telegram бота: {e}")


# ====================== FASTAPI ДЛЯ RENDER ======================
app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    """Корневой endpoint"""
    return {
        "service": "MEXC Volume Scanner",
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "tracked_pairs": len(tracked_symbols),
        "daily_volume_limit": DAILY_VOLUME_LIMIT,
        "active_alerts": len(sent_alerts)
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "tracking_active": len(tracked_symbols) > 0
    }

@app.get("/status")
async def status():
    """Статус сканера"""
    sample_symbols = list(tracked_symbols)[:10] if tracked_symbols else []
    return {
        "tracked_pairs_count": len(tracked_symbols),
        "alert_condition": f"1m volume < {MIN_PREV_VOLUME} → > {MIN_CURRENT_VOLUME} USDT",
        "daily_volume_limit": DAILY_VOLUME_LIMIT,
        "sample_symbols": sample_symbols,
        "recent_alerts_count": len(sent_alerts),
        "last_updated": datetime.now().isoformat()
    }


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "Mexcnewbot:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )











