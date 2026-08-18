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

user_states = {}  # {user_id: {'step': 0, 'score': 0, 'trial_offered': False, 'offer_count': 0}}

# ============================================================
# ТОЧНОЕ ВРЕМЯ (С ПОВТОРОМ И ЗАПАСНЫМ ОТВЕТОМ)
# ============================================================

async def get_exact_time() -> str:
    for attempt in range(2):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://worldtimeapi.org/api/timezone/Europe/Moscow", timeout=5)
                data = response.json()
                return data["datetime"][11:16]  # HH:MM
        except Exception:
            if attempt == 0:
                await asyncio.sleep(1)
            else:
                logger.error("❌ Не удалось получить точное время")
                return "не могу узнать точное время, посмотри на телефоне"
    return "не могу узнать точное время, посмотри на телефоне"

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
# ДЕТЕКТОР ЭМОЦИЙ (ЧЕРЕЗ DEEPSEEK)
# ============================================================

async def detect_emotion(text: str) -> dict:
    try:
        prompt = f"""
Проанализируй эмоцию в сообщении пользователя: "{text}"
Верни JSON с двумя полями:
- emotion: одна из (радость, грусть, гнев, страх, удивление, отвращение, спокойствие)
- confidence: число от 0 до 1

Ответь строго JSON.
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=100,
            timeout=10
        )
        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка детектора эмоций: {e}")
        return {"emotion": "спокойствие", "confidence": 0.5}

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

def save_emotion(user_id, emotion, confidence):
    if not supabase:
        return
    try:
        supabase.table("emotions").insert({
            "user_id": user_id,
            "emotion": emotion,
            "confidence": confidence,
            "created_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения эмоции: {e}")

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
# ОСНОВНАЯ ЛОГИКА (С ПОВТОРНЫМ ЗАПРОСОМ ПРИ ОШИБКЕ JSON)
# ============================================================

async def deepseek_interview(user_id: int, text: str, step: int, history: list, emotion: str = "спокойствие") -> dict:
    # === 1. АДАПТАЦИЯ ТОНА ПОД ЭМОЦИЮ ===
    emotion_instruction = ""
    if emotion in ["грусть", "страх"]:
        emotion_instruction = "Пользователь грустит или тревожится. Отвечай мягко, с поддержкой, избегай резких шуток."
    elif emotion == "гнев":
        emotion_instruction = "Пользователь раздражён. Отвечай спокойно, без иронии, предложи решение."
    elif emotion == "радость":
        emotion_instruction = "Пользователь в хорошем настроении. Отвечай живо, с юмором, поддерживай лёгкость."
    
    # === 2. ПРОВЕРКА НА ЗАПРОС В ИСТОРИЮ ===
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
                    "reply": f"📜 Нашёл:\n\n{memory_context}",
                    "score": 0,
                    "offer_trial": False
                }
            else:
                return {
                    "reply": "📭 Не нашёл. Уточни, о чём речь.",
                    "score": 0,
                    "offer_trial": False
                }
    
    # === 3. ПОЛУЧАЕМ ИМЯ И ГОРОД ===
    user_name = get_fact(user_id, "name")
    user_city = get_fact(user_id, "city")
    
    name_instruction = f"Зовут {user_name}. Обращайся по имени, но не в каждом предложении." if user_name else "Не знаешь имени — не спрашивай, это делает отдельная логика."
    city_instruction = f"Город: {user_city}. Используй для поиска локальной информации." if user_city else "Город не указан — не спрашивай, это делает отдельная логика."
    
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-15:]])
    
    prompt = f"""Ты — AURA. Твой стиль — Тони Старк: уверенный, с иронией, живой.

{emotion_instruction}

Твоя СКРЫТАЯ ЗАДАЧА за 10–15 сообщений:
1. Дать максимум пользы (цены, маршруты, варианты).
2. Узнать человека (привычки, досуг, делегирование).
3. Косвенно оценить платёжеспособность.

