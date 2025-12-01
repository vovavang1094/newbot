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

DAILY_VOLUME_LIMIT = 2_000_000  # USDT — максимальный дневной объём для отслеживания

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALL_SYMBOLS = set()
LOW_VOLUME_SYMBOLS = set()  # ← только монеты с объёмом ≤ 2M в день
bot = Bot(token=TELEGRAM_TOKEN)
sent_alerts = set()

# ====================== MEXC API ======================
async def load_symbols_and_filter_low_volume():
    global ALL_SYMBOLS, LOW_VOLUME_SYMBOLS
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Загружаем все фьючерсные пары
            async with session.get("https://contract.mexc.com/api/v1/contract/detail", timeout=15) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if not data.get("success"):
                    return False

                symbols_data = data["data"]
                ALL_SYMBOLS = {
                    s["symbol"].replace("_USDT", "USDT")
                    for s in symbols_data
                    if s["symbol"].endswith("_USDT") and s.get("state") == 1
                }

                # 2. Фильтруем по 24h объёму
                LOW_VOLUME_SYMBOLS = set()
                for s in symbols_data:
                    sym = s["symbol"].replace("_USDT", "USDT")
                    volume_24h = float(s.get("volume24h", 0))  # в контрактах, но примерно в USDT
                    if volume_24h <= DAILY_VOLUME_LIMIT and sym in ALL_SYMBOLS:
                        LOW_VOLUME_SYMBOLS.add(sym)

                logger.info(f"Загружено ВСЕГО: {len(ALL_SYMBOLS)} | ОТСЛЕЖИВАЕМЫХ (≤2M): {len(LOW_VOLUME_SYMBOLS)}")
                return True
    except Exception as e:
        logger.error(f"Ошибка загрузки/фильтрации символов: {e}")
        return False

    # Fallback
    LOW_VOLUME_SYMBOLS = {"1000BONKUSDT", "PEPEUSDT", "FLOKIUSDT", "SHIBUSDT"}
    logger.warning("Используется fallback-список (низколиквидные)")


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
    success = await load_symbols_and_filter_low_volume()
    if not success:
        logger.error("Не удалось загрузить символы — сканер остановлен")
        return

    logger.info(f"Сканер запущен → отслеживаю {len(LOW_VOLUME_SYMBOLS)} монет с дневным объёмом ≤ 2M USDT")

    while True:
        try:
            minute_key = time.strftime("%Y%m%d%H%M")

            # Перезагружаем список раз в 30 минут (чтобы новые делистинги/листинги учитывались)
            if int(time.strftime("%M")) % 30 == 0:
                await load_symbols_and_filter_low_volume()

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
                        if "chat not found" in str(e).lower():
                            logger.warning("Напиши боту /start — и алерты пойдут!")
                        else:
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
        "<b>Сканер всплесков объёма запущен!</b>\n\n"
        "Отслеживаю ТОЛЬКО монеты с дневным объёмом ≤ 2 000 000 USDT\n"
        "Условие: < 1 000 → > 2 000 USDT за 1 минуту\n\n"
        "Работаю 24/7",
        parse_mode="HTML"
    )


# ====================== ВЕБ + ЗАПУСК (ФИКС ДЛЯ WINDOWS) ======================
app = FastAPI()
@app.get("/")
async def root():
    return {"status": "ok", "tracked_coins": len(LOW_VOLUME_SYMBOLS)}

def run_web():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="error")

if __name__ == "__main__":
    import platform
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    threading.Thread(target=run_web, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))

    async def main():
        await load_symbols_and_filter_low_volume()
        application.create_task(volume_spike_scanner())
        await application.run_polling(drop_pending_updates=True)

    print("Бот запущен! Напиши /start боту в личку.")
    asyncio.run(main())


