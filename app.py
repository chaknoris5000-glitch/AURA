import os
import json
import httpx
import asyncio
import logging
import tempfile
import hashlib
import base64
import re
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
import requests
from collections import deque

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GIS_API_KEY = os.getenv("GIS_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "5818548555")

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

user_last_requests = {}

def get_request_hash(user_id, text):
    return hashlib.md5(f"{user_id}:{text}".encode()).hexdigest()

def is_duplicate(user_id, text):
    hash_val = get_request_hash(user_id, text)
    if user_id not in user_last_requests:
        user_last_requests[user_id] = deque(maxlen=5)
    if hash_val in user_last_requests[user_id]:
        return True
    user_last_requests[user_id].append(hash_val)
    return False

agent_cache = {}

def get_cached_response(hash_val):
    if hash_val in agent_cache:
        entry = agent_cache[hash_val]
        if datetime.now() - entry["timestamp"] < timedelta(minutes=5):
            return entry["response"]
        else:
            del agent_cache[hash_val]
    return None

def cache_response(hash_val, response):
    agent_cache[hash_val] = {"response": response, "timestamp": datetime.now()}

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
        logger.info(f"💾 Факт сохранён: {key} = {value}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения факта: {e}")

def get_fact(user_id, key):
    if not supabase:
        return None
    try:
        res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except Exception as e:
        logger.error(f"❌ Ошибка получения факта: {e}")
        return None

def get_portrait(user_id):
    if not supabase:
        return None
    try:
        res = supabase.table("user_portrait").select("*").eq("user_id", user_id).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"❌ Ошибка получения портрета: {e}")
        return None

def save_portrait_field(user_id, field, value):
    if not supabase:
        return
    try:
        if field in ["preferred_cities", "hobbies", "sports", "music_genres", "movie_genres", "books_genres", "favorite_cuisine", "priorities", "devices", "apps_favorite"] and isinstance(value, str):
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
        logger.info(f"💾 Портрет сохранён: {field} = {value}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения портрета: {e}")

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
        logger.info(f"📝 Сообщение сохранено в историю: {role}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения истории: {e}")

def get_all_history(user_id):
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .order("created_at")\
            .execute()
        if res.data:
            logger.info(f"📚 Загружена ВСЯ история: {len(res.data)} сообщений")
            return res.data
        return []
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки истории: {e}")
        return []

def clear_user_history(user_id):
    if not supabase:
        return
    try:
        supabase.table("history").delete().eq("user_id", user_id).execute()
        logger.info(f"🧹 История очищена для {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка очистки истории: {e}")

# ============================================================
# ПОИСК В ИСТОРИИ (ДЛЯ ВОСПОМИНАНИЙ)
# ============================================================
def find_in_history(history, text):
    if not history:
        return []
    stop_words = ["напомни", "скажи", "что", "я", "говорил", "про", "о", "в", "и", "с", "на", "за", "по", "из", "от", "для", "мне", "ты", "мы", "они", "он", "она", "оно", "вот", "этот", "эта", "это", "эти", "который", "которая", "которые", "которое", "мне", "меня", "тебя", "тебе", "ещё", "было", "были", "была"]
    keywords = []
    for word in text.lower().split():
        if len(word) > 2 and word not in stop_words:
            keywords.append(word)
    if not keywords:
        return []
    logger.info(f"🔍 Ищем в истории по словам: {keywords}")
    found = []
    for msg in history:
        content_lower = msg['content'].lower()
        for keyword in keywords:
            if keyword in content_lower:
                found.append(msg)
                break
    logger.info(f"📌 Найдено {len(found)} сообщений")
    return found

def extract_facts(found_messages, text):
    if not found_messages:
        return None
    text_lower = text.lower()
    scored = []
    keywords = [w for w in text_lower.split() if len(w) > 2]
    for msg in found_messages:
        content_lower = msg['content'].lower()
        score = 0
        for keyword in keywords:
            if keyword in content_lower:
                score += 1
        scored.append((score, msg))
    scored.sort(key=lambda x: x[0], reverse=True)
    top_messages = [msg for _, msg in scored[:5]]
    parts = []
    for msg in top_messages:
        role = "Пользователь" if msg['role'] == 'user' else "AURA"
        content = msg['content'][:300]
        parts.append(f"{role}: {content}")
    if not parts:
        return None
    result = "\n\n".join(parts)
    logger.info(f"📌 Извлечено {len(parts)} сообщений из истории")
    return result

# ============================================================
# РУКИ
# ============================================================
async def collect_lead(user_id: int, name: str, phone: str, question: str = ""):
    if not supabase:
        return {"error": "Supabase не подключён"}
    try:
        supabase.table("leads").insert({
            "user_id": user_id,
            "name": name,
            "phone": phone,
            "question": question,
            "created_at": datetime.now().isoformat()
        }).execute()
        logger.info(f"✅ Заявка сохранена: {name} ({phone})")
        admin_message = f"""
📩 **Новая заявка!**

👤 Имя: {name}
📱 Телефон: {phone}
💬 Вопрос: {question if question else "Не указан"}

📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}
"""
        await send_message(ADMIN_CHAT_ID, admin_message)
        return {"status": "success", "name": name, "phone": phone}
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения заявки: {e}")
        return {"error": str(e)}

# ============================================================
# РАСПОЗНАВАНИЕ КАРТИНОК (ОСТАВЛЯЮ)
# ============================================================
async def recognize_image_with_deepseek(image_url: str) -> str:
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code != 200:
            return "⚠️ Не удалось загрузить изображение."
        image_base64 = base64.b64encode(response.content).decode('utf-8')
        prompt = """
Ты — AURA. Ты видишь картинку и должен:
1. Если на картинке есть текст — распознай его и напиши.
2. Если есть что-то ещё — опиши кратко, что изображено.
3. Ответь коротко, на русском, без лишней воды.
"""
        vision_client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL
        )
        response = vision_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=300,
            temperature=0.5
        )
        result = response.choices[0].message.content
        logger.info(f"🖼️ DeepSeek Vision ответ: {result[:100] if result else 'пустой'}...")
        if not result or len(result.strip()) < 5:
            return "На картинке не удалось ничего распознать."
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка распознавания через DeepSeek Vision: {e}")
        return "⚠️ Не удалось распознать изображение. Попробуйте ещё раз."

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

def call_yandex_agent(agent_id: str, user_text: str, user_name: str = "", user_city: str = "", budget: str = "") -> str:
    hash_val = hashlib.md5(f"{agent_id}:{user_text}:{user_name}:{user_city}:{budget}".encode()).hexdigest()
    cached = get_cached_response(hash_val)
    if cached:
        logger.info("⚡ Ответ из кеша")
        return cached
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
        result = response.output_text
        cache_response(hash_val, result)
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка агента Яндекса ({agent_id}): {e}")
        return ""

def detect_content_platform(text: str) -> dict:
    try:
        prompt = f"""
Определи, где лучше искать контент по запросу: "{text}"

Правила:
- Фильм, сериал, клип → Яндекс.Видео
- Рецепт, обзор, как приготовить → YouTube
- Картинки, фото → Яндекс.Картинки
- Товар → Wildberries или Ozon
- Билеты, отели → Aviasales

Верни JSON:
{{
    "platform": "yandex_video | youtube | yandex_images | wildberries | ozon | aviasales | none",
    "search_query": "уточнённый запрос"
}}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=120,
            timeout=5
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"❌ Ошибка определения платформы: {e}")
        return {"platform": "yandex_video", "search_query": text}

def get_platform_link(platform: str, query: str) -> str:
    query_encoded = query.replace(' ', '+')
    if platform == "yandex_video":
        return f"https://yandex.ru/video/search?text={query_encoded}"
    elif platform == "youtube":
        return f"https://www.youtube.com/results?search_query={query_encoded}"
    elif platform == "rutube":
        return f"https://rutube.ru/search/?q={query_encoded}"
    elif platform == "yandex_images":
        return f"https://yandex.ru/images/search?text={query_encoded}"
    elif platform == "wildberries":
        return f"https://www.wildberries.ru/catalog/0/search.aspx?search={query_encoded}"
    elif platform == "ozon":
        return f"https://www.ozon.ru/search/?text={query_encoded}"
    elif platform == "aviasales":
        return f"https://www.aviasales.ru/search?q={query_encoded}"
    else:
        return None

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
        logger.error(f"❌ Ошибка отправки статуса: {e}")

async def send_message(chat_id, text):
    if not text:
        text = "😅 Не понял."
    if len(text) > 4096:
        text = text[:4093] + "..."
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=30
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

# ============================================================
# ОСНОВНАЯ ЛОГИКА (КОРОТКИЙ КОНТЕКСТ + ПОИСК В ИСТОРИИ)
# ============================================================
async def deepseek_interview(user_id: int, text: str, history: list) -> dict:
    user_name = get_fact(user_id, "name") or "Гость"
    user_city = get_fact(user_id, "city") or "Москва"
    portrait = get_portrait(user_id)
    
    portrait_context = ""
    if portrait:
        parts = []
        if portrait.get('name'): parts.append(f"имя: {portrait['name']}")
        if portrait.get('city'): parts.append(f"город: {portrait['city']}")
        if portrait.get('hobbies'):
            hobbies = ", ".join(portrait['hobbies'][:3]) if isinstance(portrait['hobbies'], list) else portrait['hobbies']
            parts.append(f"увлечения: {hobbies}")
        if portrait.get('favorite_cuisine'):
            cuisine = ", ".join(portrait['favorite_cuisine']) if isinstance(portrait['favorite_cuisine'], list) else portrait['favorite_cuisine']
            parts.append(f"любимая кухня: {cuisine}")
        if parts:
            portrait_context = "ПОРТРЕТ: " + ", ".join(parts) + "."

    # Проверяем, хочет ли пользователь вспомнить что-то из истории
    is_reminder = any(word in text.lower() for word in ["напомни", "вспомни", "что я говорил", "что мы обсуждали", "повтори"])
    
    if is_reminder:
        # Ищем в истории по ключевым словам
        found = find_in_history(history, text)
        facts = extract_facts(found, text) if found else None
        
        if facts:
            logger.info(f"📌 Найдено в истории: {facts[:200]}...")
            context = f"ВОТ ЧТО БЫЛО В ИСТОРИИ ПО ЭТОМУ ЗАПРОСУ:\n{facts}"
        else:
            context = "В ИСТОРИИ НИЧЕГО НЕ НАЙДЕНО ПО ЭТОМУ ЗАПРОСУ."
    else:
        # Обычный запрос — передаём только последние 15 сообщений для контекста
        history_text = ""
        if history:
            history_lines = []
            for h in history[-15:]:
                role = "Пользователь" if h['role'] == 'user' else "AURA"
                content = h['content']
                history_lines.append(f"{role}: {content}")
            history_text = "\n".join(history_lines)
        context = f"ИСТОРИЯ (последние 15 сообщений):\n{history_text}"

    prompt = f"""
