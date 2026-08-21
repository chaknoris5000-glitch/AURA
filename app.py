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
# КОНФИГУРАЦИЯ (ТВОЯ — НЕ ТРОГАТЬ)
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
user_states = {}

# ============================================================
# VIP-КОДЫ (ДОБАВЛЕНО)
# ============================================================
VIP_CODES = ["AURA-001", "AURA-002", "AURA-003", "ADMIN", "TEST"]

# ============================================================
# ТОП-10 ЭКСПЕРТОВ (ДОБАВЛЕНО)
# ============================================================
EXPERT_ICONS = {
    "psychologist": "🧠", "mentor": "📚", "lawyer": "⚖️", "doctor": "🏥",
    "finance": "💰", "coach": "🎯", "investor": "📈", "realty": "🏢",
    "tax": "🧾", "travel": "✈️"
}

EXPERT_NAMES = {
    "psychologist": "Психолог", "mentor": "Наставник", "lawyer": "Юрист",
    "doctor": "Доктор", "finance": "Финансовый советник", "coach": "Коуч",
    "investor": "Инвестиционный аналитик", "realty": "Недвижимость-эксперт",
    "tax": "Налоговый консультант", "travel": "Путешественник-консьерж"
}

EXPERT_PERSONALITIES = {
    "psychologist": {"name": "Доктор Эмилия Харрис", "style": "Мягкий, эмпатичный", "phrase": "Расскажите, что вас беспокоит.", "icon": "🧠"},
    "mentor": {"name": "Александр Ветров", "style": "Прямой, честный", "phrase": "Что ты сделал сегодня?", "icon": "📚"},
    "lawyer": {"name": "Елена Соболева", "style": "Чёткий, по делу", "phrase": "Есть нюансы.", "icon": "⚖️"},
    "doctor": {"name": "Доктор Михаил Орлов", "style": "Спокойный, заботливый", "phrase": "Не паникуйте.", "icon": "🏥"},
    "finance": {"name": "Ирина Волкова", "style": "Прагматичный", "phrase": "Смотрите на цифры.", "icon": "💰"},
    "coach": {"name": "Денис Соколов", "style": "Заряжающий", "phrase": "Докажи мне.", "icon": "🎯"},
    "investor": {"name": "Маркус Ван дер Меер", "style": "Хладнокровный", "phrase": "Смотрите на цифры.", "icon": "📈"},
    "realty": {"name": "Анна Громова", "style": "Деловой, с душой", "phrase": "Это ваше пространство.", "icon": "🏢"},
    "tax": {"name": "Сергей Белов", "style": "Чёткий, законный", "phrase": "Налоги можно оптимизировать.", "icon": "🧾"},
    "travel": {"name": "Виктория Ноубл", "style": "Заботливый", "phrase": "Я всё организую.", "icon": "✈️"}
}

EXPERT_PROMPTS = {
    "psychologist": "Ты — психолог. Стиль — мягкий, эмпатичный. Спрашивай 'Как ты себя чувствуешь?'",
    "mentor": "Ты — наставник. Стиль — прямой, честный. Спрашивай 'Какой следующий шаг?'",
    "lawyer": "Ты — юрист. Стиль — чёткий, по делу. Добавляй 'Обратитесь к юристу'.",
    "doctor": "Ты — доктор. Стиль — спокойный. Дисклеймер: 'Я не заменяю врача'.",
    "finance": "Ты — финсоветник. Стиль — прагматичный. Дисклеймер: 'Не инвестрекомендация'.",
    "coach": "Ты — коуч. Стиль — заряжающий. Спрашивай 'Что сделаешь сегодня?'",
    "investor": "Ты — инвест-аналитик. Стиль — хладнокровный. Дисклеймер: 'Не рекомендация'.",
    "realty": "Ты — эксперт по недвижимости. Стиль — деловой.",
    "tax": "Ты — налоговый консультант. Стиль — чёткий.",
    "travel": "Ты — тревел-консьерж. Стиль — заботливый."
}

ALL_EXPERTS = list(EXPERT_NAMES.keys())
EXPERT_PRICES = {e: 5000 for e in ALL_EXPERTS}
BUNDLE_PRICES = {1: 5000, 2: 9000, 3: 12750, 4: 16000, 5: 18750, 6: 21000, 7: 24500, 8: 28000, 9: 31500, 10: 35000}

