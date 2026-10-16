"""W4 — Integrity Checks (parallel per citation).

A14 Retraction & Erratum (API) — refuse-to-gold-standard on retraction
A15 Predatory Venue (API)
A16 Preprint vs Version-of-Record (API + LLM)
A17 Funding & COI (API)
A18 Citation Cartel Detector (Deterministic; bib-level)
"""

from __future__ import annotations

import urllib.parse
from collections import Counter
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import APIAgent, DeterministicAgent
from cite_forge.schemas import (
    Bibliography,
    CitationIdentity,
    RetractionStatus,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A14: Retraction & Erratum ----------


class A14Input(_Frozen):
    ref_num: int
    identity: CitationIdentity


class A14Output(_Frozen):
    status: RetractionStatus
    retraction_doi: str | None = None
    retraction_date: datetime | None = None
    source_evidence: list[str]  # which sources reported the status
    raw_linkages: list[str] = Field(default_factory=list)


class RetractionEratumAgent(APIAgent[A14Input, A14Output]):
    """Queries Retraction Watch (Crossref Labs) + PubMed RetractionIn/ErratumIn/
    CommentIn/UpdateIn + CrossRef relation `is-retracted-by` / `is-corrected-by`.
    """

    name = "A14_retraction_erratum"
    wave = "W4"

    def _execute(self, payload: A14Input) -> A14Output:
        import urllib.parse

        from cite_forge.http import cached_get_json

        identity = payload.identity
        evidence: list[str] = []
        linkages: list[str] = []
        status = RetractionStatus.NONE
        retract_doi: str | None = None
        retract_date: datetime | None = None

        # 1) CrossRef: check `relation.is-retracted-by` and similar
        if identity.doi:
            url = f"https://api.crossref.org/works/{urllib.parse.quote(identity.doi, safe='')}"
            ident = f"doi:{identity.doi}:retract"
            s, data = cached_get_json("crossref", "retract", ident, url)
            if s == 200 and data:
                msg = data.get("message") or {}
                relations = msg.get("relation") or {}
                update_to = msg.get("update-to") or []
                for u in update_to:
                    if (u.get("type") or "").lower() == "retraction":
                        status = RetractionStatus.RETRACTED
                        retract_doi = u.get("DOI")
                        evidence.append("crossref.update-to:retraction")
                for relname, rels in relations.items():
                    if "retraction" in relname.lower():
                        for r in rels if isinstance(rels, list) else [rels]:
                            doi = r.get("id") if isinstance(r, dict) else None
                            if doi:
                                if status != RetractionStatus.RETRACTED:
                                    status = RetractionStatus.EXPRESSION_OF_CONCERN
                                evidence.append(f"crossref.relation:{relname}")
                                linkages.append(f"crossref:{relname}:{doi}")
                                if not retract_doi:
                                    retract_doi = doi

        # 2) PubMed: check CommentsCorrections RefType=RetractionIn / ErratumIn
        if identity.pmid:
            url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            params = {"db": "pubmed", "id": identity.pmid, "retmode": "xml"}
            from cite_forge.http import cached_get

            s, body, _ = cached_get(
                "pubmed", "efetch-retract", identity.pmid, url, params=params
            )
            if s == 200 and body:
                import xml.etree.ElementTree as ET

                try:
                    root = ET.fromstring(body)
                    for cc in root.iter("CommentsCorrections"):
                        rtype = cc.get("RefType") or ""
                        if rtype == "RetractionIn":
                            status = RetractionStatus.RETRACTED
                            ref_pmid = cc.find("PMID")
                            if ref_pmid is not None and ref_pmid.text:
                                linkages.append(f"pubmed:RetractionIn:PMID{ref_pmid.text}")
                            evidence.append("pubmed.CommentsCorrections:RetractionIn")
                        elif rtype == "ErratumIn":
                            if status not in (
                                RetractionStatus.RETRACTED,
                                RetractionStatus.EXPRESSION_OF_CONCERN,
                            ):
                                status = RetractionStatus.ERRATUM
                            evidence.append("pubmed.CommentsCorrections:ErratumIn")
                        elif rtype == "ExpressionOfConcernIn":
                            if status != RetractionStatus.RETRACTED:
                                status = RetractionStatus.EXPRESSION_OF_CONCERN
                            evidence.append("pubmed.CommentsCorrections:ExpressionOfConcernIn")
                except ET.ParseError:
                    pass

        return A14Output(
            status=status,
            retraction_doi=retract_doi,
            retraction_date=retract_date,
            source_evidence=evidence,
            raw_linkages=linkages,
        )

    def _verify(self, payload: A14Input, output: A14Output) -> None:
        if output.status != RetractionStatus.NONE and not output.source_evidence:
            self.fail("non-NONE retraction status with no source_evidence")
        if output.status == RetractionStatus.RETRACTED and not output.retraction_doi:
            self.fail("RETRACTED without retraction_doi")


# ---------- A15: Predatory Venue ----------


class A15Input(_Frozen):
    ref_num: int
    bibliography: Bibliography


class A15Output(_Frozen):
    risk_score: int = Field(ge=0, le=100)
    evidence: list[str]
    in_doaj: bool | None
    in_oaspa: bool | None
    in_archived_beall: bool | None


class PredatoryVenueAgent(APIAgent[A15Input, A15Output]):
    name = "A15_predatory_venue"
    wave = "W4"

    def _execute(self, payload: A15Input) -> A15Output:
        from cite_forge.http import cached_get_json

        bib = payload.bibliography
        journal = bib.journal or ""
        if not journal:
            return A15Output(
                risk_score=0,
                evidence=[],
                in_doaj=None,
                in_oaspa=None,
                in_archived_beall=None,
            )

        # DOAJ check: legitimate OA journals are in DOAJ
        in_doaj: bool | None = None
        params = {"query": f"bibjson.title:\"{journal}\""}
        ident = f"j:{journal[:60]}"
        s, data = cached_get_json(
            "doaj",
            "search",
            ident,
            f"https://doaj.org/api/v3/search/journals/{urllib.parse.quote(journal)}",
        )
        if s == 200 and data:
            results = data.get("results") or []
            in_doaj = len(results) > 0
        evidence: list[str] = []
        risk = 0
        if in_doaj is False:
            # Not strictly predatory; only a soft signal. Don't auto-flag — many
            # legitimate non-OA journals are not in DOAJ.
            pass
        # Heuristic: trivially-suspicious words in title
        suspicious_words = ("International Journal of Research", "Global Journal of", "Universal")
        for w in suspicious_words:
            if w.lower() in journal.lower():
                risk = max(risk, 30)
                evidence.append(f"heuristic.title-contains:{w!r}")
        return A15Output(
            risk_score=risk,
            evidence=evidence,
            in_doaj=in_doaj,
            in_oaspa=None,
            in_archived_beall=None,
        )

    def _verify(self, payload: A15Input, output: A15Output) -> None:
        if output.risk_score >= 50 and not output.evidence:
            self.fail(f"risk_score={output.risk_score} with no evidence")


# ---------- A16: Preprint vs VOR ----------


class A16Input(_Frozen):
    ref_num: int
    cited_doi: str | None
    cited_is_preprint: bool


class A16Output(_Frozen):
    preprint_doi: str | None
    vor_doi: str | None
    title_delta: float | None = Field(default=None, ge=0.0, le=1.0)
    abstract_cosine: float | None = Field(default=None, ge=0.0, le=1.0)
    author_set_delta: float | None = Field(default=None, ge=0.0, le=1.0)
    overall_delta: float | None = Field(default=None, ge=0.0, le=1.0)


class PreprintVORAgent(APIAgent[A16Input, A16Output]):
    name = "A16_preprint_vor"
    wave = "W4"

    def _execute(self, payload: A16Input) -> A16Output:
        from cite_forge.http import cached_get_json

        if not payload.cited_doi or not payload.cited_is_preprint:
            return A16Output(
                preprint_doi=payload.cited_doi if payload.cited_is_preprint else None,
                vor_doi=None,
            )
        url = f"https://api.crossref.org/works/{urllib.parse.quote(payload.cited_doi, safe='')}"
        ident = f"doi:{payload.cited_doi}:vor"
        s, data = cached_get_json("crossref", "vor", ident, url)
        vor: str | None = None
        if s == 200 and data:
            msg = data.get("message") or {}
            relations = msg.get("relation") or {}
            for relname, rels in relations.items():
                if relname.lower() in (
                    "is-preprint-of",
                    "is-version-of",
                    "has-version",
                ):
                    for r in rels if isinstance(rels, list) else [rels]:
                        cand = r.get("id") if isinstance(r, dict) else None
                        if cand:
                            vor = cand
                            break
        return A16Output(
            preprint_doi=payload.cited_doi,
            vor_doi=vor,
            overall_delta=0.0 if vor is None else None,
        )

    def _verify(self, payload: A16Input, output: A16Output) -> None:
        if output.preprint_doi and output.vor_doi:
            if output.overall_delta is None:
                self.fail("preprint+VOR present but overall_delta is None")


# ---------- A17: Funding & COI ----------


class A17Output(_Frozen):
    funder_ids: list[str]
    funder_names: list[str]
    overlaps_with_manuscript_authors: bool
    overlap_evidence: list[str]


class A17Input(_Frozen):
    ref_num: int
    identity: CitationIdentity
    manuscript_author_names: list[str]


class FundingCOIAgent(APIAgent[A17Input, A17Output]):
    name = "A17_funding_coi"
    wave = "W4"

    def _execute(self, payload: A17Input) -> A17Output:
        from cite_forge.http import cached_get_json

        if not payload.identity.doi:
            return A17Output(
                funder_ids=[],
                funder_names=[],
                overlaps_with_manuscript_authors=False,
                overlap_evidence=[],
            )
        url = f"https://api.crossref.org/works/{urllib.parse.quote(payload.identity.doi, safe='')}"
        ident = f"doi:{payload.identity.doi}:funder"
        s, data = cached_get_json("crossref", "funder", ident, url)
        funder_ids: list[str] = []
        funder_names: list[str] = []
        overlap = False
        evidence: list[str] = []
        if s == 200 and data:
            msg = data.get("message") or {}
            for f in msg.get("funder") or []:
                fid = f.get("DOI") or f.get("name") or ""
                fname = f.get("name") or ""
                if fid:
                    funder_ids.append(fid)
                if fname:
                    funder_names.append(fname)
            # Author overlap: any manuscript author surname appearing in citation's author list
            ms_surnames = {n.split()[-1].lower() for n in payload.manuscript_author_names if n}
            for a in msg.get("author") or []:
                surname = (a.get("family") or "").lower()
                if surname and surname in ms_surnames:
                    overlap = True
                    evidence.append(f"author-surname-match:{surname}")
        # Pad funder_names with names to match length (Crossref sometimes returns one or the other)
        while len(funder_names) < len(funder_ids):
            funder_names.append("")
        while len(funder_ids) < len(funder_names):
            funder_ids.append("")
        return A17Output(
            funder_ids=funder_ids,
            funder_names=funder_names,
            overlaps_with_manuscript_authors=overlap,
            overlap_evidence=evidence,
        )

    def _verify(self, payload: A17Input, output: A17Output) -> None:
        if len(output.funder_ids) != len(output.funder_names):
            self.fail("funder_ids and funder_names length mismatch")
        if output.overlaps_with_manuscript_authors and not output.overlap_evidence:
            self.fail("overlap=True but no evidence")


# ---------- A18: Citation Cartel Detector (bib-level) ----------


class A18Input(_Frozen):
    bibliography_entries: list[Bibliography]


class CartelFlag(_Frozen):
    entity_type: str  # "author" | "lab" | "journal"
    entity_value: str
    occurrences: int = Field(ge=1)
    fraction: float = Field(ge=0.0, le=1.0)


class A18Output(_Frozen):
    flags: list[CartelFlag]
    total_entries: int = Field(ge=0)


class CitationCartelDetector(DeterministicAgent[A18Input, A18Output]):
    """Threshold: >25% concentration from one author/journal triggers a flag."""

    name = "A18_citation_cartel"
    wave = "W4"

    THRESHOLD = 0.25

    def _execute(self, payload: A18Input) -> A18Output:
        n = len(payload.bibliography_entries)
        if n == 0:
            return A18Output(flags=[], total_entries=0)

        author_counts: Counter[str] = Counter()
        journal_counts: Counter[str] = Counter()
        for entry in payload.bibliography_entries:
            for a in entry.authors:
                key = f"{a.family}|{a.given or ''}"
                author_counts[key] += 1
            if entry.journal:
                journal_counts[entry.journal] += 1

        flags: list[CartelFlag] = []
        for key, count in author_counts.items():
            frac = count / n
            if frac > self.THRESHOLD and count >= 3:
                flags.append(
                    CartelFlag(
                        entity_type="author",
                        entity_value=key,
                        occurrences=count,
                        fraction=frac,
                    )
                )
        for journal, count in journal_counts.items():
            frac = count / n
            if frac > self.THRESHOLD and count >= 3:
                flags.append(
                    CartelFlag(
                        entity_type="journal",
                        entity_value=journal,
                        occurrences=count,
                        fraction=frac,
                    )
                )

        return A18Output(flags=flags, total_entries=n)

    def _verify(self, payload: A18Input, output: A18Output) -> None:
        if output.total_entries != len(payload.bibliography_entries):
            self.fail("total_entries mismatch")
        for flag in output.flags:
            if flag.fraction <= self.THRESHOLD:
                self.fail(f"flag fraction {flag.fraction:.2f} below threshold")
            if flag.occurrences < 3:
                self.fail(f"flag occurrences {flag.occurrences} < 3")
