import os
import re
import asyncio
import time
import requests
from typing import Optional, Dict, Set, Tuple
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI

# ==========================================
# 1. НАСТРОЙКИ
# ==========================================

api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID")
tg_token = os.environ.get("TELEGRAM_TOKEN")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Таймеры
ANALYSIS_DELAY_SECONDS = 40 
ATTEMPT_TIMEOUT = 110

# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
threads_last_activity: Dict[str, float] = {}
threads_monitoring_tasks: Dict[str, asyncio.Task] = {}
leads_registered: Set[str] = set()

# КОНТАКТЫ ФИРМЫ (ТРИГГЕРЫ)
# Если эти цифры/слова появятся в ответе БОТА — значит, клиент их попросил.
FIRM_PHONE_FRAGMENT = "96-004-9705" 
FIRM_EMAIL_FRAGMENT = "pravothai@lexprimethailand.com"

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

# ==========================================
# 2. ФУНКЦИИ
# ==========================================

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    return text.strip()

async def get_full_history(thread_id: str) -> Tuple[str, str, str]:
    """
    Скачивает историю и разделяет текст клиента и текст бота.
    Возвращает: (Вся_История, Текст_Клиента, Текст_Бота)
    """
    try:
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=50)
        history_list = list(reversed(messages.data))
        
        full_text = ""
        user_text_blob = "" 
        bot_text_blob = ""
        
        for msg in history_list:
            role_label = "Клиент" if msg.role == "user" else "Бот"
            
            if hasattr(msg.content[0], 'text'):
                content = clean_text(msg.content[0].text.value)
                full_text += f"{role_label}: {content}\n\n"
                
                if msg.role == "user":
                    user_text_blob += content + " "
                elif msg.role == "assistant":
                    bot_text_blob += content + " "
                    
        return full_text, user_text_blob, bot_text_blob
    except Exception:
        return "История недоступна.", "", ""

async def check_and_send_notification(thread_id: str, full_history: str, user_text: str, bot_text: str):
    """
    Логика проверки:
    1. Если Клиент написал СВОЙ номер -> НОВЫЙ ЛИД.
    2. Если Бот написал ВАШ номер -> ВОЗМОЖНЫЙ ЛИД (Интерес).
    """
    if not tg_token or not tg_chat_id: return

    # --- ПРОВЕРКА 1: ДАЛ ЛИ КЛИЕНТ СВОЙ НОМЕР? (Высший приоритет) ---
    clean_user_msg = re.sub(r'[\s\-]', '', user_text)
    has_user_phone = re.search(r'\d{7,}', clean_user_msg)
    has_user_email = "@" in user_text and len(user_text) < 500 # Грубая проверка на email/телеграм

    if has_user_phone or has_user_email:
        if thread_id not in leads_registered:
            header = "🔥 <b>НОВЫЙ ЛИД! (Оставил контакт)</b>"
            leads_registered.add(thread_id)
            await send_tg(header, full_history, thread_id)
        else:
            header = "📝 <b>ДОП. ИНФО (Лид)</b>"
            await send_tg(header, full_history, thread_id)
        return

    # --- ПРОВЕРКА 2: ВЫДАЛ ЛИ БОТ КОНТАКТЫ ФИРМЫ? ---
    # Проверяем, содержат ли ответы бота ваши триггеры
    bot_gave_contacts = (FIRM_PHONE_FRAGMENT in bot_text) or (FIRM_EMAIL_FRAGMENT in bot_text)

    if bot_gave_contacts:
        if thread_id not in leads_registered:
            header = "👀 <b>ВОЗМОЖНЫЙ ЛИД (Бот выдал контакты)</b>"
            # Мы регистрируем этот тред, чтобы не спамить каждый раз, когда бот повторяет номер
            leads_registered.add(thread_id)
            await send_tg(header, full_history, thread_id)

async def send_tg(header, history, thread_id):
    msg = (
        f"{header}\n"
        f"➖➖➖➖➖➖➖\n"
        f"{history[:3800]}" 
        f"➖➖➖➖➖➖➖\n"
        f"🆔 <code>{thread_id}</code>"
    )
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = {"chat_id": tg_chat_id, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"TG Error: {e}")

# ==========================================
# 3. ФОНОВЫЙ ПРОЦЕСС
# ==========================================

async def monitor_chat_activity(thread_id: str):
    try:
        while True:
            await asyncio.sleep(5)
            last_time = threads_last_activity.get(thread_id, 0)
            
            # Если тишина > 40 секунд
            if time.time() - last_time > ANALYSIS_DELAY_SECONDS:
                history, user_blob, bot_blob = await get_full_history(thread_id)
                if history:
                    await check_and_send_notification(thread_id, history, user_blob, bot_blob)
                break
                
    except asyncio.CancelledError:
        pass
    finally:
        threads_monitoring_tasks.pop(thread_id, None)

# ==========================================
# 4. ENDPOINT
# ==========================================

async def run_assistant(thread_id, assistant_id):
    # Добавляем в промпт явное указание давать контакты только если просят или не знают ответа
    run = await client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id,
        additional_instructions=(
            "Отвечай строго по базе знаний. "
            "Если ответа нет в базе или клиент просит связаться - выдавай эти контакты: "
            "+66 96-004-9705, pravothai@lexprimethailand.com"
        )
    )
    start = time.time()
    while time.time() - start < ATTEMPT_TIMEOUT:
        run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run_status.status == 'completed':
            msgs = await client.beta.threads.messages.list(thread_id=thread_id)
            if msgs.data:
                return msgs.data[0].content[0].text.value
            return ""
        elif run_status.status in ['failed', 'expired']:
            return "Ошибка обработки."
        await asyncio.sleep(1)
    return "Связь нестабильна."

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    if not api_key or not assistant_id:
        return {"response": "Config Error", "thread_id": request.thread_id}

    threads_last_activity[request.thread_id or "new"] = time.time()

    if not request.thread_id:
        thread = await client.beta.threads.create()
        thread_id = thread.id
        threads_last_activity[thread_id] = time.time()
    else:
        thread_id = request.thread_id
        threads_last_activity[thread_id] = time.time()

    if thread_id not in threads_monitoring_tasks:
        task = asyncio.create_task(monitor_chat_activity(thread_id))
        threads_monitoring_tasks[thread_id] = task

    await client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=request.message
    )
    
    response_text = await run_assistant(thread_id, assistant_id)
    
    return {
        "response": clean_text(response_text),
        "thread_id": thread_id
    }

@app.get("/")
def home():
    return {"status": "ThaiBot v24.0 (Bot Output Trigger)"}
