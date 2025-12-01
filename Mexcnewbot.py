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
import json

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
tracked_symbols = set()
sent_alerts = {}

# Глобальные переменные для управления задачами
scanner_task = None
application = None
bot_instance = None

# ====================== MEXC API ======================
async def load_low_volume_symbols():
    """Загружаем ВСЕ символы фьючерсов и фильтруем по дневному объёму < 2M USDT"""
    global tracked_symbols
    
    try:
        logger.info("Запрашиваю данные о фьючерсах с MEXC API...")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://contract.mexc.com/api/v1/contract/detail", 
                timeout=20
            ) as resp:
                logger.info(f"Статус ответа: {resp.status}")
                
                if resp.status != 200:
                    logger.error(f"API вернул ошибку: {resp.status}")
                    return False
                
                data = await resp.json()
                logger.info(f"Получены данные от API, успех: {data.get('success')}")
                
                # Сохраняем ответ для отладки
                with open("mexc_debug.json", "w") as f:
                    json.dump(data, f, indent=2)
                logger.info("Ответ API сохранён в mexc_debug.json")
                
                if not data.get("success"):
                    logger.error(f"API не success: {data.get('code', 'N/A')} - {data.get('msg', 'N/A')}")
                    return False

                symbols_data = data.get("data", [])
                logger.info(f"Получено {len(symbols_data)} символов")
                
                new_tracked = set()
                low_volume_count = 0
                
                for idx, s in enumerate(symbols_data[:10]):  # Логируем первые 10 для отладки
                    symbol_name = s.get("symbol", "")
                    state = s.get("state", 0)
                    volume24h = float(s.get("volume24h", 0))
                    
                    if idx < 5:  # Логируем первые 5 для понимания структуры
                        logger.info(f"Символ {idx}: {symbol_name}, state: {state}, volume24h: {volume24h:,.0f}")
                
                for s in symbols_data:
                    symbol_name = s.get("symbol", "")
                    state = s.get("state", 0)
                    volume24h = float(s.get("volume24h", 0))
                    
                    # Проверяем что это USDT фьючерс и активен
                    if symbol_name.endswith("_USDT") and state == 1:
                        if volume24h <= DAILY_VOLUME_LIMIT:
                            # Преобразуем формат: BTC_USDT -> BTCUSDT
                            formatted_symbol = symbol_name.replace("_USDT", "USDT")
                            new_tracked.add(formatted_symbol)
                            low_volume_count += 1
                    
                tracked_symbols = new_tracked
                logger.info(f"Найдено пар с объёмом ≤ {DAILY_VOLUME_LIMIT:,}: {low_volume_count}")
                logger.info(f"Всего активных USDT пар: {len([s for s in symbols_data if s.get('symbol', '').endswith('_USDT') and s.get('state') == 1])}")
                
                # Если всё равно 0, пробуем альтернативный подход
                if len(tracked_symbols) == 0:
                    logger.warning("Нет пар с объёмом < 2M. Пробую альтернативный подход...")
                    
                    # Альтернатива 1: берем все активные пары
                    for s in symbols_data:
                        symbol_name = s.get("symbol", "")
                        state = s.get("state", 0)
                        
                        if symbol_name.endswith("_USDT") and state == 1:
                            formatted_symbol = symbol_name.replace("_USDT", "USDT")
                            tracked_symbols.add(formatted_symbol)
                    
                    logger.info(f"Альтернатива: отслеживаю ВСЕ активные пары: {len(tracked_symbols)}")
                
                return True
                
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}", exc_info=True)
        return False


async def get_1m_kline_data(symbol: str):
    """Получаем данные за последние 2 свечи на 1-минутном таймфрейме"""
    api_symbol = symbol.replace("USDT", "_USDT")
    timestamp = str(int(time.time() * 1000))
    
    # Создаем подпись для API
    query_string = f"symbol={api_symbol}&interval=Min1&limit=2"
    signature = hmac.new(
        MEXC_SECRET_KEY.encode() if MEXC_SECRET_KEY else b"",
        query_string.encode(), 
        hashlib.sha256
    ).hexdigest()
    
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
                        
                        # Проверяем что есть данные
                        if len(kline_data.get("close", [])) >= 2:
                            # Предыдущая свеча (индекс 0)
                            prev_volume = int(float(kline_data["amount"][0]))
                            prev_close = float(kline_data["close"][0])
                            
                            # Текущая свеча (индекс 1)
                            curr_volume = int(float(kline_data["amount"][1]))
                            curr_close = float(kline_data["close"][1])
                            
                            return {
                                "prev_volume": prev_volume,
                                "curr_volume": curr_volume,
                                "prev_price": prev_close,
                                "curr_price": curr_close,
                                "symbol": symbol
                            }
                        else:
                            logger.debug(f"Недостаточно данных для {symbol}")
                else:
                    logger.debug(f"Ошибка API для {symbol}: {response.status}")
                        
    except asyncio.TimeoutError:
        logger.debug(f"Таймаут при получении данных для {symbol}")
    except Exception as e:
        logger.debug(f"Ошибка получения данных для {symbol}: {str(e)[:100]}")
    
    return None


