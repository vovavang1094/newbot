import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
from dotenv import load_dotenv
from aiohttp import ClientTimeout
from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, CommandHandler
from fastapi import FastAPI
import uvicorn
import threading

# ====================== НАСТРОЙКИ ======================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID"))

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

DAILY_VOLUME_LIMIT = 2_000_000  # USDT — максимальный дневной объём

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALL_SYMBOLS = set()
LOW_VOLUME_SYMBOLS = set()
bot = Bot(token=TELEGRAM_TOKEN)
sent_alerts = set()

# ====================== MEXC API ======================
async def load_symbols_and_filter_low_volume():
    global ALL_SYMBOLS, LOW_VOLUME_SYMBOLS
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://contract.mexc.com/api/v1/contract/detail", timeout=15) as resp:
                if resp.status != 200:
                    logger.error(f"API вернул {resp.status} — проверь ключи")
                    return False
                data = await resp.json()
                if not data.get("success"):
                    logger.error("API не success — ошибка в ответе")
                    return False

                symbols_data = data["data"]
                ALL_SYMBOLS = {
                    s["symbol"].replace("_USDT", "USDT")
                    for s in symbols_data
                    if s["symbol"].endswith("_USDT") and s.get("state") == 1
                }

                LOW_VOLUME_SYMBOLS = {
                    s["symbol"].replace("_USDT", "USDT")
                    for s in symbols_data
                    if float(s.get("volume24h", 0)) <= DAILY_VOLUME_LIMIT and s["symbol"].endswith("_USDT") and s.get("state") == 1
                }

                logger.info(f"Загружено ВСЕГО: {len(ALL_SYMBOLS)} | ОТСЛЕЖИВАЕМЫХ (≤{DAILY_VOLUME_LIMIT:,}): {len(LOW_VOLUME_SYMBOLS)}")
                return True
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
        return False


async def get_1m_data(symbol: str):
    sym = symbol.replace("USDT", "_USDT")
    ts = str(int(time.time() * 1000))
    params = f"symbol={sym}&interval=Min1&limit=2"
    sign = hmac.new(MEXC_SECRET_KEY.encode(), params.encode(), hashlib.sha256).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": ts, "Signature": sign}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                params={"symbol": sym, "interval": "Min1", "limit": 2},
                headers=headers,
                timeout=10
            ) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    if j.get("success") and len(j["data"]["close"]) >= 2:
                        d = j["data"]
                        prev_vol = int(float(d["amount"][0]))
                        curr_vol = int(float(d["amount"][1]))
                        prev_price = float(d["close"][0])
                        curr_price = float(d["close"][1])
                        return prev_vol, curr_vol, prev_price, curr_price
    except:
        pass
    return None, None, None, None


# ====================== СКАНЕР ======================
async def volume_spike_scanner():
    logger.info("Сканер запущен")
    while True:
        try:
            minute_key = time.strftime("%Y%m%d%H%M")

            for symbol in LOW_VOLUME_SYMBOLS:
                prev_vol, curr_vol, prev_price, curr_price = await get_1m_data(symbol)
                if not all(x is not None for x in [prev_vol, curr_vol]):
                    continue

                if prev_vol < 1000 and curr_vol > 2000:
                    alert_id = f"{symbol}_{minute_key}"
                    if alert_id in sent_alerts:
                        continue

                    change_pct = (curr_price - prev_price) / prev_price * 100 if prev_price > 0 else 0
                    emoji = "UP" if change_pct >= 0 else "DOWN"

                    message = (
                        f"<b>ВСПЛЕСК ОБЪЁМА!</b>\n\n"
                        f"<b>{symbol}</b>\n"
                        f"Пред: <b>{prev_vol:,}</b> → Тек: <b>{curr_vol:,}</b> USDT\n"
                        f"Цена: {prev_price:.8f} → <b>{curr_price:.8f}</b>\n"
                        f"Δ: {emoji} <b>{change_pct:+.2f}%</b>\n\n"
                        f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>Открыть на MEXC</a>"
                    )

                    try:
                        await bot.send_message(
                            chat_id=MY_USER_ID,
                            text=message,
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                        sent_alerts.add(alert_id)
                        logger.info(f"АЛЕРТ → {symbol} | {curr_vol:,} USDT")
                    except Exception as e:
                        logger.error(f"Ошибка отправки: {e}")

            await asyncio.sleep(58)
        except Exception as e:
            logger.error(f"Ошибка в сканере: {e}")
            await asyncio.sleep(60)


# ====================== /start ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        await update.message.reply_text("Доступ запрещён")
        return
    await update.message.reply_text(
        "<b>Сканер запущен!</b>\nОтслеживаю монеты с объёмом ≤ 2M USDT в день",
        parse_mode="HTML"
    )


# ====================== ВЕБ-СЕРВЕР ======================
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok", "tracked": len(LOW_VOLUME_SYMBOLS)}


def run_web():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))

    async def init():
        if await load_symbols_and_filter_low_volume():
            application.create_task(volume_spike_scanner())
        else:
            logger.error("Символы не загрузились — сканер не стартует")

    # Запускаем инициализацию
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init())

    # Запускаем polling (основной цикл)
    logger.info("Бот запущен! Напиши /start боту в личку.")
    application.run_polling(drop_pending_updates=True)





