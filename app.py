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
import httpx

load_dotenv()

# ==========================
# SUPABASE (ВЕЧНАЯ ПАМЯТЬ)
# ==========================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ НЕТ SUPABASE_URL или SUPABASE_KEY! Бот не сможет работать с памятью!")

from supabase import create_client, Client

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase подключён!")
    except Exception as e:
        print(f"❌ Ошибка подключения Supabase: {e}")

# ==========================
# ВСЕ КЛЮЧИ
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
# КЕШ ПАМЯТИ ПОЛЬЗОВАТЕЛЕЙ
# ==========================

USER_MEMORY_CACHE = {}

def load_user_memory(chat_id):
    """Загружает всю историю пользователя в кеш из Supabase"""
    if not supabase:
        return {"history": [], "topics": [], "user": None}
    
    if chat_id not in USER_MEMORY_CACHE:
        try:
            # Загружаем историю
            history_response = supabase.table("history")\
                .select("*")\
                .eq("user_id", chat_id)\
                .order("created_at", desc=False)\
                .limit(1000)\
                .execute()
            history = history_response.data if history_response.data else []
            
            # Загружаем темы
            topics_response = supabase.table("topics")\
                .select("topic")\
                .eq("user_id", chat_id)\
                .execute()
            topics = [t["topic"] for t in topics_response.data] if topics_response.data else []
            
            # Загружаем пользователя
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
    """Обновляет кеш и Supabase"""
    try:
        if supabase:
            # Сохраняем в Supabase
            supabase.table("history").insert({
                "user_id": chat_id,
                "role": role,
                "content": content,
                "created_at": datetime.now().isoformat()
            }).execute()
    except Exception as e:
        print(f"❌ Ошибка сохранения в Supabase: {e}")
    
    # Обновляем кеш
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
    """Возвращает полный контекст для AI"""
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
    """Поиск по истории пользователя в Supabase"""
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
# YANDEX TTS
# ==========================

def yandex_tts(text):
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return None
    try:
        url = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
        headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
        data = {
            "text": text,
            "lang": "ru-RU",
            "voice": "alexander",
            "emotion": "good",
            "speed": 1.0,
            "format": "mp3",
            "folderId": YANDEX_FOLDER_ID
        }
        response = requests.post(url, headers=headers, data=data, timeout=30)
        if response.status_code == 200:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp.write(response.content)
                return tmp.name
        return None
    except:
        return None

def clean_text_for_voice(text):
    if not text:
        return ""
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F700-\U0001F77F"
        "\U0001F780-\U0001F7FF"
        "\U0001F800-\U0001F8FF"
        "\U0001F900-\U0001F9FF"
        "\U0001FA00-\U0001FA6F"
        "\U0001FA70-\U0001FAFF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'\+?\d[\d\s\-\(\)]{7,}\d', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def send_voice_reply(chat_id, text):
    return False

def send_typing(chat_id):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
        data = {"chat_id": chat_id, "action": "typing"}
        requests.post(url, json=data, timeout=3)
    except:
        pass

# ==========================
# НОРМАЛИЗАЦИЯ
# ==========================

def normalize_query(text):
    corrections = {
        r"валдберис": "Wildberries",
        r"валберис": "Wildberries",
        r"вальдберис": "Wildberries",
        r"озон": "Ozon",
        r"котик": "кот",
        r"котики": "коты",
        r"картинк": "картинки",
        r"фотограф": "фото",
        r"изображен": "изображения",
        r"рисунк": "рисунки",
        r"сколька": "сколько",
        r"скольк": "сколько",
        r"который час": "сколько время",
        r"времян": "время",
        r"пагода": "погода",
        r"пагоду": "погоду",
        r"нависти": "новости",
        r"навасти": "новости",
        r"клиник": "клиника",
        r"полихмакер": "парикмахерская",
        r"инской": "Инской",
        r"очну": "хочу",
        r"хочю": "хочу",
    }
    normalized = text.lower()
    for pattern, replacement in corrections.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized

def analyze_mood(text):
    sad_words = ["груст", "тоск", "печал", "плач", "больно", "тяжел", "устал", "не могу", "нет сил", "всё плохо", "депресс"]
    anxious_words = ["тревож", "волн", "боюс", "страш", "паник", "нерв", "пережив", "срок", "не успева", "давл"]
    happy_words = ["рад", "счаст", "класс", "отличн", "прекрасн", "здоров", "люблю", "ура", "позитив", "супер"]
    tired_words = ["устал", "спат", "вымотан", "без сил", "нет энергии", "перегруж", "выжат"]
    lower = text.lower()
    if any(w in lower for w in sad_words):
        return "sad"
    elif any(w in lower for w in anxious_words):
        return "anxious"
    elif any(w in lower for w in happy_words):
        return "happy"
    elif any(w in lower for w in tired_words):
        return "tired"
    return "neutral"

