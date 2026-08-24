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

def call_yandex_agent(agent_id, user_text, user_name="", user_city="", budget=""):
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
    if len(text) > 4096: text = text[:4093] + "..."
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=30)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        msg = body["message"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        # === АВТОРИЗАЦИЯ ===
        if str(user_id) != ADMIN_CHAT_ID:
            if not supabase or not supabase.table("users").select("user_id").eq("user_id", user_id).execute().data:
                if user_id in user_states and user_states[user_id].get("state") == "entering_password":
                    if text == ACCESS_PASSWORD:
                        supabase.table("users").insert({"user_id": user_id}).execute()
                        await send_message(user_id, "✅ Доступ разрешён!")
                        del user_states[user_id]
                        return JSONResponse({"ok": True})
                    else:
                        await send_message(user_id, "❌ Неверный пароль.")
                        return JSONResponse({"ok": True})
                await send_message(user_id, "🔐 Введите пароль:")
                user_states[user_id] = {"state": "entering_password"}
                return JSONResponse({"ok": True})

        # === КОМАНДЫ ===
        if text == "/start":
            if get_fact(user_id, "name"):
                await send_message(user_id, f"Привет, {get_fact(user_id, 'name')}!")
            else:
                await send_message(user_id, "Привет! Как тебя зовут?")
                user_states[user_id] = {"state": "collecting_name"}
            return JSONResponse({"ok": True})

        # === ЗНАКОМСТВО ===
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
                await send_message(user_id, f"Отлично, {get_fact(user_id, 'name')}! Задавай вопросы!")
                del user_states[user_id]
            else:
                await send_message(user_id, "Напиши город.")
            return JSONResponse({"ok": True})

        # === ОСНОВНАЯ ЛОГИКА ===
        user_name = get_fact(user_id, "name") or "Гость"
        user_city = get_fact(user_id, "city") or "Москва"
        budget = get_fact(user_id, "budget_travel") or ""

        if "билет" in text.lower():
            agent_id = AGENT_SEARCH_ID
        elif any(w in text.lower() for w in ["посоветуй", "что лучше"]):
            agent_id = AGENT_REASONING_ID
        elif any(w in text.lower() for w in ["сравни", "проанализируй"]):
            agent_id = AGENT_RESEARCH_ID
        else:
            agent_id = AGENT_SEARCH_ID

        raw = call_yandex_agent(agent_id, text, user_name, user_city, budget)
        if raw:
            prompt = f"Ты — AURA. Отвечай коротко, 2-3 предложения, без приветствий. На основе данных:\n{raw[:500]}"
            try:
                resp = deepseek.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "system", "content": prompt}],
                    max_tokens=100,
                    temperature=0.7,
                    timeout=15
                )
                answer = resp.choices[0].message.content
            except Exception as e:
                logger.error(f"❌ Ошибка DeepSeek: {e}")
                answer = raw[:300]
            await send_message(user_id, answer)
            save_message(user_id, "assistant", answer)
        else:
            await send_message(user_id, "Не нашёл в интернете. Попробуй уточнить.")

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
