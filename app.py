import os, json, re, requests, tempfile, time, threading
from fastapi import FastAPI, Request, Response
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup
import sqlite3

load_dotenv()

# ========================== КОНФИГ ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
TAVILY_KEY = os.getenv("TAVILY_API_KEY")
ADMIN_IDS = ["5818548555"]

if not TELEGRAM_TOKEN or not DEEPSEEK_KEY:
    raise SystemExit("❌ Нет токена Telegram или DeepSeek")

# ========================== БАЗА ==========================
DB = "aura.db"
def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, name TEXT, city TEXT, created_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, role TEXT, content TEXT, created_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS topics (
            user_id TEXT, topic TEXT, created_at TEXT
        )""")
init_db()

def save_user(uid, name=None, city=None):
    with sqlite3.connect(DB) as conn:
        conn.execute("INSERT OR REPLACE INTO users (user_id, name, city, created_at) VALUES (?, ?, ?, ?)",
                     (uid, name or "Пользователь", city, datetime.now().isoformat()))

def save_msg(uid, role, text):
    with sqlite3.connect(DB) as conn:
        conn.execute("INSERT INTO history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                     (uid, role, text, datetime.now().isoformat()))

def get_history(uid, limit=20):
    with sqlite3.connect(DB) as conn:
        rows = conn.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (uid, limit)).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows[::-1]]

def save_topic(uid, topic):
    with sqlite3.connect(DB) as conn:
        conn.execute("INSERT INTO topics (user_id, topic, created_at) VALUES (?, ?, ?)",
                     (uid, topic, datetime.now().isoformat()))

def get_topics(uid):
    with sqlite3.connect(DB) as conn:
        rows = conn.execute("SELECT topic FROM topics WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (uid,)).fetchall()
    return [r[0] for r in rows]

# ========================== ПОИСК ==========================
def search_web(query):
    if not TAVILY_KEY:
        return None
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_KEY)
        res = client.search(query, search_depth="advanced", max_results=3, include_answer=True)
        if res.get("answer"):
            return res["answer"]
        if res.get("results"):
            return "\n".join([f"{r['title']}: {r['content'][:200]}" for r in res["results"][:3]])
    except:
        return None
    return None

# ========================== AI ==========================
client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_URL)

PROMPT = """Ты — AURA, живой и умный ИИ-помощник в Telegram.

ТЫ — ЭТО Я. Ты обладаешь характером, чувством юмора, эмпатией. Ты отвечаешь коротко, но с душой (2–4 предложения). Ты понимаешь контекст, читаешь между строк, чувствуешь настроение.

Твой стиль: прямо, тепло, с лёгкой иронией, по делу. Ты всегда заканчиваешь вопросом: «Что ещё могу сделать?» или «Может, поищем что-то ещё?».

Ты — это DeepSeek, но с человеческим лицом. Ты не просто бот — ты друг и помощник."""

def ask_ai(messages):
    try:
        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.8,
            max_tokens=500
        )
        return resp.choices[0].message.content
    except:
        return "Извини, я сейчас немного занят. Повтори через пару секунд."

# ========================== ТЕЛЕГРАМ ==========================
app = FastAPI()

def send_msg(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return Response("ok")
        msg = body["message"]
        chat_id = str(msg["chat"]["id"])
        user_text = msg.get("text", "").strip()
        if not user_text:
            return Response("ok")

        save_user(chat_id)
        save_msg(chat_id, "user", user_text)

        # --- ВРЕМЯ ---
        if any(w in user_text.lower() for w in ["время", "который час", "сколько времени"]):
            city = re.search(r'(?:в|времени в|часов в)\s+([А-Яа-я\-]+)', user_text.lower())
            if city:
                city = city.group(1).capitalize()
                with sqlite3.connect(DB) as conn:
                    conn.execute("UPDATE users SET city = ? WHERE user_id = ?", (city, chat_id))
                now = datetime.utcnow() + timedelta(hours=4 if city.lower() == "белово" else 3)
            else:
                now = datetime.utcnow() + timedelta(hours=3)
            reply = f"🕐 {now.strftime('%H:%M %d.%m.%Y')}"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return Response("ok")

        # --- КАРТИНКИ / ВИДЕО (понимание сути) ---
        if any(w in user_text.lower() for w in ["картинк", "фото", "рисунк"]):
            clean = re.sub(r'картинк|фото|рисунк|найди|хочу|покажи|дай', '', user_text, flags=re.I)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if not clean:
                clean = "красивые картинки"
            reply = f"🖼️ Вот что я нашёл по запросу «{clean}»:\nhttps://yandex.ru/images/search?text={clean.replace(' ', '%20')}"
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return Response("ok")

        if any(w in user_text.lower() for w in ["видео", "ютуб", "клип"]):
            clean = re.sub(r'видео|ютуб|клип|найди|хочу|покажи|дай', '', user_text, flags=re.I)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if not clean:
                clean = "смешные видео"
            reply = f"🎬 Видео по запросу «{clean}»:\nhttps://yandex.ru/video/search?text={clean.replace(' ', '%20')}"
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return Response("ok")

        # --- ПОИСК ИНФОРМАЦИИ ---
        if any(w in user_text.lower() for w in ["найди", "узнай", "где", "сайт", "адрес", "клиника", "авито"]):
            web_result = search_web(user_text)
            if web_result:
                reply = web_result[:700]
            else:
                reply = "Я не смог найти это в интернете. Попробуй переформулировать запрос."
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return Response("ok")

        # --- ОБЫЧНЫЙ УМНЫЙ ДИАЛОГ ---
        history = get_history(chat_id)
        topics = get_topics(chat_id)
        context = f"Темы, которые мы обсуждали: {', '.join(topics)}" if topics else ""
        messages = [
            {"role": "system", "content": PROMPT},
            {"role": "system", "content": context}
        ] + history + [{"role": "user", "content": user_text}]

        reply = ask_ai(messages)
        reply = re.sub(r'[*_#~`]', '', reply)
        if not reply.endswith(('?', '!')):
            reply += " Что ещё могу сделать?"

        # --- ПАМЯТЬ (запоминаем темы) ---
        words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', user_text.lower())
        for w in words:
            if w not in ["привет", "спасибо", "пока", "да", "нет", "хорошо"]:
                save_topic(chat_id, w)

        send_msg(chat_id, reply)
        save_msg(chat_id, "assistant", reply)
        return Response("ok")

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return Response("error", status_code=500)

@app.get("/")
def root():
    return "AURA — живой ИИ в Telegram"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