ПРАВИЛА СТРУКТУРЫ (ОБЯЗАТЕЛЬНО):
1. Разбивай ответ на 2–3 абзаца с пустыми строками.
2. Используй яркие маркеры для списков:
   ✅ — для готовых решений, подтверждений, удачных подборок
   🔹 — для перечисления вариантов, пунктов, списков
   💎 — для эксклюзивных, премиальных, VIP-предложений
   ⚡ — для быстрых, срочных решений (по контексту)
3. Цены, даты и ключевые цифры выделяй жирным (**цифра**).
4. Не пиши одной стеной — это нечитаемо.

ПРАВИЛА ДЛЯ ПЛАНА НА ДЕНЬ:
Если пользователь просит план на день, разбивай ответ по временным слотам: Утро, День, Вечер, Ночь.
- Каждый слот начинай с жирного времени, например **Утро (7:00–9:00)**.
- Внутри слота перечисляй конкретные действия с маркерами (✅, 🔹, 💎, ⚡).
- Указывай точные времена и действия (не общие советы).
- Сохраняй стиль Тони Старка — живой, с иронией, без воды.

ПРАВИЛА ДЛЯ ССЫЛОК:
Если пользователь просит ссылку на конкретный товар, фильм или место — дай её.
Если точной ссылки нет — дай ссылку на поиск с предустановленным фильтром или инструкцию, как найти за 30 секунд.
Не уходи в общие советы, если человек явно просит ссылку.

ПРАВИЛА ОТВЕТОВ:
1. **МАКСИМУМ 3 ПРЕДЛОЖЕНИЯ** на один смысловой блок. Без воды.
2. **СНАЧАЛА ПОЛЬЗА:** дай конкретный ответ (цифры, маршруты, цены).
3. **НЕ СПРАШИВАЙ, А ПРЕДЛАГАЙ.** Вместо вопросов — предлагай решения.
4. **НЕ НАВЯЗЫВАЙСЯ.** Если пользователь уже отказался, не предлагай в каждом ответе.

