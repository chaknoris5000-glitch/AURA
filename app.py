import os
import json
import httpx
import asyncio
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# === КЛЮЧИ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GIS_API_KEY = os.getenv("GIS_API_KEY")

# === КЛИЕНТЫ ===
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase подключён")
    except Exception as e:
        logger.error(f"❌ Ошибка Supabase: {e}")

app = FastAPI()

# ============================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
# ============================================================

user_states = {}  # {user_id: {'step': 0, 'score': 0, 'trial_offered': False}}

# ============================================================
# РАБОТА С ПАМЯТЬЮ (FACTS)
# ============================================================

def save_fact(user_id, key, value):
    if not supabase:
        return
    try:
        existing = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        if existing.data:
            supabase.table("user_memory").update({"value": value}).eq("user_id", user_id).eq("key", key).execute()
        else:
            supabase.table("user_memory").insert({
                "user_id": user_id,
                "key": key,
                "value": value,
                "created_at": datetime.now().isoformat()
            }).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения факта: {e}")

def get_fact(user_id, key):
    if not supabase:
        return None
    try:
        res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except:
        return None

# ============================================================
# РАБОТА СО СТАТУСОМ ТРИАЛА
# ============================================================

def get_trial_status(user_id):
    if not supabase:
        return None
    try:
        res = supabase.table("trial_status").select("*").eq("user_id", user_id).execute()
        return res.data[0] if res.data else None
    except:
        return None

def save_trial_status(user_id, started, ended):
    if not supabase:
        return
    try:
        supabase.table("trial_status").insert({
            "user_id": user_id,
            "trial_started": started.isoformat(),
            "trial_ended": ended.isoformat(),
            "is_active": True
        }).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения триала: {e}")

# ============================================================
# ИСТОРИЯ СООБЩЕНИЙ
# ============================================================

def save_message(user_id, role, content):
    if not supabase:
        return
    try:
        supabase.table("history").insert({
            "user_id": user_id,
            "role": role,
            "content": content,
            "created_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")

def get_recent_history(user_id, limit=20):
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        return list(reversed(res.data)) if res.data else []
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки: {e}")
        return []

def search_history(user_id, query, days_ago=None):
    if not supabase:
        return []
    try:
        builder = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)
        if days_ago:
            cutoff = datetime.now() - timedelta(days=days_ago)
            builder = builder.gte("created_at", cutoff.isoformat())
        if query:
            builder = builder.ilike("content", f"%{query}%")
        res = builder.order("created_at", desc=True).limit(10).execute()
        return list(reversed(res.data)) if res.data else []
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
        return []

# ============================================================
# 2ГИС
# ============================================================

