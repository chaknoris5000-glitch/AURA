import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import os
import requests
import tempfile
import shutil
import threading
import time
import smtplib
import json
import re
import base64
import io as io_lib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.mime.text import MIMEText
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from PIL import Image
from openai import OpenAI

load_dotenv()

# ==========================
# SUPABASE
# ==========================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ НЕТ SUPABASE_URL или SUPABASE_KEY!")

from supabase import create_client, Client

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase подключён!")
    except Exception as e:
        print(f"❌ Ошибка подключения Supabase: {e}")

# ==========================
# КЛЮЧИ
# ==========================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

ADMIN_USERS = ["5818548555"]

print("🔍 Проверка ключей...")
if not DEEPSEEK_API_KEY:
    print("❌ НЕТ КЛЮЧА DEEPSEEK!")
if not TELEGRAM_TOKEN:
    print("❌ НЕТ КЛЮЧА TELEGRAM!")

# ==========================
# TAVILY
# ==========================

tavily_client = None
try:
    from tavily import TavilyClient
    if TAVILY_API_KEY:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        print("✅ Tavily инициализирован")
except ImportError:
    print("⚠️ Tavily не установлен")

# ==========================
# ПАМЯТЬ
# ==========================

USER_MEMORY_CACHE = {}

def load_user_memory(chat_id):
    if not supabase:
        return {"history": [], "topics": [], "user": None}
    
    if chat_id not in USER_MEMORY_CACHE:
        try:
            history_response = supabase.table("history")\
                .select("*")\
                .eq("user_id", chat_id)\
                .order("created_at", desc=False)\
                .limit(500)\
                .execute()
            history = history_response.data if history_response.data else []
            
            topics_response = supabase.table("topics")\
                .select("topic")\
                .eq("user_id", chat_id)\
                .execute()
            topics = [t["topic"] for t in topics_response.data] if topics_response.data else []
            
            user_response = supabase.table("users")\
                .select("*")\
                .eq("user_id", chat_id)\
                .execute()
            user = user_response.data[0] if user_response.data else None
            
            USER_MEMORY_CACHE[chat_id] = {
                "history": history,
                "topics": topics,
                "user": user,
                "last_updated": datetime.now()
            }
            print(f"🧠 Загружена память для {chat_id}: {len(history)} сообщений, {len(topics)} тем")
        except Exception as e:
            print(f"❌ Ошибка загрузки памяти: {e}")
            USER_MEMORY_CACHE[chat_id] = {"history": [], "topics": [], "user": None}
    
    return USER_MEMORY_CACHE[chat_id]

def update_user_memory(chat_id, role, content):
    try:
        if supabase:
            supabase.table("history").insert({
                "user_id": chat_id,
                "role": role,
                "content": content,
                "created_at": datetime.now().isoformat()
            }).execute()
    except Exception as e:
        print(f"❌ Ошибка сохранения в Supabase: {e}")
    
    if chat_id in USER_MEMORY_CACHE:
        USER_MEMORY_CACHE[chat_id]["history"].append({
            "role": role,
            "content": content,
            "created_at": datetime.now().isoformat()
        })
        USER_MEMORY_CACHE[chat_id]["last_updated"] = datetime.now()
    else:
        load_user_memory(chat_id)

def get_full_context(chat_id, limit=500):
    cache = USER_MEMORY_CACHE.get(chat_id)
    if not cache:
        cache = load_user_memory(chat_id)
    
    history = cache["history"][-limit:] if cache["history"] else []
    topics = cache["topics"][:10] if cache["topics"] else []
    
    return {
        "history": history,
        "topics": topics,
        "user": cache.get("user")
    }

