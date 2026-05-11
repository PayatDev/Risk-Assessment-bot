"""
report_agent.py — Gen report .docx ด้วย python-docx
Auto-triggered หลัง save_chatlog สำเร็จ
"""

import os
import json
import asyncio
import tempfile
from datetime import datetime, timezone, timedelta

import httpx
from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

TZ_THAI = timezone(timedelta(hours=7))

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


# ─── Google Drive ─────────────────────────────────────────────────────────────
def _drive():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"]), scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def create_folder(nickname: str, date_str: str) -> str:
    svc = _drive()
    meta = {
        "name": f"{nickname}_{date_str}",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [DRIVE_FOLDER_ID],
    }
    f = svc.files().create(body=meta, fields="id").execute()
    print(f"[DRIVE] folder: {nickname}_{date_str}")
    return f["id"]


def upload_docx(path: str, filename: str, folder_id: str) -> str:
    svc = _drive()
    meta = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(
        path,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    f = svc.files().create(body=meta, media_body=media, fields="id").execute()
    print(f"[DRIVE] uploaded: {filename}")
    return f["id"]


# ─── Claude: Score ────────────────────────────────────────────────────────────
SCORE_PROMPT = """คุณเป็นผู้ประเมินความพร้อมการคุ้มครองครอบครัว ตอบ JSON เท่านั้น ไม่มี markdown

เกณฑ์คะแนน (ห้ามใช้ดุลยพินิจนอกเกณฑ์):

หมวด 1 สภาพคล่อง:
- runway ≥ 12 เดือน → 9-10
- runway 6-11 เดือน → 6-8
- runway 3-5 เดือน → 4-5
- runway < 3 เดือน → 2-3
- ไม่รู้/ไม่ระบุ → 1

หมวด 2 ความคุ้มครองชีวิต:
- มีประกัน + ทุน ≥ หนี้ + 5ปีรายได้ → 9-10
- มีประกัน + ทุน ≥ หนี้ → 6-8
- มีประกัน + ทุน < หนี้ → 3-5
- ไม่มีประกัน + breadwinner คนเดียว → 1-2
- ไม่มีประกันเลย → 1

หมวด 3 การจัดการมรดก:
- มีพินัยกรรม + POA + Living Will → 9-10
- มีพินัยกรรม + 1 ใน POA/LW → 6-8
- มีแค่พินัยกรรม → 4-5
- ไม่มีอะไร → 1-2

หมวด 4 การดูแลลูก:
- ตกลงแล้ว + แยก Person/Money Guardian → 9-10
- ตกลงแล้ว ไม่แยก → 5-7
- มีคนในใจ แต่ยังไม่ตกลง → 3-4
- ไม่มีแผน → 1-2
- ไม่มีลูก → null

หมวด 5 ความพร้อมเอกสาร:
- ครอบครัวรู้ + เคยคุย → 9-10
- ครอบครัวรู้ ยังไม่คุย → 5-7
- ไม่มีใครรู้ → 1-3

กฎ: ห้ามให้ > 5 ถ้าไม่มีเอกสาร, "ไม่ระบุ" = ไม่มี = คะแนนต่ำ

Theme (เลือก primary + secondary):
THEME_A: runway น้อย/ไม่มีประกัน/หนี้สูง
THEME_B: มีลูกเล็ก + guardian ไม่ชัด
THEME_C: ไม่มีพินัยกรรม + มีทรัพย์สิน
THEME_D: ไม่มี POA + breadwinner คนเดียว
THEME_E: ครอบครัวไม่รู้/ไม่เคยคุยแผน

ตอบ:
{"score_1":int,"score_2":int,"score_3":int,"score_4":int|null,"score_5":int,
"overall":float,"risk_level":"ต่ำ"|"ปานกลาง"|"สูง"|"สูงมาก",
"primary_theme":"THEME_X","secondary_theme":"THEME_X",
"gaps":["gap1","gap2",...]}"""


REPORT_PROMPT = """คุณเป็นนักเขียนสไตล์ storyselling สำหรับรายงานการเงิน

สไตล์: เพื่อนที่เป็นผู้เชี่ยวชาญ ไม่สั่งสอน ไม่ขู่
ฉายภาพให้เห็น ให้จินตนาการตาม

โครงสร้าง (เขียนตามลำดับ):
1. เปิดด้วยสถานการณ์หรือคำพูดจากการสนทนา (1-2 ประโยค)
2. ฉายภาพ PRIMARY_THEME ให้เห็น (2-3 ประโยค)
3. สิ่งที่ทำได้ดีแล้ว (1 ประโยค)
4. SECONDARY_THEME เบาๆ (1-2 ประโยค)
5. ประโยคปิด (1 ประโยค)

กฎ: ห้ามแนะนำวิธีแก้, ไม่เกิน 180 คำ, plain text ไม่มี markdown
ตอบเฉพาะเนื้อหา ไม่มีคำอธิบาย"""


async def _claude(system: str, user: str, max_tokens: int = 800) -> str:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


async def gen_scores(chatlog: dict) -> dict:
    msgs = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in chatlog.get("messages", []))
    raw = await _claude(SCORE_PROMPT, f"Chatlog:\n{msgs}")
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


