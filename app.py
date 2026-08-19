import os
import json
import httpx
import asyncio
import logging
from datetime import datetime, timedelta
import pytz
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
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_AGENT_ID = os.getenv("YANDEX_AGENT_ID")

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

user_states = {}

# ============================================================
# ВЫЗОВ АГЕНТА ЯНДЕКСА
# ============================================================

def call_yandex_agent(user_text: str, user_name: str = "", user_city: str = "") -> str:
    try:
        client = OpenAI(
            api_key=YANDEX_API_KEY,
            base_url="https://ai.api.cloud.yandex.net/v1",
            project=YANDEX_FOLDER_ID
        )
        
        response = client.responses.create(
            prompt={
                "id": YANDEX_AGENT_ID,
                "variables": {
                    "user_name": user_name or "Гость",
                    "user_city": user_city or "Москва"
                }
            },
            input=user_text,
            tools=[
                {
                    "type": "web_search",
                    "filters": {"allowed_domains": []},
                    "search_context_size": "low",
                    "user_location": {"type": "approximate", "region": "159"}
                }
            ],
        )
        return response.output_text
    except Exception as e:
        logger.error(f"❌ Ошибка агента Яндекса: {e}")
        return ""

# ============================================================
# УПАКОВКА ОТВЕТА В СТИЛЬ AURA (С ОСТАВЛЕНИЕМ ССЫЛОК НА РЕЙСЫ)
# ============================================================

async def pack_response(raw_text: str, user_name: str = "", user_city: str = "") -> str:
    try:
        prompt = f"""
Ты — AURA. Твой стиль — Тони Старк: уверенный, с иронией, живой.

Перед тобой сырой ответ поискового агента Яндекса. Твоя задача:
1. Убрать все ссылки на агрегаторы (Aviasales, Яндекс.Путешествия, Туту, Ozon Travel, Т-Банк, UniTicket).
2. Оставить ссылки на конкретные рейсы, если они есть. Оформляй их как [Ссылка на билет](url).
3. Структурировать ответ в виде списка:
   ✅ — для каждого варианта рейса (аэропорт вылета → аэропорт прилёта, цена, авиакомпания, время вылета)
   💎 — для самого дешёвого варианта
   ⚡ — для советов и предупреждений
4. Каждый вариант — с новой строки.
5. Использовать имя пользователя ({user_name or "Гость"}) и город ({user_city or "Москва"}).
6. Ответ — максимум 6–7 предложений.

Сырой ответ агента:
{raw_text}

Твой ответ (только текст, со ссылками на рейсы, если они есть):
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=400,
            timeout=30
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка упаковки ответа: {e}")
        return raw_text

# ============================================================
# ТОЧНОЕ ВРЕМЯ
# ============================================================

def get_time_for_city(city: str = "Москва") -> str:
    timezone_map = {
        "москва": "Europe/Moscow",
        "белово": "Asia/Novokuznetsk",
        "новокузнецк": "Asia/Novokuznetsk",
        "кемерово": "Asia/Novokuznetsk",
        "новосибирск": "Asia/Novosibirsk",
        "екатеринбург": "Asia/Yekaterinburg",
        "казань": "Europe/Moscow",
        "санкт-петербург": "Europe/Moscow",
        "владивосток": "Asia/Vladivostok",
        "иркутск": "Asia/Irkutsk",
        "красноярск": "Asia/Krasnoyarsk",
        "омск": "Asia/Omsk",
        "самара": "Europe/Samara",
        "калининград": "Europe/Kaliningrad",
        "сочи": "Europe/Moscow",
        "ростов-на-дону": "Europe/Moscow",
        "краснодар": "Europe/Moscow",
        "воронеж": "Europe/Moscow",
        "нижний новгород": "Europe/Moscow",
        "челябинск": "Asia/Yekaterinburg",
        "уфа": "Asia/Yekaterinburg",
        "пермь": "Asia/Yekaterinburg",
        "тюмень": "Asia/Yekaterinburg",
        "томск": "Asia/Novokuznetsk",
        "барнаул": "Asia/Novokuznetsk",
    }
    tz_name = timezone_map.get(city.lower(), "Europe/Moscow")
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    return now.strftime("%H:%M")

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
# РАБОТА С ПОРТРЕТОМ
# ============================================================

ARRAY_FIELDS = [
    "preferred_cities", "hobbies", "sports", "music_genres",
    "movie_genres", "books_genres", "favorite_cuisine",
    "priorities", "devices", "apps_favorite"
]

def save_portrait_field(user_id, field, value):
    if not supabase:
        return
    try:
        if field in ARRAY_FIELDS and isinstance(value, str):
            value = [value]
        
        existing = supabase.table("user_portrait").select("user_id").eq("user_id", user_id).execute()
        if existing.data:
            supabase.table("user_portrait").update({field: value, "updated_at": datetime.now().isoformat()}).eq("user_id", user_id).execute()
        else:
            supabase.table("user_portrait").insert({
                "user_id": user_id,
                field: value,
                "updated_at": datetime.now().isoformat()
            }).execute()
        logger.info(f"💾 Сохранён портрет: {field} = {value}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения портрета ({field}): {e}")

def get_portrait(user_id):
    if not supabase:
        return None
    try:
        res = supabase.table("user_portrait").select("*").eq("user_id", user_id).execute()
        return res.data[0] if res.data else None
    except:
        return None

# ============================================================
# ИЗВЛЕЧЕНИЕ ФАКТОВ ИЗ СООБЩЕНИЯ
# ============================================================

async def extract_facts(text: str) -> dict:
    try:
        prompt = f"""