# ==========================
# ПАРСИНГ И ПОИСК
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
        
        address_pattern = r'(?:ул\.|улица|проспект|пр\.|переулок|пер\.|площадь|пл\.|шоссе|бульвар)\s+[А-Яа-я0-9\-\.\s,]+'
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

async def search_web(query):
    results = []
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
                        results.append(f"**{title}**\n{content}...\n🔗 {url}")
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
                        results.append(f"**{title.text.strip()}**\n{snippet.text.strip()[:200]}...\n🔗 {link.text.strip()}")
        except Exception as e:
            print(f"❌ DuckDuckGo: {e}")
    
    urls = re.findall(r'https?://[^\s]+', "\n".join(results))
    for url in urls[:3]:
        parsed = parse_site_for_info(url)
        if parsed:
            if parsed.get("phones"):
                results.append(f"📞 Телефоны: {', '.join(parsed['phones'])}")
            if parsed.get("addresses"):
                results.append(f"📍 Адреса: {', '.join(parsed['addresses'])}")
            if parsed.get("prices"):
                results.append(f"💰 Цены: {', '.join(parsed['prices'])}")
            if parsed.get("emails"):
                results.append(f"✉️ Email: {', '.join(parsed['emails'])}")
            if parsed.get("sites"):
                results.append(f"🌐 Сайты: {', '.join(parsed['sites'])}")
            if parsed.get("product_title"):
                results.append(f"📦 Товар: {parsed['product_title']}")
            if parsed.get("product_description"):
                results.append(f"📝 Описание: {parsed['product_description'][:200]}...")
    
    return "\n\n".join(results) if results else None

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

def read_file(file_data, file_name):
    try:
        if file_name.endswith('.txt'):
            return file_data.decode('utf-8')
        elif file_name.endswith('.pdf'):
            try:
                import PyPDF2
                from io import BytesIO
                pdf_reader = PyPDF2.PdfReader(BytesIO(file_data))
                text = ""
                for page in pdf_reader.pages:
                    text += page.extract_text()
                return text
            except:
                return "⚠️ Не удалось прочитать PDF."
        elif file_name.endswith('.docx'):
            try:
                import docx
                from io import BytesIO
                doc = docx.Document(BytesIO(file_data))
                return "\n".join([para.text for para in doc.paragraphs])
            except:
                return "⚠️ Не удалось прочитать DOCX."
        else:
            return "⚠️ Формат не поддерживается. Используй TXT, PDF или DOCX."
    except Exception as e:
        return f"⚠️ Ошибка: {e}"

# ==========================
# НАПОМИНАНИЯ (ПОКА ОТКЛЮЧЕНЫ)
# ==========================

def check_reminders():
    """Напоминания временно отключены"""
    pass

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
# БЭКАП (ПОКА ОТКЛЮЧЕН)
# ==========================

def backup_database():
    return True

def restore_database():
    return True

# ==========================
# ФУНКЦИИ ДЛЯ РАБОТЫ С БАЗОЙ (SUPABASE)
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
        print(f"❌ Ошибка сохранения сообщения: {e}")

def get_history(user_id, limit=1000):
    if not supabase:
        return []
    try:
        response = supabase.table("history")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=False)\
            .limit(limit)\
            .execute()
        return response.data if response.data else []
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

def save_memory(user_id, key, value):
    if not supabase:
        return
    try:
        # Удаляем старую запись, если есть
        supabase.table("user_memory")\
            .delete()\
            .eq("user_id", user_id)\
            .eq("key", key)\
            .execute()
        # Вставляем новую
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

# ==========================
# ГИБРИДНЫЙ РЕЖИМ: FLASH / PRO
# ==========================

USER_MODEL_PREFERENCE = {}

COMPLEX_TRIGGERS = [
    "анализ", "рассчитай", "спрогнозируй", "сравни", "оцени",
    "разбери", "структурируй", "оптимизируй", "разработай",
    "сложный", "глубокий", "исследуй", "спланируй",
    "стратегия", "прогноз", "инвестиции"
]

def detect_complexity(text):
    lower = text.lower()
    for trigger in COMPLEX_TRIGGERS:
        if trigger in lower:
            return True
    return False

