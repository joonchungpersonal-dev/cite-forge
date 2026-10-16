"""A25-Manual UM proxy URL generator tests.

Compliance-critical: these tests verify cite-forge generates proxy URLs
correctly AND that the module does not contain any HTTP fetch code that
would violate UM Library AUP.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cite_forge.proxy import (
    UM_PROXY_LOGIN,
    doi_url,
    render_missing_md_block,
    render_missing_md_entry,
    um_proxy_for_doi,
    um_proxy_url,
)


# ---------- URL generation ----------


def test_um_proxy_url_wraps_target():
    out = um_proxy_url("https://doi.org/10.1037/0033-2909.98.2.310")
    parsed = urlparse(out)
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.access.library.miami.edu"
    assert parsed.path == "/login"
    qs = parse_qs(parsed.query)
    assert qs["url"] == ["https://doi.org/10.1037/0033-2909.98.2.310"]


def test_um_proxy_url_rejects_empty():
    with pytest.raises(ValueError):
        um_proxy_url("")


def test_doi_url_validates_format():
    assert doi_url("10.1093/sleep/zsad048") == "https://doi.org/10.1093/sleep/zsad048"
    with pytest.raises(ValueError):
        doi_url("not-a-doi")
    with pytest.raises(ValueError):
        doi_url("10.")


def test_um_proxy_for_doi_round_trip():
    out = um_proxy_for_doi("10.1037/0033-2909.98.2.310")
    parsed = urlparse(out)
    assert parsed.netloc == "login.access.library.miami.edu"
    assert "doi.org" in parsed.query
    assert "0033-2909" in parsed.query


def test_um_proxy_for_doi_invalid_raises():
    with pytest.raises(ValueError):
        um_proxy_for_doi("garbage")


# ---------- Markdown rendering ----------


def test_render_missing_md_entry():
    line = render_missing_md_entry(
        ref_num=3,
        label="Baglioni 2011 J Affect Disord",
        doi="10.1016/j.jad.2011.01.011",
        publisher="Elsevier",
    )
    assert line.startswith("| 3 |")
    assert "Baglioni" in line
    assert "click to fetch via UM proxy" in line
    assert UM_PROXY_LOGIN in line
    assert "Elsevier" in line


def test_render_missing_md_entry_no_doi():
    line = render_missing_md_entry(
        ref_num=24, label="Murphy 2025 (DOI TBD)", doi=None, publisher="JAD"
    )
    assert "no DOI" in line
    assert UM_PROXY_LOGIN not in line  # no proxy link without DOI


def test_render_missing_md_block_has_header():
    block = render_missing_md_block(
        [
            {
                "ref_num": 3,
                "label": "Baglioni 2011",
                "doi": "10.1016/j.jad.2011.01.011",
                "publisher": "Elsevier",
            },
            {
                "ref_num": 9,
                "label": "Cohen & Wills 1985",
                "doi": "10.1037/0033-2909.98.2.310",
                "publisher": "APA",
            },
        ]
    )
    assert "| Ref | Citation |" in block
    assert "Baglioni" in block
    assert "Cohen" in block
    assert block.count("login.access.library.miami.edu") == 2


# ---------- Compliance guard rails ----------


def test_proxy_module_does_not_import_httpx():
    """A25-Manual must not contain HTTP client code. If this test fails, an
    automated-download path may have been added that violates UM AUP.
    """
    import cite_forge.proxy as mod

    src = Path(mod.__file__).read_text()
    # Catch obvious AUP-violating imports
    forbidden_imports = ("import httpx", "import requests", "from httpx", "from requests")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for forb in forbidden_imports:
            assert forb not in stripped, (
                f"cite_forge.proxy contains forbidden import: {stripped!r}. "
                "A25-Manual must NOT contain HTTP client code (UM AUP)."
            )


def test_proxy_module_compliance_docstring_present():
    """The compliance boundary must be documented at module level."""
    import cite_forge.proxy as mod

    doc = mod.__doc__ or ""
    assert "COMPLIANCE BOUNDARY" in doc
    assert "AUP" in doc or "Acceptable Use" in doc
    assert "Systematic or substantial" in doc  # quoted UM Library policy


# ---------- Agent integration ----------


def test_um_proxy_agent_returns_proxy_url(tmp_path):
    from cite_forge.agents.W5_retrieval import RetrievalInput, UMProxyAgent
    from cite_forge.schemas import Author, Bibliography, CitationIdentity

    bib = Bibliography(
        authors=[Author(name_verbatim="Cohen", family="Cohen", position=1)],
        title="Stress, social support, and the buffering hypothesis",
        journal="Psychological Bulletin",
        year=1985,
    )
    ident = CitationIdentity(
        doi="10.1037/0033-2909.98.2.310", consensus_confidence=1.0
    )
    payload = RetrievalInput(
        ref_num=9,
        identity=ident,
        bibliography=bib,
        target_path="papers/09_CohenWills_1985_StressBuffer.pdf",
    )
    agent = UMProxyAgent(trail_dir=tmp_path)
    out = agent.run(payload)
    assert out.pdf_bytes is None
    assert out.source_url is not None
    assert "login.access.library.miami.edu" in out.source_url
    assert "10.1037" in out.source_url
    assert out.error is not None
    assert "manual_action_required" in out.error


def test_um_proxy_agent_no_doi_returns_null_url(tmp_path):
    from cite_forge.agents.W5_retrieval import RetrievalInput, UMProxyAgent
    from cite_forge.schemas import Author, Bibliography, CitationIdentity

    bib = Bibliography(
        authors=[Author(name_verbatim="Murphy", family="Murphy", position=1)],
        title="Pathways to better sleep",
        journal="JAD",
        year=2025,
    )
    ident = CitationIdentity(doi=None, consensus_confidence=0.5)
    payload = RetrievalInput(
        ref_num=24,
        identity=ident,
        bibliography=bib,
        target_path="papers/24_Murphy_2025.pdf",
    )
    agent = UMProxyAgent(trail_dir=tmp_path)
    out = agent.run(payload)
    assert out.pdf_bytes is None
    assert out.source_url is None  # no DOI → no proxy URL
