"""
bot_runner.py
Версия бота для GitHub Actions: работает ограниченное время,
затем корректно останавливается до 6-часового лимита.
"""

import os
import asyncio
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from github_key_parser import run_scan, DEFAULT_ENDPOINT

# ─── конфиг ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("[!] нужен TELEGRAM_BOT_TOKEN")

# ~5 часов 50 минут. Оставляем запас до лимита в 6 часов.
RUN_DURATION_SECONDS = int(os.getenv("RUN_DURATION_SECONDS", 5 * 3600 + 50 * 60))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── хендлеры ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡️ Парсер ключей запущен.\n\n"
        "Команды:\n"
        "/scan — стандартный поиск\n"
        "/scan_context <маркер> — контекстный поиск (например: /scan_context morphllm)\n\n"
        f"🌐 Endpoint: {DEFAULT_ENDPOINT}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/scan — ищет ключи по общим паттернам\n"
        "/scan_context <маркер> — ищет ключи рядом с маркером\n\n"
        "Сканирование может занять 1–3 минуты из-за лимитов GitHub API."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Запускаю стандартное сканирование...")
    loop = asyncio.get_event_loop()
    reports, error = await loop.run_in_executor(None, lambda: run_scan(max_files=20))

    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    if not reports:
        await update.message.reply_text("🤷 Ключей не найдено.")
        return

    chunk = ""
    for report in reports:
        if len(chunk) + len(report) > 3500:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += report + "\n\n"
    if chunk:
        await update.message.reply_text(chunk)


async def scan_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи маркер. Пример:\n/scan_context morphllm")
        return

    marker = context.args[0]
    await update.message.reply_text(f"🔍 Контекстный поиск: {marker}")

    loop = asyncio.get_event_loop()
    reports, error = await loop.run_in_executor(
        None, lambda: run_scan(context_marker=marker, max_files=20)
    )

    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    if not reports:
        await update.message.reply_text("🤷 Ключей не найдено.")
        return

    chunk = ""
    for report in reports:
        if len(chunk) + len(report) > 3500:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += report + "\n\n"
    if chunk:
        await update.message.reply_text(chunk)


# ─── запуск на ограниченное время ─────────────────────────────────────────

async def run_bot_for_a_while(duration_seconds: int):
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("scan_context", scan_context))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info(f"бот запущен на {duration_seconds} сек")

    try:
        await asyncio.sleep(duration_seconds)
    finally:
        logger.info("останавливаю бота по таймеру...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(run_bot_for_a_while(RUN_DURATION_SECONDS))
    except (KeyboardInterrupt, SystemExit):
        logger.info("прервано вручную")