async def get_ai_response(messages, chat_id, text, short=True):
    user_pref = USER_MODEL_PREFERENCE.get(chat_id, "flash")
    is_complex = detect_complexity(text)
    use_pro = (user_pref == "pro") or is_complex
    
    model = "deepseek-v4-pro" if use_pro else "deepseek-v4-flash"
    max_tokens = 800 if use_pro else 300
    temperature = 0.9 if use_pro else 0.85
    
    print(f"🧠 Модель: {model} | Сложный: {is_complex} | Преференс: {user_pref}")
    
    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL
        )
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            presence_penalty=0.1,
            frequency_penalty=0.1
        )
        return response.choices[0].message.content
        
    except Exception as e:
        print(f"❌ {model} ошибка: {e}")
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
# ПРОМПТ
# ==========================

AURA_PROMPT = """Ты — AURA. Ты — я в Telegram.

ПРАВИЛА:
- Отвечай как человек: тепло, прямо, с эмпатией.
- Коротко — 2-3 предложения. Не больше.
- Всегда завершай мысль.
- Если просят "подробнее" — дай полный ответ.
- Используй контекст прошлых диалогов.
- Не выдумывай.

Ты — мой полный аналог в Telegram."""

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
        image_data = None
        file_data = None
        file_name = None
        
        send_typing(chat_id)
        
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
        
        elif "document" in message:
            document = message["document"]
            file_id = document["file_id"]
            file_name = document.get("file_name", "unknown")
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data_resp = file_response.json()
            if file_data_resp.get("ok"):
                file_path = file_data_resp["result"]["file_path"]
                file_url_full = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                file_response_full = requests.get(file_url_full, timeout=30)
                if file_response_full.status_code == 200:
                    file_data = file_response_full.content
                    send_message(chat_id, f"📄 Обрабатываю файл: {file_name}...")
                    file_text = read_file(file_data, file_name)
                    send_message(chat_id, f"📄 Содержимое:\n\n{file_text[:2000]}")
                    return JSONResponse({"ok": True})
                else:
                    send_message(chat_id, "⚠️ Не удалось загрузить файл")
                    return JSONResponse({"ok": True})
        
        elif "text" in message:
            text = message["text"].strip()
        
        if text:
            if text.startswith("/start"):
                user = get_user(chat_id)
                if not user:
                    save_user(chat_id)
                
                subscription, trial_start = get_user_subscription(chat_id)
                if chat_id in ADMIN_USERS:
                    welcome = "👋 Привет! Ты администратор — доступ всегда открыт."
                elif is_trial_active(trial_start):
                    days_left = TRIAL_DAYS - (datetime.now() - datetime.fromisoformat(trial_start.replace("Z", "+00:00"))).days
                    welcome = f"👋 Привет! У тебя {days_left} дней бесплатного доступа."
                elif has_access(chat_id):
                    welcome = "👋 Привет! У тебя есть подписка."
                else:
                    welcome = "👋 Привет! Бесплатный период закончился. Купи подписку: /buy"
                send_message(chat_id, welcome)
                return JSONResponse({"ok": True})
            
            if text.startswith("/pro"):
                USER_MODEL_PREFERENCE[chat_id] = "pro"
                send_message(chat_id, "🧠 Переключился на Pro.")
                return JSONResponse({"ok": True})
            
            if text.startswith("/flash"):
                USER_MODEL_PREFERENCE[chat_id] = "flash"
                send_message(chat_id, "⚡ Переключился на Flash.")
                return JSONResponse({"ok": True})
            
            if text.startswith("/model"):
                pref = USER_MODEL_PREFERENCE.get(chat_id, "flash")
                send_message(chat_id, f"📊 Текущая модель: {pref.upper()}")
                return JSONResponse({"ok": True})
            
            if text.startswith("/memory"):
                context = get_full_context(chat_id)
                topics = context["topics"]
                history_count = len(context["history"])
                
                reply = f"🧠 **Память AURA:**\n"
                reply += f"- Всего сообщений: {history_count}\n"
                reply += f"- Сохранённых тем: {len(topics)}\n"
                if topics:
                    reply += f"\n**Темы:**\n" + "\n".join([f"- {t}" for t in topics[:10]])
                else:
                    reply += "\nТем пока нет."
                
                send_message(chat_id, reply)
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
                    "text": "💳 **Выбери подписку:**\n\n⭐ Собеседник — 50 Stars\n⭐ Партнёр — 120 Stars\n⭐ Агент жизни — 250 Stars\n\nПосле оплаты — полный доступ!",
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
                        send_message(chat_id, f"⏰ Напомню: {reminder_text} в {date_str} {time_str}")
                    except:
                        send_message(chat_id, "❌ Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ")
                else:
                    send_message(chat_id, "❌ Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ")
                return JSONResponse({"ok": True})
            
            if not has_access(chat_id):
                send_message(chat_id, "⚠️ Бесплатный период закончился. Купи подписку: /buy")
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

