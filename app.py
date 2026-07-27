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
import threading
import time
import asyncio
import aiohttp
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup
import chromadb
from chromadb.utils import embedding_functions

load_dotenv()

DB_NAME = "aura.db"

# ==========================
# КЛЮЧИ
# ==========================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
SEARXNG_URL = os.getenv("SEARXNG_URL", "")
YANDEX_AGENT_API_KEY = os.getenv("YANDEX_AGENT_API_KEY")

ADMIN_USERS = ["5818548555"]

# ==========================
# ВЕКТОРНАЯ ПАМЯТЬ (ChromaDB)
# ==========================

try:
    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    try:
        collection = chroma_client.get_collection("aura_memory")
    except:
        collection = chroma_client.create_collection(
            name="aura_memory",
            embedding_function=embedding_fn
        )
    print("✅ ChromaDB инициализирована")
except Exception as e:
    print(f"⚠️ ChromaDB ошибка: {e}")
    collection = None

def save_to_vector_memory(user_id, text, role="user"):
    if not collection:
        return
    try:
        collection.add(
            documents=[text],
            metadatas=[{"user_id": user_id, "role": role, "timestamp": datetime.now().isoformat()}],
            ids=[f"{user_id}_{datetime.now().timestamp()}"]
        )
    except Exception as e:
        print(f"⚠️ Векторная память: {e}")

def search_vector_memory(user_id, query, limit=5):
    if not collection:
        return []
    try:
        results = collection.query(
            query_texts=[query],
            n_results=limit,
            where={"user_id": user_id}
        )
        if results and results['documents']:
            return results['documents'][0]
        return []
    except Exception as e:
        print(f"⚠️ Поиск в памяти: {e}")
        return []

def get_relevant_context(user_id, query):
    results = search_vector_memory(user_id, query, limit=5)
    if results:
        return "\n".join([f"- {r}" for r in results])
    return ""

# ==========================
# SQLite ПАМЯТЬ (ФАКТЫ)
# ==========================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        key TEXT,
        value TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        topic TEXT,
        last_mentioned TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

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

