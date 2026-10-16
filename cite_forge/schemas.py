"""Frozen pydantic schemas — the load-bearing contracts.

Bump SCHEMA_VERSION on any breaking change. The manifest writer refuses
to emit a manifest whose schema version differs from the consumer's
expected version.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# =====================================================================
#  ENUMS
# =====================================================================


class ClaimType(str, Enum):
    BACKGROUND = "background"
    METHODS = "methods"
    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"
    QUANTITATIVE = "quantitative"
    DEFINITIONAL = "definitional"
    QUOTATION = "quotation"


class CitationFunction(str, Enum):
    BACKGROUND = "BACKGROUND"
    METHODS_BORROWED = "METHODS_BORROWED"
    SUPPORTING_EVIDENCE = "SUPPORTING_EVIDENCE"
    CONTRADICTING = "CONTRADICTING"
    CONTRAST_COMPARISON = "CONTRAST_COMPARISON"
    DEFINITIONAL = "DEFINITIONAL"
    DATA_SOURCE = "DATA_SOURCE"
    QUOTATION = "QUOTATION"
    SELF_CITE = "SELF_CITE"


class RetractionStatus(str, Enum):
    NONE = "NONE"
    ERRATUM = "ERRATUM"
    EXPRESSION_OF_CONCERN = "EXPRESSION_OF_CONCERN"
    RETRACTED = "RETRACTED"
    REPLACED = "REPLACED"


class ConvergenceVerdict(str, Enum):
    CONVERGED = "CONVERGED"
    DIVERGED = "DIVERGED"
    UNCERTAIN = "UNCERTAIN"


class GestaltVerdict(str, Enum):
    """A51 paragraph-level verdict."""

    CALIBRATED = "CALIBRATED"
    OVERCLAIM = "OVERCLAIM"
    UNDERCLAIM = "UNDERCLAIM"
    CHERRYPICK = "CHERRYPICK"
    FALSE_CONSENSUS = "FALSE_CONSENSUS"


class LogicalRelation(str, Enum):
    """A52 sentence-pair connective relation."""

    CAUSAL = "CAUSAL"
    CORRELATIONAL = "CORRELATIONAL"
    CONTRASTIVE = "CONTRASTIVE"
    ADDITIVE = "ADDITIVE"
    CONCESSIVE = "CONCESSIVE"
    TEMPORAL = "TEMPORAL"
    EVIDENTIAL = "EVIDENTIAL"


class InflationMechanism(str, Enum):
    """A53 attribution inflation mechanism."""

    STRENGTHENED_VERB = "STRENGTHENED_VERB"
    DROPPED_QUALIFIER = "DROPPED_QUALIFIER"
    GENERALIZED_POPULATION = "GENERALIZED_POPULATION"
    TEMPORAL_SHIFT = "TEMPORAL_SHIFT"
    SAMPLE_TO_POPULATION = "SAMPLE_TO_POPULATION"
    ASSOCIATION_TO_CAUSATION = "ASSOCIATION_TO_CAUSATION"


class StudyDesign(str, Enum):
    """A55 source study design — determines if causal language is justified."""

    RCT = "RCT"
    QUASI_EXPERIMENT = "QUASI_EXPERIMENT"
    MENDELIAN_RANDOMIZATION = "MENDELIAN_RANDOMIZATION"
    INSTRUMENTAL_VARIABLE = "INSTRUMENTAL_VARIABLE"
    LONGITUDINAL_COHORT = "LONGITUDINAL_COHORT"
    CASE_CONTROL = "CASE_CONTROL"
    CROSS_SECTIONAL = "CROSS_SECTIONAL"
    QUALITATIVE = "QUALITATIVE"
    SYSTEMATIC_REVIEW = "SYSTEMATIC_REVIEW"
    META_ANALYSIS = "META_ANALYSIS"
    OPINION_EDITORIAL = "OPINION_EDITORIAL"
    SIMULATION = "SIMULATION"


class NumericMatch(str, Enum):
    """A56 numeric claim match type."""

    VERBATIM = "VERBATIM"
    ROUNDED = "ROUNDED"
    CONVERTED = "CONVERTED"
    DIRECTION_FLIP = "DIRECTION_FLIP"
    NOT_FOUND = "NOT_FOUND"


class NarrativeVerdict(str, Enum):
    """A57 narrative coherence verdict."""

    SOUND = "SOUND"
    BROKEN = "BROKEN"
    UNVERIFIABLE = "UNVERIFIABLE"


class QuoteMatch(str, Enum):
    """A31 / A47 quote verification."""

    VERBATIM = "VERBATIM"
    NEAR = "NEAR"
    NOT_FOUND = "NOT_FOUND"


# =====================================================================
#  IDENTITY GRAPH
# =====================================================================


class CitationIdentity(_Frozen):
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    nihms_id: str | None = None
    preprint_doi: str | None = None
    vor_doi: str | None = None
    semantic_scholar_id: str | None = None

    consensus_confidence: float = Field(ge=0.0, le=1.0)
    dissenting_agents: list[str] = Field(default_factory=list)


class Author(_Frozen):
    name_verbatim: str
    given: str | None = None
    family: str
    orcid: str | None = None
    affiliation: str | None = None
    position: int = Field(ge=1)


class Bibliography(_Frozen):
    authors: list[Author]
    title: str
    journal: str | None = None
    issn: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    year: int = Field(ge=1500, le=2100)
    publisher: str | None = None
    field_confidences: dict[str, float] = Field(default_factory=dict)


# =====================================================================
#  INTEGRITY
# =====================================================================


class Integrity(_Frozen):
    retraction_status: RetractionStatus = RetractionStatus.NONE
    retraction_doi: str | None = None
    retraction_date: datetime | None = None
    retraction_recheck_agrees: bool = True
    predatory_risk: int = Field(ge=0, le=100, default=0)
    predatory_evidence: list[str] = Field(default_factory=list)
    preprint_vor_delta: float | None = Field(default=None, ge=0.0, le=1.0)
    funding_overlap_with_authors: bool = False
    crossmark_update_count: int = 0


# =====================================================================
#  PDF
# =====================================================================


class PDFRecord(_Frozen):
    path: str
    sha256: str
    bytes: int = Field(ge=0)
    source: str
    source_url: str | None = None
    license: str | None = None
    retrieved_at: datetime
    verified: bool
    verified_page2: bool = False
    verification_methods: list[str] = Field(default_factory=list)
    page_count: int = Field(ge=0)
    has_text_layer: bool


# =====================================================================
#  CONTEXT
# =====================================================================


class CitationContext(_Frozen):
    sentence_verbatim: str
    prev_sentence: str | None = None
    next_sentence: str | None = None
    section_header: str
    paragraph_idx: int = Field(ge=0)
    function: CitationFunction
    function_confidence: float = Field(ge=0.0, le=1.0)
    mesh_terms: list[str] = Field(default_factory=list)
    openalex_concepts: list[str] = Field(default_factory=list)
    topic_drift_score: float | None = Field(default=None, ge=0.0, le=1.0)


class QuoteRecord(_Frozen):
    present: bool
    quote_verbatim: str | None = None
    page_in_source: int | None = None
    verbatim_match: QuoteMatch | None = None
    near_match_distance: int | None = None


# =====================================================================
#  GESTALT (A51-A58)
# =====================================================================


class ParagraphGestalt(_Frozen):
    """A51 output."""

    paragraph_idx: int = Field(ge=0)
    paragraph_verbatim: str
    verdict: GestaltVerdict
    evidence_sentences: list[str] = Field(default_factory=list)
    evidence_source_passages: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ConnectiveRecord(_Frozen):
    """A52 output."""

    sentence_a_span: tuple[int, int]
    sentence_b_span: tuple[int, int]
    connective: str
    expected_relation: LogicalRelation
    actual_relation: LogicalRelation
    match: bool
    confidence: float = Field(ge=0.0, le=1.0)


class AttributionInflation(_Frozen):
    """A53 output."""

    inflation_score: float = Field(ge=0.0, le=1.0)
    source_language: str
    manuscript_language: str
    mechanism: InflationMechanism
    confidence: float = Field(ge=0.0, le=1.0)


class OmittedCaveat(_Frozen):
    """A54 single record (list of these per unit)."""

    caveat_verbatim: str
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    source_page: int | None = None


class CausalLanguageAudit(_Frozen):
    """A55 output."""

    causal_words_in_manuscript: list[str]
    source_design: StudyDesign
    justified: bool
    rationale: str


class NumericClaim(_Frozen):
    """A56 output."""

    number_in_manuscript: float
    units_in_manuscript: str | None = None
    number_in_source: float | None = None
    units_in_source: str | None = None
    match: NumericMatch
    source_page: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class NarrativeCoherence(_Frozen):
    """A57 output."""

    inference_chain: list[str]
    chain_citations: list[int]  # ref_nums
    breaks: list[int]  # indices into inference_chain
    verdict: NarrativeVerdict
    confidence: float = Field(ge=0.0, le=1.0)


class HedgingCalibration(_Frozen):
    """A58 output. Strength 1=very weak ("might", "could possibly") ... 5=very strong ("proves", "definitively")."""

    manuscript_hedge_words: list[str]
    manuscript_hedge_strength: int = Field(ge=1, le=5)
    source_hedge_words: list[str]
    source_hedge_strength: int = Field(ge=1, le=5)
    delta: int  # manuscript_strength - source_strength
    calibrated: bool  # |delta| <= 1


class GestaltVerification(_Frozen):
    """Aggregate of W6.5 agents per AtomicUnit."""

    paragraph_gestalt: ParagraphGestalt | None = None
    connectives: list[ConnectiveRecord] = Field(default_factory=list)
    attribution_inflation: AttributionInflation | None = None
    omitted_caveats: list[OmittedCaveat] = Field(default_factory=list)
    causal_audit: CausalLanguageAudit | None = None
    numeric_claims: list[NumericClaim] = Field(default_factory=list)
    narrative: NarrativeCoherence | None = None
    hedging: HedgingCalibration | None = None


# =====================================================================
#  CITATION (per ref_num)
# =====================================================================


class Citation(_Frozen):
    ref_num: int = Field(ge=1)
    surface_forms: list[str]
    identity: CitationIdentity
    bibliography: Bibliography
    integrity: Integrity
    pdf: PDFRecord | None = None
    convergence_verdict: ConvergenceVerdict
    overall_confidence: float = Field(ge=0.0, le=1.0)
    agent_trail_dir: str


# =====================================================================
#  ATOMIC UNIT (per claim)
# =====================================================================


class AtomicUnit(_Frozen):
    unit_id: str  # "U0001"
    claim_verbatim: str
    claim_span: tuple[int, int]
    section_header: str
    paragraph_idx: int = Field(ge=0)
    sentence_idx: int = Field(ge=0)
    citation_anchors: list[str]
    cited_ref_nums: list[int]
    claim_type: ClaimType
    contains_direct_quote: bool
    quote: QuoteRecord | None = None
    context: CitationContext | None = None
    gestalt: GestaltVerification | None = None
    convergence_verdict: ConvergenceVerdict
    overall_confidence: float = Field(ge=0.0, le=1.0)


# =====================================================================
#  ESCALATION
# =====================================================================


class Escalation(_Frozen):
    unit_id: str | None = None
    ref_num: int | None = None
    reason: str
    severity: Literal["BLOCKING", "WARNING"]
    human_packet_anchor: str
    question_for_human: str
    candidate_pdfs: list[str] = Field(default_factory=list)
    conflicting_agents: list[str] = Field(default_factory=list)


# =====================================================================
#  RUN-LEVEL
# =====================================================================


class RunMetadata(_Frozen):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    manuscript_path: str
    manuscript_sha256: str
    generated_at: datetime
    cite_forge_version: str
    total_agent_calls: int = Field(ge=0)
    total_cost_usd: float | None = None


class Manifest(_Frozen):
    metadata: RunMetadata
    units: list[AtomicUnit]
    citations: list[Citation]
    escalations: list[Escalation]
