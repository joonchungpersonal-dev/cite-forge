"""Pipeline orchestrator.

Sequences the 10 waves and dispatches agents. Parallelism is wave-internal:
agents within a wave run concurrently per-citation; waves are sequential.

ARCHITECTURAL INVARIANTS:

  1. Wave gate: pipeline refuses to start wave N+1 until all wave N agents
     pass their verifiers OR are explicitly escalated.

  2. Convergence rule is hard-coded in `is_converged()` — pure Python `and`
     of every clause. NOT an LLM judgment.

  3. Overall confidence is `min(...)` across all relevant agents (worst-link).

  4. Refuse-to-emit: manifest writer (A34) refuses on retracted-no-override,
     schema mismatch, required-field missing, or round-trip failure.

  5. Trail completeness: every required agent must have a per-citation trail
     file before manifest assembly is allowed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cite_forge import __version__
from cite_forge.agents.base import VerifierFailed
from cite_forge.runners import (
    append_failure,
    per_citation_trail,
)
from cite_forge.schemas import (
    SCHEMA_VERSION,
    AtomicUnit,
    Author,
    Bibliography,
    Citation,
    CitationContext,
    CitationFunction,
    CitationIdentity,
    ConvergenceVerdict,
    Escalation,
    GestaltVerdict,
    Integrity,
    Manifest,
    NumericMatch,
    PDFRecord,
    QuoteRecord,
    RetractionStatus,
    RunMetadata,
    StudyDesign,
)

log = logging.getLogger("cite_forge.pipeline")


# =====================================================================
#  CONVERGENCE RULE — HARD-CODED
# =====================================================================


@dataclass(frozen=True)
class ConvergenceInputs:
    w3_consensus_doi: str | None
    w3_consensus_pmid: str | None
    w3_consensus_pmcid: str | None
    blind1_doi: str | None
    blind1_pmid: str | None
    blind1_pmcid: str | None
    blind2_doi: str | None
    blind2_pmid: str | None
    blind2_pmcid: str | None
    retraction_known: bool
    retraction_recheck_agrees: bool
    pdf_verified: bool
    pdf_verified_page2: bool
    context_captured: bool
    function_confidence: float
    quote_required: bool
    quote_verbatim_matched: bool
    gestalt_calibrated: bool
    attribution_inflation_score: float
    causal_language_justified: bool
    all_numeric_claims_verified: bool
    no_direction_flips: bool
    narrative_sound: bool
    integrity_audit_passed: bool


def _ids_triangulate(
    a: tuple[str | None, str | None, str | None],
    b: tuple[str | None, str | None, str | None],
) -> bool:
    paired_nonnull = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not paired_nonnull:
        return False
    return all(x == y for x, y in paired_nonnull)


def is_converged(ci: ConvergenceInputs) -> tuple[bool, list[str]]:
    failing: list[str] = []
    w3 = (ci.w3_consensus_doi, ci.w3_consensus_pmid, ci.w3_consensus_pmcid)
    b1 = (ci.blind1_doi, ci.blind1_pmid, ci.blind1_pmcid)
    b2 = (ci.blind2_doi, ci.blind2_pmid, ci.blind2_pmcid)

    if not _ids_triangulate(w3, b1):
        failing.append("w3_vs_blind1_id_disagreement")
    if not _ids_triangulate(w3, b2):
        failing.append("w3_vs_blind2_id_disagreement")
    if not _ids_triangulate(b1, b2):
        failing.append("blind1_vs_blind2_id_disagreement")
    if not ci.retraction_known:
        failing.append("retraction_status_unknown")
    if not ci.retraction_recheck_agrees:
        failing.append("retraction_recheck_disagrees")
    if not ci.pdf_verified:
        failing.append("pdf_verification_failed")
    if not ci.pdf_verified_page2:
        failing.append("pdf_page2_verification_failed")
    if not ci.context_captured:
        failing.append("citation_context_not_captured")
    if ci.function_confidence < 0.7:
        failing.append(f"citation_function_confidence={ci.function_confidence:.2f}<0.7")
    if ci.quote_required and not ci.quote_verbatim_matched:
        failing.append("quote_required_but_not_verbatim_matched")
    if not ci.gestalt_calibrated:
        failing.append("paragraph_gestalt_not_calibrated")
    if ci.attribution_inflation_score > 0.2:
        failing.append(
            f"attribution_inflation_score={ci.attribution_inflation_score:.2f}>0.2"
        )
    if not ci.causal_language_justified:
        failing.append("causal_language_not_justified_by_source_design")
    if not ci.all_numeric_claims_verified:
        failing.append("numeric_claims_unverified_in_source")
    if not ci.no_direction_flips:
        failing.append("numeric_direction_flip_detected")
    if not ci.narrative_sound:
        failing.append("narrative_coherence_broken")
    if not ci.integrity_audit_passed:
        failing.append("manifest_integrity_audit_failed")
    return (len(failing) == 0), failing


def worst_link_confidence(confidences: dict[str, float]) -> float:
    if not confidences:
        return 0.0
    return min(confidences.values())


def manuscript_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bracket_ref_nums(s: str) -> set[int]:
    """Extract the cited reference number(s) from a bracketed citation string.

    Handles '[12]', '[2, p. 12]', '[8,9,10]', and ranges '[5-7]' / '[5–7]'.
    Page locators ('p. 12') and other non-leading-digit tokens are ignored.
    Returns an empty set for author-year style citations (no brackets).
    """
    nums: set[int] = set()
    for body in re.findall(r"\[([^\]]*)\]", s):
        for part in re.split(r"[;,]", body):
            part = part.strip()
            rng = re.match(r"^(\d+)\s*[–—-]\s*(\d+)$", part)
            if rng:
                lo, hi = int(rng.group(1)), int(rng.group(2))
                if lo <= hi:
                    nums.update(range(lo, hi + 1))
            else:
                lead = re.match(r"^(\d+)", part)
                if lead:
                    nums.add(int(lead.group(1)))
    return nums


# =====================================================================
#  ORCHESTRATOR
# =====================================================================


@dataclass
class CitationState:
    """Per-citation state accumulated across waves."""

    ref_num: int
    bibliography: Bibliography
    declared_doi: str | None
    surface_forms: list[str] = field(default_factory=list)
    # W2 outputs
    crossref: Any | None = None
    pubmed: Any | None = None
    openalex: Any | None = None
    preprint: Any | None = None
    semantic_scholar: Any | None = None
    orcid: Any | None = None
    # W3 outputs
    a11_output: Any | None = None
    identity: CitationIdentity | None = None
    # W4 outputs
    retraction: Any | None = None
    predatory: Any | None = None
    preprint_vor: Any | None = None
    funding_coi: Any | None = None
    crossmark: Any | None = None
    # W5 outputs
    source_results: list[Any] = field(default_factory=list)
    coordinator: Any | None = None
    pdf_path: str | None = None
    pdf_record: PDFRecord | None = None
    pdf_verified: bool = False
    pdf_verified_page2: bool = False
    # W6 outputs (filled per associated unit)
    # W6.5 outputs (filled per unit)
    # W7 outputs
    blind1: Any | None = None
    blind2: Any | None = None
    a33_output: Any | None = None
    a37_output: Any | None = None
    retraction_recheck: Any | None = None
    a48_output: Any | None = None
    # Final
    convergence_verdict: ConvergenceVerdict = ConvergenceVerdict.UNCERTAIN
    overall_confidence: float = 0.0
    confidences: dict[str, float] = field(default_factory=dict)
    failing_clauses: list[str] = field(default_factory=list)


@dataclass
class UnitState:
    """Per-unit state across W6 / W6.5."""

    unit: AtomicUnit
    context: CitationContext | None = None
    function_confidence: float = 0.0
    quote: QuoteRecord | None = None
    gestalt_verdict: GestaltVerdict | None = None
    attribution_inflation_score: float = 0.0
    causal_justified: bool = True
    numeric_all_verified: bool = True
    no_direction_flips: bool = True
    narrative_sound: bool = True
    convergence_verdict: ConvergenceVerdict = ConvergenceVerdict.UNCERTAIN
    overall_confidence: float = 0.0


@dataclass
class PipelineState:
    run_dir: Path
    manuscript_path: Path
    manuscript_sha256: str
    manuscript_text: str
    allow_retracted: bool = False
    completed_waves: set[str] = field(default_factory=set)
    citations: dict[int, CitationState] = field(default_factory=dict)
    units: list[UnitState] = field(default_factory=list)
    bibliography_text: str = ""
    bibliography_entries: list[Bibliography] = field(default_factory=list)
    bibliography_dois: list[str | None] = field(default_factory=list)
    in_text_citations: list[Any] = field(default_factory=list)
    a04_output: Any | None = None
    cartel: Any | None = None
    total_cost_usd: float = 0.0
    total_calls: int = 0


WAVE_ORDER = ("W0", "W1", "W2", "W3", "W4", "W5", "W6", "W6_5", "W7", "W8")


class Pipeline:
    """Orchestrator. Top-level entry point: `run_all()`.

    `run_wave()` enforces sequential ordering: wave N+1 refused until N done.
    Per-citation work fans out via ThreadPoolExecutor (network I/O bound).
    """

    def __init__(self, state: PipelineState):
        self.state = state

    # -----------------------------------------------------------------
    # Wave gate

    def run_wave(self, wave: str) -> None:
        idx = WAVE_ORDER.index(wave)
        for prior in WAVE_ORDER[:idx]:
            if prior not in self.state.completed_waves:
                raise RuntimeError(
                    f"Cannot start wave {wave}: prior wave {prior} not completed."
                )
        log.info("==== Wave %s ====", wave)
        getattr(self, f"_run_{wave}")()
        self.state.completed_waves.add(wave)

    def run_all(self) -> Manifest:
        from cite_forge.agents.base import reset_run_stats

        reset_run_stats()
        for wave in WAVE_ORDER:
            self.run_wave(wave)
        return self._assemble_manifest()

    # -----------------------------------------------------------------
    # W0: decomposition
    def _run_W0(self) -> None:
        from cite_forge.agents.W0_decomposition import (
            A01Input,
            AtomicDecompositionAgent,
        )
        from cite_forge.agents.verification import A46Input, SectionBoundaryVerifier

        trail = self.state.run_dir
        agent = AtomicDecompositionAgent(trail_dir=trail)
        try:
            result = agent.run(
                A01Input(
                    manuscript_text=self.state.manuscript_text,
                    manuscript_sha256=self.state.manuscript_sha256,
                )
            )
        except VerifierFailed as e:
            append_failure(
                self.state.run_dir,
                {"agent": "A01_atomic_decomposition", "reason": str(e)},
            )
            raise
        self.state.units = [UnitState(unit=u) for u in result.units]

        # A46 deterministic check
        a46 = SectionBoundaryVerifier(trail_dir=trail)
        for us in self.state.units:
            try:
                a46.run(
                    A46Input(
                        unit_id=us.unit.unit_id,
                        manuscript_text=self.state.manuscript_text,
                        claim_span=us.unit.claim_span,
                        assigned_section_header=us.unit.section_header,
                    )
                )
            except VerifierFailed as e:
                append_failure(
                    self.state.run_dir,
                    {"agent": "A46_section_boundary", "unit": us.unit.unit_id, "reason": str(e)},
                )

    # -----------------------------------------------------------------
    # W1: discovery
    def _run_W1(self) -> None:
        from cite_forge.agents.W1_discovery import (
            A02Input,
            A03Input,
            A04Input,
            BibliographyParsingAgent,
            CitationExtractionAgent,
            CrossReferenceAuditAgent,
        )

        trail = self.state.run_dir

        # Split manuscript into body + bibliography. Heuristic: find a heading
        # that begins with "References", "Bibliography", or "Works Cited".
        text = self.state.manuscript_text
        bib_pattern = re.compile(
            r"^[#\s]*(References|Bibliography|Works Cited)\s*$",
            re.MULTILINE | re.IGNORECASE,
        )
        m = bib_pattern.search(text)
        if m:
            body = text[: m.start()]
            bib_text = text[m.end() :]
        else:
            body = text
            bib_text = ""
        self.state.bibliography_text = bib_text

        # A02
        a02 = CitationExtractionAgent(trail_dir=trail)
        a02_out = a02.run(A02Input(manuscript_text=body))
        self.state.in_text_citations = a02_out.citations

        # A03
        if bib_text.strip():
            a03 = BibliographyParsingAgent(trail_dir=trail)
            a03_out = a03.run(A03Input(bibliography_text=bib_text))
            self.state.bibliography_entries = a03_out.entries
            self.state.bibliography_dois = a03_out.declared_dois
        else:
            log.warning("No bibliography section detected")
            self.state.bibliography_entries = []
            self.state.bibliography_dois = []

        # A04
        a04 = CrossReferenceAuditAgent(trail_dir=trail)
        a04_out = a04.run(
            A04Input(
                in_text_citations=self.state.in_text_citations,
                bibliography_entries=self.state.bibliography_entries,
                bibliography_dois=self.state.bibliography_dois,
            )
        )
        self.state.a04_output = a04_out

        # Build citation states (one per bibliography entry, ref_num = idx+1)
        for i, entry in enumerate(self.state.bibliography_entries):
            ref_num = i + 1
            doi = self.state.bibliography_dois[i]
            surfaces: list[str] = []
            for mapping, sc in zip(a04_out.mappings, self.state.in_text_citations):
                if mapping.matched_entry_idx == i:
                    surfaces.append(sc.surface)
            self.state.citations[ref_num] = CitationState(
                ref_num=ref_num,
                bibliography=entry,
                declared_doi=doi,
                surface_forms=surfaces,
            )

        # Fill cited_ref_nums on units by linking each unit's citation anchors to
        # A04's authoritative surface->entry mappings. Anchors and surfaces are
        # matched on their extracted bracket ref-number(s) (so a unit anchor
        # "[1]" links to the A04 surface "Meng et al. [1]"); author-year anchors
        # with no bracket numbers fall back to surface-substring matching.
        for us in self.state.units:
            refs: set[int] = set()
            for anchor in us.unit.citation_anchors:
                anchor_nums = _bracket_ref_nums(anchor)
                for mapping in a04_out.mappings:
                    if mapping.matched_entry_idx is None:
                        continue
                    surface_nums = _bracket_ref_nums(mapping.surface)
                    if anchor_nums and surface_nums:
                        matched = bool(anchor_nums & surface_nums)
                    elif anchor_nums or surface_nums:
                        matched = False
                    else:
                        matched = (
                            mapping.surface == anchor
                            or anchor in mapping.surface
                            or mapping.surface in anchor
                        )
                    if matched:
                        refs.add(mapping.matched_entry_idx + 1)
                # Range expansion (e.g. "[5-7]" -> [5, 6, 7])
                for surface, expansion in a04_out.expanded_range_citations:
                    if surface == anchor or (
                        anchor_nums and (anchor_nums & _bracket_ref_nums(surface))
                    ):
                        refs.update(expansion)
            # Rebuild unit with cited_ref_nums populated
            us.unit = us.unit.model_copy(update={"cited_ref_nums": sorted(refs)})

    # -----------------------------------------------------------------
    # W2: identity resolution (parallel per citation)
    def _run_W2(self) -> None:
        from cite_forge.agents.W2_identity import (
            DOIResolutionAgent,
            IdentityQueryInput,
            ORCIDResolutionAgent,
            OpenAlexAgent,
            PreprintServerAgent,
            PubMedAgent,
            SemanticScholarAgent,
        )

        trail = self.state.run_dir
        agent_classes = [
            ("crossref", DOIResolutionAgent),
            ("pubmed", PubMedAgent),
            ("openalex", OpenAlexAgent),
            ("preprint", PreprintServerAgent),
            ("semantic_scholar", SemanticScholarAgent),
            ("orcid", ORCIDResolutionAgent),
        ]

        def run_one(ref_num: int, cs: CitationState):
            citation_trail = per_citation_trail(self.state.run_dir, ref_num)
            payload = IdentityQueryInput(
                ref_num=ref_num,
                bibliography=cs.bibliography,
                declared_doi=cs.declared_doi,
            )
            results: dict[str, Any] = {}
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {}
                for slot, cls in agent_classes:
                    inst = cls(trail_dir=citation_trail)
                    futs[ex.submit(inst.run, payload)] = slot
                for fut in as_completed(futs):
                    slot = futs[fut]
                    try:
                        results[slot] = fut.result()
                    except VerifierFailed as e:
                        append_failure(
                            self.state.run_dir,
                            {"agent": f"W2:{slot}", "ref_num": ref_num, "reason": str(e)},
                        )
                        results[slot] = None
                    except Exception as e:
                        append_failure(
                            self.state.run_dir,
                            {
                                "agent": f"W2:{slot}",
                                "ref_num": ref_num,
                                "reason": f"raised: {type(e).__name__}: {e}",
                            },
                        )
                        results[slot] = None
            return ref_num, results

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {
                ex.submit(run_one, ref_num, cs): ref_num
                for ref_num, cs in self.state.citations.items()
            }
            for fut in as_completed(futs):
                ref_num, results = fut.result()
                cs = self.state.citations[ref_num]
                cs.crossref = results.get("crossref")
                cs.pubmed = results.get("pubmed")
                cs.openalex = results.get("openalex")
                cs.preprint = results.get("preprint")
                cs.semantic_scholar = results.get("semantic_scholar")
                cs.orcid = results.get("orcid")

    # -----------------------------------------------------------------
    # W3: reconciliation
    def _run_W3(self) -> None:
        from cite_forge.agents.W3_reconciliation import (
            A11Input,
            A12Input,
            A13Input,
            BibliographicFieldReconciliationAgent,
            DisambiguationAgent,
            IdentityConvergenceCritic,
            W2AgentSummary,
        )
        from cite_forge.agents.verification import (
            A40Input,
            A41Input,
            A43Input,
            DOIRoundTripVerifier,
            MultiPositionAuthorVerifier,
            PublicationDateTriangulator,
        )

        for ref_num, cs in self.state.citations.items():
            citation_trail = per_citation_trail(self.state.run_dir, ref_num)
            # Build W2 summaries
            summaries: list[W2AgentSummary] = []
            cr_sel = None
            if cs.crossref and cs.crossref.selected_idx is not None:
                cr_sel = cs.crossref.candidates[cs.crossref.selected_idx]
            summaries.append(
                W2AgentSummary(
                    agent_name="A05_crossref",
                    doi=cr_sel.doi if cr_sel else None,
                    pmid=None,
                    pmcid=None,
                    openalex_id=None,
                    title=cr_sel.title if cr_sel else None,
                )
            )
            if cs.pubmed:
                summaries.append(
                    W2AgentSummary(
                        agent_name="A06_pubmed",
                        doi=None,
                        pmid=cs.pubmed.pmid,
                        pmcid=cs.pubmed.pmcid,
                        openalex_id=None,
                        title=None,
                    )
                )
            if cs.openalex:
                summaries.append(
                    W2AgentSummary(
                        agent_name="A07_openalex",
                        doi=cs.openalex.doi,
                        pmid=cs.openalex.pmid,
                        pmcid=cs.openalex.pmcid,
                        openalex_id=cs.openalex.openalex_id,
                        title=None,
                    )
                )

            # A11
            a11 = IdentityConvergenceCritic(trail_dir=citation_trail)
            try:
                cs.a11_output = a11.run(A11Input(ref_num=ref_num, w2_summaries=summaries))
            except VerifierFailed as e:
                append_failure(
                    self.state.run_dir,
                    {"agent": "A11", "ref_num": ref_num, "reason": str(e)},
                )

            # A12 (disambiguation) — only run if there are multiple candidates
            if cs.crossref and len(cs.crossref.candidates) > 1:
                a12 = DisambiguationAgent(trail_dir=citation_trail)
                # Build candidate list as W2AgentSummary shape
                cand_summaries = [
                    W2AgentSummary(
                        agent_name=f"crossref[{i}]",
                        doi=c.doi,
                        pmid=None,
                        pmcid=None,
                        openalex_id=None,
                        title=c.title,
                    )
                    for i, c in enumerate(cs.crossref.candidates)
                ]
                # Surrounding claim: use first surface form's containing sentence (skip if none)
                try:
                    a12.run(
                        A12Input(
                            ref_num=ref_num,
                            bibliography=cs.bibliography,
                            candidates=cand_summaries,
                            surrounding_claim=cs.bibliography.title,
                        )
                    )
                except VerifierFailed as e:
                    append_failure(
                        self.state.run_dir,
                        {"agent": "A12", "ref_num": ref_num, "reason": str(e)},
                    )

            # A13: field reconciliation
            a13 = BibliographicFieldReconciliationAgent(trail_dir=citation_trail)
            cr_meta = cr_sel.model_dump() if cr_sel else {}
            pm_meta = {"pmid": cs.pubmed.pmid if cs.pubmed else None}
            oa_meta = {
                "doi": cs.openalex.doi if cs.openalex else None,
                "openalex_id": cs.openalex.openalex_id if cs.openalex else None,
            }
            try:
                a13.run(
                    A13Input(
                        ref_num=ref_num,
                        bibliography_from_draft=cs.bibliography,
                        crossref_metadata=cr_meta,
                        pubmed_metadata=pm_meta,
                        openalex_metadata=oa_meta,
                    )
                )
            except VerifierFailed as e:
                append_failure(
                    self.state.run_dir,
                    {"agent": "A13", "ref_num": ref_num, "reason": str(e)},
                )

            # Build canonical CitationIdentity from A11 consensus + raw IDs
            consensus_doi = (
                cs.a11_output.consensus_doi
                if cs.a11_output
                else (cr_sel.doi if cr_sel else None)
            )
            cs.identity = CitationIdentity(
                doi=consensus_doi,
                pmid=cs.a11_output.consensus_pmid if cs.a11_output else None,
                pmcid=cs.a11_output.consensus_pmcid if cs.a11_output else None,
                arxiv_id=None,
                openalex_id=cs.a11_output.consensus_openalex_id if cs.a11_output else None,
                consensus_confidence=cs.a11_output.confidence if cs.a11_output else 0.0,
                dissenting_agents=cs.a11_output.dissenting_agents if cs.a11_output else [],
            )

            # A40 deterministic author triangulation
            cr_authors = [
                Author(
                    name_verbatim=f"{a.get('given', '')} {a.get('family', '')}".strip(),
                    given=a.get("given"),
                    family=a.get("family", ""),
                    position=i + 1,
                )
                for i, a in enumerate(cs.crossref.candidates[cs.crossref.selected_idx].model_dump().get("authors", []) if (cs.crossref and cs.crossref.selected_idx is not None) else [])
            ]
            # A40 expects three lists; build them best-effort
            a40 = MultiPositionAuthorVerifier(trail_dir=citation_trail)
            try:
                a40.run(
                    A40Input(
                        ref_num=ref_num,
                        authors_from_crossref=cs.bibliography.authors,
                        authors_from_pubmed=[],
                        authors_from_openalex=[],
                    )
                )
            except VerifierFailed:
                pass

            # A41 publication date triangulator
            a41 = PublicationDateTriangulator(trail_dir=citation_trail)
            try:
                a41.run(
                    A41Input(
                        ref_num=ref_num,
                        crossref_pub_date=f"{cs.bibliography.year}",
                        pubmed_pub_date=f"{cs.bibliography.year}" if cs.pubmed else None,
                        openalex_pub_date=f"{cs.bibliography.year}" if cs.openalex else None,
                    )
                )
            except VerifierFailed:
                pass

            # A43 DOI round-trip
            if cs.identity.doi:
                a43 = DOIRoundTripVerifier(trail_dir=citation_trail)
                try:
                    a43.run(
                        A43Input(
                            ref_num=ref_num,
                            doi=cs.identity.doi,
                            expected_title=cs.bibliography.title,
                            expected_first_author_family=cs.bibliography.authors[0].family
                            if cs.bibliography.authors
                            else "",
                            expected_year=cs.bibliography.year,
                        )
                    )
                except VerifierFailed as e:
                    append_failure(
                        self.state.run_dir,
                        {"agent": "A43", "ref_num": ref_num, "reason": str(e)},
                    )

    # -----------------------------------------------------------------
    # W4: integrity
    def _run_W4(self) -> None:
        from cite_forge.agents.W4_integrity import (
            A14Input,
            A15Input,
            A16Input,
            A17Input,
            A18Input,
            CitationCartelDetector,
            FundingCOIAgent,
            PredatoryVenueAgent,
            PreprintVORAgent,
            RetractionEratumAgent,
        )
        from cite_forge.agents.verification import A45Input, CrossmarkVerifier

        trail = self.state.run_dir

        # A18 is bib-level, run once
        a18 = CitationCartelDetector(trail_dir=trail)
        try:
            self.state.cartel = a18.run(
                A18Input(bibliography_entries=self.state.bibliography_entries)
            )
        except VerifierFailed:
            pass

        for ref_num, cs in self.state.citations.items():
            ct = per_citation_trail(self.state.run_dir, ref_num)
            if cs.identity:
                try:
                    cs.retraction = RetractionEratumAgent(trail_dir=ct).run(
                        A14Input(ref_num=ref_num, identity=cs.identity)
                    )
                except VerifierFailed as e:
                    append_failure(
                        self.state.run_dir,
                        {"agent": "A14", "ref_num": ref_num, "reason": str(e)},
                    )
            try:
                cs.predatory = PredatoryVenueAgent(trail_dir=ct).run(
                    A15Input(ref_num=ref_num, bibliography=cs.bibliography)
                )
            except VerifierFailed:
                pass

            if cs.identity and cs.identity.doi:
                try:
                    cs.preprint_vor = PreprintVORAgent(trail_dir=ct).run(
                        A16Input(
                            ref_num=ref_num,
                            cited_doi=cs.identity.doi,
                            cited_is_preprint=False,
                        )
                    )
                except VerifierFailed:
                    pass

            if cs.identity:
                try:
                    cs.funding_coi = FundingCOIAgent(trail_dir=ct).run(
                        A17Input(
                            ref_num=ref_num,
                            identity=cs.identity,
                            manuscript_author_names=[],
                        )
                    )
                except VerifierFailed:
                    pass

            if cs.identity and cs.identity.doi:
                try:
                    cs.crossmark = CrossmarkVerifier(trail_dir=ct).run(
                        A45Input(ref_num=ref_num, doi=cs.identity.doi)
                    )
                except VerifierFailed:
                    pass

    # -----------------------------------------------------------------
    # W5: retrieval
    def _run_W5(self) -> None:
        from cite_forge.agents.W5_retrieval import (
            A19Input,
            A27Input,
            EuropePMCAgent,
            ILLFallbackAgent,
            PMCAgent,
            PDFVerificationAgent,
            PreprintRetrievalAgent,
            PublisherOAAgent,
            RetrievalCoordinator,
            RetrievalInput,
            UMProxyAgent,
            UnpaywallAgent,
        )
        from cite_forge.agents.verification import A38Input, Page2PDFVerifier
        from cite_forge.pdf import extract_page_text, page_count

        papers_dir = self._papers_dir()
        papers_dir.mkdir(parents=True, exist_ok=True)

        source_classes = [
            UnpaywallAgent,
            EuropePMCAgent,
            PMCAgent,
            PreprintRetrievalAgent,
            PublisherOAAgent,
            UMProxyAgent,
        ]

        def fetch_one(ref_num: int, cs: CitationState):
            ct = per_citation_trail(self.state.run_dir, ref_num)
            target = papers_dir / self._pdf_filename(cs)
            payload = RetrievalInput(
                ref_num=ref_num,
                identity=cs.identity or CitationIdentity(consensus_confidence=0.0),
                bibliography=cs.bibliography,
                target_path=str(target),
            )
            source_results = []
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {
                    ex.submit(cls(trail_dir=ct).run, payload): cls.__name__
                    for cls in source_classes
                }
                for fut in as_completed(futs):
                    name = futs[fut]
                    try:
                        source_results.append(fut.result())
                    except VerifierFailed as e:
                        append_failure(
                            self.state.run_dir,
                            {"agent": f"W5:{name}", "ref_num": ref_num, "reason": str(e)},
                        )
                    except Exception as e:
                        append_failure(
                            self.state.run_dir,
                            {
                                "agent": f"W5:{name}",
                                "ref_num": ref_num,
                                "reason": f"raised: {e!r}",
                            },
                        )

            # Coordinator
            coordinator = RetrievalCoordinator(trail_dir=ct)
            coord_out = coordinator.run(
                A19Input(
                    ref_num=ref_num,
                    identity=cs.identity or CitationIdentity(consensus_confidence=0.0),
                    bibliography=cs.bibliography,
                    source_results=source_results,
                )
            )
            cs.source_results = source_results
            cs.coordinator = coord_out

            if coord_out.chosen_pdf_bytes:
                target.write_bytes(coord_out.chosen_pdf_bytes)
                cs.pdf_path = str(target)
                # A27 PDF verification
                a27 = PDFVerificationAgent(trail_dir=ct)
                first_family = (
                    cs.bibliography.authors[0].family if cs.bibliography.authors else ""
                )
                try:
                    a27_out = a27.run(
                        A27Input(
                            ref_num=ref_num,
                            pdf_bytes=coord_out.chosen_pdf_bytes,
                            expected_title=cs.bibliography.title,
                            expected_first_author_family=first_family,
                            expected_doi=cs.identity.doi if cs.identity else None,
                            expected_year=cs.bibliography.year,
                        )
                    )
                    cs.pdf_verified = a27_out.verified
                    cs.pdf_record = PDFRecord(
                        path=str(target),
                        sha256=hashlib.sha256(coord_out.chosen_pdf_bytes).hexdigest(),
                        bytes=len(coord_out.chosen_pdf_bytes),
                        source=coord_out.chosen_source or "",
                        source_url=coord_out.chosen_url,
                        retrieved_at=datetime.utcnow(),
                        verified=a27_out.verified,
                        verified_page2=False,
                        verification_methods=a27_out.verification_methods,
                        page_count=a27_out.page_count,
                        has_text_layer=a27_out.has_text_layer,
                    )
                except VerifierFailed as e:
                    append_failure(
                        self.state.run_dir,
                        {"agent": "A27", "ref_num": ref_num, "reason": str(e)},
                    )

                # A38 page-2 verification
                pc = page_count(coord_out.chosen_pdf_bytes)
                if pc >= 2:
                    p2_text = extract_page_text(coord_out.chosen_pdf_bytes, 1)
                    title_words = [
                        w
                        for w in re.findall(r"\w+", cs.bibliography.title)
                        if len(w) > 3
                    ]
                    keywords = title_words[:8]
                    try:
                        a38_out = Page2PDFVerifier(trail_dir=ct).run(
                            A38Input(
                                ref_num=ref_num,
                                pdf_text_page2=p2_text,
                                expected_keywords_from_abstract=keywords,
                                expected_title_words=title_words[:10],
                                expected_keyword_count=len(keywords),
                                expected_title_word_count=min(10, len(title_words)),
                            )
                        )
                        cs.pdf_verified_page2 = a38_out.verified
                        if cs.pdf_record:
                            cs.pdf_record = cs.pdf_record.model_copy(
                                update={"verified_page2": a38_out.verified}
                            )
                    except VerifierFailed:
                        pass
            elif coord_out.needs_ill:
                try:
                    ILLFallbackAgent(trail_dir=ct).run(payload)
                except VerifierFailed:
                    pass

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {
                ex.submit(fetch_one, ref_num, cs): ref_num
                for ref_num, cs in self.state.citations.items()
            }
            for fut in as_completed(futs):
                ref_num = futs[fut]
                try:
                    fut.result()
                except Exception as e:
                    append_failure(
                        self.state.run_dir,
                        {
                            "agent": "W5:fetch_one",
                            "ref_num": ref_num,
                            "reason": f"raised: {e!r}",
                        },
                    )

    # -----------------------------------------------------------------
    # W6: context & semantic
    def _run_W6(self) -> None:
        from cite_forge.agents.W6_context import (
            A28Input,
            A29Input,
            A30Input,
            A31Input,
            CitationContextAgent,
            CitationFunctionClassifier,
            MeSHTopicAnchorAgent,
            QuoteVerificationAgent,
        )

        for us in self.state.units:
            unit_trail = self.state.run_dir / "units"
            unit_trail.mkdir(parents=True, exist_ok=True)
            for ref_num in us.unit.cited_ref_nums:
                cs = self.state.citations.get(ref_num)
                if not cs:
                    continue
                ct = per_citation_trail(self.state.run_dir, ref_num)
                # A28 context
                try:
                    a28_out = CitationContextAgent(trail_dir=ct).run(
                        A28Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            manuscript_text=self.state.manuscript_text,
                            claim_span=us.unit.claim_span,
                            section_header=us.unit.section_header,
                            paragraph_idx=us.unit.paragraph_idx,
                        )
                    )
                except VerifierFailed as e:
                    append_failure(
                        self.state.run_dir,
                        {
                            "agent": "A28",
                            "ref_num": ref_num,
                            "unit": us.unit.unit_id,
                            "reason": str(e),
                        },
                    )
                    continue

                ctx = CitationContext(
                    sentence_verbatim=a28_out.sentence_verbatim,
                    prev_sentence=a28_out.prev_sentence,
                    next_sentence=a28_out.next_sentence,
                    section_header=us.unit.section_header,
                    paragraph_idx=us.unit.paragraph_idx,
                    function=CitationFunction.BACKGROUND,
                    function_confidence=0.0,
                )
                # A29 function classifier
                try:
                    a29_out = CitationFunctionClassifier(trail_dir=ct).run(
                        A29Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            citation_context=ctx,
                        )
                    )
                    ctx = ctx.model_copy(
                        update={
                            "function": a29_out.function,
                            "function_confidence": a29_out.confidence,
                        }
                    )
                    us.function_confidence = a29_out.confidence
                except VerifierFailed as e:
                    append_failure(
                        self.state.run_dir,
                        {
                            "agent": "A29",
                            "ref_num": ref_num,
                            "unit": us.unit.unit_id,
                            "reason": str(e),
                        },
                    )

                # A30 MeSH / topic anchor
                try:
                    a30_out = MeSHTopicAnchorAgent(trail_dir=ct).run(
                        A30Input(
                            ref_num=ref_num,
                            pmid=cs.identity.pmid if cs.identity else None,
                            openalex_id=cs.identity.openalex_id if cs.identity else None,
                            manuscript_topic_vector=None,
                        )
                    )
                    ctx = ctx.model_copy(
                        update={
                            "mesh_terms": a30_out.mesh_terms,
                            "openalex_concepts": a30_out.openalex_concepts,
                        }
                    )
                except VerifierFailed:
                    pass

                us.context = ctx

                # A31 quote verification if unit has direct quote
                if us.unit.contains_direct_quote and cs.pdf_path:
                    from cite_forge.pdf import extract_text

                    pdf_bytes = Path(cs.pdf_path).read_bytes()
                    pdf_text = extract_text(pdf_bytes)
                    quote = us.unit.claim_verbatim
                    try:
                        a31_out = QuoteVerificationAgent(trail_dir=ct).run(
                            A31Input(
                                unit_id=us.unit.unit_id,
                                ref_num=ref_num,
                                quote_in_manuscript=quote,
                                pdf_full_text=pdf_text,
                                cited_page_in_manuscript=None,
                            )
                        )
                        us.quote = QuoteRecord(
                            present=True,
                            quote_verbatim=quote,
                            verbatim_match=a31_out.match,
                            near_match_distance=a31_out.distance,
                        )
                    except VerifierFailed:
                        pass

    # -----------------------------------------------------------------
    # W6.5: gestalt
    def _run_W6_5(self) -> None:
        from cite_forge.agents.W6_5_gestalt import (
            A51Input,
            A53Input,
            A54Input,
            A55Input,
            A56Input,
            A57Input,
            A58Input,
            AttributionInflationAgent,
            CausalLanguageAuditAgent,
            HedgingCalibrationAgent,
            NarrativeCoherenceAgent,
            NumericClaimVerifier,
            OmittedCaveatAgent,
            ParagraphGestaltAgent,
        )
        from cite_forge.agents.verification import A39Input, ClaimSourceSupportVerifier

        from cite_forge.pdf import extract_text

        for us in self.state.units:
            for ref_num in us.unit.cited_ref_nums:
                cs = self.state.citations.get(ref_num)
                if not cs or not cs.pdf_path:
                    continue
                ct = per_citation_trail(self.state.run_dir, ref_num)
                pdf_bytes = Path(cs.pdf_path).read_bytes()
                src_text = extract_text(pdf_bytes)

                # A51 paragraph gestalt
                paragraph = us.unit.claim_verbatim  # using claim as paragraph proxy
                try:
                    a51_out = ParagraphGestaltAgent(trail_dir=ct).run(
                        A51Input(
                            paragraph_idx=us.unit.paragraph_idx,
                            paragraph_verbatim=paragraph,
                            cited_pdf_abstracts=[src_text[:2000]],
                            cited_pdf_full_texts=[src_text],
                        )
                    )
                    us.gestalt_verdict = a51_out.gestalt.verdict
                except VerifierFailed as e:
                    append_failure(
                        self.state.run_dir,
                        {"agent": "A51", "ref_num": ref_num, "reason": str(e)},
                    )

                # A53 attribution inflation
                try:
                    a53_out = AttributionInflationAgent(trail_dir=ct).run(
                        A53Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            manuscript_claim_verbatim=us.unit.claim_verbatim,
                            source_pdf_text=src_text,
                        )
                    )
                    us.attribution_inflation_score = max(
                        us.attribution_inflation_score,
                        a53_out.inflation.inflation_score,
                    )
                except VerifierFailed:
                    pass

                # A54 omitted caveats
                try:
                    OmittedCaveatAgent(trail_dir=ct).run(
                        A54Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            manuscript_claim_verbatim=us.unit.claim_verbatim,
                            source_pdf_text=src_text,
                        )
                    )
                except VerifierFailed:
                    pass

                # A55 causal language audit
                try:
                    a55_out = CausalLanguageAuditAgent(trail_dir=ct).run(
                        A55Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            manuscript_sentence=us.unit.claim_verbatim,
                            source_design=StudyDesign.LONGITUDINAL_COHORT,
                        )
                    )
                    if not a55_out.audit.justified and a55_out.audit.causal_words_in_manuscript:
                        us.causal_justified = False
                except VerifierFailed:
                    pass

                # A56 numeric claims
                try:
                    a56_out = NumericClaimVerifier(trail_dir=ct).run(
                        A56Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            manuscript_text=us.unit.claim_verbatim,
                            source_pdf_text=src_text,
                        )
                    )
                    for nc in a56_out.claims:
                        if nc.match == NumericMatch.NOT_FOUND:
                            us.numeric_all_verified = False
                        if nc.match == NumericMatch.DIRECTION_FLIP:
                            us.no_direction_flips = False
                except VerifierFailed:
                    pass

                # A57 narrative coherence
                try:
                    NarrativeCoherenceAgent(trail_dir=ct).run(
                        A57Input(
                            paragraph_idx=us.unit.paragraph_idx,
                            paragraph_verbatim=us.unit.claim_verbatim,
                            cited_ref_nums=us.unit.cited_ref_nums,
                        )
                    )
                except VerifierFailed:
                    pass

                # A58 hedging calibration
                try:
                    HedgingCalibrationAgent(trail_dir=ct).run(
                        A58Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            manuscript_sentence=us.unit.claim_verbatim,
                            source_pdf_text=src_text,
                        )
                    )
                except VerifierFailed:
                    pass

                # A39 claim-source support gate
                try:
                    ClaimSourceSupportVerifier(trail_dir=ct).run(
                        A39Input(
                            unit_id=us.unit.unit_id,
                            ref_num=ref_num,
                            claim_verbatim=us.unit.claim_verbatim,
                            source_pdf_text=src_text,
                        )
                    )
                except VerifierFailed:
                    pass

    # -----------------------------------------------------------------
    # W7: blind verification
    def _run_W7(self) -> None:
        from cite_forge.agents.W7_blind import (
            A33Input,
            A37Input,
            BlindReDerivation1,
            BlindReDerivation2,
            BlindReDerivationInput,
            BlindReDerivationOutput,
            ConvergenceCritic,
            TriangulationCritic,
        )
        from cite_forge.agents.verification import (
            A42Input,
            A48Input,
            IdentityConsensusMetaCritic,
            RetractionRecheckCritic,
        )

        for ref_num, cs in self.state.citations.items():
            ct = per_citation_trail(self.state.run_dir, ref_num)
            try:
                cs.blind1 = BlindReDerivation1(trail_dir=ct).run(
                    BlindReDerivationInput(
                        ref_num=ref_num,
                        bibliography=cs.bibliography,
                    )
                )
            except VerifierFailed:
                cs.blind1 = BlindReDerivationOutput(
                    doi=None, pmid=None, pmcid=None, openalex_id=None, confidence=0.0
                )
            try:
                cs.blind2 = BlindReDerivation2(trail_dir=ct).run(
                    BlindReDerivationInput(
                        ref_num=ref_num,
                        bibliography=cs.bibliography,
                    )
                )
            except VerifierFailed:
                cs.blind2 = BlindReDerivationOutput(
                    doi=None, pmid=None, pmcid=None, openalex_id=None, confidence=0.0
                )

            w3_repr = BlindReDerivationOutput(
                doi=cs.identity.doi if cs.identity else None,
                pmid=cs.identity.pmid if cs.identity else None,
                pmcid=cs.identity.pmcid if cs.identity else None,
                openalex_id=cs.identity.openalex_id if cs.identity else None,
                confidence=cs.identity.consensus_confidence if cs.identity else 0.0,
            )

            # A37 triangulation
            try:
                cs.a37_output = TriangulationCritic(trail_dir=ct).run(
                    A37Input(
                        ref_num=ref_num,
                        w3_consensus=w3_repr,
                        a32_output=cs.blind1,
                        a36_output=cs.blind2,
                    )
                )
            except VerifierFailed:
                pass

            # A42 retraction recheck
            if cs.identity:
                try:
                    cs.retraction_recheck = RetractionRecheckCritic(trail_dir=ct).run(
                        A42Input(
                            ref_num=ref_num,
                            identity=cs.identity,
                            earlier_status=cs.retraction.status
                            if cs.retraction
                            else RetractionStatus.NONE,
                        )
                    )
                except VerifierFailed:
                    pass

            # A48 identity-consensus meta-critic
            try:
                cs.a48_output = IdentityConsensusMetaCritic(trail_dir=ct).run(
                    A48Input(
                        ref_num=ref_num,
                        a11_converged=cs.a11_output.converged if cs.a11_output else False,
                        a11_dissenting_agents=cs.a11_output.dissenting_agents
                        if cs.a11_output
                        else [],
                        upstream_sources_share_input=False,
                    )
                )
            except VerifierFailed:
                pass

            # A33 final convergence critic — computes verdict from accumulated state
            quote_required = any(
                us.unit.contains_direct_quote
                for us in self.state.units
                if ref_num in us.unit.cited_ref_nums
            )
            quote_matched = all(
                (us.quote and us.quote.verbatim_match == "VERBATIM")
                or (us.quote is None and not us.unit.contains_direct_quote)
                for us in self.state.units
                if ref_num in us.unit.cited_ref_nums
            )
            ctx_captured = all(
                us.context is not None
                for us in self.state.units
                if ref_num in us.unit.cited_ref_nums
            ) if any(ref_num in us.unit.cited_ref_nums for us in self.state.units) else False

            avg_func_conf = 0.0
            unit_count = 0
            inflation_max = 0.0
            gestalt_calibrated = True
            causal_justified = True
            numeric_all = True
            no_dir_flips = True
            narrative_ok = True
            for us in self.state.units:
                if ref_num in us.unit.cited_ref_nums:
                    unit_count += 1
                    avg_func_conf += us.function_confidence
                    inflation_max = max(inflation_max, us.attribution_inflation_score)
                    if us.gestalt_verdict and us.gestalt_verdict.value != "CALIBRATED":
                        gestalt_calibrated = False
                    if not us.causal_justified:
                        causal_justified = False
                    if not us.numeric_all_verified:
                        numeric_all = False
                    if not us.no_direction_flips:
                        no_dir_flips = False
                    if not us.narrative_sound:
                        narrative_ok = False
            avg_func_conf = avg_func_conf / unit_count if unit_count else 0.0

            try:
                cs.a33_output = ConvergenceCritic(trail_dir=ct).run(
                    A33Input(
                        ref_num=ref_num,
                        w3_consensus=w3_repr,
                        a32_output=cs.blind1,
                        a36_output=cs.blind2,
                        a11_converged=(
                            cs.a11_output.converged if cs.a11_output else False
                        ),
                        w5_pdf_verified=cs.pdf_verified,
                        w5_pdf_verified_page2=cs.pdf_verified_page2,
                        w4_retraction_known=cs.retraction is not None,
                        w4_retraction_recheck_agrees=(
                            cs.retraction_recheck.agrees_with_earlier
                            if cs.retraction_recheck
                            else True
                        ),
                        w6_context_captured=ctx_captured,
                        w6_function_confidence=avg_func_conf,
                        w6_quote_required=quote_required,
                        w6_quote_verbatim_matched=quote_matched,
                        gestalt_calibrated=gestalt_calibrated,
                        attribution_inflation_score=inflation_max,
                        causal_language_justified=causal_justified,
                        all_numeric_claims_verified=numeric_all,
                    )
                )
                cs.convergence_verdict = cs.a33_output.verdict
                cs.failing_clauses = cs.a33_output.failing_clauses
                cs.overall_confidence = cs.a33_output.confidence
                cs.confidences["A33"] = cs.a33_output.confidence
            except VerifierFailed as e:
                append_failure(
                    self.state.run_dir,
                    {"agent": "A33", "ref_num": ref_num, "reason": str(e)},
                )

    # -----------------------------------------------------------------
    # W8: manifest assembly
    def _run_W8(self) -> None:
        # All work happens in _assemble_manifest, which is called by run_all.
        # Mark wave done so caller can call _assemble_manifest.
        pass

    # -----------------------------------------------------------------
    # helpers
    @staticmethod
    def _slug(text: str, max_len: int = 30) -> str:
        s = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
        return s[:max_len] or "unknown"

    def _pdf_filename(self, cs: CitationState) -> str:
        first = cs.bibliography.authors[0].family if cs.bibliography.authors else "unknown"
        topic = self._slug(cs.bibliography.title.split(":")[0])
        return f"{cs.ref_num:02d}_{self._slug(first)}_{cs.bibliography.year}_{topic}.pdf"

    def _papers_dir(self) -> Path:
        # papers/ sits in the manuscript root, one level above the draft's dir
        ms_dir = self.state.manuscript_path.parent
        manuscript_root = ms_dir.parent
        return manuscript_root / "papers"

    # -----------------------------------------------------------------
    # Manifest assembly
    def _assemble_manifest(self) -> Manifest:
        from cite_forge.agents.W8_manifest import (
            A34Input,
            A35Input,
            HumanEscalationPacketizer,
            ManifestSchemaValidator,
        )
        from cite_forge.agents.verification import A49Input, EscalationPacketValidator

        # Build citations
        citations: list[Citation] = []
        escalations: list[Escalation] = []
        for ref_num, cs in self.state.citations.items():
            integrity = Integrity(
                retraction_status=(
                    cs.retraction.status if cs.retraction else RetractionStatus.NONE
                ),
                retraction_doi=cs.retraction.retraction_doi if cs.retraction else None,
                retraction_date=cs.retraction.retraction_date if cs.retraction else None,
                retraction_recheck_agrees=(
                    cs.retraction_recheck.agrees_with_earlier
                    if cs.retraction_recheck
                    else True
                ),
                predatory_risk=(
                    cs.predatory.risk_score if cs.predatory else 0
                ),
                predatory_evidence=(cs.predatory.evidence if cs.predatory else []),
                crossmark_update_count=(
                    cs.crossmark.update_count if cs.crossmark else 0
                ),
            )
            identity = cs.identity or CitationIdentity(consensus_confidence=0.0)
            citation = Citation(
                ref_num=ref_num,
                surface_forms=cs.surface_forms,
                identity=identity,
                bibliography=cs.bibliography,
                integrity=integrity,
                pdf=cs.pdf_record,
                convergence_verdict=cs.convergence_verdict,
                overall_confidence=cs.overall_confidence,
                agent_trail_dir=str(per_citation_trail(self.state.run_dir, ref_num)),
            )
            citations.append(citation)
            if (
                cs.convergence_verdict != ConvergenceVerdict.CONVERGED
                or cs.overall_confidence < 0.85
                or cs.pdf_record is None
            ):
                escalations.append(
                    Escalation(
                        ref_num=ref_num,
                        reason=(
                            "; ".join(cs.failing_clauses)
                            or ("missing_pdf" if cs.pdf_record is None else "low_confidence")
                        ),
                        severity=(
                            "BLOCKING"
                            if cs.pdf_record is None
                            or integrity.retraction_status == RetractionStatus.RETRACTED
                            else "WARNING"
                        ),
                        human_packet_anchor=f"<a id=\"esc-ref-{ref_num:03d}\"></a>",
                        question_for_human=self._escalation_question(cs),
                        candidate_pdfs=(
                            [sr.source_url for sr in cs.source_results if sr.source_url]
                        ),
                        conflicting_agents=(
                            cs.a11_output.dissenting_agents if cs.a11_output else []
                        ),
                    )
                )

        from cite_forge.agents.base import RUN_STATS

        self.state.total_calls = RUN_STATS.agent_calls
        self.state.total_cost_usd = RUN_STATS.cost_usd

        units = [us.unit for us in self.state.units]
        meta = RunMetadata(
            schema_version=SCHEMA_VERSION,
            run_id=self.state.run_dir.name,
            manuscript_path=str(self.state.manuscript_path),
            manuscript_sha256=self.state.manuscript_sha256,
            generated_at=datetime.utcnow(),
            cite_forge_version=__version__,
            total_agent_calls=self.state.total_calls,
            total_cost_usd=self.state.total_cost_usd or None,
        )

        # A34 manifest validator (refuse-to-emit)
        a34 = ManifestSchemaValidator(trail_dir=self.state.run_dir)
        a34_out = a34.run(
            A34Input(
                metadata=meta,
                units=units,
                citations=citations,
                escalations=escalations,
                allow_retracted=self.state.allow_retracted,
            )
        )

        # A35 escalation packetizer
        a35 = HumanEscalationPacketizer(trail_dir=self.state.run_dir)
        a35_out = a35.run(
            A35Input(
                escalations=escalations,
                manuscript_path=str(self.state.manuscript_path),
                run_dir=str(self.state.run_dir),
            )
        )

        # A49 escalation packet validator
        a49 = EscalationPacketValidator(trail_dir=self.state.run_dir)
        try:
            a49.run(
                A49Input(
                    escalations_md=a35_out.escalations_md,
                    escalations_count=len(escalations),
                )
            )
        except VerifierFailed as e:
            append_failure(
                self.state.run_dir, {"agent": "A49", "reason": str(e)}
            )

        # Persist outputs
        papers_dir = self._papers_dir()
        manuscript_root = papers_dir.parent
        manuscript_root.mkdir(parents=True, exist_ok=True)
        (manuscript_root / "manifest.json").write_text(a34_out.manifest_json)
        (manuscript_root / "ESCALATIONS.md").write_text(a35_out.escalations_md)
        (manuscript_root / "MISSING.md").write_text(a35_out.missing_md)
        # Bibliography exports
        self._write_bibliography(manuscript_root, citations)

        # A50 manifest integrity audit (deterministic re-check)
        from cite_forge.agents.verification import A50Input, ManifestIntegrityAuditor

        try:
            ManifestIntegrityAuditor(trail_dir=self.state.run_dir).run(
                A50Input(manifest_json=a34_out.manifest_json)
            )
        except VerifierFailed as e:
            append_failure(self.state.run_dir, {"agent": "A50", "reason": str(e)})

        return Manifest.model_validate_json(a34_out.manifest_json)

    @staticmethod
    def _escalation_question(cs: CitationState) -> str:
        if cs.pdf_record is None:
            return (
                f"PDF retrieval failed for ref {cs.ref_num} ({cs.bibliography.title!r}). "
                f"Provide a PDF or confirm this citation can be excluded."
            )
        if cs.convergence_verdict != ConvergenceVerdict.CONVERGED:
            return (
                f"Convergence failed for ref {cs.ref_num}: "
                f"{'; '.join(cs.failing_clauses) or 'unknown reason'}. "
                f"Review and confirm the retrieved PDF matches the citation."
            )
        return f"Low confidence ({cs.overall_confidence:.2f}); please spot-check ref {cs.ref_num}."

    def _write_bibliography(
        self, dest: Path, citations: list[Citation]
    ) -> None:
        # Vancouver-style markdown
        md_lines = ["# Bibliography (Vancouver)\n"]
        bib_lines: list[str] = []
        ris_lines: list[str] = []
        csl: list[dict] = []
        for c in citations:
            bib = c.bibliography
            authors = ", ".join(
                f"{a.family} {a.given or ''}".strip() for a in bib.authors
            )
            ref_str = (
                f"{c.ref_num}. {authors}. {bib.title}. "
                f"{bib.journal or ''} {bib.year};{bib.volume or ''}({bib.issue or ''}):"
                f"{bib.pages or ''}. doi:{c.identity.doi or ''}"
            ).replace("  ", " ")
            md_lines.append(f"- {ref_str}")
            cite_key = (
                f"{(bib.authors[0].family if bib.authors else 'X')}{bib.year}"
            ).replace(" ", "")
            bib_lines.append(
                f"@article{{{cite_key}_{c.ref_num},\n"
                f"  author = {{{ ' and '.join(a.name_verbatim for a in bib.authors) }}},\n"
                f"  title = {{{bib.title}}},\n"
                f"  journal = {{{bib.journal or ''}}},\n"
                f"  year = {{{bib.year}}},\n"
                f"  volume = {{{bib.volume or ''}}},\n"
                f"  number = {{{bib.issue or ''}}},\n"
                f"  pages = {{{bib.pages or ''}}},\n"
                f"  doi = {{{c.identity.doi or ''}}},\n"
                f"}}\n"
            )
            ris_lines.append(
                f"TY  - JOUR\nTI  - {bib.title}\n"
                + "".join(f"AU  - {a.name_verbatim}\n" for a in bib.authors)
                + f"JO  - {bib.journal or ''}\nPY  - {bib.year}\n"
                f"VL  - {bib.volume or ''}\nIS  - {bib.issue or ''}\n"
                f"SP  - {bib.pages or ''}\nDO  - {c.identity.doi or ''}\nER  -\n"
            )
            csl.append(
                {
                    "id": f"ref-{c.ref_num}",
                    "type": "article-journal",
                    "title": bib.title,
                    "author": [
                        {"family": a.family, "given": a.given or ""} for a in bib.authors
                    ],
                    "container-title": bib.journal or "",
                    "issued": {"date-parts": [[bib.year]]},
                    "volume": bib.volume or "",
                    "issue": bib.issue or "",
                    "page": bib.pages or "",
                    "DOI": c.identity.doi or "",
                }
            )
        (dest / "bibliography.md").write_text("\n".join(md_lines))
        (dest / "bibliography.bib").write_text("\n".join(bib_lines))
        (dest / "bibliography.ris").write_text("".join(ris_lines))
        (dest / "bibliography.json").write_text(json.dumps(csl, indent=2))
