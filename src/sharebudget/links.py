from __future__ import annotations

import hashlib
import hmac
import re


def group_start_param(chat_id: int, bot_token: str) -> str:
    """Create a compact signed Mini App context without exposing a trusted raw chat id."""
    value = f"n{abs(chat_id)}" if chat_id < 0 else f"p{chat_id}"
    signature = hmac.new(bot_token.encode(), value.encode(), hashlib.sha256).hexdigest()[:12]
    return f"chat_{value}_{signature}"


def parse_group_start_param(value: str, bot_token: str) -> int | None:
    match = re.fullmatch(r"chat_([np])(\d+)_([a-f0-9]{12})", value)
    if not match:
        return None
    sign, identifier, supplied_signature = match.groups()
    payload = f"{sign}{identifier}"
    expected_signature = hmac.new(bot_token.encode(), payload.encode(), hashlib.sha256).hexdigest()[
        :12
    ]
    if not hmac.compare_digest(expected_signature, supplied_signature):
        return None
    chat_id = int(identifier)
    return -chat_id if sign == "n" else chat_id
