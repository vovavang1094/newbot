import os
import time
import logging
import aiohttp
import asyncio
from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, CommandHandler
from fastapi import FastAPI
import uvicorn
import threading

# ====================== НАСТРОЙКИ ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_USER_ID = int(os.getenv("MY_USER_ID"))

# ← ИЗМЕНЕНО: теперь только монеты с объёмом ≤ 1 000 000 USDT в сутки
DAILY_VOLUME_LIMIT = 1_000_000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOW_VOLUME_SYMBOLS = set()
sent_alerts = set()
bot = Bot(token=TELEGRAM_TOKEN)

# ====================== ЗАГРУЗКА СИМВОЛОВ ======================
async def load_symbols():
    global LOW_VOLUME_SYMBOLS
    LOW_VOLUME_SYMBOLS.clear()
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://contract.mexc.com/api/v1/contract/detail") as resp:
                if resp.status != 200:
                    raise Exception("HTTP != 200")
                data = await resp.json()
                
                if not data.get("success", False):
                    raise Exception("API not success")
                
                count = 0
                for item in data["data"]:
                    symbol = item["symbol"]
                    if not symbol.endswith("_USDT"):
                        continue
                    if item.get("state") != 1:
                        continue
                    
                    vol = float(item.get("volume24hQuote") or 0)
                    if vol <= DAILY_VOLUME_LIMIT:
                        LOW_VOLUME_SYMBOLS.add(symbol.replace("_USDT", "USDT"))
                        count += 1
                
                logger.info(f"ЗАГРУЖЕНО {count} СУПЕР-НИЗКОЛИКВИДНЫХ МОНЕТ (≤1M USDT/24h)")
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        LOW_VOLUME_SYMBOLS = {
            "1000BONKUSDT", "PEPEUSDT", "FLOKIUSDT", "1000SATSUSDT", "BABYDOGEUSDT",
            "WIFUSDT", "BOMEUSDT", "TURBOUSDT", "MEWUSDT", "NOTUSDT"
        }
        logger.warning("Аварийный список (10 монет)")

# ====================== 1M ДАННЫЕ ======================
async def get_1m_data(symbol: str):
    try:
        async with aiohttp.ClientSession() as session:
            params = {"symbol": symbol.replace("USDT", "_USDT"), "interval": "Min1", "limit": 2}
            async with session.get("https://contract.mexc.com/api/v1/contract/kline", params=params, timeout=10) as resp:
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
    await load_symbols()
    logger.info(f"СКАНЕР ЗАПУЩЕН → слежу за {len(LOW_VOLUME_SYMBOLS)} монетами (≤1M USDT/24h)")

    while True:
        try:
            minute = time.strftime("%Y%m%d%H%M")
            for symbol in list(LOW_VOLUME_SYMBOLS):
                data = await get_1m_data(symbol)
                if not data:
                    continue
                prev_vol, curr_vol, prev_price, curr_price = data

                if prev_vol < 1000 and curr_vol > 2000:
                    key = f"{symbol}_{minute}"
                    if key in sent_alerts:
                        continue

                    change = (curr_price - prev_price) / prev_price * 100
                    msg = (
                        f"ВСПЛЕСК ОБЪЁМА!\n\n"
                        f"<b>{symbol}</b>\n"
                        f"Объём: {prev_vol:,} → <b>{curr_vol:,}</b> USDT\n"
                        f"Цена: {prev_price:.8f} → {curr_price:.8f}\n"
                        f"Δ: <b>{change:+.2f}%</b>\n\n"
                        f"<a href='https://www.mexc.com/futures/{symbol[:-4]}_USDT'>MEXC ФЬЮЧЕРС</a>"
                    )

                    try:
                        await bot.send_message(
                            chat_id=MY_USER_ID,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                        sent_alerts.add(key)
                        logger.info(f"АЛЕРТ → {symbol} | +{curr_vol - prev_vol:,} USDT")
                    except Exception as e:
                        if "chat not found" in str(e).lower():
                            logger.warning("Напиши боту /start в личку!")
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
        f"<b>Сканер ультра-низколиквидных монет активен!</b>\n\n"
        f"• Порог: ≤ <b>1 000 000</b> USDT в сутки\n"
        f"• Отслеживаю: <b>{len(LOW_VOLUME_SYMBOLS)}</b> монет\n"
        f"• Условие алерта: < 1000 → > 2000 USDT за 1 минуту\n\n"
        "Готов ловить настоящие пампы",
        parse_mode="HTML"
    )

# ====================== ВЕБ + ЗАПУСК ======================
app = FastAPI()
@app.get("/")
async def root():
    return {"status": "running", "coins": len(LOW_VOLUME_SYMBOLS), "limit_usd": DAILY_VOLUME_LIMIT}

def run_web():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))

    async def main():
        await load_symbols()
        application.create_task(volume_spike_scanner())
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")












