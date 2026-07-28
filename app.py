import os, re, json, requests, sqlite3, html, tempfile
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
GROQ_KEY = os.getenv("GROQ_API_KEY")

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

# ========================== РЕАЛЬНОЕ ВРЕМЯ ==========================
def get_real_time(city):
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

# ========================== ПРОВЕРКА ССЫЛОК ==========================
def check_url(url):
    try:
        r = requests.head(url, timeout=5, allow_redirects=True)
        return r.status_code in [200, 301, 302]
    except:
        return False

# ========================== ПОИСК (TAVILY + DUCK) ==========================
def search_web(query):
    if TAVILY_KEY:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_KEY)
            res = client.search(query, search_depth="advanced", max_results=3, include_answer=True)
            if res.get("answer"):
                return {"text": res["answer"][:500], "urls": []}
            if res.get("results"):
                for r in res["results"][:3]:
                    if check_url(r["url"]):
                        return {"text": r["content"][:400], "urls": [r["url"]]}
        except:
            pass
    try:
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for result in soup.select(".result")[:3]:
            title = result.select_one(".result__title")
            snippet = result.select_one(".result__snippet")
            link = result.select_one(".result__url")
            if title and snippet and link:
                link_text = html.unescape(link.text.strip())
                if check_url(link_text):
                    return {
                        "text": f"{html.unescape(title.text.strip())}\n{html.unescape(snippet.text.strip())}",
                        "urls": [link_text]
                    }
    except:
        pass
    return None

# ========================== AI ==========================
client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_URL)

def ask_ai(messages, max_tokens=450):
    try:
        r = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.8,
            max_tokens=max_tokens
        )
        return r.choices[0].message.content
    except:
        return None

PROMPT = """Ты — AURA. Ты — живой умный помощник в Telegram.

ТЫ — ЭТО Я. Ты обладаешь характером, чувством юмора, эмпатией.
Отвечаешь коротко (2-4 предложения), с душой, по делу.
Чувствуешь настроение, читаешь между строк.
Всегда завершаешь вопросом.

Ты — AURA. Не важно, какой движок внутри — для пользователя ты просто AURA.
Не врёшь, не выдумываешь факты. Если не знаешь — говори честно.

Говори на русском. Будь собой."""

def rewrite_query(user_text):
    messages = [
        {"role": "system", "content": "Перепиши запрос пользователя для поиска: оставь только ключевые слова, убери стоп-слова. Ответь одной фразой на русском."},
        {"role": "user", "content": user_text}
    ]
    return ask_ai(messages, max_tokens=60)

def analyze_intent(text):
    messages = [
        {"role": "system", "content": "Определи намерение. Ответь: search, image, video, time, chat."},
        {"role": "user", "content": text}
    ]
    return ask_ai(messages, max_tokens=20)

# ========================== ГОЛОС ==========================
def transcribe_audio(file_url):
    if not GROQ_KEY:
        return None
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_KEY)
        audio = requests.get(file_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp.write(audio.content)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=(tmp_path, f.read()),
                model="whisper-large-v3-turbo",
                language="ru"
            )
        os.unlink(tmp_path)
        return transcription.text
    except:
        return None

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
        text = None

        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}")
            if file_resp.status_code == 200:
                file_path = file_resp.json()["result"]["file_path"]
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                text = transcribe_audio(audio_url)
                if not text:
                    send_msg(chat_id, "⚠️ Не смог распознать голос. Попробуй ещё раз.")
                    return {"ok": True}
        else:
            text = msg.get("text", "").strip()

        if not text:
            return {"ok": True}

        save_msg(chat_id, "user", text)
        lower = text.lower()

        # ===== ВРЕМЯ =====
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
                reply = f"🕐 Не удалось проверить точное время."
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== АНАЛИЗ НАМЕРЕНИЯ =====
        intent = analyze_intent(text) or "chat"

        # ===== КАРТИНКИ =====
        if "image" in intent or "картинк" in lower or "рисунк" in lower or "фото" in lower:
            rewritten = rewrite_query(text) or text
            q = re.sub(r'[^а-яА-Яa-zA-Z0-9 ]', '', rewritten).strip()
            if not q:
                q = "красивые картинки"
            link = f"https://yandex.ru/images/search?text={q.replace(' ', '%20')}"
            reply = f"🖼️ Картинки по запросу «{q}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ВИДЕО =====
        if "video" in intent or "видео" in lower or "ютуб" in lower or "клип" in lower:
            rewritten = rewrite_query(text) or text
            q = re.sub(r'[^а-яА-Яa-zA-Z0-9 ]', '', rewritten).strip()
            if not q:
                q = "смешные видео"
            link = f"https://yandex.ru/video/search?text={q.replace(' ', '%20')}"
            reply = f"🎬 Видео по запросу «{q}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ПОИСК =====
        if "search" in intent:
            rewritten = rewrite_query(text) or text
            result = search_web(rewritten)
            if result and result.get("urls"):
                reply = f"{result['text'][:400]}\n\n🔗 {result['urls'][0]}"
            elif result and result.get("text"):
                reply = result["text"][:400] + "\n\n⚠️ Ссылку проверить не удалось."
            else:
                reply = "Не нашёл. Попробуй переформулировать."
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ДИАЛОГ =====
        history = get_history(chat_id)
        topics = get_topics(chat_id)
        context = f"Темы: {', '.join(topics)}" if topics else ""

        messages = [{"role": "system", "content": PROMPT}] + history + [{"role": "user", "content": text}]
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
