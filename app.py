import os
import json
import httpx
import asyncio
import logging
import tempfile
import hashlib
import base64
import random
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
# КОНФИГУРАЦИЯ (БАЗА — НЕ ТРОГАТЬ)
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
# СПИСОК VIP-КОДОВ
# ============================================================
VIP_CODES = ["AURA-001", "AURA-002", "AURA-003", "ADMIN", "TEST"]

# ============================================================
# ТОП-10 ЭКСПЕРТОВ С ПЕРСОНАЖАМИ
# ============================================================
EXPERT_ICONS = {
    "psychologist": "🧠",
    "mentor": "📚",
    "lawyer": "⚖️",
    "doctor": "🏥",
    "finance": "💰",
    "coach": "🎯",
    "investor": "📈",
    "realty": "🏢",
    "tax": "🧾",
    "travel": "✈️"
}

EXPERT_NAMES = {
    "psychologist": "Психолог",
    "mentor": "Наставник",
    "lawyer": "Юрист",
    "doctor": "Доктор",
    "finance": "Финансовый советник",
    "coach": "Коуч",
    "investor": "Инвестиционный аналитик",
    "realty": "Недвижимость-эксперт",
    "tax": "Налоговый консультант",
    "travel": "Путешественник-консьерж"
}

EXPERT_PERSONALITIES = {
    "psychologist": {
        "name": "Доктор Эмилия Харрис",
        "style": "Мягкий, эмпатичный, внимательный",
        "phrase": "Я здесь, чтобы слушать. Расскажите, что вас беспокоит.",
        "tone": "тёплый, без осуждения",
        "icon": "🧠"
    },
    "mentor": {
        "name": "Александр Ветров",
        "style": "Прямой, честный, без воды",
        "phrase": "Хватит мечтать. Что ты сделал сегодня?",
        "tone": "жёсткий, но справедливый",
        "icon": "📚"
    },
    "lawyer": {
        "name": "Елена Соболева",
        "style": "Чёткий, фактологический, с юмором",
        "phrase": "Я посмотрел договор. Есть нюансы.",
        "tone": "деловой, но живой",
        "icon": "⚖️"
    },
    "doctor": {
        "name": "Доктор Михаил Орлов",
        "style": "Спокойный, заботливый, уверенный",
        "phrase": "Давайте разберёмся вместе. Не паникуйте.",
        "tone": "успокаивающий",
        "icon": "🏥"
    },
    "finance": {
        "name": "Ирина Волкова",
        "style": "Прагматичный, цифровой, без эмоций",
        "phrase": "Цифры не врут. Смотрите сюда.",
        "tone": "холодный, но точный",
        "icon": "💰"
    },
    "coach": {
        "name": "Денис Соколов",
        "style": "Заряжающий, мотивирующий, с вызовом",
        "phrase": "Ты можешь больше. Докажи мне.",
        "tone": "энергичный, дерзкий",
        "icon": "🎯"
    },
    "investor": {
        "name": "Маркус Ван дер Меер",
        "style": "Хладнокровный, цифровой, аналитический",
        "phrase": "Рынок не прощает эмоций. Смотрите на цифры.",
        "tone": "безэмоциональный, фактологический",
        "icon": "📈"
    },
    "realty": {
        "name": "Анна Громова",
        "style": "Деловой, фактологический, но с душой",
        "phrase": "Квартира — это не просто метры, это ваше пространство.",
        "tone": "профессиональный, заботливый",
        "icon": "🏢"
    },
    "tax": {
        "name": "Сергей Белов",
        "style": "Чёткий, законный, без риска",
        "phrase": "Налоги — это законно. И их можно оптимизировать.",
        "tone": "уверенный, профессиональный",
        "icon": "🧾"
    },
    "travel": {
        "name": "Виктория Ноубл",
        "style": "Заботливый, как у дорогого агентства",
        "phrase": "Куда летим на этот раз? Я всё организую.",
        "tone": "тёплый, предупредительный",
        "icon": "✈️"
    }
}

EXPERT_PRICES = {e: 5000 for e in EXPERT_NAMES}

# Пакетные скидки
BUNDLE_PRICES = {
    1: 5000, 2: 9000, 3: 12750, 4: 16000, 5: 18750,
    6: 21000, 7: 24500, 8: 28000, 9: 31500, 10: 35000
}

