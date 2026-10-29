"""
telegram_bot.py
Telegram-бот для запуска парсера ключей.
Команды: /start, /help, /scan, /scan_context <маркер>
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
    raise SystemExit(
        "[!] нужен TELEGRAM_BOT_TOKEN.\n"
        "    получи у @BotFather: /newbot"
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── хендлеры ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡️ Парсер ключей запущен.\n\n"
        "Доступные команды:\n"
        "/scan — стандартный поиск по всем паттернам\n"
        "/scan_context <маркер> — контекстный поиск для кастомного провайдера\n"
        "   пример: /scan_context morphllm\n\n"
        f"🌐 Endpoint по умолчанию: {DEFAULT_ENDPOINT}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/scan — ищет ключи по общим паттернам (OpenAI, Anthropic, DeepSeek и др.)\n"
        "/scan_context <маркер> — ищет ключи рядом с указанным маркером "
        "(например, morphllm)\n\n"
        "Сканирование может занять 1–3 минуты из-за лимитов GitHub API."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Запускаю стандартное сканирование...")

    # запускаем в отдельном потоке, чтобы не блокировать event loop
    loop = asyncio.get_event_loop()
    reports, error = await loop.run_in_executor(None, lambda: run_scan(max_files=20))

    if error:
        await update.message.reply_text(f"❌ {error}")
        return

    if not reports:
        await update.message.reply_text("🤷 Ключей не найдено.")
        return

    # отправляем отчёты частями, чтобы не превысить лимит сообщения
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
        await update.message.reply_text(
            "Укажи маркер. Пример:\n/scan_context morphllm"
        )
        return

    marker = context.args[0]
    await update.message.reply_text(f"🔍 Контекстный поиск по маркеру: {marker}")

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


# ─── запуск ────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("scan_context", scan_context))

    logger.info("бот запущен, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
