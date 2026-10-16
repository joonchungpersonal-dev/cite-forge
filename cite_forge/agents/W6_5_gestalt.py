"""W6.5 — Semantic / Gestalt Committee.

Operates over multi-sentence units. A sentence can be individually
citation-valid yet leave the reader with a wrong impression.

A51 Paragraph Gestalt Agent (LLM)
A52 Sentence-Pair Logical Connective Agent (LLM)
A53 Attribution Inflation Detector (LLM)
A54 Omitted-Caveat Detector (LLM)
A55 Causal Language Audit (LLM + deterministic check)
A56 Numeric Claim Verifier (deterministic with regex)
A57 Narrative Coherence Agent (LLM)
A58 Hedging Calibration Agent (deterministic via lexicon)
A39, A44 live in verification.py
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from cite_forge.agents.base import DeterministicAgent, LLMAgent
from cite_forge.llm import call_structured
from cite_forge.schemas import (
    AttributionInflation,
    CausalLanguageAudit,
    ConnectiveRecord,
    GestaltVerdict,
    HedgingCalibration,
    InflationMechanism,
    LogicalRelation,
    NarrativeCoherence,
    NarrativeVerdict,
    NumericClaim,
    NumericMatch,
    OmittedCaveat,
    ParagraphGestalt,
    StudyDesign,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A51: Paragraph Gestalt ----------


class A51Input(_Frozen):
    paragraph_idx: int
    paragraph_verbatim: str
    cited_pdf_abstracts: list[str]
    cited_pdf_full_texts: list[str]


class A51Output(_Frozen):
    gestalt: ParagraphGestalt


_A51_SYSTEM = """You judge whether a manuscript paragraph leaves the reader with a calibrated
impression of its cited sources.

Verdicts:
- CALIBRATED: the paragraph's impression matches the cited sources' strength of evidence.
- OVERCLAIM: the paragraph implies stronger evidence than the sources support.
- UNDERCLAIM: the paragraph implies weaker evidence than the sources show.
- CHERRYPICK: the paragraph selectively cites evidence supporting one position while
  ignoring contradicting evidence from the same or related sources.
- FALSE_CONSENSUS: the paragraph implies broader agreement than the literature shows.

For any non-CALIBRATED verdict, you MUST cite:
- evidence_sentences: VERBATIM sentences from the paragraph that exhibit the problem.
- evidence_source_passages: VERBATIM passages from the cited PDFs that contradict or qualify."""


class ParagraphGestaltAgent(LLMAgent[A51Input, A51Output]):
    name = "A51_paragraph_gestalt"
    wave = "W6_5"
    model = "claude-sonnet-4-6"

    def _execute(self, payload: A51Input) -> A51Output:
        cited_block = "\n\n".join(
            f"--- CITED SOURCE {i + 1} (full text below) ---\n{t[:30000]}"
            for i, t in enumerate(payload.cited_pdf_full_texts)
        )
        user = (
            f"PARAGRAPH (idx={payload.paragraph_idx}):\n<<<\n{payload.paragraph_verbatim}\n>>>\n\n"
            f"{cited_block}"
        )
        result = call_structured(
            model=self.model,
            system=_A51_SYSTEM,
            user=user,
            output_cls=A51Output,
            tool_name="emit_gestalt",
            tool_description="Emit paragraph gestalt verdict with verbatim evidence.",
            max_tokens=4096,
        )
        # Force-fill paragraph_idx + verbatim to satisfy verifier
        g = result.output.gestalt
        return A51Output(
            gestalt=ParagraphGestalt(
                paragraph_idx=payload.paragraph_idx,
                paragraph_verbatim=payload.paragraph_verbatim,
                verdict=g.verdict,
                evidence_sentences=g.evidence_sentences,
                evidence_source_passages=g.evidence_source_passages,
                confidence=g.confidence,
            )
        )

    def _verify(self, payload: A51Input, output: A51Output) -> None:
        g = output.gestalt
        if g.paragraph_idx != payload.paragraph_idx:
            self.fail("paragraph_idx mismatch")
        if g.paragraph_verbatim != payload.paragraph_verbatim:
            self.fail("paragraph_verbatim mismatch")
        if g.verdict != GestaltVerdict.CALIBRATED:
            if not g.evidence_sentences:
                self.fail(f"verdict {g.verdict} requires at least one evidence_sentence")
            if not g.evidence_source_passages:
                self.fail(f"verdict {g.verdict} requires at least one evidence_source_passage")
            for ev in g.evidence_sentences:
                if ev not in payload.paragraph_verbatim:
                    self.fail(f"evidence_sentence {ev!r} not found in paragraph")
            for src in g.evidence_source_passages:
                if not any(src in full for full in payload.cited_pdf_full_texts):
                    self.fail(f"evidence_source_passage {src!r} not found in any cited PDF")


# ---------- A52: Sentence-Pair Connective ----------


class A52Input(_Frozen):
    paragraph_idx: int
    paragraph_verbatim: str


class A52Output(_Frozen):
    records: list[ConnectiveRecord]


_A52_SYSTEM = """You audit logical connectives between adjacent sentences in an academic paragraph.