async def gen_content(chatlog: dict, scores: dict) -> str:
    msgs = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in chatlog.get("messages", []))
    prompt = f"Chatlog:\n{msgs}\n\nScores:\n{json.dumps(scores, ensure_ascii=False)}\nPRIMARY: {scores.get('primary_theme')}\nSECONDARY: {scores.get('secondary_theme')}"
    return await _claude(REPORT_PROMPT, prompt, max_tokens=600)


# ─── Build .docx ──────────────────────────────────────────────────────────────
def _color(hex_str: str) -> RGBColor:
    r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
    return RGBColor(r, g, b)


ACCENT  = "1A3A5C"
DARK    = "1A1A2E"
MUTED   = "666688"
FAINT   = "AAAAAA"
RED     = "C0392B"
ORANGE  = "D35400"
YELLOW  = "B7950B"
GREEN   = "1E8449"


def risk_color(overall: float) -> str:
    if overall >= 8: return GREEN
    if overall >= 6: return YELLOW
    if overall >= 4: return ORANGE
    return RED


def risk_label(overall: float) -> str:
    if overall >= 8: return "ความเสี่ยงต่ำ"
    if overall >= 6: return "ความเสี่ยงปานกลาง"
    if overall >= 4: return "ความเสี่ยงสูง"
    return "ความเสี่ยงสูงมาก"


def score_color(s: int) -> str:
    if s >= 8: return GREEN
    if s >= 6: return YELLOW
    if s >= 4: return ORANGE
    return RED


