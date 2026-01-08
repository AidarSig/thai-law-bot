import os
import re
import asyncio
import requests
from typing import Optional, Set
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI

# --- 1. НАСТРОЙКИ ---
api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID")
tg_token = os.environ.get("TELEGRAM_TOKEN")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Тайм-аут 110 сек для стабильности
ATTEMPT_TIMEOUT = 110 

# БАЗА ДАННЫХ В ПАМЯТИ (Хранит ID тех, кто уже оставил контакт)
# При перезагрузке сервера Render она очищается, но это не критично для новых лидов.
leads_db: Set[str] = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None

# --- 2. ФУНКЦИИ ТЕЛЕГРАМА И ИСТОРИИ ---

def clean_text(text):
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    return text.strip()

async def get_formatted_history(thread_id):
    """
    Скачивает историю диалога из OpenAI и форматирует её для Telegram.
    """
    try:
        # Получаем список сообщений (OpenAI отдает их от новых к старым)
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=20)
        
        # Разворачиваем, чтобы было хронологически (от старых к новым)
        history_list = list(reversed(messages.data))
        
        formatted_text = ""
        for msg in history_list:
            role = msg.role
            content = clean_text(msg.content[0].text.value)
            
            if role == "user":
                formatted_text += f"👤 Клиент: {content}\n\n"
            elif role == "assistant":
                formatted_text += f"🤖 Юрист: {content}\n\n"
                
        return formatted_text
    except Exception as e:
        print(f"History Error: {e}")
        return "(Не удалось загрузить историю переписки)"

async def handle_telegram_notification(text, thread_id):
    """
    Умная логика отправки уведомлений
    """
    if not tg_token or not tg_chat_id:
        return

    # 1. Проверяем, есть ли контактные данные в текущем сообщении
    # Ищем 7+ цифр подряд ИЛИ символ @ (для телеграм ников)
    clean_msg = re.sub(r'[\s\-]', '', text)
    has_contact = re.search(r'\d{7,}', clean_msg) or ("@" in text and len(text) < 50)

    # 2. СЦЕНАРИЙ А: ПЕРВЫЙ КОНТАКТ (Новый лид)
    if has_contact and thread_id not in leads_db:
        leads_db.add(thread_id) # Запоминаем клиента
        
        # Формируем полную историю
        full_history = await get_formatted_history(thread_id)
        
        msg_body = (
            f"🔥 <b>НОВЫЙ ЛИД! (Контакт получен)</b>\n"
            f"➖➖➖➖➖➖➖\n"
            f"{full_history}"
            f"➖➖➖➖➖➖➖\n"
            f"🆔 <code>{thread_id}</code>"
        )
        await send_to_tg(msg_body)

    # 3. СЦЕНАРИЙ Б: ДОПОЛНЕНИЕ (Клиент уже известен, пишет что-то еще)
    elif thread_id in leads_db:
        # Если клиент пишет дальше, мы отправляем это как дополнение, 
        # чтобы вы не потеряли контекст.
        msg_body = (
            f"📝 <b>ДОП. СООБЩЕНИЕ ОТ ЛИДА</b>\n"
            f"➖➖➖➖➖➖➖\n"
            f"👤 Клиент: {text}\n"
            f"➖➖➖➖➖➖➖\n"
            f"🔗 К треду: <code>{thread_id}</code>"
        )
        await send_to_tg(msg_body)

async def send_to_tg(text):
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = {
        "chat_id": tg_chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    # Отправляем в отдельном потоке, чтобы не тормозить ответ пользователю
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: requests.post(url, json=payload))
    except Exception as e:
        print(f"TG Send Error: {e}")

# --- 3. РАБОТА С ASSISTANT ---

async def run_assistant_with_timeout(thread_id, assistant_id, timeout):
    try:
        run = await client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        start_time = asyncio.get_event_loop().time()
        
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                try:
                    await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                except: pass
                return False 

            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            if run_status.status == 'completed':
                return True
            elif run_status.status in ['failed', 'cancelled', 'expired']:
                return False
            
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Run Error: {e}")
        return False

# --- 4. MAIN ENDPOINT ---

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    print(f"\n📩 Message: {request.message[:50]}...")

    if not api_key or not assistant_id:
        return {"response": "Ошибка ключей.", "thread_id": request.thread_id}

    if not request.message.strip():
        return {"response": "...", "thread_id": request.thread_id}

    try:
        # А. Работа с тредом
        if not request.thread_id:
            thread = await client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        # Б. Отправка сообщения в OpenAI
        await client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=request.message
        )

        # В. Генерация ответа (ждем до 110 сек)
        success = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
        
        # Г. Получение ответа
        final_answer = ""
        if success:
            messages = await client.beta.threads.messages.list(thread_id=thread_id)
            raw_answer = messages.data[0].content[0].text.value
            final_answer = clean_text(raw_answer)
        else:
            # Если не успели, даем нейтральный ответ
            final_answer = "Связь с базой данных устанавливается. Пожалуйста, подождите минуту - я анализирую ваш запрос."

        # Д. ТЕЛЕГРАМ ЛОГИКА (Запускаем ПОСЛЕ того как получили ответ от ИИ)
        # Мы делаем это в фоне, чтобы пользователь уже получил ответ на сайте
        asyncio.create_task(handle_telegram_notification(request.message, thread_id))

        return {"response": final_answer, "thread_id": thread_id}

    except Exception as e:
        print(f"Global Error: {e}")
        return {"response": "Секунду...", "thread_id": request.thread_id}

@app.get("/")
def home():
    return {"status": "ThaiBot CRM v11 Active"}
