import os
import json
import httpx
import asyncio
import logging
import tempfile
import hashlib
import base64
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
import re

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
YANDEX_VISION_API_KEY = os.getenv("YANDEX_VISION_API_KEY")

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

VIP_CODES = ["AURA-001", "AURA-002", "AURA-003", "ADMIN", "TEST"]

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

def get_portrait(user_id):
    if not supabase:
        return None
    try:
        res = supabase.table("user_portrait").select("*").eq("user_id", user_id).execute()
        return res.data[0] if res.data else None
    except:
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
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения портрета: {e}")

def update_client_status(user_id, action):
    if not supabase:
        return
    try:
        existing = supabase.table("client_status").select("user_id").eq("user_id", str(user_id)).execute()
        if existing.data:
            supabase.table("client_status").update({
                "last_active": datetime.now().isoformat(),
                "action": action
            }).eq("user_id", str(user_id)).execute()
        else:
            supabase.table("client_status").insert({
                "user_id": str(user_id),
                "last_active": datetime.now().isoformat(),
                "action": action
            }).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка обновления статуса: {e}")

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

def recognize_image(image_url: str) -> str:
    if not YANDEX_VISION_API_KEY:
        return "⚠️ Ключ Vision OCR не настроен."
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code != 200:
            return "⚠️ Не удалось загрузить изображение."
        image_base64 = base64.b64encode(response.content).decode('utf-8')
        url = "https://vision.api.cloud.yandex.net/v1/ocr"
        headers = {
            "Authorization": f"Api-Key {YANDEX_VISION_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "folderId": YANDEX_FOLDER_ID,
            "image": {"content": image_base64},
            "language": "ru"
        }
        result = requests.post(url, json=payload, headers=headers, timeout=30)
        if result.status_code != 200:
            return f"⚠️ Ошибка распознавания: {result.status_code}"
        data = result.json()
        text_blocks = []
        for page in data.get("pages", []):
            for block in page.get("blocks", []):
                for word in block.get("words", []):
                    text_blocks.append(word.get("text", ""))
        if text_blocks:
            return " ".join(text_blocks)
        else:
            return "Текст на изображении не найден."
    except Exception as e:
        logger.error(f"❌ Ошибка распознавания: {e}")
        return "⚠️ Ошибка при распознавании изображения."

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
            prompt={"id": agent_id, "variables": variables},
            input=user_text,
            tools=[{"type": "web_search", "filters": {"allowed_domains": []}, "search_context_size": "low"}],
            timeout=5.0
        )
        result = response.output_text
        cache_response(hash_val, result)
        return result
    except Exception as e:
        logger.error(f"❌ Таймаут/ошибка агента Яндекса ({agent_id}): {e}")
        return ""

def generate_link(query: str, link_type: str) -> str:
    """Генерирует ссылку на основе типа запроса"""
    encoded_query = requests.utils.quote(query)
    
    if link_type == "tickets":
        return f"https://travel.yandex.ru/search/?text={encoded_query}"
    elif link_type == "hotels":
        return f"https://travel.yandex.ru/hotels/?text={encoded_query}"
    elif link_type == "video":
        return f"https://yandex.ru/video/search?text={encoded_query}"
    elif link_type == "youtube":
        return f"https://www.youtube.com/results?search_query={encoded_query}"
    elif link_type == "rutube":
        return f"https://rutube.ru/search/?q={encoded_query}"
    elif link_type == "wildberries":
        return f"https://www.wildberries.ru/catalog?search={encoded_query}"
    elif link_type == "2gis":
        return f"https://2gis.ru/search/{encoded_query}"
    else:
        return f"https://yandex.ru/search/?text={encoded_query}"

def detect_link_type(text: str) -> str:
    """Определяет, какую ссылку дать"""
    text_lower = text.lower()
    
    if any(word in text_lower for word in ["билет", "рейс", "самолёт", "поезд", "лететь"]):
        return "tickets"
    elif any(word in text_lower for word in ["отель", "гостиница", "жильё", "апартамент"]):
        return "hotels"
    elif any(word in text_lower for word in ["фильм", "сериал", "кино", "мульт", "трейлер"]):
        return "video"
    elif any(word in text_lower for word in ["рецепт", "приготовить", "блюдо", "салат", "суп"]):
        return "youtube"
    elif any(word in text_lower for word in ["купить", "товар", "вещь", "одежда", "обувь"]):
        return "wildberries"
    elif any(word in text_lower for word in ["ресторан", "кафе", "бар", "столовая"]):
        return "2gis"
    else:
        return "yandex"

