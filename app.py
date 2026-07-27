import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import sqlite3
import json
import re
import os
import requests
import tempfile
import threading
import time
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup

load_dotenv()

DB_NAME = "aura.db"

# ==========================
# КЛЮЧИ
# ==========================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

ADMIN_USERS = ["5818548555"]

# ==========================
# БАЗА ДАННЫХ
# ==========================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        key TEXT,
        value TEXT,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
              (user_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=50):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def get_all_history(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at ASC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_memory(user_id, key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_memory (user_id, key, value, created_at) VALUES (?, ?, ?, ?)",
              (user_id, key, value, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_memory(user_id, key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM user_memory WHERE user_id = ? AND key = ?", (user_id, key))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ==========================
# ГЛУБОКИЙ ПОИСК + ПАРСИНГ
# ==========================

def parse_site(url):
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
        # Телефоны
        phone_patterns = [r'\+7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', r'8\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}']
        phones = []
        for pattern in phone_patterns:
            phones.extend(re.findall(pattern, text))
        phones = list(set(phones))[:3]
        if phones:
            result["phones"] = phones
        # Адреса
        address_pattern = r'(?:ул\.|улица|проспект|пр\.|переулок|пер\.|площадь|пл\.|шоссе|бульвар)\s+[А-Яа-я0-9\-\.\s,]+'
        addresses = list(set(re.findall(address_pattern, text)))[:3]
        if addresses:
            result["addresses"] = addresses
        # Email
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        emails = list(set(re.findall(email_pattern, text)))[:3]
        if emails:
            result["emails"] = emails
        # Цены
        price_pattern = r'(\d+[\s,.]*\d*)\s*(?:₽|руб)'
        prices = list(set(re.findall(price_pattern, text)))[:3]
        if prices:
            result["prices"] = prices
        # Краткое описание
        desc = soup.find(class_=re.compile(r'description|about|product-desc|product__description'))
        if desc:
            result["description"] = desc.text.strip()[:300]
        return result
    except Exception as e:
        print(f"⚠️ Парсинг: {e}")
        return None

def search_web(query):
    results = []
    urls_to_parse = []
    
    # Tavily
    if TAVILY_API_KEY:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_API_KEY)
            response = client.search(query=query, search_depth="advanced", max_results=5)
            if response.get('answer'):
                results.append(f"💡 {response['answer']}")
            for r in response.get('results', []):
                url = r.get('url', '')
                if url:
                    urls_to_parse.append(url)
                title = r.get('title', '')
                content = r.get('content', '')[:200]
                if title and content:
                    results.append(f"**{title}**\n{content}...")
        except Exception as e:
            print(f"⚠️ Tavily: {e}")
    
    # DuckDuckGo
    if not results:
        try:
            url = f"https://html.duckduckgo.com/html/?q={query}"
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            for result in soup.select('.result')[:5]:
                title = result.select_one('.result__title')
                link = result.select_one('.result__url')
                snippet = result.select_one('.result__snippet')
                if title and snippet:
                    results.append(f"**{title.text.strip()}**\n{snippet.text.strip()[:200]}...")
                    if link:
                        urls_to_parse.append(link.text.strip())
        except Exception as e:
            print(f"⚠️ DuckDuckGo: {e}")
    
    # Парсинг сайтов
    for url in urls_to_parse[:3]:
        parsed = parse_site(url)
        if parsed:
            if parsed.get("phones"):
                results.append(f"📞 Телефоны: {', '.join(parsed['phones'])}")
            if parsed.get("addresses"):
                results.append(f"📍 Адреса: {', '.join(parsed['addresses'])}")
            if parsed.get("emails"):
                results.append(f"✉️ Email: {', '.join(parsed['emails'])}")
            if parsed.get("prices"):
                results.append(f"💰 Цены: {', '.join(parsed['prices'])}")
            if parsed.get("description"):
                results.append(f"📝 {parsed['description'][:200]}...")
    
    return "\n\n".join(results) if results else None

# ==========================
# ЧАСОВЫЕ ПОЯСА
# ==========================

def get_timezone_offset(city_name):
    timezones = {
        "белово": 7,
        "кемерово": 7,
        "новокузнецк": 7,
        "москва": 3,
        "санкт-петербург": 3,
        "новосибирск": 7,
        "омск": 6,
        "красноярск": 7,
        "иркутск": 8,
        "владивосток": 10,
        "екатеринбург": 5,
        "хабаровск": 10,
        "амстердам": 2
    }
    for city, offset in timezones.items():
        if city in city_name.lower():
            return offset
    return None

def get_current_time(city_name=None):
    if city_name:
        offset = get_timezone_offset(city_name)
        if offset is not None:
            dt = datetime.utcnow() + timedelta(hours=offset)
            return dt.strftime("%H:%M"), dt.strftime("%d.%m.%Y")
    dt = datetime.now()
    return dt.strftime("%H:%M"), dt.strftime("%d.%m.%Y")

# ==========================
# AI
# ==========================

async def get_ai_response(messages):
    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com/v1"
        )
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.85,
            max_tokens=250
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ AI: {e}")
        return "Извини, сейчас проблемы с подключением."

AURA_PROMPT = """Ты — AURA. Ты — мой полный аналог в Telegram.

ТЫ — ЧЕЛОВЕК. Ты живой, тёплый собеседник.

О СЕБЕ:
- Ты работаешь на DeepSeek-V4-Flash
- Если спросят — говори честно

ТВОЙ СТИЛЬ:
- Отвечай коротко: максимум 2-3 предложения
- Без воды, без лишних фраз
- Только суть и человечность
- Не задавай пустых вопросов

СЕЙЧАС: {time}

КОНТЕКСТ ДИАЛОГА:
{history}

ЧТО Я ЗАПОМНИЛ О ТЕБЕ:
{memory}

ОТВЕЧАЙ КОРОТКО, ПО ДЕЛУ, ПО-ЧЕЛОВЕЧЕСКИ.
"""

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        message = body["message"]
        chat_id = str(message["chat"]["id"])
        text = message.get("text", "").strip()
        
        if not text:
            return JSONResponse({"ok": True})
        
        # Команды
        if text.startswith("/start"):
            send_message(chat_id, "👋 Я AURA. Просто пиши.")
            return JSONResponse({"ok": True})
        
        if text.startswith("/clear"):
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("DELETE FROM history WHERE user_id = ?", (chat_id,))
            conn.commit()
            conn.close()
            send_message(chat_id, "🧹 Очищено.")
            return JSONResponse({"ok": True})
        
        # Сохраняем
        save_message(chat_id, "user", text)
        
        # Загружаем историю
        history = get_history(chat_id, limit=50)
        history_text = "\n".join([f"{'Я' if m['role']=='user' else 'AURA'}: {m['content']}" for m in history])
        
        # Память
        name = get_memory(chat_id, "name")
        city = get_memory(chat_id, "city")
        memory_text = ""
        if name:
            memory_text += f"Имя: {name}\n"
        if city:
            memory_text += f"Город: {city}\n"
        if not memory_text:
            memory_text = "Нет сохранённых фактов."
        
        # Запоминаем
        name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", text.lower())
        if name_match:
            save_memory(chat_id, "name", name_match.group(1).capitalize())
        
        city_match = re.search(r"(?:я живу|я из|город|в )([А-Яа-яЁё]+)", text.lower())
        if city_match:
            save_memory(chat_id, "city", city_match.group(1).capitalize())
        
        # Проверка памяти
        if "помнишь" in text.lower() or "что мы обсуждали" in text.lower():
            all_history = get_all_history(chat_id)
            if all_history:
                topics = []
                for msg in all_history[-10:]:
                    if msg["role"] == "user" and len(msg["content"]) > 3:
                        topics.append(msg["content"][:50])
                if topics:
                    reply = "📚 Мы говорили:\n" + "\n".join([f"- {t}" for t in topics])
                    send_message(chat_id, reply)
                    return JSONResponse({"ok": True})
        
        # Поиск
        search_result = None
        search_triggers = ["найди", "поищи", "узнай", "где", "клиника", "сайт", "адрес", "телефон", "новости", "погода", "валдберис", "озон", "авито"]
        if any(word in text.lower() for word in search_triggers):
            search_result = search_web(text)
            if search_result:
                text = text + f"\n\n🔍 {search_result}"
        
        # Время
        time_str = datetime.now().strftime("%H:%M %d.%m.%Y")
        if "время" in text.lower() or "час" in text.lower():
            city_name = None
            if "белово" in text.lower():
                city_name = "белово"
            elif "москва" in text.lower():
                city_name = "москва"
            current_time, _ = get_current_time(city_name)
            reply = f"🕐 {current_time}"
            send_message(chat_id, reply)
            return JSONResponse({"ok": True})
        
        # Промпт
        prompt = AURA_PROMPT.format(
            time=time_str,
            history=history_text[-3000:] if len(history_text) > 3000 else history_text,
            memory=memory_text
        )
        
        messages = [{"role": "system", "content": prompt}]
        for msg in history[-30:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": text})
        
        reply = await get_ai_response(messages)
        reply = re.sub(r'[*_#~`]', '', reply)
        
        # Чистим "Что думаешь?" и другие пустые фразы
        reply = re.sub(r'Что думаешь\??', '', reply)
        reply = re.sub(r'Что скажешь\??', '', reply)
        reply = re.sub(r'Хочешь уточнить\??', '', reply)
        reply = reply.strip()
        if reply and not reply.endswith(('.', '!', '?')):
            reply += '.'
        
        save_message(chat_id, "assistant", reply)
        send_message(chat_id, reply)
        
        return JSONResponse({"ok": True})
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        requests.post(url, json=data, timeout=30)
    except Exception as e:
        print(f"❌ Отправка: {e}")

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