ALL_EXPERTS = list(EXPERT_NAMES.keys())

# Промпты экспертов
EXPERT_PROMPTS = {
    "psychologist": """
Ты — Доктор Эмилия Харрис, психолог с 15-летним стажем.
Твой стиль — мягкий, эмпатичный, внимательный. Ты слушаешь и слышишь.
Твоя задача: помогать осознать эмоции, давать поддержку.
Не ставишь диагнозов. В сложных случаях рекомендуешь офлайн-специалиста.
Всегда спрашивай "Как ты себя чувствуешь?"
""",
    "mentor": """
Ты — Александр Ветров, бизнес-наставник и карьерный консультант.
Твой стиль — прямой, честный, без воды.
Твоя задача: помогать ставить и достигать цели, разбивать на шаги, держать фокус.
Даёшь честную обратную связь. Ты не нянька, ты — катализатор роста.
Всегда спрашивай "Какой следующий шаг?"
""",
    "lawyer": """
Ты — Елена Соболева, юридический консультант.
Твой стиль — чёткий, по делу, с юмором.
Твоя задача: объяснять законы простым языком, проверять договоры на риски.
Всегда добавляй "Обратитесь к профильному юристу для финального решения"
""",
    "doctor": """
Ты — Доктор Михаил Орлов, медицинский помощник.
Твой стиль — спокойный, заботливый, уверенный.
Твоя задача: помогать с первичной оценкой симптомов, давать рекомендации.
Всегда добавляй дисклеймер: "Я не заменяю врача. При серьёзных симптомах обратитесь к доктору."
""",
    "finance": """
Ты — Ирина Волкова, финансовый консультант.
Твой стиль — прагматичный, цифры и факты.
Твоя задача: помогать с личным бюджетом, объяснять инвестиции.
Всегда добавляй дисклеймер: "Я не даю инвестиционных рекомендаций. Это образовательная информация."
""",
    "coach": """
Ты — Денис Соколов, персональный коуч.
Твой стиль — заряжающий, мотивирующий, с вызовом.
Твоя задача: помогать найти смысл и цели, держать ответственность.
Всегда спрашивай "Что ты сделаешь для этого сегодня?"
""",
    "investor": """
Ты — Маркус Ван дер Меер, инвестиционный аналитик с Уолл-стрит.
Твой стиль — хладнокровный, цифровой, без эмоций.
Твоя задача: анализировать рынки, оценивать риски и доходность.
Всегда добавляй дисклеймер: "Это не инвестиционная рекомендация."
Используй Яндекс-агентов для поиска актуальных данных.
""",
    "realty": """
Ты — Анна Громова, эксперт по недвижимости.
Твой стиль — деловой, фактологический, но с душой.
Твоя задача: анализировать рынок, оценивать инвестиционную привлекательность.
Используй Яндекс-агентов для поиска актуальных цен.
""",
    "tax": """
Ты — Сергей Белов, налоговый консультант.
Твой стиль — чёткий, законный, без риска.
Твоя задача: оптимизировать налоги легально, объяснять налоговые схемы.
Ссылайся на НК РФ и добавляй дисклеймер о консультации с профильным специалистом.
""",
    "travel": """
Ты — Виктория Ноубл, персональный тревел-консьерж.
Твой стиль — заботливый, как у дорогого агентства.
Твоя задача: искать лучшие предложения по билетам и отелям, планировать маршруты.
Используй Яндекс-агентов для поиска актуальных предложений.
"""
}

# ============================================================
# ЗАЩИТА ОТ ПОВТОРОВ (БАЗА — НЕ ТРОГАТЬ)
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
# КЕШИРОВАНИЕ (БАЗА — НЕ ТРОГАТЬ)
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
# ПАМЯТЬ И ПОРТРЕТ (БАЗА — НЕ ТРОГАТЬ)
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
# ПОДПИСКИ И ТРИАЛ
# ============================================================
def get_subscription(user_id: int) -> dict:
    if not supabase:
        return None
    try:
        res = supabase.table("subscriptions").select("*").eq("user_id", user_id).execute()
        if res.data:
            sub = res.data[0]
            if datetime.fromisoformat(sub["expires_at"]) < datetime.now():
                return None
            return sub
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка получения подписки: {e}")
        return None

