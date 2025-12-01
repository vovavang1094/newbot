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
from datetime import datetime, timedelta

# ====================== НАСТРОЙКИ ======================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID", 0))

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

DAILY_VOLUME_LIMIT = 1_000_000  # <-- ИЗМЕНЕНО: теперь 1M USDT вместо 2M
MIN_PREV_VOLUME = 1000  # Минимальный предыдущий объём на 1m
MIN_CURRENT_VOLUME = 2000  # Минимальный текущий объём для алерта на 1m

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Глобальные переменные
tracked_symbols = set()  # Все символы с 1D объёмом < 1M USDT
sent_alerts = {}  # Для предотвращения дублирования алертов

# Глобальные переменные для управления задачами
scanner_task = None
application = None
bot_instance = None

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
                        # Форматируем: BTC_USDT -> BTCUSDT
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
    
    # Получаем данные за 1 день (последнюю свечу)
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
                    "interval": "Day1",  # Дневной таймфрейм
                    "limit": 1  # Только последняя свеча
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
                            logger.debug(f"{symbol}: 1D объём = {volume:,.0f} USDT")
                            return volume
                
                logger.debug(f"Не удалось получить 1D объём для {symbol}")
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


# ====================== ЗАГРУЗКА И ФИЛЬТРАЦИЯ СИМВОЛОВ ======================
async def load_and_filter_symbols():
    """Загружаем ВСЕ символы и фильтруем по 1D объёму < 1M USDT"""
    global tracked_symbols
    
    logger.info("Начинаю загрузку и фильтрацию символов...")
    
    try:
        # 1. Получаем ВСЕ символы фьючерсов
        all_symbols = await get_all_futures_symbols()
        if not all_symbols:
            logger.error("Не удалось получить символы фьючерсов")
            return False
        
        logger.info(f"Получено {len(all_symbols)} символов. Начинаю проверку 1D объёма...")
        
        # 2. Проверяем 1D объём для КАЖДОГО символа (ВСЕХ)
        low_volume_symbols = []
        total_symbols = len(all_symbols)
        
        # Используем asyncio.gather для параллельной проверки
        batch_size = 20  # Проверяем по 20 символов за раз
        
        for i in range(0, total_symbols, batch_size):
            batch = all_symbols[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_symbols + batch_size - 1) // batch_size
            
            logger.info(f"Проверяю батч {batch_num}/{total_batches} ({len(batch)} символов)")
            
            # Создаем задачи для параллельной проверки
            tasks = []
            for symbol in batch:
                task = asyncio.create_task(get_1d_volume(symbol))
                tasks.append((symbol, task))
            
            # Ожидаем завершения всех задач в батче
            for symbol, task in tasks:
                try:
                    daily_volume = await task
                    
                    if daily_volume <= DAILY_VOLUME_LIMIT:
                        low_volume_symbols.append(symbol)
                        logger.debug(f"✓ {symbol}: {daily_volume:,.0f} USDT (< {DAILY_VOLUME_LIMIT:,})")
                    else:
                        logger.debug(f"✗ {symbol}: {daily_volume:,.0f} USDT (>= {DAILY_VOLUME_LIMIT:,})")
                        
                except Exception as e:
                    logger.error(f"Ошибка проверки {symbol}: {e}")
            
            # Небольшая задержка между батчами чтобы не перегружать API
            if i + batch_size < total_symbols:
                await asyncio.sleep(1)
        
        tracked_symbols = set(low_volume_symbols)
        
        logger.info(f"✅ Загрузка завершена!")
        logger.info(f"   Всего символов: {total_symbols}")
        logger.info(f"   Проверено: {total_symbols}")
        logger.info(f"   Отслеживается: {len(tracked_symbols)} (1D объём < {DAILY_VOLUME_LIMIT:,} USDT)")
        
        if tracked_symbols:
            sample = list(tracked_symbols)[:10]
            logger.info(f"   Примеры: {sample}")
            
            # Отправляем уведомление в Telegram о количестве отслеживаемых пар
            try:
                await bot_instance.send_message(
                    chat_id=MY_USER_ID,
                    text=f"✅ <b>Сканер запущен</b>\n\n"
                         f"Отслеживается: <b>{len(tracked_symbols)}</b> пар\n"
                         f"Условие: 1D объём < {DAILY_VOLUME_LIMIT:,} USDT\n"
                         f"Примеры: {', '.join(sample)}",
                    parse_mode="HTML"
                )
            except:
                pass
        
        return True
        
    except Exception as e:
        logger.error(f"Критическая ошибка при загрузке символов: {e}", exc_info=True)
        return False


