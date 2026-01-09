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
# 1. НАСТРОЙКИ И ПЕРЕМЕННЫЕ
# ==========================================

api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID")
tg_token = os.environ.get("TELEGRAM_TOKEN")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Таймер тишины: ждем 40 сек после последнего сообщения, прежде чем слать отчет в ТГ
ANALYSIS_DELAY_SECONDS = 40 
# Таймаут ожидания ответа от AI (110 сек)
ATTEMPT_TIMEOUT = 110

# Хранилище времени последней активности для каждого диалога
threads_last_activity: Dict[str, float] = {}
# Хранилище активных задач мониторинга
threads_monitoring_tasks: Dict[str, asyncio.Task] = {}

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
# 2. ФУНКЦИИ ОЧИСТКИ И ИСТОРИИ
# ==========================================

def clean_text(text: str) -> str:
    """Очищает текст от Markdown и экранирует символы для HTML Телеграма."""
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    # Экранируем теги, чтобы не ломать HTML разметку
    text = text.replace("<", "&lt;").replace(">", "&gt;") 
    return text.strip()

async def get_safe_history(thread_id: str) -> Tuple[str, str, str]:
    """
    Собирает историю диалога для отправки в Telegram.
    Возвращает: (отформатированный текст для ТГ, сырой текст юзера, сырой текст бота)
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

        # Собираем итоговый текст с конца (самые новые сообщения), следя за лимитом длины ТГ
        final_history_str = ""
        for chunk in reversed(temp_buffer):
            if len(final_history_str) + len(chunk) < 3800:
                final_history_str = chunk + final_history_str
            else:
                break 
                    
        return final_history_str, user_blob, bot_blob
    except Exception as e:
        print(f"History Error: {e}")
        return "⚠️ История недоступна.", "", ""

# ==========================================
# 3. ЛОГИКА УВЕДОМЛЕНИЙ В TELEGRAM
# ==========================================

async def send_tg_safe(text: str):
    """
    Отправляет сообщение безопасно. Если HTML сломан — шлет чистый текст.
    """
    if not tg_token or not tg_chat_id: return

    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    
    # Попытка 1: Отправка с форматированием HTML
    payload = {"chat_id": tg_chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload)
        if resp.status_code == 200:
            return
    except Exception:
        pass

    # Попытка 2: Текст без форматирования (если HTML вызвал ошибку)
    clean_msg = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
    try:
        requests.post(url, json={"chat_id": tg_chat_id, "text": clean_msg})
    except Exception as e:
        print(f"TG Critical Error: {e}")

async def check_and_send_notification(thread_id: str, formatted_history: str, user_text: str, bot_text: str):
    """
    Формирует уведомление и отправляет его.
    """
    # Заголовок по умолчанию
    header = "💬 <b>НОВЫЙ ДИАЛОГ / АКТИВНОСТЬ</b>"
    
    # Простая проверка: если клиент сам оставил контакты в тексте
    clean_user_msg = re.sub(r'[\s\-]', '', user_text)
    # Ищем 7+ цифр подряд (телефон) или символ @ (почта/телега)
    if re.search(r'\d{7,}', clean_user_msg) or ("@" in user_text and len(user_text) < 500):
        header += " (Клиент оставил контакт 📞)"

    msg = (
        f"{header}\n"
        f"➖➖➖➖➖➖➖\n\n"
        f"{formatted_history}"
        f"➖➖➖➖➖➖➖\n"
        f"🆔 <code>{thread_id}</code>"
    )
    await send_tg_safe(msg)

# ==========================================
# 4. ФОНОВЫЙ ПРОЦЕСС МОНИТОРИНГА
# ==========================================

async def monitor_chat_activity(thread_id: str):
    """
    Следит за активностью в чате. Если тишина > 40 сек, отправляет историю в ТГ.
    """
    try:
        while True:
            await asyncio.sleep(5)
            last_time = threads_last_activity.get(thread_id, 0)
            
            # Если прошло достаточно времени с последнего сообщения
            if time.time() - last_time > ANALYSIS_DELAY_SECONDS:
                history_fmt, user_blob, bot_blob = await get_safe_history(thread_id)
                if user_blob: # Отправляем только если были сообщения от юзера
                    await check_and_send_notification(thread_id, history_fmt, user_blob, bot_blob)
                break
                
    except asyncio.CancelledError:
        pass
    finally:
        # Удаляем задачу из памяти
        threads_monitoring_tasks.pop(thread_id, None)

# ==========================================
# 5. ГЛАВНАЯ ЛОГИКА АССИСТЕНТА (AI)
# ==========================================

async def run_assistant(thread_id, assistant_id):
    try:
        # ОБНОВЛЕННАЯ СИСТЕМНАЯ ИНСТРУКЦИЯ
        # 1. Четкая роль (Центр правовой помощи)
        # 2. Запрет на LexPrime в названии
        # 3. Приоритет выдачи контактов, а не их сбора
        instructions = (
            "Твоя роль: Ты — ИИ-ассистент «Центра правовой помощи соотечественникам в Таиланде». "
            "СТРОГОЕ ПРАВИЛО: Никогда не называй себя LexPrime. Ты представляешь именно Центр правовой помощи. "
            "Твоя цель: Консультировать строго на основе прикрепленного файла базы знаний. "
            "ПРАВИЛО КОНТАКТОВ: Никогда не проси у клиента его номер телефона или email первым. "
            "Вместо этого, если вопрос требует детального разбора или услуги, скажи: 'Для решения этого вопроса, пожалуйста, свяжитесь с нами' "
            "и обязательно предоставь наши контакты: "
            "📞 Телефон: +66 96-004-9705, "
            "✈️ Telegram: @pravo_thai, "
            "📧 Email: pravothai@lexprimethailand.com. "
            "ГЕОГРАФИЯ: Только Таиланд. "
            "Если ответа нет в файле — НЕ выдумывай, а сразу давай контакты."
        )

        # Создаем и запускаем "Run"
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
                return "Извините, произошла ошибка обработки запроса. Пожалуйста, попробуйте еще раз или свяжитесь с нами по телефону +66 96-004-9705."
            
            await asyncio.sleep(1)
        
        # Если таймаут
        try: await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
        except: pass
        return "Связь нестабильна. Пожалуйста, напишите нам в Telegram @pravo_thai."

    except Exception as e:
        print(f"Run Error: {e}")
        return "Внутренняя ошибка сервера."

# ==========================================
# 6. API ENDPOINTS
# ==========================================

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    if not api_key or not assistant_id:
        return {"response": "Config Error: API Key missing", "thread_id": request.thread_id}

    # Инициализация ID диалога
    thread_id = request.thread_id
    if not thread_id:
        thread = await client.beta.threads.create()
        thread_id = thread.id

    # Обновляем время активности
    threads_last_activity[thread_id] = time.time()

    # Запускаем фоновый мониторинг, если его нет
    if thread_id not in threads_monitoring_tasks:
        task = asyncio.create_task(monitor_chat_activity(thread_id))
        threads_monitoring_tasks[thread_id] = task

    # Отправляем сообщение юзера в OpenAI
    await client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=request.message
    )
    
    # Получаем ответ
    response_text = await run_assistant(thread_id, assistant_id)
    
    return {
        "response": clean_text(response_text),
        "thread_id": thread_id
    }

@app.get("/")
def home():
    return {"status": "ThaiLawBot Active", "mode": "Center for Legal Aid"}