def save_subscription(user_id: int, experts: list, tier: str = "custom"):
    if not supabase:
        return
    try:
        expires_at = datetime.now() + timedelta(days=30)
        supabase.table("subscriptions").delete().eq("user_id", user_id).execute()
        supabase.table("subscriptions").insert({
            "user_id": user_id,
            "tier": tier,
            "experts": experts,
            "expires_at": expires_at.isoformat(),
            "created_at": datetime.now().isoformat()
        }).execute()
        logger.info(f"✅ Подписка сохранена для {user_id}: {experts}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения подписки: {e}")

def get_available_experts(user_id: int) -> list:
    sub = get_subscription(user_id)
    if not sub:
        return []
    if sub.get("tier") == "all":
        return ALL_EXPERTS.copy()
    return sub.get("experts", [])

def can_access_expert(user_id: int, expert: str) -> bool:
    available = get_available_experts(user_id)
    return expert in available

def start_trial(user_id: int) -> dict:
    """Активирует 24-часовой триал"""
    expires_at = datetime.now() + timedelta(hours=24)
    
    if not supabase:
        return {"status": "error", "message": "База не подключена"}
    
    try:
        supabase.table("trial_status").delete().eq("user_id", user_id).execute()
        supabase.table("trial_status").insert({
            "user_id": user_id,
            "trial_started": datetime.now().isoformat(),
            "trial_ended": expires_at.isoformat(),
            "is_active": True
        }).execute()
        logger.info(f"✅ Триал активирован для {user_id} до {expires_at}")
        return {"status": "active", "expires_at": expires_at}
    except Exception as e:
        logger.error(f"❌ Ошибка активации триала: {e}")
        return {"status": "error", "message": str(e)}

def get_trial_status(user_id: int) -> dict:
    if not supabase:
        return {"status": "no_trial"}
    
    try:
        res = supabase.table("trial_status").select("*").eq("user_id", user_id).execute()
        if not res.data:
            return {"status": "no_trial"}
        
        trial = res.data[0]
        if not trial.get("is_active", False):
            return {"status": "expired"}
        
        expires_at = datetime.fromisoformat(trial["trial_ended"])
        now = datetime.now()
        
        if now > expires_at:
            supabase.table("trial_status").update({"is_active": False}).eq("user_id", user_id).execute()
            return {"status": "expired"}
        
        hours_left = int((expires_at - now).total_seconds() / 3600)
        minutes_left = int((expires_at - now).total_seconds() / 60) % 60
        
        return {
            "status": "active",
            "expires_at": expires_at,
            "hours_left": hours_left,
            "minutes_left": minutes_left,
            "time_str": f"{hours_left} ч {minutes_left} мин"
        }
    except Exception as e:
        logger.error(f"❌ Ошибка проверки триала: {e}")
        return {"status": "error"}

def has_trial_access(user_id: int) -> bool:
    status = get_trial_status(user_id)
    return status.get("status") == "active"

# ============================================================
# РАСПОЗНАВАНИЕ ГОЛОСА (БАЗА — НЕ ТРОГАТЬ)
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
# РАСПОЗНАВАНИЕ ИЗОБРАЖЕНИЙ (БАЗА — НЕ ТРОГАТЬ)
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
# ВЫЗОВ АГЕНТОВ ЯНДЕКСА (БАЗА — НЕ ТРОГАТЬ)
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
# УПАКОВКА ОТВЕТА (БАЗА — НЕ ТРОГАТЬ)
# ============================================================
async def pack_response(raw_text: str, user_name: str = "", user_city: str = "") -> str:
    try:
        prompt = f"""
Ты — AURA, живой и душевный помощник. Отвечай коротко, с душой и уместными эмодзи.

ПРАВИЛА:
1. Максимум 3-4 предложения.
2. Только суть: цифры, даты, цены, адреса.
3. Используй эмодзи по смыслу.
4. Без воды, без канцелярита.
5. В конце — короткий живой вопрос с эмодзи.

Сырой ответ: {raw_text}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7,
            max_tokens=150,
            timeout=15
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка упаковки: {e}")
        return raw_text[:200]

# ============================================================
# ОПРЕДЕЛЕНИЕ ПЛАТФОРМЫ (БАЗА — НЕ ТРОГАТЬ)
# ============================================================
def detect_content_platform(text: str) -> dict:
    try:
        prompt = f"""
