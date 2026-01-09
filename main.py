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
# 2. БЕЗОПАСНАЯ ИСТОРИЯ (FIX)
# ==========================================

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    # ВАЖНО: Экранируем скобки, чтобы не ломать HTML разметку Телеграма
    text = text.replace("<", "&lt;").replace(">", "&gt;") 
    return text.strip()

async def get_safe_history(thread_id: str) -> Tuple[str, str, str]:
    """
    Собирает историю аккуратно, чтобы не ломать HTML-теги при обрезке.
    """
    try:
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=40)
        raw_msgs = list(reversed(messages.data))
        
        user_blob = "" 
        bot_blob = ""
        temp_buffer = []

        for msg in raw_msgs:
            if hasattr(msg.content[0], 'text'):
                content = clean_text(msg.content[0].text.value)
                
                chunk = ""
                if msg.role == "user":
                    chunk = f"👤 <b>Клиент:</b>\n{content}\n\n"
                    user_blob += content + " "
                elif msg.role == "assistant":
                    chunk = f"🤖 <b>Бот:</b>\n{content}\n\n"
                    bot_blob += content + " "
                
                temp_buffer.append(chunk)

        # Собираем итоговый текст с конца (самые новые), следя за лимитом
        final_history_str = ""
        for chunk in reversed(temp_buffer):
            if len(final_history_str) + len(chunk) < 3800:
                final_history_str = chunk + final_history_str
            else:
                break # Лимит исчерпан
                    
        return final_history_str, user_blob, bot_blob
    except Exception as e:
        print(f"History Error: {e}")
        return "⚠️ История недоступна.", "", ""

# ==========================================
# 3. ЛОГИКА УВЕДОМЛЕНИЙ (FIX)
# ==========================================

async def send_tg_safe(text: str):
    """
    Отправляет сообщение безопасно. Если HTML сломан — шлет чистый текст.
    """
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    
    # Попытка 1: HTML
    payload = {"chat_id": tg_chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload)
        if resp.status_code == 200:
            return
    except Exception:
        pass

    # Попытка 2: Текст без форматирования (страховка)
    clean_msg = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
    try:
        requests.post(url, json={"chat_id": tg_chat_id, "text": clean_msg})
    except Exception as e:
        print(f"TG Critical Error: {e}")

async def check_and_send_notification(thread_id: str, formatted_history: str, user_text: str, bot_text: str):
    if not tg_token or not tg_chat_id: return

    clean_user_msg = re.sub(r'[\s\-]', '', user_text)
    
    # 1. ЕСТЬ ЛИ КОНТАКТ ОТ КЛИЕНТА?
    has_user_phone = re.search(r'\d{7,}', clean_user_msg)
    has_user_email = "@" in user_text and len(user_text) < 500
    user_gave_contact = bool(has_user_phone or has_user_email)

    # 2. ДАЛ ЛИ БОТ КОНТАКТЫ ФИРМЫ?
    bot_gave_contact = (FIRM_PHONE_FRAGMENT in bot_text) or (FIRM_EMAIL_FRAGMENT in bot_text)

    current_status = leads_status.get(thread_id)
    header = ""

    # Приоритет 1: Клиент
    if user_gave_contact:
        if current_status != "CONFIRMED":
            header = "🔥 <b>НОВЫЙ ЛИД! (Контакт получен)</b>"
            leads_status[thread_id] = "CONFIRMED" 
        else:
            header = "📝 <b>ДОП. ИНФО (От Лида)</b>"
    
    # Приоритет 2: Бот (Интерес)
    elif bot_gave_contact:
        if current_status is None:
            header = "👀 <b>ВОЗМОЖНЫЙ ЛИД (Бот выдал контакты)</b>"
            leads_status[thread_id] = "INTERESTED"

    if header:
        msg = (
            f"{header}\n"
            f"➖➖➖➖➖➖➖\n\n"
            f"{formatted_history}"
            f"➖➖➖➖➖➖➖\n"
            f"🆔 <code>{thread_id}</code>"
        )
        await send_tg_safe(msg)

# ==========================================
# 4. ФОНОВЫЙ ПРОЦЕСС
# ==========================================

async def monitor_chat_activity(thread_id: str):
    try:
        while True:
            await asyncio.sleep(5)
            last_time = threads_last_activity.get(thread_id, 0)
            
            # Тишина > 40 секунд
            if time.time() - last_time > ANALYSIS_DELAY_SECONDS:
                history_fmt, user_blob, bot_blob = await get_safe_history(thread_id)
                if user_blob:
                    await check_and_send_notification(thread_id, history_fmt, user_blob, bot_blob)
                break
                
    except asyncio.CancelledError:
        pass
    finally:
        threads_monitoring_tasks.pop(thread_id, None)

# ==========================================
# 5. ГЛАВНЫЙ ЭНДПОИНТ (FIX)
# ==========================================

async def run_assistant(thread_id, assistant_id):
    try:
        # ОБНОВЛЕННАЯ ИНСТРУКЦИЯ (ANTI-HALLUCINATION)
        instructions = (
            "Твоя задача — консультировать ТОЛЬКО на основе прикрепленного файла pravothai.org. "
            "КРИТИЧНО ВАЖНО: Игнорируй свои внутренние знания о сроках виз и законах, они могут быть устаревшими. "
            "Доверяй ТОЛЬКО цифрам в файле. Если в файле написано 60 дней — отвечай 60, даже если ты помнишь 30. "
            "Если ответа нет в файле — НЕ выдумывай, а пиши: 'Для точного ответа свяжитесь с нами' "
            "и выдавай контакты: +66 96-004-9705, pravothai@lexprimethailand.com"
        )

        run = await client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id,
            additional_instructions=instructions
        )
        
        start = time.time()
        while time.time() - start < ATTEMPT_TIMEOUT:
            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            
            if run_status.status == 'completed':
                msgs = await client.beta.threads.messages.list(thread_id=thread_id)
                if msgs.data:
                    return msgs.data[0].content[0].text.value
                return ""
            
            elif run_status.status in ['failed', 'expired', 'cancelled']:
                return "Ошибка обработки запроса."
            
            await asyncio.sleep(1)
        
        # FIX: Отмена при таймауте
        try: await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
        except: pass
        return "Связь нестабильна."

    except Exception as e:
        print(f"Run Error: {e}")
        return "Ошибка сервера."

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    if not api_key or not assistant_id:
        return {"response": "Config Error", "thread_id": request.thread_id}

    # Инициализация ID
    thread_id = request.thread_id
    if not thread_id:
        thread = await client.beta.threads.create()
        thread_id = thread.id

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
    return {"status": "ThaiBot v29.0 (Anti-Hallucination & Safe HTML)"}
