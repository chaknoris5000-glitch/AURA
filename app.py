import os
import logging
import requests
from fastapi import FastAPI, Request, Response
from supabase import create_client
from yandex_ai_studio_sdk import AIStudio

logging.basicConfig(level=logging.INFO)
app = FastAPI()

# === Переменные окружения ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# === ID агентов ===
AGENTS = {
    "search": "fvt3te2kgttig7u3a1fb",
    "researcher": "fvti80ngse2778agbmdl",
    "reasoning": "fvtg0c38oi7n43d0n9gf"
}

def detect_agent(text):
    text_lower = text.lower()
    if any(word in text_lower for word in ["сравни", "проанализируй", "исследуй", "динамика", "статистика"]):
        return AGENTS["researcher"]
    elif any(word in text_lower for word in ["посоветуй", "что лучше", "стоит ли", "как думаешь"]):
        return AGENTS["reasoning"]
    else:
        return AGENTS["search"]

def call_yandex_agent(text, agent_id):
    try:
        sdk = AIStudio(folder_id=YANDEX_FOLDER_ID, auth=YANDEX_API_KEY)
        # Правильный формат URI агента
        model = sdk.models.completions(f"agent:{agent_id}")
        model = model.configure(temperature=0.6)
        result = model.run(text)
        return result.alternatives[0].text
    except Exception as e:
        logging.error(f"Ошибка вызова агента: {e}")
        return f"Ошибка: {str(e)}"

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        if 'message' not in data:
            return Response(status_code=200)

        message = data['message']
        chat_id = message['chat']['id']
        user_text = message.get('text', '')

        if not user_text:
            return Response(status_code=200)

        if SUPABASE_URL and SUPABASE_KEY:
            try:
                supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
                supabase.table("history").insert({
                    "user_id": chat_id,
                    "role": "user",
                    "content": user_text
                }).execute()
            except Exception as e:
                logging.warning(f"Ошибка записи в Supabase: {e}")

        agent_id = detect_agent(user_text)
        answer = call_yandex_agent(user_text, agent_id)

        if SUPABASE_URL and SUPABASE_KEY:
            try:
                supabase.table("history").insert({
                    "user_id": chat_id,
                    "role": "assistant",
                    "content": answer
                }).execute()
            except Exception as e:
                logging.warning(f"Ошибка записи в Supabase: {e}")

        telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(telegram_url, json={"chat_id": chat_id, "text": answer})

        return Response(status_code=200)

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        return Response(status_code=500)

@app.get("/")
def home():
    return {"status": "AURA работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