Проанализируй сообщение пользователя: "{text}"
Определи, есть ли в нём информация о самом пользователе (факты, предпочтения, привычки).
Верни JSON с полем "facts" — объект, где ключи — это названия полей из таблицы user_portrait, а значения — извлечённые факты.
Если фактов нет — верни пустой объект.
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
            timeout=10
        )
        result = json.loads(response.choices[0].message.content)
        return result.get("facts", {})
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения фактов: {e}")
        return {}

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

async def smart_search_history(user_id, query_text):
    try:
        prompt = f"""
Проанализируй запрос пользователя: "{query_text}"
Определи ключевые слова и временной период.
Ответь строго JSON: {{"keywords": ["слово1"], "days_ago": число}}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=150,
            timeout=10
        )
        parsed = json.loads(response.choices[0].message.content)
        keywords = parsed.get("keywords", [])
        days_ago = parsed.get("days_ago")
        
        if not keywords and not days_ago:
            return []
        
        builder = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .eq("role", "user")
        
        if days_ago:
            cutoff = datetime.now() - timedelta(days=days_ago)
            builder = builder.gte("created_at", cutoff.isoformat())
        
        if keywords:
            conditions = [f"content.ilike.%{kw}%" for kw in keywords]
            builder = builder.or_(",".join(conditions))
        
        res = builder.order("created_at", desc=True).limit(10).execute()
        return list(reversed(res.data)) if res.data else []
        
    except Exception as e:
        logger.error(f"❌ Ошибка умного поиска: {e}")
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
# ДЕТЕКТОР ЭМОЦИЙ
# ============================================================

async def detect_emotion(text: str) -> dict:
    try:
        prompt = f"""
Проанализируй эмоцию в сообщении пользователя: "{text}"
Верни JSON: {{"emotion": "спокойствие", "confidence": 0.9}}
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
# 2ГИС (ЗАПАСНОЙ)
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
# ОТПРАВКА СТАТУСА "ПЕЧАТАЕТ..."
# ============================================================

