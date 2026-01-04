import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

# Инициализация
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
app = FastAPI()

# Разрешаем Тильде стучаться к нам
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

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    try:
        # 1. Работаем с диалогом (Thread)
        if not request.thread_id:
            thread = client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        # 2. Добавляем вопрос пользователя
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=request.message
        )

        # 3. Запускаем Ассистента и ждем ответ
        run = client.beta.threads.runs.create_and_poll(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )

        if run.status == 'completed': 
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            # OpenAI возвращает ответы в обратном порядке, берем первый (свежий)
            bot_answer = messages.data[0].content[0].text.value
            return {"response": bot_answer, "thread_id": thread_id}
        else:
            return {"response": "Ошибка обработки. Попробуйте позже.", "thread_id": thread_id}

    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def home():
    return {"status": "Legal Bot is active"}