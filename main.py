import os
import re
import asyncio
import time
import requests
from typing import Optional, Dict, Tuple
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

# Таймер тишины перед отправкой (40 сек)
ANALYSIS_DELAY_SECONDS = 40 
ATTEMPT_TIMEOUT = 110

# СТАТУСЫ: None -> "INTERESTED" -> "CONFIRMED"
leads_status: Dict[str, str] = {}
threads_last_activity: Dict[str, float] = {}
threads_monitoring_tasks: Dict[str, asyncio.Task] = {}

# ТРИГГЕРЫ БОТА (Если бот сам выдал эти данные)
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
# 2. ФОРМАТИРОВАНИЕ (СО СМАЙЛИКАМИ)
# ==========================================

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    return text.strip()

async def get_formatted_history(thread_id: str) -> Tuple[str, str, str]:
    """
    Формирует красивую историю с иконками 👤 и 🤖.
    """
    try:
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=50)
        history_list = list(reversed(messages.data))
        
        formatted_text = ""
        user_blob = "" 
        bot_blob = ""
        
        for msg in history_list:
            if hasattr(msg.content[0], 'text'):
                content = clean_text(msg.content[0].text.value)
                
                if msg.role == "user":
                    # Смайлик + Жирный заголовок + Отступ
                    formatted_text += f"👤 <b>Клиент:</b>\n{content}\n\n"
                    user_blob += content + " "
                elif msg.role == "assistant":
                    # Смайлик + Жирный заголовок + Отступ
                    formatted_text += f"🤖 <b>Бот:</b>\n{content}\n\n"
                    bot_blob += content + " "
                    
        return formatted_text, user_blob, bot_blob
    except Exception:
        return "⚠️ История недоступна.", "", ""

# ==========================================
# 3. ГЛАВНАЯ ЛОГИКА СТАТУСОВ
# ==========================================

async def check_and_send_notification(thread_id: str, formatted_history: str, user_text: str, bot_text: str):
    if not tg_token or not tg_chat_id: return

    # Очистка текста клиента для поиска номера
    clean_user_msg = re.sub(r'[\s\-]', '', user_text)
    
    # 1. ЕСТЬ ЛИ КОНТАКТ ОТ КЛИЕНТА? (Regex)
    has_user_phone = re.search(r'\d{7,}', clean_user_msg)
    has_user_email = "@" in user_text and len(user_text) < 500
    user_gave_contact = bool(has_user_phone or has_user_email)

    # 2. ДАЛ ЛИ БОТ КОНТАКТЫ ФИРМЫ?
    bot_gave_contact = (FIRM_PHONE_FRAGMENT in bot_text) or (FIRM_EMAIL_FRAGMENT in bot_text)

    # Текущий статус треда
    current_status = leads_status.get(thread_id)
    
    header = ""

    # --- ПРИОРИТЕТ 1: Клиент дал свои данные (CONFIRMED) ---
    if user_gave_contact:
        # Логика: Если статус еще не "Подтвержден" — это НОВЫЙ ЛИД.
        # (Даже если до этого он был "Interested", мы повышаем его до "Confirmed")
        if current_status != "CONFIRMED":
            header = "🔥 <b>НОВЫЙ ЛИД! (Контакт получен)</b>"
            leads_status[thread_id] = "CONFIRMED" 
        else:
            # Если он уже "Подтвержден", то просто доп. инфо
            header = "📝 <b>ДОП. ИНФО (От Лида)</b>"
    
    # --- ПРИОРИТЕТ 2: Бот дал контакты (INTERESTED) ---
    elif bot_gave_contact:
        # Уведомляем только если статус еще "Никакой" (None).
        # Если статус уже "Interested" или "Confirmed", мы НЕ шлем повторно.
        if current_status is None:
            header = "👀 <b>ВОЗМОЖНЫЙ ЛИД (Бот выдал контакты)</b>"
            leads_status[thread_id] = "INTERESTED"

    # --- ОТПРАВКА ---
    if header:
        msg = (
            f"{header}\n"
            f"➖➖➖➖➖➖➖\n\n"
            f"{formatted_history[:3800]}"
            f"➖➖➖➖➖➖➖\n"
            f"🆔 <code>{thread_id}</code>"
        )
        await send_tg(msg)

async def send_tg(text):
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = {"chat_id": tg_chat_id, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"TG Error: {e}")

# ==========================================
# 4. ФОНОВЫЙ ПРОЦЕСС (НАБЛЮДАТЕЛЬ)
# ==========================================

async def monitor_chat_activity(thread_id: str):
    try:
        while True:
            await asyncio.sleep(5)
            last_time = threads_last_activity.get(thread_id, 0)
            
            # Если тишина > 40 секунд, запускаем проверку
            if time.time() - last_time > ANALYSIS_DELAY_SECONDS:
                history_fmt, user_blob, bot_blob = await get_formatted_history(thread_id)
                if user_blob:
                    await check_and_send_notification(thread_id, history_fmt, user_blob, bot_blob)
                break
                
    except asyncio.CancelledError:
        pass
    finally:
        threads_monitoring_tasks.pop(thread_id, None)

# ==========================================
# 5. ГЛАВНЫЙ ЭНДПОИНТ
# ==========================================

async def run_assistant(thread_id, assistant_id):
    # Промпт: Строго по базе + призыв к контакту если что-то неясно
    run = await client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id,
        additional_instructions=(
            "Отвечай строго по базе знаний pravothai.org. "
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
            return "Ошибка обработки запроса."
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
    return {"status": "ThaiBot v27.0 (Icons & Logic Verified)"}
