import base64
import hashlib
import hmac
import time
from typing import Any, Optional


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(token: str) -> bytes:
    padding = "=" * ((4 - len(token) % 4) % 4)
    return base64.urlsafe_b64decode((token + padding).encode("utf-8"))


def generate_risk_artifact_token(
    event_id: int,
    kind: str,
    secret: str,
    ttl_seconds: int = 600,
    now_ts: Optional[int] = None,
) -> tuple[str, int]:
    now = int(now_ts or time.time())
    expires_at = now + max(60, int(ttl_seconds))
    payload = f"v1|{int(event_id)}|{kind}|{expires_at}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    token = _b64url_encode(f"{payload}|{sig}".encode("utf-8"))
    return token, expires_at


def verify_risk_artifact_token(
    token: str,
    secret: str,
    event_id: int,
    kind: str,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    try:
        raw = _b64url_decode(token).decode("utf-8")
        parts = raw.split("|")
        if len(parts) != 5:
            return {"ok": False, "reason": "malformed"}
        version, token_event_id, token_kind, exp, sig = parts
        if version != "v1":
            return {"ok": False, "reason": "version"}
        if int(token_event_id) != int(event_id):
            return {"ok": False, "reason": "event_id"}
        if token_kind != kind:
            return {"ok": False, "reason": "kind"}
        exp_int = int(exp)
        now = int(now_ts or time.time())
        if exp_int < now:
            return {"ok": False, "reason": "expired"}
        payload = f"{version}|{token_event_id}|{token_kind}|{exp_int}"
        expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, sig):
            return {"ok": False, "reason": "signature"}
        return {"ok": True, "expires_at": exp_int}
    except Exception:
        return {"ok": False, "reason": "invalid"}