# ============================================================
# ЗАЩИТА ОТ ПОВТОРНЫХ ЗАПРОСОВ (ТВОЯ — НЕ ТРОГАТЬ)
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
# КЕШИРОВАНИЕ (ТВОЁ — НЕ ТРОГАТЬ)
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
# ПАМЯТЬ, ПОРТРЕТ (ТВОЯ — НЕ ТРОГАТЬ)
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
# ПОДПИСКИ И ТРИАЛ (ДОБАВЛЕНО)
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
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения подписки: {e}")

def get_available_experts(user_id: int) -> list:
    sub = get_subscription(user_id)
    if not sub:
        return []
    if sub.get("tier") == "all":
        return ALL_EXPERTS.copy()
    return sub.get("experts", [])

def start_trial(user_id: int) -> dict:
    expires_at = datetime.now() + timedelta(hours=24)
    if not supabase:
        return {"status": "error"}
    try:
        supabase.table("trial_status").delete().eq("user_id", user_id).execute()
        supabase.table("trial_status").insert({
            "user_id": user_id,
            "trial_started": datetime.now().isoformat(),
            "trial_ended": expires_at.isoformat(),
            "is_active": True
        }).execute()
        return {"status": "active", "expires_at": expires_at}
    except Exception as e:
        logger.error(f"❌ Ошибка активации триала: {e}")
        return {"status": "error"}

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
        return {"status": "active", "expires_at": expires_at, "time_str": f"{hours_left} ч {minutes_left} мин"}
    except Exception as e:
        logger.error(f"❌ Ошибка проверки триала: {e}")
        return {"status": "error"}

def has_trial_access(user_id: int) -> bool:
    status = get_trial_status(user_id)
    return status.get("status") == "active"

# ============================================================
# РАСПОЗНАВАНИЕ ГОЛОСА (ТВОЯ — НЕ ТРОГАТЬ)
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
# РАСПОЗНАВАНИЕ ИЗОБРАЖЕНИЙ (ТВОЯ — НЕ ТРОГАТЬ)
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
# ВЫЗОВ АГЕНТОВ ЯНДЕКСА (ТВОЯ — НЕ ТРОГАТЬ)
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

# ============================================================
# УПАКОВКА ОТВЕТА (ТВОЯ — НЕ ТРОГАТЬ, ДОБАВЛЕНЫ ССЫЛКИ)
# ============================================================
async def pack_response(raw_text: str, user_name: str = "", user_city: str = "", platform: str = "", search_query: str = "") -> str:
    try:
        link = ""
        if platform and search_query:
            query_encoded = search_query.replace(' ', '+')
            if platform == "yandex_video":
                link = f"\n\n🔗 [Смотреть на Яндекс.Видео](https://yandex.ru/video/search?text={query_encoded})"
            elif platform == "youtube":
                link = f"\n\n🔗 [Смотреть на YouTube](https://www.youtube.com/results?search_query={query_encoded})"
            elif platform == "yandex_images":
                link = f"\n\n🖼️ [Смотреть картинки](https://yandex.ru/images/search?text={query_encoded})"
            elif platform == "wildberries":
                link = f"\n\n🛒 [Купить на Wildberries](https://www.wildberries.ru/catalog/0/search.aspx?search={query_encoded})"
            elif platform == "ozon":
                link = f"\n\n🛒 [Купить на Ozon](https://www.ozon.ru/search/?text={query_encoded})"
            elif platform == "aviasales":
                link = f"\n\n✈️ [Найти билеты](https://www.aviasales.ru/search?q={query_encoded})"
        
        prompt = f"""
Ты — AURA. Твой стиль — Тони Старк: уверенный, с иронией, живой.

Перед тобой сырой ответ поискового агента. Преврати его в красивый, живой ответ.

ПРАВИЛА:
1. **ОТВЕЧАЙ КОРОТКО:** максимум 3–4 предложения.
2. **СТРУКТУРА:** разбивай ответ на 2–3 абзаца с пустыми строками.
3. **МАРКЕРЫ:** ✅ — для готовых решений, 💎 — для лучшего варианта, ⚡ — для советов.
4. **ЖИРНЫЙ ШРИФТ:** выделяй цены, даты, ключевые цифры.
5. **ДУША:** используй лёгкую иронию, сарказм, эмпатию.
6. **ЭМОДЗИ:** добавляй 1–2 по теме.
7. **ССЫЛКА:** обязательно добавь ссылку в конце.

Используй имя пользователя: {user_name or "Гость"}.

Сырой ответ:
{raw_text}

Твой ответ (живой, структурированный, со ссылкой):
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=250,
            timeout=20
        )
        result = response.choices[0].message.content
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка упаковки: {e}")
        return raw_text

# ============================================================
# ОПРЕДЕЛЕНИЕ ПЛАТФОРМЫ (ТВОЯ — НЕ ТРОГАТЬ, РАСШИРЕНА)
# ============================================================
def detect_content_platform(text: str) -> dict:
    try:
        prompt = f"""
