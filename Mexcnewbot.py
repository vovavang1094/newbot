import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
import json
from dotenv import load_dotenv
from aiohttp import ClientTimeout
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)
from fastapi import FastAPI
import uvicorn
import threading

# ====================== НАСТРОЙКИ ======================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

# Файл для хранения истории
VOLUME_HISTORY_FILE = "/tmp/volume_history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Глобальные переменные
ALL_SYMBOLS = set()
volume_history = {}  # История объемов: {symbol: [{'timestamp': ts, 'volume': vol}, ...]}
last_prices = {}  # Текущие цены
last_alert_time = {}  # Время последнего алерта для каждой монеты

# ====================== СОХРАНЕНИЕ И ЗАГРУЗКА ИСТОРИИ ======================
def save_history():
    try:
        # Сохраняем только последние 10 записей для каждой монеты
        history_to_save = {}
        for symbol, history in volume_history.items():
            if history:
                history_to_save[symbol] = history[-10:]  # Последние 10 записей
        with open(VOLUME_HISTORY_FILE, 'w') as f:
            json.dump(history_to_save, f, indent=2)
        logger.info("История объемов сохранена")
    except Exception as e:
        logger.error(f"Ошибка сохранения истории: {e}")

def load_history():
    global volume_history
    try:
        if os.path.exists(VOLUME_HISTORY_FILE):
            with open(VOLUME_HISTORY_FILE, 'r') as f:
                volume_history = json.load(f)
            logger.info(f"История объемов загружена: {len(volume_history)} пар")
        else:
            volume_history = {}
            logger.info("Файл истории не найден, создаю новый")
    except Exception as e:
        logger.error(f"Ошибка загрузки истории: {e}")
        volume_history = {}

# ====================== MEXC API ======================
async def load_symbols():
    global ALL_SYMBOLS
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://contract.mexc.com/api/v1/contract/detail", timeout=ClientTimeout(total=10)) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data"):
                        # Берем только популярные пары для тестирования
                        all_pairs = {x["symbol"].replace("_USDT", "USDT") for x in j["data"] if "_USDT" in x["symbol"]}
                        # Выбираем топ-20 по популярности (первые в списке)
                        ALL_SYMBOLS = set(list(all_pairs)[:20])
                        logger.info(f"Загружено {len(ALL_SYMBOLS)} пар для мониторинга")
                        return True
        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "MATICUSDT"}
        logger.info(f"Используем базовый список из {len(ALL_SYMBOLS)} пар")
        return True
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "MATICUSDT"}
        return False

