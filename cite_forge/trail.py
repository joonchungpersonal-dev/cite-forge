"""Per-citation agent trail writer.

Mirrors /veracity-tweaked-555's audit-trail pattern. For every manuscript run:

    audit-trails/cite-forge/{run_id}/
        run_metadata.json
        manuscript_input.{md,docx,pdf}.sha256
        units/
            U0001.json          # atomic decomposition output for the unit
        citations/
            {NN}/
                A01_atomic_decomposition.json
                A05_doi_resolution.json
                A06_pubmed.json
                ...
                A50_manifest_integrity.json
        cross_references.json
        convergence_critic.json
        escalations.json
        failures.json

Trail dirs are append-only within a run. Replays read from this tree.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

TRAIL_ROOT = Path.home() / ".claude" / "audit-trails" / "cite-forge"


def new_run_dir(run_id: str | None = None) -> Path:
    run_id = run_id or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = TRAIL_ROOT / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "units").mkdir(exist_ok=True)
    (path / "citations").mkdir(exist_ok=True)
    return path


def citation_trail_dir(run_dir: Path, ref_num: int) -> Path:
    path = run_dir / "citations" / f"{ref_num:03d}"
    path.mkdir(parents=True, exist_ok=True)
    return path
