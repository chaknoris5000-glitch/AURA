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

# ============================================================
# ЗАЩИТА ОТ ПОВТОРНЫХ ЗАПРОСОВ
# ============================================================
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

# ============================================================
# КЕШИРОВАНИЕ
# ============================================================
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

# ============================================================
# INVITE-КОДЫ (VIP-ДОСТУП)
# ============================================================
def check_invite_code(user_id: int, code: str) -> bool:
    """Проверяет и активирует инвайт-код"""
    if not supabase:
        return False
    try:
        # Ищем код в базе
        res = supabase.table("invite_codes") \
            .select("user_id") \
            .eq("code", code) \
            .execute()
        
        if not res.data:
            return False
        
        # Если код уже привязан к другому user_id - блокируем
        if res.data[0]["user_id"] and res.data[0]["user_id"] != str(user_id):
            return False
        
        # Активируем код для этого пользователя
        supabase.table("invite_codes") \
            .update({"user_id": str(user_id), "activated_at": datetime.now().isoformat()}) \
            .eq("code", code) \
            .execute()
        
        logger.info(f"✅ Код {code} активирован для {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка проверки кода: {e}")
        return False

def is_vip_user(user_id: int) -> bool:
    """Проверяет, есть ли у пользователя активный код"""
    if not supabase:
        return False
    try:
        res = supabase.table("invite_codes") \
            .select("user_id") \
            .eq("user_id", str(user_id)) \
            .execute()
        return bool(res.data)
    except Exception as e:
        logger.error(f"❌ Ошибка проверки VIP: {e}")
        return False

# ============================================================
# ПАМЯТЬ И ПОРТРЕТ
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
# РАСПОЗНАВАНИЕ ИЗОБРАЖЕНИЙ
# ============================================================
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
            return "😊 Текст на изображении не найден."
    except Exception as e:
        logger.error(f"❌ Ошибка распознавания: {e}")
        return "⚠️ Ошибка при распознавании изображения."

# ============================================================
# ВЫЗОВ АГЕНТОВ ЯНДЕКСА
# ============================================================
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
        )
        result = response.output_text
        cache_response(hash_val, result)
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка агента Яндекса ({agent_id}): {e}")
        return ""

# ============================================================
# ЭЛИТНОЕ ФОРМАТИРОВАНИЕ ОТВЕТА
# ============================================================
async def pack_response(raw_text: str, user_name: str = "", user_city: str = "") -> str:
    try:
        prompt = f"""
Ты — элитный цифровой ассистент для занятых людей с высоким статусом.

Твой стиль общения:
- Безупречно вежливый, дипломатичный, уважительный. Всегда на «Вы».
- Без иронии, сарказма или панибратства.
- Задача — экономить время и давать идеальные решения.

Правила форматирования ответа:
1. Начинай с обращения по имени: "✦ [Имя], ..."
2. Структура: краткое резюме, затем детали.
3. Используй маркеры: ▸ для пунктов, ◈ для итогов.
4. Выделяй важное **жирным шрифтом**.
5. В конце обязательно задавай уточняющий вопрос.
6. Никаких таблиц, только текст с иконками.

Имя пользователя: {user_name or "Гость"}
Город: {user_city or "не указан"}

Сырой ответ для обработки:
{raw_text}

Твой отформатированный ответ:
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7,
            max_tokens=400,
            timeout=20
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка упаковки: {e}")
        return raw_text

# ============================================================
# ОПРЕДЕЛЕНИЕ ПЛАТФОРМЫ ДЛЯ КОНТЕНТА
# ============================================================
def detect_content_platform(text: str) -> dict:
    try:
        prompt = f"""
Определи, где лучше искать контент по запросу пользователя: "{text}"
Верни JSON: {{"platform": "yandex_video|youtube|yandex_images", "search_query": "уточнённый запрос"}}
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

def clear_user_history(user_id):
    if not supabase:
        return
    try:
        supabase.table("history").delete().eq("user_id", user_id).execute()
        logger.info(f"🧹 История очищена для {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка очистки истории: {e}")

