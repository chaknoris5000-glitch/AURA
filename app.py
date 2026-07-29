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
TAVILY_KEY = os.getenv("TAVILY_API_KEY")
GROQ_KEY = os.getenv("GROQ_API_KEY")

if not TELEGRAM_TOKEN or not DEEPSEEK_KEY:
    raise Exception("❌ НЕТ ТОКЕНОВ")

# ========================== TAVILY ==========================
tavily_client = None
if TAVILY_KEY:
    try:
        from tavily import TavilyClient
        tavily_client = TavilyClient(api_key=TAVILY_KEY)
        print("✅ Tavily инициализирован")
    except:
        print("⚠️ Tavily не подключён")

DB = "aura.db"
def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, city TEXT, last_search TEXT)")
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

def save_last_search(uid, query):
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE users SET last_search = ? WHERE user_id = ?", (query, uid))

def get_last_search(uid):
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT last_search FROM users WHERE user_id = ?", (uid,)).fetchone()
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

# ========================== ВРЕМЯ ==========================
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

# ========================== ГЛУБОКИЙ ПАРСИНГ ==========================

def parse_site_for_info(url):
    """Парсит сайт: телефоны, адреса, цены, описания"""
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
        
        phone_patterns = [
            r'\+7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}',
            r'8\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}',
            r'7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}'
        ]
        phones = []
        for pattern in phone_patterns:
            phones.extend(re.findall(pattern, text))
        phones = [re.sub(r'\s+', ' ', p).strip() for p in phones]
        phones = list(set(phones))[:5]
        if phones:
            result["phones"] = phones
        
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        emails = list(set(re.findall(email_pattern, text)))[:3]
        if emails:
            result["emails"] = emails
        
        address_pattern = r'(?:ул\.|улица|проспект|пр\.|переулок|пер\.|площадь|пл\.|шоссе|бульвар)\s+[А-Яа-я0-9\-\.\s,]+'
        addresses = list(set(re.findall(address_pattern, text)))[:3]
        if addresses:
            result["addresses"] = addresses
        
        price_pattern = r'(\d+[\s,.]*\d*)\s*(?:₽|руб|рублей|\$|€)'
        prices = list(set(re.findall(price_pattern, text)))[:5]
        if prices:
            result["prices"] = prices
        
        site_pattern = r'(?:https?://)?(?:www\.)?([a-zA-Z0-9\-]+\.(?:ru|рф|com|org|net))'
        sites = list(set(re.findall(site_pattern, text)))[:3]
        if sites:
            result["sites"] = sites
        
        title = soup.find('h1')
        if title:
            result["product_title"] = title.text.strip()
        
        desc = soup.find(class_=re.compile(r'description|about|product-desc|product__description'))
        if desc:
            result["product_description"] = desc.text.strip()[:500]
        
        result["snippet"] = text[:1000].replace("\n", " ")
        
        return result
    except Exception as e:
        print(f"❌ Ошибка парсинга: {e}")
        return None

# ========================== ОСНОВНАЯ ФУНКЦИЯ ПОИСКА ==========================

async def search_web(query):
    """Глубокий поиск через Tavily + DuckDuckGo + парсинг"""
    results = []
    
    # 1. TAVILY
    if tavily_client:
        try:
            response = tavily_client.search(
                query=query,
                search_depth="advanced",
                max_results=5,
                include_answer=True,
                include_images=False
            )
            if response.get('answer'):
                results.append(f"💡 {response['answer']}")
            if response.get('results'):
                for r in response['results'][:5]:
                    title = r.get('title', '')
                    url = r.get('url', '')
                    content = r.get('content', '')[:300]
                    if title and url:
                        results.append(f"**{title}**\n{content}...\n🔗 {url}")
        except Exception as e:
            print(f"❌ Tavily: {e}")
    
    # 2. DUCKDUCKGO
    if not results:
        try:
            url = f"https://html.duckduckgo.com/html/?q={query}"
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            for result in soup.select('.result')[:3]:
                title = result.select_one('.result__title')
                if title:
                    link = result.select_one('.result__url')
                    snippet = result.select_one('.result__snippet')
                    if snippet and link:
                        results.append(f"**{title.text.strip()}**\n{snippet.text.strip()[:200]}...\n🔗 {link.text.strip()}")
        except Exception as e:
            print(f"❌ DuckDuckGo: {e}")
    
    # 3. ПАРСИНГ НАЙДЕННЫХ САЙТОВ
    urls = re.findall(r'https?://[^\s]+', "\n".join(results))
    for url in urls[:3]:
        parsed = parse_site_for_info(url)
        if parsed:
            if parsed.get("phones"):
                results.append(f"📞 Телефоны: {', '.join(parsed['phones'])}")
            if parsed.get("addresses"):
                results.append(f"📍 Адреса: {', '.join(parsed['addresses'])}")
            if parsed.get("prices"):
                results.append(f"💰 Цены: {', '.join(parsed['prices'])}")
            if parsed.get("emails"):
                results.append(f"✉️ Email: {', '.join(parsed['emails'])}")
            if parsed.get("sites"):
                results.append(f"🌐 Сайты: {', '.join(parsed['sites'])}")
            if parsed.get("product_title"):
                results.append(f"📦 Товар: {parsed['product_title']}")
            if parsed.get("product_description"):
                results.append(f"📝 Описание: {parsed['product_description'][:200]}...")
    
    return "\n\n".join(results) if results else None

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

Говори на русском. ТОЛЬКО НА РУССКОМ. Ни слова на английском."""

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

        # ===== ССЫЛКА =====
        if "ссылк" in lower or "сайт" in lower or "адрес" in lower:
            last_search = get_last_search(chat_id)
            if last_search:
                result = await search_web(last_search)
                if result:
                    reply = result
                else:
                    reply = f"Не нашёл ссылку по запросу «{last_search}»."
            else:
                reply = "Ты ещё ничего не искал. Напиши, что нужно найти, и я поищу."
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== НАПОМНИ =====
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

        # ===== КАРТИНКИ =====
        if "картинк" in lower or "рисунк" in lower or "фото" in lower:
            q = clean_query(text)
            link = f"https://yandex.ru/images/search?text={q.replace(' ', '%20')}"
            reply = f"🖼️ Картинки по запросу «{q}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ВИДЕО =====
        if "видео" in lower or "ютуб" in lower or "клип" in lower:
            q = clean_query(text)
            link = f"https://yandex.ru/video/search?text={q.replace(' ', '%20')}"
            reply = f"🎬 Видео по запросу «{q}»:\n{link}\n\nЧто ещё могу сделать?"
            send_msg(chat_id, reply)
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ПОИСК (ОСНОВНОЙ) =====
        if any(w in lower for w in ["найди", "узнай", "где", "телефон", "контакт", "клиника", "авито", "дром", "озон", "валдберис", "wildberries"]):
            q = clean_query(text)
            if not q or len(q) < 3:
                q = text
            
            save_last_search(chat_id, q)
            
            search_result = await search_web(q)
            
            if search_result:
                reply = search_result
            else:
                reply = "Не нашёл. Попробуй переформулировать."
            
            send_msg(chat_id, reply + "\n\nЧто ещё могу сделать?")
            save_msg(chat_id, "assistant", reply)
            return {"ok": True}

        # ===== ДИАЛОГ =====
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