def build_docx(nickname: str, date_th: str, content: str, scores: dict, gaps: list) -> str:
    doc = Document()

    # Page margins (A4)
    for section in doc.sections:
        section.page_height = Cm(29.7)
        section.page_width  = Cm(21.0)
        section.top_margin    = Cm(2.5)
        section.bottom_margin = Cm(2.5)
        section.left_margin   = Cm(3.0)
        section.right_margin  = Cm(3.0)

    def para(text, bold=False, size=11, color=DARK, align=WD_ALIGN_PARAGRAPH.LEFT,
             space_before=0, space_after=6, italic=False):
        p = doc.add_paragraph()
        p.alignment = align
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after  = Pt(space_after)
        run = p.add_run(text)
        run.bold   = bold
        run.italic = italic
        run.font.size  = Pt(size)
        run.font.color.rgb = _color(color)
        return p

    def label(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(14)
        p.paragraph_format.space_after  = Pt(4)
        run = p.add_run(text.upper())
        run.font.size  = Pt(8)
        run.font.color.rgb = _color(FAINT)
        run.font.bold  = True
        # เส้นบนบาง
        pf = p.paragraph_format
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        top = OxmlElement("w:top")
        top.set(qn("w:val"), "single")
        top.set(qn("w:sz"), "4")
        top.set(qn("w:space"), "1")
        top.set(qn("w:color"), "E8E6DF")
        pBdr.append(top)
        pPr.append(pBdr)

    overall = scores.get("overall", 0)

    # ─── Header ───────────────────────────────────────────────────────────────
    para("PAYAT FINANCIAL PLANNING", bold=True, size=9, color=ACCENT, space_after=2)
    para("รายงานประเมินความพร้อมการคุ้มครองครอบครัว", bold=True, size=16, color=DARK, space_after=2)
    para(f"สำหรับคุณ{nickname}  ·  {date_th}", size=10, color=FAINT, space_after=14)

    # ─── Narrative ────────────────────────────────────────────────────────────
    label("บทวิเคราะห์")
    for line in content.split("\n"):
        if line.strip():
            para(line.strip(), size=11, color=MUTED, space_after=6)

    # ─── Score ────────────────────────────────────────────────────────────────
    label("คะแนนรวม")
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(2)
    r1 = p.add_run(f"{overall:.1f}")
    r1.bold = True
    r1.font.size = Pt(36)
    r1.font.color.rgb = _color(risk_color(overall))
    r2 = p.add_run(" / 10")
    r2.font.size = Pt(14)
    r2.font.color.rgb = _color(FAINT)

    para(risk_label(overall), bold=True, size=11, color=risk_color(overall), space_after=10)

    # ─── Score breakdown ──────────────────────────────────────────────────────
    label("คะแนนรายหมวด")
    rows = [
        ("สภาพคล่องฉุกเฉิน", scores.get("score_1")),
        ("ความคุ้มครองชีวิต", scores.get("score_2")),
        ("การจัดการมรดก",     scores.get("score_3")),
        ("การดูแลลูก",        scores.get("score_4")),
        ("ความพร้อมเอกสาร",  scores.get("score_5")),
    ]
    for lbl, s in rows:
        if s is None:
            continue
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after  = Pt(2)
        r1 = p.add_run(f"{lbl:<18}")
        r1.font.size = Pt(10)
        r1.font.color.rgb = _color(MUTED)
        r2 = p.add_run(f"  {s}/10")
        r2.bold = True
        r2.font.size = Pt(10)
        r2.font.color.rgb = _color(score_color(s))

    # ─── Gaps ─────────────────────────────────────────────────────────────────
    label(f"พบช่องโหว่ {len(gaps)} จุดที่ควรรับทราบ")
    for g in gaps:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after  = Pt(1)
        p.paragraph_format.left_indent  = Pt(12)
        r = p.add_run(f"—  {g}")
        r.font.size = Pt(10)
        r.font.color.rgb = _color(MUTED)

    # ─── CTA ──────────────────────────────────────────────────────────────────
    label("ขั้นตอนถัดไป")
    para(
        "รายงานนี้เป็นภาพรวมเบื้องต้น ช่องโหว่ที่พบต้องการแผนที่ออกแบบเฉพาะสำหรับครอบครัวของคุณ "
        "คุณพยัตพร้อมนั่งคุยโดยตรงเพื่อวางแผนที่ครอบคลุมทุกด้าน",
        size=10, color=MUTED, space_after=8
    )
    para("คุณพยัต จิรสุวรรณพงศ์", bold=True, size=11, color=ACCENT, space_after=2)
    para("นักวางแผนการเงิน · ที่ปรึกษากฎหมาย", size=9, color=FAINT, space_after=2)
    para("นัดคุยฟรี 30 นาที  ·  LINE: @payat  ·  payat.jira@gmail.com", size=9, color=ACCENT, space_after=2)
    para("บริการวางแผนคุ้มครองครอบครัวเต็มรูปแบบ เริ่มต้น 1,990 บาท", size=9, color=FAINT, space_after=10)

    # Footer note
    para(
        "รายงานนี้จัดทำโดย AI ภายใต้การดูแลของคุณพยัต · ข้อมูลทั้งหมดเป็นความลับ · ไม่ใช่คำแนะนำทางกฎหมายหรือการเงิน",
        size=8, color=FAINT, align=WD_ALIGN_PARAGRAPH.CENTER, space_before=20
    )

    # Save
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    doc.save(tmp.name)
    return tmp.name


# ─── Main Pipeline ────────────────────────────────────────────────────────────
async def run(chatlog: dict):
    nickname = chatlog.get("nickname") or "ลูกค้า"
    date_str = datetime.now(TZ_THAI).strftime("%Y%m%d")
    date_th  = datetime.now(TZ_THAI).strftime("%-d/%m/%Y")
    filename = f"{nickname}_{date_str}.docx"

    print(f"[AGENT] Starting: {nickname}")

    scores  = await gen_scores(chatlog)
    print(f"[AGENT] Overall={scores.get('overall')} Theme={scores.get('primary_theme')}")

    content = await gen_content(chatlog, scores)
    gaps    = scores.get("gaps", [])

    docx_path = build_docx(nickname, date_th, content, scores, gaps)

    try:
        folder_id = create_folder(nickname, date_str)
        file_id   = upload_docx(docx_path, filename, folder_id)
        print(f"[AGENT] Done → {filename} ({file_id})")
        return file_id
    finally:
        import os as _os
        if _os.path.exists(docx_path):
            _os.unlink(docx_path)