async def search_organization(query: str, city: str = "Белово") -> dict:
    if not GIS_API_KEY:
        return {"error": "Нет ключа 2ГИС"}
    url = "https://catalog.api.2gis.com/3.0/items"
    params = {
        "q": query,
        "city_name": city,
        "type": "branch",
        "sort": "rating",
        "page_size": 1,
        "fields": "items.name,items.address,items.phones,items.site,items.schedule,items.rating,items.reviews_count",
        "key": GIS_API_KEY
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                items = data.get("result", {}).get("items", [])
                if items:
                    item = items[0]
                    return {
                        "name": item.get("name", "Неизвестно"),
                        "address": item.get("address", {}).get("full_name", "Адрес не указан"),
                        "phones": [p.get("number") for p in item.get("phones", []) if p.get("number")],
                        "site": item.get("site", ""),
                        "rating": item.get("rating", {}).get("value", 0),
                        "reviews": item.get("reviews_count", 0)
                    }
            return {"error": "Не найдено"}
    except Exception as e:
        logger.error(f"❌ Ошибка 2ГИС: {e}")
        return {"error": str(e)}

# ============================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================

async def deepseek_interview(user_id: int, text: str, step: int, history: list) -> dict:
    # === 1. ПРОВЕРКА НА ЗАПРОС В ИСТОРИЮ ===
    memory_triggers = ["помнишь", "напомни", "вспомни", "подскажи", "что я говорил", "что я просил", "на той неделе", "в прошлый раз"]
    if any(word in text.lower() for word in memory_triggers):
        query_words = [w for w in text.split() if len(w) > 2 and w.lower() not in memory_triggers]
        query = " ".join(query_words) if query_words else None
        days_ago = None
        if "неделе" in text.lower():
            days_ago = 7
        elif "месяц" in text.lower():
            days_ago = 30
        elif "вчера" in text.lower():
            days_ago = 1
        if query or days_ago:
            results = search_history(user_id, query, days_ago)
            if results:
                memory_context = "\n".join([f"{h['role']}: {h['content']}" for h in results[-5:]])
                return {
                    "reply": f"📜 Вот что я нашёл:\n\n{memory_context}",
                    "score": 0,
                    "offer_trial": False
                }
            else:
                return {
                    "reply": "📭 Не нашёл ничего по вашему запросу. Уточните, о чём именно вы говорили.",
                    "score": 0,
                    "offer_trial": False
                }
    
    # === 2. ПОЛУЧАЕМ ИМЯ И ГОРОД ===
    user_name = get_fact(user_id, "name")
    user_city = get_fact(user_id, "city")
    
    name_instruction = f"Пользователя зовут {user_name}. Используй имя 1 раз в ответе." if user_name else "Пользователь ещё не назвал имя. В конце ответа мягко спроси: 'Как тебя зовут?'."
    city_instruction = f"Город пользователя: {user_city}. Используй его для поиска по умолчанию." if user_city else "Город не указан. Если пользователь что-то ищет — спроси, в каком городе искать."
    
    # === 3. СОКРАЩАЕМ ИСТОРИЮ ===
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-15:]])
    
    prompt = f"""Ты — AURA. Твой стиль — Тони Старк: уверенный, с иронией, но человечный.

ПРАВИЛА ОТВЕТОВ:
1. **ОТВЕЧАЙ КОРОТКО:** максимум 3–5 предложений. Без воды.
2. **СТРУКТУРА:** используй маркеры ✅ и 🌟 вместо цифр в списках.
3. **ЗАВЕРШЁННОСТЬ:** всегда ставь точку в конце. Не обрывай мысль.
4. **ЭМОДЗИ:** 1–2 по теме (✈️, 🍽️, 🏖️ и т.д.).

{name_instruction}
{city_instruction}

ПРЕДЫДУЩИЕ СООБЩЕНИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ НАПИСАЛ: "{text}"

Твоя задача: ответить по существу и, если нужно, задать 1 уточняющий вопрос.

ОТВЕТЬ ТОЛЬКО JSON:
{{
    "reply": "твой короткий ответ (3–5 предложений)",
    "score": число_от_0_до_100,
    "offer_trial": false
}}
"""
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=300,
            timeout=30
        )
        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return {
            "reply": "😅 Что-то пошло не так. Попробуй ещё раз.",
            "score": 0,
            "offer_trial": False
        }

# ============================================================
# ОТПРАВКА СООБЩЕНИЙ
# ============================================================

async def send_message(chat_id, text):
    if not text:
        text = "😅 Что-то пошло не так."
    if len(text) > 4000:
        text = text[:3997] + "..."
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=30
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

# ============================================================
# WEBHOOK
# ============================================================

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        msg = body["message"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")
        
        if not text:
            return JSONResponse({"ok": True})
        
        if text == "/start":
            await send_message(user_id, "Привет! Я AURA. 👋 Напиши, что нужно найти или сделать — я помогу.")
            save_message(user_id, "assistant", "Привет! Я AURA. 👋")
            return JSONResponse({"ok": True})
        
        save_message(user_id, "user", text)
        history = get_recent_history(user_id, limit=20)
        
        # === ПРОВЕРКА ТРИАЛА ===
        trial = get_trial_status(user_id)
        if trial:
            result = await deepseek_interview(user_id, text, 0, history)
            reply = result.get("reply", "😅 Не понял.")
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})
        
        # === НОВЫЙ ПОЛЬЗОВАТЕЛЬ / ИНТЕРВЬЮ ===
        if user_id not in user_states:
            user_states[user_id] = {"step": 0, "score": 0, "trial_offered": False}
        
        state = user_states[user_id]
        state["step"] += 1
        
        result = await deepseek_interview(user_id, text, state["step"], history)
        reply = result.get("reply", "😅 Не понял.")
        score = result.get("score", 0)
        state["score"] = min(100, state["score"] + score // 2)
        
        # === ПРЕДЛОЖЕНИЕ ТРИАЛА ===
        if not state["trial_offered"] and state["step"] >= 8 and state["score"] > 60:
            state["trial_offered"] = True
            save_trial_status(user_id, datetime.now(), datetime.now() + timedelta(days=3))
            reply += "\n\n🔥 Давай я дам тебе доступ на 3 дня — попробуешь и решишь. Идёт?"
        
        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)
        
        return JSONResponse({"ok": True})
    
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA VIP работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
