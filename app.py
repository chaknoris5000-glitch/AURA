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
from dotenv import load_dotenv
from openai import OpenAI
import chromadb
from chromadb.utils import embedding_functions

load_dotenv()

DB_NAME = "aura.db"

# ==========================
# КЛЮЧИ
# ==========================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YANDEX_AGENT_API_KEY = os.getenv("YANDEX_AGENT_API_KEY")  # API ключ агента

ADMIN_USERS = ["5818548555"]

# ==========================
# ВЕКТОРНАЯ ПАМЯТЬ (ChromaDB)
# ==========================

# Инициализация ChromaDB
chroma_client = chromadb.PersistentClient(path="./chroma_db")
embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

# Получаем или создаём коллекцию
try:
    collection = chroma_client.get_collection("aura_memory")
except:
    collection = chroma_client.create_collection(
        name="aura_memory",
        embedding_function=embedding_fn
    )

def save_to_vector_memory(user_id, text, role="user"):
    """Сохраняет сообщение в векторную память"""
    try:
        collection.add(
            documents=[text],
            metadatas=[{"user_id": user_id, "role": role, "timestamp": datetime.now().isoformat()}],
            ids=[f"{user_id}_{datetime.now().timestamp()}"]
        )
        print(f"🧠 Сохранено в векторную память: {text[:50]}...")
    except Exception as e:
        print(f"⚠️ Ошибка сохранения в векторную память: {e}")

def search_vector_memory(user_id, query, limit=5):
    """Поиск по векторной памяти"""
    try:
        results = collection.query(
            query_texts=[query],
            n_results=limit,
            where={"user_id": user_id}
        )
        if results and results['documents']:
            return [doc for doc in results['documents'][0]]
        return []
    except Exception as e:
        print(f"⚠️ Ошибка поиска в векторной памяти: {e}")
        return []

def get_relevant_context(user_id, query):
    """Получает релевантный контекст из векторной памяти"""
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

# ==========================
# ВЫЗОВ АГЕНТА ЯНДЕКСА
# ==========================

def call_yandex_agent(query):
    """Вызывает агента Яндекса для выполнения действия"""
    if not YANDEX_AGENT_API_KEY:
        return None
    
    try:
        # TODO: Заменить на реальный API эндпоинт Яндекса
        url = "https://api.yandex.ai/agent/v1/execute"
        headers = {
            "Authorization": f"Bearer {YANDEX_AGENT_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "query": query,
            "tools": ["search", "browser", "booking"]  # нужные инструменты
        }
        response = requests.post(url, json=data, headers=headers, timeout=60)
        if response.status_code == 200:
            return response.json().get("result", "Готово!")
        return None
    except Exception as e:
        print(f"⚠️ Ошибка вызова агента: {e}")
        return None

# ==========================
# AI (DeepSeek)
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

# ==========================
# FASTAPI
# ==========================

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
        
        # Сохраняем в векторную память
        save_to_vector_memory(chat_id, text, "user")
        save_history(chat_id, "user", text)
        
        # Извлекаем релевантный контекст
        relevant_context = get_relevant_context(chat_id, text)
        
        # Факты о пользователе
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
        
        # История
        history = get_history(chat_id, limit=30)
        history_text = "\n".join([f"{'Я' if m['role']=='user' else 'AURA'}: {m['content']}" for m in history])
        
        # Проверяем, нужно ли вызвать агента
        agent_triggers = ["найди", "запиши", "забронируй", "купи", "закажи", "собери"]
        agent_result = None
        if any(word in text.lower() for word in agent_triggers):
            agent_result = call_yandex_agent(text)
            if agent_result:
                text = text + f"\n\n🔧 Агент выполнил: {agent_result}"
        
        # Время
        time_str = datetime.now().strftime("%H:%M %d.%m.%Y")
        
        # Промпт
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
        reply = reply.strip()
        if reply and not reply.endswith(('.', '!', '?')):
            reply += '.'
        
        # Сохраняем ответ в векторную память
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