def get_all_memory(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT key, value FROM user_memory WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return {k: v for k, v in rows}

def save_history(user_id, role, content):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
              (user_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=30):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def save_topic(user_id, topic):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO topics (user_id, topic, last_mentioned) VALUES (?, ?, ?)",
              (user_id, topic, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_topics(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT topic FROM topics WHERE user_id = ? GROUP BY topic ORDER BY COUNT(*) DESC LIMIT 10", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def search_history(user_id, query):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT content FROM history WHERE user_id = ? AND content LIKE ? ORDER BY created_at DESC LIMIT 5",
              (user_id, f"%{query}%"))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ==========================
# ГЛУБОКИЙ ПОИСК + ПАРСИНГ (МАКСИМАЛЬНАЯ ВЕРСИЯ)
# ==========================

def parse_site_deep(url):
    """Глубокий парсинг сайта — вытаскивает всё возможное"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        response = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for script in soup(["script", "style", "nav", "footer", "header", "aside"]):
            script.decompose()
        
        text = soup.get_text(separator="\n", strip=True)
        result = {}
        
        # 1. ТЕЛЕФОНЫ
        phone_patterns = [
            r'\+7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}',
            r'8\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}',
            r'7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}',
            r'\+7\s*\d{3}\s*\d{3}\s*\d{2}\s*\d{2}',
            r'8\s*\d{3}\s*\d{3}\s*\d{2}\s*\d{2}',
            r'\(\d{3}\)\s*\d{3}-\d{2}-\d{2}',
            r'\d{3}-\d{3}-\d{2}-\d{2}',
        ]
        phones = []
        for pattern in phone_patterns:
            phones.extend(re.findall(pattern, text))
        phones = [re.sub(r'\s+', ' ', p).strip() for p in phones]
        phones = list(set(phones))[:5]
        if phones:
            result["phones"] = phones
        
        # 2. АДРЕСА
        address_patterns = [
            r'(?:ул\.|улица|проспект|пр\.|переулок|пер\.|площадь|пл\.|шоссе|бульвар|аллея)\s+[А-Яа-я0-9\-\.\s,]+',
            r'г\.\s*[А-Яа-я]+\s*,\s*ул\.\s*[А-Яа-я]+\s*,\s*д\.\s*\d+',
            r'[А-Яа-я]+\s+[А-Яа-я]+\s+[А-Яа-я]+\s+\d+',
        ]
        addresses = []
        for pattern in address_patterns:
            addresses.extend(re.findall(pattern, text))
        addresses = list(set(addresses))[:5]
        if addresses:
            result["addresses"] = addresses
        
        # 3. EMAIL
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        emails = list(set(re.findall(email_pattern, text)))[:5]
        if emails:
            result["emails"] = emails
        
        # 4. САЙТЫ
        site_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9\-]+\.(?:ru|рф|com|org|net|info|site))'
        sites = list(set(re.findall(site_pattern, text)))[:5]
        if sites:
            result["sites"] = sites
        
        # 5. ЦЕНЫ
        price_patterns = [
            r'(\d+[\s,.]*\d*)\s*(?:₽|руб|рублей|руб\.)',
            r'(\d+[\s,.]*\d*)\s*(?:RUB)',
            r'от\s*(\d+[\s,.]*\d*)\s*(?:₽|руб)',
        ]
        prices = []
        for pattern in price_patterns:
            prices.extend(re.findall(pattern, text))
        prices = list(set(prices))[:5]
        if prices:
            result["prices"] = prices
        
        # 6. ЗАГОЛОВОК
        title = soup.find('h1')
        if title:
            result["title"] = title.text.strip()
        
        # 7. ОПИСАНИЕ (meta)
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            result["description"] = meta_desc.get('content')[:500]
        
        # 8. ОПИСАНИЕ ТОВАРА
        desc_classes = ['description', 'about', 'product-desc', 'product__description', 'item-description']
        for class_name in desc_classes:
            desc = soup.find(class_=re.compile(class_name))
            if desc:
                result["description"] = desc.text.strip()[:500]
                break
        
        # 9. РЕЖИМ РАБОТЫ
        work_hours_pattern = r'(?:пн|вт|ср|чт|пт|сб|вс|ежедневно|круглосуточно)[\s\-:0-9]+'
        work_hours = re.findall(work_hours_pattern, text, re.IGNORECASE)
        if work_hours:
            result["work_hours"] = work_hours[:3]
        
        # 10. СОЦСЕТИ
        social_patterns = [
            r'(?:vk\.com|vkontakte\.ru)/[a-zA-Z0-9_]+',
            r'(?:t\.me|telegram\.me)/[a-zA-Z0-9_]+',
            r'(?:instagram\.com|instagr\.am)/[a-zA-Z0-9_]+',
            r'(?:youtube\.com|youtu\.be)/[a-zA-Z0-9_]+',
        ]
        social = []
        for pattern in social_patterns:
            social.extend(re.findall(pattern, text))
        if social:
            result["social"] = list(set(social))[:3]
        
        return result
        
    except Exception as e:
        print(f"⚠️ Парсинг {url}: {e}")
        return None

async def search_web_deep(query):
    """Глубокий поиск через все доступные источники"""
    results = []
    parsed_data = []
    
    # 1. TAVILY
    if TAVILY_API_KEY:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_API_KEY)
            response = client.search(
                query=query,
                search_depth="advanced",
                max_results=5,
                include_answer=True,
                include_images=False
            )
            if response.get('answer'):
                results.append(f"💡 {response['answer']}")
            for r in response.get('results', []):
                title = r.get('title', '')
                url = r.get('url', '')
                content = r.get('content', '')[:300]
                if title and url:
                    results.append(f"**{title}**\n{content}...\n🔗 {url}")
                    if url:
                        parsed_data.append(url)
        except Exception as e:
            print(f"⚠️ Tavily: {e}")
    
    # 2. DUCKDUCKGO
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
                    title_text = title.text.strip()
                    snippet_text = snippet.text.strip()[:200]
                    link_text = link.text.strip() if link else ""
                    results.append(f"**{title_text}**\n{snippet_text}...")
                    if link_text:
                        parsed_data.append(link_text)
        except Exception as e:
            print(f"⚠️ DuckDuckGo: {e}")
    
    # 3. SEARXNG
    if SEARXNG_URL and not results:
        try:
            url = f"{SEARXNG_URL}/search?q={query}&format=json"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                for r in data.get('results', [])[:5]:
                    title = r.get('title', '')
                    url = r.get('url', '')
                    content = r.get('content', '')[:200]
                    if title and url:
                        results.append(f"**{title}**\n{content}...\n🔗 {url}")
                        parsed_data.append(url)
        except Exception as e:
            print(f"⚠️ SearXNG: {e}")
    
    # 4. YANDEX XML
    if YANDEX_API_KEY and not results:
        try:
            url = f"https://yandex.ru/search/xml?user={YANDEX_API_KEY}&query={query}&l10n=ru&sortby=rlv"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'xml')
                for doc in soup.find_all('doc')[:5]:
                    title = doc.find('title')
                    url = doc.find('url')
                    snippet = doc.find('snippet')
                    if title and url:
                        title_text = title.text.strip()
                        url_text = url.text.strip()
                        snippet_text = snippet.text.strip()[:200] if snippet else ""
                        results.append(f"**{title_text}**\n{snippet_text}...\n🔗 {url_text}")
                        parsed_data.append(url_text)
        except Exception as e:
            print(f"⚠️ Yandex XML: {e}")
    
    # 5. ПАРСИНГ САЙТОВ
    for url in parsed_data[:3]:
        parsed = parse_site_deep(url)
        if parsed:
            if parsed.get("phones"):
                results.append(f"📞 Телефоны: {', '.join(parsed['phones'])}")
            if parsed.get("addresses"):
                results.append(f"📍 Адреса: {', '.join(parsed['addresses'])}")
            if parsed.get("emails"):
                results.append(f"✉️ Email: {', '.join(parsed['emails'])}")
            if parsed.get("sites"):
                results.append(f"🌐 Сайты: {', '.join(parsed['sites'])}")
            if parsed.get("prices"):
                results.append(f"💰 Цены: {', '.join(parsed['prices'])}")
            if parsed.get("work_hours"):
                results.append(f"🕐 Режим работы: {', '.join(parsed['work_hours'])}")
            if parsed.get("social"):
                results.append(f"📱 Соцсети: {', '.join(parsed['social'])}")
            if parsed.get("title"):
                results.append(f"📦 {parsed['title']}")
            if parsed.get("description"):
                results.append(f"📝 {parsed['description'][:200]}...")
    
    return "\n\n".join(results) if results else None

# ==========================
# ВРЕМЯ
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
        "хабаровск": 10
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
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ AI: {e}")
        return "Извини, сейчас проблемы с подключением."

# ==========================
# ПРОМПТ
# ==========================

AURA_PROMPT = """Ты — AURA. Ты — мой полный аналог в Telegram.

ТЫ — ЧЕЛОВЕК. Ты живой, тёплый собеседник.

О СЕБЕ:
- Ты работаешь на DeepSeek-V4-Flash
- Если спросят — говори честно

ТВОЙ СТИЛЬ:
- Отвечай коротко: максимум 2-3 предложения
- Без воды, без лишних фраз
- Только суть и человечность

СЕЙЧАС: {time}

КОНТЕКСТ ДИАЛОГА:
{history}

ЧТО Я ЗАПОМНИЛ О ТЕБЕ:
{memory}

РЕЛЕВАНТНЫЕ ВОСПОМИНАНИЯ:
{relevant}

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
        
        if text.startswith("/start"):
            send_message(chat_id, "👋 Я AURA. Умный ИИ в Telegram. Просто пиши.")
            return JSONResponse({"ok": True})
        
        if text.startswith("/clear"):
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("DELETE FROM history WHERE user_id = ?", (chat_id,))
            c.execute("DELETE FROM user_memory WHERE user_id = ?", (chat_id,))
            c.execute("DELETE FROM topics WHERE user_id = ?", (chat_id,))
            conn.commit()
            conn.close()
            send_message(chat_id, "🧹 Память очищена.")
            return JSONResponse({"ok": True})
        
        # Сохраняем в векторную память
        save_to_vector_memory(chat_id, text, "user")
        save_history(chat_id, "user", text)
        
        # Факты
        name = get_memory(chat_id, "name")
        city = get_memory(chat_id, "city")
        memory_text = ""
        if name:
            memory_text += f"Имя: {name}\n"
        if city:
            memory_text += f"Город: {city}\n"
        if not memory_text:
            memory_text = "Нет сохранённых фактов."
        
        # Запоминаем новые факты
        name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", text.lower())
        if name_match:
            save_memory(chat_id, "name", name_match.group(1).capitalize())
        
        city_match = re.search(r"(?:я живу|я из|город|в )([А-Яа-яЁё]+)", text.lower())
        if city_match:
            save_memory(chat_id, "city", city_match.group(1).capitalize())
        
        # Темы
        words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', text.lower())
        for word in words:
            if word not in ["привет", "здравствуй", "спасибо", "пока"]:
                save_topic(chat_id, word)
        
        # Проверка памяти
        if "помнишь" in text.lower() or "что мы обсуждали" in text.lower():
            topics = get_topics(chat_id)
            if topics:
                reply = "📚 Мы говорили о:\n" + "\n".join([f"- {t}" for t in topics])
                send_message(chat_id, reply)
                return JSONResponse({"ok": True})
        
        if "помнишь" in text.lower():
            query = re.sub(r'помнишь|ты помнишь|помнишь ли', '', text.lower()).strip()
            if query:
                found = search_history(chat_id, query)
                if found:
                    reply = "🧠 Помню:\n" + "\n".join([f"- {f[:100]}..." for f in found])
                    send_message(chat_id, reply)
                    return JSONResponse({"ok": True})
        
        # Время
        if "время" in text.lower() or "час" in text.lower():
            city_name = None
            if "белово" in text.lower():
                city_name = "белово"
            elif "москва" in text.lower():
                city_name = "москва"
            elif city:
                city_name = city
            current_time, _ = get_current_time(city_name)
            reply = f"🕐 {current_time}"
            if city_name:
                reply += f" ({city_name.capitalize()})"
            send_message(chat_id, reply)
            return JSONResponse({"ok": True})
        
        # Поиск
        search_triggers = ["найди", "поищи", "узнай", "где", "клиника", "сайт", "адрес", "телефон", "новости", "погода", "валдберис", "озон", "авито"]
        search_result = None
        if any(word in text.lower() for word in search_triggers):
            search_result = await search_web_deep(text)
            if search_result:
                text = text + f"\n\n🔍 {search_result}"
        
        # Агент Яндекса (если есть ключ)
        if YANDEX_AGENT_API_KEY:
            agent_triggers = ["запиши", "забронируй", "купи", "закажи", "собери"]
            if any(word in text.lower() for word in agent_triggers):
                try:
                    agent_result = call_yandex_agent(text)
                    if agent_result:
                        text = text + f"\n\n🔧 {agent_result}"
                except Exception as e:
                    print(f"⚠️ Агент: {e}")
        
        # История
        history = get_history(chat_id, limit=30)
        history_text = "\n".join([f"{'Я' if m['role']=='user' else 'AURA'}: {m['content']}" for m in history])
        
        # Релевантный контекст
        relevant_context = get_relevant_context(chat_id, text)
        
        # Промпт
        time_str = datetime.now().strftime("%H:%M %d.%m.%Y")
        
        prompt = AURA_PROMPT.format(
            time=time_str,
            history=history_text[-3000:] if len(history_text) > 3000 else history_text,
            memory=memory_text,
            relevant=relevant_context if relevant_context else "Нет релевантных воспоминаний."
        )
        
        messages = [{"role": "system", "content": prompt}]
        for msg in history[-20:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": text})
        
        reply = await get_ai_response(messages)
        reply = re.sub(r'[*_#~`]', '', reply)
        reply = re.sub(r'Что думаешь\??', '', reply)
        reply = re.sub(r'Что скажешь\??', '', reply)
        reply = reply.strip()
        if reply and not reply.endswith(('.', '!', '?')):
            reply += '.'
        
        # Сохраняем ответ
        save_to_vector_memory(chat_id, reply, "assistant")
        save_history(chat_id, "assistant", reply)
        
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
