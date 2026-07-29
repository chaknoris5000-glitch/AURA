import os, re, json, requests, sqlite3, html, tempfile
from fastapi import FastAPI, Request
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
OPENSERP_URL = os.getenv("OPENSERP_URL", "http://localhost:7000")  # можно поменять на адрес на Render
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

def search_in_history(uid, keyword):
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM history WHERE user_id = ? AND content LIKE ? ORDER BY created_at DESC LIMIT 10",
            (uid, f"%{keyword}%")
        ).fetchall()
    return rows

def get_today_history(uid):
    today = datetime.now().date().isoformat()
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM history WHERE user_id = ? AND date(created_at) = ? ORDER BY created_at DESC LIMIT 20",
            (uid, today)
        ).fetchall()
    return rows

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

# ========================== ПОИСК ЧЕРЕЗ OPENSERP ==========================
def search_openserv(query, search_type="web"):
    """
    search_type: web, images, video
    """
    try:
        params = {
            "text": query,
            "engine": "yandex",
            "gl": "ru",
            "hl": "ru"
        }
        
        # Для картинок и видео меняем движок
        if search_type == "images":
            params["engine"] = "yandex_images"
        elif search_type == "video":
            params["engine"] = "yandex_video"
        
        resp = requests.get(f"{OPENSERP_URL}/search", params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            
            # Формируем ответ
            if results:
                text_results = []
                urls = []
                for r in results[:3]:
                    title = r.get("title", "")
                    snippet = r.get("snippet", "")
                    url = r.get("url", "")
                    if title and url:
                        text_results.append(f"{title}\n{snippet[:200]}...")
                        urls.append(url)
                
                return {
                    "text": "\n\n".join(text_results),
                    "urls": urls
                }
        
        return None
    except Exception as e:
        print(f"❌ OpenSERP ошибка: {e}")
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

def analyze_intent(text):
    messages = [
        {"role": "system", "content": "Определи намерение. Ответь: search, image, video, time, chat, remind."},
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

        # ==========================
        # 1. ВРЕМЯ
        # ==========================
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

        # ==========================
        # 2. НАПОМНИ
        # ==========================
        if "напомни" in lower or "что мы обсуждали" in lower or "что я спрашивал" in lower:
            keyword_match = re.search(r'напомни\s+(.+)', lower)
            if keyword_match:
                keyword = keyword_match.group(1).strip()
                found = search_in_history(chat_id, keyword)
                if found:
                    reply = "🧠 Вот что я нашёл по твоему запросу:\n\n"
                    for role, content, created_at in found[:5]:
                        time_str = datetime.fromisoformat(created_at).strftime("%H:%M")
                        role_label = "Ты" if role == "user" else "Я"
                        reply += f"[{time_str}] {role_label}: {content[:150]}...\n"
                else:
                    reply = f"🔍 Ничего не нашёл по запросу «{keyword}» в истории."
            else:
                topics = get_topics(chat_id)
                if topics:
                    reply = f"📚 Мы обсуждали: {', '.join(topics[:10])}"
                else:
                    today_history = get_today_history(chat_id)
                    if today_history:
                        reply = "📝 Вот что мы обсуждали сегодня:\n\n"
                        for role, content, created_at in today_history[:5]:
                            time_str = datetime.fromisoformat(created_at).strftime("%H:%M")
                            role_label = "Ты" if role == "user" else "Я"
                            reply += f"[{time_str}] {role_label}: {content[:100]}...\n"
                    else:
                        reply = "Мы пока ничего не обсуждали сегодня."
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ==========================
        # 3. КАРТИНКИ (через OpenSERP)
        # ==========================
        if "картинк" in lower or "рисунк" in lower or "фото" in lower:
            q = clean_query(text)
            result = search_openserv(q, search_type="images")
            if result and result.get("urls"):
                reply = f"🖼️ Картинки по запросу «{q}»:\n🔗 {result['urls'][0]}"
            else:
                link = f"https://yandex.ru/images/search?text={q.replace(' ', '%20')}"
                reply = f"🖼️ Картинки по запросу «{q}»:\n{link}"
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ==========================
        # 4. ВИДЕО (через OpenSERP)
        # ==========================
        if "видео" in lower or "ютуб" in lower or "клип" in lower:
            q = clean_query(text)
            result = search_openserv(q, search_type="video")
            if result and result.get("urls"):
                reply = f"🎬 Видео по запросу «{q}»:\n🔗 {result['urls'][0]}"
            else:
                link = f"https://yandex.ru/video/search?text={q.replace(' ', '%20')}"
                reply = f"🎬 Видео по запросу «{q}»:\n{link}"
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ==========================
        # 5. ПОИСК (через OpenSERP)
        # ==========================
        if any(w in lower for w in ["найди", "узнай", "где", "сайт", "адрес", "телефон", "клиника", "авито", "дром", "озон", "валдберис", "wildberries"]):
            result = search_openserv(text, search_type="web")
            if result and result.get("urls"):
                reply = f"{result['text'][:400]}\n\n🔗 {result['urls'][0]}"
            elif result and result.get("text"):
                reply = result["text"][:400]
            else:
                reply = "Не нашёл. Попробуй переформулировать."
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ==========================
        # 6. ДИАЛОГ
        # ==========================
        history = get_history(chat_id, limit=15)
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
