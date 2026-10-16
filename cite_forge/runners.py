"""Wave dispatchers — per-wave runners that orchestrate per-citation fan-out
and parallel-source probes.

ARCHITECTURAL INVARIANTS:
  1. Each wave runner consumes a typed input (built from prior waves) and
     returns a typed output written to the run directory.
  2. Parallel-per-citation work uses ThreadPoolExecutor (I/O-bound APIs +
     blocking Anthropic SDK).
  3. A wave is "done" only when every required agent has a trail file
     with verifier_passed=True OR its citation is in escalations.
  4. Verifier failures DON'T crash the run; they record into failures.json
     and route the citation to escalations.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from cite_forge.agents.base import VerifierFailed
from cite_forge.trail import citation_trail_dir

log = logging.getLogger("cite_forge.runners")

DEFAULT_WORKERS = int(os.environ.get("CITE_FORGE_WORKERS", "8"))

_failure_lock = threading.Lock()
_escalation_lock = threading.Lock()
_trail_locks: dict[str, threading.Lock] = {}
_trail_locks_guard = threading.Lock()


def _trail_lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _trail_locks_guard:
        if key not in _trail_locks:
            _trail_locks[key] = threading.Lock()
        return _trail_locks[key]


def run_parallel(
    fns: list[tuple[str, callable]],
    *,
    max_workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    """Run a list of (label, fn) pairs in parallel. Returns {label: result_or_exception}."""
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fn): label for label, fn in fns}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except VerifierFailed as e:
                results[label] = e
            except Exception as e:
                results[label] = e
    return results


def append_failure(run_dir: Path, record: dict) -> None:
    path = run_dir / "failures.json"
    with _failure_lock:
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except json.JSONDecodeError:
                existing = {"failures": []}
        else:
            existing = {"failures": []}
        existing["failures"].append(record)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, indent=2, default=str))
        tmp.replace(path)


def append_escalation(run_dir: Path, record: dict) -> None:
    path = run_dir / "escalations.json"
    with _escalation_lock:
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except json.JSONDecodeError:
                existing = {"escalations": []}
        else:
            existing = {"escalations": []}
        existing["escalations"].append(record)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, indent=2, default=str))
        tmp.replace(path)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def per_citation_trail(run_dir: Path, ref_num: int) -> Path:
    return citation_trail_dir(run_dir, ref_num)
