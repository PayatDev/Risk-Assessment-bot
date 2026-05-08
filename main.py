"""
main.py — น้องริค FastAPI App
LINE Bot webhook → Claude chat → save chatlog → fixed message → dead session
"""

import os
import hmac
import hashlib
import base64
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

import session_manager as sm
from session_manager import SessionStatus
from claude_client import chat_reply
from error_handler import (
    RickError, ErrorCode, USER_MESSAGES,
    parse_line_error, log_error, log_info, log_warn,
)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# Fixed message หลัง save chatlog สำเร็จ
COMPLETE_MSG = (
    "น้องริคทำหน้าที่เสร็จแล้วครับ 😊\n"
    "รายงานของคุณกำลังจัดทำอยู่\n"
    "หากมีข้อสงสัยติดต่อคุณพยัตได้โดยตรงครับ"
)

# Budget limit
BUDGET_EXCEEDED_MSG = "ขอโทษนะครับ session นี้ยาวเกินไปแล้ว กรุณาเริ่มการประเมินใหม่ได้เลยครับ 🙏"

# Error messages
ERROR_CLAUDE_MSG = "ขออภัยครับ ระบบขัดข้องชั่วคราว กรุณาส่งข้อความใหม่อีกครั้งครับ"
ERROR_SAVE_MSG = "ขออภัยครับ เกิดข้อผิดพลาดในการบันทึกข้อมูล กรุณาติดต่อคุณพยัตโดยตรงครับ"


# ─── LINE Signature Verification ─────────────────────────────────────────────
def verify_line_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode("utf-8"), signature)


# ─── LINE Reply ───────────────────────────────────────────────────────────────
async def line_reply(reply_token: str, text: str, user_id: str = None) -> bool:
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(LINE_REPLY_URL, headers=headers, json=payload)
            resp.raise_for_status()
            return True
    except Exception as e:
        err = parse_line_error(e, user_id=user_id)
        if err.code == ErrorCode.LINE_INVALID_TOKEN:
            log_warn("LINE reply token expired", user_id=user_id)
        else:
            # LINE API error → log only, ไม่ reply ลูกค้า
            log_error(err, context={"action": "line_reply_failed"})
        return False


# ─── Background: save chatlog → send fixed message ───────────────────────────
async def finalize_session(user_id: str, reply_token: str):
    """
    1. extract email/nickname จาก last messages
    2. save chatlog ลง Sheets
    3. ถ้า OK → mark complete → ส่ง fixed message
    4. ถ้า fail → ส่ง error message ให้ลูกค้า
    """
    session = sm.get(user_id)
    if not session:
        return

    # Extract email/nickname อย่างง่ายจาก messages
    _extract_basic_data(session)

    from sheets_handler import save_chatlog
    saved = save_chatlog(session)

    if saved:
        session.mark_complete(output_path="sheets")
        log_info("Session complete — chatlog saved", user_id=user_id)
        await line_reply(reply_token, COMPLETE_MSG, user_id=user_id)
    else:
        # Save failed → แจ้งลูกค้า แต่ยัง mark timeout เพื่อไม่ให้ loop
        session.mark_timeout()
        log_error(
            RickError(code=ErrorCode.OUTPUT_SAVE_FAILED,
                      message="save_chatlog failed", user_id=user_id),
        )
        await line_reply(reply_token, ERROR_SAVE_MSG, user_id=user_id)


def _extract_basic_data(session):
    """Extract email และ nickname จาก conversation แบบง่ายๆ ไม่เรียก Claude"""
    for msg in reversed(session.messages):
        if msg.role == "user" and "@" in msg.content and "." in msg.content:
            words = msg.content.split()
            for w in words:
                if "@" in w and "." in w:
                    session.data.email = w.strip()
                    break
        if session.data.email:
            break


# ─── FastAPI App ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log_info("น้องริค ready 🤖")
    yield
    log_info("น้องริค shutting down")


app = FastAPI(title="น้องริค", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "bot": "น้องริค"}


@app.get("/test-sheets")
async def test_sheets():
    from sheets_handler import test_connection
    return test_connection()


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    data = json.loads(body)
    events = data.get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_id = event["source"]["userId"]
        reply_token = event["replyToken"]
        user_text = event["message"]["text"].strip()

        # ─── Guard: dead session → fixed message เสมอ ────────────────────────
        session = sm.get(user_id)
        if session and session.is_dead():
            await line_reply(reply_token, COMPLETE_MSG, user_id=user_id)
            log_info("Dead session — sent fixed message", user_id=user_id)
            continue

        # ─── Get/create session ───────────────────────────────────────────────
        session = sm.get_or_create(user_id)

        # ─── Guard: budget exceeded ───────────────────────────────────────────
        if session.should_force_close():
            log_warn("Budget exceeded", user_id=user_id, turns=session.turn_count)
            await line_reply(reply_token, BUDGET_EXCEEDED_MSG, user_id=user_id)
            session.mark_timeout()
            continue

        # ─── Add user message ─────────────────────────────────────────────────
        session.add_message("user", user_text)

        # ─── Claude API ───────────────────────────────────────────────────────
        try:
            reply_text, flow_complete = await chat_reply(session, user_text)

        except RickError as e:
            log_error(e, context={"turn": session.turn_count})
            # Claude error → แจ้งลูกค้าให้ลองใหม่
            await line_reply(reply_token, ERROR_CLAUDE_MSG, user_id=user_id)
            if not e.retryable:
                session.mark_timeout()
            continue

        except Exception as e:
            log_error(RickError(code=ErrorCode.UNKNOWN, message=str(e),
                                user_id=user_id, original=e))
            await line_reply(reply_token, ERROR_CLAUDE_MSG, user_id=user_id)
            continue

        # ─── Add assistant reply ──────────────────────────────────────────────
        session.add_message("assistant", reply_text)

        # ─── Reply to user ────────────────────────────────────────────────────
        if reply_text:
            await line_reply(reply_token, reply_text, user_id=user_id)

        # ─── Flow complete → save chatlog in background ───────────────────────
        if flow_complete:
            log_info("Flow complete — queueing finalize", user_id=user_id,
                     turns=session.turn_count)
            background_tasks.add_task(finalize_session, user_id, reply_token)

    return JSONResponse({"status": "ok"})


# ─── Dev endpoints ────────────────────────────────────────────────────────────
@app.get("/sessions/{user_id}")
async def get_session_info(user_id: str):
    session = sm.get(user_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "user_id": session.user_id,
        "status": session.status.value,
        "turn_count": session.turn_count,
        "budget_remaining": session.budget_remaining(),
        "email": session.data.email,
        "nickname": session.data.nickname,
    }


@app.delete("/sessions/{user_id}")
async def reset_session(user_id: str):
    sm.delete(user_id)
    return {"status": "deleted", "user_id": user_id}
