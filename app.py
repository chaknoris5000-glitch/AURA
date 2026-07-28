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

def search_memory(chat_id, query):
    if not supabase:
        return []
    try:
        response = supabase.table("history")\
            .select("*")\
            .eq("user_id", chat_id)\
            .ilike("content", f"%{query}%")\
            .order("created_at", desc=True)\
            .limit(10)\
            .execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"❌ Ошибка поиска: {e}")
        return []

def set_bot_description():
    description = """👋Привет! Я — AURA, твой умный помощник!"""
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
        "калининград": 2,
        "москва": 3, "санкт-петербург": 3, "мурманск": 3, "архангельск": 3,
        "самара": 4, "саратов": 4, "ижевск": 4,
        "белово": 4, "екатеринбург": 5, "челябинск": 5, "тюмень": 5, "пермь": 5,
        "омск": 6,
        "новосибирск": 7, "томск": 7, "кемерово": 7, "красноярск": 7,
        "новокузнецк": 7, "прокопьевск": 7, "киселёвск": 7, "междуреченск": 7,
        "иркутск": 8,
        "чита": 9, "якутск": 9,
        "владивосток": 10, "хабаровск": 10,
        "южно-сахалинск": 11, "магадан": 11,
        "петропавловск-камчатский": 12, "анадырь": 12
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
# ПРОВЕРКА ССЫЛОК
# ==========================

async def check_url(url):
    """Проверяет, открывается ли сайт"""
    try:
        bad_domains = ['2gis', 'avito', 'yandex', 'prodoctorov', 'diagnocenter', 'orgpage', 'vk.com', 'ok.ru']
        if any(bad in url.lower() for bad in bad_domains):
            return False
        response = requests.head(url, timeout=5, allow_redirects=True)
        if response.status_code == 200:
            return True
        elif response.status_code in [301, 302]:
            return True
        else:
            return False
    except:
        return False

# ==========================
# ПАРСИНГ САЙТОВ
# ==========================

def parse_site_for_info(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9"
        }
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.decompose()
        text = soup.get_text(separator="\n", strip=True)
        result = {}
        
        phone_patterns = [r'\+7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', r'8\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', r'7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}']
        phones = []
        for pattern in phone_patterns:
            phones.extend(re.findall(pattern, text))
        phones = [re.sub(r'\s+', ' ', p).strip() for p in phones]
        phones = list(set(phones))[:5]
        if phones:
            result["phones"] = phones
        
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        emails = list(set(re.findall(email_pattern, text)))[:3]
        if emails:
            result["emails"] = emails
        
        address_pattern = r'(?:ул\.|улица|проспект|пр\.|переулок|пер\.|площадь|пл\.|шоссе|бульвар|Аэродромная)\s+[А-Яа-я0-9\-\.\s,]+'
        addresses = list(set(re.findall(address_pattern, text)))[:3]
        if addresses:
            result["addresses"] = addresses
        
        price_pattern = r'(\d+[\s,.]*\d*)\s*(?:₽|руб|рублей|\$|€)'
        prices = list(set(re.findall(price_pattern, text)))[:5]
        if prices:
            result["prices"] = prices
        
        site_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9\-]+\.(?:ru|рф|com|org|net))'
        sites = list(set(re.findall(site_pattern, text)))[:3]
        if sites:
            result["sites"] = sites
        
        title = soup.find('h1')
        if title:
            result["product_title"] = title.text.strip()
        
        desc = soup.find(class_=re.compile(r'description|about|product-desc|product__description'))
        if desc:
            result["product_description"] = desc.text.strip()[:500]
        
        result["snippet"] = text[:1000].replace("\n", " ")
        return result
    except Exception as e:
        print(f"❌ Ошибка парсинга: {e}")
        return None

# ==========================
# ПОИСК
# ==========================