async def pack_response(raw_text: str, user_name: str = "", user_city: str = "", user_query: str = "") -> str:
    try:
        link_type = detect_link_type(user_query)
        link = generate_link(user_query, link_type)
        
        prompt = f"""
Ты — AURA, живой помощник. Отвечай коротко, с душой и уместными эмодзи.

ПРАВИЛА:
1. **Максимум 3-4 предложения.** Только по делу.
2. Цифры, даты, цены, адреса — обязательно.
3. Используй эмодзи для настроения (✈️ 🚂 🏨 🍽️ 🎯 😊 🔥 💎).
4. **В КОНЦЕ ОБЯЗАТЕЛЬНО ДАЙ ССЫЛКУ** на найденное: {link}
5. Подпись к ссылке: "🔗 Подробнее: [Ссылка]({link})"
6. **ЗАПРЕЩЕНО** предлагать фото, картинки, галереи.
7. В конце — один короткий вопрос с эмодзи.

Сырой ответ: {raw_text}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7,
            max_tokens=180,
            timeout=10
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка упаковки: {e}")
        return f"{raw_text[:200]}\n\n🔗 Подробнее: [Ссылка]({generate_link(user_query, 'yandex')})"

def get_main_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📅 Билеты", "callback_data": "tickets"},
                {"text": "🍽️ Рестораны", "callback_data": "restaurants"}
            ],
            [
                {"text": "🏨 Отели", "callback_data": "hotels"},
                {"text": "📊 Аналитика", "callback_data": "analytics"}
            ],
            [
                {"text": "💳 Оплатить", "callback_data": "pay"},
                {"text": "❓ Помощь", "callback_data": "help"}
            ]
        ]
    }

async def send_message_with_buttons(chat_id, text, keyboard=None):
    if not text:
        text = "Извините, я не смог обработать ваш запрос."
    if len(text) > 4096:
        text = text[:4093] + "..."
    if keyboard is None:
        keyboard = get_main_keyboard()
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard
        }
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
            timeout=30
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки с кнопками: {e}")

async def send_message(chat_id, text):
    if not text:
        text = "Извините, я не смог обработать ваш запрос."
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

async def handle_callback(callback_data, user_id, message_id):
    try:
        if callback_data == "tickets":
            await send_message(user_id, "Напишите город отправления, город прибытия и дату.")
        elif callback_data == "restaurants":
            await send_message(user_id, "Напишите город и тип кухни.")
        elif callback_data == "hotels":
            await send_message(user_id, "Напишите город, даты заезда и выезда.")
        elif callback_data == "analytics":
            await send_message(user_id, "Напишите, что именно проанализировать (рынок, цены, конкурентов).")
        elif callback_data == "pay":
            await send_message(user_id, """
💳 **Оплата подписки AURA**

Стоимость: **150 000 ₽ / месяц**

Ссылка для оплаты через Telegram Stars:
[Оплатить](https://t.me/AuraMegaBot?start=pay)

После оплаты ваш код будет активирован автоматически.
""")
        elif callback_data == "help":
            await send_message_with_buttons(user_id, """
❓ **Помощь**

Я умею:
▸ Искать билеты, рестораны, отели
▸ Анализировать рынок и цены
▸ Давать короткие ответы с душой

Просто напишите, что нужно — и я помогу.
""", get_main_keyboard())
        
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
            json={"chat_id": user_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            timeout=10
        )
    except Exception as e:
        logger.error(f"❌ Ошибка обработки callback: {e}")

def detect_content_platform(text: str) -> dict:
    try:
        prompt = f"""
Определи, где лучше искать контент по запросу пользователя: "{text}"
Верни JSON: {{"platform": "yandex_video|youtube|rutube|yandex_images", "search_query": "уточнённый запрос"}}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=100,
            timeout=10
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"❌ Ошибка определения платформы: {e}")
        return {"platform": "yandex_video", "search_query": text}

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

