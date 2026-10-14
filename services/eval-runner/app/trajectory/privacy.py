"""pii-v1 的保守内容检查；字段 allowlist 仍是主边界，不声称识别任意 PII。"""

import re
from datetime import date
from decimal import Decimal

CONTENT_POLICY_VERSION = "pii-v1"
_CARD = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
_SSN = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")
_IDENTITY = re.compile(r"(?<![0-9])[0-9]{17}[0-9Xx](?![0-9])")
_CREDENTIAL = re.compile(r"\b(?:bearer\s+\S+|authorization\s*[:=]|(?:set-)?cookie\s*[:=])", re.IGNORECASE)
_STRING_KINDS = {"identifier": re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,255}"), "currency": re.compile(r"[A-Z]{3}")}


def strings_allowed(value, kind):
    """固定语义，不接受来源自由文本或调用方提供的 regex。"""
    if type(value) is list:
        return all(strings_allowed(item, kind) for item in value)
    if type(value) is not str:
        return True
    pattern = _STRING_KINDS.get(kind)
    return pattern is not None and pattern.fullmatch(value) is not None


def sensitive_key(name: str) -> bool:
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", split)
    parts = set(re.split(r"[^a-z0-9]+", split.casefold()))
    compact = re.sub(r"[^a-z0-9]", "", name.casefold())
    return bool(any(part.startswith("ssn") for part in parts) or "socialsecurity" in compact
        or parts & {"authorization", "cookie", "cookies", "headers", "password", "secret", "token",
        "credential", "credentials", "pan", "ssn", "passport", "note", "notes", "message", "conversation"}
        or compact.endswith(("apikey", "privatekey", "secretkey", "cardnumber", "creditcard", "debitcard",
            "ssnnumber", "socialsecuritynumber", "socialsecurityno", "socialsecurity", "idcard", "identitynumber", "identitycard", "nationalid", "idnumber", "freetext")))


def _luhn(digits):
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value = int(digit) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return len(set(digits)) > 1 and total % 10 == 0


def sensitive_value(value) -> bool:
    if type(value) is list:
        return any(sensitive_value(item) for item in value)
    if type(value) is int:
        text = str(abs(value))
    elif type(value) is Decimal and value.is_finite() and value == value.to_integral_value():
        text = str(abs(int(value)))
    elif type(value) is str:
        text = value
    else:
        return False
    if _CREDENTIAL.search(text) or _SSN.search(text):
        return True
    for match in _CARD.finditer(text):
        if _luhn(re.sub(r"[^0-9]", "", match[0])):
            return True
    for match in _IDENTITY.finditer(text):
        identity = match[0]
        try:
            born = date(int(identity[6:10]), int(identity[10:12]), int(identity[12:14]))
        except ValueError:
            continue
        if 1900 <= born.year <= 2099:
            return True
    return False
