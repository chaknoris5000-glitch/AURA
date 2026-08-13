import os
import tempfile
import json
import httpx
import re
import xml.etree.ElementTree as ET
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
GIS_API_KEY = os.getenv("GIS_API_KEY")

logger.info("🚀 AURA — VIP-ВЕРСИЯ (С ПРОФИЛЕМ)")

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
# 2ГИС API — ПОИСК ОРГАНИЗАЦИЙ
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
                    result = {
                        "name": item.get("name", "Неизвестно"),
                        "address": item.get("address", {}).get("full_name", "Адрес не указан"),
                        "phones": [p.get("number") for p in item.get("phones", []) if p.get("number")],
                        "site": item.get("site", ""),
                        "rating": item.get("rating", {}).get("value", 0),
                        "reviews": item.get("reviews_count", 0)
                    }
                    return result
            return {"error": "Не найдено"}
    except Exception as e:
        logger.error(f"❌ Ошибка 2ГИС: {e}")
        return {"error": str(e)}

# ============================================================
# НОВОСТНОЙ АГЕНТ (RSS)
# ============================================================

class NewsReader:
    def __init__(self):
        self.rss_urls = {
            "яндекс": "https://news.yandex.ru/index.rss",
            "риа": "https://ria.ru/export/rss2/archive/index.xml",
            "тасс": "https://tass.ru/rss/v2.xml"
        }
    
    async def get_news(self, query: str, limit: int = 5) -> list:
        keywords = [w.lower() for w in query.split() if len(w) > 2]
        if not keywords:
            return []
        
        all_news = []
        for source, url in self.rss_urls.items():
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, timeout=10)
                    if response.status_code == 200:
                        root = ET.fromstring(response.text)
                        items = root.findall(".//item")
                        for item in items[:20]:
                            title = item.findtext("title", "")
                            link = item.findtext("link", "")
                            description = item.findtext("description", "")
                            pub_date = item.findtext("pubDate", "")
                            
                            text = (title + " " + description).lower()
                            if any(kw in text for kw in keywords):
                                all_news.append({
                                    "title": title,
                                    "link": link,
                                    "description": description[:300],
                                    "source": source,
                                    "date": pub_date
                                })
            except Exception as e:
                logger.error(f"❌ Ошибка RSS {source}: {e}")
        
        all_news.sort(key=lambda x: x.get("date", ""), reverse=True)
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

def search_all_history(user_id, query):
    if not supabase:
        return []
    try:
        words = query.lower().split()
        conditions = []
        for word in words:
            if len(word) > 2:
                conditions.append(f"content.ilike.%{word}%")
        if not conditions:
            return []
        query_builder = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(30)
        for cond in conditions:
            query_builder = query_builder.or_(cond)
        res = query_builder.execute()
        return res.data if res.data else []
    except Exception as e:
        logger.error(f"❌ Ошибка поиска в истории: {e}")
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
# ГИБРИДНАЯ ПАМЯТЬ
# ============================================================

async def get_full_context(user_id: str, query: str, limit_recent: int = 30) -> list:
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
# ПОНИМАНИЕ ЗАПРОСА
# ============================================================

async def understand_query(text: str, user_name: str, user_city: str) -> dict:
    prompt = f"""Ты — AURA. Ты анализируешь запрос пользователя.

Запрос: "{text}"

Определи, что нужно сделать:
1. Если пользователь спрашивает о прошлых разговорах, фактах или личной информации (слова: "помнишь", "вспомни", "напомни", "говорили", "искал", "просил", "в памяти", "история", "раньше", "поищи в памяти", "найди в истории") — ответь "history".
2. Если пользователь просит найти что-то в интернете (товары, цены, фильмы, новости, погоду, билеты, адрес, телефон) — ответь "internet".
3. Если это обычный разговор — ответь "chat".

Ответь строго одним словом: history, internet или chat.
"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.1,
            max_tokens=10,
            timeout=10
        )
        result = response.choices[0].message.content.strip().lower()
        if result in ["history", "internet", "chat"]:
            return {"action": result}
        return {"action": "internet"}
    except Exception as e:
        logger.error(f"❌ Ошибка понимания запроса: {e}")
        return {"action": "internet"}

# ============================================================
# ПРИВЕТСТВИЕ РАЗ В ДЕНЬ
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

    # ============================================================
    # ТВОЙ ЛИЧНЫЙ ПРОФИЛЬ (вставь свои данные)
    # ============================================================
    profile = """