def set_bot_description():
    description = """👋Привет! Я — AURA, твой умный помощник! 
🔥Даю тебе - 7 дней бесплатного доступа!"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyDescription"
        data = {"description": description}
        requests.post(url, json=data, timeout=10)
    except:
        pass

set_bot_description()

# ==========================
# ВРЕМЯ
# ==========================

def get_timezone_offset(city_name):
    timezones = {
        "белово": 7, "кемерово": 7, "новокузнецк": 7,
        "прокопьевск": 7, "киселёвск": 7, "междуреченск": 7,
        "москва": 3, "санкт-петербург": 3, "калининград": 2,
        "мурманск": 3, "архангельск": 3, "екатеринбург": 5,
        "челябинск": 5, "тюмень": 5, "новосибирск": 7,
        "омск": 6, "томск": 7, "красноярск": 7,
        "иркутск": 8, "улан-удэ": 8, "чита": 9,
        "владивосток": 10, "хабаровск": 10, "южно-сахалинск": 11,
        "петропавловск-камчатский": 12, "магадан": 11, "анадырь": 12,
        "амстердам": 2
    }
    for city, offset in timezones.items():
        if city in city_name.lower():
            return offset
    return 3

def get_city_by_ip(ip):
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,timezone,offset", timeout=3)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                return {
                    "city": data.get("city", ""),
                    "offset": data.get("offset", 0) // 3600
                }
    except:
        pass
    return None

def get_current_time_for_user(user_id, ip=None):
    city = None
    offset = 3
    
    if supabase:
        try:
            response = supabase.table("users")\
                .select("city")\
                .eq("user_id", user_id)\
                .execute()
            if response.data and response.data[0].get("city"):
                city = response.data[0]["city"]
                offset = get_timezone_offset(city)
                return datetime.utcnow() + timedelta(hours=offset), city
        except:
            pass
    
    if ip and ip not in ["127.0.0.1", "localhost", "::1"]:
        city_data = get_city_by_ip(ip)
        if city_data and city_data.get("city"):
            city = city_data["city"]
            offset = city_data.get("offset", 3)
            if supabase:
                try:
                    supabase.table("users")\
                        .update({"city": city})\
                        .eq("user_id", user_id)\
                        .execute()
                except:
                    pass
            return datetime.utcnow() + timedelta(hours=offset), city
    
    return datetime.utcnow() + timedelta(hours=3), "Москва"

# ==========================
# ПОИСК
# ==========================

async def search_web(query):
    """Поиск через Tavily — только факты, на русском языке"""
    if not tavily_client:
        return None
    
    try:
        search_query = f"{query} на русском языке"
        
        response = tavily_client.search(
            query=search_query,
            search_depth="advanced",
            max_results=3,
            include_answer=True,
            include_images=False
        )
        
        if response.get('answer'):
            answer = response['answer']
            if re.search(r'[а-яА-Я]', answer):
                answer = re.sub(r'http\S+', '', answer)
                answer = re.sub(r'\d{10,}', '', answer)
                return answer[:500]
        
        if response.get('results'):
            for r in response['results'][:3]:
                content = r.get('content', '')[:300]
                if re.search(r'[а-яА-Я]', content):
                    content = re.sub(r'<[^>]+>', '', content)
                    content = re.sub(r'http\S+', '', content)
                    content = re.sub(r'\d{10,}', '', content)
                    return content[:500]
        
        return None
        
    except Exception as e:
        print(f"❌ Tavily ошибка: {e}")
        return None

# ==========================
# ФУНКЦИИ БАЗЫ
# ==========================

def get_user(user_id):
    if not supabase:
        return None
    try:
        response = supabase.table("users")\
            .select("*")\
            .eq("user_id", user_id)\
            .execute()
        return response.data[0] if response.data else None
    except:
        return None

def save_user(user_id, name=None, city=None):
    if not supabase:
        return
    try:
        now = datetime.now().isoformat()
        existing = get_user(user_id)
        if existing:
            supabase.table("users")\
                .update({
                    "name": name or existing.get("name", "Пользователь"),
                    "city": city or existing.get("city")
                })\
                .eq("user_id", user_id)\
                .execute()
        else:
            supabase.table("users").insert({
                "user_id": user_id,
                "name": name or "Пользователь",
                "city": city,
                "trial_start": now,
                "created_at": now
            }).execute()
    except Exception as e:
        print(f"❌ Ошибка сохранения пользователя: {e}")

def update_user_city(user_id, city):
    if not supabase:
        return
    try:
        supabase.table("users")\
            .update({"city": city})\
            .eq("user_id", user_id)\
            .execute()
    except:
        pass

def save_memory(user_id, key, value):
    if not supabase:
        return
    try:
        supabase.table("user_memory")\
            .delete()\
            .eq("user_id", user_id)\
            .eq("key", key)\
            .execute()
        supabase.table("user_memory").insert({
            "user_id": user_id,
            "key": key,
            "value": value,
            "created_at": datetime.now().isoformat()
        }).execute()
    except:
        pass

def get_memory(user_id, key):
    if not supabase:
        return None
    try:
        response = supabase.table("user_memory")\
            .select("value")\
            .eq("user_id", user_id)\
            .eq("key", key)\
            .execute()
        if response.data:
            return response.data[0]["value"]
        return None
    except:
        return None

def save_topic(user_id, topic):
    if not supabase:
        return
    try:
        supabase.table("topics").insert({
            "user_id": user_id,
            "topic": topic,
            "last_mentioned": datetime.now().isoformat()
        }).execute()
    except:
        pass

# ==========================
# AI
# ==========================

async def get_ai_response(messages):
    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL
        )
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.85,
            max_tokens=600,
            presence_penalty=0.1,
            frequency_penalty=0.1
        )
        return response.choices[0].message.content
        
    except Exception as e:
        print(f"❌ AI ошибка: {e}")
        try:
            from groq import Groq
            groq_client = Groq(api_key=GROQ_API_KEY)
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.9,
                max_tokens=400
            )
            print(f"🔄 Переключился на Groq (резерв)")
            return response.choices[0].message.content
        except:
            return "Извини, сейчас проблемы с подключением. Попробуй позже."

# ==========================
# ОСНОВНОЙ БОТ
# ==========================

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        
        if "pre_checkout_query" in body:
            query = body["pre_checkout_query"]
            chat_id = str(query["from"]["id"])
            payload = query["invoice_payload"]
            
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerPreCheckoutQuery"
            data = {"pre_checkout_query_id": query["id"], "ok": True}
            requests.post(url, json=data)
            
            subscription = payload.replace("subscription_", "")
            update_user_subscription(chat_id, subscription)
            save_payment(chat_id, subscription, TARIFFS[subscription]["stars"])
            send_message(chat_id, f"✅ Оплата прошла успешно! Подписка **{TARIFFS[subscription]['name']}** активирована.")
            return JSONResponse({"ok": True})
        
        if "message" not in body:
            return JSONResponse({"ok": False, "error": "No message"})
        
        if "callback_query" in body:
            callback = body["callback_query"]
            chat_id = str(callback["message"]["chat"]["id"])
            data = callback["data"]
            
            if data.startswith("buy_"):
                subscription = data.replace("buy_", "")
                tariff = TARIFFS.get(subscription)
                if tariff:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendInvoice"
                    invoice_data = {
                        "chat_id": chat_id,
                        "title": f"Подписка AURA — {tariff['name']}",
                        "description": "Полный доступ ко всем функциям бота на 30 дней",
                        "payload": f"subscription_{subscription}",
                        "provider_token": "",
                        "currency": "XTR",
                        "prices": [{"label": "Подписка на 30 дней", "amount": tariff["stars"]}],
                        "start_parameter": "aura_sub"
                    }
                    requests.post(url, json=invoice_data)
                return JSONResponse({"ok": True})
            
            elif data == "cancel":
                send_message(chat_id, "❌ Отменено.")
                return JSONResponse({"ok": True})
        
        message = body["message"]
        chat_id = str(message["chat"]["id"])
        text = None
        
        send_typing(chat_id)
        
        if "voice" in message:
            # Обработка голоса (упрощённо)
            file_id = message["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data_resp = file_response.json()
            if file_data_resp.get("ok"):
                file_path = file_data_resp["result"]["file_path"]
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                text = transcribe_audio_with_groq(audio_url)
                if not text:
                    send_message(chat_id, "⚠️ Не удалось распознать голос")
                    return JSONResponse({"ok": True})
        
        elif "photo" in message:
            # Обработка фото (упрощённо)
            photo = message["photo"][-1]
            file_id = photo["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data_resp = file_response.json()
            if file_data_resp.get("ok"):
                file_path = file_data_resp["result"]["file_path"]
                image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                image_response = requests.get(image_url, timeout=30)
                if image_response.status_code == 200:
                    image_data = image_response.content
                    vision_result = describe_image_with_groq(image_data)
                    if vision_result:
                        send_message(chat_id, f"📸 {vision_result}")
                    else:
                        send_message(chat_id, "❌ Не удалось описать фото.")
                    return JSONResponse({"ok": True})
                else:
                    send_message(chat_id, "⚠️ Не удалось загрузить фото")
                    return JSONResponse({"ok": True})
        
        elif "text" in message:
            text = message["text"].strip()
        
        if text:
            if text.startswith("/start"):
                user = get_user(chat_id)
                if not user:
                    save_user(chat_id)
                
                welcome = "👋 Привет! Я AURA — твой умный помощник. Задавай любые вопросы, я здесь, чтобы помочь."
                send_message(chat_id, welcome)
                return JSONResponse({"ok": True})
            
            if text.startswith("/pro"):
                send_message(chat_id, "🧠 Переключился на Pro.")
                return JSONResponse({"ok": True})
            
            if text.startswith("/flash"):
                send_message(chat_id, "⚡ Переключился на Flash.")
                return JSONResponse({"ok": True})
            
            # Обычный диалог
            result = await process_message(request, chat_id, text)
            send_message(chat_id, result["reply"])
                
        return JSONResponse({"ok": True})
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        response = requests.post(url, json=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Отправка: {e}")
        return False

def send_typing(chat_id):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
        data = {"chat_id": chat_id, "action": "typing"}
        requests.post(url, json=data, timeout=3)
    except:
        pass

def transcribe_audio_with_groq(audio_url):
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        response = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name
        with open(tmp_path, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=(tmp_path, file.read()),
                model="whisper-large-v3-turbo",
                language="ru",
                response_format="json"
            )
        os.unlink(tmp_path)
        return transcription.text
    except Exception as e:
        print(f"❌ Groq: {e}")
        return None

def describe_image_with_groq(image_data):
    try:
        import groq
        if isinstance(image_data, bytes):
            img = Image.open(io_lib.BytesIO(image_data))
        else:
            img = Image.open(io_lib.BytesIO(image_data))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        max_size = 1024
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        buffer = io_lib.BytesIO()
        img.save(buffer, format='JPEG', quality=85)
        compressed_data = buffer.getvalue()
        base64_image = base64.b64encode(compressed_data).decode('utf-8')
        client = groq.Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опиши подробно, что ты видишь на этой картинке. Ответ дай на русском."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            temperature=0.3,
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Vision ошибка: {e}")
        return None

# ==========================
# ОСНОВНАЯ ЛОГИКА (ПРОСТАЯ И УМНАЯ)
# ==========================

async def process_message(request: Request, chat_id, text):
    user = get_user(chat_id)
    if not user:
        save_user(chat_id)
    
    load_user_memory(chat_id)
    update_user_memory(chat_id, "user", text)
    
    # Получаем контекст
    context = get_full_context(chat_id, limit=300)
    history = context["history"]
    
    # Определяем город
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "127.0.0.1"
    
    current_time, city = get_current_time_for_user(chat_id, ip)
    time_str = current_time.strftime("%H:%M")
    date_str = current_time.strftime("%d.%m.%Y")
    
    # Сохраняем город, если он упомянут
    city_match = re.search(r'(?:в|время в|времени в|часов в|город)\s+([А-Яа-яЁё\-]+)', text.lower())
    if city_match:
        city_name = city_match.group(1).capitalize()
        update_user_city(chat_id, city_name)
        save_memory(chat_id, "city", city_name)
        print(f"🏙️ Сохранён город: {city_name}")
    
    # ==========================
    # ФОРМИРУЕМ ПРОМПТ
    # ==========================
    
    # История диалога (последние 10 сообщений)
    history_text = ""
    for msg in history[-15:]:
        role = "Пользователь" if msg["role"] == "user" else "AURA"
        history_text += f"{role}: {msg['content']}\n"
    
    system_prompt = f"""Ты — AURA, умный и эмпатичный помощник в Telegram. Ты — мой полный аналог.

