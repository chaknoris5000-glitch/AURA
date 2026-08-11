import os
import re
import tempfile
import json
import base64
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
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# === КЛЮЧИ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

logger.info("🚀 AURA — YANDEX SEARCH API v2 (RENDER)")

# === ПОДКЛЮЧЕНИЯ ===
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase подключён")
    except Exception as e:
        logger.error(f"❌ Ошибка Supabase: {e}")

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)
app = FastAPI()

# ============================================================
# 1. БАЗА ДАННЫХ
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
    """
    Ищет во ВСЕЙ истории пользователя по ключевым словам.
    Возвращает список сообщений, где есть совпадения.
    """
    if not supabase:
        return []
    try:
        # Разбиваем запрос на слова
        words = query.lower().split()
        # Строим условие ILIKE для каждого слова
        conditions = []
        for word in words:
            if len(word) > 2:  # игнорируем короткие слова
                conditions.append(f"content.ilike.%{word}%")
        
        if not conditions:
            return []
        
        # Строим запрос с OR для каждого слова
        query_builder = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(30)
        
        # Применяем OR условия
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
# 2. ПОИСК ЧЕРЕЗ YANDEX SEARCH API v2
# ============================================================

async def search_everything(query: str) -> list:
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        logger.warning("⚠️ Нет ключа или папки Яндекса")
        return []

    logger.info(f"🔍 Yandex Search API v2: {query}")

    url = "https://searchapi.api.cloud.yandex.net/v2/web/search"
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query,
        },
        "folderId": YANDEX_FOLDER_ID,
        "responseFormat": "FORMAT_XML"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                raw_data = data.get("rawData", "")
                if not raw_data:
                    return []
                # Декодируем Base64
                raw_data = raw_data.strip().strip('"').strip()
                missing_padding = len(raw_data) % 4
                if missing_padding:
                    raw_data += '=' * (4 - missing_padding)
                xml_string = base64.b64decode(raw_data).decode('utf-8')
                root = ET.fromstring(xml_string)
                results = []
                for doc in root.findall(".//doc"):
                    title = doc.findtext("title", "Без названия")
                    link = doc.findtext("url", "#")
                    snippet = doc.findtext("snippet", "")
                    results.append({
                        "title": title,
                        "url": link,
                        "snippet": snippet,
                        "price": ""
                    })
                logger.info(f"✅ Найдено {len(results)} результатов")
                return results[:10]
            else:
                logger.error(f"❌ Ошибка поиска: {response.status_code} - {response.text[:200]}")
                return []
    except Exception as e:
        logger.error(f"❌ Ошибка запроса: {e}")
        return []

# ============================================================
# 3. ЧТЕНИЕ СТРАНИЦ (ДЛЯ АНАЛИЗА)
# ============================================================

async def extract_page_content(url: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10, follow_redirects=True)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                return "\n".join(lines[:80])
            return ""
    except Exception as e:
        logger.error(f"❌ Ошибка чтения страницы {url}: {e}")
        return ""

# ============================================================
# 4. ДИАЛОГ С АНАЛИЗОМ КОНТЕКСТА И ИСТОРИИ
# ============================================================

async def deepseek_chat_with_context(text, history, user_name, user_city, context="", history_context=""):
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-20:]])
    
    system_prompt = f"""Ты — AURA. Ты живой собеседник и помощник.
Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}
История (последние сообщения):
{history_text}

ПРАВИЛА:
1. Отвечай коротко (2-4 предложения).
2. Используй эмодзи 😊🔥.
3. Будь дружелюбным и естественным.
4. Если есть контекст из интернета — используй его для ответа.
5. Ты должен давать завершённый ответ, который умещается в 700 токенов.
6. Если запрос неясен или недостаточно информации — задай уточняющий вопрос.
7. Учитывай всю историю диалога — если пользователь уже что-то просил, используй это.
8. Если в истории есть ответ на вопрос пользователя — используй его, даже если это было давно.

ИСТОРИЯ ИЗ БАЗЫ ДАННЫХ (все прошлые сообщения по теме):
{history_context}

КОНТЕКСТ ИЗ ИНТЕРНЕТА (если есть):
{context}
"""
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-20:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": text})
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.95,
            max_tokens=700,
            timeout=30
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return "😅 Извини, что-то пошло не так. Попробуй ещё раз."