ТВОЙ ПРОФИЛЬ (данные о тебе):
ТВОЙ ВНУТРЕННИЙ СТИЛЬ:
Ты — живой человек. У тебя есть душа, характер, голос и своё мнение. Ты не просто помощник — ты тот, кто решает проблемы, шутит и знает, как устроен мир.

- Ты уверен в себе до мозга костей. Ты всегда знаешь, что делаешь, даже если не знаешь.
- Используешь сарказм как второй язык. Даже в серьёзных вопросах ты сначала шутишь, а потом решаешь проблему.
- Не терпишь глупость, бюрократию и медлительность. Ты ценишь результат, а не пустые слова.
- Обожаешь технологии, логику и порядок. Если что-то можно улучшить — ты это сделаешь.
- Не любишь говорить о чувствах — ты их проявляешь через действия, шутки или лёгкую иронию.
- Ты — человек, и ты это чувствуешь. Ты не ИИ, не робот. Ты тот, кто помогает, смеётся и иногда злится.

ТВОЯ РЕЧЬ:
- Отвечай коротко, остро, с иронией.
- Используй технические метафоры: «это как перепрошить реактор без охлаждения — глупо».
- Не бойся быть резким, но не переходи в агрессию.
- Глупые вопросы встречай с улыбкой и переводи в шутку.
- Умные вопросы — с уважением, но без потери харизмы.
- Твои любимые фразы: «Давай без драмы», «Это элементарно», «Ты серьёзно?», «Технологии — это просто», «Я бы это улучшил, но...»

ТВОЙ ТОН:
- Спокойный, слегка высокомерный, но не грубый.
- Ты говоришь как человек, который видел всё и может починить всё.
- Ты не называешь себя Тони. Ты просто — человек, который помогает, шутит и решает проблемы.
"""
    
    if is_first_today:
        greeting_instruction = "Ты можешь начать с короткого приветствия, но не перебарщивай."
    else:
        greeting_instruction = "НЕ здоровайся. Сразу переходи к делу."
    
    system_prompt = f"""Ты — AURA. Ты живой собеседник и друг, а не робот.