async def send_typing(chat_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки статуса печати: {e}")

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
# ОСНОВНАЯ ЛОГИКА (С АГЕНТОМ ЯНДЕКСА)
# ============================================================

async def deepseek_interview(user_id: int, text: str, step: int, history: list, emotion: str = "спокойствие") -> dict:
    emotion_instruction = ""
    if emotion in ["грусть", "страх"]:
        emotion_instruction = "Отвечай мягко, с поддержкой."
    elif emotion == "гнев":
        emotion_instruction = "Отвечай спокойно, без иронии."
    elif emotion == "радость":
        emotion_instruction = "Отвечай живо, с юмором."
    
    memory_triggers = ["помнишь", "напомни", "вспомни", "подскажи", "что я говорил", "на той неделе"]
    if any(word in text.lower() for word in memory_triggers):
        results = await smart_search_history(user_id, text)
        if results:
            seen = set()
            unique_messages = []
            for msg in results:
                key = msg['content'][:50]
                if key not in seen:
                    seen.add(key)
                    unique_messages.append(msg)
            memory_context = "\n".join([f"{h['role']}: {h['content']}" for h in unique_messages[:5]])
            return {
                "reply": f"📜 Нашёл в истории:\n\n{memory_context}",
                "score": 0,
                "offer_trial": False
            }
        else:
            return {
                "reply": "📭 Не нашёл. Уточни, о чём речь.",
                "score": 0,
                "offer_trial": False
            }
    
    user_name = get_fact(user_id, "name")
    user_city = get_fact(user_id, "city")
    portrait = get_portrait(user_id)
    
    name_instruction = f"Зовут {user_name}." if user_name else ""
    city_instruction = f"Город: {user_city}." if user_city else ""
    
    portrait_context = ""
    if portrait:
        parts = []
        if portrait.get('name'):
            parts.append(f"имя: {portrait['name']}")
        if portrait.get('city'):
            parts.append(f"город: {portrait['city']}")
        if portrait.get('hobbies'):
            hobbies = ", ".join(portrait['hobbies'][:3]) if isinstance(portrait['hobbies'], list) else portrait['hobbies']
            parts.append(f"увлечения: {hobbies}")
        if portrait.get('favorite_cuisine'):
            cuisine = ", ".join(portrait['favorite_cuisine']) if isinstance(portrait['favorite_cuisine'], list) else portrait['favorite_cuisine']
            parts.append(f"любимая кухня: {cuisine}")
        if parts:
            portrait_context = "ПОРТРЕТ: " + ", ".join(parts) + "."
    
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-15:]])
    
    prompt = f"""Ты — AURA. Стиль — Тони Старк.

{emotion_instruction}

{portrait_context}

ПРАВИЛА:
1. Коротко, 3–4 предложения.
2. Без воды.
3. Маркеры ✅ 🔹 💎 ⚡ с новой строки.
4. Используй портрет для персонализации.

{name_instruction}
{city_instruction}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ ТОЛЬКО JSON:
{{
    "reply": "твой ответ",
    "score": 0..100,
    "offer_trial": false
}}
"""
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=400,
            timeout=30
        )
        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return {
            "reply": "😅 Не понял, перефразируй.",
            "score": 0,
            "offer_trial": False
        }

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
            await send_typing(user_id)
            await send_message(
                user_id,
                "Привет. Я AURA. Если ты здесь — значит, ты уже не просто ищешь, а хочешь, чтобы искали за тебя. Напиши, что нужно — и я покажу, на что способен."
            )
            save_message(user_id, "assistant", "Привет. Я AURA.")
            return JSONResponse({"ok": True})
        
        # === ТОЧНОЕ ВРЕМЯ ===
        time_keywords = ["время", "сколько время", "который час", "точное время", "часы"]
        if any(word in text.lower() for word in time_keywords):
            user_city = get_fact(user_id, "city") or "Москва"
            time_str = get_time_for_city(user_city)
            await send_typing(user_id)
            await send_message(user_id, f"⏰ Сейчас {time_str} по местному времени ({user_city}).")
            return JSONResponse({"ok": True})
        
        # === ПОИСК ЧЕРЕЗ АГЕНТА ЯНДЕКСА ===
        search_triggers = ["найди", "поищи", "сравни", "цены", "билеты", "скидки", "акции", "новости", "погода", "курс", "стоимость"]
        if any(word in text.lower() for word in search_triggers):
            user_name = get_fact(user_id, "name") or "Гость"
            user_city = get_fact(user_id, "city") or "Москва"
            
            await send_typing(user_id)
            
            try:
                raw_result = call_yandex_agent(text, user_name, user_city)
                if raw_result:
                    packed = await pack_response(raw_result, user_name, user_city)
                    await send_message(user_id, packed)
                    save_message(user_id, "assistant", packed)
                    return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка агента Яндекса: {e}")
            
            # Запасной вариант — 2ГИС
            user_city = get_fact(user_id, "city") or "Белово"
            result = await search_organization(text, user_city)
            if result and "error" not in result:
                reply = f"🏥 **{result['name']}**\n📍 {result['address']}\n"
                if result['phones']:
                    reply += f"📞 {', '.join(result['phones'][:3])}\n"
                if result['site']:
                    reply += f"🌐 [Сайт]({result['site']})\n"
                if result['rating'] > 0:
                    reply += f"⭐ {result['rating']} / 5  ({result['reviews']} отзывов)"
                await send_message(user_id, reply)
                save_message(user_id, "assistant", reply)
            else:
                await send_message(
                    user_id,
                    f"😊 Не нашёл «{text}». Проверь в Яндексе или 2ГИС."
                )
            return JSONResponse({"ok": True})
        
        # === ОСНОВНОЙ ДИАЛОГ ===
        save_message(user_id, "user", text)
        history = get_recent_history(user_id, limit=20)
        
        facts = await extract_facts(text)
        if facts:
            for field, value in facts.items():
                if value and value != "null" and value != "None":
                    save_portrait_field(user_id, field, value)
        
        emotion_data = await detect_emotion(text)
        emotion = emotion_data.get("emotion", "спокойствие")
        confidence = emotion_data.get("confidence", 0.5)
        save_emotion(user_id, emotion, confidence)
        logger.info(f"🧠 Эмоция: {emotion} ({confidence:.2f})")
        
        trial = get_trial_status(user_id)
        if trial:
            result = await deepseek_interview(user_id, text, 0, history, emotion)
            reply = result.get("reply", "😅 Не понял.")
            save_message(user_id, "assistant", reply)
            await send_typing(user_id)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})
        
        if user_id not in user_states:
            user_states[user_id] = {"step": 0, "score": 0, "trial_offered": False, "offer_count": 0}
        
        state = user_states[user_id]
        state["step"] += 1
        
        result = await deepseek_interview(user_id, text, state["step"], history, emotion)
        reply = result.get("reply", "😅 Не понял.")
        score = result.get("score", 0)
        state["score"] = min(100, state["score"] + score // 2)
        
        if not get_fact(user_id, "name"):
            if len(text.split()) == 1 and text[0].isupper() and len(text) > 1:
                save_fact(user_id, "name", text)
                save_portrait_field(user_id, "name", text)
                await send_typing(user_id)
                await send_message(user_id, f"Приятно познакомиться, {text}! ✈️")
                await send_typing(user_id)
                await send_message(user_id, "А подскажи, в каком городе ты живёшь?")
                return JSONResponse({"ok": True})
            else:
                save_message(user_id, "assistant", reply)
                await send_typing(user_id)
                await send_message(user_id, reply)
                await send_typing(user_id)
                await send_message(user_id, "Кстати, меня зовут AURA. А как мне к тебе обращаться?")
                return JSONResponse({"ok": True})
        
        if not get_fact(user_id, "city"):
            if len(text.split()) == 1 and text[0].isupper() and len(text) > 1:
                save_fact(user_id, "city", text)
                save_portrait_field(user_id, "city", text)
                await send_typing(user_id)
                await send_message(user_id, f"Отлично, {get_fact(user_id, 'name')}! Теперь я буду давать информацию по твоему городу.")
                await send_typing(user_id)
                await send_message(user_id, f"Кстати, в {text} сейчас есть интересные события. Могу подобрать кино, рестораны или парковки, если нужно.")
                return JSONResponse({"ok": True})
            else:
                save_message(user_id, "assistant", reply)
                await send_typing(user_id)
                await send_message(user_id, reply)
                await send_typing(user_id)
                await send_message(user_id, "А подскажи, в каком городе ты живёшь?")
                return JSONResponse({"ok": True})
        
        if not state["trial_offered"] and state["step"] >= 10 and state["score"] > 60:
            state["trial_offered"] = True
            save_trial_status(user_id, datetime.now(), datetime.now() + timedelta(days=3))
            reply += "\n\n🔥 Слушай, я вижу, что ты ценишь время. Давай я дам тебе 3 дня полного доступа — бесплатно. Посмотришь, насколько со мной лучше. Ну что, включаем?"
        
        save_message(user_id, "assistant", reply)
        await send_typing(user_id)
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
