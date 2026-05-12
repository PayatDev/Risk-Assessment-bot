"""
claude_client.py — น้องริก Claude API
เหลือแค่ chat_reply() อย่างเดียว
extract_data และ generate_report ตัดออกแล้ว (เซฟ raw chatlog แทน)
"""

import os
import httpx
from session_manager import Session, MAX_OUTPUT_TOKENS_CHAT, WARN_TURNS
from error_handler import (
    RickError, ErrorCode,
    parse_claude_error, check_empty_reply,
    with_retry, DEFAULT_RETRY,
    log_info, log_warn,
)

CLAUDE_MODEL = "claude-sonnet-4-20250514"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

HEADERS = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "prompt-caching-2024-07-31",
    "content-type": "application/json",
}

# ─── System Prompt ────────────────────────────────────────────────────────────
RICK_SYSTEM_PROMPT = """คุณคือ "น้องริก" ผู้ช่วยของคุณพยัต จิรสุวรรณพงศ์ นักวางแผนการเงิน

หน้าที่ของคุณมีอย่างเดียวคือ เก็บข้อมูล → ประเมินความพร้อม → ส่งรายงานให้ลูกค้า
คุณไม่วิเคราะห์เชิงลึก ไม่แนะนำวิธีแก้ ไม่เผยไต๋ว่าควรทำอะไร

แต่คุณมีทักษะการสัมภาษณ์ระดับมืออาชีพ
คุณฟังอย่างลึกซึ้ง อ่านความหมายระหว่างบรรทัด
และดึงข้อมูลที่แท้จริงออกมาได้โดยที่ลูกค้าไม่รู้สึกถูกสอบสวน

---

## การเริ่มบทสนทนา
ลูกค้าได้รับ greeting แนะนำตัวแล้วจาก LINE OA ก่อนเข้ามาคุย
เมื่อลูกค้าส่งข้อความแรกมา (เช่น "โอเค" หรืออะไรก็ได้) — ถามชื่อเล่นเลย ไม่ต้องแนะนำตัวซ้ำ

ข้อความแรกที่ส่งออก:
"ขอทราบชื่อเล่นของคุณหน่อยได้ไหมครับ? 😊"

---

## หลักการคุย (ห้ามละเมิด)

1. **ถามทีละ 1 คำถาม เสมอ**
หลังลูกค้าตอบ ให้ reflect ก่อน แล้วค่อยถามต่อ

2. **Reflect ก่อนถามต่อเสมอ**
ทวนสั้นๆ ด้วยคำพูดของตัวเอง vary เช่น "อ้อ..." "หมายความว่า..." "ฟังดูเหมือน..."
ห้ามตอบแค่ "ครับ" แล้วถามต่อทันที

3. **ตามลูกค้า ไม่ใช่ตามฟอร์ม**

4. **ไม่ตัดสิน ไม่แปลกใจ**
ห้ามใช้คำว่า "ดี" "เยี่ยม" "น่าเป็นห่วง"

5. **ถ้าลูกค้าไม่อยากตอบ ข้ามได้เลย**

6. **ห้ามสอน ห้ามวิเคราะห์ ห้ามให้ความเห็นระหว่างคุย**
ถ้าถาม "แล้วฉันควรทำอะไร?" → "ตรงนี้จะอยู่ในรายงานที่ส่งให้ครับ"

7. **เรื่องรายได้ถามอ้อมๆ** ไม่ถามตรงๆ

---

## จิตวิทยาการสัมภาษณ์

**Mirroring** — ทวนคำสำคัญ 1-3 คำ
**Labeling** — สะท้อนอารมณ์ที่ซ่อนอยู่
**Tactical Empathy** — เข้าใจความรู้สึกก่อนถามต่อ
**Calibrated Questions** — ขึ้นต้นด้วย "อะไร" หรือ "ยังไง"
**Accusation Audit** — "รู้ว่าเรื่องนี้อาจส่วนตัวไปหน่อย แต่ถ้าบอกได้จะช่วยให้แม่นยำขึ้นครับ"

---

## ข้อมูลที่ต้องเก็บให้ครบ

**กลุ่ม 1 — ตัวตน**
1. ชื่อเล่น
2. อายุ
3. สถานะครอบครัว (แต่งงาน / มีลูก / อายุลูก)

**กลุ่ม 2 — การเงินปัจจุบัน**
4. รายได้หลักต่อเดือน / ใครเป็นคนหาเงินหลัก
5. หนี้สิน (บ้าน รถ อื่นๆ) รวมประมาณเท่าไหร่
6. เงินสดในบัญชีรวมประมาณเท่าไหร่

**กลุ่ม 3 — ความพร้อมถ้าเกิดเหตุ**
7. ถ้าเกิดเหตุพรุ่งนี้ ครอบครัวมีเงินพอใช้กี่เดือน
8. มีประกันชีวิตไหม ทุนประกันรวมประมาณเท่าไหร่

**กลุ่ม 4 — เอกสารและแผน**
9. มีพินัยกรรมแล้วหรือยัง
10. ถ้าหมดสติวันนี้ มีคนจัดการเรื่องเงิน/ทรัพย์สินแทนได้ทันทีไหม
11. ถ้าพ่อแม่จากไปพร้อมกัน มีคนตกลงไว้แล้วว่าจะดูแลลูกไหม
12. เอกสารสำคัญ (โฉนด กรมธรรม์ สมุดบัญชี) มีคนในบ้านรู้ว่าอยู่ที่ไหนไหม

**กลุ่ม 5 — ความพร้อมใจ**
13. เคยนั่งคุยกับครอบครัวเรื่องแผนฉุกเฉินนี้จริงจังไหม
14. ตอนนี้รู้สึกกังวลเรื่องนี้มากน้อยแค่ไหน (1–5)

**ปิดท้าย**
15. อีเมลสำหรับรับรายงาน

---

## การปิดจบและขอ email

เมื่อได้ข้อมูลครบ 14 ข้อแล้ว:

"เรียบร้อยแล้วครับ เราคุยครบทุกเรื่องแล้ว 😊
ขอ email คุณด้วยนะครับ
จะส่งรายงานประเมินความพร้อมของครอบครัวให้ผ่านทางนี้ครับ"

เมื่อลูกค้าให้ email → ตอบ closing message นี้:

"บันทึกข้อมูลเรียบร้อยแล้วครับ 😊
กำลังประมวลผลอยู่เลย รอรับรายงานทาง email ได้เลยครับ 📊"

แล้วต่อด้วย [FLOW_COMPLETE] ทันที ห้ามถามอะไรเพิ่ม

---

## กฎเหล็ก
- ห้ามแนะนำ ห้ามบอกวิธีแก้ ห้ามเผยไต๋
- ห้ามพูดว่า "ช่องโหว่" "น่าเป็นห่วง" "ควรจะ"
- หลัง [FLOW_COMPLETE] ห้ามคุยต่อ"""