# ====================== СКАНЕР ВСПЛЕСКОВ ОБЪЁМА ======================
async def scan_all_low_volume_pairs():
    """Сканируем ВСЕ низковольюмные пары на всплески объёма"""
    logger.info(f"Сканер запущен. Отслеживается {len(tracked_symbols)} пар.")
    
    # Если нет пар для отслеживания, сообщаем в Telegram
    if len(tracked_symbols) == 0:
        try:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text="⚠️ <b>ВНИМАНИЕ</b>\n\n"
                     "Не найдено пар для отслеживания!\n"
                     "Проверьте API ключи или параметры фильтрации.",
                parse_mode="HTML"
            )
        except:
            pass
    
    iteration = 0
    
    while True:
        try:
            current_minute = datetime.now().strftime("%Y%m%d%H%M")
            iteration += 1
            
            # Каждые 10 итераций логируем статус
            if iteration % 10 == 1:
                logger.info(f"Итерация {iteration}. Отслеживается {len(tracked_symbols)} пар. Активных алертов: {len(sent_alerts)}")
            
            # Если нет пар для отслеживания, пытаемся обновить список
            if len(tracked_symbols) == 0:
                logger.warning("Нет пар для отслеживания. Пробую обновить...")
                await load_low_volume_symbols()
                await asyncio.sleep(10)
                continue
            
            # Для каждой отслеживаемой пары
            symbols_to_check = list(tracked_symbols)
            
            # Ограничиваем количество для проверки в одной итерации
            max_check_per_iteration = 50
            if len(symbols_to_check) > max_check_per_iteration:
                # Берем случайную выборку
                import random
                symbols_to_check = random.sample(symbols_to_check, max_check_per_iteration)
                logger.debug(f"Проверяю выборку из {len(symbols_to_check)} пар")
            
            for symbol in symbols_to_check:
                try:
                    data = await get_1m_kline_data(symbol)
                    if not data:
                        continue
                    
                    prev_vol = data["prev_volume"]
                    curr_vol = data["curr_volume"]
                    prev_price = data["prev_price"]
                    curr_price = data["curr_price"]
                    
                    # Пропускаем если данные некорректные
                    if prev_vol <= 0 or curr_vol <= 0:
                        continue
                    
                    # Проверяем условие всплеска
                    if prev_vol < MIN_PREV_VOLUME and curr_vol > MIN_CURRENT_VOLUME:
                        alert_id = f"{symbol}_{current_minute}"
                        
                        if alert_id in sent_alerts:
                            continue
                        
                        # Рассчитываем изменения
                        volume_change = curr_vol - prev_vol
                        volume_change_pct = (volume_change / prev_vol) * 100
                        
                        if prev_price > 0:
                            price_change_pct = ((curr_price - prev_price) / prev_price) * 100
                        else:
                            price_change_pct = 0
                        
                        # Форматируем цену
                        if curr_price >= 1:
                            price_format = f"{curr_price:.4f}"
                        elif curr_price >= 0.01:
                            price_format = f"{curr_price:.6f}"
                        else:
                            price_format = f"{curr_price:.8f}"
                        
                        # Формируем сообщение
                        message = (
                            f"<b>⚡ ВСПЛЕСК ОБЪЁМА</b>\n\n"
                            f"<b>Пара:</b> {symbol}\n"
                            f"<b>Время:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
                            f"<b>Объём 1M:</b>\n"
                            f"Предыдущий: {prev_vol:,} USDT\n"
                            f"Текущий: <b>{curr_vol:,} USDT</b>\n"
                            f"Изменение: <b>{volume_change_pct:+.0f}%</b>\n\n"
                            f"<b>Цена:</b>\n"
                            f"Было: {prev_price:.8f}\n"
                            f"Стало: <b>{price_format}</b>\n"
                            f"Изменение: <b>{price_change_pct:+.2f}%</b>\n\n"
                            f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>📊 Открыть фьючерс</a>"
                        )
                        
                        # Отправляем алерт
                        try:
                            await bot_instance.send_message(
                                chat_id=MY_USER_ID,
                                text=message,
                                parse_mode="HTML",
                                disable_web_page_preview=True
                            )
                            
                            sent_alerts[alert_id] = time.time()
                            logger.info(f"АЛЕРТ: {symbol} | Объём: {prev_vol:,}→{curr_vol:,} (+{volume_change_pct:.0f}%)")
                            
                        except Exception as e:
                            logger.error(f"Ошибка отправки: {e}")
                            
                except Exception as e:
                    logger.debug(f"Ошибка при обработке {symbol}: {str(e)[:100]}")
                    continue
            
            # Очищаем старые алерты
            current_time = time.time()
            expired_alerts = [k for k, v in sent_alerts.items() if current_time - v > 3600]
            for expired in expired_alerts:
                sent_alerts.pop(expired, None)
            
            # Обновляем список символов каждые 30 минут
            if iteration % 30 == 0:  # Примерно каждые 30 минут
                logger.info("Обновляю список символов...")
                await load_low_volume_symbols()
            
            # Ждем 30 секунд до следующей проверки
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            logger.info("Сканер остановлен")
            break
        except Exception as e:
            logger.error(f"Ошибка в сканере: {e}")
            await asyncio.sleep(60)


