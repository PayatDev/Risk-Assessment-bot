"""
main.py — น้องริค FastAPI App
LINE Bot webhook → Claude chat → save chatlog → fixed message → dead session
"""

import os
import hmac
import hashlib
import base64
import json
import asyncio
import threading
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
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
LINE_PUSH_URL  = "https://api.line.me/v2/bot/message/push"

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


# ─── LINE Push (ไม่มีหมดอายุ ใช้ user_id) ─────────────────────────────────────
async def line_push(user_id: str, text: str) -> bool:
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(LINE_PUSH_URL, headers=headers, json=payload)
            resp.raise_for_status()
            return True
    except Exception as e:
        err = parse_line_error(e, user_id=user_id)
        log_error(err, context={"action": "line_push_failed"})
        return False


# Completed users — 2 layers: memory + session marker (กัน Railway restart)
COMPLETED_USERS: dict = {}


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


async def _run_report_agent(chatlog: dict, user_id: str):
    """Wrapper สำหรับเรียก report_agent.run() ใน thread"""
    try:
        from report_agent import run as agent_run
        await agent_run(chatlog)
        log_info("Report agent completed", user_id=user_id)
    except Exception as e:
        log_error(
            RickError(code=ErrorCode.UNKNOWN, message=f"report_agent failed: {e}", user_id=user_id)
        )


# ─── Background: save chatlog + push fixed message ───────────────────────────
async def finalize_session(user_id: str):
    """
    Background task:
    1. save chatlog ลง Sheets
    2. push fixed message ด้วย user_id (ไม่มีหมดอายุ)
    3. mark complete / timeout
    """
    session = sm.get(user_id)
    if not session:
        return

    _extract_basic_data(session)

    from sheets_handler import save_chatlog
    saved = save_chatlog(session)

    if saved:
        session.mark_complete(output_path="sheets")
        COMPLETED_USERS[user_id] = True  # mark ใน memory
        log_info("Session complete — chatlog saved", user_id=user_id)
        await line_push(user_id, COMPLETE_MSG)

        # Trigger report agent
        chatlog = {
            "nickname": session.data.nickname or "",
            "email": session.data.email or "",
            "messages": [{"role": m.role, "content": m.content} for m in session.messages],
        }
        threading.Thread(
            target=lambda: asyncio.run(_run_report_agent(chatlog, user_id)),
            daemon=True
        ).start()

    else:
        session.mark_timeout()
        log_error(
            RickError(code=ErrorCode.OUTPUT_SAVE_FAILED,
                      message="save_chatlog failed", user_id=user_id),
        )
        await line_push(user_id, ERROR_SAVE_MSG)


def _extract_basic_data(session):
    """Extract email และ nickname จาก conversation แบบง่ายๆ ไม่เรียก Claude"""
    # Extract email
    for msg in reversed(session.messages):
        if msg.role == "user" and "@" in msg.content and "." in msg.content:
            words = msg.content.split()
            for w in words:
                if "@" in w and "." in w:
                    session.data.email = w.strip()
                    break
        if session.data.email:
            break

    # Extract nickname จาก assistant message แรก
    # น้องริคจะพูดถึงชื่อเล่นในช่วงต้นสนทนา
    for msg in session.messages:
        if msg.role == "assistant" and session.data.nickname:
            break
        if msg.role == "user" and len(msg.content) <= 20:
            # message สั้นๆ ในช่วงต้น มักเป็นชื่อเล่น
            if msg == session.messages[0] or session.messages.index(msg) <= 2:
                candidate = msg.content.strip()
                # ไม่ใช่ตัวเลข ไม่ใช่ email ไม่ยาวเกิน
                if candidate and "@" not in candidate and not candidate.isdigit():
                    session.data.nickname = candidate


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


