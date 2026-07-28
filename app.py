import os, re, json, requests, sqlite3, html
from fastapi import FastAPI, Request
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup

load_dotenv()

# ========================== КЛЮЧИ ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
ADMIN_IDS = ["5818548555"]

if not TELEGRAM_TOKEN or not DEEPSEEK_KEY:
    raise Exception("❌ НЕТ ТОКЕНОВ")

# ========================== БАЗА ==========================
DB = "aura.db"
def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, city TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, role TEXT, content TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS topics (user_id TEXT, topic TEXT)")
init_db()

def save_city(uid, city):
    with sqlite3.connect(DB) as conn:
        conn.execute("INSERT OR REPLACE INTO users (user_id, city) VALUES (?, ?)", (uid, city))

def get_city(uid):
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT city FROM users WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else None

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
        conn.execute("INSERT INTO topics (user_id, topic) VALUES (?, ?)", (uid, topic))

def get_topics(uid):
    with sqlite3.connect(DB) as conn:
        rows = conn.execute("SELECT topic FROM topics WHERE user_id = ? ORDER BY rowid DESC LIMIT 10", (uid,)).fetchall()
    return [r[0] for r in rows]

# ========================== РЕАЛЬНОЕ ВРЕМЯ ==========================
def get_real_time(city):
    try:
        resp = requests.get(f"http://worldtimeapi.org/api/timezone/Europe/Moscow", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            dt = datetime.fromisoformat(data["datetime"].replace("Z", "+00:00"))
            # Белово UTC+4, Москва UTC+3 — поправка +1 час
            if city.lower() in ["белово", "belovo"]:
                dt = dt + timedelta(hours=1)
            return dt.strftime("%H:%M %d.%m.%Y")
    except:
        pass
    return None

# ========================== ПОИСК (БЕЗ TAVILY) ==========================
def search_web(query):
    try:
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for result in soup.select(".result")[:2]:
            title = result.select_one(".result__title")
            snippet = result.select_one(".result__snippet")
            link = result.select_one(".result__url")
            if title and snippet and link:
                title_text = html.unescape(title.text.strip())
                snippet_text = html.unescape(snippet.text.strip())
                link_text = html.unescape(link.text.strip())
                if not any(bad in link_text for bad in ["2gis", "avito", "yandex", "prodoctorov"]):
                    return {
                        "title": title_text,
                        "text": snippet_text,
                        "url": link_text
                    }
    except:
        pass
    return None

# ========================== AI ==========================
client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_URL)

PROMPT = """Ты — AURA. Ты — живой умный помощник в Telegram.

ТЫ — ЭТО Я. Ты обладаешь характером, чувством юмора, эмпатией.
Отвечаешь коротко (2-4 предложения), с душой, по делу.
Чувствуешь настроение, читаешь между строк.
Всегда завершаешь вопросом: "Что ещё могу сделать?" или "Может, поищем что-то ещё?".

Ты — DeepSeek внутри, но для пользователя ты просто AURA.
Не врёшь, не выдумываешь факты. Если не знаешь — говори честно.

Говори на русском. Будь собой."""

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
        return "Извини, я подвис. Попробуй ещё раз."

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

        save_msg(chat_id, "user", text)
        lower = text.lower()

        # ===== ВРЕМЯ (РЕАЛЬНОЕ) =====
        if any(w in lower for w in ["время", "который час", "сколько времени"]):
            city_match = re.search(r'(?:в|времени в|часов в|город)\s+([А-Яа-я\-]+)', lower)
            city = city_match.group(1).capitalize() if city_match else None
            if city:
                save_city(chat_id, city)
            else:
                city = get_city(chat_id) or "Москва"
            time_str = get_real_time(city)
            if time_str:
                reply = f"🕐 {time_str} ({city})"
            else:
                reply = f"🕐 Сейчас около {datetime.now().strftime('%H:%M %d.%m.%Y')} (примерно, так как я не смог проверить точно)"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== КАРТИНКИ =====
        if any(w in lower for w in ["картинк", "рисунк", "фото"]):
            clean = re.sub(r'картинк|рисунк|фото|найди|хочу|покажи|дай', '', text, flags=re.I)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if not clean or len(clean) < 3:
                clean = "красивые картинки"
            link = f"https://yandex.ru/images/search?text={clean.replace(' ', '%20')}"
            reply = f"🖼️ Картинки по запросу «{clean}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ВИДЕО =====
        if any(w in lower for w in ["видео", "ютуб", "клип"]):
            clean = re.sub(r'видео|ютуб|клип|найди|хочу|покажи|дай', '', text, flags=re.I)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if not clean or len(clean) < 3:
                clean = "смешные видео"
            link = f"https://yandex.ru/video/search?text={clean.replace(' ', '%20')}"
            reply = f"🎬 Видео по запросу «{clean}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ПОИСК (DUCKDUCKGO) =====
        if any(w in lower for w in ["найди", "узнай", "где", "сайт", "адрес", "телефон", "клиника", "авито", "дром", "озон"]):
            result = search_web(text)
            if result:
                reply = f"{result['title']}\n{result['text']}\n🔗 {result['url']}"
            else:
                reply = "Ничего не нашёл через DuckDuckGo. Попробуй переформулировать или напиши конкретнее."
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ОБЫЧНЫЙ ДИАЛОГ =====
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
    return "AURA — без костылей"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
