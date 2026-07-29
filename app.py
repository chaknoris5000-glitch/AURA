import os, re, json, requests, sqlite3, html
from fastapi import FastAPI, Request
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
TAVILY_KEY = os.getenv("TAVILY_API_KEY")

if not TELEGRAM_TOKEN or not DEEPSEEK_KEY:
    raise Exception("❌ НЕТ ТОКЕНОВ")

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

# ========================== ВРЕМЯ ==========================
def get_time_for_city(city):
    try:
        if city.lower() in ["белово", "belovo"]:
            resp = requests.get("https://timeapi.io/api/Time/current/zone?timeZone=Asia/Novokuznetsk", timeout=5)
        else:
            resp = requests.get("https://timeapi.io/api/Time/current/zone?timeZone=Europe/Moscow", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return f"{data['hour']:02d}:{data['minute']:02d} {data['day']:02d}.{data['month']:02d}.{data['year']}"
    except:
        pass
    return None

# ========================== ПОИСК (БЕЗ ПАРСИНГА — ДЛЯ БЫСТРОТЫ) ==========================
async def search_web(query):
    results = []
    
    if TAVILY_KEY:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_KEY)
            resp = client.search(query, search_depth="advanced", max_results=3, include_answer=True)
            if resp.get("answer"):
                return resp["answer"] + "\n\nЧто ещё могу сделать?"
            if resp.get("results"):
                for r in resp["results"][:3]:
                    title = r.get("title", "")
                    url = r.get("url", "")
                    content = r.get("content", "")[:300]
                    if title and url:
                        results.append(f"{title}\n{content}\n🔗 {url}")
        except:
            pass
    
    if not results:
        try:
            url = f"https://html.duckduckgo.com/html/?q={query}"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select(".result")[:3]:
                title = result.select_one(".result__title")
                if title:
                    link = result.select_one(".result__url")
                    snippet = result.select_one(".result__snippet")
                    if snippet and link:
                        results.append(f"{title.text.strip()}\n{snippet.text.strip()[:200]}\n🔗 {link.text.strip()}")
        except:
            pass
    
    return "\n\n".join(results) if results else None

# ========================== AI ==========================
client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_URL)

PROMPT = """Ты — AURA. Ты — живой умный помощник в Telegram.
Отвечаешь коротко (2-4 предложения), с душой, по делу.
Всегда завершаешь вопросом: "Что ещё могу сделать?"
Говори только на русском. Без английских слов и фраз."""

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
        return None

def clean_query(text):
    stop = ["картинк", "рисунк", "фото", "видео", "ютуб", "клип", "музык", "песн",
            "найди", "хочу", "покажи", "дай", "ссылку", "про", "на", "с", "и", "в", "а",
            "к", "у", "о", "от", "до", "за", "мне", "меня", "посмотреть", "найти", "пожалуйста"]
    for w in stop:
        text = text.replace(f" {w} ", " ")
        text = text.replace(f"{w} ", " ")
        text = text.replace(f" {w}", " ")
    text = re.sub(r'\s+', ' ', text).strip()
    if not text or len(text) < 3:
        if "котик" in text or "кот" in text:
            return "котики"
        elif "закат" in text:
            return "закаты"
        elif "соба" in text:
            return "собаки"
        return "красивые картинки"
    return text

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

        # ВРЕМЯ
        if any(w in lower for w in ["время", "который час", "сколько времени"]):
            city_match = re.search(r'(?:в|времени в|часов в|город)\s+([А-Яа-я\-]+)', lower)
            city = city_match.group(1).capitalize() if city_match else get_city(chat_id) or "Москва"
            if city_match:
                save_city(chat_id, city)
            t = get_time_for_city(city)
            reply = f"🕐 {t} ({city})" if t else "Не удалось проверить время."
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # КАРТИНКИ
        if "картинк" in lower or "рисунк" in lower or "фото" in lower:
            q = clean_query(text)
            link = f"https://yandex.ru/images/search?text={q.replace(' ', '%20')}"
            reply = f"🖼️ Картинки по запросу «{q}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ВИДЕО
        if "видео" in lower or "ютуб" in lower or "клип" in lower:
            q = clean_query(text)
            link = f"https://yandex.ru/video/search?text={q.replace(' ', '%20')}"
            reply = f"🎬 Видео по запросу «{q}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ПОИСК
        if any(w in lower for w in ["найди", "узнай", "где", "сайт", "адрес", "телефон", "клиника", "авито", "дром", "озон", "валдберис"]):
            q = clean_query(text) or text
            result = await search_web(q)
            if result:
                reply = result
            else:
                reply = "Не нашёл. Попробуй переформулировать."
            if not reply.endswith("?"):
                reply += "\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ДИАЛОГ
        history = get_history(chat_id)
        topics = get_topics(chat_id)
        context = f"Темы: {', '.join(topics)}" if topics else ""

        messages = [{"role": "system", "content": PROMPT}]
        if context:
            messages.append({"role": "system", "content": f"Контекст: {context}"})
        messages += history
        messages.append({"role": "user", "content": text})

        reply = ask_ai(messages)
        if reply:
            reply = re.sub(r'[*_#~`]', '', reply)
            if not reply.endswith(('?', '!')):
                reply += " Что ещё могу сделать?"
        else:
            reply = "Извини, я подвис. Попробуй ещё раз."

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
    return "AURA"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