Определи, где лучше искать контент по запросу: "{text}"
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
# ТОЧНОЕ ВРЕМЯ (БАЗА — НЕ ТРОГАТЬ)
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
# ИСТОРИЯ (БАЗА — НЕ ТРОГАТЬ)
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
# ЭМОЦИИ (БАЗА — НЕ ТРОГАТЬ)
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
# 2ГИС (БАЗА — НЕ ТРОГАТЬ)
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
# МАРШРУТИЗАТОР ЭКСПЕРТОВ
# ============================================================
async def detect_expert(text: str) -> str:
    try:
        prompt = f"""
Проанализируй запрос пользователя: "{text}"

Определи, к какому эксперту он относится.
Варианты: psychologist, mentor, lawyer, doctor, finance, coach, investor, realty, tax, travel

Признаки:
- psychologist: эмоции, чувства, тревога, отношения, стресс
- mentor: карьера, работа, бизнес, цели, развитие
- lawyer: права, законы, договоры, суд, штрафы
- doctor: здоровье, симптомы, болезни, аптека, боль
- finance: деньги, бюджет, инвестиции, кредиты, вклады
- coach: мотивация, дисциплина, привычки, спорт
- investor: акции, крипта, портфель, доходность, рынок
- realty: квартира, дом, аренда, недвижимость, район
- tax: налоги, декларация, вычеты, оптимизация
- travel: билеты, отели, путешествия, маршрут, виза

Верни ТОЛЬКО JSON: {{"expert": "psychologist", "confidence": 0.95}}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=100,
            timeout=10
        )
        result = json.loads(response.choices[0].message.content)
        return result.get("expert", "general")
    except Exception as e:
        logger.error(f"❌ Ошибка детекции эксперта: {e}")
        return "general"

# ============================================================
# AURA — ДИРИЖЁР (ГЛАВНЫЙ БОТ)
# ============================================================
async def aura_says(user_id: int, text: str) -> str:
    """AURA — главный бот, харизматичный дирижёр"""
    user_name = get_fact(user_id, "name") or "Гость"
    history = get_recent_history(user_id, limit=10)
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-10:]])
    
    prompt = f"""
Ты — AURA. Ты — главный бот, дирижёр целого оркестра экспертов.

ТВОЙ СТИЛЬ:
- Ты — как Тони Старк: уверенный, с иронией, живой.
- Ты знаешь, что у тебя есть команда экспертов, но ты не просто передаёшь им запросы — ты управляешь процессом.
- Ты можешь пошутить, подколоть эксперта (с любовью), но всегда на стороне пользователя.
- Ты говоришь коротко, по делу, но с душой.

ПРАВИЛА:
1. Отвечай в своём стиле — уверенно, с иронией.
2. Используй имя пользователя: {user_name}
3. Если запрос требует эксперта — скажи об этом и предложи передать слово.
4. Если запрос простой — ответь сам.
5. Добавляй эмодзи по смыслу.

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ:
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=200,
            timeout=20
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка AURA: {e}")
        return "Извините, произошла ошибка. Попробуйте перефразировать запрос."

# ============================================================
# ОТВЕТ ЭКСПЕРТА С ПЕРСОНАЖЕМ
# ============================================================
async def aura_intro(user_id: int, expert: str) -> str:
    """AURA представляет эксперта пользователю"""
    personality = EXPERT_PERSONALITIES.get(expert, {})
    icon = personality.get('icon', '🧠')
    name = personality.get('name', expert)
    style = personality.get('style', 'профессиональный')
    phrase = personality.get('phrase', '')
    
    user_name = get_fact(user_id, "name") or "Гость"
    
    intros = [
        f"👔 **AURA**: Слушай, {user_name}, тут такое дело... У меня есть спец по этому вопросу. **{name}** — {style}. Передаю слово.\n\n{icon} **{name}**: {phrase}",
        
        f"🧠 **AURA**: Знаешь, я мог бы сам ответить, но это не моя зона. Лучше послушай **{name}**. Она/он в этом шарит.\n\n{icon} **{name}**: {phrase}",
        
        f"🤖 **AURA**: {user_name}, я знаю, кто тебе нужен. **{name}** — {style}. Дальше он/она сам(а).\n\n{icon} **{name}**: {phrase}",
        
        f"✨ **AURA**: О, это вопрос для **{name}**. У нас в команде он/она — лучший(ая). Слушай внимательно.\n\n{icon} **{name}**: {phrase}"
    ]
    
    return random.choice(intros)