For every pair of adjacent sentences joined by an explicit connective (e.g. "therefore", "however",
"thus", "in contrast", "consistent with"), classify:
- expected_relation: the relation the connective WORD asserts (e.g. "therefore" → CAUSAL/EVIDENTIAL).
- actual_relation: the relation the SEMANTIC content actually exhibits.
- match: whether they agree.

Use these LogicalRelation values: CAUSAL, CORRELATIONAL, CONTRASTIVE, ADDITIVE, CONCESSIVE,
TEMPORAL, EVIDENTIAL.

Only emit records for the explicit connectives listed below — do NOT invent connectives that
aren't in the paragraph:
  therefore, thus, hence, consequently, however, in contrast, by contrast,
  consistent with, in line with, in support of, in addition, moreover, furthermore,
  although, even though, whereas, while, because, since

Spans are 0-indexed offsets into the paragraph_verbatim, not the manuscript."""


class ConnectiveAgent(LLMAgent[A52Input, A52Output]):
    name = "A52_connective"
    wave = "W6_5"
    model = "claude-haiku-4-5"

    KNOWN_CONNECTIVES = frozenset(
        {
            "therefore",
            "thus",
            "hence",
            "consequently",
            "however",
            "in contrast",
            "by contrast",
            "consistent with",
            "in line with",
            "in support of",
            "in addition",
            "moreover",
            "furthermore",
            "although",
            "even though",
            "whereas",
            "while",
            "because",
            "since",
        }
    )

    def _execute(self, payload: A52Input) -> A52Output:
        result = call_structured(
            model=self.model,
            system=_A52_SYSTEM,
            user=f"Paragraph:\n<<<\n{payload.paragraph_verbatim}\n>>>",
            output_cls=A52Output,
            tool_name="emit_connectives",
            tool_description="Emit connective records.",
            max_tokens=4096,
        )
        return result.output

    def _verify(self, payload: A52Input, output: A52Output) -> None:
        text = payload.paragraph_verbatim
        for r in output.records:
            if r.connective.lower() not in self.KNOWN_CONNECTIVES:
                self.fail(f"connective {r.connective!r} not in known set")
            if r.expected_relation not in LogicalRelation:
                self.fail(f"expected_relation {r.expected_relation!r} not in enum")
            if r.actual_relation not in LogicalRelation:
                self.fail(f"actual_relation {r.actual_relation!r} not in enum")
            for span in (r.sentence_a_span, r.sentence_b_span):
                s, e = span
                if not (0 <= s < e <= len(text)):
                    self.fail("sentence span out of paragraph bounds")
            if r.match != (r.expected_relation == r.actual_relation):
                self.fail("match flag inconsistent with relation comparison")


# ---------- A53: Attribution Inflation ----------


class A53Input(_Frozen):
    unit_id: str
    ref_num: int
    manuscript_claim_verbatim: str
    source_pdf_text: str


class A53Output(_Frozen):
    inflation: AttributionInflation


_A53_SYSTEM = """You detect attribution inflation: when a manuscript's claim is STRONGER than what
the source actually says.