async def fetch_volume_and_price(symbol: str) -> tuple:
    """Получает объем и цену для 1м таймфрейма"""
    sym = symbol.replace("USDT", "_USDT")
    ts = str(int(time.time() * 1000))
    query = f"symbol={sym}&interval=Min1&limit=2"  # Берем 2 свечи: текущую и предыдущую
    sign = hmac.new(MEXC_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": ts, "Signature": sign}
    
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                params={"symbol": sym, "interval": "Min1", "limit": 2},
                headers=headers,
                timeout=ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data"):
                        data = j["data"]
                        # Предыдущая свеча (индекс 1) и текущая (индекс 0)
                        if len(data.get("amount", [])) >= 2:
                            prev_volume = int(float(data["amount"][1]))  # Объем предыдущей минуты
                            current_volume = int(float(data["amount"][0]))  # Объем текущей минуты
                            current_price = float(data["close"][0]) if data.get("close") else 0
                            return prev_volume, current_volume, current_price
    except Exception as e:
        logger.error(f"Ошибка получения данных для {symbol}: {e}")
    return 0, 0, 0

# ====================== ОСНОВНОЙ МОНИТОРИНГ ======================
async def monitor_all_symbols(application: Application):
    await asyncio.sleep(5)
    loaded = await load_symbols()
    
    if not loaded:
        logger.warning("Не удалось загрузить символы, используем базовый список")
    
    logger.info(f"Мониторинг запущен для {len(ALL_SYMBOLS)} пар")
    
    try:
        while True:
            try:
                alerts_sent = 0
                current_time = time.time()
                
                for symbol in list(ALL_SYMBOLS):
                    try:
                        # Получаем объем предыдущей минуты и объем текущей минуты
                        prev_volume, current_volume, current_price = await fetch_volume_and_price(symbol)
                        
                        if current_volume == 0:
                            continue
                        
                        # Сохраняем текущую цену
                        last_prices[symbol] = current_price
                        
                        # Сохраняем историю
                        if symbol not in volume_history:
                            volume_history[symbol] = []
                        
                        volume_history[symbol].append({
                            'timestamp': current_time,
                            'prev_volume': prev_volume,
                            'current_volume': current_volume,
                            'price': current_price
                        })
                        
                        # Ограничиваем историю
                        if len(volume_history[symbol]) > 100:
                            volume_history[symbol] = volume_history[symbol][-50:]
                        
                        # Проверяем условие: объем предыдущей минуты < 1000, объем текущей минуты > 2000
                        if (prev_volume < 1000 and 
                            current_volume > 2000 and 
                            current_volume > prev_volume):  # Убедимся, что объем вырос
                            
                            # Проверяем, не было ли алерта за последние 5 минут
                            last_alert = last_alert_time.get(symbol, 0)
                            if current_time - last_alert < 300:  # 5 минут
                                continue
                            
                            # Рассчитываем процент изменения
                            if prev_volume > 0:
                                change_percent = ((current_volume - prev_volume) / prev_volume) * 100
                            else:
                                change_percent = 99999  # Рост от 0
                            
                            # Формируем сообщение
                            symbol_name = symbol.replace("USDT", "")
                            url = f"https://www.mexc.com/ru-RU/futures/{symbol_name}_USDT"
                            
                            message = (
                                f"🚨 <b>ВСПЛЕСК ОБЪЁМА {symbol_name}</b> 🚨\n\n"
                                f"📈 <b>Изменение за 1 минуту:</b> {change_percent:+.1f}%\n"
                                f"📊 <b>Объем (пред. минута):</b> {prev_volume:,} USDT\n"
                                f"📊 <b>Объем (тек. минута):</b> {current_volume:,} USDT\n"
                                f"💰 <b>Цена:</b> ${current_price:.4f}\n"
                                f"🔗 <a href='{url}'>MEXC Futures: {symbol_name}/USDT</a>\n\n"
                                f"<i>Объем вырос с менее 1,000 до более 2,000 USDT за 1 минуту</i>"
                            )
                            
                            # Отправляем уведомление
                            try:
                                await application.bot.send_message(
                                    ALLOWED_USER_ID,
                                    message,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True
                                )
                                
                                alerts_sent += 1
                                last_alert_time[symbol] = current_time
                                logger.info(f"Алерт для {symbol}: {prev_volume} → {current_volume} USDT ({change_percent:+.1f}%)")
                            except Exception as e:
                                logger.error(f"Ошибка отправки сообщения: {e}")
                        
                        # Задержка между запросами
                        await asyncio.sleep(0.1)
                        
                    except Exception as e:
                        logger.error(f"Ошибка мониторинга {symbol}: {e}")
                        continue
                
                # Сохраняем историю
                if alerts_sent > 0 or current_time % 300 < 30:
                    save_history()
                
                if alerts_sent > 0:
                    logger.info(f"Отправлено алертов: {alerts_sent}")
                
                # Ждем до следующей минуты (55 секунд, чтобы попасть на начало следующей минуты)
                await asyncio.sleep(55)
                
            except asyncio.CancelledError:
                logger.info("Мониторинг остановлен")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(60)
                
    except Exception as e:
        logger.error(f"Критическая ошибка мониторинга: {e}")
    finally:
        save_history()

# ====================== ОБРАБОТЧИК КОМАНД ======================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    await update.message.reply_text(
        f"🤖 <b>MEXC 1-Minute Volume Spike Bot</b>\n\n"
        f"📊 <b>Мониторинг:</b> {len(ALL_SYMBOLS)} пар на 1m таймфрейме\n"
        f"🔔 <b>Условие алерта:</b>\n"
        f"   • Объем на предыдущей минуте < 1,000 USDT\n"
        f"   • Объем на текущей минуте > 2,000 USDT\n"
        f"⏰ <b>Частота проверки:</b> Каждую минуту\n"
        f"🛡️ <b>Защита от спама:</b> Максимум 1 алерт в 5 минут на пару\n\n"
        f"👤 <b>Ваш ID:</b> {user_id}\n"
        f"📈 <b>Последняя цена BTC:</b> ${last_prices.get('BTCUSDT', 0):.2f}",
        parse_mode="HTML"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Статистика за последний час
    one_hour_ago = time.time() - 3600
    alert_count = 0
    
    for symbol, alerts in last_alert_time.items():
        if alerts > one_hour_ago:
            alert_count += 1
    
    status_text = (
        f"📊 <b>Статистика мониторинга</b>\n\n"
        f"✅ <b>Отслеживается пар:</b> {len(ALL_SYMBOLS)}\n"
        f"🚨 <b>Алертов за час:</b> {alert_count}\n"
        f"⏰ <b>Текущее время:</b> {time.strftime('%H:%M:%S')}\n\n"
        f"<b>Последние 5 цен:</b>\n"
    )
    
    # Показываем последние 5 пар с ценами
    count = 0
    for symbol in list(ALL_SYMBOLS)[:5]:
        price = last_prices.get(symbol, 0)
        if price > 0:
            status_text += f"• {symbol}: ${price:.4f}\n"
            count += 1
    
    if count == 0:
        status_text += "Нет данных о ценах\n"
    
    await update.message.reply_text(status_text, parse_mode="HTML")

# ====================== POST_INIT ======================
async def post_init(application: Application):
    load_history()
    await load_symbols()
    
    if ALLOWED_USER_ID == 0:
        logger.warning("ALLOWED_USER_ID не установлен! Бот не будет отправлять уведомления.")
    else:
        logger.info(f"Уведомления будут отправляться пользователю: {ALLOWED_USER_ID}")
    
    # Запускаем мониторинг
    asyncio.create_task(monitor_all_symbols(application))
    logger.info("Бот инициализирован")

# ====================== ВЕБ-СЕРВЕР ДЛЯ RENDER ======================
web_app = FastAPI()

@web_app.get("/")
async def root():
    return {
        "status": "MEXC 1-Minute Volume Bot работает",
        "pairs_monitored": len(ALL_SYMBOLS),
        "last_update": time.strftime("%H:%M:%S")
    }

@web_app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time()}

def run_web_server():
    uvicorn.run(web_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")

# ====================== ЗАПУСК БОТА ======================
def run_bot():
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Команды
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^/start$'), start_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^/stats$'), stats_command))

    logger.info("MEXC 1-Minute Volume Spike Bot запускается...")
    
    try:
        application.run_polling(
            drop_pending_updates=True,
            timeout=30,
            allowed_updates=Update.ALL_TYPES
        )
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
    finally:
        save_history()

# ====================== ГЛАВНАЯ ФУНКЦИЯ ======================
if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    time.sleep(2)
    run_bot()

