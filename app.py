import os
import tempfile
import json
import httpx
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
import requests
import logging

from agents.searcher import Searcher
from agents.analyzer import Analyzer
from agents.responder import Responder
from utils.helpers import get_current_time, extract_city_from_query

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

logger.info("🚀 AURA — VIP-ВЕРСИЯ (ФИНАЛ)")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase подключён")
    except Exception as e:
        logger.error(f"❌ Ошибка Supabase: {e}")

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

searcher = Searcher()
analyzer = Analyzer()
responder = Responder(deepseek)

app = FastAPI()

# ============================================================
# НОВОСТНОЙ АГЕНТ
# ============================================================

class NewsReader:
    async def get_news(self, query: str, limit: int = 5) -> list:
        keywords = [w.lower() for w in query.split() if len(w) > 2]
        if not keywords:
            return []
        
        all_news = []
        for keyword in keywords[:2]:
            url = f"https://news.yandex.ru/search?text={keyword}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        items = re.findall(r'<a class="mg-card__link" href="([^"]+)"[^>]*>([^<]+)</a>', response.text)
                        for link, title in items[:5]:
                            if any(kw in title.lower() for kw in keywords):
                                full_link = f"https://news.yandex.ru{link}" if link.startswith('/') else link
                                all_news.append({
                                    "title": title.strip(),
                                    "link": full_link,
                                    "source": "Яндекс.Новости"
                                })
                        if len(all_news) >= limit:
                            break
            except Exception as e:
                logger.error(f"❌ Ошибка парсинга новостей: {e}")
        
        return all_news[:limit]

news_reader = NewsReader()

# ============================================================
# БАЗА ДАННЫХ
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

def get_recent_history(user_id, limit=50):
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

# ============================================================
# ГИБРИДНАЯ ПАМЯТЬ (последние сообщения + поиск по истории)
# ============================================================

async def get_full_context(user_id: str, query: str, limit_recent: int = 20) -> list:
    """Гибридный контекст: последние N сообщений + старые по ключевым словам"""
    recent = get_recent_history(user_id, limit_recent)
    
    keywords = [w for w in query.lower().split() if len(w) > 3]
    old_messages = []
    if keywords and supabase:
        try:
            conditions = [f"content.ilike.%{kw}%" for kw in keywords]
            query_builder = supabase.table("history")\
                .select("role, content, created_at")\
                .eq("user_id", user_id)\
                .order("created_at", desc=True)\
                .limit(30)
            for cond in conditions:
                query_builder = query_builder.or_(cond)
            res = query_builder.execute()
            old_messages = res.data if res.data else []
        except Exception as e:
            logger.error(f"❌ Ошибка поиска в истории: {e}")
    
    seen = set()
    all_messages = []
    for msg in recent + old_messages:
        key = (msg["content"], msg["created_at"])
        if key not in seen:
            seen.add(key)
            all_messages.append(msg)
    
    all_messages.sort(key=lambda x: x["created_at"])
    return all_messages

# ============================================================
# ГЕНЕРАЦИЯ ПРИВЕТСТВИЯ (РАЗ В ДЕНЬ)
# ============================================================

async def generate_greeting(user_name: str, user_city: str) -> str:
    try:
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 12:
            time_of_day = "утро"
        elif 12 <= hour < 18:
            time_of_day = "день"
        else:
            time_of_day = "вечер"
        
        prompt = f"""Ты — AURA. Придумай короткое живое приветствие для пользователя {user_name or "друг"} из {user_city or "Белово"}.
Сейчас {time_of_day}.
Напиши 1–2 предложения, с эмодзи, без шаблонов. Разное каждый раз.
Примеры: «Доброе утро, Вадим! Как спалось? ☀️», «Привет! Чем займёмся сегодня? 😊», «Вечер в самом разгаре, Вадим. Как дела? 🔥»"""
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.9,
            max_tokens=60,
            timeout=10
        )
        greeting = response.choices[0].message.content.strip()
        return greeting if greeting else f"Привет, {user_name or 'друг'}! Рад тебя видеть 😊"
    except Exception as e:
        logger.error(f"❌ Ошибка генерации приветствия: {e}")
        return f"Привет, {user_name or 'друг'}! Как дела? 😊"

# ============================================================
# ДИАЛОГ
# ============================================================

async def deepseek_chat_with_context(text, history, user_name, user_city, context="", is_first_today=False):
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-30:]])
    
    if is_first_today:
        greeting_instruction = "Ты можешь начать с короткого приветствия, но не перебарщивай."
    else:
        greeting_instruction = "НЕ здоровайся. Сразу переходи к делу."
    
    system_prompt = f"""Ты — AURA. Ты живой собеседник и друг, а не робот.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История общения (последние сообщения):
{history_text}

КОНТЕКСТ:
{context if context else "Нет дополнительного контекста"}

ПРАВИЛА ОБЩЕНИЯ:
1. Отвечай как живой человек — тепло, вовлечённо, по делу.
2. Если знаешь ответ — дай чётко и коротко (2–4 предложения).
3. Если не знаешь — честно скажи «не нашёл» и предложи уточнить. НЕ выдумывай.
4. Используй 1–2 эмодзи, не больше.
5. Учитывай всю историю разговора — не теряй нить.
6. Не повторяйся, не мусоль.
7. Отвечай завершённо, не обрывай мысль.
8. {greeting_instruction}
"""
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-30:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": text})
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.85,
            max_tokens=600,
            timeout=30
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return "😅 Что-то пошло не так. Попробуй ещё раз."

