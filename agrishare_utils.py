"""Pure helper functions (no Streamlit dependency) so they can be unit-tested."""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
from urllib.parse import urlparse

import pandas as pd

CURRENCIES = ["EUR", "USD", "GBP", "GHS", "NGN", "KES", "ZAR", "XOF", "XAF", "UGX", "TZS",
              "ETB", "EGP", "MAD", "RWF", "ZMW", "MWK", "SLE", "Other"]
_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$&:])")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_text(value, max_len: int = 500) -> str:
    """Strip control characters and surrounding whitespace, and enforce a length limit."""
    text = _CONTROL_CHARS.sub("", str(value or "")).strip()
    return text[:max_len]


def md_escape(value) -> str:
    """Escape Markdown so user text cannot create links, images, headings or LaTeX."""
    return _MD_SPECIAL.sub(r"\\\1", str(value if value is not None else ""))


def html_escape(value) -> str:
    """Escape text for HTML *and* for JS template literals (folium embeds popups in JS)."""
    text = html.escape(str(value if value is not None else ""), quote=True)
    for ch, ent in (("`", "&#96;"), ("$", "&#36;"), ("{", "&#123;"), ("}", "&#125;"), ("\\", "&#92;")):
        text = text.replace(ch, ent)
    return text


def valid_http_url(url: str, max_len: int = 500) -> bool:
    url = (url or "").strip()
    if not url or len(url) > max_len or re.search(r"\s", url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def valid_email(email: str) -> bool:
    email = (email or "").strip()
    return len(email) <= 254 and bool(_EMAIL.match(email))


def valid_coordinates(lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    return not (abs(lat) < 1e-9 and abs(lon) < 1e-9)  # (0, 0) is almost always an unset field


def money(value, currency: str = "EUR") -> str:
    symbol = _SYMBOLS.get(currency, f"{currency} ")
    return f"{symbol}{value:,.0f}"


def csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Neutralise spreadsheet formula injection (=, +, -, @) in text cells before CSV export."""
    out = df.copy()
    for col in out.columns:
        # Not `dtype == object`: pandas 3 uses a dedicated string dtype. Anything non-numeric is checked.
        if not pd.api.types.is_numeric_dtype(out[col]) and not pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].map(
                lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
            )
    return out


def new_update_code() -> str:
    return secrets.token_urlsafe(8)


def hash_code(code: str) -> str:
    return hashlib.sha256((code or "").strip().encode("utf-8")).hexdigest()


def check_code(code: str, stored_hash) -> bool:
    if not stored_hash or not code:
        return False
    return hmac.compare_digest(hash_code(code), stored_hash)