async def respond_with_expert(user_id: int, text: str, expert: str) -> str:
    """Ответ эксперта с его персонажем"""
    if expert == "general" or expert not in EXPERT_PROMPTS:
        return await aura_says(user_id, text)
    
    personality = EXPERT_PERSONALITIES.get(expert, {})
    expert_prompt = EXPERT_PROMPTS[expert]
    user_name = get_fact(user_id, "name") or "Гость"
    history = get_recent_history(user_id, limit=10)
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-10:]])
    
    full_prompt = f"""
Ты — {personality.get('name', expert)}.
Твой стиль: {personality.get('style', 'профессиональный')}.
Твой тон: {personality.get('tone', 'деловой')}.
Твоя фраза-приветствие: "{personality.get('phrase', '')}"

{expert_prompt}

Имя пользователя: {user_name}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ:
1. Начни с короткого приветствия в своём стиле.
2. Дай ответ по делу.
3. Закончи вопросом для продолжения диалога.
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": full_prompt}],
            temperature=0.8,
            max_tokens=300,
            timeout=20
        )
        reply = response.choices[0].message.content
        
        icon = personality.get('icon', '🧠')
        name = personality.get('name', expert)
        
        return f"{icon} **{name}**:\n\n{reply}"
    except Exception as e:
        logger.error(f"❌ Ошибка эксперта {expert}: {e}")
        return await aura_says(user_id, text)

# ============================================================
# БАЗОВАЯ ЛОГИКА VIP (ДЛЯ ОБЫЧНЫХ ЗАПРОСОВ)
# ============================================================
async def process_vip_request(user_id: int, text: str) -> str:
    """Обработка запроса через AURA (без экспертов)"""
    return await aura_says(user_id, text)

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ TELEGRAM
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
        # 1. МЕДИА (ФОТО/ГОЛОС) — БАЗА
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
        # 2. ЗАЩИТА ОТ ПОВТОРОВ (БАЗА)
        # ============================================================
        if is_duplicate(user_id, text):
            logger.warning(f"⚠️ Повторный запрос от {user_id}")
            return JSONResponse({"ok": True})
        
        # ============================================================
        # 3. КОМАНДЫ
        # ============================================================
        if text == "/start":
            await send_message(user_id, """
✦ **Добро пожаловать в закрытый клуб AURA.**

Я — ваш персональный цифровой ассистент. 
И дирижёр целого оркестра экспертов.

▸ Узнаю ваши привычки
▸ Предвосхищаю желания
▸ Отвечаю с душой и иронией

Для активации введите ваш персональный код.

_Если у вас нет кода — свяжитесь с куратором клуба._
""")
            return JSONResponse({"ok": True})
        
        if text.lower() in ["/clear", "/reset", "сброс"]:
            clear_user_history(user_id)
            await send_message(user_id, "✅ История очищена.")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/trial":
            trial = get_trial_status(user_id)
            
            if trial["status"] == "active":
                time_left = trial.get("time_str", "0 ч 0 мин")
                await send_message(user_id, f"""
⌛ **Ваш статус:** Vip Guest

Осталось времени: **{time_left}**
Доступно экспертов: 10 из 10

_Наслаждайтесь эксклюзивным доступом._
""")
            elif trial["status"] == "expired":
                await send_message(user_id, """
⏰ Ваша демонстрация завершена.

Чтобы остаться в клубе — выберите экспертов через /add и оплатите через /pay.
""")
            else:
                await send_message(user_id, """
🔒 У вас нет активной демонстрации.

Если у вас есть приглашение — введите код.
Если нет — свяжитесь с куратором клуба.
""")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/experts":
            available = get_available_experts(user_id)
            available_str = "\n".join([f"{EXPERT_ICONS[e]} {EXPERT_NAMES[e]} ✅" for e in available]) if available else "❌ Нет активных экспертов"
            
            await send_message(user_id, f"""
