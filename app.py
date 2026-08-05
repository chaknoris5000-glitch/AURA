import os
import re
import tempfile
import json
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
import urllib.parse

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

# === КЛЮЧИ ЯНДЕКСА ===
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

logger.info("🚀 AURA — YANDEX SEARCH API v2 (ФИНАЛЬНАЯ ВЕРСИЯ)")

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
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .ilike("content", f"%{query}%")\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute()
        return res.data if res.data else []
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
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
# 2. ПОИСК ЧЕРЕЗ YANDEX SEARCH API (v2) — ПРОВЕРЕННЫЙ URL
# ============================================================

async def search_everything(query: str) -> list:
    """
    Поиск через официальный Yandex Search API (v2) с правильным URL
    """
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        logger.warning("⚠️ Нет ключа или папки Яндекса")
        return []

    logger.info(f"🔍 Yandex Search API: {query}")

    # ПРАВИЛЬНЫЙ URL ДЛЯ YANDEX SEARCH API v2
    url = "https://search-api.yandex.net/v2/search"
    
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "query": query,
        "folder_id": YANDEX_FOLDER_ID,
        "language": "ru",
        "search_type": "web",
        "page": 0,
        "page_size": 5,
        "sort_by": "relevance"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                results = []
                for doc in data.get("results", []):
                    results.append({
                        "title": doc.get("title", "Без названия"),
                        "url": doc.get("url", "#"),
                        "snippet": doc.get("snippet", ""),
                    })
                logger.info(f"✅ Найдено {len(results)} результатов")
                return results
            else:
                logger.error(f"❌ Ошибка поиска: {response.status_code} - {response.text}")
                return []
    except Exception as e:
        logger.error(f"❌ Ошибка запроса: {e}")
        return []

# ============================================================
# 3. АНАЛИЗ ЦЕН
# ============================================================

def analyze_prices(results: list) -> dict:
    prices = []
    for res in results:
        if res.get('price'):
            numbers = re.findall(r'\d+', res['price'])
            if numbers:
                price_int = int(''.join(numbers))
                prices.append({
                    "value": price_int,
                    "title": res['title'],
                    "url": res['url']
                })
    
    if prices:
        sorted_prices = sorted(prices, key=lambda x: x['value'])
        return {
            "cheapest": sorted_prices[0] if sorted_prices else None,
            "all": sorted_prices
        }
    return {"cheapest": None, "all": []}

# ============================================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
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
# 5. ОСНОВНАЯ ЛОГИКА
# ============================================================

async def deepseek_chat(text, history, user_name, user_city):
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-10:]])
    
    system_prompt = f"""Ты — AURA. Ты живой собеседник и друг.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История (последние сообщения):
{history_text}

ПРАВИЛА:
1. Отвечай коротко (2-3 предложения)
2. Используй эмодзи 😊🔥
3. Будь собой
"""
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-10:]:
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
        
        # === ПОИСК В ИСТОРИИ ===
        if any(word in text.lower() for word in ["говорили", "раньше", "помнишь", "вспомни"]):
            results = search_all_history(user_id, text)
            if results:
                history_text = "\n".join([f"{h['role']}: {h['content'][:100]}..." for h in results[:5]])
                return f"🔍 Нашёл в истории:\n\n{history_text}"
            else:
                return "Ничего не нашёл в истории 😊"
        
        # === ПОИСК В ИНТЕРНЕТЕ ===
        search_triggers = [
            "найди", "поищи", "найти", "покажи", "где", "сайт", "фильм", 
            "клиника", "адрес", "маршрут", "ссылка", "цены", "билеты", 
            "купить", "скидки", "товар", "отель", "ресторан", "погода",
            "машина", "квартира", "дом", "работа", "вакансия"
        ]
        if any(word in text.lower() for word in search_triggers):
            logger.info(f"🔍 Поиск: '{text}'")
            
            query = text
            if user_city:
                query = f"{text} {user_city}"
            
            results = await search_everything(query)
            
            if results:
                price_analysis = analyze_prices(results)
                
                response = "🔍 Нашёл! Вот лучшие результаты:\n\n"
                
                if price_analysis['cheapest']:
                    cheapest = price_analysis['cheapest']
                    response += f"💰 **Самый дешёвый вариант:**\n"
                    response += f"**{cheapest['title']}**\n"
                    response += f"Цена: **{cheapest['value']} ₽**\n"
                    response += f"[Ссылка]({cheapest['url']})\n\n"
                
                response += "📋 **Другие варианты:**\n"
                for i, res in enumerate(results[:5], 1):
                    price_text = f" — {res['price']}" if res.get('price') else ""
                    response += f"{i}. [{res['title']}]({res['url']}){price_text}\n"
                
                return response
            else:
                return "Не нашёл ничего по этому запросу. Попробуй переформулировать 😊"
        
        # === ОБЫЧНЫЙ ДИАЛОГ ===
        history = get_recent_history(user_id, limit=30)
        return await deepseek_chat(text, history, user_name, user_city)
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return "😅 Произошла ошибка. Попробуй ещё раз."

# ============================================================
# 6. WEBHOOK
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
        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)
        
        return JSONResponse({"ok": True})
        
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        try:
            await send_message(user_id, "😅 Что-то пошло не так. Попробуй ещё раз.")
        except:
            pass
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA — YANDEX SEARCH API v2 (ФИНАЛЬНАЯ ВЕРСИЯ)"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
