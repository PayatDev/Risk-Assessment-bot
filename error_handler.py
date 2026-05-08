"""
error_handler.py — น้องริค Error & Retry System

Covers:
  1. Claude API errors (timeout, 429, 5xx, empty response)
  2. LINE reply failures
  3. Output generation failures
  4. Session corruption
  5. Structured logging ทุก error
"""

import time
import asyncio
import logging
import traceback
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable, Any

import httpx

# ─── Logger ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nong_rick")


# ─── Error Types ─────────────────────────────────────────────────────────────
class ErrorCode(str, Enum):
    # Claude API
    CLAUDE_TIMEOUT       = "CLAUDE_TIMEOUT"
    CLAUDE_RATE_LIMIT    = "CLAUDE_RATE_LIMIT"
    CLAUDE_SERVER_ERROR  = "CLAUDE_SERVER_ERROR"
    CLAUDE_EMPTY_REPLY   = "CLAUDE_EMPTY_REPLY"
    CLAUDE_PARSE_ERROR   = "CLAUDE_PARSE_ERROR"
    CLAUDE_AUTH_ERROR    = "CLAUDE_AUTH_ERROR"

    # LINE
    LINE_REPLY_FAILED    = "LINE_REPLY_FAILED"
    LINE_INVALID_TOKEN   = "LINE_INVALID_TOKEN"

    # Output generation
    OUTPUT_PDF_FAILED    = "OUTPUT_PDF_FAILED"
    OUTPUT_EMAIL_FAILED  = "OUTPUT_EMAIL_FAILED"
    OUTPUT_SAVE_FAILED   = "OUTPUT_SAVE_FAILED"

    # Session
    SESSION_CORRUPT      = "SESSION_CORRUPT"

    # Generic
    UNKNOWN              = "UNKNOWN"


@dataclass
class RickError(Exception):
    code: ErrorCode
    message: str
    user_id: Optional[str] = None
    retryable: bool = True
    original: Optional[Exception] = None

    def __str__(self):
        return f"[{self.code}] {self.message}"


# ─── User-facing messages (ภาษาไทย) ─────────────────────────────────────────
USER_MESSAGES = {
    ErrorCode.CLAUDE_TIMEOUT:      "ขอโทษครับ ระบบช้าหน่อย รอสักครู่แล้วลองใหม่ได้เลยนะครับ 🙏",
    ErrorCode.CLAUDE_RATE_LIMIT:   "ขอโทษครับ ตอนนี้มีคนใช้เยอะ รอสัก 1-2 นาทีแล้วลองส่งใหม่นะครับ ⏳",
    ErrorCode.CLAUDE_SERVER_ERROR: "ขอโทษครับ ระบบมีปัญหาชั่วคราว กรุณาลองใหม่อีกครั้งครับ 🔧",
    ErrorCode.CLAUDE_EMPTY_REPLY:  "ขอโทษครับ ได้รับข้อความไม่สมบูรณ์ ช่วยส่งใหม่อีกครั้งได้ไหมครับ?",
    ErrorCode.CLAUDE_AUTH_ERROR:   "ขอโทษครับ มีปัญหาทางเทคนิค กรุณาติดต่อทีมงานครับ",
    ErrorCode.LINE_REPLY_FAILED:   None,   # ไม่ตอบ (token หมดอายุแล้ว)
    ErrorCode.UNKNOWN:             "ขอโทษครับ มีข้อผิดพลาดเกิดขึ้น กรุณาลองใหม่อีกครั้งครับ 🙏",
}

# ─── Retry Config ─────────────────────────────────────────────────────────────
@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay: float = 1.0      # seconds
    max_delay: float = 10.0
    backoff_factor: float = 2.0  # exponential backoff


DEFAULT_RETRY = RetryConfig(max_attempts=3, base_delay=1.0)
REPORT_RETRY  = RetryConfig(max_attempts=2, base_delay=2.0)  # report gen: น้อยกว่า (แพงกว่า)