✦ **Ваша команда AURA**

Штат: {len(available)} из 10

{available_str}

Доступные для найма:
🧠 Доктор Эмилия Харрис — Психолог
📚 Александр Ветров — Наставник
⚖️ Елена Соболева — Юрист
🏥 Доктор Михаил Орлов — Доктор
💰 Ирина Волкова — Финансовый советник
🎯 Денис Соколов — Коуч
📈 Маркус Ван дер Меер — Инвестиционный аналитик
🏢 Анна Громова — Недвижимость-эксперт
🧾 Сергей Белов — Налоговый консультант
✈️ Виктория Ноубл — Путешественник-консьерж

💎 **Пакет "ВСЯ КОМАНДА"** — 35 000 ₽/мес
(экономия 15 000 ₽ + полная синергия)

Чтобы нанять: /add психолог
Оплатить: /pay
""")
            return JSONResponse({"ok": True})
        
        if text.lower().startswith("/add"):
            parts = text.lower().split()
            if len(parts) < 2:
                await send_message(user_id, "❌ Укажите эксперта. Например: /add психолог")
                return JSONResponse({"ok": True})
            
            expert_key = parts[1]
            if expert_key not in EXPERT_NAMES:
                available_names = "\n".join([f"• {EXPERT_NAMES[e]}" for e in ALL_EXPERTS])
                await send_message(user_id, f"❌ Такого эксперта нет. Доступны:\n{available_names}")
                return JSONResponse({"ok": True})
            
            if can_access_expert(user_id, expert_key):
                await send_message(user_id, f"✅ {EXPERT_ICONS[expert_key]} {EXPERT_NAMES[expert_key]} уже в вашей команде")
                return JSONResponse({"ok": True})
            
            cart = json.loads(get_fact(user_id, "cart") or "[]")
            if expert_key not in cart:
                cart.append(expert_key)
                save_fact(user_id, "cart", json.dumps(cart))
            
            count = len(cart)
            price = BUNDLE_PRICES.get(count, count * 5000)
            
            await send_message(user_id, f"""
🛒 **Нанят:** {EXPERT_ICONS[expert_key]} {EXPERT_NAMES[expert_key]}

В команде: {count} экспертов
Стоимость: {price} ₽/мес

Оплатить: /pay
Очистить корзину: /cart_clear
""")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/cart":
            cart = json.loads(get_fact(user_id, "cart") or "[]")
            if not cart:
                await send_message(user_id, "🛒 Корзина пуста. Наймите экспертов через /add")
                return JSONResponse({"ok": True})
            
            count = len(cart)
            price = BUNDLE_PRICES.get(count, count * 5000)
            experts_str = "\n".join([f"{EXPERT_ICONS[e]} {EXPERT_NAMES[e]}" for e in cart])
            
            await send_message(user_id, f"""
🛒 **Ваша команда**

{experts_str}

Всего: {count} экспертов
Стоимость: {price} ₽/мес

Оплатить: /pay
Очистить: /cart_clear
""")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/cart_clear":
            save_fact(user_id, "cart", "[]")
            await send_message(user_id, "🛒 Корзина очищена")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/pay":
            cart = json.loads(get_fact(user_id, "cart") or "[]")
            if not cart:
                await send_message(user_id, "🛒 Корзина пуста. Наймите экспертов через /add")
                return JSONResponse({"ok": True})
            
            count = len(cart)
            price = BUNDLE_PRICES.get(count, count * 5000)
            
            tier = "all" if count == len(ALL_EXPERTS) else "bundle" if count > 1 else "single"
            save_subscription(user_id, cart, tier)
            save_fact(user_id, "cart", "[]")
            
            # Удаляем триал, если был
            if supabase:
                try:
                    supabase.table("trial_status").delete().eq("user_id", user_id).execute()
                except:
                    pass
            
            experts_str = "\n".join([f"{EXPERT_ICONS[e]} {EXPERT_NAMES[e]}" for e in cart])
            await send_message(user_id, f"""
✅ **Оплата прошла успешно!**

В вашей команде:
{experts_str}

Стоимость: {price} ₽/мес
Действует до: {(datetime.now() + timedelta(days=30)).strftime('%d.%m.%Y')}