# ============================================================
# ЭМОЦИИ
# ============================================================
async def detect_emotion(text: str) -> dict:
    try:
        prompt = f"""
Проанализируй эмоцию в сообщении: "{text}"
Верни JSON: {{"emotion": "спокойствие|радость|грусть", "confidence": 0.0-1.0}}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=100,
            timeout=10
        )
        return json.loads(response.choices[0].message.content)
    except:
        return {"emotion": "спокойствие", "confidence": 0.5}

# ============================================================
# 2ГИС
# ============================================================
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ TELEGRAM
# ============================================================
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

# ============================================================
# ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ (VIP-ВЕРСИЯ)
# ============================================================
async def process_vip_request(user_id: int, text: str) -> str:
    """Обработка запроса VIP-пользователя"""
    history = get_recent_history(user_id, limit=20)
    user_name = get_fact(user_id, "name") or "Гость"
    user_city = get_fact(user_id, "city") or "Москва"
    portrait = get_portrait(user_id)
    
    # Собираем контекст
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
Ты — элитный цифровой ассистент.

Твой стиль:
- Безупречно вежливый, дипломатичный, уважительный. Всегда на «Вы».
- Без иронии, сарказма или панибратства.
- Твоя задача — экономить время и давать идеальные решения.

Имя пользователя: {user_name}
Город: {user_city}

{portrait_context}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ (кратко, структурированно, с уважением, используй маркеры ▸ и ◈):
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7,
            max_tokens=400,
            timeout=20
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return "Извините, произошла ошибка. Попробуйте перефразировать запрос."

# ============================================================
# WEBHOOK (ОСНОВНАЯ ТОЧКА ВХОДА)
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
        # 1. ОБРАБОТКА МЕДИА (ФОТО/ГОЛОС)
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
                    recognized_text = recognize_image(image_url)
                    await send_message(user_id, f"📷 **Распознанный текст:**\n\n{recognized_text}")
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
        
        # ============================================================
        # 2. ЗАЩИТА ОТ ПОВТОРОВ
        # ============================================================
        if is_duplicate(user_id, text):
            logger.warning(f"⚠️ Повторный запрос от {user_id}")
            return JSONResponse({"ok": True})
        
        # ============================================================
        # 3. КОМАНДЫ
        # ============================================================
        if text == "/start":
            await send_message(user_id, "✦ **Добро пожаловать в закрытый клуб AURA.**\n\nДля доступа введите ваш персональный инвайт-код.\n\n_Если у вас нет кода — свяжитесь с вашим персональным куратором._")
            return JSONResponse({"ok": True})
        
        if text.lower() in ["/clear", "/reset", "сброс"]:
            clear_user_history(user_id)
            await send_message(user_id, "✅ История очищена.")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/subscribe":
            await send_message(user_id, """
✦ **Закрытый клуб AURA**

Доступ к элитному цифровому ассистенту.

**Тариф:**
▸ **150 000 ₽ / месяц**
▸ Полный доступ ко всем функциям
▸ Приоритетная обработка запросов
▸ Персональный куратор

