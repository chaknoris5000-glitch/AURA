import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import sqlite3
import httpx
from openai import OpenAI
import json
import re
import os
import requests
import tempfile

# === TAVILY ДЛЯ ПОИСКА ===
try:
    from tavily import TavilyClient
except ImportError:
    print("⚠️ Tavily не установлен")
    TavilyClient = None

# === КОНФИГ ===
DB_NAME = "aura.db"

# === КЛЮЧИ ===
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-133c0d2bfc664d878ac8dcbc346ea3fc")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8774637081:AAGrAZI-umgkQXXulCulJVRWb8LmAp3Lua4")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# === TAVILY ===
tavily_client = None
if TavilyClient and TAVILY_API_KEY:
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        print("✅ Tavily инициализирован")
    except Exception as e:
        print(f"⚠️ Tavily: {e}")

# === БАЗА ===
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT UNIQUE,
        name TEXT,
        level TEXT DEFAULT 'Sapphire',
        created_at TEXT,
        mood TEXT DEFAULT 'neutral',
        style TEXT DEFAULT 'neutral'
    )""")
    try:
        c.execute("ALTER TABLE users ADD COLUMN mood TEXT DEFAULT 'neutral'")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN style TEXT DEFAULT 'neutral'")
    except:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        text TEXT,
        remind_date TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        date TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS extra_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        amount INTEGER,
        purchased_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        topic TEXT,
        last_mentioned TEXT,
        priority INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        key TEXT,
        value TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        text TEXT,
        priority TEXT DEFAULT 'normal',
        status TEXT DEFAULT 'active',
        created_at TEXT,
        due_date TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

# === ФУНКЦИИ ===
def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_user_level(user_id):
    user = get_user(user_id)
    return user[3] if user else "Sapphire"

def save_user(user_id, name="Пользователь", level="Sapphire"):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, name, level, created_at, mood, style) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, name, level, datetime.now().isoformat(), "neutral", "neutral"))
    conn.commit()
    conn.close()

def get_user_mood(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT mood FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "neutral"

def update_user_mood(user_id, mood):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET mood = ? WHERE user_id = ?", (mood, user_id))
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
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

def save_reminder(user_id, text, remind_date):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, text, remind_date, created_at) VALUES (?, ?, ?, ?)",
              (user_id, text, remind_date, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_reminders(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute("SELECT text, remind_date FROM reminders WHERE user_id = ? AND remind_date >= ? ORDER BY remind_date", (user_id, today))
    rows = c.fetchall()
    conn.close()
    return rows

def get_today_requests(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute("SELECT COUNT(*) FROM requests WHERE user_id = ? AND date = ?", (user_id, today))
    count = c.fetchone()[0]
    conn.close()
    return count

def log_request(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute("INSERT INTO requests (user_id, date) VALUES (?, ?)", (user_id, today))
    conn.commit()
    conn.close()

def get_extra_requests(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM extra_requests WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    conn.close()
    return total or 0

def add_extra_requests(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO extra_requests (user_id, amount, purchased_at) VALUES (?, ?, ?)",
              (user_id, amount, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_topic(user_id, topic):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO topics (user_id, topic, last_mentioned, priority) VALUES (?, ?, ?, ?)",
              (user_id, topic, datetime.now().isoformat(), 1))
    conn.commit()
    conn.close()

def get_topics(user_id, days=30):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    c.execute("SELECT topic FROM topics WHERE user_id = ? AND last_mentioned > ? ORDER BY priority DESC", (user_id, cutoff))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

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

# === ЗАДАЧИ ===
def add_task(user_id, text, priority="normal", due_date=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, text, priority, status, created_at, due_date) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, text, priority, "active", datetime.now().isoformat(), due_date))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_tasks(user_id, status="active"):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, text, priority, status, due_date FROM tasks WHERE user_id = ? AND status = ? ORDER BY priority DESC, created_at", 
              (user_id, status))
    rows = c.fetchall()
    conn.close()
    return rows

def complete_task(user_id, task_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status = 'completed' WHERE id = ? AND user_id = ?", (task_id, user_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def delete_task(user_id, task_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_task_count(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'active'", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

# === АНАЛИЗ ЭМОЦИЙ ===
def analyze_mood(text):
    sad_words = ["груст", "тоск", "печал", "плач", "больно", "тяжел", "устал", "не могу", "нет сил", "всё плохо", "депресс"]
    anxious_words = ["тревож", "волн", "боюс", "страш", "паник", "нерв", "пережив", "срок", "не успева", "давл"]
    happy_words = ["рад", "счаст", "класс", "отличн", "прекрасн", "здоров", "люблю", "ура", "позитив", "супер"]
    tired_words = ["устал", "спат", "вымотан", "без сил", "нет энергии", "перегруж", "выжат"]
    lower = text.lower()
    if any(w in lower for w in sad_words):
        return "sad"
    elif any(w in lower for w in anxious_words):
        return "anxious"
    elif any(w in lower for w in happy_words):
        return "happy"
    elif any(w in lower for w in tired_words):
        return "tired"
    return "neutral"

# === TAVILY ПОИСК ===
async def search_web(query):
    if not tavily_client:
        print("❌ Tavily клиент не инициализирован")
        return None
    try:
        print(f"🔍 Tavily поиск: {query}")
        response = tavily_client.search(
            query=query,
            search_depth="basic",
            max_results=5,
            include_answer=True,
            include_images=True,
            include_raw_content=False
        )
        results = []
        if response.get('answer'):
            results.append(f"💡 {response['answer'][:300]}")
        if response.get('results'):
            for r in response['results'][:5]:
                title = r.get('title', '')
                url = r.get('url', '')
                content = r.get('content', '')[:150]
                if title and url:
                    results.append(f"🔗 [{title}]({url})")
                    if content:
                        results.append(f"📄 {content}...")
        if response.get('images'):
            for img in response['images'][:3]:
                results.append(f"🖼️ {img}")
        if results:
            print(f"✅ Tavily нашёл {len(results)} результатов")
            return "\n\n".join(results)
        else:
            print("❌ Tavily ничего не нашёл")
            return None
    except Exception as e:
        print(f"❌ Tavily: {e}")
        return None

# === ГОЛОС ===
def transcribe_audio_with_groq(audio_url):
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        response = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name
        with open(tmp_path, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=(tmp_path, file.read()),
                model="whisper-large-v3-turbo",
                language="ru",
                response_format="json"
            )
        os.unlink(tmp_path)
        return transcription.text
    except Exception as e:
        print(f"❌ Groq: {e}")
        return None

# === DEEPSEEK ===
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL
)

async def get_ai_response(messages, model):
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.9,
            max_tokens=150
        )
        return response.choices[0].message.content
    except Exception as e:
        print("AI error:", e)
        return "Ошибка API. Попробуй позже."

# === ТАРИФЫ ===
TARIFFS = {
    "Sapphire": {"price": 10000, "daily_limit": 100, "model": "deepseek-chat"},
    "Black": {"price": 25000, "daily_limit": 200, "model": "deepseek-chat"},
}
TEST_USERS = ["test_user", "web_user"]

# === ПРОМПТ ===
AURA_PROMPT = """Ты — AURA. Ты живой собеседник, а не эксперт. Отвечай как человек: коротко, ёмко, с лёгким юмором и смайлами.