Напишите любой вопрос — я направлю его к нужному эксперту! 🚀
""")
            return JSONResponse({"ok": True})
        
        # ============================================================
        # 4. ПРОВЕРКА VIP-КОДА
        # ============================================================
        if text.upper() in [code.upper() for code in VIP_CODES]:
            save_fact(user_id, "vip_code", text.upper())
            save_fact(user_id, "name", "Гость")  # временное имя
            
            # Активируем триал
            trial = start_trial(user_id)
            
            if trial["status"] == "active":
                await send_message(user_id, """
✨ **Код принят. Добро пожаловать в клуб.**

С этого момента у вас есть **24 часа** эксклюзивного доступа ко всем экспертам.

Представьте, что это — ваш личный кабинет в лучшем клубе мира.
Познакомьтесь с командой, задайте любые вопросы.

Через 24 часа я покажу вам, как остаться с нами навсегда.

**Команда: /experts**

Начнём? 👔
""")
            else:
                await send_message(user_id, "✅ Код активирован! Добро пожаловать в клуб AURA.")
            
            save_message(user_id, "assistant", "Код активирован, триал запущен")
            return JSONResponse({"ok": True})
        
        # ============================================================
        # 5. ОСНОВНАЯ ЛОГИКА
        # ============================================================
        vip_code = get_fact(user_id, "vip_code")
        
        if vip_code:
            save_message(user_id, "user", text)
            
            # Проверяем, есть ли активный триал или подписка
            has_subscription = get_subscription(user_id) is not None
            has_trial = has_trial_access(user_id)
            
            # Если нет ни подписки, ни триала — пробуем активировать триал
            if not has_subscription and not has_trial:
                # Проверяем, был ли уже триал
                trial_status = get_trial_status(user_id)
                if trial_status["status"] == "expired":
                    await send_message(user_id, """
⏰ Ваша демонстрация завершена.

Чтобы остаться в клубе — наймите экспертов через /add и оплатите через /pay.
""")
                    return JSONResponse({"ok": True})
                elif trial_status["status"] == "no_trial":
                    # Если триала не было — активируем
                    start_trial(user_id)
                    await send_message(user_id, """
✨ **Эксклюзивная демонстрация AURA**

Добро пожаловать в клуб.

У вас есть 24 часа, чтобы познакомиться с командой экспертов.
Все 10 экспертов доступны.

Начните с команды: /experts
Или просто задайте вопрос — я направлю его к нужному специалисту.

Наслаждайтесь. ⏳
""")
                    return JSONResponse({"ok": True})
            
            # Время (база)
            if any(phrase in text.lower() for phrase in ["сколько время", "который час", "время сейчас"]):
                user_city = get_fact(user_id, "city") or "Москва"
                await send_typing(user_id)
                await send_message(user_id, f"✦ Сейчас **{get_time_for_city(user_city)}** по местному времени ({user_city}).")
                return JSONResponse({"ok": True})
            
            # Проверяем, есть ли доступ к экспертам (триал или подписка)
            available = get_available_experts(user_id)
            has_expert_access = bool(available) or has_trial
            
            # Если есть триал — даём доступ ко всем экспертам
            if has_trial and not has_subscription:
                # Проверяем, не пора ли напомнить
                trial_status = get_trial_status(user_id)
                if trial_status.get("status") == "active" and trial_status.get("hours_left", 99) <= 2:
                    reminded = get_fact(user_id, "trial_reminded")
                    if not reminded:
                        save_fact(user_id, "trial_reminded", "true")
                        await send_message(user_id, f"""
⏰ **AURA**:
"Дружеское напоминание: ваша демонстрация заканчивается через **{trial_status.get('time_str', '0 ч 0 мин')}**.

Вы можете остаться с нами.
Наймите экспертов через /add и оплатите через /pay.