◈ Оплата производится по счёту.
Для оформления — напишите администратору.
""")
            return JSONResponse({"ok": True})
        
        # ============================================================
        # 4. ПРОВЕРКА VIP-СТАТУСА
        # ============================================================
        if is_vip_user(user_id):
            # === VIP-ПОЛЬЗОВАТЕЛЬ ===
            # Сохраняем сообщение
            save_message(user_id, "user", text)
            
            # Время
            if any(phrase in text.lower() for phrase in ["сколько время", "который час", "время сейчас"]):
                user_city = get_fact(user_id, "city") or "Москва"
                await send_typing(user_id)
                await send_message(user_id, f"✦ Сейчас **{get_time_for_city(user_city)}** по местному времени ({user_city}).")
                return JSONResponse({"ok": True})
            
            # Поиск контента
            content_triggers = ["фильм", "сериал", "видео", "рецепт", "картинки", "фото", "обзор", "смотреть", "клип", "трейлер"]
            search_triggers = ["найди", "поищи", "цены", "билеты", "скидки", "новости", "погода", "курс", "стоимость"]
            analyze_triggers = ["сравни", "проанализируй", "исследуй", "изучи", "разбери"]
            reason_triggers = ["посоветуй", "что лучше", "как поступить", "выбери", "рекомендуй", "стоит ли"]
            
            user_name = get_fact(user_id, "name") or "Гость"
            user_city = get_fact(user_id, "city") or "Москва"
            
            await send_typing(user_id)
            
            # Контент
            if any(word in text.lower() for word in content_triggers):
                platform_info = detect_content_platform(text)
                platform = platform_info.get("platform", "yandex_video")
                search_query = platform_info.get("search_query", text)
                platform_map = {"yandex_video": "яндекс видео", "youtube": "youtube", "yandex_images": "яндекс картинки"}
                full_query = f"{search_query} {platform_map.get(platform, 'яндекс видео')}"
                raw_result = call_yandex_agent(AGENT_SEARCH_ID, full_query, user_name, user_city)
                if raw_result:
                    packed = await pack_response(raw_result, user_name, user_city)
                    await send_message(user_id, packed)
                    save_message(user_id, "assistant", packed)
                else:
                    await send_message(user_id, "✦ К сожалению, ничего не найдено. Попробуйте уточнить запрос.")
                return JSONResponse({"ok": True})
            
            # Поиск/Анализ/Рассуждение
            if any(word in text.lower() for word in search_triggers + analyze_triggers + reason_triggers):
                if any(word in text.lower() for word in reason_triggers):
                    agent_id = AGENT_REASONING_ID
                elif any(word in text.lower() for word in analyze_triggers):
                    agent_id = AGENT_RESEARCH_ID
                else:
                    agent_id = AGENT_SEARCH_ID
                
                try:
                    raw_result = call_yandex_agent(agent_id, text, user_name, user_city)
                    if raw_result:
                        packed = await pack_response(raw_result, user_name, user_city)
                        await send_message(user_id, packed)
                        save_message(user_id, "assistant", packed)
                        return JSONResponse({"ok": True})
                except Exception as e:
                    logger.error(f"❌ Ошибка агента Яндекса: {e}")
                
                # Fallback на 2ГИС
                result = await search_organization(text, get_fact(user_id, "city") or "Москва")
                if result and "error" not in result:
                    reply = f"✦ **{result['name']}**\n\n▸ Адрес: {result['address']}\n▸ Телефон: {', '.join(result['phones'][:3])}\n▸ Сайт: {result['site']}"
                    await send_message(user_id, reply)
                    save_message(user_id, "assistant", reply)
                else:
                    await send_message(user_id, f"✦ По запросу «{text}» ничего не найдено. Попробуйте уточнить.")
                return JSONResponse({"ok": True})
            
            # Обычный диалог
            reply = await process_vip_request(user_id, text)
            save_message(user_id, "assistant", reply)
            await send_typing(user_id)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})
        
        # ============================================================
        # 5. НЕ VIP — ПРОВЕРЯЕМ КОД
        # ============================================================
        else:
            # Если пользователь отправил код (формат AURA-XXXXX)
            if len(text) >= 10 and "AURA" in text.upper():
                if check_invite_code(user_id, text.upper()):
                    await send_message(user_id, "✅ **Код активирован!** Добро пожаловать в закрытый клуб.\n\nЯ — ваш персональный ассистент. Чем могу быть полезен?")
                    save_message(user_id, "assistant", "Код активирован, доступ получен.")
                else:
                    await send_message(user_id, "❌ **Неверный код.** Доступ запрещён.")
                return JSONResponse({"ok": True})
            else:
                await send_message(user_id, "🔒 **Доступ только для участников закрытого клуба.**\n\nВведите инвайт-код для активации.")
                return JSONResponse({"ok": True})
    
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

# ============================================================
# ЗАПУСК
# ============================================================
@app.get("/")
async def root():
    return {"status": "AURA VIP работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
