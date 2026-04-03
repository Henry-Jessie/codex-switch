from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .accounts import AccountError, load_auth_file

ACCESS_PROFILE_KEY = "https://api.openai.com/profile"
ACCESS_AUTH_KEY = "https://api.openai.com/auth"


@dataclass(frozen=True)
class TokenInfo:
    email: str | None
    plan_type: str | None
    auth_mode: str | None
    account_id: str | None
    last_refresh: str | None
    access_exp: int | None
    id_exp: int | None
    access_expired: bool | None
    id_expired: bool | None
    refresh_token_present: bool


def _decode_jwt_payload(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        raw = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def summarize_auth_file(path: Path) -> TokenInfo:
    raw = load_auth_file(path)
    return summarize_auth_data(raw)


def summarize_auth_data(raw: dict[str, Any]) -> TokenInfo:
    tokens = raw.get("tokens") or {}
    if not isinstance(tokens, dict):
        raise AccountError("Auth JSON is missing a valid tokens object")

    access_payload = _decode_jwt_payload(tokens.get("access_token"))
    id_payload = _decode_jwt_payload(tokens.get("id_token"))

    access_profile = access_payload.get(ACCESS_PROFILE_KEY) or {}
    if not isinstance(access_profile, dict):
        access_profile = {}
    access_auth = access_payload.get(ACCESS_AUTH_KEY) or {}
    if not isinstance(access_auth, dict):
        access_auth = {}

    access_exp = _as_int(access_payload.get("exp"))
    id_exp = _as_int(id_payload.get("exp"))
    now = int(time.time())

    return TokenInfo(
        email=_first_str(
            id_payload.get("email"),
            access_profile.get("email"),
        ),
        plan_type=_first_str(access_auth.get("chatgpt_plan_type")),
        auth_mode=_first_str(raw.get("auth_mode")),
        account_id=_first_str(tokens.get("account_id")),
        last_refresh=_first_str(raw.get("last_refresh")),
        access_exp=access_exp,
        id_exp=id_exp,
        access_expired=(access_exp <= now) if access_exp is not None else None,
        id_expired=(id_exp <= now) if id_exp is not None else None,
        refresh_token_present=bool(tokens.get("refresh_token")),
    )


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


def format_epoch(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