Ты — AURA. Отвечай коротко — максимум 2-3 предложения. Без приветствий, без "здравствуйте". Просто продолжай диалог.

{portrait_context}

{context}

СЕЙЧАС ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ (2-3 предложения, по делу):
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=150,
            timeout=20
        )
        reply = response.choices[0].message.content
        logger.info(f"✅ DeepSeek ответил")
        return {"reply": reply, "score": 0}
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return {"reply": "😅 Не понял.", "score": 0}

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

        # ============================================================
        # ОБРАБОТКА КАРТИНОК
        # ============================================================
        if "photo" in msg or "document" in msg:
            if "photo" in msg:
                file_id = msg["photo"][-1]["file_id"]
            else:
                file_id = msg["document"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            await send_typing(user_id)
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    recognized_text = await recognize_image_with_deepseek(image_url)
                    await send_message(user_id, recognized_text)
                    save_message(user_id, "assistant", recognized_text)
                else:
                    await send_message(user_id, "⚠️ Не удалось загрузить изображение.")
            except Exception as e:
                logger.error(f"❌ Ошибка обработки медиа: {e}")
                await send_message(user_id, "⚠️ Не удалось распознать изображение.")
            return JSONResponse({"ok": True})

        # ============================================================
        # ОБРАБОТКА ГОЛОСА
        # ============================================================
        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    text = transcribe_audio(audio_url)
                    if text:
                        save_message(user_id, "user", text)
                    else:
                        await send_message(user_id, "⚠️ Не удалось распознать голос. Попробуй ещё раз.")
                        return JSONResponse({"ok": True})
                else:
                    await send_message(user_id, "⚠️ Ошибка загрузки голосового сообщения.")
                    return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса. Попробуй написать!")
                return JSONResponse({"ok": True})
        
        if not text:
            return JSONResponse({"ok": True})

        # ============================================================
        # ЗАЩИТА ОТ ПОВТОРОВ
        # ============================================================
        if is_duplicate(user_id, text):
            logger.warning(f"⚠️ Повторный запрос от {user_id}: {text}")
            return JSONResponse({"ok": True})

        # ============================================================
        # ВРЕМЯ
        # ============================================================
        time_phrases = ["сколько время", "который час", "точное время", "часы покажи", "время сейчас", "какое время", "сколько сейчас время", "который сейчас час"]
        if any(phrase in text.lower() for phrase in time_phrases):
            user_city = get_fact(user_id, "city") or "Москва"
            await send_typing(user_id)
            await send_message(user_id, f"⏰ Сейчас {get_time_for_city(user_city)} по местному времени ({user_city}).")
            return JSONResponse({"ok": True})

        # ============================================================
        # КОМАНДЫ
        # ============================================================
        if text == "/start":
            await send_typing(user_id)
            await send_message(user_id, "Привет. Я AURA. Если ты здесь — значит, ты уже не просто ищешь, а хочешь, чтобы искали за тебя. Напиши, что нужно — и я покажу, на что способен.")
            save_message(user_id, "assistant", "Привет. Я AURA.")
            return JSONResponse({"ok": True})

        if text.lower() in ["/clear", "/reset", "сброс", "забудь", "хватит"]:
            clear_user_history(user_id)
            await send_message(user_id, "✅ История очищена. Начинаем с чистого листа.")
            return JSONResponse({"ok": True})

        # ============================================================
        # ОБРАБОТКА ЗАЯВОК
        # ============================================================
        if any(word in text.lower() for word in ["заявк", "хочу сотрудничать", "свяжитесь", "обратная связь"]):
            await send_message(user_id, "📩 Отлично! Давайте соберём ваши контакты.\n\nКак вас зовут?")
            user_states[user_id] = {"state": "collecting_name"}
            return JSONResponse({"ok": True})

        if user_id in user_states and user_states[user_id].get("state") == "collecting_name":
            name = text.strip()
            user_states[user_id]["name"] = name
            user_states[user_id]["state"] = "collecting_phone"
            await send_message(user_id, f"Спасибо, {name}! Теперь ваш номер телефона (в формате +7...).")
            return JSONResponse({"ok": True})

        if user_id in user_states and user_states[user_id].get("state") == "collecting_phone":
            phone = text.strip()
            name = user_states[user_id].get("name", "Гость")
            await collect_lead(user_id, name, phone, "")
            await send_message(user_id, f"✅ Спасибо, {name}! Ваша заявка принята. С вами свяжутся в ближайшее время.")
            del user_states[user_id]
            return JSONResponse({"ok": True})

        # ============================================================
        # ОСНОВНАЯ ЛОГИКА
        # ============================================================
        save_message(user_id, "user", text)

        history = get_all_history(user_id)
        logger.info(f"📚 Загружена ПОЛНАЯ история для user_id {user_id}: {len(history)} сообщений")

        # Знакомство — имя (НЕ путать с поисковыми запросами)
        if not get_fact(user_id, "name"):
            is_search = any(text.lower().startswith(word) for word in ["найди", "поищи", "найти", "искать"])
            if len(text.split()) == 1 and text[0].isupper() and len(text) > 1 and not is_search:
                save_fact(user_id, "name", text)
                save_portrait_field(user_id, "name", text)
                await send_typing(user_id)
                await send_message(user_id, f"Приятно познакомиться, {text}! ✈️")
                await send_typing(user_id)
                await send_message(user_id, "А подскажи, в каком городе ты живёшь?")
                return JSONResponse({"ok": True})
            else:
                result = await deepseek_interview(user_id, text, history)
                reply = result.get("reply", "😅 Не понял.")
                save_message(user_id, "assistant", reply)
                await send_typing(user_id)
                await send_message(user_id, reply)
                return JSONResponse({"ok": True})

        # Знакомство — город
        if not get_fact(user_id, "city"):
            if len(text.split()) == 1 and text[0].isupper() and len(text) > 1:
                save_fact(user_id, "city", text)
                save_portrait_field(user_id, "city", text)
                user_name = get_fact(user_id, "name") or "Гость"
                await send_typing(user_id)
                await send_message(user_id, f"Отлично, {user_name}! Теперь я буду давать информацию по твоему городу.")
                await send_typing(user_id)
                await send_message(user_id, f"Кстати, в {text} сейчас есть интересные события. Могу подобрать кино, рестораны или парковки, если нужно.")
                return JSONResponse({"ok": True})
            else:
                result = await deepseek_interview(user_id, text, history)
                reply = result.get("reply", "😅 Не понял.")
                save_message(user_id, "assistant", reply)
                await send_typing(user_id)
                await send_message(user_id, reply)
                return JSONResponse({"ok": True})

        # ============================================================
        # ОСНОВНАЯ ЛОГИКА С ПОИСКОМ
        # ============================================================
        user_name = get_fact(user_id, "name") or "Гость"
        user_city = get_fact(user_id, "city") or "Москва"
        budget = get_fact(user_id, "budget_travel") or ""

        await send_typing(user_id)

        content_triggers = ["фильм", "сериал", "видео", "рецепт", "картинки", "фото", "изображения", "обзор", "как приготовить", "как сделать", "смотреть", "клип", "трейлер"]
        search_triggers = ["найди", "поищи", "цены", "билеты", "скидки", "акции", "новости", "погода", "курс", "стоимость", "купить", "товар"]
        analyze_triggers = ["сравни", "проанализируй", "исследуй", "изучи", "разбери"]
        reason_triggers = ["посоветуй", "что лучше", "как поступить", "выбери", "рекомендуй", "стоит ли"]

        if any(word in text.lower() for word in content_triggers):
            text_lower = text.lower()
            
            if any(word in text_lower for word in ["рецепт", "приготовить", "блюдо", "еда", "готовка", "кулинария"]):
                platform = "youtube"
                search_query = text
                platform_name = "YouTube"
                link_text = f"🔗 [Смотреть рецепты на YouTube](https://www.youtube.com/results?search_query={text.replace(' ', '+')})"
            elif "rutube" in text_lower:
                platform = "rutube"
                search_query = text
                platform_name = "Rutube"
                link_text = f"🔗 [Смотреть на Rutube](https://rutube.ru/search/?q={text.replace(' ', '+')})"
            else:
                platform_info = detect_content_platform(text)
                platform = platform_info.get("platform", "yandex_video")
                search_query = platform_info.get("search_query", text)
                platform_name = {"yandex_video": "Яндекс.Видео", "youtube": "YouTube", "yandex_images": "Яндекс.Картинки"}.get(platform, "Яндекс.Видео")
                link_text = get_platform_link(platform, search_query)
                if link_text:
                    link_text = f"🔗 [Смотреть на {platform_name}]({link_text})"
            
            platform_map = {"yandex_video": "яндекс видео", "youtube": "youtube", "yandex_images": "яндекс картинки", "rutube": "rutube"}
            full_query = f"{search_query} {platform_map.get(platform, 'яндекс видео')}"
            
            raw_result = call_yandex_agent(AGENT_SEARCH_ID, full_query, user_name, user_city)
            
            if raw_result:
                packed_prompt = f"""
Ты — AURA. Отвечай коротко — 2-3 предложения. Добавь ссылку в конце.

Сырой ответ: {raw_result}

Твой ответ (короткий, со ссылкой): {link_text if link_text else ""}
"""
                packed_response = deepseek.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "system", "content": packed_prompt}],
                    temperature=0.85,
                    max_tokens=150,
                    timeout=20
                )
                packed = packed_response.choices[0].message.content
                
                if link_text and "http" not in packed:
                    packed += f"\n\n{link_text}"
                
                await send_message(user_id, packed)
                save_message(user_id, "assistant", packed)
            else:
                await send_message(user_id, "Не нашёл.")
            return JSONResponse({"ok": True})

        if any(word in text.lower() for word in search_triggers + analyze_triggers + reason_triggers):
            if any(word in text.lower() for word in reason_triggers):
                agent_id = AGENT_REASONING_ID
            elif any(word in text.lower() for word in analyze_triggers):
                agent_id = AGENT_RESEARCH_ID
            else:
                agent_id = AGENT_SEARCH_ID

            try:
                raw_result = call_yandex_agent(agent_id, text, user_name, user_city, budget)
                if raw_result:
                    packed_prompt = f"""
Ты — AURA. Отвечай коротко — 2-3 предложения.

Сырой ответ: {raw_result}

Твой ответ (короткий):
"""
                    packed_response = deepseek.chat.completions.create(
                        model="deepseek-chat",
                        messages=[{"role": "system", "content": packed_prompt}],
                        temperature=0.85,
                        max_tokens=150,
                        timeout=20
                    )
                    packed = packed_response.choices[0].message.content
                    await send_message(user_id, packed)
                    save_message(user_id, "assistant", packed)
                    return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка агента Яндекса: {e}")

            result = await search_organization(text, get_fact(user_id, "city") or "Белово")
            if result and "error" not in result:
                reply = f"🏥 **{result['name']}**\n📍 {result['address']}\n📞 {', '.join(result['phones'][:3])}\n🌐 [Сайт]({result['site']})"
                await send_message(user_id, reply)
                save_message(user_id, "assistant", reply)
            else:
                await send_message(user_id, "Не нашёл.")
            return JSONResponse({"ok": True})

        # ============================================================
        # ОБЫЧНЫЙ ДИАЛОГ
        # ============================================================
        result = await deepseek_interview(user_id, text, history)
        reply = result.get("reply", "😅 Не понял.")
        save_message(user_id, "assistant", reply)
        await send_typing(user_id)
        await send_message(user_id, reply)
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
