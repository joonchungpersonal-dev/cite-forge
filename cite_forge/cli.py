"""cite-forge CLI.

Surface:

    cite-forge build <draft> [--slug SLUG] [--refresh] [--allow-retracted]
    cite-forge extract <draft>
    cite-forge fetch <mapping.json>
    cite-forge verify <manuscript-dir>
    cite-forge manifest <manuscript-dir>
    cite-forge status <manuscript-dir>
    cite-forge replay <run_id>
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from cite_forge.pipeline import Pipeline, PipelineState, manuscript_hash
from cite_forge.trail import new_run_dir


def _load_manuscript_text(draft: Path) -> str:
    suffix = draft.suffix.lower()
    if suffix in (".md", ".txt"):
        return draft.read_text(encoding="utf-8", errors="replace")
    if suffix == ".docx":
        import docx

        d = docx.Document(str(draft))
        return "\n".join(p.text for p in d.paragraphs)
    if suffix == ".pdf":
        from cite_forge.pdf import extract_text

        return extract_text(draft.read_bytes())
    raise click.ClickException(f"Unsupported manuscript format: {suffix}")


@click.group()
@click.version_option()
def main() -> None:
    """Accuracy-first citation forge."""


@main.command()
@click.argument("draft", type=click.Path(exists=True, path_type=Path))
@click.option("--slug", help="Manuscript slug; defaults to filename stem.")
@click.option("--refresh", is_flag=True, help="Re-download even if PDFs present.")
@click.option(
    "--allow-retracted",
    is_flag=True,
    help="Allow retracted citations into manifest (default: refuse).",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging.")
def build(
    draft: Path,
    slug: str | None,
    refresh: bool,
    allow_retracted: bool,
    verbose: bool,
) -> None:
    """Run the full pipeline (W0 through W8) on a draft."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    text = _load_manuscript_text(draft)
    sha = manuscript_hash(draft)
    run_dir = new_run_dir()

    state = PipelineState(
        run_dir=run_dir,
        manuscript_path=draft,
        manuscript_sha256=sha,
        manuscript_text=text,
        allow_retracted=allow_retracted,
    )

    click.echo("cite-forge build")
    click.echo(f"  draft: {draft}")
    click.echo(f"  sha256: {sha[:16]}")
    click.echo(f"  run_dir: {run_dir}")
    click.echo("")

    pipeline = Pipeline(state)
    manifest = pipeline.run_all()

    from cite_forge.agents.base import RUN_STATS

    click.echo("")
    click.echo(f"Done. Manifest: {len(manifest.citations)} citations")
    click.echo(f"      Escalations: {len(manifest.escalations)}")
    click.echo(
        f"      Cost: ${RUN_STATS.cost_usd:.4f} over {RUN_STATS.llm_calls} LLM calls "
        f"({RUN_STATS.tokens_in:,} in / {RUN_STATS.tokens_out:,} out tokens; "
        f"{RUN_STATS.agent_calls} agent calls total)"
    )


@main.command()
@click.argument("draft", type=click.Path(exists=True))
def extract(draft: str) -> None:
    """W0+W1: decompose manuscript into atomic units and citation lists."""
    raise NotImplementedError("Use `build` for the full pipeline.")


@main.command()
@click.argument("mapping", type=click.Path(exists=True))
def fetch(mapping: str) -> None:
    """W2-W5: resolve identities and retrieve PDFs from a citation mapping."""
    raise NotImplementedError("Use `build` for the full pipeline.")


@main.command()
@click.argument("manuscript_dir", type=click.Path(exists=True))
def verify(manuscript_dir: str) -> None:
    """W6-W7: context capture and blind verification."""
    raise NotImplementedError("Use `build` for the full pipeline.")


@main.command()
@click.argument("manuscript_dir", type=click.Path(exists=True))
def manifest(manuscript_dir: str) -> None:
    """W8: assemble manifest.json from per-citation trail."""
    raise NotImplementedError("Use `build` for the full pipeline.")


