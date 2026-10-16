"""sqlite cache for API responses (positive AND negative).

Cache is keyed by (source, identifier_type, identifier). Negative entries
carry a TTL so paywall/404 failures eventually re-try. CrossRef and PubMed
responses cache for 30 days; OA-locations cache for 7 days; retraction
status caches for 24 hours (retraction events are time-sensitive).

User-Agent for every outbound request:

    cite-forge/{__version__} (mailto:joon.chung.personal@gmail.com)

Polite-pool tier for CrossRef + EuropePMC.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    source TEXT NOT NULL,
    id_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body BLOB,
    headers_json TEXT,
    fetched_at INTEGER NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    PRIMARY KEY (source, id_type, identifier)
);

CREATE INDEX IF NOT EXISTS idx_fetched ON responses (fetched_at);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    wave TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    fetched_at INTEGER NOT NULL,
    draft_hash TEXT,
    citation_ref_num INTEGER
);

CREATE INDEX IF NOT EXISTS idx_llm_prompt ON llm_calls (prompt_hash);
CREATE INDEX IF NOT EXISTS idx_llm_draft ON llm_calls (draft_hash);
"""


def open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn
