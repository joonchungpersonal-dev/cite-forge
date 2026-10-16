"""Deterministic citation existence gate — cite-forge adapter.

The decision logic (DOI -> PMID -> title-search cascade, title containment
matching, EXISTS/NOT_FOUND/UNREACHABLE verdicts) is the SAME canonical module
that /veracity-ironclad uses: ~/.claude/lib/cite_verify.py. We load it here and
supply cite-forge's own transport (httpx with the polite-pool User-Agent) via
the module's injectable `fetcher` seam, so both pipelines agree, byte for byte,
on whether a citation exists.

This is an INDEPENDENT cross-check, deliberately not a replacement for W2's
six-source identity resolution. It is a fast, dependency-light existence
primitive: a citation that no source can retrieve is fabricated, full stop. Hook
it in as a W2 pre-filter / cross-check, or call `verify_all` directly.

NOTE: this adapter does not modify the frozen `is_converged()` rule. Promoting
existence to a convergence clause is a deliberate schema change (see
ConvergenceInputs in pipeline.py) and is intentionally left opt-in.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any

import httpx

from cite_forge.http import USER_AGENT

_SHARED_PATH = os.path.expanduser("~/.claude/lib/cite_verify.py")


def _load_shared() -> Any:
    spec = importlib.util.spec_from_file_location("cite_verify_shared", _SHARED_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load shared cite_verify from {_SHARED_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _load_shared()

# Re-export the shared public surface so callers can `from cite_forge.cite_verify
# import verify_citation, EXISTS, ...` without touching the absolute path.
EXISTS = _shared.EXISTS
NOT_FOUND = _shared.NOT_FOUND
UNREACHABLE = _shared.UNREACHABLE
AMBIGUOUS = _shared.AMBIGUOUS
PASSING_VERDICTS = _shared.PASSING_VERDICTS


def httpx_fetcher(timeout: float = 12.0):
    """Build a fetcher that uses httpx + cite-forge's polite-pool User-Agent.

    Returns a `Callable[[str], tuple[int, str]]` matching the shared module's
    `Fetcher` contract. The polite UA earns CrossRef's fast tier.
    """

    def _fetch(url: str) -> tuple[int, str]:
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
                follow_redirects=True,
            )
            return resp.status_code, resp.text
        except Exception:
            return 0, ""

    return _fetch


def verify_citation(citation: dict, fetcher=None) -> dict:
    """Existence verdict for one citation (httpx transport by default)."""
    return _shared.verify_citation(citation, fetcher or httpx_fetcher())


def verify_all(citations: list[dict], fetcher=None) -> dict:
    """Existence verdicts for many citations. Non-empty `red` => fabrications."""
    return _shared.verify_all(citations, fetcher or httpx_fetcher())