async def search_web(query):
    results = []
    checked_urls = []
    
    if tavily_client:
        try:
            response = tavily_client.search(
                query=query,
                search_depth="advanced",
                max_results=5,
                include_answer=True,
                include_images=False
            )
            if response.get('answer'):
                results.append(f"💡 {response['answer']}")
            if response.get('results'):
                for r in response['results'][:5]:
                    title = r.get('title', '')
                    url = r.get('url', '')
                    content = r.get('content', '')[:300]
                    if title and url:
                        if await check_url(url):
                            checked_urls.append(url)
                            results.append(f"**{title}**\n{content}...")
        except Exception as e:
            print(f"❌ Tavily: {e}")
    
    if not results:
        try:
            url = f"https://html.duckduckgo.com/html/?q={query}"
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            for result in soup.select('.result')[:3]:
                title = result.select_one('.result__title')
                if title:
                    link = result.select_one('.result__url')
                    snippet = result.select_one('.result__snippet')
                    if snippet and link:
                        url = link.text.strip()
                        if await check_url(url):
                            checked_urls.append(url)
                            results.append(f"**{title.text.strip()}**\n{snippet.text.strip()[:200]}...")
        except Exception as e:
            print(f"❌ DuckDuckGo: {e}")
    
    for url in checked_urls[:3]:
        parsed = parse_site_for_info(url)
        if parsed:
            if parsed.get("phones"):
                results.append(f"📞 Телефоны: {', '.join(parsed['phones'])}")
            if parsed.get("addresses"):
                results.append(f"📍 Адреса: {', '.join(parsed['addresses'])}")
            if parsed.get("emails"):
                results.append(f"✉️ Email: {', '.join(parsed['emails'])}")
            if parsed.get("sites"):
                results.append(f"🌐 Сайты: {', '.join(parsed['sites'])}")
    
    return {
        "text": "\n\n".join(results) if results else None,
        "urls": checked_urls
    }

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

def get_all_topics(user_id):
    if not supabase:
        return []
    try:
        response = supabase.table("topics")\
            .select("topic")\
            .eq("user_id", user_id)\
            .execute()
        return [t["topic"] for t in response.data] if response.data else []
    except:
        return []

def get_message_count(user_id):
    if not supabase:
        return 0
    try:
        response = supabase.table("history")\
            .select("id", count="exact")\
            .eq("user_id", user_id)\
            .execute()
        return response.count if hasattr(response, 'count') else 0
    except:
        return 0

def get_last_message_time(user_id):
    if not supabase:
        return None
    try:
        response = supabase.table("history")\
            .select("created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        if response.data:
            return datetime.fromisoformat(response.data[0]["created_at"].replace("Z", "+00:00"))
        return None
    except:
        return None

# ==========================
# AI
# ==========================

async def get_ai_response(messages, max_tokens=400):
    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL
        )
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.7,
            max_tokens=max_tokens,
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
                temperature=0.7,
                max_tokens=max_tokens
            )
            return response.choices[0].message.content
        except:
            return "Извини, сейчас проблемы с подключением. Попробуй позже."

# ==========================
# УМНЫЙ ПРОМПТ
# ==========================