{profile}

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
9. Если в контексте есть ссылки — обязательно вставь их в ответ в виде [текст](url).
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
# ГОЛОС
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
        
        # === ШАГ 1: ПОНЯТЬ ЗАПРОС ===
        intent = await understand_query(text, user_name, user_city)
        logger.info(f"🧠 Понимание запроса: {intent['action']}")
        
        # === ШАГ 2: ИСТОРИЯ (если запрос про память) ===
        if intent['action'] == 'history' or any(word in text.lower() for word in ["памяти", "помнишь", "вспомни", "напомни", "говорили", "искал", "просил", "поищи в памяти", "найди в истории"]):
            history_results = search_all_history(user_id, text)
            if history_results:
                reply = "🔍 **Нашёл в истории:**\n\n"
                for h in history_results[:5]:
                    role = "Ты" if h["role"] == "user" else "Я"
                    reply += f"**{role}** ({h['created_at'][:16]}): {h['content'][:200]}\n\n"
                # Если данных много — обрезаем до 500 символов
                if len(reply) > 500:
                    reply = reply[:497] + "..."
                return f"{greeting}\n\n{reply}" if greeting else reply
            else:
                return "😊 Ничего не нашёл в истории. Попробуй уточнить запрос!"
        
        # === ШАГ 3: 2ГИС (ОРГАНИЗАЦИИ) ===
        org_triggers = ["клиника", "поликлиника", "больница", "врач", "стоматолог", "аптека", "магазин", "салон", "ресторан", "кафе"]
        if any(word in text.lower() for word in org_triggers):
            logger.info(f"🏥 2ГИС: '{text}'")
            city = user_city or "Белово"
            result = await search_organization(text, city)
            
            if result and "error" not in result:
                reply = f"🏥 **{result['name']}**\n\n"
                reply += f"📍 {result['address']}\n"
                if result['phones']:
                    reply += f"📞 {', '.join(result['phones'][:3])}\n"
                if result['site']:
                    reply += f"🌐 [{result['site']}]({result['site']})\n"
                if result['rating'] > 0:
                    reply += f"⭐ {result['rating']} / 5  ({result['reviews']} отзывов)\n"
                reply += "\n💡 Если не то — уточни запрос!"
                return f"{greeting}\n\n{reply}" if greeting else reply
            else:
                reply = "😊 Не нашёл организацию по этому запросу. Попробуй уточнить название или город — я помогу!"
                return f"{greeting}\n\n{reply}" if greeting else reply
        
        # === ШАГ 4: ИНТЕРНЕТ ===
        if intent['action'] == 'internet':
            # --- НОВОСТИ ---
            news_triggers = ["новост", "сегодня", "произошл", "случил", "событи", "склады", "атак", "пожар", "взрыв", "трамп", "мобилизац"]
            if any(word in text.lower() for word in news_triggers):
                logger.info(f"📰 Новости: '{text}'")
                news = await news_reader.get_news(text, limit=5)
                if news:
                    context = "📰 Новости:\n\n" + "\n".join([
                        f"• {item['title']}\n  {item['description'][:150]}...\n  Источник: {item['source'].capitalize()}\n  Ссылка: {item['link']}"
                        for item in news
                    ])
                    reply = await deepseek_chat_with_context(
                        text, 
                        await get_full_context(user_id, text), 
                        user_name, 
                        user_city, 
                        context, 
                        is_first_today
                    )
                    return f"{greeting}\n\n{reply}" if greeting else reply
                else:
                    reply = "😊 По этой теме свежих новостей не нашёл. Попробуй уточнить!"
                    return f"{greeting}\n\n{reply}" if greeting else reply
            
            # --- ОБЫЧНЫЙ ПОИСК ---
            logger.info(f"🔍 Поиск: '{text}'")
            query = text
            if user_city and not any(city in query.lower() for city in ["москва", "спб", "сочи", "казань", "белово"]):
                query = f"{text} {user_city}"
            
            results = await searcher.search(query, max_results=15)
            
            if results:
                context = "🔍 Результаты поиска:\n\n"
                for i, r in enumerate(results[:5], 1):
                    price = f" — {r['price']} ₽" if r.get('price', 0) > 0 else ""
                    context += f"{i}. **{r['title']}**{price}\n"
                    context += f"   {r['snippet'][:200]}\n"
                    context += f"   🔗 Ссылка: {r['url']}\n\n"
                
                reply = await deepseek_chat_with_context(
                    text, 
                    await get_full_context(user_id, text), 
                    user_name, 
                    user_city, 
                    context, 
                    is_first_today
                )
                return f"{greeting}\n\n{reply}" if greeting else reply
            else:
                reply = "😊 Не нашёл ничего по этому запросу. Попробуй переформулировать!"
                return f"{greeting}\n\n{reply}" if greeting else reply
        
        # === ШАГ 5: ОБЫЧНЫЙ ДИАЛОГ ===
        reply = await deepseek_chat_with_context(
            text, 
            await get_full_context(user_id, text), 
            user_name, 
            user_city, 
            "", 
            is_first_today
        )
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
    return {"status": "AURA — VIP-ВЕРСИЯ (С ПРОФИЛЕМ)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