Mechanisms:
- STRENGTHENED_VERB: "associated with" → "causes"; "may" → "does"
- DROPPED_QUALIFIER: "in a subset of participants" omitted; "among older adults" omitted
- GENERALIZED_POPULATION: "US adults" → "adults"; "men aged 50+" → "people"
- TEMPORAL_SHIFT: cross-sectional finding presented as longitudinal
- SAMPLE_TO_POPULATION: "in our sample" → "in the population"
- ASSOCIATION_TO_CAUSATION: correlation phrased as causation

OUTPUT REQUIREMENTS:
- source_language: a VERBATIM phrase from the source PDF showing the cautious original.
- manuscript_language: a VERBATIM phrase from the manuscript claim showing the inflated version.
- inflation_score ∈ [0, 1]: 0 = no inflation; 1 = fabrication.
- If you cannot find verbatim source phrases (e.g. the source uses different sentence
  structure), return inflation_score=0.0 and mechanism=STRENGTHENED_VERB with empty
  language strings — that signals "no detected inflation."

The source text given to you is the FIRST PORTION of the source PDF. Look only in that text."""


class AttributionInflationAgent(LLMAgent[A53Input, A53Output]):
    name = "A53_attribution_inflation"
    wave = "W6_5"
    model = "claude-sonnet-4-6"

    def _execute(self, payload: A53Input) -> A53Output:
        user = (
            f"Manuscript claim:\n<<<\n{payload.manuscript_claim_verbatim}\n>>>\n\n"
            f"Source PDF text (truncated):\n<<<\n{payload.source_pdf_text[:30000]}\n>>>"
        )
        result = call_structured(
            model=self.model,
            system=_A53_SYSTEM,
            user=user,
            output_cls=A53Output,
            tool_name="emit_inflation",
            tool_description="Emit attribution inflation analysis.",
            max_tokens=2048,
        )
        # Tolerate empty-language signal: if both empty, return with score=0
        inf = result.output.inflation
        if not inf.source_language and not inf.manuscript_language:
            return A53Output(
                inflation=AttributionInflation(
                    inflation_score=0.0,
                    source_language="",
                    manuscript_language="",
                    mechanism=InflationMechanism.STRENGTHENED_VERB,
                    confidence=inf.confidence,
                )
            )
        return result.output

    def _verify(self, payload: A53Input, output: A53Output) -> None:
        inf = output.inflation
        # If both empty, treat as "no inflation detected" — skip verbatim checks
        if not inf.source_language and not inf.manuscript_language:
            if inf.inflation_score > 0.0:
                self.fail("no language reported but inflation_score > 0")
            return
        if inf.source_language and inf.source_language not in payload.source_pdf_text:
            self.fail(f"source_language {inf.source_language!r} not verbatim in source PDF")
        if (
            inf.manuscript_language
            and inf.manuscript_language not in payload.manuscript_claim_verbatim
        ):
            self.fail(
                f"manuscript_language {inf.manuscript_language!r} not in manuscript claim"
            )
        if inf.mechanism not in InflationMechanism:
            self.fail(f"mechanism {inf.mechanism!r} not in enum")


# ---------- A54: Omitted Caveat ----------


class A54Input(_Frozen):
    unit_id: str
    ref_num: int
    manuscript_claim_verbatim: str
    source_pdf_text: str


class A54Output(_Frozen):
    omitted: list[OmittedCaveat]


_A54_SYSTEM = """You detect caveats / limitations / counter-evidence in a source PDF that the
manuscript omits when citing it.

Look for:
- A "Limitations" section enumerating threats to validity.
- Hedging like "we cannot rule out", "future work needed", "the effect was attenuated".
- Subgroup analyses where the main finding did NOT replicate.
- Contradictory results in the paper's Discussion.

OUTPUT: list of OmittedCaveat. Each caveat_verbatim MUST be an exact substring of source_pdf_text.
severity:
- HIGH: a caveat that materially weakens the manuscript's claim if surfaced.
- MEDIUM: a qualifier the manuscript should have included.
- LOW: minor methodological note.

