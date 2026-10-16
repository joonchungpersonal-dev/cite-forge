"""W5 — Retrieval (parallel per citation).

A19 Retrieval Coordinator (deterministic, fully implemented)
A20 Unpaywall          (API)
A21 Europe PMC         (API)
A22 PubMed Central     (API)
A23 arXiv/bioRxiv/medRxiv (API)
A24 Publisher OA       (API)
A25 UM Library Proxy   (API; Shibboleth — skipped at runtime, returns null)
A26 ILL Fallback       (non-blocking ticket; here just records intent)
A27 PDF Verification Agent — THE CRITICAL ACCURACY GATE
A38 lives in verification.py
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import APIAgent, DeterministicAgent
from cite_forge.http import cached_get_json, fetch_pdf
from cite_forge.pdf import (
    cosine_tokens,
    extract_page_text,
    has_text_layer,
    looks_like_pdf,
    page_count,
    sha256_bytes,
)
from cite_forge.schemas import Bibliography, CitationIdentity


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- shared ----------


class SourceResult(_Frozen):
    source: str
    pdf_bytes: bytes | None = None
    sha256: str | None = None
    source_url: str | None = None
    license: str | None = None
    retrieved_at: datetime | None = None
    page_count: int | None = None
    has_text_layer: bool | None = None
    error: str | None = None
    is_published_version: bool


class RetrievalInput(_Frozen):
    ref_num: int
    identity: CitationIdentity
    bibliography: Bibliography
    target_path: str


# ---------- source specialists ----------


class _SourceAgent(APIAgent[RetrievalInput, SourceResult]):
    wave = "W5"

    def _verify(self, payload: RetrievalInput, output: SourceResult) -> None:
        if output.pdf_bytes is None:
            return
        if not output.pdf_bytes.startswith(b"%PDF-"):
            self.fail(f"source {output.source}: returned bytes lack PDF magic")
        if not output.sha256:
            self.fail(f"source {output.source}: sha256 not computed")
        expected = hashlib.sha256(output.pdf_bytes).hexdigest()
        if output.sha256 != expected:
            self.fail(f"source {output.source}: sha256 mismatch")
        if len(output.pdf_bytes) < 4096:
            self.fail(f"source {output.source}: PDF < 4KB suspicious")


def _wrap_pdf_fetch(
    source: str,
    identifier: str,
    url: str,
    *,
    is_published: bool,
    license: str | None = None,
) -> SourceResult:
    try:
        status, body, headers = fetch_pdf(source, identifier, url)
    except Exception as e:
        return SourceResult(source=source, error=str(e), is_published_version=is_published)
    if status != 200 or not body or not looks_like_pdf(body):
        return SourceResult(
            source=source,
            error=f"non-200 or non-PDF (status={status}, len={len(body) if body else 0})",
            is_published_version=is_published,
        )
    try:
        pc = page_count(body)
        htl = has_text_layer(body)
    except Exception:
        pc = None
        htl = None
    return SourceResult(
        source=source,
        pdf_bytes=body,
        sha256=sha256_bytes(body),
        source_url=url,
        license=license,
        retrieved_at=datetime.utcnow(),
        page_count=pc,
        has_text_layer=htl,
        is_published_version=is_published,
    )


# ---------- A20: Unpaywall ----------


class UnpaywallAgent(_SourceAgent):
    name = "A20_unpaywall"

    EMAIL = "joon.chung.personal@gmail.com"
    BASE = "https://api.unpaywall.org/v2"

    def _execute(self, payload: RetrievalInput) -> SourceResult:
        doi = payload.identity.doi
        if not doi:
            return SourceResult(source=self.name, error="no DOI", is_published_version=True)
        url = f"{self.BASE}/{urllib.parse.quote(doi, safe='')}"
        s, data = cached_get_json(
            "unpaywall", "doi", doi, url, params={"email": self.EMAIL}
        )
        if s != 200 or not data:
            return SourceResult(
                source=self.name,
                error=f"unpaywall status {s}",
                is_published_version=True,
            )
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") or best.get("url")
        if not pdf_url:
            return SourceResult(
                source=self.name, error="no OA pdf url", is_published_version=True
            )
        license_ = best.get("license")
        host_type = best.get("host_type") or "publisher"
        return _wrap_pdf_fetch(
            self.name,
            doi,
            pdf_url,
            is_published=(host_type == "publisher"),
            license=license_,
        )


# ---------- A21: Europe PMC ----------


class EuropePMCAgent(_SourceAgent):
    name = "A21_europepmc"

    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def _execute(self, payload: RetrievalInput) -> SourceResult:
        pmcid = payload.identity.pmcid
        if pmcid:
            url = f"{self.BASE}/{pmcid}/fullTextPDF"
            return _wrap_pdf_fetch(self.name, pmcid, url, is_published=True)
        # Try by DOI or PMID via search
        if payload.identity.doi:
            q = f"DOI:{payload.identity.doi}"
            ident = f"doi:{payload.identity.doi}"
        elif payload.identity.pmid:
            q = f"EXT_ID:{payload.identity.pmid} AND SRC:MED"
            ident = f"pmid:{payload.identity.pmid}"
        else:
            return SourceResult(
                source=self.name, error="no doi/pmid/pmcid", is_published_version=True
            )
        s, data = cached_get_json(
            "europepmc",
            "search",
            ident,
            f"{self.BASE}/search",
            params={"query": q, "format": "json", "resultType": "core"},
        )
        if s != 200 or not data:
            return SourceResult(
                source=self.name, error=f"europepmc status {s}", is_published_version=True
            )
        results = ((data.get("resultList") or {}).get("result")) or []
        if not results:
            return SourceResult(
                source=self.name, error="no results", is_published_version=True
            )
        hit = results[0]
        # full-text URL
        fulltext_list = (hit.get("fullTextUrlList") or {}).get("fullTextUrl") or []
        for ft in fulltext_list:
            if (ft.get("documentStyle") or "").lower() == "pdf":
                return _wrap_pdf_fetch(
                    self.name, ident, ft.get("url"), is_published=True
                )
        pmcid = hit.get("pmcid")
        if pmcid:
            return _wrap_pdf_fetch(
                self.name, pmcid, f"{self.BASE}/{pmcid}/fullTextPDF", is_published=True
            )
        return SourceResult(
            source=self.name, error="no fulltext PDF", is_published_version=True
        )


# ---------- A22: PubMed Central ----------


class PMCAgent(_SourceAgent):
    name = "A22_pmc"

    def _execute(self, payload: RetrievalInput) -> SourceResult:
        pmcid = payload.identity.pmcid
        if not pmcid:
            return SourceResult(source=self.name, error="no PMCID", is_published_version=True)
        num = pmcid.removeprefix("PMC")
        # PMC direct PDF URL
        url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/"
        result = _wrap_pdf_fetch(self.name, pmcid, url, is_published=True)
        if result.pdf_bytes:
            return result
        # Try OAI service
        url2 = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{num}/pdf/main.pdf"
        return _wrap_pdf_fetch(self.name, pmcid, url2, is_published=True)


# ---------- A23: Preprint (arXiv/bioRxiv/medRxiv) ----------


class PreprintRetrievalAgent(_SourceAgent):
    name = "A23_preprint_retrieval"

    def _execute(self, payload: RetrievalInput) -> SourceResult:
        ident = payload.identity
        if ident.arxiv_id:
            url = f"https://arxiv.org/pdf/{ident.arxiv_id}"
            return _wrap_pdf_fetch(self.name, ident.arxiv_id, url, is_published=False)
        if ident.preprint_doi:
            doi = ident.preprint_doi
            # bioRxiv / medRxiv pattern 10.1101/...
            if doi.startswith("10.1101/"):
                # Determine server from journal title heuristic
                server = "biorxiv"
                if "medrxiv" in (payload.bibliography.journal or "").lower():
                    server = "medrxiv"
                # bioRxiv stable URL pattern
                url = f"https://www.{server}.org/content/{doi}v1.full.pdf"
                result = _wrap_pdf_fetch(self.name, doi, url, is_published=False)
                if result.pdf_bytes:
                    return result
                # Fallback: try without v1
                url2 = f"https://www.{server}.org/content/{doi}.full.pdf"
                return _wrap_pdf_fetch(self.name, doi, url2, is_published=False)
        return SourceResult(
            source=self.name, error="no preprint id", is_published_version=False
        )


# ---------- A24: Publisher OA ----------


class PublisherOAAgent(_SourceAgent):
    """Direct publisher-OA endpoints. Tries PLOS, Frontiers, MDPI, eLife,
    Nature OA, Wiley OA based on DOI prefix."""

    name = "A24_publisher_oa"

    PUBLISHER_PREFIXES: dict[str, str] = {
        "10.1371/": "plos",
        "10.3389/": "frontiers",
        "10.3390/": "mdpi",
        "10.7554/": "elife",
        "10.1186/": "biomedcentral",
        "10.1038/": "nature-oa",
    }

    def _execute(self, payload: RetrievalInput) -> SourceResult:
        doi = payload.identity.doi
        if not doi:
            return SourceResult(source=self.name, error="no DOI", is_published_version=True)

        publisher: str | None = None
        for prefix, p in self.PUBLISHER_PREFIXES.items():
            if doi.startswith(prefix):
                publisher = p
                break
        if not publisher:
            return SourceResult(
                source=self.name,
                error="no publisher OA endpoint for prefix",
                is_published_version=True,
            )

        url: str | None = None
        if publisher == "plos":
            url = f"https://journals.plos.org/plosone/article/file?id={doi}&type=printable"
        elif publisher == "frontiers":
            # Frontiers /pdf endpoint
            article_id = doi.split("/")[-1]
            url = f"https://www.frontiersin.org/articles/{doi}/pdf"
        elif publisher == "mdpi":
            # MDPI pattern: /pdf
            url = f"https://www.mdpi.com/{doi.split('/', 1)[1]}/pdf"
        elif publisher == "elife":
            article_id = doi.split(".")[-1]
            url = f"https://elifesciences.org/articles/{article_id}.pdf"
        elif publisher == "biomedcentral":
            # BMC: doi resolves
            url = f"https://doi.org/{doi}"
        elif publisher == "nature-oa":
            # Nature open access PDFs aren't predictable; rely on Unpaywall
            return SourceResult(
                source=self.name,
                error="nature PDF needs unpaywall",
                is_published_version=True,
            )
        if not url:
            return SourceResult(
                source=self.name, error="no constructed url", is_published_version=True
            )
        return _wrap_pdf_fetch(self.name, doi, url, is_published=True)


# ---------- A25: UM Library Proxy (Manual variant — COMPLIANCE-LOAD-BEARING) ----------


class UMProxyAgent(_SourceAgent):
    """A25-Manual: generates a CLICKABLE UM proxy URL. Does NOT authenticate,
    does NOT fetch, does NOT bulk-download.

    This variant is intentionally non-automated to comply with UM Libraries'
    Acceptable Use of Electronic Resources Policy, which prohibits "systematic
    or substantial ... downloading" and warns that violations can suspend
    access "for the entire university community." See `cite_forge.proxy` for
    the full compliance docstring.

    Output: SourceResult with pdf_bytes=None and source_url pointing at
    `https://login.access.library.miami.edu/login?url=https://doi.org/{DOI}`.
    The retrieval coordinator (A19) will not select this as a chosen source
    (no PDF bytes), but the URL is preserved for MISSING.md/ESCALATIONS.md
    so the user can click through manually.

    DO NOT add cookie-jar automation, credential storage, or batch fetching
    to this agent. That would violate UM AUP. Use publisher TDM APIs
    (`cite_forge.tdm`, not yet implemented) for any automation needs.
    """

    name = "A25_um_proxy"

    def _execute(self, payload: RetrievalInput) -> SourceResult:
        from cite_forge.proxy import um_proxy_for_doi

        proxy_url: str | None = None
        doi = payload.identity.doi if payload.identity else None
        if doi:
            try:
                proxy_url = um_proxy_for_doi(doi)
            except ValueError:
                proxy_url = None

        return SourceResult(
            source=self.name,
            pdf_bytes=None,
            source_url=proxy_url,
            error=(
                "manual_action_required: click `source_url` in your browser, "
                "authenticate via Shibboleth, save the PDF into the manuscript's "
                "papers/ folder, then run `cite-forge ingest <manuscript-dir>`."
            ),
            is_published_version=True,
        )


# ---------- A26: ILL Fallback ----------


class ILLResult(_Frozen):
    ticket_id: str
    submitted_at: datetime
    expected_delivery_hours: int | None = None


class ILLFallbackAgent(APIAgent[RetrievalInput, ILLResult]):
    name = "A26_ill_fallback"
    wave = "W5"

    def _execute(self, payload: RetrievalInput) -> ILLResult:
        # Non-blocking placeholder — would integrate with DOCLINE/ILLiad.
        # We emit a synthetic ticket so the pipeline can record intent.
        ticket = f"PENDING-{payload.ref_num:03d}-{int(datetime.utcnow().timestamp())}"
        return ILLResult(
            ticket_id=ticket,
            submitted_at=datetime.utcnow(),
            expected_delivery_hours=48,
        )

    def _verify(self, payload: RetrievalInput, output: ILLResult) -> None:
        if not output.ticket_id:
            self.fail("ILL request returned no ticket_id")


# ---------- A19: Retrieval Coordinator (deterministic, unchanged) ----------


class A19Input(_Frozen):
    ref_num: int
    identity: CitationIdentity
    bibliography: Bibliography
    source_results: list[SourceResult]


class A19Output(_Frozen):
    chosen_source: str | None
    chosen_pdf_bytes: bytes | None
    chosen_url: str | None
    rationale: str
    needs_ill: bool


class RetrievalCoordinator(DeterministicAgent[A19Input, A19Output]):
    name = "A19_retrieval_coordinator"
    wave = "W5"

    SOURCE_PRIORITY = (
        ("A21_europepmc", 100),
        ("A22_pmc", 95),
        ("A20_unpaywall", 90),
        ("A07_openalex", 85),
        ("A24_publisher_oa", 80),
        ("A23_preprint_retrieval", 60),
        ("A25_um_proxy", 50),
    )

    def _execute(self, payload: A19Input) -> A19Output:
        hits = [r for r in payload.source_results if r.pdf_bytes is not None]
        if not hits:
            return A19Output(
                chosen_source=None,
                chosen_pdf_bytes=None,
                chosen_url=None,
                rationale="all sources failed",
                needs_ill=True,
            )

        priority_map = dict(self.SOURCE_PRIORITY)
        published_hits = [h for h in hits if h.is_published_version]
        pool = published_hits or hits
        pool_sorted = sorted(
            pool,
            key=lambda r: (
                -priority_map.get(r.source, 0),
                -(len(r.pdf_bytes) if r.pdf_bytes else 0),
            ),
        )
        chosen = pool_sorted[0]
        return A19Output(
            chosen_source=chosen.source,
            chosen_pdf_bytes=chosen.pdf_bytes,
            chosen_url=chosen.source_url,
            rationale=(
                f"selected {chosen.source} "
                f"({'published' if chosen.is_published_version else 'preprint'}, "
                f"{len(chosen.pdf_bytes) if chosen.pdf_bytes else 0} bytes)"
            ),
            needs_ill=False,
        )

    def _verify(self, payload: A19Input, output: A19Output) -> None:
        if output.chosen_source is None and not output.needs_ill:
            self.fail("no chosen source and needs_ill=False")
        if output.chosen_source is not None:
            sources = [r.source for r in payload.source_results]
            if output.chosen_source not in sources:
                self.fail(f"chosen_source {output.chosen_source!r} not in source_results")


# ---------- A27: PDF Verification Agent — THE GATE ----------


class A27Input(_Frozen):
    ref_num: int
    pdf_bytes: bytes
    expected_title: str
    expected_first_author_family: str
    expected_doi: str | None
    expected_year: int


class A27Output(_Frozen):
    title_cosine: float = Field(ge=0.0, le=1.0)
    first_author_matched: bool
    doi_imprint_matched: bool | None
    year_matched: bool
    page_count: int = Field(ge=0)
    has_text_layer: bool
    verified: bool
    verification_methods: list[str]


class PDFVerificationAgent(DeterministicAgent[A27Input, A27Output]):
    """Primary accuracy gate. Opens PDF, extracts title-page text,
    compares against expected metadata. Pure-Python token-cosine + regex —
    no LLM call required for this hot-path check."""

    name = "A27_pdf_verification"
    wave = "W5"

    TITLE_COSINE_THRESHOLD = 0.90

    def _execute(self, payload: A27Input) -> A27Output:
        methods: list[str] = []
        pc = page_count(payload.pdf_bytes)
        htl = has_text_layer(payload.pdf_bytes)
        methods.append(f"page_count={pc}")
        methods.append(f"has_text_layer={htl}")

        if not htl:
            return A27Output(
                title_cosine=0.0,
                first_author_matched=False,
                doi_imprint_matched=None,
                year_matched=False,
                page_count=pc,
                has_text_layer=False,
                verified=False,
                verification_methods=methods,
            )

        # Title cosine — bag-of-tokens against first 3 pages
        first_pages_text = "\n".join(
            extract_page_text(payload.pdf_bytes, i) for i in range(min(3, pc))
        )
        title_cos = cosine_tokens(payload.expected_title, first_pages_text)
        methods.append(f"title_cosine={title_cos:.3f}")

        # First-author surname match — substring on first page
        first_page = extract_page_text(payload.pdf_bytes, 0)
        family = payload.expected_first_author_family
        author_match = bool(
            re.search(rf"\b{re.escape(family)}\b", first_page, flags=re.IGNORECASE)
        )
        methods.append(f"first_author_family_present={author_match}")

        # DOI imprint
        doi_match: bool | None = None
        if payload.expected_doi:
            doi_pattern = re.escape(payload.expected_doi)
            present = bool(
                re.search(doi_pattern, first_pages_text, flags=re.IGNORECASE)
            )
            doi_match = present
            methods.append(f"doi_imprint_present={present}")

        # Year — first 3 pages
        year_match = str(payload.expected_year) in first_pages_text
        methods.append(f"year_present={year_match}")

        verified = (
            title_cos >= self.TITLE_COSINE_THRESHOLD
            and author_match
            and year_match
            and (doi_match is not False)
        )
        return A27Output(
            title_cosine=min(1.0, title_cos),
            first_author_matched=author_match,
            doi_imprint_matched=doi_match,
            year_matched=year_match,
            page_count=pc,
            has_text_layer=htl,
            verified=verified,
            verification_methods=methods,
        )

    def _verify(self, payload: A27Input, output: A27Output) -> None:
        should_verify = (
            output.title_cosine >= self.TITLE_COSINE_THRESHOLD
            and output.first_author_matched
            and output.year_matched
            and output.doi_imprint_matched is not False
            and output.has_text_layer
        )
        if output.verified != should_verify:
            self.fail(
                f"verified={output.verified} inconsistent with measurements "
                f"(cosine={output.title_cosine:.2f}, author_match={output.first_author_matched}, "
                f"doi_match={output.doi_imprint_matched}, year_match={output.year_matched})"
            )
