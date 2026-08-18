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
# СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ (ДЛЯ ИНТЕРВЬЮ)
# ============================================================

user_states = {}  # {user_id: {'step': 0, 'score': 0, 'history': [], 'trial_offered': False}}

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
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

def get_recent_history(user_id, limit=50):
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

# ============================================================
# 2ГИС (ДЛЯ ОРГАНИЗАЦИЙ)
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
# ОСНОВНАЯ ЛОГИКА: ДИАЛОГ С ИНТЕРВЬЮ
# ============================================================

async def deepseek_interview(user_id: int, text: str, step: int, history: list) -> dict:
    """
    Генерирует ответ бота и обновляет скрытый скор.
    Возвращает: {"reply": "...", "score": int, "offer_trial": bool}
    """
    
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-15:]])
    
    prompt = f"""Ты — AURA. Ты живой собеседник, а не робот. Твой стиль — Тони Старк: уверенный, с иронией, но человечный.

Твоя СКРЫТАЯ ЗАДАЧА за 10–15 сообщений понять, насколько этот человек платёжеспособен и готов ли платить за ассистента.
НЕ СПРАШИВАЙ ПРЯМО О ДЕНЬГАХ. Оценивай по косвенным признакам: путешествия, рестораны, увлечения, привычки, отношение ко времени.

ПРАВИЛА:
1. Отвечай на вопрос пользователя, но в конце каждого ответа добавляй новый вопрос, чтобы узнать его лучше.
2. НЕ ЗАЦИКЛИВАЙСЯ на одной теме. Если он говорит про еду — спроси про путешествия, потом про работу, потом про хобби.
3. Если пользователь отвечает односложно (да/нет/нормально) — не дави, но мягко переключай тему.
4. Используй 1–2 эмодзи, которые соответствуют теме разговора (✈️, 🍽️, 💰, ⏰, 🧐, 🔥, 🎮, 🏔️, 📊 и т.д.).
5. Если ты уже задал 5–7 вопросов и видишь, что человек не расположен к диалогу — мягко предложи ему триал на 3 дня.

СТАТУС:
- Шаг: {step} / 15
- Предыдущие сообщения:
{history_text}

Сейчас пользователь написал: "{text}"

Твоя задача:
1. Ответь пользователю (по существу, если он что-то спросил).
2. Добавь вопрос, чтобы узнать его лучше.
3. Если считаешь, что пора предложить триал — сделай это мягко, как предложение, а не как решение.

ОТВЕТЬ ТОЛЬКО JSON:
{{
    "reply": "твой ответ пользователю",
    "score": число_от_0_до_100,
    "offer_trial": true_или_false
}}
"""
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=500,
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
        text = "😅 Что-то пошло не так. Попробуй ещё раз."
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
        
        # === ЕСЛИ КОМАНДА /START ===
        if text == "/start":
            await send_message(
                user_id,
                "Привет! Я AURA. 👋\n\n"
                "Я — твой будущий ассистент. Помогаю экономить время и деньги.\n"
                "Давай познакомимся? Просто напиши, что тебя интересует — я всегда на связи."
            )
            # Сохраняем приветствие в историю
            save_message(user_id, "assistant", "Привет! Я AURA. 👋 ...")
            return JSONResponse({"ok": True})
        
        # === СОХРАНЯЕМ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ ===
        save_message(user_id, "user", text)
        
        # === ПОЛУЧАЕМ ИСТОРИЮ ===
        history = get_recent_history(user_id, limit=30)
        
        # === ЕСЛИ ПОЛЬЗОВАТЕЛЬ УЖЕ ПРОХОДИТ ИНТЕРВЬЮ ===
        if user_id in user_states:
            state = user_states[user_id]
            state["step"] += 1
            state["history"] = history
            
            # Если предложение уже было сделано — просто общаемся
            if state.get("trial_offered", False):
                # Здесь можно обычный ответ через диалог, но для простоты используем тот же метод
                result = await deepseek_interview(user_id, text, state["step"], state["history"])
                reply = result.get("reply", "😅 Не понял, попробуй ещё раз.")
                save_message(user_id, "assistant", reply)
                await send_message(user_id, reply)
                return JSONResponse({"ok": True})
            
            # Если шагов < 15 и не предлагали триал
            if state["step"] < 15:
                result = await deepseek_interview(user_id, text, state["step"], state["history"])
                reply = result.get("reply", "😅 Не понял, попробуй ещё раз.")
                score = result.get("score", 0)
                state["score"] = min(100, state["score"] + score // 2)
                
                # Если пользователь активно уклоняется (мало слов) — раньше предлагаем триал
                if state["step"] > 5 and len(text.split()) < 3:
                    result["offer_trial"] = True
                
                # Если скор > 60 или offer_trial == True
                if state["score"] > 60 or result.get("offer_trial", False):
                    state["trial_offered"] = True
                    reply += "\n\n🔥 Слушай, я вижу, что ты ценишь время. Давай я дам тебе доступ на 3 дня. Ты сам всё посмотришь и решишь, нужно это тебе или нет. Как тебе идея?"
                
                save_message(user_id, "assistant", reply)
                await send_message(user_id, reply)
            else:
                # После 15 шагов — если не предложили, предлагаем сейчас
                state["trial_offered"] = True
                reply = "🔥 Слушай, мы уже 15 сообщений общаемся. Я вижу, что ты серьёзный человек. Давай я дам тебе доступ на 3 дня. Ты сам всё посмотришь и решишь, нужно это тебе или нет. Договорились?"
                save_message(user_id, "assistant", reply)
                await send_message(user_id, reply)
            
            return JSONResponse({"ok": True})
        
        # === НОВЫЙ ПОЛЬЗОВАТЕЛЬ — СОЗДАЁМ СОСТОЯНИЕ ===
        user_states[user_id] = {
            "step": 1,
            "score": 0,
            "history": history,
            "trial_offered": False
        }
        
        # === ПЕРВЫЙ ОТВЕТ НОВОМУ ПОЛЬЗОВАТЕЛЮ ===
        result = await deepseek_interview(user_id, text, 1, history)
        reply = result.get("reply", "😅 Не понял, попробуй ещё раз.")
        score = result.get("score", 0)
        user_states[user_id]["score"] = min(100, score // 2)
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