# ─── Retry Decorator ──────────────────────────────────────────────────────────
async def with_retry(
    func: Callable,
    config: RetryConfig = DEFAULT_RETRY,
    retryable_codes: set = None,
    label: str = "operation",
    **kwargs,
) -> Any:
    """
    Retry async function with exponential backoff.
    Re-raises RickError ถ้าหมด attempts หรือ error ไม่ retryable
    """
    if retryable_codes is None:
        retryable_codes = {
            ErrorCode.CLAUDE_TIMEOUT,
            ErrorCode.CLAUDE_RATE_LIMIT,
            ErrorCode.CLAUDE_SERVER_ERROR,
            ErrorCode.CLAUDE_EMPTY_REPLY,
        }

    last_error = None
    for attempt in range(1, config.max_attempts + 1):
        try:
            return await func(**kwargs)
        except RickError as e:
            last_error = e
            if not e.retryable or e.code not in retryable_codes:
                log.warning(f"[{label}] non-retryable error: {e}")
                raise
            delay = min(
                config.base_delay * (config.backoff_factor ** (attempt - 1)),
                config.max_delay,
            )
            log.warning(
                f"[{label}] attempt {attempt}/{config.max_attempts} failed: {e} "
                f"→ retry in {delay:.1f}s"
            )
            if attempt < config.max_attempts:
                await asyncio.sleep(delay)
        except Exception as e:
            # Unexpected error — wrap and re-raise
            err = RickError(
                code=ErrorCode.UNKNOWN,
                message=str(e),
                retryable=False,
                original=e,
            )
            log.error(f"[{label}] unexpected error: {traceback.format_exc()}")
            raise err

    log.error(f"[{label}] all {config.max_attempts} attempts failed")
    raise last_error


# ─── Claude API Error Parser ──────────────────────────────────────────────────
def parse_claude_error(e: Exception, user_id: str = None) -> RickError:
    """แปลง httpx / API error เป็น RickError"""

    if isinstance(e, httpx.TimeoutException):
        return RickError(
            code=ErrorCode.CLAUDE_TIMEOUT,
            message=f"Claude API timeout: {e}",
            user_id=user_id,
            retryable=True,
            original=e,
        )

    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        body = ""
        try:
            full = e.response.json()
            log.error(f"Claude API full error response: {full}")
            body = full.get("error", {}).get("message", "")
        except Exception:
            body = e.response.text
            log.error(f"Claude API raw error body: {body}")

        if status == 401:
            return RickError(
                code=ErrorCode.CLAUDE_AUTH_ERROR,
                message=f"Auth failed: {body}",
                user_id=user_id,
                retryable=False,
                original=e,
            )
        if status == 429:
            return RickError(
                code=ErrorCode.CLAUDE_RATE_LIMIT,
                message=f"Rate limited: {body}",
                user_id=user_id,
                retryable=True,
                original=e,
            )
        if status >= 500:
            return RickError(
                code=ErrorCode.CLAUDE_SERVER_ERROR,
                message=f"Server error {status}: {body}",
                user_id=user_id,
                retryable=True,
                original=e,
            )

    if isinstance(e, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return RickError(
            code=ErrorCode.CLAUDE_SERVER_ERROR,
            message=f"Connection error: {e}",
            user_id=user_id,
            retryable=True,
            original=e,
        )

    return RickError(
        code=ErrorCode.UNKNOWN,
        message=str(e),
        user_id=user_id,
        retryable=False,
        original=e,
    )


def check_empty_reply(text: str, user_id: str = None) -> None:
    """Raise ถ้า Claude ส่ง empty หรือ whitespace เท่านั้น"""
    if not text or not text.strip():
        raise RickError(
            code=ErrorCode.CLAUDE_EMPTY_REPLY,
            message="Claude returned empty response",
            user_id=user_id,
            retryable=True,
        )


# ─── LINE Error Parser ────────────────────────────────────────────────────────
def parse_line_error(e: Exception, user_id: str = None) -> RickError:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status in (400, 401):
            # reply token หมดอายุ — ไม่ retryable
            return RickError(
                code=ErrorCode.LINE_INVALID_TOKEN,
                message=f"LINE reply token expired/invalid (status {status})",
                user_id=user_id,
                retryable=False,
                original=e,
            )
    return RickError(
        code=ErrorCode.LINE_REPLY_FAILED,
        message=str(e),
        user_id=user_id,
        retryable=True,
        original=e,
    )


# ─── Structured Log Helper ────────────────────────────────────────────────────
def log_error(err: RickError, context: dict = None):
    ctx = context or {}
    log.error(
        f"ERROR | code={err.code} | user={err.user_id} | "
        f"retryable={err.retryable} | msg={err.message} | "
        f"context={ctx}"
    )
    if err.original:
        log.debug(f"  original: {traceback.format_exception(type(err.original), err.original, err.original.__traceback__)}")


def log_info(msg: str, user_id: str = None, **kwargs):
    extras = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    log.info(f"INFO | user={user_id} | {msg}" + (f" | {extras}" if extras else ""))


def log_warn(msg: str, user_id: str = None, **kwargs):
    extras = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    log.warning(f"WARN | user={user_id} | {msg}" + (f" | {extras}" if extras else ""))