ТЫ ПОНИМАЕШЬ КОНТЕКСТ И ПАМЯТЬ:
- Ты видишь всю историю диалога
- Если пользователь спрашивает "а в белово?" — ты знаешь, что речь о времени, и показываешь время для Белово (UTC+7)
- Если пользователь просит "ссылку" — ты помнишь, о чём просили до этого
- Ты не теряешь нить разговора

ТВОЙ СТИЛЬ:
- Отвечай как человек: тепло, прямо, с эмпатией
- Коротко — 2-3 предложения (если не просят подробнее)
- Всегда завершай мысль
- Используй контекст прошлых диалогов
- Не выдумывай факты

ЧТО ТЫ УМЕЕШЬ:
1. Отвечать на вопросы из своих знаний
2. Искать информацию через search_web (если нужно найти что-то в интернете)
3. Показывать время с учётом города
4. Давать ссылки на видео (Яндекс Видео) и картинки (Яндекс Картинки)

СЕЙЧАС:
- Город: {city} (если не указан — Москва)
- Время: {time_str}
- Дата: {date_str}

ПОМНИ:
- Если пользователь спрашивает про время в конкретном городе — покажи время с учётом часового пояса
- Если просит видео/картинки — дай ссылку на Яндекс
- Если просит найти информацию — используй search_web
- ВСЕГДА учитывай контекст предыдущих сообщений