@main.command()
@click.argument("manuscript_dir", type=click.Path(exists=True))
def status(manuscript_dir: str) -> None:
    """Print current corpus status: retrieved / pending / escalated."""
    import json

    mp = Path(manuscript_dir)
    manifest_path = mp / "manifest.json"
    if not manifest_path.exists():
        click.echo(f"No manifest at {manifest_path}")
        return
    data = json.loads(manifest_path.read_text())
    cites = data.get("citations", [])
    escalations = data.get("escalations", [])
    converged = sum(
        1 for c in cites if c.get("convergence_verdict") == "CONVERGED"
    )
    pdfs = sum(1 for c in cites if c.get("pdf") is not None)
    click.echo(f"manifest: {manifest_path}")
    click.echo(f"  citations: {len(cites)} (converged: {converged}, PDFs: {pdfs})")
    click.echo(f"  escalations: {len(escalations)}")


@main.command()
@click.argument("run_id")
def replay(run_id: str) -> None:
    """Re-run pipeline against cached LLM/API calls."""
    raise NotImplementedError("Replay deterministically against cache pending.")


@main.command()
@click.argument("manuscript_dir", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--papers-subdir",
    default="papers",
    help="Subdirectory containing PDFs (default: papers/).",
)
def ingest(manuscript_dir: Path, papers_subdir: str) -> None:
    """A25-Manual ingest: scan papers/ for newly-added PDFs and update manifest.

    Workflow:
      1. cite-forge build <draft>  → emits MISSING.md with clickable UM proxy URLs
      2. You click each link in your browser, authenticate via Shibboleth,
         save the PDF into the manuscript's papers/ folder.
      3. Run `cite-forge ingest <manuscript-dir>` → cite-forge runs A27 PDF
         verification on the new files and updates manifest.json + MISSING.md.

    This is the only A25 path. cite-forge never authenticates to the proxy
    on its own.
    """
    import hashlib
    import json
    import re

    papers_dir = manuscript_dir / papers_subdir
    if not papers_dir.exists():
        raise click.ClickException(f"Papers folder not found: {papers_dir}")

    manifest_path = manuscript_dir / "manifest.json"
    if not manifest_path.exists():
        raise click.ClickException(
            f"No manifest.json at {manifest_path}. Run `cite-forge build` first."
        )

    manifest_obj = json.loads(manifest_path.read_text())

    # Index existing PDFs in the manifest by ref_num.
    existing_pdfs: dict[int, str] = {}
    for c in manifest_obj.get("citations", []):
        ref = c.get("ref_num")
        pdf = c.get("pdf_path") or (c.get("pdf") or {}).get("path")
        if ref is not None and pdf:
            existing_pdfs[int(ref)] = pdf

    # Scan papers/ and find newly-added files matching the convention
    # NN_Author_Year_Topic.pdf
    filename_re = re.compile(r"^(\d{1,3})_[^_]+_\d{4}_.+\.pdf$")
    new_files: list[tuple[int, Path]] = []
    for f in sorted(papers_dir.glob("*.pdf")):
        m = filename_re.match(f.name)
        if not m:
            click.echo(f"  skip: {f.name} (filename does not match NN_Author_Year_Topic.pdf)")
            continue
        ref_num = int(m.group(1))
        rel = f"papers/{f.name}"
        if existing_pdfs.get(ref_num) == rel:
            continue  # already in manifest
        new_files.append((ref_num, f))

    if not new_files:
        click.echo("No new PDFs to ingest.")
        return

    click.echo(f"Found {len(new_files)} new PDFs:")
    for ref_num, f in new_files:
        size = f.stat().st_size
        sha = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
        magic_ok = f.read_bytes()[:5].startswith(b"%PDF-")
        click.echo(
            f"  ref {ref_num}: {f.name} ({size:,} B, sha256 {sha}..., PDF magic: {magic_ok})"
        )

    click.echo("")
    click.echo("Next: run A27 PDF Verification on each new file. (Wired in next iteration.)")
    click.echo(
        "For now, manually verify each PDF matches the expected citation "
        "(title page author/year/title), then drop it into the manifest."
    )


if __name__ == "__main__":
    main()