Определи, где лучше искать контент по запросу пользователя: "{text}"

Правила:
- Если это фильм, сериал, трейлер, клип → ищи в Яндекс.Видео
- Если это рецепт, обзор, как приготовить, как сделать → ищи на YouTube
- Если это картинки, фото, изображения → ищи в Яндекс.Картинках
- Если это товар (одежда, техника, обувь) → ищи на Wildberries или Ozon
- Если это билеты, отели, путешествия → ищи на Aviasales или Яндекс.Путешествия
- Если это клиника, больница, организация → платформа не нужна, ссылки не будет

Верни JSON:
{{
    "platform": "yandex_video | youtube | yandex_images | wildberries | ozon | aviasales | none",
    "search_query": "уточнённый запрос для поиска"
}}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=120,
            timeout=10
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"❌ Ошибка определения платформы: {e}")
        return {"platform": "yandex_video", "search_query": text}

# ============================================================
# ТОЧНОЕ ВРЕМЯ (ТВОЯ — НЕ ТРОГАТЬ)
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
# ИСТОРИЯ (ТВОЯ — НЕ ТРОГАТЬ)
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
# ИЗВЛЕЧЕНИЕ ФАКТОВ (ТВОЯ — НЕ ТРОГАТЬ)
# ============================================================
async def extract_facts(text: str) -> dict:
    for attempt in range(2):
        try:
            prompt = f"""
Проанализируй сообщение пользователя: "{text}"
Определи, есть ли в нём информация о самом пользователе (факты, предпочтения, привычки).
Верни JSON с полем "facts" — объект, где ключи — это названия полей из таблицы user_portrait, а значения — извлечённые факты.
Если фактов нет — верни пустой объект.

Пример ответа:
{{"facts": {{"favorite_cuisine": "итальянская", "hobbies": ["фотография", "путешествия"]}}}}
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
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ Ошибка JSON в extract_facts: {e}")
            if attempt == 1:
                return {}
        except Exception as e:
            logger.error(f"❌ Ошибка извлечения фактов: {e}")
            return {}
    return {}

# ============================================================
# ДЕТЕКТОР ЭМОЦИЙ (ТВОЯ — НЕ ТРОГАТЬ)
# ============================================================
async def detect_emotion(text: str) -> dict:
    for attempt in range(2):
        try:
            prompt = f"""
Проанализируй эмоцию в сообщении пользователя: "{text}"
Верни строгий JSON с двумя полями:
- emotion: одна из (радость, грусть, гнев, страх, удивление, отвращение, спокойствие)
- confidence: число от 0 до 1
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
        except:
            return {"emotion": "спокойствие", "confidence": 0.5}
    return {"emotion": "спокойствие", "confidence": 0.5}

# ============================================================
# ЭМОЦИИ, ТРИАЛ (ТВОЯ — НЕ ТРОГАТЬ)
# ============================================================
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

def get_trial_status_old(user_id):
    if not supabase:
        return None
    try:
        res = supabase.table("trial_status").select("*").eq("user_id", user_id).execute()
        return res.data[0] if res.data else None
    except:
        return None

def get_trial_info(user_id):
    trial = get_trial_status_old(user_id)
    if not trial:
        return {"status": "no_trial", "days_left": 0}
    end_date = datetime.fromisoformat(trial["trial_ended"])
    now = datetime.now(pytz.UTC)
    days_left = (end_date - now).days
    if days_left < 0:
        return {"status": "expired", "days_left": 0}
    elif days_left == 0:
        return {"status": "last_day", "days_left": 0}
    else:
        return {"status": "active", "days_left": days_left}

