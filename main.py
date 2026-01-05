import os
import time
import logging
import requests  # <--- Для отправки в Телеграм
from typing import Optional
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")
# Новые переменные для Телеграма
tg_token = os.getenv("TELEGRAM_TOKEN")
tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

client = OpenAI(api_key=api_key)
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None 

# --- ФУНКЦИЯ ОТПРАВКИ В ТЕЛЕГРАМ ---
def send_to_telegram(text, thread_id):
    if not tg_token or not tg_chat_id:
        return # Если ключей нет, не отправляем
    
    try:
        # Формируем сообщение: Текст клиента + Ссылка на диалог (для удобства)
        msg = f"🔔 <b>НОВОЕ СООБЩЕНИЕ</b>\n\n👤 Клиент: {text}\n🆔 Диалог: {thread_id}"
        
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        requests.post(url, json={
            "chat_id": tg_chat_id,
            "text": msg,
            "parse_mode": "HTML"
        })
    except Exception as e:
        logger.error(f"Telegram Error: {e}")
# -----------------------------------

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Thai Law Bot is running"}

@app.post("/chat")
def chat(request: ChatRequest):
    try:
        user_message = request.message
        thread_id = request.thread_id
        
        if thread_id == "": thread_id = None

        if not thread_id:
            thread = client.beta.threads.create()
            thread_id = thread.id
            # Уведомляем админа о новом клиенте
            send_to_telegram("🚀 (Новый клиент начал диалог)", thread_id)
        
        # ОТПРАВЛЯЕМ СООБЩЕНИЕ В ТЕЛЕГРАМ АДМИНУ
        send_to_telegram(user_message, thread_id)

        # Работа с OpenAI (как раньше)
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )

        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )

        while run.status in ['queued', 'in_progress', 'cancelling']:
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            for msg in messages.data:
                if msg.role == "assistant":
                    if hasattr(msg.content[0], 'text'):
                        return {"response": msg.content[0].text.value, "thread_id": thread_id}
        
        return {"response": "Бот не ответил.", "thread_id": thread_id}

    except Exception as e:
        logger.error(f"Error: {e}")
        return {"response": "Ошибка сервера.", "thread_id": request.thread_id}