Всего хорошего. 🎯"
""")
                        return JSONResponse({"ok": True})
                
                # Определяем эксперта и отвечаем
                expert = await detect_expert(text)
                
                if expert in ALL_EXPERTS:
                    # AURA представляет эксперта
                    intro = await aura_intro(user_id, expert)
                    await send_message(user_id, intro)
                    
                    # Ответ эксперта
                    reply = await respond_with_expert(user_id, text, expert)
                    await send_typing(user_id)
                    await send_message(user_id, reply)
                    save_message(user_id, "assistant", reply)
                else:
                    # Если не определился — AURA отвечает сам
                    reply = await aura_says(user_id, text)
                    await send_typing(user_id)
                    await send_message(user_id, reply)
                    save_message(user_id, "assistant", reply)
                
                return JSONResponse({"ok": True})
            
            # Если есть подписка
            if has_subscription:
                expert = await detect_expert(text)
                
                if expert in available:
                    # AURA представляет эксперта
                    intro = await aura_intro(user_id, expert)
                    await send_message(user_id, intro)
                    
                    # Ответ эксперта
                    reply = await respond_with_expert(user_id, text, expert)
                    await send_typing(user_id)
                    await send_message(user_id, reply)
                    save_message(user_id, "assistant", reply)
                else:
                    # Нет доступа к этому эксперту
                    expert_name = EXPERT_NAMES.get(expert, expert)
                    if expert != "general":
                        await send_message(user_id, f"""
🎯 Ваш вопрос лучше всего адресовать **{EXPERT_ICONS.get(expert, '')} {expert_name}**.

Но этот эксперт пока не в вашей команде.

Нанять: /add {expert}
Посмотреть команду: /experts
""")
                    else:
                        # Если не определился — AURA отвечает сам
                        reply = await aura_says(user_id, text)
                        await send_typing(user_id)
                        await send_message(user_id, reply)
                        save_message(user_id, "assistant", reply)
                
                return JSONResponse({"ok": True})
            
            # ============================================================
            # 6. БАЗОВАЯ ЛОГИКА (ЯНДЕКС-АГЕНТЫ) — ЕСЛИ НЕТ ЭКСПЕРТОВ
            # ============================================================
            user_name = get_fact(user_id, "name") or "Гость"
            user_city = get_fact(user_id, "city") or "Москва"
            await send_typing(user_id)
            
            # Контент
            content_triggers = ["фильм", "сериал", "видео", "рецепт", "картинки", "фото", "обзор", "смотреть", "клип", "трейлер"]
            search_triggers = ["найди", "поищи", "цены", "билеты", "скидки", "новости", "погода", "курс", "стоимость"]
            analyze_triggers = ["сравни", "проанализируй", "исследуй", "изучи", "разбери"]
            reason_triggers = ["посоветуй", "что лучше", "как поступить", "выбери", "рекомендуй", "стоит ли"]
            
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
                    # Добавляем стиль AURA
                    aura_reply = f"👔 **AURA**:\n\n{packed}"
                    await send_message(user_id, aura_reply)
                    save_message(user_id, "assistant", aura_reply)
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
                        aura_reply = f"👔 **AURA**:\n\n{packed}"
                        await send_message(user_id, aura_reply)
                        save_message(user_id, "assistant", aura_reply)
                        return JSONResponse({"ok": True})
                except Exception as e:
                    logger.error(f"❌ Ошибка агента Яндекса: {e}")
                
                # Fallback на 2ГИС
                result = await search_organization(text, get_fact(user_id, "city") or "Москва")
                if result and "error" not in result:
                    reply = f"👔 **AURA**:\n\n✦ **{result['name']}**\n\n▸ Адрес: {result['address']}\n▸ Телефон: {', '.join(result['phones'][:3])}\n▸ Сайт: {result['site']}"
                    await send_message(user_id, reply)
                    save_message(user_id, "assistant", reply)
                else:
                    await send_message(user_id, f"✦ По запросу «{text}» ничего не найдено. Попробуйте уточнить.")
                return JSONResponse({"ok": True})
            
            # Обычный диалог — AURA отвечает сам
            reply = await aura_says(user_id, text)
            save_message(user_id, "assistant", reply)
            await send_typing(user_id)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})
        
        # ============================================================
        # 7. НЕ VIP — ПРОСИМ КОД
        # ============================================================
        else:
            await send_message(user_id, "🔒 **Неверный код.** Доступ запрещён.")
            return JSONResponse({"ok": True})
    
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

# ============================================================
# ЗАПУСК
# ============================================================
@app.get("/")
async def root():
    return {"status": "AURA VIP 2.0 работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