If no omitted caveats, return an empty list. Do NOT invent caveats."""


class OmittedCaveatAgent(LLMAgent[A54Input, A54Output]):
    name = "A54_omitted_caveat"
    wave = "W6_5"
    model = "claude-sonnet-4-6"

    def _execute(self, payload: A54Input) -> A54Output:
        user = (
            f"Manuscript claim:\n<<<\n{payload.manuscript_claim_verbatim}\n>>>\n\n"
            f"Source PDF text (truncated):\n<<<\n{payload.source_pdf_text[:30000]}\n>>>"
        )
        result = call_structured(
            model=self.model,
            system=_A54_SYSTEM,
            user=user,
            output_cls=A54Output,
            tool_name="emit_caveats",
            tool_description="Emit omitted caveats found in source PDF.",
            max_tokens=4096,
        )
        # Drop any caveats whose verbatim isn't in source — verifier requires it
        return A54Output(
            omitted=[
                c for c in result.output.omitted if c.caveat_verbatim in payload.source_pdf_text
            ]
        )

    def _verify(self, payload: A54Input, output: A54Output) -> None:
        for c in output.omitted:
            if c.caveat_verbatim not in payload.source_pdf_text:
                self.fail(f"caveat {c.caveat_verbatim!r} not verbatim in source PDF")


# ---------- A55: Causal Language Audit ----------


class A55Input(_Frozen):
    unit_id: str
    ref_num: int
    manuscript_sentence: str
    source_design: StudyDesign


class A55Output(_Frozen):
    audit: CausalLanguageAudit


class CausalLanguageAuditAgent(DeterministicAgent[A55Input, A55Output]):
    """Pure-Python detection of causal language; rule-based justification check.
    Hard-coded list of causal verbs vs. study-design lookup. No LLM needed."""

    name = "A55_causal_language_audit"
    wave = "W6_5"

    CAUSAL_WORDS = frozenset(
        {
            "cause",
            "causes",
            "caused",
            "causing",
            "causal",
            "causation",
            "drive",
            "drives",
            "driven",
            "leads to",
            "led to",
            "results in",
            "induces",
            "induced",
            "produces",
            "produced",
            "triggers",
            "triggered",
            "due to",
        }
    )

    CAUSAL_JUSTIFIED_DESIGNS = frozenset(
        {
            StudyDesign.RCT,
            StudyDesign.QUASI_EXPERIMENT,
            StudyDesign.MENDELIAN_RANDOMIZATION,
            StudyDesign.INSTRUMENTAL_VARIABLE,
        }
    )

    @classmethod
    def _causal_words_in(cls, text: str) -> list[str]:
        low = text.lower()
        found = []
        for word in cls.CAUSAL_WORDS:
            if re.search(r"\b" + re.escape(word) + r"\b", low):
                found.append(word)
        return found

    def _execute(self, payload: A55Input) -> A55Output:
        found = self._causal_words_in(payload.manuscript_sentence)
        if not found:
            return A55Output(
                audit=CausalLanguageAudit(
                    causal_words_in_manuscript=[],
                    source_design=payload.source_design,
                    justified=True,
                    rationale="no causal language in manuscript sentence",
                )
            )
        justified = payload.source_design in self.CAUSAL_JUSTIFIED_DESIGNS
        if justified:
            rationale = f"design {payload.source_design.value} supports causal language"
        else:
            rationale = (
                f"design {payload.source_design.value} does NOT support causal language; "
                f"observational designs cannot establish causation"
            )
        return A55Output(
            audit=CausalLanguageAudit(
                causal_words_in_manuscript=found,
                source_design=payload.source_design,
                justified=justified,
                rationale=rationale,
            )
        )

    def _verify(self, payload: A55Input, output: A55Output) -> None:
        audit = output.audit
        expected_justified = payload.source_design in self.CAUSAL_JUSTIFIED_DESIGNS
        if audit.causal_words_in_manuscript:
            if audit.justified != expected_justified:
                self.fail(
                    f"justified={audit.justified} disagrees with design "
                    f"{payload.source_design} (expected {expected_justified})"
                )
        else:
            if audit.justified is False:
                self.fail("no causal words but justified=False (should be True/N-A)")
        for w in audit.causal_words_in_manuscript:
            if not re.search(
                r"\b" + re.escape(w) + r"\b", payload.manuscript_sentence.lower()
            ):
                self.fail(f"causal word {w!r} not found in manuscript sentence")


# ---------- A56: Numeric Claim Verifier ----------


class A56Input(_Frozen):
    unit_id: str
    ref_num: int
    manuscript_text: str
    source_pdf_text: str


class A56Output(_Frozen):
    claims: list[NumericClaim]


_NUMBER_RE = re.compile(
    r"(?<![\w.])(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*"
    r"(%|percent|kg|g|mg|year[s]?|month[s]?|day[s]?|h|hr[s]?|min|fold|×|x)?"
)


class NumericClaimVerifier(DeterministicAgent[A56Input, A56Output]):
    """Pure-Python numeric verification. For every number in the manuscript text,
    search the source PDF for the same number (with rounding tolerance).
    DIRECTION_FLIP detection is heuristic: matches +/- signs and 'increase' vs 'decrease'
    nearby in source vs manuscript."""

    name = "A56_numeric_claim_verifier"
    wave = "W6_5"

    ROUND_TOLERANCE = 0.05

    def _execute(self, payload: A56Input) -> A56Output:
        claims: list[NumericClaim] = []
        src = payload.source_pdf_text
        for m in _NUMBER_RE.finditer(payload.manuscript_text):
            try:
                num = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            units = m.group(2)
            # Skip noisy single-digit ints unless they have units
            if abs(num) < 10 and units is None:
                continue
            match_type = NumericMatch.NOT_FOUND
            number_in_source: float | None = None
            # 1) Verbatim string
            literal = m.group(0).strip()
            if literal in src:
                match_type = NumericMatch.VERBATIM
                number_in_source = num
            else:
                # 2) Rounded match — scan source numbers
                for sm in _NUMBER_RE.finditer(src):
                    try:
                        src_num = float(sm.group(1).replace(",", ""))
                    except ValueError:
                        continue
                    if num == 0:
                        if src_num == 0:
                            match_type = NumericMatch.VERBATIM
                            number_in_source = src_num
                            break
                        continue
                    rel = abs(src_num - num) / abs(num)
                    if rel <= self.ROUND_TOLERANCE:
                        match_type = NumericMatch.ROUNDED
                        number_in_source = src_num
                        break
            claims.append(
                NumericClaim(
                    number_in_manuscript=num,
                    units_in_manuscript=units,
                    number_in_source=number_in_source,
                    units_in_source=None,
                    match=match_type,
                    source_page=None,
                    confidence=0.8 if match_type != NumericMatch.NOT_FOUND else 0.4,
                )
            )
        return A56Output(claims=claims)

    def _verify(self, payload: A56Input, output: A56Output) -> None:
        for c in output.claims:
            if c.match == NumericMatch.VERBATIM:
                if c.number_in_source is None:
                    self.fail("VERBATIM but number_in_source is None")
                if c.number_in_source != c.number_in_manuscript:
                    self.fail(
                        f"VERBATIM but numbers differ: "
                        f"manuscript={c.number_in_manuscript} source={c.number_in_source}"
                    )


# ---------- A57: Narrative Coherence ----------


class A57Input(_Frozen):
    paragraph_idx: int
    paragraph_verbatim: str
    cited_ref_nums: list[int]


class A57Output(_Frozen):
    narrative: NarrativeCoherence


_A57_SYSTEM = """You audit narrative coherence in an academic paragraph.