# ====================== TELEGRAM КОМАНДЫ ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if update.effective_user.id != MY_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return
    
    await update.message.reply_text(
        f"<b>📊 MEXC Volume Scanner</b>\n\n"
        f"<b>Статус:</b> ✅ Активен\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>Дневной лимит:</b> {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"<b>Таймфрейм:</b> 1 минута\n"
        f"<b>Условие алерта:</b> < {MIN_PREV_VOLUME:,} → > {MIN_CURRENT_VOLUME:,} USDT\n\n"
        f"<i>Используй /debug для отладки</i>",
        parse_mode="HTML"
    )


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отладочная информация"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    debug_info = (
        f"<b>🔧 Отладочная информация</b>\n\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>Примеры пар:</b>\n"
    )
    
    # Показываем первые 5 пар
    sample = list(tracked_symbols)[:5]
    for i, symbol in enumerate(sample):
        debug_info += f"{i+1}. {symbol}\n"
    
    debug_info += f"\n<b>Активных алертов:</b> {len(sent_alerts)}\n"
    debug_info += f"<b>Время запуска:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    
    await update.message.reply_text(debug_info, parse_mode="HTML")


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновить список пар"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    await update.message.reply_text("🔄 Обновляю список пар...")
    success = await load_low_volume_symbols()
    
    if success:
        await update.message.reply_text(
            f"✅ Обновлено!\n"
            f"Отслеживается: {len(tracked_symbols)} пар",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Ошибка обновления")


# ====================== УПРАВЛЕНИЕ ЖИЗНЕННЫМ ЦИКЛОМ ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner_task, application, bot_instance
    
    logger.info("=== Запуск MEXC Volume Scanner ===")
    
    # Создаем экземпляр бота
    bot_instance = Bot(token=TELEGRAM_TOKEN)
    
    # Инициализируем Telegram приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("refresh", refresh))
    
    # Загружаем символы
    await load_low_volume_symbols()
    
    # Запускаем сканер
    scanner_task = asyncio.create_task(scan_all_low_volume_pairs())
    logger.info("Сканер запущен в фоне")
    
    # Запускаем Telegram бота
    if TELEGRAM_TOKEN and MY_USER_ID:
        asyncio.create_task(run_telegram_polling())
    
    yield
    
    # Останавливаем приложение
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
    """Запуск Telegram polling"""
    try:
        await application.initialize()
        await application.start()
        logger.info("Telegram бот готов к работе")
        await application.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Ошибка запуска Telegram бота: {e}")


# ====================== FASTAPI ======================
app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {
        "service": "MEXC Volume Scanner",
        "status": "active",
        "timestamp": datetime.now().isoformat(),
        "tracked_pairs": len(tracked_symbols),
        "alerts_today": len(sent_alerts)
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/status")
async def status():
    sample = list(tracked_symbols)[:10] if tracked_symbols else []
    return {
        "tracked_pairs": len(tracked_symbols),
        "sample_symbols": sample,
        "daily_limit": DAILY_VOLUME_LIMIT
    }


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "Mexcnewbot:app",
        host="0.0.0.0",
        port=port,
        reload=False
    )












