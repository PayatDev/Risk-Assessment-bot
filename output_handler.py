"""
output_handler.py — บันทึก chatlog + สร้าง PDF report
Folder structure: outputs/{user_id}_{timestamp}/
  ├── chatlog.json
  └── risk_report.pdf
"""

import os
import json
import time
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

from session_manager import Session, CollectedData
from claude_client import extract_data_from_history, generate_report

# Thai timezone (UTC+7)
TZ_THAI = timezone(timedelta(hours=7))

OUTPUT_BASE = Path(os.environ.get("OUTPUT_DIR", "./outputs"))
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


def _thai_now() -> str:
    return datetime.now(TZ_THAI).strftime("%d/%m/%Y %H:%M น.")


def _session_folder(user_id: str) -> Path:
    ts = datetime.now(TZ_THAI).strftime("%Y%m%d_%H%M%S")
    safe_id = user_id.replace(":", "_")[:20]
    folder = OUTPUT_BASE / f"{safe_id}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_chatlog(session: Session, folder: Path) -> Path:
    """บันทึก chatlog เป็น JSON"""
    log = {
        "user_id": session.user_id,
        "status": session.status.value,
        "created_at": datetime.fromtimestamp(session.created_at, TZ_THAI).isoformat(),
        "completed_at": datetime.fromtimestamp(session.completed_at, TZ_THAI).isoformat()
            if session.completed_at else None,
        "turn_count": session.turn_count,
        "total_input_tokens_est": session.total_input_tokens,
        "data": {
            "nickname": session.data.nickname,
            "age": session.data.age,
            "marital_status": session.data.marital_status,
            "children": session.data.children,
            "monthly_income": session.data.monthly_income,
            "breadwinner": session.data.breadwinner,
            "total_debt": session.data.total_debt,
            "liquid_savings": session.data.liquid_savings,
            "runway_months": session.data.runway_months,
            "has_life_insurance": session.data.has_life_insurance,
            "insurance_coverage": session.data.insurance_coverage,
            "has_will": session.data.has_will,
            "has_poa": session.data.has_poa,
            "guardian_arranged": session.data.guardian_arranged,
            "documents_accessible": session.data.documents_accessible,
            "family_discussion": session.data.family_discussion,
            "worry_score": session.data.worry_score,
            "email": session.data.email,
        },
        "messages": [
            {"role": m.role, "content": m.content}
            for m in session.messages
        ],
    }
    path = folder / "chatlog.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OUTPUT] Chatlog saved: {path}")
    return path


def markdown_to_pdf(md_content: str, output_path: Path, nickname: str) -> Path:
    """
    แปลง Markdown → PDF ด้วย weasyprint
    ถ้า weasyprint ไม่มี → fallback เป็น .txt
    """
    try:
        import markdown
        from weasyprint import HTML, CSS

        html_body = markdown.markdown(md_content, extensions=["tables", "nl2br"])
        html_full = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Sarabun:wght@400;600;700&display=swap');
  @page {{ margin: 2cm; size: A4; }}
  body {{
    font-family: 'Sarabun', 'Noto Sans Thai', sans-serif;
    font-size: 14px;
    line-height: 1.8;
    color: #1a1a1a;
  }}
  h1 {{ font-size: 20px; color: #1a3a5c; border-bottom: 2px solid #1a3a5c; padding-bottom: 8px; }}
  h2 {{ font-size: 15px; color: #1a3a5c; margin-top: 20px; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 16px 0; }}
  strong {{ color: #1a3a5c; }}
  p {{ margin: 8px 0; }}
  em {{ color: #666; font-size: 12px; }}
</style>
</head>
<body>{html_body}</body>
</html>"""

        HTML(string=html_full).write_pdf(str(output_path))
        print(f"[OUTPUT] PDF saved: {output_path}")
        return output_path

    except ImportError:
        # Fallback: save as markdown file
        txt_path = output_path.with_suffix(".md")
        txt_path.write_text(md_content, encoding="utf-8")
        print(f"[OUTPUT] weasyprint not found — saved as markdown: {txt_path}")
        return txt_path


async def process_and_save(session: Session) -> tuple[Path, Path]:
    """
    Main pipeline:
    1. Extract structured data from conversation
    2. Generate report markdown
    3. Save chatlog.json
    4. Save risk_report.pdf
    Returns (chatlog_path, report_path)
    """
    folder = _session_folder(session.user_id)

    # Step 1: Extract data
    print(f"[OUTPUT] Extracting data for {session.user_id}...")
    session.data = await extract_data_from_history(session)

    # Step 2: Save to Google Sheets
    from sheets_handler import save_session
    saved = save_session(session)
    print(f"[OUTPUT] Sheets save: {'ok' if saved else 'failed'}")

    # Step 3: Save chatlog
    chatlog_path = save_chatlog(session, folder)

    # Step 3: Generate report content
    print(f"[OUTPUT] Generating report...")
    report_md = await generate_report(session.data, session.data.nickname)

    # Step 4: Save PDF
    pdf_path = folder / "risk_report.pdf"
    report_path = markdown_to_pdf(report_md, pdf_path, session.data.nickname)

    print(f"[OUTPUT] Done → {folder}")
    return chatlog_path, report_path


def get_output_summary(folder: Path) -> dict:
    """คืน paths สำหรับ email/notification"""
    return {
        "folder": str(folder),
        "chatlog": str(folder / "chatlog.json"),
        "report": str(folder / "risk_report.pdf"),
    }
