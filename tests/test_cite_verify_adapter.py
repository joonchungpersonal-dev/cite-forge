"""The cite-forge adapter wraps the shared canonical cite_verify module.

These tests inject fake fetchers, so they are fully offline. They confirm the
adapter loads the shared logic and that a fabricated citation is rejected — the
same existence gate /veracity-ironclad enforces.
"""

import json

from cite_forge import cite_verify


def test_adapter_reexports_shared_verdicts():
    assert cite_verify.EXISTS == "EXISTS"
    assert cite_verify.NOT_FOUND == "NOT_FOUND"


def test_adapter_real_doi_passes():
    def fetcher(url):
        if "/works/" in url:
            return 200, json.dumps({"message": {"title": ["Array programming with NumPy"]}})
        return 404, ""

    res = cite_verify.verify_citation(
        {"title": "Array programming with NumPy", "doi": "10.1038/s41586-020-2649-2"},
        fetcher,
    )
    assert res["passed"] is True
    assert res["verdict"] == cite_verify.EXISTS


def test_adapter_fabricated_citation_is_red():
    def fetcher(url):
        if "crossref.org/works?" in url:
            return 200, json.dumps({"message": {"items": []}})
        if "esearch" in url:
            return 200, json.dumps({"esearchresult": {"idlist": []}})
        return 404, ""

    summary = cite_verify.verify_all(
        [{"title": "US-Netherlands harmonization of MIDUS sleep latent classes", "year": "2023"}],
        fetcher,
    )
    assert summary["red_count"] == 1
    assert summary["passed"] == 0


def test_httpx_fetcher_is_callable():
    f = cite_verify.httpx_fetcher()
    assert callable(f)
