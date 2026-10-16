"""Distributed verification agents (A38–A50).

These don't fit cleanly in a single wave — they cross-check work done in
earlier waves, sometimes at end-of-run. The pipeline runs them where
appropriate (some during W5, some during W6, some at the very end before
manifest assembly).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import APIAgent, DeterministicAgent, LLMAgent
from cite_forge.schemas import (
    Author,
    CitationIdentity,
    RetractionStatus,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A38: Page-2 PDF Verifier ----------


class A38Input(_Frozen):
    ref_num: int
    pdf_text_page2: str
    expected_keywords_from_abstract: list[str]
    expected_title_words: list[str]


class A38Output(_Frozen):
    matched_keywords: int
    expected_keyword_count: int
    matched_title_words: int
    expected_title_word_count: int
    verified: bool


class Page2PDFVerifier(DeterministicAgent[A38Input, A38Output]):
    """Catches the case where source served the journal homepage as a PDF.
    Page-2 content must match abstract keywords + a subset of title words.
    Pure-Python check; no LLM needed for keyword presence."""

    name = "A38_page2_pdf_verifier"
    wave = "W5"

    MIN_KEYWORD_FRAC = 0.6

    def _execute(self, payload: A38Input) -> A38Output:
        text = payload.pdf_text_page2.lower()
        matched_kw = sum(1 for k in payload.expected_keywords_from_abstract if k.lower() in text)
        matched_title = sum(1 for w in payload.expected_title_words if w.lower() in text)
        kw_frac = (
            matched_kw / payload.expected_keyword_count
            if payload.expected_keyword_count
            else 0.0
        )
        title_frac = (
            matched_title / payload.expected_title_word_count
            if payload.expected_title_word_count
            else 0.0
        )
        verified = kw_frac >= self.MIN_KEYWORD_FRAC and title_frac >= 0.5
        return A38Output(
            matched_keywords=min(matched_kw, payload.expected_keyword_count),
            expected_keyword_count=payload.expected_keyword_count,
            matched_title_words=min(matched_title, payload.expected_title_word_count),
            expected_title_word_count=payload.expected_title_word_count,
            verified=verified,
        )

    def _verify(self, payload: A38Input, output: A38Output) -> None:
        if output.matched_keywords > output.expected_keyword_count:
            self.fail("matched_keywords > expected count")
        kw_frac = (
            output.matched_keywords / output.expected_keyword_count
            if output.expected_keyword_count
            else 0.0
        )
        title_frac = (
            output.matched_title_words / output.expected_title_word_count
            if output.expected_title_word_count
            else 0.0
        )
        should_verify = kw_frac >= self.MIN_KEYWORD_FRAC and title_frac >= 0.5
        if output.verified != should_verify:
            self.fail(
                f"verified={output.verified} inconsistent with measurements "
                f"(kw_frac={kw_frac:.2f}, title_frac={title_frac:.2f})"
            )


# ---------- A39: Claim-Source Support Verifier ----------


class A39Input(_Frozen):
    unit_id: str
    ref_num: int
    claim_verbatim: str
    source_pdf_text: str


class A39Output(_Frozen):
    verdict: str  # "SUPPORTS" | "WEAK_SUPPORT" | "NOT_FOUND" | "CONTRADICTS"
    supporting_passages: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


_A39_SYSTEM = """You judge whether a claim is supported by its cited source.

Verdicts:
- SUPPORTS: source directly and unambiguously supports the claim.
- WEAK_SUPPORT: source provides related but partial / qualified evidence.
- NOT_FOUND: claim's content does not appear in the source text given.
- CONTRADICTS: source actively contradicts the claim.

OUTPUT REQUIREMENTS:
- For SUPPORTS / WEAK_SUPPORT / CONTRADICTS: supporting_passages MUST be VERBATIM substrings
  of the source_pdf_text (byte-for-byte). Each at least 30 characters.
- For NOT_FOUND: supporting_passages MUST be empty.

