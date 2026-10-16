"""PDF utilities: text extraction, page-N text, simple cosine."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter

from pypdf import PdfReader

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_text(pdf_bytes: bytes, max_pages: int | None = None) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    n = len(reader.pages) if max_pages is None else min(max_pages, len(reader.pages))
    out: list[str] = []
    for i in range(n):
        try:
            out.append(reader.pages[i].extract_text() or "")
        except Exception:
            out.append("")
    return "\n".join(out)


def extract_page_text(pdf_bytes: bytes, page_idx: int) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if page_idx >= len(reader.pages):
        return ""
    try:
        return reader.pages[page_idx].extract_text() or ""
    except Exception:
        return ""


def page_count(pdf_bytes: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def has_text_layer(pdf_bytes: bytes) -> bool:
    """Heuristic: at least one page has > 100 non-whitespace chars."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    for page in reader.pages[: min(5, len(reader.pages))]:
        try:
            txt = page.extract_text() or ""
        except Exception:
            continue
        if sum(1 for c in txt if not c.isspace()) > 100:
            return True
    return False


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def cosine_tokens(a: str, b: str) -> float:
    """Bag-of-tokens cosine. 0..1."""
    ta = Counter(tokenize(a))
    tb = Counter(tokenize(b))
    if not ta or not tb:
        return 0.0
    dot = sum(ta[w] * tb.get(w, 0) for w in ta)
    na = math.sqrt(sum(v * v for v in ta.values()))
    nb = math.sqrt(sum(v * v for v in tb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def looks_like_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")
