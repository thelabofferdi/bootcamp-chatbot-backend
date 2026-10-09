import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, get_db
from database.models import Conversation, Message

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
MODELS = [
    m.strip()
    for m in os.getenv("RODIUMAI_MODELS", "anthropic/claude-sonnet-4-5-20250929,openai/gpt-4o").split(",")
    if m.strip()
]
PREVIEW_LENGTH = 60
MAX_TOKENS = 512

# Roles the LLM understands. Every other role stored in the DB stays out of its prompt (see build_llm_history).
LLM_ROLES = {"user", "assistant"}
NOTE_ROLE = "note"
NOTIFICATION_ROLE = "system-notification"
NOTIFICATION_EVERY = 10  # a notification each time the dialogue reaches a multiple of this many messages
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

app = FastAPI(title="Study Buddy Chatbot")


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None  # first message, truncated; None while the conversation is empty


class ChatRequest(BaseModel):
    conversation_id: int
    message: str = Field(min_length=1)
    model: str


class NoteRequest(BaseModel):
    content: str = Field(min_length=1)


class ModelsResponse(BaseModel):
    models: list[str]
    default: str


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    # A conversation's messages in order; 404 if the conversation doesn't exist.
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def next_seq_for(rows: list[Message]) -> int:
    # seq follows every stored row (notes and notifications included) so it stays unique.
    return rows[-1].seq + 1 if rows else 1


def build_llm_history(rows: list[Message]) -> list[dict]:
    # Notes are personal: they are stored and displayed but never sent to the LLM, so they are dropped here.
    # The same goes for notifications, which only exist for the interface.
    return [{"role": m.role, "content": m.content} for m in rows if m.role in LLM_ROLES]


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


DONE_EVENT = "data: [DONE]\n\n"


def open_llm_stream(model: str, messages: list[dict]) -> tuple[httpx.Client, httpx.Response]:
    # Opened before the response starts, so an upstream failure still becomes a clean 502.
    client = httpx.Client(timeout=60)
    try:
        request = client.build_request(
            "POST",
            RODIUMAI_URL,
            headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
            json={"model": model, "messages": messages, "max_tokens": MAX_TOKENS, "stream": True},
        )
        upstream = client.send(request, stream=True)
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        client.close()
        raise HTTPException(status_code=502, detail="The LLM API call failed.") from exc
    return client, upstream


def stream_turn(
    req: ChatRequest,
    history_len: int,
    next_seq: int,
    client: httpx.Client,
    upstream: httpx.Response,
):
    parts: list[str] = []
    completed = False
    try:
        for line in upstream.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                completed = True
                break
            delta = json.loads(data)["choices"][0]["delta"].get("content")
            if delta:
                parts.append(delta)
                yield sse({"content": delta})
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        completed = False
    finally:
        upstream.close()
        client.close()

    # A connection closed before [DONE] is an interruption, even if it ended without an exception.
    # Nothing is saved for this turn: the DB never holds half an answer.
    if not completed:
        yield sse({"error": "La réponse du LLM a été interrompue."})
        yield DONE_EVENT
        return

    reply = "".join(parts)
    if not reply:
        yield sse({"error": "Le LLM n'a pas renvoyé de réponse."})
        yield DONE_EVENT
        return

    # Count only the dialogue (user + assistant): counting notifications too would shift the total
    # off the multiples of 10 for good after the first one.
    notification = NOTIFICATION_TEXT if (history_len + 2) % NOTIFICATION_EVERY == 0 else None
    turn = [
        Message(conversation_id=req.conversation_id, seq=next_seq, role="user", content=req.message),
        Message(conversation_id=req.conversation_id, seq=next_seq + 1, role="assistant", content=reply),
    ]
    if notification:
        turn.append(Message(
            conversation_id=req.conversation_id, seq=next_seq + 2, role=NOTIFICATION_ROLE, content=notification
        ))

    try:
        with SessionLocal() as db:
            db.add_all(turn)
            db.commit()
    except IntegrityError:
        # Another request already wrote these seq numbers in this conversation while we were streaming.
        yield sse({"error": "La conversation a été modifiée en parallèle, réessayez."})
        yield DONE_EVENT
        return

    yield sse({"done": True, "notification": notification})
    yield DONE_EVENT


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    # Newest first, each joined to its first message (seq 1) for the preview.
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.get("/models")
def list_models() -> ModelsResponse:
    return ModelsResponse(models=MODELS, default=MODELS[0])


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def create_note(conversation_id: int, req: NoteRequest, db: Session = Depends(get_db)) -> MessageResponse:
    rows = load_messages(db, conversation_id)
    note = Message(conversation_id=conversation_id, seq=next_seq_for(rows), role=NOTE_ROLE, content=req.content)
    db.add(note)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="The conversation was updated concurrently, please retry.")
    return MessageResponse(seq=note.seq, role=note.role, content=note.content, created_at=note.created_at)


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    if req.model not in MODELS:
        raise HTTPException(status_code=400, detail="Modèle non autorisé.")

    with SessionLocal() as db:
        rows = load_messages(db, req.conversation_id)
        history = build_llm_history(rows)
        next_seq = next_seq_for(rows)

    # The LLM is stateless: resend the system prompt + the whole conversation each turn.
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": req.message},
    ]
    client, upstream = open_llm_stream(req.model, messages)
    return StreamingResponse(
        stream_turn(req, len(history), next_seq, client, upstream),
        media_type="text/event-stream",
    )
