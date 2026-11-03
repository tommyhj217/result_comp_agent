"""값 정규화.

* ``canon``      : 엄격(strict) 정규형. 이 값이 같으면 '변경 없음'으로 본다.
                   (NFC 유니코드 정규화, 줄바꿈 통일, 1 == 1.0, datetime(00:00) == date 만 동일 취급)
* ``canon_loose``: 느슨한 정규형. strict 는 다르지만 loose 가 같으면 '서식/표기만 변경(format_only)'.
                   (공백 축약, "1,000" == 1000, "12%" == 0.12, "2024.01.05" == 2024-01-05 등)

정규형은 타입 태그가 붙은 문자열("s:", "n:", "d:", "f:" ...)이라 JSON 직렬화와 해시가 안전하다.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from typing import Any, Optional

_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_PCT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*%$")
_DATE_RE = re.compile(r"^(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*[.일]?$")
_REF_RE = re.compile(r"\$?\b[A-Z]{1,3}\$?\d+\b")
_NUM_IN_TEXT_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)*")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def clean_text(s: str) -> str:
    """엄격 비교용 최소 정리: NFC + 줄바꿈 통일 (PPT 줄바꿈 \\x0b 포함)."""
    s = unicodedata.normalize("NFC", s)
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")


def collapse_ws(s: str) -> str:
    return _WS_RE.sub(" ", s.replace(" ", " ")).strip()


def num_str(x: Any) -> str:
    f = float(x)
    if math.isnan(f):
        return "nan"
    if math.isinf(f):
        return "inf" if f > 0 else "-inf"
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _round_sig(f: float, sig: int = 10) -> str:
    if f == 0 or math.isnan(f) or math.isinf(f):
        return num_str(f)
    return num_str(float(f"{f:.{sig}g}"))


def canon(value: Any) -> Optional[str]:
    """엄격 정규형. 빈 값은 None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if isinstance(value, (int, float)):
        return "n:" + num_str(value)
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0) and value.tzinfo is None:
            return "d:" + value.date().isoformat()
        return "dt:" + value.isoformat()
    if isinstance(value, dt.date):
        return "d:" + value.isoformat()
    if isinstance(value, dt.time):
        return "t:" + value.isoformat()
    if isinstance(value, dt.timedelta):
        return "td:" + num_str(value.total_seconds())
    s = clean_text(str(value))
    if s == "":
        return None
    return "s:" + s


def canon_formula(text: str) -> str:
    return "f:" + clean_text(text).strip()


def tag(c: Optional[str]) -> Optional[str]:
    return None if c is None else c.split(":", 1)[0]


def body(c: Optional[str]) -> str:
    return "" if c is None else c.split(":", 1)[1]


def parse_number(s: str) -> Optional[float]:
    s = s.strip()
    m = _PCT_RE.match(s)
    if m:
        return float(m.group(1)) / 100.0
    if _NUM_RE.match(s):
        return float(s.replace(",", ""))
    return None


def parse_date(s: str) -> Optional[str]:
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def canon_loose(c: Optional[str]) -> Optional[str]:
    if c is None:
        return None
    t, b = c.split(":", 1)
    if t == "s":
        s = collapse_ws(b)
        if not s:
            return None
        n = parse_number(s)
        if n is not None:
            return "n:" + _round_sig(n)
        d = parse_date(s)
        if d is not None:
            return "d:" + d
        return "s:" + s
    if t == "n":
        try:
            return "n:" + _round_sig(float(b))
        except ValueError:
            return c
    if t == "f":
        return "f:" + _WS_RE.sub("", b).upper()
    if t == "dt":
        return c
    return c


def compare(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """None: 동일 / 'format': 서식·표기만 다름 / 'content': 내용 변경."""
    if a == b:
        return None
    if canon_loose(a) == canon_loose(b):
        return "format"
    return "content"


def display(c: Optional[str]) -> str:
    if c is None:
        return ""
    return body(c)


def numeric_value(c: Optional[str]) -> Optional[float]:
    if c is None:
        return None
    t, b = c.split(":", 1)
    if t == "n":
        try:
            return float(b)
        except ValueError:
            return None
    if t == "s":
        return parse_number(collapse_ws(b))
    return None


def formula_shape(c: Optional[str]) -> Optional[str]:
    """수식에서 셀 참조를 제거한 형태 (행/열 삽입으로 인한 참조 이동 판별용)."""
    if c is None or not c.startswith("f:"):
        return None
    return _REF_RE.sub("REF", _WS_RE.sub("", body(c)).upper())


def numbers_in_text(s: str) -> list[str]:
    return [n.replace(",", "") for n in _NUM_IN_TEXT_RE.findall(s or "")]


def strip_numbers(s: str) -> str:
    return _NUM_IN_TEXT_RE.sub("#", s or "")


def norm_for_match(s: str) -> str:
    """코드 앵커 매칭용 정규화: NFC + casefold + 공백/구두점 제거."""
    return _PUNCT_RE.sub("", unicodedata.normalize("NFC", s).casefold())


def has_cjk(s: str) -> bool:
    return any("ㄱ" <= ch <= "힣" or "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ" for ch in s)


def shorten(s: Optional[str], n: int = 120) -> str:
    if s is None:
        return ""
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"
