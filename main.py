import os
import time
import logging
import requests
import re
from typing import Optional, Set
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")
tg_token = os.getenv("TELEGRAM_TOKEN")
tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

client = OpenAI(api_key=api_key)
app = FastAPI()

# --- ПАМЯТЬ БОТА (Кто уже оставил заявку) ---
# Храним ID диалогов тех, кто уже "сдал" номер.
# Чтобы их следующие сообщения тоже приходили юристу.
active_leads: Set[str] = set() 
# --------------------------------------------

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

# --- ФУНКЦИЯ 1: Скачивает всю историю (Для первой заявки) ---
def get_thread_history(thread_id):
    try:
        messages = client.beta.threads.messages.list(thread_id=thread_id, limit=50)
        history_text = ""
        for msg in reversed(list(messages.data)):
            role = "👤 Клиент" if msg.role == "user" else "🤖 Юрист"
            if hasattr(msg.content[0], 'text'):
                text = msg.content[0].text.value
                text = re.sub(r'\*\*|__', '', text) 
                history_text += f"<b>{role}:</b> {text}\n\n"
        return history_text
    except Exception as e:
        return f"Ошибка истории: {e}"

# --- ФУНКЦИЯ 2: Отправляет ГЛАВНУЮ заявку (Пакет) ---
def send_lead_package(history_text, thread_id):
    if not tg_token or not tg_chat_id: return 
    try:
        msg = (
            f"🔥 <b>НОВЫЙ ЛИД! (Контакт получен)</b>\n"
            f"➖➖➖➖➖➖➖\n"
            f"{history_text}"
            f"➖➖➖➖➖➖➖\n"
            f"🆔 <code>{thread_id}</code>"
        )
        if len(msg) > 4000: msg = msg[:4000] + "... (обрезано)"
        requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", json={
            "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
        })
    except Exception as e:
        logger.error(f"Telegram Error: {e}")

# --- ФУНКЦИЯ 3: Отправляет "догоняющие" сообщения ---
def send_follow_up(text, thread_id):
    if not tg_token or not tg_chat_id: return 
    try:
        msg = f"💬 <b>Клиент пишет (дополнение):</b>\n{text}\n\n<code>{thread_id}</code>"
        requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", json={
            "chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"
        })
    except Exception as e:
        logger.error(f"Telegram Error: {e}")

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Thai Law Bot is running"}

@app.post("/chat")
def chat(request: ChatRequest):
    global active_leads
    try:
        user_message = request.message
        thread_id = request.thread_id
        
        if thread_id == "": thread_id = None

        if not thread_id:
            thread = client.beta.threads.create()
            thread_id = thread.id
        
        # 1. Сначала проверяем, не оставил ли клиент контакт ПРЯМО СЕЙЧАС
        digit_count = sum(c.isdigit() for c in user_message)
        is_contact_message = (digit_count >= 6) or ('@' in user_message) or ('телеграм' in user_message.lower())

        # ЛОГИКА ОТПРАВКИ В ТЕЛЕГРАМ:
        
        # СЦЕНАРИЙ А: Это сообщение с контактом (Лид!)
        if is_contact_message:
            # Отправляем сообщение в OpenAI (чтобы оно сохранилось в историю)
            client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)
            
            # Ждем секунду, чтобы ИИ "осознал"
            # (Тут мы не ждем ответа ИИ, а сразу шлем заявку тебе)
            
            # Собираем историю И отправляем
            full_history = get_thread_history(thread_id)
            # Добавляем текущее сообщение, если get_thread_history его еще не видит (иногда бывает задержка)
            if user_message not in full_history:
                 full_history += f"<b>👤 Клиент:</b> {user_message}\n\n"
            
            send_lead_package(full_history, thread_id)
            
            # Запоминаем этого клиента как "Активного"
            active_leads.add(thread_id)

        # СЦЕНАРИЙ Б: Контакта нет, НО клиент уже в базе (пишет вдогонку)
        elif thread_id in active_leads:
             # Просто пересылаем это сообщение тебе
             send_follow_up(user_message, thread_id)
             client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)

        # СЦЕНАРИЙ В: Просто болтовня без контактов
        else:
             # Ничего тебе не шлем, просто общаемся с ботом
             client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)


        # --- ЗАПУСК БОТА (ОТВЕТ) ---
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)
        while run.status in ['queued', 'in_progress', 'cancelling']:
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            for msg in messages.data:
                if msg.role == "assistant":
                    if hasattr(msg.content[0], 'text'):
                        return {"response": msg.content[0].text.value, "thread_id": thread_id}
        
        return {"response": "...", "thread_id": thread_id}

    except Exception as e:
        logger.error(f"Error: {e}")
        return {"response": "Ошибка сервера.", "thread_id": request.thread_id}
