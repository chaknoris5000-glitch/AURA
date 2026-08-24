import os
import json
import logging
import tempfile
import hashlib
import base64
import requests
from datetime import datetime, timedelta
from collections import deque
import pytz
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== КЛЮЧИ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "5818548555")

AGENT_SEARCH_ID = os.getenv("YANDEX_AGENT_ID", "fvt3te2kgttig7u3a1fb")
AGENT_RESEARCH_ID = os.getenv("YANDEX_AGENT_RESEARCH_ID", "fvti80ngse2778agbmdl")
AGENT_REASONING_ID = os.getenv("YANDEX_AGENT_REASONING_ID", "fvtg0c38oi7n43d0n9gf")

ACCESS_PASSWORD = "12355"

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Supabase подключён")

app = FastAPI()
user_states = {}
user_last_requests = {}
agent_cache = {}

# ===== ПАМЯТЬ =====
def save_fact(user_id, key, value):
    if not supabase: return
    try:
        existing = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        if existing.data:
            supabase.table("user_memory").update({"value": value}).eq("user_id", user_id).eq("key", key).execute()
        else:
            supabase.table("user_memory").insert({"user_id": user_id, "key": key, "value": value, "created_at": datetime.now().isoformat()}).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")

def get_fact(user_id, key):
    if not supabase: return None
    try:
        res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except:
        return None

def save_message(user_id, role, content):
    if not supabase: return
    try:
        supabase.table("history").insert({"user_id": user_id, "role": role, "content": content, "created_at": datetime.now().isoformat()}).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения истории: {e}")

def get_recent_history(user_id, limit=10):
    if not supabase: return []
    try:
        res = supabase.table("history").select("role, content, created_at").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
        return list(reversed(res.data)) if res.data else []
    except:
        return []

def search_history(user_id, keywords):
    if not supabase: return []
    try:
        res = supabase.table("history").select("role, content, created_at").eq("user_id", user_id).order("created_at", desc=True).execute()
        if not res.data: return []
        history = list(reversed(res.data))
        found = []
        for msg in history:
            content_lower = msg['content'].lower()
            for keyword in keywords:
                if keyword in content_lower:
                    found.append(msg)
                    break
        return found[:5]
    except:
        return []

def call_yandex_agent(agent_id, user_text, user_name="", user_city="", budget=""):
    # Добавляем город в запрос, чтобы поиск был локальным
    if user_city and user_city != "Москва":
        user_text = f"{user_text} в городе {user_city}"
    
    hash_val = hashlib.md5(f"{agent_id}:{user_text}:{user_name}:{user_city}:{budget}".encode()).hexdigest()
    if hash_val in agent_cache:
        entry = agent_cache[hash_val]
        if datetime.now() - entry["timestamp"] < timedelta(hours=24):
            return entry["response"]
    try:
        client = OpenAI(api_key=YANDEX_API_KEY, base_url="https://ai.api.cloud.yandex.net/v1", project=YANDEX_FOLDER_ID)
        response = client.responses.create(
            prompt={"id": agent_id, "variables": {"user_name": user_name or "Гость", "user_city": user_city or "Москва", "budget": budget or "не указан"}},
            input=user_text,
            tools=[{"type": "web_search", "filters": {"allowed_domains": []}, "search_context_size": "low"}]
        )
        result = response.output_text
        agent_cache[hash_val] = {"response": result, "timestamp": datetime.now()}
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка агента: {e}")
        return ""

async def send_message(chat_id, text):
    if not text: text = "😅 Не понял."
    # Обрезаем до 200 символов, но не разрываем посреди слова
    if len(text) > 200:
        # Находим последний пробел перед 200 символами
        cut = text[:200].rfind(' ')
        if cut > 0:
            text = text[:cut] + "..."
        else:
            text = text[:197] + "..."
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=30)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