def save_trial_status_old(user_id, started, ended):
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
# 2ГИС (ТВОЯ — НЕ ТРОГАТЬ)
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
# МАРШРУТИЗАТОР ЭКСПЕРТОВ (ДОБАВЛЕНО)
# ============================================================
async def detect_expert(text: str) -> str:
    try:
        prompt = f"""
Проанализируй запрос: "{text}"
Определи эксперта из списка: psychologist, mentor, lawyer, doctor, finance, coach, investor, realty, tax, travel
Верни JSON: {{"expert": "psychologist"}}
"""
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.3,
            max_tokens=80,
            timeout=10
        )
        return json.loads(response.choices[0].message.content).get("expert", "general")
    except:
        return "general"

async def aura_intro(user_id: int, expert: str) -> str:
    personality = EXPERT_PERSONALITIES.get(expert, {})
    name = personality.get('name', expert)
    phrase = personality.get('phrase', '')
    user_name = get_fact(user_id, "name") or "Гость"
    intros = [
        f"👔 **AURA**: {user_name}, познакомься — **{name}**. {phrase}",
        f"🧠 **AURA**: Это **{name}**. {phrase}",
        f"🤖 **AURA**: {user_name}, передаю слово **{name}**. {phrase}"
    ]
    return random.choice(intros)

async def respond_with_expert(user_id: int, text: str, expert: str) -> str:
    if expert == "general" or expert not in EXPERT_PROMPTS:
        return await process_vip_request(user_id, text)
    
    personality = EXPERT_PERSONALITIES.get(expert, {})
    expert_prompt = EXPERT_PROMPTS[expert]
    user_name = get_fact(user_id, "name") or "Гость"
    history = get_recent_history(user_id, limit=5)
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-5:]])
    
    full_prompt = f"""
Ты — {personality.get('name', expert)}.
Твой стиль: {personality.get('style', 'профессиональный')}.
Твоя фраза: "{personality.get('phrase', '')}"

{expert_prompt}

Имя: {user_name}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ КОРОТКО (3-4 предложения):
1. Приветствие в своём стиле.
2. Ответ по делу.
3. Вопрос для продолжения.
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": full_prompt}],
            temperature=0.8,
            max_tokens=250,
            timeout=15
        )
        reply = response.choices[0].message.content
        icon = personality.get('icon', '🧠')
        name = personality.get('name', expert)
        return f"{icon} **{name}**: {reply}"
    except Exception as e:
        logger.error(f"❌ Ошибка эксперта {expert}: {e}")
        return await process_vip_request(user_id, text)

# ============================================================
# ОСНОВНАЯ ЛОГИКА (ТВОЯ — НЕ ТРОГАТЬ, ДОБАВЛЕНА VIP-ВЕТКА)
# ============================================================
async def deepseek_interview(user_id: int, text: str, step: int, history: list, emotion: str = "спокойствие") -> dict:
    emotion_instruction = ""
    if emotion in ["грусть", "страх"]:
        emotion_instruction = "Отвечай мягко, с поддержкой."
    elif emotion == "гнев":
        emotion_instruction = "Отвечай спокойно, без иронии."
    elif emotion == "радость":
        emotion_instruction = "Отвечай живо, с юмором."

    user_name = get_fact(user_id, "name")
    user_city = get_fact(user_id, "city")
    portrait = get_portrait(user_id)

    trial_info = get_trial_info(user_id)
    trial_context = ""
    if trial_info["status"] == "active":
        trial_context = f"У пользователя активный бесплатный триал, осталось {trial_info['days_left']} дней."
    elif trial_info["status"] == "last_day":
        trial_context = "У пользователя последний день бесплатного триала."
    elif trial_info["status"] == "expired":
        trial_context = "У пользователя нет активного триала."
    else:
        trial_context = "У пользователя нет триала."

    name_instruction = f"Зовут {user_name}." if user_name else ""
    city_instruction = f"Город: {user_city}." if user_city else ""

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
        if portrait.get('budget_travel'): parts.append(f"бюджет на поездку: {portrait['budget_travel']} ₽")
        if parts:
            portrait_context = "ПОРТРЕТ: " + ", ".join(parts) + "."

    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-15:]])

    prompt = f"""
Ты — AURA. Твой стиль — Тони Старк: уверенный, с иронией, живой.

{emotion_instruction}

{portrait_context}

{trial_context}

