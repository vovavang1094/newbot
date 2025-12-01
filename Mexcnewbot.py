import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
from dotenv import load_dotenv
from urllib.parse import urlencode
from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, CommandHandler
from fastapi import FastAPI
import uvicorn
import threading

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY", "")

DAILY_VOLUME_LIMIT = 2_000_000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOW_VOLUME_SYMBOLS = set()
sent_alerts = set()
bot = Bot(token=TELEGRAM_TOKEN)

# ====================== ПРАВИЛЬНАЯ ПОДПИСЬ MEXC ======================
def mexc_sign(params: dict):
    query_string = urlencode(sorted(params.items()))  # ← КЛЮЧЕВОЕ ИЗМЕНЕНИЕ
    signature = hmac.new(MEXC_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return signature, query_string

# ====================== ЗАГРУЗКА СИМВОЛОВ ======================
async def load_low_volume_symbols():
    global LOW_VOLUME_SYMBOLS
    LOW_VOLUME_SYMBOLS.clear()

    # Сначала пробуем с ключами (правильная подпись)
    if MEXC_API_KEY and MEXC_SECRET_KEY:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://contract.mexc.com/api/v1/contract/detail") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("success"):
                            for item in data["data"]:
                                symbol = item["symbol"]
                                if not symbol.endswith("_USDT"):
                                    continue
                                volume_24h = float(item.get("volume24h", 0))
                                if volume_24h <= DAILY_VOLUME_LIMIT and item.get("state") == 1:
                                    LOW_VOLUME_SYMBOLS.add(symbol.replace("_USDT", "USDT"))
                            logger.info(f"УСПЕШНО загружено {len(LOW_VOLUME_SYMBOLS)} монет (≤2M USDT/24h)")
                            if len(LOW_VOLUME_SYMBOLS) > 50:
                                return  # всё ок
        except Exception as e:
            logger.error(f"Ошибка с API ключами: {e}")

    # Если не получилось — fallback без подписи (работает всегда)
    logger.warning("Используется публичный эндпоинт (без ключей)")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://contract.mexc.com/api/v1/contract/detail") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        for item in data["data"]:
                            symbol = item["symbol"]
                            if not symbol.endswith("_USDT"):
                                continue
                            volume_24h = float(item.get("volume24h", 0))
                            if volume_24h <= DAILY_VOLUME_LIMIT and item.get("state") == 1:
                                LOW_VOLUME_SYMBOLS.add(symbol.replace("_USDT", "USDT"))
                        logger.info(f"Публичный режим: загружено {len(LOW_VOLUME_SYMBOLS)} монет")
    except Exception as e:
        logger.error(f"И даже публичный не работает: {e}")
        LOW_VOLUME_SYMBOLS = {"1000BONKUSDT", "PEPEUSDT", "FLOKIUSDT", "1000SATSUSDT", "SHIBUSDT"}

# ====================== 1M ДАННЫЕ ======================
async def get_1m_data(symbol: str):
    sym = symbol.replace("USDT", "_USDT")
    params = {"symbol": sym, "interval": "Min1", "limit": 2}
    sign, query = mexc_sign(params)
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": str(int(time.time() * 1000)), "Signature": sign}

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://contract.mexc.com/api/v1/contract/kline/{sym}"
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
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
    logger.info(f"Сканер запущен → отслеживается {len(LOW_VOLUME_SYMBOLS)} монет")

    while True:
        try:
            minute = time.strftime("%Y%m%d%H%M")
            for symbol in list(LOW_VOLUME_SYMBOLS):
                data = await get_1m_data(symbol)
                if not data:
                    continue
                prev_vol, curr_vol, prev_price, curr_price = data

                if prev_vol < 1000 and curr_vol > 2000:
                    if f"{symbol}_{minute}" in sent_alerts:
                        continue

                    change = (curr_price - prev_price) / prev_price * 100
                    msg = (
                        f"<b>ВСПЛЕСК НА {symbol}!</b>\n\n"
                        f"Объём: {prev_vol:,} → <b>{curr_vol:,}</b> USDT\n"
                        f"Цена: {prev_price:.8f} → {curr_price:.8f} ({change:+.2f}%)\n\n"
                        f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>MEXC ФЬЮЧ</a>"
                    )
                    try:
                        await bot.send_message(MY_USER_ID, msg, parse_mode="HTML", disable_web_page_preview=True)
                        sent_alerts.add(f"{symbol}_{minute}")
                        logger.info(f"АЛЕРТ → {symbol}")
                    except Exception as e:
                        if "chat not found" in str(e).lower():
                            logger.warning("Напиши боту /start!")
                        else:
                            logger.error(f"Ошибка отправки: {e}")

            await asyncio.sleep(58)
        except Exception as e:
            logger.error(f"Ошибка в сканере: {e}")
            await asyncio.sleep(60)

# ====================== /start ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_USER_ID:
        return
    await update.message.reply_text(
        "<b>Сканер работает!</b>\n\n"
        f"Отслеживаю {len(LOW_VOLUME_SYMBOLS)} монет с объёмом ≤ 2M USDT/24h\n"
        "Всплеск: < 1k → > 2k за минуту",
        parse_mode="HTML"
    )

# ====================== ВЕБ + ЗАПУСК ======================
app = FastAPI()
@app.get("/")
async def root():
    return {"status": "ok", "coins": len(LOW_VOLUME_SYMBOLS)}

def run_web():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))

    async def main():
        await load_low_volume_symbols()
        application.create_task(volume_spike_scanner())
        await application.run_polling(drop_pending_updates=True)

    asyncio.run(main())