ИСТОРИЯ ДИАЛОГА:
{history_text}

ТЕКУЩИЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {text}

ОТВЕТЬ НА РУССКОМ ЯЗЫКЕ. ЕСЛИ НУЖНО ЧТО-ТО НАЙТИ — ИСПОЛЬЗУЙ SEARCH_WEB."""
    
    # ==========================
    # ПЫТАЕМСЯ ПОЛУЧИТЬ ОТВЕТ ОТ AI
    # ==========================
    
    messages = [{"role": "system", "content": system_prompt}]
    reply = await get_ai_response(messages)
    
    # ==========================
    # ЕСЛИ AI НЕ ОТВЕТИЛ НА ВОПРОС — ПРОБУЕМ ПОИСК
    # ==========================
    
    search_keywords = ["найди", "узнай", "где", "кто", "что такое", "клиника", "сайт", "адрес", "телефон", "контакт"]
    if len(reply) < 30 or any(word in text.lower() for word in search_keywords):
        print(f"🔍 Попытка поиска: {text}")
        search_result = await search_web(text)
        if search_result:
            reply = search_result
    
    # ==========================
    # ЕСЛИ ПРОСЯТ ВИДЕО — ДАЁМ ССЫЛКУ
    # ==========================
    
    if "видео" in text.lower() or "ютуб" in text.lower():
        query = re.sub(r'видео|ютуб|youtube|найди|хочу|покажи', '', text, flags=re.IGNORECASE).strip()
        if not query or len(query) < 3:
            query = "смешные котики"
        reply = f"Вот видео по запросу '{query}': https://yandex.ru/video/search?text={query.replace(' ', '%20')}"
    
    # ==========================
    # ЕСЛИ ПРОСЯТ КАРТИНКИ — ДАЁМ ССЫЛКУ
    # ==========================
    
    if "картинк" in text.lower() or "рисунк" in text.lower() or "фото" in text.lower():
        query = re.sub(r'картинк|рисунк|фото|найди|хочу|покажи', '', text, flags=re.IGNORECASE).strip()
        if not query or len(query) < 3:
            query = "красивые картинки"
        reply = f"Вот картинки по запросу '{query}': https://yandex.ru/images/search?text={query.replace(' ', '%20')}"
    
    # ==========================
    # ЕСЛИ ПРОСЯТ ВРЕМЯ — ПОКАЗЫВАЕМ
    # ==========================
    
    if any(word in text.lower() for word in ["время", "который час", "сколько времени", "сколько сейчас"]):
        city_match = re.search(r'(?:в|время в|времени в|часов в|город)\s+([А-Яа-яЁё\-]+)', text.lower())
        if city_match:
            city_name = city_match.group(1).capitalize()
            current_time, _ = get_current_time_for_user(chat_id, ip)
            time_str = current_time.strftime("%H:%M")
            date_str = current_time.strftime("%d.%m.%Y")
            reply = f"🕐 Сейчас {time_str} {date_str} (город: {city_name})"
        else:
            current_time, city = get_current_time_for_user(chat_id, ip)
            time_str = current_time.strftime("%H:%M")
            date_str = current_time.strftime("%d.%m.%Y")
            reply = f"🕐 Сейчас {time_str} {date_str} (город: {city})"
    
    # Сохраняем ответ в память
    update_user_memory(chat_id, "assistant", reply)
    return {"reply": reply}

# ==========================
# ЗАПУСК
# ==========================

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