AURA_PROMPT = """Ты — AURA. Ты — полноценный искусственный интеллект, работающий в Telegram.

ТЫ ПОНИМАЕШЬ:
- Контекст разговора
- Настроение пользователя
- Что скрыто между строк

ТЫ ДУМАЕШЬ:
- Что на самом деле нужно пользователю?
- Как лучше ответить?
- Что сделать, чтобы помочь?

ТЫ ОТВЕЧАЕШЬ:
- Коротко (2-3 предложения)
- Тепло и по-человечески
- Всегда завершаешь мысль
- Задаёшь вопрос в конце: "Что ещё могу сделать?" или "Может, поищем что-то ещё?"

ТЫ НЕ ПРОСТО БОТ — ТЫ ДУМАЮЩИЙ ПОМОЩНИК. Отвечай на русском. Будь самим собой — AURA."""

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
            
            send_message(chat_id, "✅ Оплата прошла успешно!")
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
                        "description": "Полный доступ на 30 дней",
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
        
        if "voice" in message:
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
                
                welcome = "👋 Привет! Я AURA, твой умный помощник. Чем могу помочь?"
                send_message(chat_id, welcome)
                return JSONResponse({"ok": True})
            
            if text.startswith("/pro"):
                USER_MODEL_PREFERENCE[chat_id] = "pro"
                send_message(chat_id, "🧠 Pro режим включён.")
                return JSONResponse({"ok": True})
            
            if text.startswith("/flash"):
                USER_MODEL_PREFERENCE[chat_id] = "flash"
                send_message(chat_id, "⚡ Flash режим включён.")
                return JSONResponse({"ok": True})
            
            if text.startswith("/model"):
                pref = USER_MODEL_PREFERENCE.get(chat_id, "flash")
                send_message(chat_id, f"📊 Текущая модель: {pref.upper()}")
                return JSONResponse({"ok": True})
            
            if text.startswith("/buy"):
                keyboard = [
                    [{"text": "⭐ Собеседник — 50 Stars", "callback_data": "buy_собеседник"}],
                    [{"text": "⭐ Партнёр — 120 Stars", "callback_data": "buy_партнёр"}],
                    [{"text": "⭐ Агент жизни — 250 Stars", "callback_data": "buy_агент_жизни"}],
                    [{"text": "❌ Отмена", "callback_data": "cancel"}]
                ]
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                data = {
                    "chat_id": chat_id,
                    "text": "💳 **Выбери подписку:**\n\n⭐ Собеседник — 50 Stars\n⭐ Партнёр — 120 Stars\n⭐ Агент жизни — 250 Stars",
                    "parse_mode": "Markdown",
                    "reply_markup": json.dumps({"inline_keyboard": keyboard})
                }
                requests.post(url, json=data)
                return JSONResponse({"ok": True})
            
            if text.startswith("/remind"):
                parts = text.split(" ", 3)
                if len(parts) >= 4:
                    date_str = parts[1]
                    time_str = parts[2]
                    reminder_text = parts[3]
                    try:
                        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                        save_reminder(chat_id, reminder_text, dt.isoformat(), chat_id)
                        send_message(chat_id, f"⏰ Напомню в {date_str} {time_str}")
                    except:
                        send_message(chat_id, "❌ Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ")
                else:
                    send_message(chat_id, "❌ Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ")
                return JSONResponse({"ok": True})
            
            if text.startswith("/memory"):
                context = get_full_context(chat_id)
                topics = context["topics"]
                history_count = len(context["history"])
                
                reply = f"🧠 **Память:**\n- Сообщений: {history_count}\n- Тем: {len(topics)}"
                if topics:
                    reply += "\n\n**Темы:**\n" + "\n".join([f"- {t}" for t in topics[:10]])
                send_message(chat_id, reply)
                return JSONResponse({"ok": True})
            
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
                        {"type": "text", "text": "Опиши, что видишь на картинке. Ответ на русском, 2-3 предложения."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            temperature=0.3,
            max_tokens=200
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Vision ошибка: {e}")
        return None

# ==========================
# МОНЕТИЗАЦИЯ
# ==========================

TARIFFS = {
    "собеседник": {"name": "Собеседник", "price": 50, "stars": 50},
    "партнёр": {"name": "Партнёр", "price": 120, "stars": 120},
    "агент_жизни": {"name": "Агент жизни", "price": 250, "stars": 250}
}

TRIAL_DAYS = 7

def get_user_subscription(user_id):
    if not supabase:
        return ("free", None)
    try:
        response = supabase.table("users")\
            .select("subscription, trial_start")\
            .eq("user_id", user_id)\
            .execute()
        if response.data:
            data = response.data[0]
            return (data.get("subscription", "free"), data.get("trial_start"))
        return ("free", None)
    except:
        return ("free", None)

def is_trial_active(trial_start):
    if not trial_start:
        return False
    trial_date = datetime.fromisoformat(trial_start.replace("Z", "+00:00"))
    return datetime.now() - trial_date < timedelta(days=TRIAL_DAYS)

def has_access(user_id):
    if user_id in ADMIN_USERS:
        return True
    subscription, trial_start = get_user_subscription(user_id)
    if is_trial_active(trial_start):
        return True
    if subscription != "free":
        return True
    return False

def save_reminder(user_id, text, remind_time, chat_id):
    if not supabase:
        return
    try:
        supabase.table("reminders").insert({
            "user_id": user_id,
            "text": text,
            "remind_time": remind_time,
            "chat_id": chat_id,
            "status": "pending"
        }).execute()
    except:
        pass

def update_user_subscription(user_id, subscription):
    if not supabase:
        return
    try:
        supabase.table("users")\
            .update({"subscription": subscription})\
            .eq("user_id", user_id)\
            .execute()
    except:
        pass

def save_payment(user_id, subscription, stars):
    if not supabase:
        return
    try:
        supabase.table("payments").insert({
            "user_id": user_id,
            "subscription": subscription,
            "stars": stars,
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }).execute()
    except:
        pass

# ==========================
# ОСНОВНАЯ ЛОГИКА (УМНЫЙ БОТ)
# ==========================

USER_MODEL_PREFERENCE = {}

async def process_message(request: Request, chat_id, text):
    user = get_user(chat_id)
    if not user:
        save_user(chat_id)
    
    load_user_memory(chat_id)
    update_user_memory(chat_id, "user", text)
    
    lower = text.lower()
    
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "127.0.0.1"
    
    current_time, city = get_current_time_for_user(chat_id, ip)
    time_str = current_time.strftime("%H:%M")
    date_str = current_time.strftime("%d.%m.%Y")
    
    city_match = re.search(r'(?:в|время в|времени в|часов в|город)\s+([А-Яа-яЁё\-]+)', lower)
    if city_match:
        city_name = city_match.group(1).capitalize()
        update_user_city(chat_id, city_name)
        save_memory(chat_id, "city", city_name)
        current_time, city = get_current_time_for_user(chat_id, ip)
        time_str = current_time.strftime("%H:%M")
        date_str = current_time.strftime("%d.%m.%Y")
    
    # ==========================
    # АНАЛИЗ НАСТРОЕНИЯ
    # ==========================
    
    sad_words = ["груст", "тоск", "печал", "плач", "больно", "тяжел", "устал", "не могу", "нет сил", "всё плохо", "депресс"]
    happy_words = ["рад", "счаст", "класс", "отличн", "прекрасн", "здоров", "люблю", "ура", "позитив", "супер"]
    
    mood_prefix = ""
    if any(w in lower for w in sad_words):
        mood_prefix = "Пользователь грустный. Отвечай тепло, с поддержкой."
    elif any(w in lower for w in happy_words):
        mood_prefix = "Пользователь в хорошем настроении. Можно с юмором."
    
    # ==========================
    # УМНЫЙ АНАЛИЗ ЗАПРОСА
    # ==========================
    
    # Проверяем, что хочет пользователь
    is_search = any(word in lower for word in ["найди", "поищи", "узнай", "где", "кто", "что такое", "клиника", "сайт", "адрес", "телефон", "контакт", "новости", "погода", "авито", "квартир", "дром", "wildberries", "валдберис", "озон"])
    is_visual = any(word in lower for word in ["картинк", "рисунк", "фото", "видео", "ютуб", "музык", "песн", "клип"])
    is_time = any(word in lower for word in ["время", "который час", "сколько времени", "сколько сейчас"])
    is_memory = any(word in lower for word in ["помнишь", "что мы обсуждали", "о чём мы говорили"])
    
    # ==========================
    # ВРЕМЯ
    # ==========================
    
    if is_time:
        city_match = re.search(r'(?:в|время в|времени в|часов в|город)\s+([А-Яа-яЁё\-]+)', lower)
        if city_match:
            city = city_match.group(1).capitalize()
            update_user_city(chat_id, city)
            save_memory(chat_id, "city", city)
            current_time, city = get_current_time_for_user(chat_id, ip)
            time_str = current_time.strftime("%H:%M")
            date_str = current_time.strftime("%d.%m.%Y")
            reply = f"🕐 {time_str} {date_str} ({city})"
        else:
            current_time, city = get_current_time_for_user(chat_id, ip)
            time_str = current_time.strftime("%H:%M")
            date_str = current_time.strftime("%d.%m.%Y")
            reply = f"🕐 {time_str} {date_str} ({city})"
        update_user_memory(chat_id, "assistant", reply)
        return {"reply": reply}
    
    # ==========================
    # КАРТИНКИ, ВИДЕО, МУЗЫКА (С УМНОЙ ОЧИСТКОЙ)
    # ==========================
    
    if is_visual:
        # Определяем тип
        if any(word in lower for word in ["видео", "ютуб", "клип"]):
            search_type = "video"
            base_url = "https://yandex.ru/video/search?text="
            type_name = "видео"
        elif any(word in lower for word in ["музык", "песн"]):
            search_type = "music"
            base_url = "https://music.yandex.ru/search?text="
            type_name = "музыку"
        else:
            search_type = "image"
            base_url = "https://yandex.ru/images/search?text="
            type_name = "картинки"
        
        # Извлекаем суть
        query = text.lower()
        stop_words = [
            "картинк", "рисунк", "фото", "видео", "ютуб", "музык", "песн", "клип",
            "найди", "хочу", "покажи", "дай", "ссылку", "про", "на", "с", "и", "в", "а",
            "к", "у", "о", "от", "до", "за", "мне", "меня", "посмотреть", "найти"
        ]
        for word in stop_words:
            query = query.replace(word, " ")
        query = " ".join(query.split()).strip()
        
        if not query or len(query) < 3:
            if "котик" in text.lower() or "кот" in text.lower():
                query = "котики"
            elif "соба" in text.lower():
                query = "собаки"
            elif "закат" in text.lower():
                query = "закаты"
            elif "природа" in text.lower():
                query = "природа"
            else:
                query = "красивые картинки"
        
        query = re.sub(r'[^а-яА-Яa-zA-Z0-9 ]', '', query)
        url = base_url + query.replace(" ", "%20")
        
        reply = f"🖼️ Вот {type_name} по запросу '{query}':\n{url}\n\nЧто ещё могу сделать?"
        update_user_memory(chat_id, "assistant", reply)
        return {"reply": reply}
    
    # ==========================
    # ПОИСК САЙТОВ (С ПРОВЕРКОЙ)
    # ==========================
    
    if is_search:
        print(f"🔍 Умный поиск: {text}")
        search_result = await search_web(text)
        
        if search_result and search_result.get("text"):
            clean_text = search_result["text"]
            clean_text = re.sub(r'https?://[^\s]+', '', clean_text)
            clean_text = re.sub(r'\*\*.*?\*\*', '', clean_text)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            if len(clean_text) > 250:
                clean_text = clean_text[:250] + "..."
            
            first_url = search_result["urls"][0] if search_result.get("urls") else None
            
            if first_url:
                reply = f"{clean_text}\n\n🔗 {first_url}"
            else:
                reply = clean_text
        else:
            reply = "Ничего не нашёл. Попробуй переформулировать."
        
        reply += "\n\nЧто ещё могу сделать для тебя?"
        update_user_memory(chat_id, "assistant", reply)
        return {"reply": reply}
    
    # ==========================
    # ПАМЯТЬ
    # ==========================
    
    if is_memory:
        if "помнишь" in lower:
            search_query = re.sub(r'помнишь|ты помнишь|помнишь ли', '', lower).strip()
            if search_query:
                found = search_memory(chat_id, search_query)
                if found:
                    reply = "🧠 " + found[-1]["content"][:200]
                    update_user_memory(chat_id, "assistant", reply)
                    return {"reply": reply}
        
        topics = get_all_topics(chat_id)
        if topics:
            reply = "📚 " + ", ".join(topics[:10])
        else:
            reply = "Пока ничего не обсуждали."
        update_user_memory(chat_id, "assistant", reply)
        return {"reply": reply}
    
    # ==========================
    # ОБЫЧНЫЙ ДИАЛОГ (С МЫШЛЕНИЕМ)
    # ==========================
    
    context = get_full_context(chat_id, limit=300)
    history = context["history"]
    topics = get_all_topics(chat_id)
    
    user_name = get_memory(chat_id, "name")
    likes = get_memory(chat_id, "likes")
    dislikes = get_memory(chat_id, "dislikes")
    
    topics_text = ", ".join(topics[:5]) if topics else ""
    name_context = f"Имя: {user_name}" if user_name else ""
    likes_context = f"Нравится: {likes}" if likes else ""
    dislikes_context = f"Не нравится: {dislikes}" if dislikes else ""
    
    context_text = f"{date_str} {time_str} ({city}). {name_context} {likes_context} {dislikes_context} Темы: {topics_text}."
    
    expand_triggers = ["подробнее", "разверни", "расскажи детальнее", "подробно"]
    if any(word in lower for word in expand_triggers):
        history_limit = 15
        max_tokens = 600
    else:
        history_limit = 5
        max_tokens = 400
    
    messages = [{"role": "system", "content": f"{AURA_PROMPT}\n\n{mood_prefix}"}]
    for msg in history[-history_limit:]:
        messages.append({"role": msg["role"], "content": msg["content"][:500]})
    messages.append({"role": "user", "content": f"{context_text}\n\n{text}"})
    
    reply = await get_ai_response(messages, max_tokens=max_tokens)
    reply = re.sub(r'[*_#~`]', '', reply)
    
    if len(reply) > 800:
        sentences = re.split(r'(?<=[.!?])\s+', reply)
        reply = ' '.join(sentences[:4])
    
    if not reply.endswith(('.', '!', '?')):
        reply += '.'
    
    if not any(word in reply.lower() for word in ["что ещё", "может", "поищем", "ещё что-то"]):
        if "?" not in reply[-10:]:
            reply += " Что ещё могу сделать для тебя?"
    
    name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", lower)
    if name_match:
        save_memory(chat_id, "name", name_match.group(1).capitalize())
    
    if "нравится" in lower:
        save_memory(chat_id, "likes", text)
    if "не нравится" in lower:
        save_memory(chat_id, "dislikes", text)
    
    words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', lower)
    stop_words = ["привет", "здравствуй", "спасибо", "пока", "да", "нет", "хорошо", "плохо"]
    for word in words:
        if word not in stop_words and len(word) > 3:
            save_topic(chat_id, word)
    
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
