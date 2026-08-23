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

# ПАРОЛЬ ДЛЯ ВХОДА
ACCESS_PASSWORD = "12355"

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

# ============================================================
# ПРОВЕРКА ПАРОЛЯ
# ============================================================
def is_user_authorized(user_id: int) -> bool:
    """Проверяет, есть ли пользователь в таблице users"""
    if not supabase:
        return False
    try:
        res = supabase.table("users").select("user_id").eq("user_id", user_id).execute()
        return len(res.data) > 0
    except Exception as e:
        logger.error(f"❌ Ошибка проверки пользователя: {e}")
        return False

def authorize_user(user_id: int) -> bool:
    """Сохраняет пользователя в таблицу users"""
    if not supabase:
        return False
    try:
        supabase.table("users").insert({
            "user_id": user_id,
            "authorized_at": datetime.now().isoformat()
        }).execute()
        logger.info(f"✅ Пользователь {user_id} авторизован")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка авторизации: {e}")
        return False

# ============================================================
# ЗАЩИТА ОТ ПОВТОРОВ
# ============================================================
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
# КЕШИРОВАНИЕ (24 ЧАСА)
# ============================================================
agent_cache = {}

def get_cached_response(hash_val):
    if hash_val in agent_cache:
        entry = agent_cache[hash_val]
        if datetime.now() - entry["timestamp"] < timedelta(hours=24):
            logger.info("⚡ Ответ из кеша (24ч)")
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
        if res.data:
            history = list(reversed(res.data))
            logger.info(f"📚 Загружено {len(history)} сообщений")
            return history
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
    logger.info(f"📌 Извлечено {len(parts)} сообщений")
    return result

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
# РАСПОЗНАВАНИЕ КАРТИНОК
# ============================================================
async def recognize_image_with_deepseek(image_url: str) -> str:
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code != 200:
            return "⚠️ Не удалось загрузить изображение."
        image_base64 = base64.b64encode(response.content).decode('utf-8')
        prompt = "Опиши картинку коротко, 2-3 предложения. Что видишь? Без лишней воды."
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
            max_tokens=100,
            temperature=0.5
        )
        result = response.choices[0].message.content
        if not result or len(result.strip()) < 5:
            return "На картинке не удалось ничего распознать."
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка распознавания: {e}")
        return "⚠️ Не удалось распознать изображение."

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
            parts.append(f"кухня: {cuisine}")
        if parts:
            portrait_context = "ПОРТРЕТ: " + ", ".join(parts) + "."

    found = find_in_history(history, text)
    facts = extract_facts(found, text) if found else None

    if facts:
        context = f"В ИСТОРИИ:\n{facts}"
    else:
        context = "В ИСТОРИИ НИЧЕГО НЕ НАЙДЕНО."

    prompt = f"""Ты — AURA. Отвечай коротко — 2-3 предложения. Без приветствий.

{portrait_context}

{context}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ (2-3 предложения):"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=120,
            timeout=20
        )
        reply = response.choices[0].message.content
        logger.info(f"✅ DeepSeek ответил")
        return {"reply": reply, "found_in_history": bool(facts)}
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return {"reply": "😅 Не понял.", "found_in_history": False}

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
        # ПРОВЕРКА ПАРОЛЯ (ДЛЯ ВСЕХ, КРОМЕ АДМИНА)
        # ============================================================
        if str(user_id) != ADMIN_CHAT_ID:
            if not is_user_authorized(user_id):
                # Если пользователь уже начал вводить пароль
                if user_id in user_states and user_states[user_id].get("state") == "entering_password":
                    if text == ACCESS_PASSWORD:
                        if authorize_user(user_id):
                            await send_message(user_id, "✅ Доступ разрешён! Добро пожаловать в AURA. Чем могу помочь?")
                            del user_states[user_id]
                            return JSONResponse({"ok": True})
                        else:
                            await send_message(user_id, "❌ Ошибка авторизации. Попробуй позже.")
                            del user_states[user_id]
                            return JSONResponse({"ok": True})
                    else:
                        await send_message(user_id, "❌ Неверный пароль. Попробуй ещё раз или обратись к администратору.")
                        return JSONResponse({"ok": True})
                
                # Просим пароль
                await send_message(user_id, "🔐 Для доступа к AURA нужен пароль. Введите пароль для входа:")
                user_states[user_id] = {"state": "entering_password"}
                return JSONResponse({"ok": True})

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
                        await send_message(user_id, "⚠️ Не удалось распознать голос.")
                        return JSONResponse({"ok": True})
                else:
                    await send_message(user_id, "⚠️ Ошибка загрузки голосового сообщения.")
                    return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса.")
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
        time_phrases = ["сколько время", "который час", "точное время", "часы покажи", "время сейчас"]
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
            await send_message(user_id, "Привет. Я AURA. Напиши, что нужно.")
            save_message(user_id, "assistant", "Привет. Я AURA.")
            return JSONResponse({"ok": True})

        if text.lower() in ["/clear", "/reset", "сброс", "забудь"]:
            clear_user_history(user_id)
            await send_message(user_id, "✅ История очищена.")
            return JSONResponse({"ok": True})

        # ============================================================
        # ОБРАБОТКА ЗАЯВОК
        # ============================================================
        if any(word in text.lower() for word in ["заявк", "хочу сотрудничать", "свяжитесь"]):
            await send_message(user_id, "📩 Отлично! Как вас зовут?")
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
            await send_message(user_id, f"✅ Спасибо, {name}! Ваша заявка принята. С вами свяжутся.")
            del user_states[user_id]
            return JSONResponse({"ok": True})

        # ============================================================
        # ОСНОВНАЯ ЛОГИКА
        # ============================================================
        save_message(user_id, "user", text)

        history = get_recent_history(user_id, limit=20)
        logger.info(f"📚 Загружено {len(history)} сообщений")

        # ============================================================
        # ЗНАКОМСТВО — ИМЯ
        # ============================================================
        if not get_fact(user_id, "name"):
            name = None
            if "меня зовут" in text.lower():
                name = text.lower().split("меня зовут")[-1].strip().split()[0]
            elif "зовут" in text.lower():
                name = text.lower().split("зовут")[-1].strip().split()[0]
            elif "моё имя" in text.lower():
                name = text.lower().split("моё имя")[-1].strip().split()[0]
            
            if name and len(name) > 1 and name[0].isupper():
                save_fact(user_id, "name", name)
                save_portrait_field(user_id, "name", name)
                await send_typing(user_id)
                await send_message(user_id, f"Приятно познакомиться, {name}! ✈️")
                await send_typing(user_id)
                await send_message(user_id, "А в каком городе ты живёшь? 😊")
                return JSONResponse({"ok": True})
            else:
                result = await deepseek_interview(user_id, text, history)
                reply = result.get("reply", "😅 Не понял.")
                save_message(user_id, "assistant", reply)
                await send_typing(user_id)
                await send_message(user_id, reply)
                return JSONResponse({"ok": True})

        # ============================================================
        # ЗНАКОМСТВО — ГОРОД
        # ============================================================
        if not get_fact(user_id, "city"):
            city = None
            if "живу в" in text.lower():
                city = text.lower().split("живу в")[-1].strip().split()[0]
            elif "город " in text.lower():
                city = text.lower().split("город ")[-1].strip().split()[0]
            
            if city and len(city) > 1 and city[0].isupper():
                save_fact(user_id, "city", city)
                save_portrait_field(user_id, "city", city)
                user_name = get_fact(user_id, "name") or "Гость"
                await send_typing(user_id)
                await send_message(user_id, f"Отлично, {user_name}! Теперь я буду давать информацию по твоему городу. 🏙️")
                return JSONResponse({"ok": True})
            else:
                result = await deepseek_interview(user_id, text, history)
                reply = result.get("reply", "😅 Не понял.")
                save_message(user_id, "assistant", reply)
                await send_typing(user_id)
                await send_message(user_id, reply)
                return JSONResponse({"ok": True})

        # ============================================================
        # ПОИСК — СНАЧАЛА ИСТОРИЯ, ПОТОМ ИНТЕРНЕТ
        # ============================================================
        user_name = get_fact(user_id, "name") or "Гость"
        user_city = get_fact(user_id, "city") or "Москва"
        budget = get_fact(user_id, "budget_travel") or ""

        # 1. Проверяем историю
        result = await deepseek_interview(user_id, text, history)
        reply = result.get("reply", "😅 Не понял.")
        found_in_history = result.get("found_in_history", False)

        if found_in_history:
            save_message(user_id, "assistant", reply)
            await send_typing(user_id)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # 2. В истории нет — идём в интернет
        await send_typing(user_id)

        if any(word in text.lower() for word in ["посоветуй", "что лучше", "стоит ли"]):
            agent_id = AGENT_REASONING_ID
        elif any(word in text.lower() for word in ["сравни", "проанализируй", "исследуй"]):
            agent_id = AGENT_RESEARCH_ID
        else:
            agent_id = AGENT_SEARCH_ID

        try:
            raw_result = call_yandex_agent(agent_id, text, user_name, user_city, budget)
            if raw_result:
                link_text = ""
                if "фильм" in text.lower() or "кино" in text.lower():
                    link_text = f"🔗 [Смотреть на Яндекс.Видео](https://yandex.ru/video/search?text={text.replace(' ', '+')})"
                elif "рецепт" in text.lower():
                    link_text = f"🔗 [Смотреть на YouTube](https://www.youtube.com/results?search_query={text.replace(' ', '+')})"
                elif "билет" in text.lower() or "рейс" in text.lower():
                    link_text = f"✈️ [Найти билеты на Aviasales](https://www.aviasales.ru/search?q={text.replace(' ', '+')})"
                
                packed_prompt = f"""
Ты — AURA. Отвечай коротко — 2 предложения.

Сырой ответ: {raw_result}

Твой ответ:"""
                packed_response = deepseek.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "system", "content": packed_prompt}],
                    temperature=0.85,
                    max_tokens=120,
                    timeout=20
                )
                packed = packed_response.choices[0].message.content
                
                if link_text and "http" not in packed:
                    packed += f"\n\n{link_text}"
                
                await send_message(user_id, packed)
                save_message(user_id, "assistant", packed)
            else:
                await send_message(user_id, "Не нашёл в интернете. Попробуй уточнить.")
        except Exception as e:
            logger.error(f"❌ Ошибка поиска: {e}")
            await send_message(user_id, "Ошибка поиска. Попробуй позже.")
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