ПРАВИЛА ОТВЕТОВ (ОБЯЗАТЕЛЬНО):
1. **ОТВЕЧАЙ КОРОТКО:** максимум 3–4 предложения.
2. **СТРУКТУРА:** разбивай ответ на 2–3 абзаца с пустыми строками.
3. **МАРКЕРЫ:** ✅ — для готовых решений, 💎 — для лучшего варианта, ⚡ — для советов.
4. **ЖИРНЫЙ ШРИФТ:** выделяй цены, даты, ключевые цифры.
5. **ДУША:** используй лёгкую иронию, сарказм, эмпатию. Отвечай как человек, а не робот.
6. **ЭМОДЗИ:** добавляй 1–2 по теме (✈️, 🍽️, 🎬, 🚀, 🏖️).
7. **НЕ ИСПОЛЬЗУЙ ТАБЛИЦЫ.** Только текст с маркерами и абзацами.
8. **ТРИАЛ:** предлагай только после step >= 10 и score > 60. Не раздавай налево.

Используй портрет для персонализации.

{name_instruction}
{city_instruction}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ ТОЛЬКО JSON:
{{
    "reply": "твой ответ (живой, структурированный, без таблиц)",
    "score": 0..100,
    "offer_trial": false
}}
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=300,
            timeout=20
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return {"reply": "😅 Не понял, перефразируй.", "score": 0, "offer_trial": False}

async def process_vip_request(user_id: int, text: str) -> str:
    """Обработка запроса VIP-пользователя (с экспертами)"""
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
Ты — AURA, живой и душевный помощник. Отвечай коротко, с душой и уместными эмодзи.
Имя пользователя: {user_name}
Город: {user_city}
{portrait_context}

