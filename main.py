import os
import re
import asyncio
import requests
from typing import Optional, Set, Tuple
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

ATTEMPT_TIMEOUT = 110 

# База данных отправленных тредов (чтобы не дублировать "Новый лид")
leads_db: Set[str] = set()

# Ключевые слова для определения интереса
CONTACT_KEYWORDS = [
    "контакт", "телефон", "номер", "позвонить", "связ", "адрес", "почта", "email",
    "contact", "phone", "number", "call", "address", "whatsapp", "telegram"
]

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

# --- 2. ЛОГИКА АНАЛИЗА И ТЕЛЕГРАМА ---

def clean_text(text):
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    return text.strip()

async def get_history_data(thread_id) -> Tuple[str, int]:
    """
    Возвращает:
    1. Отформатированный текст истории.
    2. Количество сообщений ОТ ПОЛЬЗОВАТЕЛЯ (для фильтра).
    """
    try:
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=30)
        history_list = list(reversed(messages.data))
        
        formatted_text = ""
        user_msg_count = 0

        for msg in history_list:
            role = msg.role
            content = clean_text(msg.content[0].text.value)
            
            if role == "user":
                user_msg_count += 1
                formatted_text += f"👤 Клиент: {content}\n\n"
            elif role == "assistant":
                formatted_text += f"🤖 Юрист: {content}\n\n"
                
        return formatted_text, user_msg_count
    except Exception as e:
        print(f"History Error: {e}")
        return "(Ошибка загрузки истории)", 0

async def handle_telegram_notification(text, thread_id):
    if not tg_token or not tg_chat_id:
        return

    # А. ПРОВЕРКА НА ЯВНЫЙ КОНТАКТ (НОМЕР ТЕЛЕФОНА) -> ЭТО ЛИД
    clean_msg = re.sub(r'[\s\-]', '', text)
    has_phone = re.search(r'\d{7,}', clean_msg) or ("@" in text and len(text) < 50)

    if has_phone:
        # Если это первый раз, когда он дал номер
        if thread_id not in leads_db:
            leads_db.add(thread_id)
            history_text, _ = await get_history_data(thread_id)
            
            msg = (
                f"🔥 <b>НОВЫЙ ЛИД! (Контакт получен)</b>\n"
                f"➖➖➖➖➖➖➖\n"
                f"{history_text}"
                f"➖➖➖➖➖➖➖\n"
                f"🆔 <code>{thread_id}</code>"
            )
            await send_to_tg(msg)
        else:
            # Если уже был лидом, но пишет еще что-то
            msg = (
                f"📝 <b>ДОП. ИНФО ОТ ЛИДА</b>\n"
                f"➖➖➖➖➖➖➖\n"
                f"👤 Клиент: {text}\n"
                f"➖➖➖➖➖➖➖\n"
                f"🔗 <code>{thread_id}</code>"
            )
            await send_to_tg(msg)
        return # Выходим, так как приоритет отработан

    # Б. ПРОВЕРКА НА ЗАПРОС КОНТАКТОВ (ИНТЕРЕС)
    # Сработает только если клиент НЕ давал свой номер, но просит ваш
    
    # 1. Есть ли ключевое слово?
    is_asking_contacts = any(word in text.lower() for word in CONTACT_KEYWORDS)
    
    if is_asking_contacts and thread_id not in leads_db:
        # 2. Получаем историю и считаем сообщения
        history_text, user_count = await get_history_data(thread_id)
        
        # 3. ФИЛЬТР: Только если диалог содержательный (более 2 сообщений от юзера)
        if user_count > 2:
            leads_db.add(thread_id) # Помечаем, чтобы не спамить каждым сообщением
            
            msg = (
                f"👀 <b>ЗАПРОС КОНТАКТОВ (Интерес)</b>\n"
                f"<i>Клиент активно интересуется связью, но свой номер пока не дал.</i>\n"
                f"➖➖➖➖➖➖➖\n"
                f"{history_text}"
                f"➖➖➖➖➖➖➖\n"
                f"🆔 <code>{thread_id}</code>"
            )
            await send_to_tg(msg)

async def send_to_tg(text):
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = { "chat_id": tg_chat_id, "text": text, "parse_mode": "HTML" }
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: requests.post(url, json=payload))
    except Exception as e:
        print(f"TG Error: {e}")

# --- 3. ASSISTANT LOGIC ---

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
                try: await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                except: pass
                return False 
            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run_status.status == 'completed': return True
            elif run_status.status in ['failed', 'cancelled', 'expired']: return False
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Run Error: {e}")
        return False

# --- 4. ENDPOINT ---

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    # Фоновая проверка на триггеры Телеграма (ДО ответа ИИ, чтобы быстрее реагировать)
    # Но для "Запроса контактов" нам нужна история, поэтому лучше запустим параллельно
    
    if not api_key or not assistant_id:
        return {"response": "Config Error", "thread_id": request.thread_id}

    if not request.message.strip():
        return {"response": "...", "thread_id": request.thread_id}

    try:
        if not request.thread_id:
            thread = await client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        await client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=request.message
        )

        # ЗАПУСК ТЕЛЕГРАМ-АНАЛИЗАТОРА
        # Мы запускаем его "в фоне", но передаем thread_id
        asyncio.create_task(handle_telegram_notification(request.message, thread_id))

        success = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
        
        final_answer = ""
        if success:
            messages = await client.beta.threads.messages.list(thread_id=thread_id)
            raw_answer = messages.data[0].content[0].text.value
            final_answer = clean_text(raw_answer)
        else:
            final_answer = "Связь с базой данных устанавливается. Пожалуйста, подождите..."

        return {"response": final_answer, "thread_id": thread_id}

    except Exception as e:
        print(f"Error: {e}")
        return {"response": "Секунду...", "thread_id": request.thread_id}

@app.get("/")
def home():
    return {"status": "ThaiBot v12 (Smart Leads)"}
