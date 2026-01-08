import os
import re
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APIError

# --- 1. НАСТРОЙКИ ---
# Получаем ключи. Если их нет - код не упадет сразу, но выдаст ошибку в лог.
api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("OPENAI_ASSISTANT_ID") # Обратите внимание: имя переменной может отличаться в Render

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Тайм-аут ставим больше, чтобы не рубить connection раньше времени
ATTEMPT_TIMEOUT = 50 
MAX_RETRIES = 1 # Снижаем кол-во попыток для скорости

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserRequest(BaseModel):
    message: str
    thread_id: str = None

# --- 2. ФУНКЦИИ ПОМОЩНИКИ ---

def clean_text(text):
    if not text: return ""
    # Удаляем аннотации типа 【4:0†source】
    text = re.sub(r'【.*?】', '', text)
    # Удаляем Markdown заголовки
    text = text.replace("###", "").replace("**", "")
    # Чистим пробелы
    text = re.sub(r' +', ' ', text)
    return text.strip()

# --- ВРЕМЕННО ОТКЛЮЧИЛ ВАЛИДАТОР ДЛЯ СКОРОСТИ ---
# На бесплатном тарифе Render двойной запрос к OpenAI вызывает Timeout
# async def validate_answer_quality(answer_text): ...

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
                print(f"⏳ Time is up! ({elapsed}s)")
                # Пытаемся отменить, но не блокируем, если не вышло
                try:
                    await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                except: pass
                return False # Возвращаем False вместо ошибки, чтобы обработать мягко

            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            if run_status.status == 'completed':
                return True
            elif run_status.status in ['failed', 'cancelled', 'expired']:
                print(f"❌ Run failed: {run_status.status}")
                return False
            
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Run Error: {e}")
        return False

# --- 3. ГЛАВНЫЙ ЭНДПОИНТ ---

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    print(f"\n📩 NEW: {request.message[:50]}... [Thread: {request.thread_id}]")

    if not api_key or not assistant_id:
        return {"response": "Ошибка сервера: не настроены ключи API.", "thread_id": request.thread_id}

    if not request.message.strip():
        return {"response": "...", "thread_id": request.thread_id}

    try:
        # 1. Thread
        if not request.thread_id:
            thread = await client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        # 2. Message
        await client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=request.message
        )

        # 3. Run
        success = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
        
        if success:
            messages = await client.beta.threads.messages.list(thread_id=thread_id)
            # Берем последнее сообщение
            raw_answer = messages.data[0].content[0].text.value
            final_answer = clean_text(raw_answer)
            print(f"🤖 BOT: {final_answer[:50]}...")
            return {"response": final_answer, "thread_id": thread_id}
        else:
            # Если не успели или ошибка
            return {
                "response": "Извините, сервер перегружен. Пожалуйста, повторите вопрос через 10 секунд.",
                "thread_id": thread_id
            }

    except Exception as e:
        print(f"💥 GLOBAL ERROR: {e}")
        return {"response": "Техническая заминка. Повторите вопрос.", "thread_id": request.thread_id}

@app.get("/")
def home():
    return {"status": "FastAPI ThaiBot Running"}