async def send_typing(chat_id):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except:
        pass

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        msg = body["message"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        # ===== АВТОРИЗАЦИЯ =====
        if str(user_id) != ADMIN_CHAT_ID:
            if not supabase or not supabase.table("users").select("user_id").eq("user_id", user_id).execute().data:
                if user_id in user_states and user_states[user_id].get("state") == "entering_password":
                    if text == ACCESS_PASSWORD:
                        supabase.table("users").insert({"user_id": user_id}).execute()
                        await send_message(user_id, "✅ Доступ разрешён! Добро пожаловать в AURA.")
                        del user_states[user_id]
                        return JSONResponse({"ok": True})
                    else:
                        await send_message(user_id, "❌ Неверный пароль.")
                        return JSONResponse({"ok": True})
                await send_message(user_id, "🔐 Введите пароль для входа:")
                user_states[user_id] = {"state": "entering_password"}
                return JSONResponse({"ok": True})

        if not text:
            return JSONResponse({"ok": True})

        # ===== ЗАЩИТА ОТ ПОВТОРОВ =====
        hash_val = hashlib.md5(f"{user_id}:{text}".encode()).hexdigest()
        if user_id not in user_last_requests:
            user_last_requests[user_id] = deque(maxlen=3)
        if hash_val in user_last_requests[user_id]:
            return JSONResponse({"ok": True})
        user_last_requests[user_id].append(hash_val)

        # ===== КОМАНДЫ =====
        if text == "/start":
            if get_fact(user_id, "name"):
                await send_message(user_id, f"Привет, {get_fact(user_id, 'name')}! Чем могу помочь?")
            else:
                await send_message(user_id, "Привет! Я AURA. Как тебя зовут?")
                user_states[user_id] = {"state": "collecting_name"}
            return JSONResponse({"ok": True})

        # ===== ЗНАКОМСТВО =====
        if user_id in user_states and user_states[user_id].get("state") == "collecting_name":
            name = text.strip()
            if name and len(name) > 1:
                save_fact(user_id, "name", name)
                await send_message(user_id, f"Приятно познакомиться, {name}! В каком городе ты живёшь?")
                user_states[user_id] = {"state": "collecting_city"}
            else:
                await send_message(user_id, "Напиши имя.")
            return JSONResponse({"ok": True})

        if user_id in user_states and user_states[user_id].get("state") == "collecting_city":
            city = text.strip()
            if city and len(city) > 1:
                save_fact(user_id, "city", city)
                await send_message(user_id, f"Отлично, {get_fact(user_id, 'name')}! Задавай вопросы.")
                del user_states[user_id]
            else:
                await send_message(user_id, "Напиши город.")
            return JSONResponse({"ok": True})

        # ===== ОСНОВНАЯ ЛОГИКА =====
        save_message(user_id, "user", text)
        
        user_name = get_fact(user_id, "name") or "Гость"
        user_city = get_fact(user_id, "city") or "Москва"
        budget = get_fact(user_id, "budget_travel") or ""

        # Загружаем историю для контекста
        history = get_recent_history(user_id, limit=10)
        history_text = ""
        if history:
            history_lines = []
            for msg in history:
                role = "Пользователь" if msg['role'] == 'user' else "AURA"
                history_lines.append(f"{role}: {msg['content'][:300]}")
            history_text = "\n".join(history_lines)

        # Поиск по ключевым словам
        keywords = [w for w in text.lower().split() if len(w) > 3 and w not in ["найди", "меня", "что", "это"]]
        found = []
        if keywords:
            found = search_history(user_id, keywords)
        found_text = ""
        if found:
            found_lines = []
            for msg in found:
                role = "Пользователь" if msg['role'] == 'user' else "AURA"
                found_lines.append(f"{role}: {msg['content'][:300]}")
            found_text = "\n".join(found_lines)

        # ===== ПРОМПТ ДЛЯ DEEPSEEK =====
        prompt = f"""Ты — AURA. Личный ассистент. Отвечай коротко (1-2 предложения), всегда по делу.

О пользователе:
- Имя: {user_name}
- Город: {user_city}

История диалога:
{history_text}

Найдено по ключевым словам:
{found_text if found_text else "Ничего"}

Вопрос: "{text}"

Правила:
1. Если ищешь клинику/магазин/услугу — ищи в городе пользователя ({user_city}).
2. Если есть ссылка — добавь её сразу.
3. Ответ должен быть коротким, завершённым по смыслу.
4. Не повторяй одно и то же.

Ответ:"""

        try:
            resp = deepseek.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": prompt}],
                max_tokens=120,
                temperature=0.7,
                timeout=15
            )
            answer = resp.choices[0].message.content
        except Exception as e:
            logger.error(f"❌ Ошибка DeepSeek: {e}")
            answer = "Ошибка. Попробуй ещё раз."

        # ===== ДОБАВЛЯЕМ ССЫЛКУ =====
        if "фильм" in text.lower() and "http" not in answer:
            answer += f"\n\n🔗 [Смотреть](https://yandex.ru/video/search?text={text.replace(' ', '+')})"
        elif "билет" in text.lower() and "http" not in answer:
            answer += f"\n\n✈️ [Найти билеты](https://www.aviasales.ru/search?q={text.replace(' ', '+')})"
        elif "клиника" in text.lower() and "http" not in answer:
            # Ссылка добавляется только если есть сайт в ответе
            if "сайт" not in answer.lower() and ".ru" not in answer.lower():
                # Если нет сайта — добавляем ссылку на поиск Яндекса
                answer += f"\n\n🔍 [Поиск](https://yandex.ru/search/?text={text.replace(' ', '+')}%20{user_city})"

        await send_message(user_id, answer)
        save_message(user_id, "assistant", answer)

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