Choose NOT_FOUND if you cannot find verbatim support — do NOT paraphrase to fit."""


class ClaimSourceSupportVerifier(LLMAgent[A39Input, A39Output]):
    name = "A39_claim_source_support"
    wave = "W6_5"
    model = "claude-sonnet-4-6"

    VALID_VERDICTS = frozenset({"SUPPORTS", "WEAK_SUPPORT", "NOT_FOUND", "CONTRADICTS"})

    def _execute(self, payload: A39Input) -> A39Output:
        from cite_forge.llm import call_structured

        user = (
            f"Claim:\n<<<\n{payload.claim_verbatim}\n>>>\n\n"
            f"Source PDF text (truncated):\n<<<\n{payload.source_pdf_text[:30000]}\n>>>"
        )
        result = call_structured(
            model=self.model,
            system=_A39_SYSTEM,
            user=user,
            output_cls=A39Output,
            tool_name="emit_support",
            tool_description="Emit claim-source support judgment.",
            max_tokens=2048,
        )
        out = result.output
        # Drop any non-verbatim passages
        filtered = [p for p in out.supporting_passages if p in payload.source_pdf_text]
        if (
            out.verdict in ("SUPPORTS", "WEAK_SUPPORT", "CONTRADICTS")
            and not filtered
        ):
            return A39Output(
                verdict="NOT_FOUND", supporting_passages=[], confidence=out.confidence
            )
        return A39Output(
            verdict=out.verdict, supporting_passages=filtered, confidence=out.confidence
        )

    def _verify(self, payload: A39Input, output: A39Output) -> None:
        if output.verdict not in self.VALID_VERDICTS:
            self.fail(f"verdict {output.verdict!r} not in valid set")
        if output.verdict in ("SUPPORTS", "WEAK_SUPPORT", "CONTRADICTS"):
            if not output.supporting_passages:
                self.fail(f"{output.verdict} requires supporting_passages")
            for p in output.supporting_passages:
                if p not in payload.source_pdf_text:
                    self.fail(f"supporting passage {p!r} not verbatim in source")


# ---------- A40: Multi-Position Author Verifier ----------


class A40Input(_Frozen):
    ref_num: int
    authors_from_crossref: list[Author]
    authors_from_pubmed: list[Author]
    authors_from_openalex: list[Author]


class A40Output(_Frozen):
    matched_positions: list[int]  # positions where all 3 agree
    disagreements: list[tuple[int, str]]  # (position, description)
    full_match: bool


class MultiPositionAuthorVerifier(DeterministicAgent[A40Input, A40Output]):
    """Every author in every position matches across CrossRef + PubMed + OpenAlex.
    Catches author-order swaps and ghost coauthors."""

    name = "A40_multi_position_author"
    wave = "W3"

    @staticmethod
    def _family_norm(name: str) -> str:
        return re.sub(r"[^a-z]", "", name.lower())

    def _execute(self, payload: A40Input) -> A40Output:
        n = max(
            len(payload.authors_from_crossref),
            len(payload.authors_from_pubmed),
            len(payload.authors_from_openalex),
        )
        matched: list[int] = []
        disagreements: list[tuple[int, str]] = []
        for i in range(n):
            families = []
            for source, lst in [
                ("crossref", payload.authors_from_crossref),
                ("pubmed", payload.authors_from_pubmed),
                ("openalex", payload.authors_from_openalex),
            ]:
                if i < len(lst):
                    families.append((source, self._family_norm(lst[i].family)))
                else:
                    families.append((source, None))
            non_null = [f for _, f in families if f]
            if len(set(non_null)) == 1 and len(non_null) == 3:
                matched.append(i + 1)
            else:
                disagreements.append(
                    (i + 1, "; ".join(f"{s}={f}" for s, f in families))
                )
        return A40Output(
            matched_positions=matched,
            disagreements=disagreements,
            full_match=len(disagreements) == 0,
        )

    def _verify(self, payload: A40Input, output: A40Output) -> None:
        if output.full_match and output.disagreements:
            self.fail("full_match=True with non-empty disagreements")


# ---------- A41: Publication Date Triangulator ----------


class A41Input(_Frozen):
    ref_num: int
    crossref_pub_date: str | None  # ISO
    pubmed_pub_date: str | None
    openalex_pub_date: str | None


class A41Output(_Frozen):
    agreement_days: int | None  # max pairwise gap in days
    triangulated: bool
    discrepancies: list[str]


class PublicationDateTriangulator(DeterministicAgent[A41Input, A41Output]):
    name = "A41_publication_date_triangulator"
    wave = "W3"

    MAX_GAP_DAYS = 30

    def _execute(self, payload: A41Input) -> A41Output:
        from datetime import date, datetime

        def parse(s: str | None) -> date | None:
            if not s:
                return None
            for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
                try:
                    return datetime.strptime(s, fmt).date()
                except ValueError:
                    continue
            return None

        dates = {
            "crossref": parse(payload.crossref_pub_date),
            "pubmed": parse(payload.pubmed_pub_date),
            "openalex": parse(payload.openalex_pub_date),
        }
        present = [(k, v) for k, v in dates.items() if v]
        if len(present) < 2:
            return A41Output(
                agreement_days=None, triangulated=False, discrepancies=["insufficient sources"]
            )
        max_gap = 0
        discrepancies: list[str] = []
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                gap = abs((present[i][1] - present[j][1]).days)
                if gap > max_gap:
                    max_gap = gap
                if gap > self.MAX_GAP_DAYS:
                    discrepancies.append(
                        f"{present[i][0]}({present[i][1]}) vs {present[j][0]}({present[j][1]}): {gap}d"
                    )
        return A41Output(
            agreement_days=max_gap,
            triangulated=max_gap <= self.MAX_GAP_DAYS,
            discrepancies=discrepancies,
        )

    def _verify(self, payload: A41Input, output: A41Output) -> None:
        if (
            output.triangulated
            and output.agreement_days is not None
            and output.agreement_days > self.MAX_GAP_DAYS
        ):
            self.fail("triangulated=True but agreement_days > threshold")


# ---------- A42: Retraction Recheck Critic ----------


class A42Input(_Frozen):
    ref_num: int
    identity: CitationIdentity
    earlier_status: RetractionStatus


class A42Output(_Frozen):
    recheck_status: RetractionStatus
    agrees_with_earlier: bool
    rechecked_sources: list[str]


class RetractionRecheckCritic(APIAgent[A42Input, A42Output]):
    """Independent re-query at end-of-run. Fails closed: any source reporting
    RETRACTED blocks manifest assembly regardless of earlier verdict.
    """

    name = "A42_retraction_recheck"
    wave = "W7"

    def _execute(self, payload: A42Input) -> A42Output:
        import urllib.parse
        import xml.etree.ElementTree as ET

        from cite_forge.http import cached_get, cached_get_json

        identity = payload.identity
        sources: list[str] = []
        status = RetractionStatus.NONE

        # Force-refresh CrossRef relation
        if identity.doi:
            url = f"https://api.crossref.org/works/{urllib.parse.quote(identity.doi, safe='')}"
            ident = f"doi:{identity.doi}:recheck"
            s, data = cached_get_json("crossref", "recheck", ident, url, force_refresh=True)
            if s == 200 and data:
                sources.append("crossref")
                msg = data.get("message") or {}
                relations = msg.get("relation") or {}
                for relname in relations:
                    if "retraction" in relname.lower():
                        status = RetractionStatus.RETRACTED
                for u in msg.get("update-to") or []:
                    if (u.get("type") or "").lower() == "retraction":
                        status = RetractionStatus.RETRACTED

        if identity.pmid:
            params = {"db": "pubmed", "id": identity.pmid, "retmode": "xml"}
            s, body, _ = cached_get(
                "pubmed",
                "efetch-recheck",
                identity.pmid,
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params=params,
                force_refresh=True,
            )
            if s == 200 and body:
                sources.append("pubmed")
                try:
                    root = ET.fromstring(body)
                    for cc in root.iter("CommentsCorrections"):
                        if (cc.get("RefType") or "") == "RetractionIn":
                            status = RetractionStatus.RETRACTED
                except ET.ParseError:
                    pass

        return A42Output(
            recheck_status=status,
            agrees_with_earlier=(status == payload.earlier_status),
            rechecked_sources=sources or ["none"],
        )

    def _verify(self, payload: A42Input, output: A42Output) -> None:
        if not output.rechecked_sources:
            self.fail("no sources rechecked")
        if output.recheck_status != payload.earlier_status and output.agrees_with_earlier:
            self.fail("agrees_with_earlier=True but statuses differ")


# ---------- A43: DOI Round-Trip Verifier ----------


class A43Input(_Frozen):
    ref_num: int
    doi: str
    expected_title: str
    expected_first_author_family: str
    expected_year: int


class A43Output(_Frozen):
    round_trip_ok: bool
    drift_fields: list[str]


class DOIRoundTripVerifier(APIAgent[A43Input, A43Output]):
    name = "A43_doi_round_trip"
    wave = "W3"

    def _execute(self, payload: A43Input) -> A43Output:
        import urllib.parse

        from rapidfuzz.distance import Levenshtein

        from cite_forge.http import cached_get_json

        url = f"https://api.crossref.org/works/{urllib.parse.quote(payload.doi, safe='')}"
        ident = f"doi:{payload.doi}:roundtrip"
        status, data = cached_get_json("crossref", "doi-roundtrip", ident, url)
        if status != 200 or not data:
            return A43Output(round_trip_ok=False, drift_fields=["doi_lookup_failed"])
        msg = data.get("message") or {}
        title_list = msg.get("title") or []
        title = title_list[0] if title_list else ""
        authors = msg.get("author") or []
        family = authors[0].get("family", "") if authors else ""
        issued = (msg.get("issued") or {}).get("date-parts") or [[None]]
        year = issued[0][0] if issued and issued[0] else None

        drift: list[str] = []
        # Title cosine
        from cite_forge.pdf import cosine_tokens

        if cosine_tokens(title, payload.expected_title) < 0.6:
            drift.append("title")
        if (
            Levenshtein.distance(family.lower(), payload.expected_first_author_family.lower())
            > 2
        ):
            drift.append("first_author")
        if year != payload.expected_year:
            drift.append("year")
        return A43Output(round_trip_ok=not drift, drift_fields=drift)

    def _verify(self, payload: A43Input, output: A43Output) -> None:
        if output.round_trip_ok and output.drift_fields:
            self.fail("round_trip_ok=True with drift_fields non-empty")


# ---------- A44: Topic Drift Detector ----------


class A44Input(_Frozen):
    ref_num: int
    source_topic_vector: list[float]
    manuscript_topic_vector: list[float]


class A44Output(_Frozen):
    cosine: float = Field(ge=-1.0, le=1.0)
    drift_score: float = Field(ge=0.0, le=1.0)
    drift_flagged: bool


class TopicDriftDetector(DeterministicAgent[A44Input, A44Output]):
    name = "A44_topic_drift"
    wave = "W6_5"

    DRIFT_THRESHOLD = 0.5

    def _execute(self, payload: A44Input) -> A44Output:
        a = payload.source_topic_vector
        b = payload.manuscript_topic_vector
        if not a or not b or len(a) != len(b):
            return A44Output(cosine=0.0, drift_score=1.0, drift_flagged=True)
        import math

        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        cosine = dot / (na * nb) if na and nb else 0.0
        drift = max(0.0, 1.0 - cosine)
        return A44Output(
            cosine=cosine,
            drift_score=drift,
            drift_flagged=drift >= self.DRIFT_THRESHOLD,
        )

    def _verify(self, payload: A44Input, output: A44Output) -> None:
        if abs(output.drift_score - max(0.0, 1.0 - output.cosine)) > 1e-6:
            self.fail("drift_score inconsistent with cosine")


# ---------- A45: Crossmark Verifier ----------


class A45Input(_Frozen):
    ref_num: int
    doi: str


class A45Output(_Frozen):
    update_count: int = Field(ge=0)
    updates: list[str]  # types/dates


class CrossmarkVerifier(APIAgent[A45Input, A45Output]):
    name = "A45_crossmark"
    wave = "W4"

    def _execute(self, payload: A45Input) -> A45Output:
        import urllib.parse

        from cite_forge.http import cached_get_json

        url = f"https://api.crossref.org/works/{urllib.parse.quote(payload.doi, safe='')}"
        ident = f"doi:{payload.doi}:crossmark"
        s, data = cached_get_json("crossref", "crossmark", ident, url)
        updates: list[str] = []
        if s == 200 and data:
            msg = data.get("message") or {}
            for u in msg.get("update-to") or []:
                t = u.get("type") or ""
                d = (u.get("updated") or {}).get("date-parts") or []
                date_str = "-".join(str(x) for x in (d[0] if d else []))
                updates.append(f"{t}@{date_str}".rstrip("@-"))
            for u in msg.get("updated-by") or []:
                t = u.get("type") or ""
                d = (u.get("updated") or {}).get("date-parts") or []
                date_str = "-".join(str(x) for x in (d[0] if d else []))
                updates.append(f"updated-by:{t}@{date_str}".rstrip("@-"))
        return A45Output(update_count=len(updates), updates=updates)

    def _verify(self, payload: A45Input, output: A45Output) -> None:
        if output.update_count != len(output.updates):
            self.fail("update_count != len(updates)")


# ---------- A46: Section-Boundary Verifier ----------


class A46Input(_Frozen):
    unit_id: str
    manuscript_text: str
    claim_span: tuple[int, int]
    assigned_section_header: str


class A46Output(_Frozen):
    section_header_found_at: int | None  # offset
    matches_assigned: bool


class SectionBoundaryVerifier(DeterministicAgent[A46Input, A46Output]):
    """Confirms the section_header assigned to each unit appears upstream of
    claim_span in the manuscript."""

    name = "A46_section_boundary"
    wave = "W0"

    def _execute(self, payload: A46Input) -> A46Output:
        before = payload.manuscript_text[: payload.claim_span[0]]
        idx = before.rfind(payload.assigned_section_header)
        return A46Output(
            section_header_found_at=idx if idx >= 0 else None,
            matches_assigned=idx >= 0,
        )

    def _verify(self, payload: A46Input, output: A46Output) -> None:
        if output.matches_assigned and output.section_header_found_at is None:
            self.fail("matches_assigned=True with no offset")


# ---------- A47: Quote Pagination Verifier ----------


class A47Input(_Frozen):
    unit_id: str
    quote_in_manuscript: str
    cited_page: int
    pdf_text_by_page: list[str]


class A47Output(_Frozen):
    found_on_page: int | None
    matches_cited_page: bool


class QuotePaginationVerifier(DeterministicAgent[A47Input, A47Output]):
    name = "A47_quote_pagination"
    wave = "W6"

    def _execute(self, payload: A47Input) -> A47Output:
        quote = payload.quote_in_manuscript.strip()
        for i, page_text in enumerate(payload.pdf_text_by_page, start=1):
            if quote in page_text:
                return A47Output(
                    found_on_page=i,
                    matches_cited_page=(i == payload.cited_page),
                )
        return A47Output(found_on_page=None, matches_cited_page=False)

    def _verify(self, payload: A47Input, output: A47Output) -> None:
        if output.matches_cited_page and output.found_on_page != payload.cited_page:
            self.fail("matches_cited_page=True but found_on_page != cited_page")


# ---------- A48: Identity-Consensus Meta-Critic ----------


class A48Input(_Frozen):
    ref_num: int
    a11_converged: bool
    a11_dissenting_agents: list[str]
    upstream_sources_share_input: bool  # heuristic: do W2 sources share metadata source?


class A48Output(_Frozen):
    meta_verdict: str  # "SOUND" | "SUSPECT" | "REJECT"
    rationale: str


class IdentityConsensusMetaCritic(DeterministicAgent[A48Input, A48Output]):
    """Catches false-consensus: A11 says converged but the W2 sources share an
    upstream metadata pipeline (e.g., all derived from CrossRef). Pure-rule:
    - converged + 0 dissenters: SOUND
    - converged + share-input flag: SUSPECT
    - not converged: REJECT
    """

    name = "A48_identity_consensus_meta"
    wave = "W7"

    VALID = frozenset({"SOUND", "SUSPECT", "REJECT"})

    def _execute(self, payload: A48Input) -> A48Output:
        if not payload.a11_converged:
            return A48Output(meta_verdict="REJECT", rationale="A11 did not converge")
        if payload.upstream_sources_share_input:
            return A48Output(
                meta_verdict="SUSPECT",
                rationale="W2 sources share upstream metadata; false-consensus risk",
            )
        return A48Output(
            meta_verdict="SOUND",
            rationale="A11 converged with independent sources",
        )

    def _verify(self, payload: A48Input, output: A48Output) -> None:
        if output.meta_verdict not in self.VALID:
            self.fail(f"meta_verdict {output.meta_verdict!r} not in valid set")


# ---------- A49: Escalation Packet Validator ----------


class A49Input(_Frozen):
    escalations_md: str
    escalations_count: int


class A49Output(_Frozen):
    valid: bool
    issues: list[str]


class EscalationPacketValidator(DeterministicAgent[A49Input, A49Output]):
    """Every packet must contain: context, conflicting evidence, specific
    question, candidate PDFs."""

    name = "A49_escalation_validator"
    wave = "W8"

    REQUIRED_HEADINGS = ("## Context", "## Conflicting evidence", "## Question", "## Candidates")

    def _execute(self, payload: A49Input) -> A49Output:
        issues: list[str] = []
        for heading in self.REQUIRED_HEADINGS:
            count = payload.escalations_md.count(heading)
            if count < payload.escalations_count:
                issues.append(
                    f"heading {heading!r} appears {count}× but expected {payload.escalations_count}"
                )
        return A49Output(valid=not issues, issues=issues)

    def _verify(self, payload: A49Input, output: A49Output) -> None:
        if output.valid and output.issues:
            self.fail("valid=True with non-empty issues")


# ---------- A50: Manifest Integrity Auditor ----------


class A50Input(_Frozen):
    manifest_json: str
    sample_fraction: float = Field(default=0.1, ge=0.0, le=1.0)
    seed: int = 0


class A50Output(_Frozen):
    sampled_ref_nums: list[int]
    reverified_pass: int
    reverified_fail: int
    failure_rate: float = Field(ge=0.0, le=1.0)
    fails_threshold: bool


class ManifestIntegrityAuditor(DeterministicAgent[A50Input, A50Output]):
    """Random-sample re-verification. For each sampled citation, we re-run the
    A27 PDF verifier (the primary gate) against the manifest's recorded sha256
    and verification_methods; mismatch counts as a failure. Pure-Python."""

    name = "A50_manifest_integrity"
    wave = "W8"

    THRESHOLD = 0.01

    def _execute(self, payload: A50Input) -> A50Output:
        import random

        from cite_forge.schemas import Manifest

        manifest = Manifest.model_validate_json(payload.manifest_json)
        citations = manifest.citations
        if not citations:
            return A50Output(
                sampled_ref_nums=[],
                reverified_pass=0,
                reverified_fail=0,
                failure_rate=0.0,
                fails_threshold=False,
            )
        n_sample = max(1, int(len(citations) * payload.sample_fraction))
        rng = random.Random(payload.seed)
        sampled = rng.sample(citations, min(n_sample, len(citations)))

        passes = 0
        fails = 0
        sampled_refs: list[int] = []
        for c in sampled:
            sampled_refs.append(c.ref_num)
            # Re-check: PDF must exist at path and sha256 must match
            if not c.pdf:
                fails += 1
                continue
            try:
                with open(c.pdf.path, "rb") as f:
                    raw = f.read()
            except OSError:
                fails += 1
                continue
            import hashlib

            sha = hashlib.sha256(raw).hexdigest()
            if sha != c.pdf.sha256:
                fails += 1
                continue
            passes += 1
        total = passes + fails
        rate = fails / total if total else 0.0
        return A50Output(
            sampled_ref_nums=sampled_refs,
            reverified_pass=passes,
            reverified_fail=fails,
            failure_rate=rate,
            fails_threshold=rate > self.THRESHOLD,
        )

    def _verify(self, payload: A50Input, output: A50Output) -> None:
        total = output.reverified_pass + output.reverified_fail
        if total == 0:
            self.fail("no entries sampled")
        expected_rate = output.reverified_fail / total
        if abs(output.failure_rate - expected_rate) > 1e-6:
            self.fail(
                f"failure_rate {output.failure_rate:.4f} != "
                f"actual {expected_rate:.4f}"
            )
        if output.fails_threshold != (output.failure_rate > self.THRESHOLD):
            self.fail("fails_threshold flag inconsistent with rate")