# ====================== СКАНЕР ВСПЛЕСКОВ ОБЪЁМА ======================
async def volume_spike_scanner():
    """Сканируем все низковольюмные пары на всплески объёма на 1m"""
    logger.info(f"🚀 Сканер запущен! Отслеживаю {len(tracked_symbols)} пар")
    
    if len(tracked_symbols) == 0:
        logger.warning("Нет пар для отслеживания! Отправляю уведомление...")
        try:
            await bot_instance.send_message(
                chat_id=MY_USER_ID,
                text="⚠️ <b>ВНИМАНИЕ</b>\n\n"
                     "Сканер запущен, но не найдено пар для отслеживания!\n"
                     "Проверьте настройки или API доступ.",
                parse_mode="HTML"
            )
        except:
            pass
    
    iteration = 0
    
    while True:
        try:
            current_minute = datetime.now().strftime("%Y%m%d%H%M")
            iteration += 1
            
            # Логируем статус каждые 10 итераций (примерно каждые 5 минут)
            if iteration % 10 == 1:
                logger.info(f"Итерация {iteration}. Отслеживается {len(tracked_symbols)} пар. Активных алертов: {len(sent_alerts)}")
            
            # Если нет пар, пробуем перезагрузить
            if len(tracked_symbols) == 0:
                logger.warning("Нет пар для отслеживания. Перезагружаю...")
                await load_and_filter_symbols()
                await asyncio.sleep(30)
                continue
            
            # Получаем список пар для проверки
            symbols_list = list(tracked_symbols)
            
            # Ограничиваем количество проверок за итерацию
            max_per_iteration = min(100, len(symbols_list))  # Увеличил до 100
            
            # Перемешиваем список для равномерной проверки
            import random
            random.shuffle(symbols_list)
            
            # Проверяем каждую пару
            checked_count = 0
            for symbol in symbols_list[:max_per_iteration]:
                try:
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
                        
                        # Рассчитываем изменения
                        volume_change_pct = ((curr_vol - prev_vol) / max(prev_vol, 1)) * 100
                        price_change_pct = ((curr_price - prev_price) / max(prev_price, 0.00000001)) * 100
                        
                        # Определяем эмодзи для цены
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
                        
                        # Форматируем сообщение
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
                            f"• Стало: <b>{curr_price:.8f}</b>\n"
                            f"• Изменение: {price_emoji} <b>{price_change_pct:+.2f}%</b>\n\n"
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
                            logger.info(f"🚨 АЛЕРТ: {symbol} | {prev_vol:,}→{curr_vol:,} (+{volume_change_pct:.0f}%) | Цена: {price_change_pct:+.2f}%")
                            
                        except Exception as e:
                            logger.error(f"Ошибка отправки: {e}")
                    
                    checked_count += 1
                            
                except Exception as e:
                    logger.debug(f"Ошибка обработки {symbol}: {str(e)[:100]}")
                    continue
            
            # Очищаем старые алерты (старше 1 часа)
            current_time = time.time()
            expired = [k for k, v in sent_alerts.items() if current_time - v > 3600]
            for exp in expired:
                sent_alerts.pop(exp, None)
            
            # Обновляем список символов каждые 6 часов
            if iteration % 720 == 0:  # 30 сек * 720 = 6 часов
                logger.info("🔄 Обновляю список символов (каждые 6 часов)...")
                await load_and_filter_symbols()
            
            # Ждем 30 секунд до следующей проверки
            await asyncio.sleep(30)
            
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
        f"<b>Статус:</b> ✅ Активен\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>Фильтр по 1D объёму:</b> < {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"<b>Таймфрейм для алертов:</b> 1 минута\n"
        f"<b>Условие алерта:</b> Объём < {MIN_PREV_VOLUME:,} → > {MIN_CURRENT_VOLUME:,} USDT\n\n"
        f"<i>Команды: /stats, /list, /refresh</i>",
        parse_mode="HTML"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    await update.message.reply_text(
        f"<b>📈 Статистика</b>\n\n"
        f"<b>Отслеживаемых пар:</b> {len(tracked_symbols)}\n"
        f"<b>Алертов сегодня:</b> {len(sent_alerts)}\n"
        f"<b>Лимит 1D объёма:</b> {DAILY_VOLUME_LIMIT:,} USDT\n"
        f"<b>Условие 1M алерта:</b> <{MIN_PREV_VOLUME:,} → >{MIN_CURRENT_VOLUME:,} USDT\n"
        f"<b>Время:</b> {datetime.now().strftime('%H:%M:%S')}",
        parse_mode="HTML"
    )


async def list_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список отслеживаемых пар"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    if not tracked_symbols:
        await update.message.reply_text("ℹ️ Нет отслеживаемых пар")
        return
    
    symbols_list = sorted(list(tracked_symbols))
    
    # Разбиваем на части по 30 символов
    chunks = [symbols_list[i:i+30] for i in range(0, len(symbols_list), 30)]
    
    for i, chunk in enumerate(chunks):
        await update.message.reply_text(
            f"<b>📋 Отслеживаемые пары ({i+1}/{len(chunks)})</b>\n\n" +
            "\n".join(f"• {symbol}" for symbol in chunk),
            parse_mode="HTML"
        )


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновить список пар"""
    if update.effective_user.id != MY_USER_ID:
        return
    
    await update.message.reply_text("🔄 Обновляю список пар...")
    
    success = await load_and_filter_symbols()
    
    if success:
        await update.message.reply_text(
            f"✅ Обновлено!\n"
            f"Отслеживается: {len(tracked_symbols)} пар\n"
            f"Условие: 1D объём < {DAILY_VOLUME_LIMIT:,} USDT",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Ошибка обновления")


# ====================== УПРАВЛЕНИЕ ЖИЗНЕННЫМ ЦИКЛОМ ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global scanner_task, application, bot_instance
    
    logger.info("=== Запуск MEXC 1D Volume Scanner ===")
    
    # Создаем экземпляр бота
    bot_instance = Bot(token=TELEGRAM_TOKEN)
    
    # Инициализируем Telegram приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("list", list_symbols))
    application.add_handler(CommandHandler("refresh", refresh))
    
    # Загружаем и фильтруем символы
    await load_and_filter_symbols()
    
    # Запускаем сканер
    scanner_task = asyncio.create_task(volume_spike_scanner())
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
        "service": "MEXC 1D Volume Scanner",
        "status": "active",
        "timestamp": datetime.now().isoformat(),
        "tracked_pairs": len(tracked_symbols),
        "daily_volume_limit": DAILY_VOLUME_LIMIT,
        "alerts_today": len(sent_alerts)
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
