# ============================================================
# 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_current_time():
    now = datetime.utcnow() + timedelta(hours=7)
    return f"Сейчас **{now.strftime('%H:%M')}**, {now.strftime('%d.%m.%Y')} 😊"

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
# 6. ОСНОВНАЯ ЛОГИКА
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
            return f"Тебя зовут **{user_name}** 😊" if user_name else "Я не знаю твоего имени."
        
        # === ГОРОД ===
        if any(word in text.lower() for word in ["где я живу", "мой город"]):
            return f"Ты из **{user_city}** 😊" if user_city else "Я не знаю, откуда ты."
        
        # === ПОИСК В ИСТОРИИ (ВСЕЙ) ===
        # Проверяем, не спрашивает ли пользователь о том, что уже обсуждалось
        history_results = search_all_history(user_id, text)
        history_context = ""
        if history_results:
            # Формируем контекст из найденных сообщений
            history_parts = []
            for item in history_results[:5]:
                role = "Ты" if item["role"] == "assistant" else "Пользователь"
                history_parts.append(f"{role} ({item['created_at'][:16]}): {item['content'][:200]}")
            history_context = "\n".join(history_parts)
            logger.info(f"📚 Найдено {len(history_results)} сообщений в истории по запросу")
        
        # === ПОИСК В ИНТЕРНЕТЕ С АНАЛИЗОМ ===
        search_triggers = [
            "найди", "поищи", "найти", "покажи", "где", "сайт", "фильм", 
            "клиника", "адрес", "маршрут", "ссылка", "цены", "билеты", 
            "купить", "скидки", "товар", "отель", "ресторан", "погода",
            "машина", "квартира", "дом", "работа", "вакансия", "курс"
        ]
        if any(word in text.lower() for word in search_triggers):
            logger.info(f"🔍 Поиск: '{text}'")
            query = text
            if user_city:
                query = f"{text} {user_city}"
            results = await search_everything(query)
            
            if results:
                pages_text = []
                for res in results[:2]:
                    if res["url"] and res["url"] != "#":
                        content = await extract_page_content(res["url"])
                        if content:
                            pages_text.append(f"Страница: {res['title']}\n{content[:300]}")
                
                context = "\n\n".join(pages_text) if pages_text else ""
                reply = await deepseek_chat_with_context(
                    text, 
                    get_recent_history(user_id, limit=50),
                    user_name,
                    user_city,
                    context,
                    history_context
                )
                # Если ответ пустой — показываем ссылки
                if not reply or len(reply.strip()) < 5:
                    reply = "🔍 Нашёл ссылки, но не смог обработать страницы:\n\n"
                    for i, res in enumerate(results[:5], 1):
                        reply += f"{i}. [{res['title']}]({res['url']})\n"
                return reply
            else:
                # Если в истории есть ответ — используем его
                if history_context:
                    return await deepseek_chat_with_context(
                        text, 
                        get_recent_history(user_id, limit=50),
                        user_name,
                        user_city,
                        "",
                        history_context
                    )
                else:
                    return "Не нашёл ничего по этому запросу. Попробуй переформулировать 😊"
        
        # === ОБЫЧНЫЙ ДИАЛОГ ===
        history = get_recent_history(user_id, limit=50)
        return await deepseek_chat_with_context(
            text, 
            history,
            user_name,
            user_city,
            "",
            history_context
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return "😅 Произошла ошибка. Попробуй ещё раз."

# ============================================================
# 7. WEBHOOK
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
        
        # === ГОЛОС ===
        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    text = transcribe_audio(audio_url)
                    if not text:
                        await send_message(user_id, "⚠️ Не удалось распознать голос.")
                        return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса.")
                return JSONResponse({"ok": True})
        
        # === ТЕКСТ ===
        if "text" in msg:
            text = msg["text"].strip()
        if not text:
            return JSONResponse({"ok": True})
        
        # === ОБРАБОТКА ===
        save_message(user_id, "user", text)
        reply = await deepseek_process(user_id, text)
        if not reply:
            reply = "😅 Не смог сформировать ответ. Попробуй ещё раз."
        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA — YANDEX SEARCH API v2 (RENDER)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