GREETING_MSG = """สวัสดีครับ ผมน้องริค 😊
มีหน้าที่เก็บข้อมูลเกี่ยวกับครอบครัวของคุณครับ
คุณพยัตจะใช้ข้อมูลที่เราคุยกันวันนี้ในการประเมินความเสี่ยง

เราจะคุยกันประมาณ 10-15 นาทีนะครับ
พร้อมแล้ว พิมพ์ "โอเค" ได้เลยครับ"""


async def line_push_greeting(user_id: str):
    """ส่ง greeting หลังแอดเพื่อน"""
    await asyncio.sleep(1)  # รอ LINE process follow event ก่อน
    await line_push(user_id, GREETING_MSG)


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    data = json.loads(body)
    events = data.get("events", [])

    for event in events:
        # ─── Follow event (แอดเพื่อน) → ส่ง greeting ────────────────────────────
        if event.get("type") == "follow":
            user_id = event["source"]["userId"]
            log_info("New follow — sending greeting", user_id=user_id)
            threading.Thread(
                target=lambda uid=user_id: asyncio.run(line_push_greeting(uid)),
                daemon=True
            ).start()
            continue

        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_id = event["source"]["userId"]
        reply_token = event["replyToken"]
        user_text = event["message"]["text"].strip()

        # ─── Guard: dead session → fixed message เสมอ ────────────────────────
        # ─── Guard: completed (memory หรือ session) ──────────────────────────
        if user_id in COMPLETED_USERS or (sm.get(user_id) and sm.get(user_id).is_dead()):
            await line_reply(reply_token, COMPLETE_MSG, user_id=user_id)
            log_info("Completed session — sent fixed message", user_id=user_id)
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

        # ─── Flow complete → reply ทันที แล้วค่อย save ใน background ──────────
        if flow_complete:
            log_info("Flow complete — queueing finalize", user_id=user_id,
                     turns=session.turn_count)
            # push message ใน background หลัง save Sheets สำเร็จ
            threading.Thread(target=lambda: __import__('asyncio').run(finalize_session(user_id)), daemon=True).start()

    return JSONResponse({"status": "ok"})


# ─── Dev: test report agent ──────────────────────────────────────────────────
@app.get("/test-report")
async def test_report():
    """ดึง chatlog row ล่าสุดจาก Sheets แล้วรัน report agent"""
    import threading, asyncio
    from sheets_handler import _get_service, SHEET_ID, SHEET_NAME

    try:
        # ดึง row ล่าสุดจาก Sheets
        svc = _get_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A:G"
        ).execute()
        rows = result.get("values", [])
        if len(rows) < 2:
            return {"status": "error", "message": "ไม่มีข้อมูลใน Sheets"}

        last_row = rows[-1]
        # col G (index 6) = chatlog_json
        if len(last_row) < 7:
            return {"status": "error", "message": "ไม่มี chatlog_json ใน row ล่าสุด"}

        chatlog = json.loads(last_row[6])
        # ดึง nickname จาก col C ก่อน ถ้าไม่มีค่อย extract จาก messages
        nickname = last_row[2] if len(last_row) > 2 and last_row[2] else ""
        if not nickname:
            # หา user message ที่ตอบหลัง bot ถามชื่อเล่น
            msgs = chatlog.get("messages", [])
            for i, msg in enumerate(msgs):
                if msg.get("role") == "assistant" and "ชื่อเล่น" in msg.get("content", ""):
                    if i + 1 < len(msgs) and msgs[i + 1].get("role") == "user":
                        candidate = msgs[i + 1].get("content", "").strip()
                        if len(candidate) <= 10 and "@" not in candidate:
                            nickname = candidate
                            break
        chatlog["nickname"] = nickname or "ลูกค้า"

        threading.Thread(
            target=lambda: asyncio.run(_run_report_agent(chatlog, "TEST")),
            daemon=True
        ).start()

        return {"status": "started", "nickname": chatlog["nickname"], "message": "ดู Railway logs"}

    except Exception as e:
        return {"status": "error", "message": str(e)}


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
