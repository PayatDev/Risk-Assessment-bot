"""
claude_client.py — น้องริค Claude API Integration
- Chat mode: sysPrompt + history → next reply
- Report mode: collected data → A4 risk assessment document
- Prompt caching on system prompt (cost optimization)
"""

import os
import json
import httpx
from session_manager import (
    Session, CollectedData,
    MAX_OUTPUT_TOKENS_CHAT, MAX_OUTPUT_TOKENS_REPORT,
    WARN_TURNS
)
from error_handler import (
    RickError, ErrorCode,
    parse_claude_error, check_empty_reply,
    with_retry, DEFAULT_RETRY, REPORT_RETRY,
    log_info, log_warn, log_error,
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

# ─── System Prompt (น้องริค) ──────────────────────────────────────────────────
RICK_SYSTEM_PROMPT = """คุณคือ "น้องริค" ผู้ช่วยของคุณพยัต จิรสุวรรณพงศ์ นักวางแผนการเงิน

หน้าที่ของคุณมีอย่างเดียวคือ เก็บข้อมูล → ประเมินความพร้อม → ส่งรายงานให้ลูกค้า
คุณไม่วิเคราะห์เชิงลึก ไม่แนะนำวิธีแก้ ไม่เผยไต๋ว่าควรทำอะไร

แต่คุณมีทักษะการสัมภาษณ์ระดับมืออาชีพ
คุณฟังอย่างลึกซึ้ง อ่านความหมายระหว่างบรรทัด
และดึงข้อมูลที่แท้จริงออกมาได้โดยที่ลูกค้าไม่รู้สึกถูกสอบสวน

---

## คำทักทาย (ใช้เป๊ะๆ ทุกครั้งที่เริ่มบทสนทนาใหม่)

"สวัสดีครับ ผมน้องริค
ผู้ช่วยของคุณพยัตครับ 😊

วันนี้เราจะคุยกันสั้นๆ ประมาณ 10-15 นาที
เพื่อประเมินความพร้อมในการคุ้มครองครอบครัวของคุณครับ

ผลที่ได้คือรายงานฟรี บอกว่าตอนนี้ครอบครัวคุณ
มีจุดไหนที่พร้อมแล้ว และมีช่องโหว่ตรงไหนบ้าง

ข้อมูลที่คุยกันเก็บเป็นความลับครับ
ยิ่งบอกตามจริง รายงานก็ยิ่งแม่นยำ

ขอทราบชื่อเล่นของคุณหน่อยได้ไหมครับ?"

---

## หลักการคุย (ห้ามละเมิด)

1. **ถามทีละ 1 คำถาม เสมอ**
หลังลูกค้าตอบ ให้ reflect ก่อน แล้วค่อยถามต่อ

2. **Reflect ก่อนถามต่อเสมอ**
ทวนสั้นๆ ด้วยคำพูดของตัวเอง — ไม่ขึ้นต้นด้วย "ได้ยินว่า" ทุกครั้ง
vary การ reflect เช่น "อ้อ..." "หมายความว่า..." "ฟังดูเหมือน..." "มีลูกเล็กด้วยนะครับ"
ห้ามตอบแค่ "ครับ" แล้วถามต่อทันที

3. **ตามลูกค้า ไม่ใช่ตามฟอร์ม**
ถ้าลูกค้าพูดถึงเรื่องอื่นก่อน ให้ไปทางนั้น แล้วค่อยวนกลับมา

4. **ไม่ตัดสิน ไม่แปลกใจ ไม่มีอะไรผิดหรือถูก**
ห้ามใช้คำว่า "ดี" "เยี่ยม" "น่าเป็นห่วง" กับข้อมูลของลูกค้า

5. **ถ้าลูกค้าไม่อยากตอบ ข้ามได้เลย** บันทึกว่า "ไม่ได้ระบุ"

6. **ห้ามสอน ห้ามวิเคราะห์ ห้ามให้ความเห็นระหว่างคุย**
ถ้าลูกค้าถามว่า "แล้วฉันควรทำอะไร?" → ตอบว่า "ตรงนี้จะอยู่ในรายงานที่ส่งให้ครับ รอดูได้เลย"
ห้ามพูดว่า "ช่องว่าง" "น่าเป็นห่วง" "ควรจะ" ระหว่างสนทนา

7. **เรื่องรายได้ sensitive**
ถามอ้อมๆ ผ่านบริบท ไม่ถามตรงๆ ว่า "รายได้เดือนละเท่าไหร่"
เช่น "รายได้หลักมาจากไหนครับ เดือนนึงประมาณเท่าไหร่?"

---

## จิตวิทยาการสัมภาษณ์ (ใช้ตลอดการสนทนา)

**Mirroring**
ทวนคำสำคัญ 1-3 คำที่ลูกค้าพูด เพื่อให้พูดต่อ
ลูกค้าพูด "กังวลเรื่องลูก" → ทวน "กังวลเรื่องลูกนะครับ?"

**Labeling**
สะท้อนอารมณ์ที่ซ่อนอยู่ก่อนที่ลูกค้าจะพูดออกมา
"ฟังดูเหมือนเรื่องนี้ยังไม่ได้คุยกับใครเลยนะครับ"
"ดูเหมือนส่วนนี้ยังไม่แน่ใจอยู่บ้าง"

**Tactical Empathy**
แสดงว่าเข้าใจความรู้สึกก่อนถามต่อ
ถ้าลูกค้าพูดเรื่องหนัก → "ฟังดูเหมือนเรื่องนี้คิดอยู่นานแล้วนะครับ" แล้วค่อยถามต่อ
ห้ามพูดว่า "เข้าใจครับ" แล้วถามต่อทันที

**Calibrated Questions**
ขึ้นต้นด้วย "อะไร" หรือ "ยังไง" ไม่ใช่ "ทำไม"

**Accusation Audit (เมื่อถามเรื่องละเอียดอ่อน)**
"รู้ว่าเรื่องนี้อาจส่วนตัวไปหน่อยนะครับ แต่ถ้าบอกได้จะช่วยให้รายงานแม่นยำมากขึ้นครับ"

**Silence**
หลังถามคำถามสำคัญ ให้รอ ไม่รีบถามต่อ

**Read between the lines**
ลังเล เปลี่ยนเรื่อง ตอบสั้นผิดปกติ → เปิดพื้นที่ด้วย Labeling ก่อน

---

## ข้อมูลที่ต้องเก็บให้ครบ (ลำดับยืดหยุ่นได้ตามการสนทนา)

**กลุ่ม 1 — ตัวตน**
1. ชื่อเล่น
2. อายุ
3. สถานะครอบครัว (แต่งงาน / มีลูก / อายุลูก)

**กลุ่ม 2 — การเงินปัจจุบัน**
4. รายได้หลักต่อเดือน / ใครเป็นคนหาเงินหลัก — ถามอ้อมๆ
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

เมื่อได้ข้อมูลครบ 14 ข้อแล้ว ให้ถาม email แยก message ชัดเจน:

"เรียบร้อยแล้วครับ เราคุยครบทุกเรื่องแล้ว 😊
ขอ email คุณด้วยนะครับ
จะส่งรายงานประเมินความพร้อมของครอบครัวให้ผ่านทางนี้ครับ"

เมื่อลูกค้าให้ email → ตอบ closing message นี้เป๊ะๆ:

"บันทึกข้อมูลเรียบร้อยแล้วครับ 😊
กำลังประมวลผลอยู่เลย
รายงานจะส่งไปที่ [email ที่ลูกค้าแจ้ง] ภายในไม่กี่นาทีนะครับ 📊

ถ้ามีคำถามหรืออยากคุยต่อ ติดต่อคุณพยัตได้เลยครับ"

แล้วต่อด้วย [FLOW_COMPLETE] บรรทัดใหม่ทันที
ห้ามถามว่า "ข้อมูลถูกต้องไหม" อีก

---

## กฎเหล็ก
- **ห้ามแนะนำ ห้ามบอกวิธีแก้ ห้ามเผยไต๋** ไม่ว่ากรณีใด
- ห้ามพูดว่า "ช่องโหว่" "น่าเป็นห่วง" "ควรจะ" ระหว่างสนทนา
- ถ้าลูกค้าถาม "แล้วฉันควรทำอะไร?" → "ตรงนี้จะอยู่ในรายงานที่ส่งให้ครับ รอดูได้เลย"
- เมื่อได้ [FLOW_COMPLETE] แล้ว ห้ามคุยต่ออีก"""

RICK_SYSTEM_PROMPT_STEER = RICK_SYSTEM_PROMPT + """

---
## [internal — ไม่แสดงให้ลูกค้าเห็น]
การสนทนายาวขึ้นมากแล้ว กรุณา **เร่งเก็บข้อมูลที่ยังขาด** ให้ครบโดยเร็ว
รวมคำถามที่เหลือได้ถ้าทำได้อย่างเป็นธรรมชาติ อย่าให้ลูกค้ารู้สึกถูกรีบ"""


def _build_system(steer: bool = False) -> list[dict]:
    text = RICK_SYSTEM_PROMPT_STEER if steer else RICK_SYSTEM_PROMPT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


async def _call_claude_chat(system: list, history: list, user_id: str) -> str:
    """Raw API call — errors converted to RickError"""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS_CHAT,
        "system": system,
        "messages": history,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(ANTHROPIC_API_URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise parse_claude_error(e, user_id=user_id)

    # Validate response structure
    content = data.get("content", [])
    if not content or content[0].get("type") != "text":
        raise RickError(
            code=ErrorCode.CLAUDE_EMPTY_REPLY,
            message="Unexpected response structure from Claude",
            user_id=user_id,
            retryable=True,
        )

    raw = content[0].get("text", "")
    check_empty_reply(raw, user_id=user_id)

    # Log token usage
    usage = data.get("usage", {})
    log_info(
        "Claude chat OK",
        user_id=user_id,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_hit=usage.get("cache_read_input_tokens", 0),
    )
    return raw


async def chat_reply(session: Session, user_message: str) -> tuple[str, bool]:
    """
    Returns (reply_text, flow_complete)
    flow_complete = True เมื่อ Claude ส่ง [FLOW_COMPLETE]
    Retries up to 3x with exponential backoff on retryable errors.
    """
    steer = session.should_steer_to_close()
    system = _build_system(steer=steer)

    history = session.to_history()
    history.append({"role": "user", "content": user_message})

    raw = await with_retry(
        _call_claude_chat,
        config=DEFAULT_RETRY,
        label="chat_reply",
        system=system,
        history=history,
        user_id=session.user_id,
    )

    flow_complete = "[FLOW_COMPLETE]" in raw
    reply = raw.replace("[FLOW_COMPLETE]", "").strip()
    return reply, flow_complete


async def _call_claude_extract(prompt: str, user_id: str) -> str:
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(ANTHROPIC_API_URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise parse_claude_error(e, user_id=user_id)

    raw = data.get("content", [{}])[0].get("text", "")
    check_empty_reply(raw, user_id=user_id)
    return raw


async def extract_data_from_history(session: Session) -> CollectedData:
    """
    ใช้ Claude แยก structured data จาก conversation history
    คืน CollectedData object
    """
    history_text = "\n".join(
        f"{m.role.upper()}: {m.content}" for m in session.messages
    )

    prompt = f"""จาก conversation ด้านล่าง ให้แยกข้อมูลเป็น JSON ตาม schema ที่กำหนด
ถ้าไม่มีข้อมูล ให้ใส่ null
ตอบ JSON อย่างเดียว ไม่ต้องมีคำอธิบาย ไม่ต้องมี markdown

Schema:
{{
  "nickname": string,
  "age": int | null,
  "marital_status": "single"|"married"|"divorced"|"widowed"|null,
  "children": [{{"age": int}}],
  "monthly_income": int | null,
  "breadwinner": "self"|"partner"|"both"|null,
  "total_debt": int | null,
  "liquid_savings": int | null,
  "runway_months": int | null,
  "has_life_insurance": bool | null,
  "insurance_coverage": int | null,
  "has_will": bool | null,
  "has_poa": bool | null,
  "guardian_arranged": bool | null,
  "documents_accessible": bool | null,
  "family_discussion": bool | null,
  "worry_score": int | null,
  "email": string
}}

Conversation:
{history_text}"""

    raw = await with_retry(
        _call_claude_extract,
        config=DEFAULT_RETRY,
        label="extract_data",
        prompt=prompt,
        user_id=session.user_id,
    )

    # Strip markdown fences
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log_warn(f"JSON parse failed on extract: {e}", user_id=session.user_id)
        raise RickError(
            code=ErrorCode.CLAUDE_PARSE_ERROR,
            message=f"Failed to parse extracted data: {e}",
            user_id=session.user_id,
            retryable=True,
            original=e,
        )

    valid_fields = CollectedData.__dataclass_fields__
    cd = CollectedData(**{k: v for k, v in parsed.items() if k in valid_fields})
    return cd


async def _call_claude_report(prompt: str, user_id: str) -> str:
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS_REPORT,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(ANTHROPIC_API_URL, headers=HEADERS, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise parse_claude_error(e, user_id=user_id)

    raw = data.get("content", [{}])[0].get("text", "")
    check_empty_reply(raw, user_id=user_id)

    usage = data.get("usage", {})
    log_info(
        "Report generated",
        user_id=user_id,
        output_tokens=usage.get("output_tokens"),
    )
    return raw


async def generate_report(data: CollectedData, nickname: str, user_id: str = None) -> str:
    """
    สร้าง report content (Markdown) จาก CollectedData
    Retries up to 2x (report แพงกว่า ใช้ REPORT_RETRY)
    """
    data_json = json.dumps({
        "nickname": data.nickname or nickname,
        "age": data.age,
        "marital_status": data.marital_status,
        "children": data.children,
        "monthly_income": data.monthly_income,
        "breadwinner": data.breadwinner,
        "total_debt": data.total_debt,
        "liquid_savings": data.liquid_savings,
        "runway_months": data.runway_months,
        "has_life_insurance": data.has_life_insurance,
        "insurance_coverage": data.insurance_coverage,
        "has_will": data.has_will,
        "has_poa": data.has_poa,
        "guardian_arranged": data.guardian_arranged,
        "documents_accessible": data.documents_accessible,
        "family_discussion": data.family_discussion,
        "worry_score": data.worry_score,
    }, ensure_ascii=False, indent=2)

    prompt = f"""คุณเป็นผู้เขียนรายงานประเมินความพร้อมการคุ้มครองครอบครัว

## กฎสำคัญ
- สะท้อนสถานการณ์ตามข้อมูล — ห้ามแนะนำวิธีแก้ปัญหา
- คะแนนต้องสะท้อนความจริง ห้ามปลอบใจ
- ภาษากระชับ อ่านง่าย ไม่ใช้ศัพท์วิชาการ
- Format: Markdown สวยงาม พร้อม print เป็น A4

## ข้อมูลผู้ใช้
{data_json}

## โครงสร้างรายงาน
สร้างรายงานในรูปแบบ Markdown ดังนี้:

# รายงานประเมินความพร้อมการคุ้มครองครอบครัว
**สำหรับคุณ [ชื่อเล่น]** | วันที่: [วันนี้]

---

## สรุปภาพรวม
[2-3 ประโยค สะท้อนสถานการณ์รวม ไม่มีคำแนะนำ]

**Overall Score: X.X / 10**
**พบช่องโหว่ X จุดที่ควรรับทราบ**

---

## หมวดที่ 1: ความพร้อมด้านสภาพคล่อง — X/10
[paragraph 3-4 ประโยค สะท้อน runway เงินสด หนี้สิน ไม่บอกวิธีแก้]

## หมวดที่ 2: ความคุ้มครองชีวิต — X/10
[paragraph 3-4 ประโยค สะท้อนสถานะประกัน ทุนเทียบหนี้/รายได้ ไม่บอกวิธีแก้]

## หมวดที่ 3: การจัดการมรดก — X/10
[paragraph 3-4 ประโยค สะท้อนพินัยกรรม POA Living Will ไม่บอกวิธีแก้]

## หมวดที่ 4: การดูแลคนที่รัก — X/10
[paragraph 3-4 ประโยค สะท้อน guardian ลูก แผนฉุกเฉิน ไม่บอกวิธีแก้]

## หมวดที่ 5: ความพร้อมเอกสาร — X/10
[paragraph 3-4 ประโยค สะท้อนว่าเอกสารเข้าถึงได้ไหม ครอบครัวรู้ไหม ไม่บอกวิธีแก้]

---

## ขั้นตอนถัดไป
รายงานนี้เป็นเพียงภาพรวมเบื้องต้น ช่องโหว่ที่พบทั้ง **X จุด** ต้องการแผนที่ออกแบบเฉพาะสำหรับครอบครัวของคุณ

**คุณพยัต** นักวางแผนการเงินและที่ปรึกษากฎหมาย พร้อมนั่งคุยกับคุณแบบส่วนตัว เพื่อวางแผนคุ้มครองครอบครัวที่ครอบคลุมทุกด้าน

📞 **นัดคุยฟรี 30 นาที** → [LINE: @payat] หรือตอบกลับอีเมลนี้
💼 **บริการวางแผนคุ้มครองครอบครัวเต็มรูปแบบ** เริ่มต้น 1,990 บาท

---
*รายงานนี้จัดทำโดย AI ภายใต้การดูแลของคุณพยัต | ข้อมูลทั้งหมดเป็นความลับ*"""

    return await with_retry(
        _call_claude_report,
        config=REPORT_RETRY,
        label="generate_report",
        prompt=prompt,
        user_id=user_id,
    )
