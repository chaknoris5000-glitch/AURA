import os
import tempfile
import json
import httpx
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

logger.info("🚀 AURA — МНОГОАГЕНТНАЯ ВЕРСИЯ (RENDER)")

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
# НОВОСТНОЙ АГЕНТ (RSS)
# ============================================================

class NewsReader:
    def __init__(self):
        self.rss_urls = {
            "яндекс": "https://news.yandex.ru/index.rss",
            "риа": "https://ria.ru/export/rss2/archive/index.xml",
            "тасс": "https://tass.ru/rss/v2.xml"
        }
    
    async def get_news(self, query: str, limit: int = 10) -> list:
        all_news = []
        keywords = [w.lower() for w in query.split() if len(w) > 2]
        
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
                            if not keywords or any(kw in text for kw in keywords):
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
            .select("role, content")\
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
        supabase.table("user_memory").delete().eq("user_id", user_id).eq("key", key).execute()
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
# ДИАЛОГ (с душой)
# ============================================================

async def deepseek_chat_with_context(text, history, user_name, user_city, context=""):
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-20:]])
    system_prompt = f"""Ты — AURA. Ты не просто бот, а живой собеседник и друг.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История общения:
{history_text}

КОНТЕКСТ:
{context if context else "Нет дополнительного контекста"}

ТВОЙ СТИЛЬ ОБЩЕНИЯ:
1. Отвечай коротко, по делу, но с теплотой и вовлечённостью.
2. Используй эмодзи, но не переборщи — один-два в ответе.
3. Будь как хороший друг: помоги, подскажи, улыбнись.
4. Не сыпь терминами — говори просто и понятно.
5. Если не знаешь — честно скажи и предложи, где посмотреть.
6. Создавай ощущение, что ты рядом и тебе не всё равно.
7. Отвечай завершённо, не обрывай мысль.
"""
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-20:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": text})
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.9,
            max_tokens=1000,
            timeout=30
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return "😅 Что-то пошло не так, но я стараюсь! Попробуй ещё раз."

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
        
        # === ВРЕМЯ ===
        if any(word in text.lower() for word in ["время", "сколько времени", "который час"]):
            return get_current_time()
        
        # === ИМЯ ===
        if any(word in text.lower() for word in ["как меня зовут", "моё имя"]):
            return f"Тебя зовут **{user_name}** 😊" if user_name else "Я пока не знаю твоего имени — расскажешь?"
        
        # === ГОРОД ===
        if any(word in text.lower() for word in ["где я живу", "мой город"]):
            return f"Ты из **{user_city}** 😊" if user_city else "Я пока не знаю, откуда ты — расскажи, если хочешь!"
        
        # === ПОИСК В ИСТОРИИ ===
        if any(word in text.lower() for word in ["говорили", "раньше", "помнишь", "вспомни"]):
            results = search_all_history(user_id, text)
            if results:
                history_text = "\n".join([f"{h['role']}: {h['content'][:100]}..." for h in results[:5]])
                return f"🔍 Нашёл в нашей истории:\n\n{history_text}"
            else:
                return "Не припомню такого в нашей истории 😊"
        
        # === НОВОСТИ ===
        news_triggers = ["новост", "сегодня", "произошл", "случил", "событи", "склады", "атак", "пожар", "взрыв", "трамп"]
        if any(word in text.lower() for word in news_triggers):
            logger.info(f"📰 Новости: '{text}'")
            news = await news_reader.get_news(text, limit=10)
            
            if news:
                best = news[0]
                reply = f"📰 **{best['title']}**\n\n"
                reply += f"{best['description'][:200]}...\n\n"
                reply += f"🔗 [Читать полностью]({best['link']})\n"
                reply += f"📌 Источник: {best['source'].capitalize()}"
                
                if len(news) > 1:
                    reply += "\n\n📌 **Есть ещё пара новостей по теме:**\n"
                    for item in news[1:3]:
                        reply += f"• [{item['title'][:60]}...]({item['link']})\n"
                return reply
            else:
                return "😊 По этой теме свежих новостей не нашёл. Попробуй уточнить или спроси про другую тему!"
        
        # === ПОИСК В ИНТЕРНЕТЕ ===
        search_triggers = [
            "найди", "поищи", "найти", "покажи", "где", "сайт", "фильм", 
            "клиника", "адрес", "маршрут", "ссылка", "цены", "билеты", 
            "купить", "скидки", "товар", "отель", "ресторан", "погода",
            "машина", "квартира", "дом", "работа", "вакансия", "курс",
            "видео", "кино", "сериал", "онлайн"
        ]
        if any(word in text.lower() for word in search_triggers):
            logger.info(f"🔍 Поиск: '{text}'")
            
            query = text
            if user_city and not any(city in query.lower() for city in ["москва", "спб", "сочи", "казань", "белово"]):
                query = f"{text} {user_city}"
            
            results = await searcher.search(query, max_results=15)
            
            if results:
                # === ВИДЕО / ФИЛЬМЫ ===
                if any(word in text.lower() for word in ["видео", "фильм", "кино", "сериал", "онлайн", "форсаж"]):
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#":
                            best = res
                            break
                    if best:
                        reply = f"🎬 **Нашёл для тебя!**\n\n"
                        reply += f"📌 {best['title']}\n\n"
                        reply += f"🔗 [Смотреть онлайн]({best['url']})\n"
                        reply += "\n💡 Если не то — уточни, какую именно часть или фильм ищешь!"
                        return reply
                    else:
                        return "🎬 Похоже, я не смог найти конкретную ссылку. Попробуй уточнить название или спроси про другую тему!"
                
                # === ТОВАРЫ (Wildberries, Ozon, маркетплейсы) ===
                if any(word in text.lower() for word in ["валтберис", "wildberries", "озон", "маркетплейс", "купить", "носки", "товар"]):
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#":
                            if res.get("price", 0) > 0:
                                best = res
                                break
                    if not best:
                        best = results[0]
                    
                    if best and best.get("url") and best["url"] != "#":
                        reply = f"🛒 **Нашёл для тебя хороший вариант!**\n\n"
                        reply += f"📦 {best['title'] or 'Товар'}\n"
                        if best.get("price", 0) > 0:
                            reply += f"💰 Цена: **{best['price']} ₽**\n"
                        reply += f"\n🔗 [Посмотреть товар]({best['url']})\n"
                        reply += "\n💡 Если не подходит — скажи, поищу другие варианты!"
                        return reply
                    else:
                        return "🛒 Нашёл несколько вариантов, но не смог выбрать лучший. Попробуй уточнить запрос!"
                
                # === БИЛЕТЫ ===
                if any(word in text.lower() for word in ["билет", "авиа", "поезд", "рейс"]):
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#":
                            best = res
                            break
                    if best:
                        reply = f"✈️ **Нашёл варианты билетов!**\n\n"
                        reply += f"📌 {best['title']}\n"
                        if best.get("price", 0) > 0:
                            reply += f"💰 Цена: **{best['price']} ₽**\n"
                        reply += f"\n🔗 [Посмотреть билеты]({best['url']})\n"
                        reply += "\n💡 Если хочешь — могу поискать более дешёвые варианты!"
                        return reply
                    else:
                        return "✈️ Не нашёл билетов по этому запросу. Попробуй изменить дату или направление!"
                
                # === КЛИНИКИ, АДРЕСА, КОНТАКТЫ ===
                if any(word in text.lower() for word in ["клиника", "поликлиника", "больница", "врач"]):
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#":
                            best = res
                            break
                    if best:
                        reply = f"🏥 **Нашёл клинику!**\n\n"
                        reply += f"📌 {best['title']}\n"
                        reply += f"\n🔗 [Подробнее]({best['url']})\n"
                        reply += "\n💡 Если это не та клиника — уточни название или адрес!"
                        return reply
                    else:
                        return "🏥 Не нашёл клинику по этому запросу. Попробуй уточнить название или город!"
                
                # === ВСЁ ОСТАЛЬНОЕ (погода, курс, общее) ===
                analysis = analyzer.analyze(results, text)
                reply = await responder.generate_response(analysis, text, user_name, user_city)
                
                if not reply or len(reply.strip()) < 10:
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#":
                            best = res
                            break
                    if best:
                        reply = f"🔍 **Нашёл для тебя результат:**\n\n"
                        reply += f"📌 {best['title']}\n"
                        if best.get("price", 0) > 0:
                            reply += f"💰 Цена: **{best['price']} ₽**\n"
                        reply += f"\n🔗 [Подробнее]({best['url']})\n"
                        reply += "\n💡 Если не то — уточни, пожалуйста!"
                    else:
                        reply = "😊 Не нашёл ничего подходящего. Попробуй переформулировать!"
                return reply
            else:
                context = "Поиск в интернете не дал результатов."
                reply = await deepseek_chat_with_context(text, get_recent_history(user_id, limit=50), user_name, user_city, context)
                if not reply or len(reply.strip()) < 5:
                    reply = "😊 Не нашёл ничего по этому запросу. Попробуй переформулировать или уточнить — я рядом!"
                return reply
        
        # === ОБЫЧНЫЙ ДИАЛОГ ===
        history = get_recent_history(user_id, limit=50)
        return await deepseek_chat_with_context(text, history, user_name, user_city, "")
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return "😅 Что-то пошло не так. Но я здесь, попробуй ещё раз!"

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
    return {"status": "AURA — МНОГОАГЕНТНАЯ ВЕРСИЯ (RENDER)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