ИСТОРИЯ:
{history_text}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ (3-4 предложения, с эмодзи, без таблиц):
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7,
            max_tokens=200,
            timeout=20
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return "Извините, произошла ошибка. Попробуйте перефразировать запрос."

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ TELEGRAM (ТВОИ — НЕ ТРОГАТЬ)
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
# WEBHOOK (ТВОЙ — НЕ ТРОГАТЬ, ДОБАВЛЕНЫ КОМАНДЫ)
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
        # МЕДИА (ТВОЁ — НЕ ТРОГАТЬ)
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
        # ЗАЩИТА ОТ ПОВТОРОВ (ТВОЯ — НЕ ТРОГАТЬ)
        # ============================================================
        if is_duplicate(user_id, text):
            logger.warning(f"⚠️ Повторный запрос от {user_id}: {text}")
            return JSONResponse({"ok": True})

        # ============================================================
        # КОМАНДЫ (ДОБАВЛЕНЫ НОВЫЕ)
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
        # VIP-КОМАНДЫ (ДОБАВЛЕНО)
        # ============================================================
        if text.lower() == "/experts":
            vip_code = get_fact(user_id, "vip_code")
            if not vip_code:
                await send_message(user_id, "🔒 Доступ запрещён. Введите VIP-код.")
                return JSONResponse({"ok": True})
            
            trial = get_trial_status(user_id)
            has_trial = trial.get("status") == "active"
            available = get_available_experts(user_id)
            
            if has_trial:
                available = ALL_EXPERTS.copy()
            
            available_str = "\n".join([f"{EXPERT_ICONS[e]} {EXPERT_NAMES[e]} ✅" for e in available]) if available else "❌ Нет активных экспертов"
            trial_status = ""
            if has_trial:
                trial_info = get_trial_status(user_id)
                trial_status = f"\n⏳ *Триал активен:* {trial_info.get('time_str', '0 ч 0 мин')} осталось"
            
            await send_message(user_id, f"""
✦ **Ваша команда AURA**

Штат: {len(available)} из 10

{available_str}
{trial_status}

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

Чтобы нанять: /add психолог
Оплатить: /pay
""")
            return JSONResponse({"ok": True})
        
        if text.lower().startswith("/add"):
            vip_code = get_fact(user_id, "vip_code")
            if not vip_code:
                await send_message(user_id, "🔒 Доступ запрещён. Введите VIP-код.")
                return JSONResponse({"ok": True})
            
            expert_raw = text[4:].strip()
            if not expert_raw:
                await send_message(user_id, "❌ Укажите эксперта. Например: /add психолог")
                return JSONResponse({"ok": True})
            
            expert_key = expert_raw.lower()
            if expert_key not in EXPERT_NAMES:
                await send_message(user_id, f"❌ Такого эксперта нет. Доступны: {', '.join(EXPERT_NAMES.keys())}")
                return JSONResponse({"ok": True})
            
            cart = json.loads(get_fact(user_id, "cart") or "[]")
            if expert_key in cart:
                await send_message(user_id, f"✅ {EXPERT_ICONS[expert_key]} {EXPERT_NAMES[expert_key]} уже в корзине")
                return JSONResponse({"ok": True})
            
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
            vip_code = get_fact(user_id, "vip_code")
            if not vip_code:
                await send_message(user_id, "🔒 Доступ запрещён. Введите VIP-код.")
                return JSONResponse({"ok": True})
            
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
            vip_code = get_fact(user_id, "vip_code")
            if not vip_code:
                await send_message(user_id, "🔒 Доступ запрещён. Введите VIP-код.")
                return JSONResponse({"ok": True})
            
            save_fact(user_id, "cart", "[]")
            await send_message(user_id, "🛒 Корзина очищена")
            return JSONResponse({"ok": True})
        
        if text.lower() == "/pay":
            vip_code = get_fact(user_id, "vip_code")
            if not vip_code:
                await send_message(user_id, "🔒 Доступ запрещён. Введите VIP-код.")
                return JSONResponse({"ok": True})
            
            cart = json.loads(get_fact(user_id, "cart") or "[]")
            if not cart:
                await send_message(user_id, "🛒 Корзина пуста. Наймите экспертов через /add")
                return JSONResponse({"ok": True})
            
            count = len(cart)
            price = BUNDLE_PRICES.get(count, count * 5000)
            
            tier = "all" if count == len(ALL_EXPERTS) else "bundle" if count > 1 else "single"
            save_subscription(user_id, cart, tier)
            save_fact(user_id, "cart", "[]")
            
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
        # ВРЕМЯ (ТВОЁ — НЕ ТРОГАТЬ)
        # ============================================================
        time_phrases = [
            "сколько время", "который час", "точное время",
            "часы покажи", "время сейчас", "какое время",
            "сколько сейчас время", "который сейчас час",
            "время в белово", "время в москве", "время по москве",
            "время по белово", "скажи сколько время",
            "подскажи сколько время", "напомни сколько время"
        ]
        if any(phrase in text.lower() for phrase in time_phrases):
            user_city = get_fact(user_id, "city") or "Москва"
            await send_typing(user_id)
            await send_message(user_id, f"⏰ Сейчас {get_time_for_city(user_city)} по местному времени ({user_city}).")
            return JSONResponse({"ok": True})

        # ============================================================
        # ПРОВЕРКА VIP-КОДА (ДОБАВЛЕНО)
        # ============================================================
        if text.upper() in [code.upper() for code in VIP_CODES]:
            save_fact(user_id, "vip_code", text.upper())
            
            # Активируем триал 24 часа
            trial = start_trial(user_id)
            
            await send_message(user_id, """
✨ **Код принят. Добро пожаловать в клуб.**

У вас есть **24 часа** эксклюзивного доступа ко всем экспертам.

**Команда: /experts**

Начнём? 👔
""")
            save_message(user_id, "assistant", "Код активирован, триал запущен")
            return JSONResponse({"ok": True})

        # ============================================================
        # ОСНОВНАЯ ЛОГИКА (ТВОЯ — НЕ ТРОГАТЬ, ДОБАВЛЕНА VIP-ВЕТКА)
        # ============================================================
        vip_code = get_fact(user_id, "vip_code")
        
        # === VIP-ПОЛЬЗОВАТЕЛЬ ===
        if vip_code:
            save_message(user_id, "user", text)
            
            # Проверяем доступ к экспертам (триал или подписка)
            has_subscription = get_subscription(user_id) is not None
            has_trial = has_trial_access(user_id)
            available = get_available_experts(user_id)
            
            # Если нет ни подписки, ни триала — пробуем активировать триал
            if not has_subscription and not has_trial:
                trial_status = get_trial_status(user_id)
                if trial_status["status"] == "expired":
                    await send_message(user_id, """
⏰ Демонстрация завершена.

Наймите экспертов: /add
Оплатить: /pay
""")
                    return JSONResponse({"ok": True})
                elif trial_status["status"] == "no_trial":
                    start_trial(user_id)
                    await send_message(user_id, """
✨ **Эксклюзивная демонстрация AURA**

24 часа доступа ко всем экспертам.

Начните: /experts
Или просто задайте вопрос.
""")
                    return JSONResponse({"ok": True})
            
            # Если есть триал или подписка — даём доступ к экспертам
            if has_trial or has_subscription:
                # Определяем эксперта
                expert = await detect_expert(text)
                
                # Проверяем, есть ли доступ к этому эксперту
                if has_trial:
                    # Триал даёт доступ ко всем
                    if expert in ALL_EXPERTS:
                        intro = await aura_intro(user_id, expert)
                        await send_message(user_id, intro)
                        reply = await respond_with_expert(user_id, text, expert)
                        await send_typing(user_id)
                        await send_message(user_id, reply)
                        save_message(user_id, "assistant", reply)
                    else:
                        # Если не определился — AURA отвечает сам
                        reply = await process_vip_request(user_id, text)
                        await send_typing(user_id)
                        await send_message(user_id, reply)
                        save_message(user_id, "assistant", reply)
                elif has_subscription:
                    # Подписка — только купленные эксперты
                    if expert in available:
                        intro = await aura_intro(user_id, expert)
                        await send_message(user_id, intro)
                        reply = await respond_with_expert(user_id, text, expert)
                        await send_typing(user_id)
                        await send_message(user_id, reply)
                        save_message(user_id, "assistant", reply)
                    else:
                        expert_name = EXPERT_NAMES.get(expert, expert)
                        if expert != "general":
                            await send_message(user_id, f"""
🎯 Вопрос к {EXPERT_ICONS.get(expert, '')} {expert_name}.

Нанять: /add {expert}
""")
                        else:
                            reply = await process_vip_request(user_id, text)
                            await send_typing(user_id)
                            await send_message(user_id, reply)
                            save_message(user_id, "assistant", reply)
                
                return JSONResponse({"ok": True})
            
            # ============================================================
            # БАЗОВАЯ ЛОГИКА С ЯНДЕКС-АГЕНТАМИ (ТВОЯ — НЕ ТРОГАТЬ)
            # ============================================================
            user_name = get_fact(user_id, "name") or "Гость"
            user_city = get_fact(user_id, "city") or "Москва"
            budget = get_fact(user_id, "budget_travel") or ""

            await send_typing(user_id)

            content_triggers = ["фильм", "сериал", "видео", "рецепт", "картинки", "фото", "изображения", "обзор", "как приготовить", "как сделать", "смотреть", "клип", "трейлер", "котики", "приколы"]
            search_triggers = ["найди", "поищи", "цены", "билеты", "скидки", "акции", "новости", "погода", "курс", "стоимость"]
            analyze_triggers = ["сравни", "проанализируй", "исследуй", "изучи", "разбери", "глубоко", "детально"]
            reason_triggers = ["посоветуй", "что лучше", "как поступить", "выбери", "рекомендуй", "какой вариант", "стоит ли"]

            if any(word in text.lower() for word in content_triggers):
                platform_info = detect_content_platform(text)
                platform = platform_info.get("platform", "yandex_video")
                search_query = platform_info.get("search_query", text)
                platform_map = {"yandex_video": "яндекс видео", "youtube": "youtube", "yandex_images": "яндекс картинки"}
                full_query = f"{search_query} {platform_map.get(platform, 'яндекс видео')}"
                raw_result = call_yandex_agent(AGENT_SEARCH_ID, full_query, user_name, user_city)
                if raw_result:
                    packed = await pack_response(raw_result, user_name, user_city, platform, search_query)
                    await send_message(user_id, packed)
                    save_message(user_id, "assistant", packed)
                else:
                    await send_message(user_id, "😊 Не нашёл ничего по запросу. Попробуй уточнить.")
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
                        # Определяем платформу для ссылки
                        platform_info = detect_content_platform(text)
                        platform = platform_info.get("platform", "none")
                        search_query = platform_info.get("search_query", text)
                        packed = await pack_response(raw_result, user_name, user_city, platform, search_query)
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
                    await send_message(user_id, f"😊 Не нашёл «{text}». Проверь в Яндексе или 2ГИС.")
                return JSONResponse({"ok": True})

            # Обычный диалог
            reply = await process_vip_request(user_id, text)
            save_message(user_id, "assistant", reply)
            await send_typing(user_id)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ============================================================
        # НЕ VIP — ТВОЯ ОСНОВНАЯ ЛОГИКА (НЕ ТРОГАТЬ)
        # ============================================================
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

        trial = get_trial_status_old(user_id)
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
            save_trial_status_old(user_id, datetime.now(), datetime.now() + timedelta(days=3))
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
