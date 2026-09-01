import os
import logging
import requests
from fastapi import FastAPI, Request, Response
from supabase import create_client

logging.basicConfig(level=logging.INFO)
app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# === Функция вызова Яндекса (исправленный парсинг) ===
def call_yandex_agent(text):
    try:
        url = "https://llm.api.cloud.yandex.net/v1/completion"
        headers = {
            "Authorization": f"Api-Key {YANDEX_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite",
            "completionOptions": {"temperature": 0.6},
            "messages": [{"role": "user", "text": text}]
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        result = response.json()
        # Исправленный путь к тексту ответа
        return result.get('result', {}).get('alternatives', [{}])[0].get('message', {}).get('text', "Не понял")
    except Exception as e:
        logging.error(f"Ошибка вызова Яндекса: {e}")
        return f"Ошибка: {str(e)}"

# === Обработчик сообщений ===
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

        # Сохраняем запрос в историю (если Supabase доступен)
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

        # Получаем ответ от Яндекса
        answer = call_yandex_agent(user_text)

        # Сохраняем ответ в историю
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                supabase.table("history").insert({
                    "user_id": chat_id,
                    "role": "assistant",
                    "content": answer
                }).execute()
            except Exception as e:
                logging.warning(f"Ошибка записи в Supabase: {e}")

        # Отправляем ответ в Telegram
        telegram_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(telegram_url, json={"chat_id": chat_id, "text": answer})

        return Response(status_code=200)

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        return Response(status_code=500)

# === Здоровье бота ===
@app.get("/")
def home():
    return {"status": "AURA работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