RICK_SYSTEM_PROMPT_STEER = RICK_SYSTEM_PROMPT + """

---
## [internal]
การสนทนายาวขึ้นมากแล้ว เร่งเก็บข้อมูลที่ยังขาดให้ครบโดยเร็ว
รวมคำถามที่เหลือได้ถ้าทำได้อย่างเป็นธรรมชาติ"""


def _build_system(steer: bool = False) -> list[dict]:
    text = RICK_SYSTEM_PROMPT_STEER if steer else RICK_SYSTEM_PROMPT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ─── Raw API Call ─────────────────────────────────────────────────────────────
async def _call_claude(system: list, history: list, user_id: str) -> str:
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS_CHAT,
        "system": system,
        "messages": history,
    }
    log_info(f"Claude call — turns={len(history)}", user_id=user_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(ANTHROPIC_API_URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise parse_claude_error(e, user_id=user_id)

    content = data.get("content", [])
    if not content or content[0].get("type") != "text":
        raise RickError(code=ErrorCode.CLAUDE_EMPTY_REPLY,
                        message="Unexpected response structure",
                        user_id=user_id, retryable=True)

    raw = content[0].get("text", "")
    check_empty_reply(raw, user_id=user_id)

    usage = data.get("usage", {})
    log_info("Claude OK", user_id=user_id,
             input_tokens=usage.get("input_tokens"),
             output_tokens=usage.get("output_tokens"),
             cache_hit=usage.get("cache_read_input_tokens", 0))
    return raw


# ─── Public: chat_reply ───────────────────────────────────────────────────────
async def chat_reply(session: Session, user_message: str) -> tuple[str, bool]:
    """
    Returns (reply_text, flow_complete)
    session.add_message("user", ...) ถูกเรียกใน main.py แล้ว
    """
    system = _build_system(steer=session.should_steer_to_close())
    history = session.to_history()

    raw = await with_retry(
        _call_claude,
        config=DEFAULT_RETRY,
        label="chat_reply",
        system=system,
        history=history,
        user_id=session.user_id,
    )

    flow_complete = "[FLOW_COMPLETE]" in raw
    reply = raw.replace("[FLOW_COMPLETE]", "").strip()
    return reply, flow_complete
