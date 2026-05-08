"""
sheets_handler.py — บันทึก chatlog JSON ลง Google Sheets คอลัมน์เดียว
Schema: timestamp | user_id | nickname | email | turn_count | status | chatlog_json
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
SHEET_NAME = "RiskAssessment"
HEADERS = ["timestamp", "user_id", "nickname", "email", "turn_count", "status", "chatlog_json"]


def _get_service():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def _ensure_header(service):
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A1:G1"
    ).execute()
    if not result.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW", body={"values": [HEADERS]},
        ).execute()


def save_chatlog(session) -> bool:
    """บันทึก chatlog ทั้งก้อนเป็น JSON string — Return True ถ้าสำเร็จ"""
    try:
        service = _get_service()
        _ensure_header(service)

        chatlog = {
            "user_id": session.user_id,
            "turn_count": session.turn_count,
            "messages": [{"role": m.role, "content": m.content} for m in session.messages],
        }

        now = datetime.now(TZ_THAI).strftime("%d/%m/%Y %H:%M")
        row = [
            now,
            session.user_id,
            session.data.nickname or "",
            session.data.email or "",
            session.turn_count,
            session.status.value,
            json.dumps(chatlog, ensure_ascii=False),
        ]

        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        print(f"[SHEETS] Saved chatlog for {session.user_id}")
        return True

    except Exception as e:
        print(f"[SHEETS ERROR] {e}")
        return False


def test_connection() -> dict:
    try:
        service = _get_service()
        _ensure_header(service)
        now = datetime.now(TZ_THAI).strftime("%d/%m/%Y %H:%M")
        test_row = [now, "TEST", "ทดสอบ", "test@test.com", 0, "test",
                    json.dumps({"messages": []}, ensure_ascii=False)]
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [test_row]},
        ).execute()
        return {"status": "ok", "message": "เชื่อมต่อ Google Sheets สำเร็จ ✅"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
