"""HTTP client with polite-pool User-Agent, sqlite cache, rate-limit, retry.

ARCHITECTURAL INVARIANTS:
  1. Every outbound request carries the polite-pool User-Agent so CrossRef
     and EuropePMC return us their fast tier.
  2. Cache lookup is by (source, id_type, identifier). Negative entries TTL out.
  3. Rate limit per source is enforced via aiolimiter (async) and a simple
     min-interval token bucket (sync).
  4. Retries on 429/5xx with exponential backoff.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from cite_forge import __version__
from cite_forge.cache import open_cache

USER_AGENT = f"cite-forge/{__version__} (mailto:joon.chung.personal@gmail.com)"

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

CACHE_PATH = Path.home() / ".claude" / "audit-trails" / "cite-forge" / "cache.sqlite3"

# TTL in seconds
TTL = {
    "crossref": 30 * 86400,
    "openalex": 30 * 86400,
    "pubmed": 30 * 86400,
    "europepmc": 7 * 86400,
    "unpaywall": 7 * 86400,
    "semantic_scholar": 30 * 86400,
    "retraction_watch": 86400,  # time-sensitive
    "doi_resolver": 30 * 86400,
    "openalex_oa": 7 * 86400,
    "publisher_oa": 7 * 86400,
    "arxiv": 30 * 86400,
    "biorxiv": 30 * 86400,
    "medrxiv": 30 * 86400,
    "crossmark": 86400,
    "orcid": 30 * 86400,
    "doaj": 7 * 86400,
    "negative": 6 * 3600,  # short TTL for misses
}

# Per-source min interval between requests (seconds)
RATE = {
    "crossref": 0.05,
    "openalex": 0.1,
    "pubmed": 0.34,  # NCBI: 3 req/sec without key
    "europepmc": 0.1,
    "unpaywall": 0.1,
    "semantic_scholar": 1.0,  # public API is strict
    "retraction_watch": 0.5,
    "doi_resolver": 0.1,
    "arxiv": 1.0,
    "biorxiv": 0.5,
    "medrxiv": 0.5,
    "crossmark": 0.1,
    "orcid": 0.1,
    "doaj": 0.1,
    "default": 0.1,
}


class _RateGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, source: str) -> None:
        interval = RATE.get(source, RATE["default"])
        with self._lock:
            last = self._last.get(source, 0.0)
            elapsed = time.monotonic() - last
            if elapsed < interval:
                time.sleep(interval - elapsed)
            self._last[source] = time.monotonic()


_gate = _RateGate()
_cache_lock = threading.Lock()
_cache_conn: sqlite3.Connection | None = None


def _cache() -> sqlite3.Connection:
    global _cache_conn
    if _cache_conn is None:
        _cache_conn = open_cache(CACHE_PATH)
    return _cache_conn


def cache_lookup(
    source: str, id_type: str, identifier: str
) -> tuple[int, bytes | None, dict | None] | None:
    """Returns (status_code, body, headers) or None if no fresh entry."""
    with _cache_lock:
        cur = _cache().execute(
            "SELECT status_code, body, headers_json, fetched_at, ttl_seconds "
            "FROM responses WHERE source=? AND id_type=? AND identifier=?",
            (source, id_type, identifier),
        )
        row = cur.fetchone()
    if row is None:
        return None
    status_code, body, headers_json, fetched_at, ttl = row
    if time.time() - fetched_at > ttl:
        return None
    headers = json.loads(headers_json) if headers_json else None
    return (status_code, body, headers)


def cache_store(
    source: str,
    id_type: str,
    identifier: str,
    status_code: int,
    body: bytes | None,
    headers: Mapping[str, str] | None,
    ttl_seconds: int,
) -> None:
    with _cache_lock:
        _cache().execute(
            "INSERT OR REPLACE INTO responses "
            "(source, id_type, identifier, status_code, body, headers_json, fetched_at, ttl_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source,
                id_type,
                identifier,
                status_code,
                body,
                json.dumps(dict(headers)) if headers else None,
                int(time.time()),
                ttl_seconds,
            ),
        )
        _cache().commit()


_client: httpx.Client | None = None


def http_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _request_with_retry(
    method: str, url: str, *, headers: dict | None = None, params: dict | None = None
) -> httpx.Response:
    resp = http_client().request(method, url, headers=headers, params=params)
    if resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp


def cached_get(
    source: str,
    id_type: str,
    identifier: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json_response: bool = True,
    force_refresh: bool = False,
) -> tuple[int, bytes, dict[str, str]]:
    """Cached GET. Returns (status_code, body_bytes, response_headers)."""
    if not force_refresh:
        hit = cache_lookup(source, id_type, identifier)
        if hit is not None:
            return (hit[0], hit[1] or b"", hit[2] or {})

    _gate.wait(source)
    try:
        resp = _request_with_retry("GET", url, headers=headers, params=params)
        body = resp.content
        status = resp.status_code
        resp_headers = dict(resp.headers)
    except httpx.HTTPError:
        # Store a 0/no-body negative cache so we don't hammer
        cache_store(source, id_type, identifier, 0, None, None, TTL["negative"])
        raise

    ttl = TTL.get(source, TTL["negative"]) if 200 <= status < 300 else TTL["negative"]
    cache_store(source, id_type, identifier, status, body, resp_headers, ttl)
    return (status, body, resp_headers)


def cached_get_json(
    source: str,
    id_type: str,
    identifier: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    force_refresh: bool = False,
) -> tuple[int, Any]:
    status, body, _ = cached_get(
        source,
        id_type,
        identifier,
        url,
        headers={"Accept": "application/json", **(headers or {})},
        params=params,
        force_refresh=force_refresh,
    )
    if status != 200 or not body:
        return (status, None)
    try:
        return (status, json.loads(body))
    except json.JSONDecodeError:
        return (status, None)


def fetch_pdf(
    source: str,
    identifier: str,
    url: str,
    *,
    headers: dict | None = None,
    force_refresh: bool = False,
) -> tuple[int, bytes, dict[str, str]]:
    """Fetch a PDF. PDF bodies are stored in cache as bytes."""
    return cached_get(
        source,
        "pdf",
        identifier,
        url,
        headers={"Accept": "application/pdf", **(headers or {})},
        force_refresh=force_refresh,
    )
