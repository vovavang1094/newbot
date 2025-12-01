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
import uvicorn
import threading

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID") or 0)
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY", "")

DAILY_VOLUME_LIMIT = 2_000_000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOW_VOLUME_SYMBOLS = set()
bot = Bot(token=TELEGRAM_TOKEN)
sent_alerts = set()

# ====================== ЗАГРУЗКА СИМВОЛОВ ======================
async def load_low_volume_symbols():
    global LOW_VOLUME_SYMBOLS
    if not MEXC_API_KEY or not MEXC_SECRET_KEY:
        logger.error("MEXC_API_KEY или MEXC_SECRET_KEY не указаны!")
        LOW_VOLUME_SYMBOLS = {"1000BONKUSDT", "PEPEUSDT", "FLOKIUSDT", "SHIBUSDT"}
        logger.warning("Используется fallback-лист (4 монеты)")
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://contract.mexc.com/api/v1/contract/detail", timeout=15) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()

                if not data.get("success"):
                    raise Exception("API не вернул success")

                symbols = [
                    s["symbol"].replace("_USDT", "USDT")
                    for s in data["data"]
                    if s["symbol"].endswith("_USDT")
                    and s.get("state") == 1
                    and float(s.get("volume24h", 0)) <= DAILY_VOLUME_LIMIT
                ]

                LOW_VOLUME_SYMBOLS = set(symbols)
                logger.info(f"Успешно загружено {len(LOW_VOLUME_SYMBOLS)} монет с объёмом ≤ 2M USDT")
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
        LOW_VOLUME_SYMBOLS = {"1000BONKUSDT", "PEPEUSDT", "FLOKIUSDT", "SHIBUSDT", "1000SATSUSDT"}
        logger.warning("Используется аварийный список (5 монет)")


# ====================== ДАННЫЕ 1M ======================
async def get_1m_data(symbol: str):
    if not MEXC_API_KEY:
        return 500, 2500, 0.001, 0.0011  # фейковые данные для теста

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
                        return (
                            int(float(d["amount"][0])),
                            int(float(d["amount"][1])),
                            float(d["close"][0]),
                            float(d["close"][1])
                        )
    except:
        pass
    return None, None, None, None


# ====================== СКАНЕР ======================
async def volume_spike_scanner():
    await load_low_volume_symbols()
    logger.info(f"Сканер запущен → отслеживаю {len(LOW_VOLUME_SYMBOLS)} монет")

    while True:
        try:
            minute = time.strftime("%Y%m%d%H%M")
            for symbol in list(LOW_VOLUME_SYMBOLS):
                prev_vol, curr_vol, prev_price, curr_price = await get_1m_data(symbol)
                if prev_vol is None:
                    continue
                if prev_vol < 1000 and curr_vol > 2000:
                    if f"{symbol}_{minute}" in sent_alerts:
                        continue
                    change = (curr_price - prev_price) / prev_price * 100
                    msg = (
                        f"<b>ВСПЛЕСК НА {symbol}!</b>\n\n"
                        f"Объём: {prev_vol:,} → <b>{curr_vol:,}</b> USDT\n"
                        f"Цена: {prev_price:.8f} → {curr_price:.8f} ({change:+.2f}%)\n\n"
                        f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>MEXC ФЬЮЧЕРС</a>"
                    )
                    try:
                        await bot.send_message(MY_USER_ID, msg, parse_mode="HTML", disable_web_page_preview=True)
                        sent_alerts.add(f"{symbol}_{minute}")
                        logger.info(f"АЛЕРТ: {symbol}")
                    except Exception as e:
                        if "chat not found" in str(e).lower():
                            logger.warning("Напиши боту /start!")
                        else:
                            logger.error(f"Ошибка отправки: {e}")
            await asyncio.sleep(58)
        except Exception as e:
            logger.error(f"Ошибка сканера: {e}")
            await asyncio.sleep(60)


# ====================== /start ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        return
    await update.message.reply_text(
        "<b>Сканер всплесков объёма активен!</b>\n\n"
        "Отслеживаю только низколиквидные альты (≤ 2M USDT в день)\n"
        "Условие: < 1k → > 2k за минуту",
        parse_mode="HTML"
    )


# ====================== ВЕБ + ЗАПУСК ======================
app = FastAPI()
@app.get("/")
async def root():
    return {"status": "alive", "tracking": len(LOW_VOLUME_SYMBOLS)}

def run_web():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    # Правильный запуск на Render
    async def main():
        await load_low_volume_symbols()
        app.create_task(volume_spike_scanner())
        await app.run_polling(drop_pending_updates=True)

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")






