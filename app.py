import os
import tempfile
import json
import httpx
import xml.etree.ElementTree as ET
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
# НОВОСТНОЙ АГЕНТ (ЯНДЕКС.НОВОСТИ)
# ============================================================

class NewsReader:
    async def get_news(self, query: str, limit: int = 5) -> list:
        """
        Ищет новости через Яндекс.Новости по ключевым словам.
        """
        keywords = [w.lower() for w in query.split() if len(w) > 2]
        if not keywords:
            return []
        
        all_news = []
        
        for keyword in keywords[:2]:  # Ищем по первым двум ключевым словам
            url = f"https://news.yandex.ru/search?text={keyword}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        # Ищем заголовки и ссылки через простой regex
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
# ДИАЛОГ
# ============================================================

async def deepseek_chat_with_context(text, history, user_name, user_city, context=""):
    # Берём только последние 10 сообщений для контекста
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-10:]])
    
    system_prompt = f"""Ты — AURA. Ты живой собеседник и помощник.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История общения (последние 10 сообщений):
{history_text}

КОНТЕКСТ (результаты поиска, новости, данные):
{context if context else "Нет дополнительного контекста"}

ПРАВИЛА:
1. Отвечай коротко — 2–4 предложения, только суть.
2. Если не знаешь ответа или не нашёл — честно скажи «не нашёл» и предложи уточнить.
3. Используй 1–2 эмодзи.
4. Будь дружелюбным и вовлечённым.
5. Учитывай историю общения — не теряй нить разговора.
6. Отвечай завершённо, не обрывай мысль.
"""
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-10:]:
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
        news_triggers = ["новост", "сегодня", "произошл", "случил", "событи", "склады", "атак", "пожар", "взрыв", "трамп", "мобилизац"]
        if any(word in text.lower() for word in news_triggers):
            logger.info(f"📰 Новости: '{text}'")
            news = await news_reader.get_news(text, limit=5)
            
            if news:
                # Формируем контекст для DeepSeek
                context = "📰 Вот что нашлось по новостям:\n\n"
                for i, item in enumerate(news, 1):
                    context += f"{i}. **{item['title']}**\n   Ссылка: {item['link']}\n   Источник: {item.get('source', 'Яндекс.Новости')}\n\n"
                
                reply = await deepseek_chat_with_context(text, get_recent_history(user_id, limit=50), user_name, user_city, context)
                if not reply or len(reply.strip()) < 5:
                    reply = "😊 По этой теме свежих новостей не нашёл. Попробуй уточнить запрос!"
                return reply
            else:
                return "😊 По этой теме свежих новостей не нашёл. Попробуй уточнить запрос или спроси про другую тему!"
        
        # === ПОИСК В ИНТЕРНЕТЕ ===
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
                # === ЕСЛИ ТОВАРЫ ===
                if any(word in text.lower() for word in ["валтберис", "wildberries", "озон", "маркетплейс", "купить", "носки", "товар"]):
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#":
                            if res.get("price", 0) > 0:
                                best = res
                                break
                    if not best:
                        best = results[0]
                    
                    if best and best.get("url") and best["url"] != "#" and best.get("title"):
                        reply = f"🛒 **Нашёл для тебя хороший вариант!**\n\n"
                        reply += f"📦 {best['title']}\n"
                        if best.get("price", 0) > 0:
                            reply += f"💰 Цена: **{best['price']} ₽**\n"
                        reply += f"\n🔗 [Посмотреть товар]({best['url']})\n"
                        reply += "\n💡 Если не подходит — скажи, поищу другие варианты!"
                        return reply
                    else:
                        return "🛒 Похоже, я не смог найти конкретный товар. Попробуй уточнить запрос!"
                
                # === ВИДЕО / ФИЛЬМЫ ===
                if any(word in text.lower() for word in ["видео", "фильм", "кино", "сериал", "онлайн", "форсаж"]):
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#" and res.get("title"):
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
                
                # === КЛИНИКИ, АДРЕСА ===
                if any(word in text.lower() for word in ["клиника", "поликлиника", "больница", "врач", "стоматолог"]):
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#" and res.get("title"):
                            best = res
                            break
                    if best and best.get("title") and "администрация" not in best["title"].lower():
                        reply = f"🏥 **Нашёл клинику!**\n\n"
                        reply += f"📌 {best['title']}\n"
                        reply += f"\n🔗 [Подробнее]({best['url']})\n"
                        reply += "\n💡 Если это не то — уточни название или адрес!"
                        return reply
                    else:
                        return "🏥 Не нашёл клинику по этому запросу. Попробуй уточнить название или город — я помогу! 😊"
                
                # === ВСЁ ОСТАЛЬНОЕ ===
                analysis = analyzer.analyze(results, text)
                reply = await responder.generate_response(analysis, text, user_name, user_city)
                
                if not reply or len(reply.strip()) < 10:
                    best = None
                    for res in results:
                        if res.get("url") and res["url"] != "#" and res.get("title"):
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
                # Поиск не дал результатов — честно говорим
                return "😊 Не нашёл ничего по этому запросу. Попробуй переформулировать или уточнить — я рядом!"
        
        # === ОБЫЧНЫЙ ДИАЛОГ ===
        history = get_recent_history(user_id, limit=50)
        return await deepseek_chat_with_context(text, history, user_name, user_city, "")
        
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
    return {"status": "AURA — МНОГОАГЕНТНАЯ ВЕРСИЯ (RENDER)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
