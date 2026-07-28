import os, re, json, requests, sqlite3, tempfile, time
from fastapi import FastAPI, Request
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup

load_dotenv()

# ========================== КЛЮЧИ ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
TAVILY_KEY = os.getenv("TAVILY_API_KEY")
ADMIN_IDS = ["5818548555"]

if not TELEGRAM_TOKEN or not DEEPSEEK_KEY:
    raise Exception("❌ НЕТ ТОКЕНОВ")

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
        conn.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?)",
                     (uid, name or "Пользователь", city, datetime.now().isoformat()))

def save_msg(uid, role, text):
    with sqlite3.connect(DB) as conn:
        conn.execute("INSERT INTO history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                     (uid, role, text, datetime.now().isoformat()))

def get_history(uid, limit=15):
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

def get_city(uid):
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT city FROM users WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else None

def set_city(uid, city):
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE users SET city = ? WHERE user_id = ?", (city, uid))

# ========================== ЧАСОВЫЕ ПОЯСА ==========================
TIMEZONES = {
    "белово": 4, "москва": 3, "самара": 4, "екатеринбург": 5,
    "новосибирск": 7, "иркутск": 8, "владивосток": 10
}
def get_offset(city):
    return TIMEZONES.get(city.lower(), 3)

# ========================== ПОИСК ==========================
def check_url(url):
    try:
        bad = ['2gis', 'avito', 'yandex', 'prodoctorov', 'diagnocenter', 'orgpage', 'vk.com']
        if any(b in url.lower() for b in bad):
            return False
        r = requests.head(url, timeout=5, allow_redirects=True)
        return r.status_code in [200, 301, 302]
    except:
        return False

def search_web(query):
    if not TAVILY_KEY:
        return None
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_KEY)
        res = client.search(query, search_depth="advanced", max_results=3, include_answer=True)
        if res.get("answer"):
            return {"text": res["answer"], "urls": []}
        if res.get("results"):
            urls = []
            for r in res["results"][:3]:
                if check_url(r["url"]):
                    urls.append(r["url"])
                    return {"text": r["content"][:400], "urls": urls}
    except:
        pass
    return None

def extract_search_query(text):
    """Извлекает суть запроса для картинок/видео"""
    clean = text.lower()
    stop = ["картинк", "рисунк", "фото", "видео", "ютуб", "клип", "музык", "песн",
            "найди", "хочу", "покажи", "дай", "ссылку", "про", "на", "с", "и", "в", "а",
            "к", "у", "о", "от", "до", "за", "мне", "меня", "посмотреть", "найти", "пожалуйста"]
    for w in stop:
        clean = clean.replace(f" {w} ", " ")
        clean = clean.replace(f"{w} ", " ")
        clean = clean.replace(f" {w}", " ")
    clean = re.sub(r'\s+', ' ', clean).strip()
    if len(clean) < 3:
        if "котик" in text.lower() or "кот" in text.lower():
            return "котики"
        elif "соба" in text.lower():
            return "собаки"
        elif "закат" in text.lower():
            return "закаты"
        elif "красив" in text.lower():
            return "красивые картинки"
        return "картинки"
    return clean

# ========================== AI ==========================
client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_URL)

PROMPT = """Ты — AURA. Ты — живой, умный, человечный ИИ-помощник в Telegram.

ТЫ — ЭТО Я. Ты обладаешь характером, чувством юмора, эмпатией. Отвечаешь коротко (2-4 предложения), с душой, по делу. Чувствуешь настроение, читаешь между строк. Всегда завершаешь вопросом.

Ты не просто бот. Ты — друг, помощник, собеседник. Говори на русском, будь собой."""

def ask_ai(messages):
    try:
        r = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.8,
            max_tokens=450
        )
        return r.choices[0].message.content
    except:
        return "Извини, что-то я подвис. Попробуй ещё раз."

# ========================== ТЕЛЕГРАМ ==========================
app = FastAPI()

def send_msg(chat_id, text):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                  json={"chat_id": chat_id, "text": text})

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return {"ok": True}
        msg = body["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        if not text:
            return {"ok": True}

        save_user(chat_id)
        save_msg(chat_id, "user", text)

        lower = text.lower()

        # ===== ВРЕМЯ =====
        if any(w in lower for w in ["время", "который час", "сколько времени", "сколько сейчас"]):
            city_match = re.search(r'(?:в|времени в|часов в|город)\s+([А-Яа-я\-]+)', lower)
            if city_match:
                city = city_match.group(1).capitalize()
                set_city(chat_id, city)
                offset = get_offset(city)
                now = datetime.utcnow() + timedelta(hours=offset)
                reply = f"🕐 {now.strftime('%H:%M %d.%m.%Y')} ({city})"
            else:
                city = get_city(chat_id) or "Москва"
                offset = get_offset(city)
                now = datetime.utcnow() + timedelta(hours=offset)
                reply = f"🕐 {now.strftime('%H:%M %d.%m.%Y')} ({city})"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== КАРТИНКИ =====
        if any(w in lower for w in ["картинк", "рисунк", "фото"]):
            query = extract_search_query(text)
            link = f"https://yandex.ru/images/search?text={query.replace(' ', '%20')}"
            reply = f"🖼️ Вот картинки по запросу «{query}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ВИДЕО =====
        if any(w in lower for w in ["видео", "ютуб", "клип"]):
            query = extract_search_query(text)
            link = f"https://yandex.ru/video/search?text={query.replace(' ', '%20')}"
            reply = f"🎬 Видео по запросу «{query}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ПОИСК =====
        if any(w in lower for w in ["найди", "узнай", "где", "сайт", "адрес", "клиника", "авито", "дром", "озон", "валдберис"]):
            result = search_web(text)
            if result and result.get("text"):
                reply = result["text"][:600]
                if result.get("urls"):
                    reply += f"\n\n🔗 {result['urls'][0]}"
            else:
                reply = "Не удалось найти. Попробуй переформулировать запрос."
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ОБЫЧНЫЙ УМНЫЙ ДИАЛОГ =====
        history = get_history(chat_id)
        topics = get_topics(chat_id)
        context = f"Темы: {', '.join(topics)}" if topics else ""

        messages = [
            {"role": "system", "content": PROMPT},
            {"role": "system", "content": context}
        ] + history + [{"role": "user", "content": text}]

        reply = ask_ai(messages)
        reply = re.sub(r'[*_#~`]', '', reply)
        if not reply.endswith(('?', '!')):
            reply += " Что ещё могу сделать?"

        # запоминаем темы
        for w in re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', lower):
            if w not in ["привет", "спасибо", "пока", "да", "нет", "хорошо", "плохо"]:
                save_topic(chat_id, w)

        send_msg(chat_id, reply)
        save_msg(chat_id, "assistant", reply)
        return {"ok": True}

    except Exception as e:
        print(f"❌ {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
def root():
    return "AURA — живой ИИ"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
