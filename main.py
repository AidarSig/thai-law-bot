import os
import re
import asyncio
from typing import Optional  # <--- ВАЖНО: Добавили для исправления ошибки 422
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APIError

# --- 1. НАСТРОЙКИ ---
api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID")

if not api_key or not assistant_id:
    print("CRITICAL ERROR: Keys missing in Environment!")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

ATTEMPT_TIMEOUT = 60 

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ИСПРАВЛЕНИЕ ОШИБКИ 422 ЗДЕСЬ ---
class UserRequest(BaseModel):
    message: str
    # Мы разрешаем thread_id быть None (null), чтобы Pydantic не ругался
    thread_id: Optional[str] = None 

# --- 2. ФУНКЦИИ ---

def clean_text(text):
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    text = re.sub(r' +', ' ', text)
    return text.strip()

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
                print(f"⏳ Timeout ({elapsed}s)")
                try:
                    await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                except: pass
                return False 

            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            if run_status.status == 'completed':
                return True
            elif run_status.status in ['failed', 'cancelled', 'expired']:
                print(f"❌ Status: {run_status.status}")
                return False
            
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Run Error: {e}")
        return False

# --- 3. ЭНДПОИНТ ---

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    print(f"\n📩 Message: {request.message[:50]}...")

    if not api_key or not assistant_id:
        return {"response": "Ошибка конфигурации сервера.", "thread_id": request.thread_id}

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

        success = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
        
        if success:
            messages = await client.beta.threads.messages.list(thread_id=thread_id)
            raw_answer = messages.data[0].content[0].text.value
            final_answer = clean_text(raw_answer)
            return {"response": final_answer, "thread_id": thread_id}
        else:
            return {
                "response": "Сервер просыпается. Пожалуйста, повторите вопрос.",
                "thread_id": thread_id
            }

    except Exception as e:
        print(f"💥 Error: {e}")
        return {"response": "Техническая заминка. Повторите вопрос.", "thread_id": request.thread_id}

@app.get("/")
def home():
    return {"status": "FastAPI ThaiBot Active"}