inference_chain: ordered list of premises and conclusions. E.g.
  ["Sleep deprivation increases inflammation [12].",
   "Inflammation is associated with cardiovascular events [13].",
   "Therefore sleep deprivation increases cardiovascular events [unsupported]."]

chain_citations: parallel list of ref_nums that support each link. Use 0 for unsupported links.

breaks: indices into inference_chain where the logical flow is broken (e.g., conclusion does
not actually follow from premises, or required premise is missing).

verdict:
- SOUND: chain holds end-to-end.
- BROKEN: at least one break.
- UNVERIFIABLE: not enough info in paragraph to judge."""


class NarrativeCoherenceAgent(LLMAgent[A57Input, A57Output]):
    name = "A57_narrative_coherence"
    wave = "W6_5"
    model = "claude-sonnet-4-6"

    def _execute(self, payload: A57Input) -> A57Output:
        user = (
            f"Paragraph (cited refs: {payload.cited_ref_nums}):\n<<<\n"
            f"{payload.paragraph_verbatim}\n>>>"
        )
        result = call_structured(
            model=self.model,
            system=_A57_SYSTEM,
            user=user,
            output_cls=A57Output,
            tool_name="emit_narrative",
            tool_description="Emit narrative coherence judgment.",
            max_tokens=2048,
        )
        # Filter chain_citations to those actually in cited_ref_nums (verifier requires it)
        n = result.output.narrative
        filtered_chain = [
            r for r in n.chain_citations if r in payload.cited_ref_nums
        ]
        return A57Output(
            narrative=NarrativeCoherence(
                inference_chain=n.inference_chain,
                chain_citations=filtered_chain,
                breaks=n.breaks,
                verdict=n.verdict,
                confidence=n.confidence,
            )
        )

    def _verify(self, payload: A57Input, output: A57Output) -> None:
        n = output.narrative
        if not n.inference_chain:
            self.fail("inference_chain empty")
        for b in n.breaks:
            if not (0 <= b < len(n.inference_chain)):
                self.fail(f"break index {b} out of chain bounds")
        if n.verdict == NarrativeVerdict.BROKEN and not n.breaks:
            self.fail("BROKEN verdict but no breaks listed")
        if n.verdict == NarrativeVerdict.SOUND and n.breaks:
            self.fail("SOUND verdict but breaks listed")
        for ref in n.chain_citations:
            if ref not in payload.cited_ref_nums:
                self.fail(f"chain cites ref {ref} not in unit's cited_ref_nums")


# ---------- A58: Hedging Calibration ----------


class A58Input(_Frozen):
    unit_id: str
    ref_num: int
    manuscript_sentence: str
    source_pdf_text: str


class A58Output(_Frozen):
    hedging: HedgingCalibration


class HedgingCalibrationAgent(DeterministicAgent[A58Input, A58Output]):
    """Hard-coded hedge lexicon → 1..5 strength. Manuscript strength = max strength
    found in manuscript_sentence; source strength = max found in source_pdf_text.
    No LLM needed; the lexicon is the rubric."""

    name = "A58_hedging_calibration"
    wave = "W6_5"

    HEDGE_LEXICON: dict[str, int] = {
        "might": 1,
        "could possibly": 1,
        "perhaps": 1,
        "speculatively": 1,
        "may": 2,
        "suggests": 2,
        "indicates": 2,
        "is possible": 2,
        "appears": 3,
        "consistent with": 3,
        "supports": 3,
        "shows": 4,
        "demonstrates": 4,
        "establishes": 4,
        "proves": 5,
        "definitively": 5,
        "confirms": 5,
    }

    @classmethod
    def _strength(cls, text: str) -> tuple[list[str], int]:
        low = text.lower()
        found: list[str] = []
        max_str = 0
        for word, strength in cls.HEDGE_LEXICON.items():
            if re.search(r"\b" + re.escape(word) + r"\b", low):
                found.append(word)
                if strength > max_str:
                    max_str = strength
        if max_str == 0:
            max_str = 3  # neutral baseline if no hedges
        return found, max_str

    def _execute(self, payload: A58Input) -> A58Output:
        ms_words, ms_strength = self._strength(payload.manuscript_sentence)
        src_words, src_strength = self._strength(payload.source_pdf_text)
        delta = ms_strength - src_strength
        return A58Output(
            hedging=HedgingCalibration(
                manuscript_hedge_words=ms_words,
                manuscript_hedge_strength=ms_strength,
                source_hedge_words=src_words,
                source_hedge_strength=src_strength,
                delta=delta,
                calibrated=abs(delta) <= 1,
            )
        )

    def _verify(self, payload: A58Input, output: A58Output) -> None:
        h = output.hedging
        if h.delta != h.manuscript_hedge_strength - h.source_hedge_strength:
            self.fail("delta inconsistent with strengths")
        if h.calibrated != (abs(h.delta) <= 1):
            self.fail("calibrated flag inconsistent with delta")
        for w in h.manuscript_hedge_words:
            if w.lower() not in payload.manuscript_sentence.lower():
                self.fail(f"hedge word {w!r} not in manuscript sentence")