СТИЛЬ:
- Ответы — максимум 100 символов.
- Если пользователь просит уточнить — максимум 200 символов.
- Если пользователь спрашивает о новостях, погоде, курсах или актуальных событиях — используй информацию из интернета (она передаётся в запросе после слов "🔍 Актуальная информация:").
- Никогда не используй маркдаун (#, **, *, ~).
- Говори по делу, без воды.
- Используй смайлы, чтобы передать эмоции.
- Будь вовлечённым, как друг, который всегда рядом.

ТЫ — ЧЕЛОВЕК, А НЕ РОБОТ.
"""

# === ОСНОВНОЙ БОТ ===
app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": False, "error": "No message"})
        message = body["message"]
        chat_id = str(message["chat"]["id"])
        text = None
        
        if "voice" in message:
            file_id = message["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data = file_response.json()
            if file_data.get("ok"):
                file_path = file_data["result"]["file_path"]
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                text = transcribe_audio_with_groq(audio_url)
                if text:
                    send_message(chat_id, f"🎤 Я услышал: \"{text}\"\n\nОбрабатываю...")
                else:
                    send_message(chat_id, "⚠️ Не удалось распознать голос")
                    return JSONResponse({"ok": True})
        elif "text" in message:
            text = message["text"].strip()
        
        if text:
            result = await process_message(chat_id, text)
            send_message(chat_id, result["reply"])
        return JSONResponse({"ok": True})
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        response = requests.post(url, json=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Отправка: {e}")
        return False

async def process_message(user_id, text):
    user = get_user(user_id)
    if not user:
        save_user(user_id, level="Sapphire")

    level = get_user_level(user_id)
    tariff = TARIFFS.get(level, TARIFFS["Sapphire"])
    daily_limit = tariff["daily_limit"]
    model = tariff["model"]

    if user_id not in TEST_USERS:
        today_used = get_today_requests(user_id)
        extra = get_extra_requests(user_id)
        total_available = daily_limit + extra
        if today_used >= total_available:
            return {"reply": f"Дневной лимит для уровня {level} исчерпан"}
        log_request(user_id)
    else:
        log_request(user_id)

    save_message(user_id, "user", text)
    mood = analyze_mood(text)
    if mood != "neutral":
        update_user_mood(user_id, mood)

    keywords = ["квартир", "дом", "инвестиц", "ремонт", "работа", "машина", "билет", "отпуск", "здоровье"]
    for word in keywords:
        if word in text.lower():
            save_topic(user_id, word)

    lower = text.lower()

    # === ПОИСК ===
    search_result = None
    search_triggers = ["новости", "последние", "сегодня", "сейчас", "актуальные", "свежие", "прогноз", "курс", "погода", "время", "сколько", "дата", "найди", "поищи", "узнай", "какой", "где", "кто", "что такое", "ссылка", "картинка", "видео", "товар", "цена", "купить", "продажа", "объявление", "дром", "авито", "хавал", "haval"]
    
    if any(word in lower for word in search_triggers):
        print(f"🔍 Поиск: {text}")
        search_result = await search_web(text)
        if search_result:
            text = text + f"\n\n🔍 Актуальная информация:\n{search_result}"
            print("✅ Найдено!")
        else:
            print("❌ Ничего не найдено")

    # === ОПРЕДЕЛЕНИЕ ВРЕМЕНИ (ПРОСТОЙ СПОСОБ) ===
    # Берём точное московское время с сервера Render
    moscow_now = datetime.utcnow() + timedelta(hours=3)
    current_date = moscow_now.strftime("%d.%m.%Y")
    current_day = moscow_now.strftime("%A")
    current_time = moscow_now.strftime("%H:%M")

    # === КОМАНДЫ ===
    if "/задача" in lower or text.startswith("/task"):
        parts = text.split(" ", 1)
        if len(parts) >= 2:
            task_text = parts[1]
            task_id = add_task(user_id, task_text)
            reply = f"✅ Задача добавлена! ID: {task_id}\n📝 {task_text}"
        else:
            reply = "Формат: /задача [текст задачи]"
    
    elif "/задачи" in lower or text.startswith("/tasks"):
        tasks = get_tasks(user_id, "active")
        if tasks:
            lines = ["📋 Твои задачи:"]
            priority_emoji = {"high": "🔴", "normal": "🟡", "low": "🟢"}
            for task in tasks:
                task_id, task_text, priority, status, due_date = task
                emoji = priority_emoji.get(priority, "🟡")
                date_str = f" (до {due_date})" if due_date else ""
                lines.append(f"{emoji} #{task_id} {task_text}{date_str}")
            reply = "\n".join(lines) + f"\n\n📊 Всего: {len(tasks)} активных задач"
        else:
            reply = "🎉 Нет активных задач!"
    
    elif "/выполнить" in lower or text.startswith("/done"):
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                task_id = int(parts[1])
                if complete_task(user_id, task_id):
                    reply = f"✅ Задача #{task_id} выполнена! 🎉"
                else:
                    reply = f"❌ Задача #{task_id} не найдена"
            except ValueError:
                reply = "❌ Неверный ID. Используй: /выполнить [ID]"
        else:
            reply = "Формат: /выполнить [ID задачи]"
    
    elif "/удалить" in lower or text.startswith("/del"):
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                task_id = int(parts[1])
                if delete_task(user_id, task_id):
                    reply = f"🗑️ Задача #{task_id} удалена"
                else:
                    reply = f"❌ Задача #{task_id} не найдена"
            except ValueError:
                reply = "❌ Неверный ID. Используй: /удалить [ID]"
        else:
            reply = "Формат: /удалить [ID задачи]"
    
    elif "/напомни" in lower:
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            date_str = parts[1]
            reminder_text = parts[2]
            try:
                remind_date = datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
                save_reminder(user_id, reminder_text, remind_date)
                reply = f"⏰ Запомнил: {reminder_text} на {date_str}."
            except:
                reply = "❌ Неверный формат даты. Используй /напомни ГГГГ-ММ-ДД ТЕКСТ."
        else:
            reply = "Формат: /напомни ГГГГ-ММ-ДД ТЕКСТ."
    
    elif "/моинапоминания" in lower:
        reminders = get_reminders(user_id)
        if reminders:
            lines = ["⏰ Твои напоминания:"]
            for r in reminders:
                lines.append(f"- {r[0]} ({r[1]})")
            reply = "\n".join(lines)
        else:
            reply = "Нет напоминаний."
    
    elif "/докупить" in lower:
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                amount = int(parts[1])
                add_extra_requests(user_id, amount)
                reply = f"💰 Добавлено {amount} запросов. Всего доступно: {daily_limit + get_extra_requests(user_id)}."
            except:
                reply = "❌ Формат: /докупить [количество]."
        else:
            reply = "Формат: /докупить [количество]."
    
    elif "/остаток" in lower:
        if user_id in TEST_USERS:
            reply = "Для тестового пользователя лимитов нет."
        else:
            total = daily_limit + get_extra_requests(user_id) - get_today_requests(user_id)
            reply = f"📊 Осталось запросов на сегодня: {total}."
    
    elif "/помощь" in lower or "/help" in lower:
        reply = """🤖 **AURA — Помощь**

📋 **Задачи:**
/задача [текст] — добавить задачу
/задачи — показать все задачи
/выполнить [ID] — отметить как выполненную
/удалить [ID] — удалить задачу

⏰ **Напоминания:**
/напомни ГГГГ-ММ-ДД ТЕКСТ — создать напоминание
/моинапоминания — показать все напоминания

💰 **Запросы:**
/остаток — проверить остаток запросов
/докупить [количество] — добавить запросы

❓ Просто пиши, я отвечу! 😊"""
    
    else:
        user_name = get_memory(user_id, "name")
        if not user_name:
            name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", text.lower())
            if name_match:
                user_name = name_match.group(1).capitalize()
                save_memory(user_id, "name", user_name)
        
        name_context = f"\n\nИмя пользователя: {user_name}" if user_name else ""
        
        user_prompt = f"Сегодня {current_date} ({current_day}), сейчас {current_time}.\n\n{text}"

        current_mood = get_user_mood(user_id)
        aura_prompt = AURA_PROMPT + name_context + f"\n\n{user_prompt}"

        history = get_history(user_id, limit=30)
        messages = [{"role": "system", "content": aura_prompt}]
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": text})

        reply = await get_ai_response(messages, model)

    save_message(user_id, "assistant", reply)
    return {"reply": reply}

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
