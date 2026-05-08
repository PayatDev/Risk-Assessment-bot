"""
main.py — น้องริค FastAPI App
LINE Bot webhook → Claude chat → output gen → dead session
"""

import os
import hmac
import hashlib
import base64
import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

import session_manager as sm
from session_manager import SessionStatus
from claude_client import chat_reply
from output_handler import process_and_save
from error_handler import (
    RickError, ErrorCode, USER_MESSAGES,
    parse_line_error, log_error, log_info, log_warn,
)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# Message เมื่อ session จบแล้ว (ไม่คุยต่อ)
DEAD_SESSION_MSG = None  # None = ไม่ตอบเลย (เงียบ) | ใส่ string ถ้าอยากตอบสั้นๆ

# Message เมื่อ budget หมด
BUDGET_EXCEEDED_MSG = (
    "ขอโทษนะครับ session นี้ยาวเกินไปแล้ว "
    "กรุณาเริ่มการประเมินใหม่ได้เลยครับ 🙏"
)


# ─── LINE Signature Verification ─────────────────────────────────────────────
def verify_line_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ─── LINE Reply Helper ────────────────────────────────────────────────────────
async def line_reply(reply_token: str, text: str, user_id: str = None) -> bool:
    """
    Send reply to LINE user.
    Returns True on success, False on failure.
    Does NOT raise — LINE reply failures are non-critical (token expires in 30s anyway).
    """
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(LINE_REPLY_URL, headers=headers, json=payload)
            resp.raise_for_status()
            return True
    except Exception as e:
        err = parse_line_error(e, user_id=user_id)
        # Token expired (400/401) is expected sometimes — log at debug level
        if err.code == ErrorCode.LINE_INVALID_TOKEN:
            log_warn("LINE reply token expired — skipping", user_id=user_id)
        else:
            log_error(err, context={"reply_token": reply_token[:8] + "..."})
        return False


# ─── Background: gen output + mark complete ──────────────────────────────────
async def finalize_session(user_id: str):
    """
    Background task หลัง flow_complete = True
    1. gen output files (with retry inside process_and_save)
    2. mark session complete → bot หยุดตอบ
    3. TODO: send email with report
    """
    session = sm.get(user_id)
    if not session:
        log_warn("finalize_session: session not found", user_id=user_id)
        return

    try:
        log_info("Starting output generation", user_id=user_id)
        chatlog_path, report_path = await process_and_save(session)
        session.mark_complete(output_path=str(report_path.parent))
        log_info(
            "Session finalized",
            user_id=user_id,
            folder=str(report_path.parent),
        )
        # TODO: await send_email(session.data.email, report_path)

    except RickError as e:
        log_error(e, context={"phase": "finalize_session"})
        # Mark timeout so bot doesn't keep responding to a broken session
        session.mark_timeout()

    except Exception as e:
        log_error(
            RickError(code=ErrorCode.UNKNOWN, message=str(e), user_id=user_id, original=e),
            context={"phase": "finalize_session_unexpected"},
        )
        session.mark_timeout()


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


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    import json
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

        # ─── Guard: dead session → เงียบ ─────────────────────────────────────
        session = sm.get(user_id)
        if session and session.is_dead():
            if DEAD_SESSION_MSG:
                await line_reply(reply_token, DEAD_SESSION_MSG, user_id=user_id)
            log_info("Dead session — ignoring message", user_id=user_id)
            continue

        # ─── Get/Create session ───────────────────────────────────────────────
        session = sm.get_or_create(user_id)

        # ─── Guard: budget exceeded ───────────────────────────────────────────
        if session.should_force_close():
            log_warn("Budget exceeded — forcing timeout", user_id=user_id,
                     turns=session.turn_count, tokens=session.total_input_tokens)
            await line_reply(reply_token, BUDGET_EXCEEDED_MSG, user_id=user_id)
            session.mark_timeout()
            continue

        # ─── Add user message ─────────────────────────────────────────────────
        session.add_message("user", user_text)

        # ─── Claude API call ──────────────────────────────────────────────────
        try:
            reply_text, flow_complete = await chat_reply(session, user_text)

        except RickError as e:
            log_error(e, context={"turn": session.turn_count})
            # ส่ง user-facing message ถ้ามี
            user_msg = USER_MESSAGES.get(e.code, USER_MESSAGES[ErrorCode.UNKNOWN])
            if user_msg:
                await line_reply(reply_token, user_msg, user_id=user_id)
            # ถ้า non-retryable (auth error ฯลฯ) → mark timeout
            if not e.retryable:
                session.mark_timeout()
            continue

        except Exception as e:
            # Unexpected — catch-all
            log_error(
                RickError(code=ErrorCode.UNKNOWN, message=str(e), user_id=user_id, original=e)
            )
            await line_reply(
                reply_token,
                USER_MESSAGES[ErrorCode.UNKNOWN],
                user_id=user_id,
            )
            continue

        # ─── Validate reply ───────────────────────────────────────────────────
        if not reply_text:
            # Claude returned empty after stripping [FLOW_COMPLETE]
            # Could be normal (flow_complete only) — check
            if not flow_complete:
                log_warn("Claude returned empty reply without flow_complete", user_id=user_id)

        # ─── Add assistant reply to session ──────────────────────────────────
        session.add_message("assistant", reply_text)

        # ─── Send reply to LINE ───────────────────────────────────────────────
        if reply_text:
            await line_reply(reply_token, reply_text, user_id=user_id)

        # ─── Trigger output generation ────────────────────────────────────────
        if flow_complete:
            log_info("Flow complete — queueing finalize", user_id=user_id,
                     turns=session.turn_count)
            background_tasks.add_task(finalize_session, user_id)

    return JSONResponse({"status": "ok"})


# ─── Dev: test Google Sheets ──────────────────────────────────────────────────
@app.get("/test-sheets")
async def test_sheets():
    from sheets_handler import test_connection
    return test_connection()


# ─── Dev: manual session inspection ──────────────────────────────────────────
@app.get("/sessions/{user_id}")
async def get_session_info(user_id: str):
    session = sm.get(user_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "user_id": session.user_id,
        "status": session.status.value,
        "turn_count": session.turn_count,
        "total_input_tokens_est": session.total_input_tokens,
        "budget_remaining": session.budget_remaining(),
        "data_collected": {
            "nickname": session.data.nickname,
            "email": session.data.email,
        },
    }


@app.delete("/sessions/{user_id}")
async def reset_session(user_id: str):
    """Dev only — reset session สำหรับ testing"""
    sm.delete(user_id)
    return {"status": "deleted", "user_id": user_id}