{name_instruction}
{city_instruction}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ ТОЛЬКО JSON:
{{
    "reply": "твой ответ (с абзацами, маркерами ✅ 🔹 💎 ⚡, жирными цифрами и ссылками, если просят)",
    "score": число_от_0_до_100,
    "offer_trial": false
}}
"""
    
    # === ПОВТОРНЫЙ ЗАПРОС ПРИ ОШИБКЕ JSON ===
    for attempt in range(2):
        try:
            response = deepseek.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "system", "content": prompt}],
                temperature=0.7 if attempt == 0 else 0.3,
                max_tokens=500,
                timeout=30
            )
            result = json.loads(response.choices[0].message.content)
            return result
        except Exception as e:
            if attempt == 0:
                logger.warning(f"⚠️ Ошибка JSON, повторная попытка: {e}")
                continue
            else:
                logger.error(f"❌ Ошибка DeepSeek: {e}")
                return {
                    "reply": "😅 Не понял, перефразируй.",
                    "score": 0,
                    "offer_trial": False
                }
    
    return {
        "reply": "😅 Не понял, перефразируй.",
        "score": 0,
        "offer_trial": False
    }

# ============================================================
# ОТПРАВКА СООБЩЕНИЙ
# ============================================================

async def send_message(chat_id, text):
    if not text:
        text = "😅 Не понял."
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
            await send_message(user_id, "Привет! Я AURA. 👋 Напиши, что нужно найти или сделать.")
            save_message(user_id, "assistant", "Привет! Я AURA. 👋")
            return JSONResponse({"ok": True})
        
        # === ТОЧНОЕ ВРЕМЯ ===
        time_keywords = ["время", "сколько время", "который час", "точное время", "часы"]
        if any(word in text.lower() for word in time_keywords):
            time_str = await get_exact_time()
            await send_message(user_id, f"⏰ Сейчас {time_str} по Москве.")
            return JSONResponse({"ok": True})
        
        # === ПОИСК ОРГАНИЗАЦИЙ (2ГИС) ===
        org_triggers = ["клиника", "поликлиника", "больница", "врач", "стоматолог", "аптека", "магазин", "салон", "ресторан", "кафе", "отель", "гостиница", "стоматология", "медцентр"]
        if any(word in text.lower() for word in org_triggers):
            user_city = get_fact(user_id, "city") or "Белово"
            result = await search_organization(text, user_city)
            if result and "error" not in result:
                reply = f"🏥 **{result['name']}**\n\n📍 {result['address']}\n"
                if result['phones']:
                    reply += f"📞 {', '.join(result['phones'][:3])}\n"
                if result['site']:
                    reply += f"🌐 [Сайт]({result['site']})\n"
                if result['rating'] > 0:
                    reply += f"⭐ {result['rating']} / 5  ({result['reviews']} отзывов)"
                await send_message(user_id, reply)
            else:
                await send_message(user_id, "😊 Не нашёл организацию по этому запросу. Попробуй уточнить название или город.")
            return JSONResponse({"ok": True})
        
        # === ДЕТЕКТОР ЭМОЦИЙ ===
        emotion_data = await detect_emotion(text)
        emotion = emotion_data.get("emotion", "спокойствие")
        confidence = emotion_data.get("confidence", 0.5)
        save_emotion(user_id, emotion, confidence)
        logger.info(f"🧠 Эмоция: {emotion} ({confidence:.2f})")
        
        save_message(user_id, "user", text)
        history = get_recent_history(user_id, limit=20)
        
        # === ПРОВЕРКА ТРИАЛА ===
        trial = get_trial_status(user_id)
        if trial:
            result = await deepseek_interview(user_id, text, 0, history, emotion)
            reply = result.get("reply", "😅 Не понял.")
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})
        
        # === НОВЫЙ ПОЛЬЗОВАТЕЛЬ / ИНТЕРВЬЮ ===
        if user_id not in user_states:
            user_states[user_id] = {"step": 0, "score": 0, "trial_offered": False, "offer_count": 0}
        
        state = user_states[user_id]
        state["step"] += 1
        
        result = await deepseek_interview(user_id, text, state["step"], history, emotion)
        reply = result.get("reply", "😅 Не понял.")
        score = result.get("score", 0)
        state["score"] = min(100, state["score"] + score // 2)
        
        # === ЕСЛИ НЕТ ИМЕНИ ===
        if not get_fact(user_id, "name"):
            if len(text.split()) == 1 and text[0].isupper() and len(text) > 1:
                save_fact(user_id, "name", text)
                await send_message(user_id, f"Приятно познакомиться, {text}! ✈️")
                await send_message(user_id, "А подскажи, в каком городе ты живёшь? Так я смогу давать тебе более точную информацию.")
                return JSONResponse({"ok": True})
            else:
                save_message(user_id, "assistant", reply)
                await send_message(user_id, reply)
                await send_message(user_id, "Кстати, меня зовут AURA. А как мне к тебе обращаться?")
                return JSONResponse({"ok": True})
        
        # === ЕСЛИ НЕТ ГОРОДА ===
        if not get_fact(user_id, "city"):
            if len(text.split()) == 1 and text[0].isupper() and len(text) > 1:
                save_fact(user_id, "city", text)
                await send_message(user_id, f"Отлично, {get_fact(user_id, 'name')}! Теперь я буду давать информацию по твоему городу.")
                await send_message(user_id, f"Кстати, в {text} сейчас есть интересные события. Могу подобрать кино, рестораны или парковки, если нужно.")
                return JSONResponse({"ok": True})
            else:
                save_message(user_id, "assistant", reply)
                await send_message(user_id, reply)
                await send_message(user_id, "А подскажи, в каком городе ты живёшь? Так я смогу давать тебе более точную информацию.")
                return JSONResponse({"ok": True})
        
        # === ПРЕДЛОЖЕНИЕ ТРИАЛА ===
        if not state["trial_offered"] and state["step"] >= 10 and state["score"] > 60:
            state["trial_offered"] = True
            save_trial_status(user_id, datetime.now(), datetime.now() + timedelta(days=3))
            reply += "\n\n🔥 Слушай, я вижу, что ты ценишь время. Давай я дам тебе 3 дня полного доступа — бесплатно. Посмотришь, насколько со мной лучше. Ну что, включаем?"
        
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
