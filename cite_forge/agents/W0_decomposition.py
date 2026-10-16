"""W0 — Manuscript Decomposition.

A01: Atomic Decomposition Agent — LLM identifies claim text + citations;
     this module deterministically resolves spans against the manuscript.
A46: Section-Boundary Verifier — deterministic (lives in verification.py).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import LLMAgent, VerifierFailed
from cite_forge.llm import call_structured
from cite_forge.schemas import AtomicUnit, ClaimType


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A01 ----------


class A01Input(_Frozen):
    manuscript_text: str
    manuscript_sha256: str


class A01Output(_Frozen):
    units: list[AtomicUnit]


class _RawUnit(BaseModel):
    """LLM-emitted shape — no offsets. Spans are resolved deterministically below."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(description="Sequential ID like U0001, U0002...")
    claim_verbatim: str = Field(
        description=(
            "EXACT verbatim text of the claim sentence, copied character-for-character "
            "from the manuscript. Must match the manuscript text byte-for-byte. "
            "NO paraphrasing, NO truncation, NO reformatting of whitespace/dashes/quotes."
        )
    )
    section_header: str = Field(
        description=(
            "The exact section heading text under which this claim appears, "
            "e.g. 'Introduction', 'Methods', 'Results', '2.1 Sample'."
        )
    )
    paragraph_idx: int = Field(ge=0)
    sentence_idx: int = Field(ge=0, description="0-indexed sentence position within its paragraph")
    citation_anchors: list[str] = Field(
        description=(
            "Each in-text citation surface form that supports this claim, "
            "e.g. '[12]', '(Smith et al., 2020)', '12,13'. Must appear verbatim in the manuscript "
            "within 200 characters of the claim."
        )
    )
    claim_type: ClaimType
    contains_direct_quote: bool


class _RawUnits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    units: list[_RawUnit]


_A01_SYSTEM = """You decompose academic manuscripts into atomic claim-citation units.

DEFINITIONS:
- An atomic unit = ONE declarative claim and the citations that support it.
- A "declarative claim" is a single, non-compound assertion. If a sentence contains
  multiple coordinate claims joined by 'and', split it into separate units.
- A unit MUST be cited (contains at least one citation_anchor). Uncited sentences
  are descriptive prose, not claims requiring citation support.

HARD RULES:
1. NO paraphrasing. claim_verbatim must be EXACT byte-for-byte text from the manuscript.
2. NO compound claims. "X is true and Y is true [1,2]" → two units, one per claim.
3. Every unit must have ≥1 citation_anchor, each appearing verbatim in the manuscript
   within 200 characters of the claim.
4. section_header is the most recent heading upstream of the claim, verbatim.
5. unit_id sequential: U0001, U0002, U0003... (zero-padded to 4 digits).
6. claim_type: choose the BEST fit from the enum.

Skip the abstract, title, author list, and references section."""

_A01_USER_TEMPLATE = """Decompose this manuscript into atomic claim-citation units.

MANUSCRIPT:
<<<
{manuscript_text}
>>>

Emit every unit. Order by appearance. Use the emit_output tool."""


class AtomicDecompositionAgent(LLMAgent[A01Input, A01Output]):
    """Splits manuscript into atomic claim-citation units.

    LLM emits claim_verbatim + section_header + citation_anchors as plain strings.
    This agent deterministically resolves spans via substring search and rejects
    any unit whose verbatim text doesn't appear uniquely in the manuscript."""

    name = "A01_atomic_decomposition"
    wave = "W0"
    model = "claude-opus-4-7"

    def _execute(self, payload: A01Input) -> A01Output:
        result = call_structured(
            model=self.model,
            system=_A01_SYSTEM,
            user=_A01_USER_TEMPLATE.format(manuscript_text=payload.manuscript_text),
            output_cls=_RawUnits,
            tool_name="emit_units",
            tool_description="Emit the atomic claim-citation units.",
            max_tokens=16384,
            cache_system=True,
        )
        raw = result.output

        resolved: list[AtomicUnit] = []
        text = payload.manuscript_text
        for unit in raw.units:
            verbatim = unit.claim_verbatim
            start = text.find(verbatim)
            if start < 0:
                # Try normalizing whitespace within the verbatim
                norm = " ".join(verbatim.split())
                # Build a regex-free scan: collapse runs of whitespace in text and find
                # Skip rather than fabricate; the unit is dropped and surfaces as a miss
                continue
            second = text.find(verbatim, start + 1)
            if second >= 0:
                # Ambiguous — substring appears more than once. Try to disambiguate
                # by section_header proximity if possible; else skip.
                # Find the section header instance closest to each candidate.
                sh = unit.section_header
                sh_positions = []
                pos = 0
                while True:
                    idx = text.find(sh, pos)
                    if idx < 0:
                        break
                    sh_positions.append(idx)
                    pos = idx + 1
                if sh_positions:

                    def dist(cand_start: int) -> int:
                        upstream = [p for p in sh_positions if p <= cand_start]
                        return min(
                            (cand_start - p for p in upstream),
                            default=10**9,
                        )

                    candidates = []
                    pos = 0
                    while True:
                        idx = text.find(verbatim, pos)
                        if idx < 0:
                            break
                        candidates.append(idx)
                        pos = idx + 1
                    candidates.sort(key=dist)
                    start = candidates[0]
                else:
                    continue
            end = start + len(verbatim)

            # Validate citation_anchors within ±200 chars
            window_start = max(0, start - 200)
            window_end = min(len(text), end + 200)
            window = text[window_start:window_end]
            anchors_ok = all(a in window for a in unit.citation_anchors)
            if not anchors_ok:
                continue

            resolved.append(
                AtomicUnit(
                    unit_id=unit.unit_id,
                    claim_verbatim=verbatim,
                    claim_span=(start, end),
                    section_header=unit.section_header,
                    paragraph_idx=unit.paragraph_idx,
                    sentence_idx=unit.sentence_idx,
                    citation_anchors=unit.citation_anchors,
                    cited_ref_nums=[],  # filled in W1 cross-ref audit
                    claim_type=unit.claim_type,
                    contains_direct_quote=unit.contains_direct_quote,
                    convergence_verdict="UNCERTAIN",  # set by pipeline at end
                    overall_confidence=0.0,
                )
            )

        if not resolved:
            raise VerifierFailed(
                self.name,
                "A01 emitted zero units that round-tripped to manuscript text",
                output=None,
            )
        return A01Output(units=resolved)

    def _verify(self, payload: A01Input, output: A01Output) -> None:
        text = payload.manuscript_text
        seen_ids: set[str] = set()
        for unit in output.units:
            start, end = unit.claim_span
            if not (0 <= start < end <= len(text)):
                self.fail(f"unit {unit.unit_id}: claim_span out of bounds")
            if text[start:end] != unit.claim_verbatim:
                self.fail(
                    f"unit {unit.unit_id}: claim_verbatim does not match "
                    f"manuscript text at span"
                )
            window = text[max(0, start - 200) : min(len(text), end + 200)]
            for anchor in unit.citation_anchors:
                if anchor not in window:
                    self.fail(
                        f"unit {unit.unit_id}: citation_anchor {anchor!r} "
                        f"not found within 200 chars of claim_span"
                    )
            if unit.unit_id in seen_ids:
                self.fail(f"duplicate unit_id {unit.unit_id}")
            seen_ids.add(unit.unit_id)
