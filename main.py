import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI
# --- ГЛАВНОЕ ИЗМЕНЕНИЕ: Импорт модуля CORS ---
from fastapi.middleware.cors import CORSMiddleware 

# Получаем ключи из настроек Render
api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")

client = OpenAI(api_key=api_key)
app = FastAPI()

# --- ГЛАВНОЕ ИЗМЕНЕНИЕ: Разрешаем Тильде стучаться к нам ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # Разрешить всем сайтам (включая твою Тильду)
    allow_credentials=True,
    allow_methods=["*"],     # Разрешить любые методы (GET, POST и т.д.)
    allow_headers=["*"],     # Разрешить любые заголовки
)
# ------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    thread_id: str = None

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Thai Law Bot is running"}

@app.post("/chat")
def chat(request: ChatRequest):
    user_message = request.message
    thread_id = request.thread_id

    # 1. Если нет thread_id, создаем новый диалог
    if not thread_id:
        thread = client.beta.threads.create()
        thread_id = thread.id
    
    # 2. Добавляем сообщение пользователя в диалог
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_message
    )

    # 3. Запускаем Ассистента (чтобы он подумал)
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id
    )

    # 4. Ждем ответа (проверяем статус каждую секунду)
    while run.status in ['queued', 'in_progress', 'cancelling']:
        time.sleep(1)
        run = client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id
        )

    # 5. Если всё ок — забираем последний ответ
    if run.status == 'completed':
        messages = client.beta.threads.messages.list(
            thread_id=thread_id
        )
        # Ищем последнее сообщение от ассистента
        for msg in messages.data:
            if msg.role == "assistant":
                bot_response = msg.content[0].text.value
                return {"response": bot_response, "thread_id": thread_id}
    
    # Если что-то пошло не так
    return {"response": "Извините, я задумался. Попробуйте спросить еще раз.", "thread_id": thread_id}
