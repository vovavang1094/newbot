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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
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

# Файл для хранения предыдущих объемов
VOLUME_HISTORY_FILE = "/tmp/volume_history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Глобальные переменные
ALL_SYMBOLS = set()
volume_history = {}  # Для хранения предыдущих объемов
last_prices = {}  # Для хранения текущих цен

# ====================== СОХРАНЕНИЕ И ЗАГРУЗКА ИСТОРИИ ======================
def save_history():
    try:
        with open(VOLUME_HISTORY_FILE, 'w') as f:
            json.dump(volume_history, f, indent=2)
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
                        # Берем все пары USDT
                        ALL_SYMBOLS = {x["symbol"].replace("_USDT", "USDT") for x in j["data"] if "_USDT" in x["symbol"]}
                        logger.info(f"Загружено {len(ALL_SYMBOLS)} пар для мониторинга")
                        return True
        # Если API не ответил, используем базовый список
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
    query = f"symbol={sym}&interval=Min1&limit=1"
    sign = hmac.new(MEXC_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": ts, "Signature": sign}
    
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                params={"symbol": sym, "interval": "Min1", "limit": 1},
                headers=headers,
                timeout=ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data"):
                        # Получаем объем (amount) и цену закрытия (close)
                        data = j["data"]
                        volume = int(float(data["amount"][0])) if data.get("amount") else 0
                        price = float(data["close"][0]) if data.get("close") else 0
                        return volume, price
    except Exception as e:
        logger.error(f"Ошибка получения данных для {symbol}: {e}")
    return 0, 0

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
                for symbol in list(ALL_SYMBOLS):
                    try:
                        # Получаем текущий объем и цену
                        current_volume, current_price = await fetch_volume_and_price(symbol)
                        
                        if current_volume == 0:
                            continue
                        
                        # Сохраняем текущую цену
                        last_prices[symbol] = current_price
                        
                        # Получаем предыдущий объем
                        prev_volume = volume_history.get(symbol, {}).get('volume', 0)
                        prev_time = volume_history.get(symbol, {}).get('timestamp', 0)
                        
                        # Проверяем условие: предыдущий объем < 1000, текущий объем > 2000
                        if (prev_volume < 1000 and 
                            current_volume > 2000 and 
                            (time.time() - prev_time) > 30):  # Защита от повторных алертов
                            
                            # Рассчитываем процент изменения
                            if prev_volume > 0:
                                change_percent = ((current_volume - prev_volume) / prev_volume) * 100
                            else:
                                change_percent = 99999  # Очень большой процент роста
                            
                            # Формируем сообщение
                            symbol_name = symbol.replace("USDT", "")
                            url = f"https://www.mexc.com/ru-RU/futures/{symbol_name}_USDT"
                            
                            message = (
                                f"🚨 <b>ВСПЛЕСК ОБЪЁМА {symbol_name}</b> 🚨\n\n"
                                f"📈 <b>Изменение:</b> {change_percent:+.1f}%\n"
                                f"📊 <b>Пред. объем:</b> {prev_volume:,} USDT\n"
                                f"📊 <b>Тек. объем:</b> {current_volume:,} USDT\n"
                                f"💰 <b>Цена:</b> ${current_price:.4f}\n"
                                f"🔗 <a href='{url}'>MEXC Futures: {symbol_name}/USDT</a>"
                            )
                            
                            # Отправляем уведомление всем авторизованным пользователям
                            await application.bot.send_message(
                                ALLOWED_USER_ID,
                                message,
                                parse_mode="HTML",
                                disable_web_page_preview=True
                            )
                            
                            alerts_sent += 1
                            logger.info(f"Алерт отправлен для {symbol}: {prev_volume} -> {current_volume} USDT")
                        
                        # Обновляем историю
                        volume_history[symbol] = {
                            'volume': current_volume,
                            'timestamp': time.time()
                        }
                        
                        # Небольшая задержка между запросами, чтобы не перегружать API
                        await asyncio.sleep(0.1)
                        
                    except Exception as e:
                        logger.error(f"Ошибка мониторинга {symbol}: {e}")
                        continue
                
                # Сохраняем историю каждую итерацию
                if alerts_sent > 0 or time.time() % 300 < 30:  # Сохраняем каждые 5 минут или если были алерты
                    save_history()
                
                logger.info(f"Цикл мониторинга завершен. Отправлено алертов: {alerts_sent}")
                await asyncio.sleep(30)  # Пауза между циклами мониторинга
                
            except asyncio.CancelledError:
                logger.info("Мониторинг остановлен")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(60)
                
    except Exception as e:
        logger.error(f"Критическая ошибка мониторинга: {e}")
    finally:
        # Сохраняем историю перед завершением
        save_history()

# ====================== ОБРАБОТЧИК КОМАНД ======================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return
    
    await update.message.reply_text(
        "🤖 <b>MEXC Volume Spike Bot</b>\n\n"
        "📊 <b>Мониторинг:</b> Все пары на 1m таймфрейме\n"
        "🔔 <b>Условие:</b> Пред. объем < 1,000 USDT → Тек. объем > 2,000 USDT\n"
        "⚡ <b>Частота:</b> Проверка каждые 30 секунд\n"
        f"👁️ <b>Отслеживается:</b> {len(ALL_SYMBOLS)} пар\n\n"
        "Бот автоматически отправляет уведомления при обнаружении всплесков объема.",
        parse_mode="HTML"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return
    
    # Показываем статистику
    monitored_count = len(ALL_SYMBOLS)
    history_count = len(volume_history)
    
    status_text = (
        f"📊 <b>Статус мониторинга</b>\n\n"
        f"✅ <b>Мониторится пар:</b> {monitored_count}\n"
        f"📈 <b>В истории:</b> {history_count} пар\n"
        f"⏰ <b>Последнее обновление:</b> {time.strftime('%H:%M:%S')}\n\n"
    )
    
    # Добавляем топ-5 пар по текущему объему
    if volume_history:
        status_text += "<b>Текущие объемы (топ-5):</b>\n"
        
        # Сортируем по объему
        sorted_items = sorted(volume_history.items(), 
                             key=lambda x: x[1].get('volume', 0), 
                             reverse=True)
        
        for i, (symbol, data) in enumerate(sorted_items[:5]):
            volume = data.get('volume', 0)
            price = last_prices.get(symbol, 0)
            status_text += f"{i+1}. {symbol}: {volume:,} USDT (${price:.4f})\n"
    else:
        status_text += "📭 Нет данных об объемах\n"
    
    await update.message.reply_text(status_text, parse_mode="HTML")

# ====================== POST_INIT ======================
async def post_init(application: Application):
    load_history()  # Загружаем историю объемов
    await load_symbols()  # Загружаем символы
    # Запускаем мониторинг
    asyncio.create_task(monitor_all_symbols(application))
    logger.info("Бот инициализирован и готов к работе")

# ====================== ВЕБ-СЕРВЕР ДЛЯ RENDER ======================
web_app = FastAPI()

@web_app.get("/")
async def root():
    return {
        "status": "MEXC Volume Spike Bot работает",
        "monitored_pairs": len(ALL_SYMBOLS),
        "in_history": len(volume_history),
        "time": time.strftime("%H:%M:%S")
    }

@web_app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time()}

def run_web_server():
    uvicorn.run(web_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")

# ====================== ЗАПУСК БОТА ======================
def run_bot():
    # Создаем application
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Добавляем обработчики команд
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^/start$'), start_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^/status$'), status_command))

    logger.info("MEXC Volume Spike Bot запускается...")
    
    try:
        # Используем стандартный run_polling
        application.run_polling(
            drop_pending_updates=True,
            timeout=30,
            allowed_updates=Update.ALL_TYPES
        )
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
    finally:
        # Сохраняем историю при завершении
        save_history()
        logger.info("История сохранена")

# ====================== ГЛАВНАЯ ФУНКЦИЯ ======================
if __name__ == "__main__":
    # Запускаем веб-сервер в отдельном потоке
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    # Даем веб-серверу время запуститься
    time.sleep(2)
    
    # Запускаем бота
    run_bot()
