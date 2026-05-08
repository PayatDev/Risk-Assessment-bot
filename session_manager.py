"""
session_manager.py — น้องริค Session State Machine
States: active → complete → (dead / no more replies)
Token budget: protects against runaway costs
"""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum


class SessionStatus(str, Enum):
    ACTIVE = "active"        # กำลังคุยอยู่
    COMPLETE = "complete"    # เก็บข้อมูลครบ + gen report แล้ว
    TIMEOUT = "timeout"      # เกิน token budget / turn limit


# ─── Token Budget Constants ──────────────────────────────────────────────────
MAX_TURNS = 25                  # hard cap (ยืดหยุ่นกว่า 15 เพราะสนทนาธรรมชาติ)
MAX_INPUT_TOKENS_SESSION = 20_000   # รวมทุก turn ต่อ session
MAX_OUTPUT_TOKENS_CHAT = 600        # per Claude reply (chat mode)
MAX_OUTPUT_TOKENS_REPORT = 3_000    # สำหรับ gen รายงาน
WARN_TURNS = 22                 # เริ่ม steer ให้จบถ้าใกล้ limit


@dataclass
class Message:
    role: str       # "user" | "assistant"
    content: str
    tokens_est: int = 0     # estimated tokens (len/4 approximation)


@dataclass
class CollectedData:
    """ข้อมูลที่เก็บได้จาก flow"""
    nickname: str = ""
    age: Optional[int] = None
    marital_status: str = ""
    children: list = field(default_factory=list)     # [{"age": 2}]
    monthly_income: Optional[int] = None
    breadwinner: str = ""                            # "self" | "partner" | "both"
    total_debt: Optional[int] = None
    liquid_savings: Optional[int] = None
    runway_months: Optional[int] = None
    has_life_insurance: Optional[bool] = None
    insurance_coverage: Optional[int] = None
    has_will: Optional[bool] = None
    has_poa: Optional[bool] = None
    guardian_arranged: Optional[bool] = None
    documents_accessible: Optional[bool] = None
    family_discussion: Optional[bool] = None
    worry_score: Optional[int] = None               # 1–5
    email: str = ""


@dataclass
class Session:
    user_id: str                                     # LINE userId
    status: SessionStatus = SessionStatus.ACTIVE
    messages: list = field(default_factory=list)     # List[Message]
    data: CollectedData = field(default_factory=CollectedData)
    turn_count: int = 0
    total_input_tokens: int = 0
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    output_path: Optional[str] = None               # path ของ output folder

    def is_dead(self) -> bool:
        """Bot หยุดตอบหลัง complete หรือ timeout"""
        return self.status in (SessionStatus.COMPLETE, SessionStatus.TIMEOUT)

    def budget_remaining(self) -> int:
        return MAX_INPUT_TOKENS_SESSION - self.total_input_tokens

    def should_steer_to_close(self) -> bool:
        return self.turn_count >= WARN_TURNS

    def should_force_close(self) -> bool:
        return (
            self.turn_count >= MAX_TURNS
            or self.total_input_tokens >= MAX_INPUT_TOKENS_SESSION
        )

    def add_message(self, role: str, content: str):
        est = len(content) // 4
        self.messages.append(Message(role=role, content=content, tokens_est=est))
        if role == "user":
            self.total_input_tokens += est
            self.turn_count += 1

    def to_history(self) -> list[dict]:
        """Format for Claude API messages array"""
        return [{"role": m.role, "content": m.content} for m in self.messages]

    def mark_complete(self, output_path: str):
        self.status = SessionStatus.COMPLETE
        self.completed_at = time.time()
        self.output_path = output_path

    def mark_timeout(self):
        self.status = SessionStatus.TIMEOUT
        self.completed_at = time.time()

    def to_json(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["data"] = asdict(self.data)
        return d


# ─── In-Memory Store (replace with Redis for production) ─────────────────────
_sessions: dict[str, Session] = {}


def get_or_create(user_id: str) -> Session:
    if user_id not in _sessions:
        _sessions[user_id] = Session(user_id=user_id)
    return _sessions[user_id]


def get(user_id: str) -> Optional[Session]:
    return _sessions.get(user_id)


def delete(user_id: str):
    _sessions.pop(user_id, None)