def clear_user_history(user_id):
    if not supabase:
        return
    try:
        supabase.table("history").delete().eq("user_id", user_id).execute()
        logger.info(f"🧹 История очищена для {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка очистки истории: {e}")

async def search_organization(query: str, city: str = "Москва") -> dict:
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
            response = await client.get(url, params=params, timeout=5)
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

async def check_inactive_users():
    while True:
        await asyncio.sleep(21600)
        try:
            if not supabase:
                continue
            two_days_ago = (datetime.now() - timedelta(days=2)).isoformat()
            res = supabase.table("client_status") \
                .select("user_id") \
                .lt("last_active", two_days_ago) \
                .execute()
            
            for client in res.data:
                user_id = int(client["user_id"])
                await send_message(user_id, """
Доброе утро! ☀️

Я подобрал для вас несколько интересных новостей и предложений по вашим темам.
Хотите посмотреть? Просто напишите, что вас интересует — билеты, рестораны или аналитика.
""")
                logger.info(f"📨 Авто-приветствие отправлено {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка проверки неактивных: {e}")

async def process_vip_request(user_id: int, text: str) -> str:
    history = get_recent_history(user_id, limit=20)
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
    
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-15:]])
    
    prompt = f"""
Ты — AURA, живой и душевный помощник.

ПРАВИЛА:
- 3-4 предложения максимум.
- Только факты: цифры, даты, цены, адреса.
- Добавь эмодзи по смыслу (✈️ 🚂 🏨 🍽️ 🎯 😊 🔥 💎).
- **ЗАПРЕЩЕНО** предлагать фото, картинки, галереи.
- **ЗАПРЕЩЕНО** говорить «посмотрите», «вот фото».
- В конце — один короткий вопрос с эмодзи.

Имя пользователя: {user_name}
Город: {user_city}

{portrait_context}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ:
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7,
            max_tokens=150,
            timeout=15
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return "Извините, произошла ошибка. Попробуйте перефразировать запрос."

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        
        if "callback_query" in body:
            callback = body["callback_query"]
            user_id = callback["from"]["id"]
            callback_data = callback["data"]
            message_id = callback["message"]["message_id"]
            await handle_callback(callback_data, user_id, message_id)
            return JSONResponse({"ok": True})
        
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        msg = body["message"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")
        
        if "photo" in msg or "document" in msg:
            if "photo" in msg:
                file_id = msg["photo"][-1]["file_id"]
            else:
                file_id = msg["document"]["file_id"]
            
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    recognized_text = recognize_image(image_url)
                    await send_message_with_buttons(user_id, f"📷 **Распознанный текст:**\n\n{recognized_text}")
                    save_message(user_id, "assistant", f"Распознан текст: {recognized_text[:200]}...")
                else:
                    await send_message(user_id, "⚠️ Не удалось загрузить изображение.")
            except Exception as e:
                logger.error(f"❌ Ошибка обработки медиа: {e}")
                await send_message(user_id, "⚠️ Не удалось распознать изображение.")
            return JSONResponse({"ok": True})
        
        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    transcribed = transcribe_audio(audio_url)
                    if transcribed:
                        text = transcribed
                        save_message(user_id, "user", text)
                    else:
                        await send_message(user_id, "⚠️ Не удалось распознать голос. Попробуйте ещё раз.")
                        return JSONResponse({"ok": True})
                else:
                    await send_message(user_id, "⚠️ Ошибка загрузки голосового сообщения.")
                    return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса. Попробуйте написать!")
                return JSONResponse({"ok": True})
        
        if not text:
            return JSONResponse({"ok": True})
        
        if is_duplicate(user_id, text):
            logger.warning(f"⚠️ Повторный запрос от {user_id}")
            return JSONResponse({"ok": True})
        
        if text == "/start":
            await send_message_with_buttons(user_id, """
✦ **Добро пожаловать в закрытый клуб AURA.**

Для доступа введите ваш персональный инвайт-код.

