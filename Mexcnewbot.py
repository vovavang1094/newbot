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
if not TELEGRAM_TOKEN:
    raise ValueError("Укажи TELEGRAM_TOKEN в .env")

# Твой личный Telegram ID (узнай через @userinfobot)
MY_USER_ID = int(os.getenv("MY_USER_ID"))

MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

if not MEXC_API_KEY or not MEXC_SECRET_KEY:
    print("⚠️ MEXC ключи не указаны — будет fallback на 3 монеты")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALL_SYMBOLS = set()
bot = Bot(token=TELEGRAM_TOKEN)
sent_alerts = set()  # чтобы не спамить

# ====================== MEXC API ======================
async def load_symbols():
    global ALL_SYMBOLS
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://contract.mexc.com/api/v1/contract/detail", timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        ALL_SYMBOLS = {
                            s["symbol"].replace("_USDT", "USDT")
                            for s in data["data"]
                            if s["symbol"].endswith("_USDT") and s.get("state") == 1
                        }
                        logger.info(f"Успешно загружено {len(ALL_SYMBOLS)} фьючерсных пар")
                        return
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")

    # Fallback, если API упал
    ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "1000SHIBUSDT"}
    logger.warning("Используется fallback-лист (5 монет)")


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
    await asyncio.sleep(15)
    await load_symbols()
    logger.info("Сканер всплесков объёма запущен — слежу за всеми монетами")

    while True:
        try:
            minute_key = time.strftime("%Y%m%d%H%M")

            for symbol in list(ALL_SYMBOLS):
                prev_vol, curr_vol, prev_price, curr_price = await get_1m_data(symbol)
                if not all(x is not None for x in [prev_vol, curr_vol, prev_price, curr_price]):
                    continue

                if prev_vol < 1000 and curr_vol > 2000:
                    alert_id = f"{symbol}_{minute_key}"
                    if alert_id in sent_alerts:
                        continue

                    change_pct = (curr_price - prev_price) / prev_price * 100
                    emoji = "UP" if change_pct >= 0 else "DOWN"

                    message = (
                        f"<b>ВСПЛЕСК ОБЪЁМА НА {symbol}!</b>\n\n"
                        f"Предыдущий объём: <b>{prev_vol:,}</b> USDT\n"
                        f"Текущий объём: <b>{curr_vol:,}</b> USDT (+{curr_vol-prev_vol:,})\n"
                        f"Цена: {prev_price:.6f} → <b>{curr_price:.6f}</b> USDT\n"
                        f"Изменение: {emoji} <b>{change_pct:+.2f}%</b>\n\n"
                        f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>Открыть на MEXC</a>"
                    )

                    await bot.send_message(
                        chat_id=MY_USER_ID,
                        text=message,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                    sent_alerts.add(alert_id)
                    logger.info(f"АЛЕРТ → {symbol} | {curr_vol:,} USDT")

            # Чистим старые алерты (старше 10 минут)
            sent_alerts = {a for a in sent_alerts if a.endswith(minute_key[-4:]) or int(time.time()) - int(a.split("_")[-1]) < 600}

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
        "<b>Сканер всплесков объёма запущен!</b>\n\n"
        "Я пришлю уведомление, как только у любой монеты на фьючерсах MEXC:\n"
        "• был объём < 1 000 USDT\n"
        "• стал > 2 000 USDT за 1 минуту\n\n"
        "Работаю 24/7",
        parse_mode="HTML"
    )


# ====================== ВЕБ ДЛЯ RENDER ======================
app = FastAPI()
@app.get("/")
async def root():
    return {"status": "ok", "scanner": "running", "symbols": len(ALL_SYMBOLS)}

def run_web():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))

    # Запускаем сканер в фоне
    async def main():
        await load_symbols()
        application.create_task(volume_spike_scanner())
        await application.run_polling()

    asyncio.run(main())