# ============================================================
# ГОЛОС И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
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

async def send_chat_action(chat_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5
        )
    except:
        pass

async def send_message(chat_id, text):
    await send_chat_action(chat_id)
    if not text:
        text = "😅 Что-то пошло не так. Попробуй ещё раз."
    if len(text) > 4000:
        text = text[:3997] + "..."
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=30
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

# ============================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================

async def deepseek_process(user_id, text):
    try:
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")
        
        # === ПРОВЕРКА НА НОВЫЙ ДЕНЬ ===
        today = datetime.now().date().isoformat()
        last_greeting = get_fact(user_id, "last_greeting_date")
        is_first_today = (last_greeting != today)
        
        if is_first_today:
            save_fact(user_id, "last_greeting_date", today)
            greeting = await generate_greeting(user_name, user_city)
            save_message(user_id, "assistant", greeting)
        else:
            greeting = None
        
        # === ПРОСТЫЕ КОМАНДЫ ===
        if any(word in text.lower() for word in ["время", "сколько времени", "который час"]):
            reply = get_current_time()
            return f"{greeting}\n\n{reply}" if greeting else reply
        
        if any(word in text.lower() for word in ["как меня зовут", "моё имя"]):
            reply = f"Тебя зовут **{user_name}** 😊" if user_name else "Я пока не знаю твоего имени — расскажешь?"
            return f"{greeting}\n\n{reply}" if greeting else reply
        
        if any(word in text.lower() for word in ["где я живу", "мой город"]):
            reply = f"Ты из **{user_city}** 😊" if user_city else "Я пока не знаю, откуда ты — расскажи, если хочешь!"
            return f"{greeting}\n\n{reply}" if greeting else reply
        
        # === ГИБРИДНЫЙ КОНТЕКСТ (память) ===
        history = await get_full_context(user_id, text, limit_recent=20)
        
        # === НОВОСТИ ===
        news_triggers = ["новост", "сегодня", "произошл", "случил", "событи", "склады", "атак", "пожар", "взрыв", "трамп", "мобилизац"]
        if any(word in text.lower() for word in news_triggers):
            logger.info(f"📰 Новости: '{text}'")
            news = await news_reader.get_news(text, limit=5)
            if news:
                context = "📰 Новости:\n\n" + "\n".join([f"• {item['title']}\n  Ссылка: {item['link']}" for item in news])
                reply = await deepseek_chat_with_context(text, history, user_name, user_city, context, is_first_today)
                return f"{greeting}\n\n{reply}" if greeting else reply
            else:
                reply = "😊 По этой теме свежих новостей не нашёл. Попробуй уточнить!"
                return f"{greeting}\n\n{reply}" if greeting else reply
        
        # === ПОИСК ===
        search_triggers = [
            "найди", "поищи", "найти", "покажи", "где", "сайт", "фильм", 
            "клиника", "адрес", "маршрут", "ссылка", "цены", "билеты", 
            "купить", "скидки", "товар", "отель", "ресторан", "погода",
            "машина", "квартира", "дом", "работа", "вакансия", "курс",
            "видео", "кино", "сериал", "онлайн", "расстояние"
        ]
        if any(word in text.lower() for word in search_triggers):
            logger.info(f"🔍 Поиск: '{text}'")
            query = text
            if user_city and not any(city in query.lower() for city in ["москва", "спб", "сочи", "казань", "белово"]):
                query = f"{text} {user_city}"
            
            results = await searcher.search(query, max_results=15)
            
            if results:
                context = "🔍 Результаты поиска:\n\n" + "\n".join([
                    f"{i+1}. {r['title']}" + (f" — {r['price']} ₽" if r.get('price', 0) > 0 else "")
                    for i, r in enumerate(results[:5])
                ])
                reply = await deepseek_chat_with_context(text, history, user_name, user_city, context, is_first_today)
                return f"{greeting}\n\n{reply}" if greeting else reply
            else:
                reply = "😊 Не нашёл ничего по этому запросу. Попробуй переформулировать!"
                return f"{greeting}\n\n{reply}" if greeting else reply
        
        # === ОБЫЧНЫЙ ДИАЛОГ ===
        reply = await deepseek_chat_with_context(text, history, user_name, user_city, "", is_first_today)
        return f"{greeting}\n\n{reply}" if greeting else reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return "😅 Что-то пошло не так. Попробуй ещё раз!"

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
        user_id = str(msg["from"]["id"])
        text = None
        
        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    text = transcribe_audio(audio_url)
                    if not text:
                        await send_message(user_id, "⚠️ Не удалось распознать голос. Попробуй сказать чётче!")
                        return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса. Попробуй написать!")
                return JSONResponse({"ok": True})
        
        if "text" in msg:
            text = msg["text"].strip()
        if not text:
            return JSONResponse({"ok": True})
        
        save_message(user_id, "user", text)
        reply = await deepseek_process(user_id, text)
        if not reply:
            reply = "😅 Что-то пошло не так. Попробуй ещё раз!"
        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA — VIP-ВЕРСИЯ (ФИНАЛ)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
