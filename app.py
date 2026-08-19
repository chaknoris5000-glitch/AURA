import os
import json
import httpx
import asyncio
import logging
import tempfile
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GIS_API_KEY = os.getenv("GIS_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

AGENT_SEARCH_ID = os.getenv("YANDEX_AGENT_ID", "fvt3te2kgttig7u3a1fb")
AGENT_RESEARCH_ID = os.getenv("YANDEX_AGENT_RESEARCH_ID", "fvti80ngse2778agbmdl")
AGENT_REASONING_ID = os.getenv("YANDEX_AGENT_REASONING_ID", "fvtg0c38oi7n43d0n9gf")

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
user_states = {}

# ============================================================
# РАСПОЗНАВАНИЕ ГОЛОСА
# ============================================================

def transcribe_audio(audio_url):
    try:
        resp = requests.get(audio_url, timeout=30)
        if resp.status_code != 200:
            return None
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            result = groq.audio.transcriptions.create(
                file=(tmp_path, f.read()),
                model="whisper-large-v3-turbo",
                language="ru"
            )
        os.unlink(tmp_path)
        return result.text
    except Exception as e:
        logger.error(f"❌ Ошибка распознавания: {e}")
        return None

# ============================================================
# ВЫЗОВ АГЕНТОВ ЯНДЕКСА
# ============================================================

def call_yandex_agent(agent_id: str, user_text: str, user_name: str = "", user_city: str = "", budget: str = "") -> str:
    try:
        client = OpenAI(
            api_key=YANDEX_API_KEY,
            base_url="https://ai.api.cloud.yandex.net/v1",
            project=YANDEX_FOLDER_ID
        )
        variables = {
            "user_name": user_name or "Гость",
            "user_city": user_city or "Москва",
            "budget": budget or "не указан"
        }
        response = client.responses.create(
            prompt={
                "id": agent_id,
                "variables": variables
            },
            input=user_text,
            tools=[{"type": "web_search", "filters": {"allowed_domains": []}, "search_context_size": "low"}],
        )
        return response.output_text
    except Exception as e:
        logger.error(f"❌ Ошибка агента Яндекса ({agent_id}): {e}")
        return ""

# ============================================================
# УПАКОВКА ОТВЕТА В СТИЛЬ AURA (КОРОТКО И С ДУШОЙ)
# ============================================================

async def pack_response(raw_text: str, user_name: str = "", user_city: str = "") -> str:
    try:
        prompt = f"""
Ты — AURA. Твой стиль — Тони Старк: уверенный, ироничный, живой.

Перед тобой сырой ответ поискового агента. Твоя задача — превратить его в короткий, красивый ответ в стиле AURA.

ПРАВИЛА (ОБЯЗАТЕЛЬНО):
1. **МАКСИМУМ 3–4 ПРЕДЛОЖЕНИЯ.** Без воды. Только суть.
2. **ОТВЕЧАЙ КОРОТКО:** цифры, факты, вывод.
3. **ИСПОЛЬЗУЙ МАРКЕРЫ:** ✅ — для готовых решений, 💎 — для лучшего варианта, ⚡ — для советов.
4. **ВЫДЕЛЯЙ ГЛАВНОЕ ЖИРНЫМ:** цены, даты, ключевые цифры.
5. **ПИШИ С ДУШОЙ:** с лёгкой иронией, теплотой, как другу.
6. **ССЫЛКИ:** если есть — оформляй как [текст](url).

Убери ссылки на агрегаторы (Aviasales, Яндекс.Путешествия и т.д.). Оставь ссылки на конкретные рейсы.

Используй имя пользователя ({user_name or "Гость"}) и город ({user_city or "Москва"}).

Сырой ответ агента:
{raw_text}

Твой ответ (только текст, коротко и с душой):
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=300,
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
# ПАМЯТЬ, ПОРТРЕТ, ФАКТЫ
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
# ИСТОРИЯ
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

async def send_typing(chat_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки статуса печати: {e}")

async def send_message(chat_id, text):
    if not text:
        text = "😅 Не понял."
    if len(text) > 1500:
        text = text[:1497] + "..."
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=30
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

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
        if portrait.get('budget_travel'):
            parts.append(f"бюджет на поездку: {portrait['budget_travel']} ₽")
        if parts:
            portrait_context = "ПОРТРЕТ: " + ", ".join(parts) + "."
    
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-15:]])
    
    prompt = f"""
Ты — AURA. Твой стиль — Тони Старк: уверенный, ироничный, живой.

{emotion_instruction}

{portrait_context}

ПРАВИЛА ОТВЕТОВ (ОБЯЗАТЕЛЬНО):
1. **МАКСИМУМ 3–4 ПРЕДЛОЖЕНИЯ.** Без воды. Только суть.
2. **ОТВЕЧАЙ КОРОТКО:** цифры, факты, вывод. Без поэм.
3. **ИСПОЛЬЗУЙ МАРКЕРЫ:** ✅ — для готовых решений, 💎 — для лучшего варианта, ⚡ — для советов.
4. **ВЫДЕЛЯЙ ГЛАВНОЕ ЖИРНЫМ:** цены, даты, ключевые цифры.
5. **ПИШИ С ДУШОЙ:** с лёгкой иронией, теплотой, как другу.
6. **ССЫЛКИ:** если есть — оформляй как [текст](url).

Используй портрет для персонализации.

{name_instruction}
{city_instruction}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ ТОЛЬКО JSON:
{{
    "reply": "твой ответ (коротко, с душой, маркерами)",
    "score": 0..100,
    "offer_trial": false
}}
"""
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=200,
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

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        msg = body["message"]
        user_id = msg["from"]["id"]
        
        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    text = transcribe_audio(audio_url)
                    if not text:
                        await send_message(user_id, "⚠️ Не удалось распознать голос. Попробуй ещё раз.")
                        return JSONResponse({"ok": True})
                else:
                    await send_message(user_id, "⚠️ Ошибка загрузки голосового сообщения.")
                    return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса. Попробуй написать!")
                return JSONResponse({"ok": True})
        else:
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
        
        time_keywords = ["время", "сколько время", "который час", "точное время", "часы"]
        if any(word in text.lower() for word in time_keywords):
            user_city = get_fact(user_id, "city") or "Москва"
            time_str = get_time_for_city(user_city)
            await send_typing(user_id)
            await send_message(user_id, f"⏰ Сейчас {time_str} по местному времени ({user_city}).")
            return JSONResponse({"ok": True})
        
        search_triggers = ["найди", "поищи", "цены", "билеты", "скидки", "акции", "новости", "погода", "курс", "стоимость"]
        analyze_triggers = ["сравни", "проанализируй", "исследуй", "изучи", "разбери", "глубоко", "детально"]
        reason_triggers = ["посоветуй", "что лучше", "как поступить", "выбери", "рекомендуй", "какой вариант", "стоит ли"]
        
        if any(word in text.lower() for word in search_triggers + analyze_triggers + reason_triggers):
            user_name = get_fact(user_id, "name") or "Гость"
            user_city = get_fact(user_id, "city") or "Москва"
            budget = get_fact(user_id, "budget_travel") or ""
            
            await send_typing(user_id)
            
            if any(word in text.lower() for word in reason_triggers):
                agent_id = AGENT_REASONING_ID
                logger.info(f"🧠 Использую рассуждающего агента для запроса: {text}")
            elif any(word in text.lower() for word in analyze_triggers):
                agent_id = AGENT_RESEARCH_ID
                logger.info(f"🔬 Использую агента-исследователя для запроса: {text}")
            else:
                agent_id = AGENT_SEARCH_ID
                logger.info(f"🔍 Использую поискового агента для запроса: {text}")
            
            try:
                raw_result = call_yandex_agent(agent_id, text, user_name, user_city, budget)
                if raw_result:
                    packed = await pack_response(raw_result, user_name, user_city)
                    await send_message(user_id, packed)
                    save_message(user_id, "assistant", packed)
                    return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка агента Яндекса: {e}")
            
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
