"""
sheets_handler.py — บันทึกข้อมูลลง Google Sheets
"""

import os
import json
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

TZ_THAI = timezone(timedelta(hours=7))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SHEET_NAME = "RiskAssessment"  # ชื่อ tab ใน sheet


def _get_service():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def _ensure_header(service):
    """สร้าง header row ถ้ายังไม่มี"""
    headers = [
        "timestamp", "user_id", "nickname", "age", "marital_status",
        "children", "monthly_income", "breadwinner", "total_debt",
        "liquid_savings", "runway_months", "has_life_insurance",
        "insurance_coverage", "has_will", "has_poa", "guardian_arranged",
        "documents_accessible", "family_discussion", "worry_score", "email",
        "status", "turn_count",
    ]

    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_NAME}!A1:V1"
    ).execute()

    if not result.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()


def save_session(session) -> bool:
    """บันทึก session ลง Google Sheets — return True ถ้าสำเร็จ"""
    try:
        service = _get_service()
        _ensure_header(service)

        d = session.data
        now = datetime.now(TZ_THAI).strftime("%d/%m/%Y %H:%M")

        row = [
            now,
            session.user_id,
            d.nickname or "",
            d.age or "",
            d.marital_status or "",
            json.dumps(d.children, ensure_ascii=False) if d.children else "",
            d.monthly_income or "",
            d.breadwinner or "",
            d.total_debt or "",
            d.liquid_savings or "",
            d.runway_months or "",
            str(d.has_life_insurance) if d.has_life_insurance is not None else "",
            d.insurance_coverage or "",
            str(d.has_will) if d.has_will is not None else "",
            str(d.has_poa) if d.has_poa is not None else "",
            str(d.guardian_arranged) if d.guardian_arranged is not None else "",
            str(d.documents_accessible) if d.documents_accessible is not None else "",
            str(d.family_discussion) if d.family_discussion is not None else "",
            d.worry_score or "",
            d.email or "",
            session.status.value,
            session.turn_count,
        ]

        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        return True

    except Exception as e:
        print(f"[SHEETS ERROR] {e}")
        return False


def test_connection() -> dict:
    """ทดสอบการเชื่อมต่อ — ใช้กับ /test-sheets endpoint"""
    try:
        service = _get_service()
        _ensure_header(service)

        # ลองเขียน test row
        now = datetime.now(TZ_THAI).strftime("%d/%m/%Y %H:%M")
        test_row = [now, "TEST", "ทดสอบ", "", "", "", "", "", "", "",
                    "", "", "", "", "", "", "", "", "", "", "test", 0]

        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [test_row]},
        ).execute()

        return {"status": "ok", "message": "เชื่อมต่อ Google Sheets สำเร็จ ✅"}

    except Exception as e:
        return {"status": "error", "message": str(e)}