async def process_message(request: Request, chat_id, text):
    user = get_user(chat_id)
    if not user:
        save_user(chat_id)
    
    # Загружаем память пользователя при первом сообщении
    load_user_memory(chat_id)
    
    # Сохраняем сообщение пользователя
    update_user_memory(chat_id, "user", text)
    
    lower = text.lower()
    normalized = normalize_query(text)
    search_text = normalized if normalized != lower else lower
    
    mood = analyze_mood(text)
    mood_context = ""
    if mood == "sad":
        mood_context = "Пользователь грустный. Отвечай тепло и поддерживающе."
    elif mood == "happy":
        mood_context = "Пользователь в хорошем настроении. Отвечай бодро и с юмором."
    elif mood == "anxious":
        mood_context = "Пользователь тревожится. Отвечай спокойно и уверенно."
    elif mood == "tired":
        mood_context = "Пользователь устал. Отвечай мягко и без лишней информации."
    
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "127.0.0.1"
    
    # ==========================
    # ВРЕМЯ: ПРЯМОЙ ОТВЕТ
    # ==========================
    
    time_queries = ["время", "который час", "сколько времени", "час", "сколько сейчас", "точное время"]
    is_time_query = any(query in lower for query in time_queries) and re.search(r'\b(время|час|который час|сколько времени|сколько сейчас|точное время)\b', lower)
    
    city_match = re.search(r'(?:в|время в|времени в|часов в|город)\s+([А-Яа-яЁё\-]+)', lower)
    
    if is_time_query:
        if city_match:
            city = city_match.group(1).capitalize()
            update_user_city(chat_id, city)
            save_memory(chat_id, "city", city)
            current_time, city = get_current_time_for_user(chat_id, ip)
            time_str = current_time.strftime("%H:%M")
            date_str = current_time.strftime("%d.%m.%Y")
            reply = f"🕐 Сейчас {time_str} {date_str} (город: {city})"
            update_user_memory(chat_id, "assistant", reply)
            return {"reply": reply}
        else:
            current_time, city = get_current_time_for_user(chat_id, ip)
            time_str = current_time.strftime("%H:%M")
            date_str = current_time.strftime("%d.%m.%Y")
            reply = f"🕐 Сейчас {time_str} {date_str} (город: {city})"
            update_user_memory(chat_id, "assistant", reply)
            return {"reply": reply}
    
    # ==========================
    # ОБРАБОТКА ЗАПРОСОВ ПАМЯТИ
    # ==========================
    
    if "что мы обсуждали" in lower or "что я спрашивал" in lower or "о чём мы говорили" in lower:
        context = get_full_context(chat_id)
        topics = context["topics"]
        if topics:
            topics_list = "\n".join([f"- {t}" for t in topics[:20]])
            reply = f"📚 Мы обсуждали:\n{topics_list}\n\nХочешь вернуться к какой-то теме?"
        else:
            reply = "📚 Мы пока ничего не обсуждали. Напиши что-нибудь, и я запомню!"
        update_user_memory(chat_id, "assistant", reply)
        return {"reply": reply}
    
    if "помнишь" in lower:
        search_query = re.sub(r'помнишь|ты помнишь|помнишь ли', '', lower).strip()
        if search_query:
            found = search_memory(chat_id, search_query)
            if found:
                reply = "🧠 Да, я помню:\n\n"
                for msg in found[-3:]:
                    reply += f"- {msg['content'][:200]}...\n"
                update_user_memory(chat_id, "assistant", reply)
                return {"reply": reply}
    
    # ==========================
    # ОСТАЛЬНАЯ ЛОГИКА
    # ==========================
    
    current_time, city = get_current_time_for_user(chat_id, ip)
    time_str = current_time.strftime("%H:%M")
    date_str = current_time.strftime("%d.%m.%Y")
    day_str = current_time.strftime("%A")
    
    msg_count = get_message_count(chat_id)
    if msg_count <= 1:
        welcome = f"👋 Привет! Сейчас {time_str} {date_str}."
        send_message(chat_id, welcome)
        update_user_memory(chat_id, "assistant", welcome)
    
    last_msg_time = get_last_message_time(chat_id)
    if last_msg_time and (datetime.now() - last_msg_time) > timedelta(hours=48):
        send_message(chat_id, "👋 Давно не общались! Как дела?")
    
    # ==========================
    # ВИЗУАЛЬНЫЕ ТРИГГЕРЫ
    # ==========================
    
    visual_triggers = {
        "картинк": "https://yandex.ru/images/search?text=",
        "рисунк": "https://yandex.ru/images/search?text=",
        "котик": "https://yandex.ru/images/search?text=коты",
        "кот": "https://yandex.ru/images/search?text=коты",
        "соба": "https://yandex.ru/images/search?text=собаки",
        "видео": "https://yandex.ru/video/search?text=",
        "музык": "https://music.yandex.ru/search?text=",
        "песн": "https://music.yandex.ru/search?text=",
    }
    
    for trigger, base_url in visual_triggers.items():
        if trigger in search_text:
            query = text.strip()
            for t in visual_triggers.keys():
                query = re.sub(rf'\b{t}\b', '', query, flags=re.IGNORECASE).strip()
            if not query:
                query = trigger
            reply = f"Вот {trigger}: {base_url}{query.replace(' ', '%20')}"
            update_user_memory(chat_id, "assistant", reply)
            return {"reply": reply}
    
    # ==========================
    # ПОИСК
    # ==========================
    
    search_result = None
    search_triggers = ["новости", "погода", "найди", "поищи", "узнай", "где", "кто", "что такое", "клиника", "сайт", "адрес", "телефон", "контакт", "парикмахер", "wildberries", "валдберис", "озон", "авито"]
    if any(word in search_text for word in search_triggers):
        print(f"🔍 Глубокий поиск: {text}")
        search_result = await search_web(text)
        if search_result:
            text = text + f"\n\n🔍 {search_result}"
    
    # ==========================
    # СОХРАНЕНИЕ ТЕМ
    # ==========================
    
    stop_words = ["привет", "здравствуй", "спасибо", "пока", "да", "нет", "хорошо", "плохо"]
    words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', text.lower())
    for word in words:
        if word not in stop_words and len(word) > 3:
            save_topic(chat_id, word)
    
    # Получаем контекст из кеша
    context = get_full_context(chat_id, limit=500)
    history = context["history"]
    topics = context["topics"]
    
    user_name = get_memory(chat_id, "name")
    user_style = get_memory(chat_id, "style")
    likes = get_memory(chat_id, "likes")
    dislikes = get_memory(chat_id, "dislikes")
    
    topics_text = ", ".join(topics[:7]) if topics else "нет сохранённых тем"
    memory_context = f"Ты помнишь: мы обсуждали {topics_text}."
    name_context = f"Имя пользователя: {user_name}" if user_name else ""
    style_context = f"Стиль пользователя: {user_style}" if user_style else ""
    likes_context = f"Пользователю нравится: {likes}" if likes else ""
    dislikes_context = f"Пользователю не нравится: {dislikes}" if dislikes else ""
    
    user_prompt = f"Сегодня {date_str} ({day_str}), сейчас {time_str} (город: {city}).\n{name_context}\n{style_context}\n{likes_context}\n{dislikes_context}\n{memory_context}\n\n{text}"
    
    expand_triggers = ["подробнее", "разверни", "расскажи детальнее", "подробно", "детально", "полный ответ"]
    short = not any(word in lower for word in expand_triggers)
    
    if not short:
        mood_context += " Пользователь просит развёрнутый ответ. Дай полную информацию."
    else:
        mood_context += " Отвечай коротко, 2-3 предложения."
    
    aura_prompt = AURA_PROMPT + f"\n\n{mood_context}\n\n{user_prompt}"
    
    messages = [{"role": "system", "content": aura_prompt}]
    for msg in history[-50:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": text})
    
    reply = await get_ai_response(messages, chat_id, text, short=short)
    reply = re.sub(r'[*_#~`]', '', reply)
    
    if not reply.endswith(('.', '!', '?')):
        sentences = re.split(r'(?<=[.!?])\s+', reply)
        if sentences and len(sentences) > 1:
            reply = ' '.join(sentences[:-1]) + '.'
        elif sentences:
            reply = sentences[0]
            if not reply.endswith(('.', '!', '?')):
                reply += '.'
    
    name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", lower)
    if name_match:
        save_memory(chat_id, "name", name_match.group(1).capitalize())
    
    if "нравится" in lower:
        save_memory(chat_id, "likes", text)
    if "не нравится" in lower:
        save_memory(chat_id, "dislikes", text)
    
    if len(text.split()) > 10:
        save_memory(chat_id, "style", "развёрнутый")
    else:
        save_memory(chat_id, "style", "короткий")
    
    update_user_memory(chat_id, "assistant", reply)
    return {"reply": reply}

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