_Если у вас нет кода — свяжитесь с вашим персональным куратором._
""")
            return JSONResponse({"ok": True})
        
        if text.lower() in ["/clear", "/reset", "сброс"]:
            clear_user_history(user_id)
            await send_message(user_id, "✅ История очищена.")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/subscribe" or text.lower() == "оплатить":
            await send_message(user_id, """
✦ **Закрытый клуб AURA**

Доступ к элитному цифровому ассистенту.

**Тариф:**
▸ **150 000 ₽ / месяц**
▸ Полный доступ ко всем функциям
▸ Приоритетная обработка запросов
▸ Персональный куратор

◈ Оплата производится по счёту или через Telegram Stars.
""")
            return JSONResponse({"ok": True})
        
        if text.upper() in [code.upper() for code in VIP_CODES]:
            save_fact(user_id, "vip_code", text.upper())
            update_client_status(user_id, "activation")
            await send_message_with_buttons(user_id, """
✅ **Код активирован!** Добро пожаловать в закрытый клуб.

Я — ваш персональный ассистент. Чем могу помочь?
""")
            save_message(user_id, "assistant", "Код активирован, доступ получен.")
            return JSONResponse({"ok": True})
        
        vip_code = get_fact(user_id, "vip_code")
        
        if vip_code:
            update_client_status(user_id, "active")
            save_message(user_id, "user", text)
            
            if any(phrase in text.lower() for phrase in ["сколько время", "который час", "время сейчас"]):
                user_city = get_fact(user_id, "city") or "Москва"
                await send_message_with_buttons(user_id, f"✦ Сейчас **{get_time_for_city(user_city)}** по местному времени ({user_city}).")
                return JSONResponse({"ok": True})
            
            user_name = get_fact(user_id, "name") or "Гость"
            user_city = get_fact(user_id, "city") or "Москва"
            
            search_triggers = ["найди", "поищи", "цены", "билеты", "скидки", "новости", "погода", "курс", "стоимость", "ресторан", "отель", "фильм", "сериал", "рецепт", "купить"]
            
            if any(word in text.lower() for word in search_triggers):
                try:
                    raw_result = call_yandex_agent(AGENT_SEARCH_ID, text, user_name, user_city)
                    if raw_result:
                        packed = await pack_response(raw_result, user_name, user_city, text)
                        await send_message_with_buttons(user_id, packed)
                        save_message(user_id, "assistant", packed)
                        return JSONResponse({"ok": True})
                except Exception as e:
                    logger.error(f"❌ Ошибка агента Яндекса: {e}")
                
                # Если Яндекс не ответил — пробуем 2ГИС или генерируем ссылку сами
                result = await search_organization(text, get_fact(user_id, "city") or "Москва")
                if result and "error" not in result:
                    link_type = detect_link_type(text)
                    link = generate_link(text, link_type)
                    reply = f"✦ **{result['name']}**\n\n▸ Адрес: {result['address']}\n▸ Телефон: {', '.join(result['phones'][:3])}\n▸ Сайт: {result['site']}\n\n🔗 [Подробнее]({link})"
                    await send_message_with_buttons(user_id, reply)
                    save_message(user_id, "assistant", reply)
                else:
                    # Генерируем ссылку самостоятельно
                    link_type = detect_link_type(text)
                    link = generate_link(text, link_type)
                    reply = f"✦ По запросу «{text}» я нашёл информацию.\n\n🔗 [Подробнее]({link})\n\nЧто именно вас интересует? 😊"
                    await send_message_with_buttons(user_id, reply)
                    save_message(user_id, "assistant", reply)
                return JSONResponse({"ok": True})
            
            reply = await process_vip_request(user_id, text)
            save_message(user_id, "assistant", reply)
            await send_message_with_buttons(user_id, reply)
            return JSONResponse({"ok": True})
        
        else:
            await send_message(user_id, "🔒 **Неверный код.** Доступ запрещён.")
            return JSONResponse({"ok": True})
    
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.on_event("startup")
async def startup():
    asyncio.create_task(check_inactive_users())

@app.get("/")
async def root():
    return {"status": "AURA VIP работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
