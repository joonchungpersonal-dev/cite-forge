"""W1 — Discovery (parallel).

A02 Citation Extraction (LLM, span-resolved deterministically)
A03 Bibliography Parsing (LLM)
A04 Cross-Reference Audit (Deterministic, fully implemented)
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import DeterministicAgent, LLMAgent, VerifierFailed
from cite_forge.llm import call_structured
from cite_forge.schemas import Author, Bibliography

CURRENT_YEAR = datetime.now().year

DOI_REGEX = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9%]+$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A02: Citation Extraction ----------


class SurfaceCitation(_Frozen):
    surface: str
    span: tuple[int, int]
    normalized_first_author: str
    normalized_year: int | None
    inferred_ref_num: int | None = None


class A02Input(_Frozen):
    manuscript_text: str


class A02Output(_Frozen):
    citations: list[SurfaceCitation]


class _RawCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str = Field(
        description=(
            "EXACT in-text citation surface, byte-for-byte from manuscript. "
            "Examples: '[12]', '(Smith et al., 2020)', '12,13', '[12-15]', 'Smith et al. (2020)'."
        )
    )
    normalized_first_author: str = Field(
        description="First author surname; for numeric styles, leave blank string ''"
    )
    normalized_year: int | None = Field(
        description="Cited year as int; null for numeric-only styles"
    )
    inferred_ref_num: int | None = Field(
        description=(
            "If numeric style like '[12]', the ref number. "
            "For ranges like '[12-15]', leave null (A04 expands)."
        )
    )


class _RawCitations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    citations: list[_RawCitation]


_A02_SYSTEM = """You extract every in-text citation surface form from an academic manuscript.

WHAT COUNTS:
- Bracketed numerics: [12], [12,13], [12-15], [12, 14-16]
- Parenthetical authors: (Smith, 2020), (Smith et al., 2020), (Smith and Jones, 2020)
- Narrative: Smith et al. (2020) showed... → surface is "Smith et al. (2020)"
- Inline numerics: superscript 12 or 12,13 attached to a word

WHAT DOESN'T COUNT:
- References section entries themselves
- Table/figure callouts ("Table 1", "Figure 3")
- Equation references ("Eq. 5")
- Section/page numbers
- Self-references to the same manuscript

HARD RULES:
1. surface must be EXACT byte-for-byte from the manuscript.
2. For ranges [12-15], emit ONE entry with inferred_ref_num=null (A04 will expand).
3. For comma lists [12,13,14], emit ONE entry per range or as one surface — be consistent."""

_A02_USER_TEMPLATE = """Extract every in-text citation from this manuscript.
Skip the bibliography/references section at the end.

MANUSCRIPT:
<<<
{manuscript_text}
>>>

Emit every citation in order. Use the emit_citations tool."""


class CitationExtractionAgent(LLMAgent[A02Input, A02Output]):
    name = "A02_citation_extraction"
    wave = "W1"
    model = "claude-sonnet-4-6"

    def _execute(self, payload: A02Input) -> A02Output:
        result = call_structured(
            model=self.model,
            system=_A02_SYSTEM,
            user=_A02_USER_TEMPLATE.format(manuscript_text=payload.manuscript_text),
            output_cls=_RawCitations,
            tool_name="emit_citations",
            tool_description="Emit every in-text citation surface form.",
            max_tokens=16384,
        )
        raw = result.output
        text = payload.manuscript_text

        # Deterministically resolve spans. Allow multiple occurrences — emit
        # one SurfaceCitation per occurrence to preserve count.
        emitted: list[SurfaceCitation] = []
        seen_at: dict[str, int] = {}  # surface -> next start to search from
        for c in raw.citations:
            surface = c.surface
            start_from = seen_at.get(surface, 0)
            start = text.find(surface, start_from)
            if start < 0:
                # First-occurrence retry from 0 in case order was permuted
                start = text.find(surface)
                if start < 0 or start < start_from:
                    continue
            end = start + len(surface)
            seen_at[surface] = end
            year = c.normalized_year
            if year is not None and not (1500 <= year <= CURRENT_YEAR + 1):
                continue
            emitted.append(
                SurfaceCitation(
                    surface=surface,
                    span=(start, end),
                    normalized_first_author=c.normalized_first_author or "",
                    normalized_year=year,
                    inferred_ref_num=c.inferred_ref_num,
                )
            )
        return A02Output(citations=emitted)

    def _verify(self, payload: A02Input, output: A02Output) -> None:
        text = payload.manuscript_text
        for c in output.citations:
            s, e = c.span
            if not (0 <= s < e <= len(text)):
                self.fail(f"citation {c.surface!r}: span out of bounds")
            if text[s:e] != c.surface:
                self.fail(f"citation {c.surface!r}: surface mismatch at span")
            if c.normalized_year is not None and not (
                1500 <= c.normalized_year <= CURRENT_YEAR + 1
            ):
                self.fail(
                    f"citation {c.surface!r}: year {c.normalized_year} out of range"
                )


# ---------- A03: Bibliography Parsing ----------


class A03Input(_Frozen):
    bibliography_text: str


class A03Output(_Frozen):
    entries: list[Bibliography]
    declared_dois: list[str | None]


class _RawAuthor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name_verbatim: str
    given: str | None = None
    family: str
    position: int = Field(ge=1)


class _RawBibEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authors: list[_RawAuthor]
    title: str
    journal: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    year: int = Field(ge=1500, le=2100)
    publisher: str | None = None
    declared_doi: str | None = Field(
        default=None, description="DOI if printed in the bib entry; null otherwise"
    )


class _RawBib(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[_RawBibEntry]


_A03_SYSTEM = """You parse the bibliography section of an academic manuscript into structured records.

HARD RULES:
1. Preserve the manuscript's reference numbering (entry index = ref_num - 1).
2. authors: every author in order. position is 1-indexed (1st author = position 1).
3. family = surname only; given = given names/initials.
4. year: 4-digit publication year.
5. declared_doi: include ONLY if a DOI is literally printed in the entry; do not invent.
6. journal/title: do not paraphrase or abbreviate beyond what the entry shows."""

_A03_USER_TEMPLATE = """Parse this bibliography section into structured entries.

BIBLIOGRAPHY:
<<<
{bibliography_text}
>>>

Emit one entry per reference in the order they appear. Use the emit_bib tool."""


class BibliographyParsingAgent(LLMAgent[A03Input, A03Output]):
    name = "A03_bibliography_parsing"
    wave = "W1"
    model = "claude-sonnet-4-6"

    def _execute(self, payload: A03Input) -> A03Output:
        result = call_structured(
            model=self.model,
            system=_A03_SYSTEM,
            user=_A03_USER_TEMPLATE.format(bibliography_text=payload.bibliography_text),
            output_cls=_RawBib,
            tool_name="emit_bib",
            tool_description="Emit the parsed bibliography entries.",
            max_tokens=16384,
        )
        entries: list[Bibliography] = []
        dois: list[str | None] = []
        for e in result.output.entries:
            entries.append(
                Bibliography(
                    authors=[
                        Author(
                            name_verbatim=a.name_verbatim,
                            given=a.given,
                            family=a.family,
                            position=a.position,
                        )
                        for a in e.authors
                    ],
                    title=e.title,
                    journal=e.journal,
                    volume=e.volume,
                    issue=e.issue,
                    pages=e.pages,
                    year=e.year,
                    publisher=e.publisher,
                )
            )
            doi = e.declared_doi
            if doi and not DOI_REGEX.match(doi):
                doi = None
            dois.append(doi)
        if not entries:
            raise VerifierFailed(self.name, "no bibliography entries parsed", output=None)
        return A03Output(entries=entries, declared_dois=dois)

    def _verify(self, payload: A03Input, output: A03Output) -> None:
        if len(output.declared_dois) != len(output.entries):
            self.fail("declared_dois length != entries length")
        for i, (entry, doi) in enumerate(zip(output.entries, output.declared_dois)):
            if not entry.authors:
                self.fail(f"entry {i}: no authors")
            if not entry.title.strip():
                self.fail(f"entry {i}: empty title")
            if doi is not None and not DOI_REGEX.match(doi):
                self.fail(f"entry {i}: DOI {doi!r} fails regex")


# ---------- A04: Cross-Reference Audit (deterministic, unchanged) ----------


class A04Input(_Frozen):
    in_text_citations: list[SurfaceCitation]
    bibliography_entries: list[Bibliography]
    bibliography_dois: list[str | None]


class CrossRefMapping(_Frozen):
    surface: str
    matched_entry_idx: int | None


class A04Output(_Frozen):
    mappings: list[CrossRefMapping]
    expanded_range_citations: list[tuple[str, list[int]]]
    orphan_in_text: list[str]
    orphan_bib_indices: list[int]


class CrossReferenceAuditAgent(DeterministicAgent[A04Input, A04Output]):
    """Pure Python matching. Heuristic: first_author surname + year."""

    name = "A04_cross_reference_audit"
    wave = "W1"

    @staticmethod
    def _expand_range(surface: str) -> list[int] | None:
        m = re.fullmatch(r"\[?(\d+)\s*[-–]\s*(\d+)\]?", surface.strip())
        if not m:
            return None
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo >= hi or hi - lo > 100:
            return None
        return list(range(lo, hi + 1))

    @staticmethod
    def _expand_list(surface: str) -> list[int] | None:
        s = surface.strip().strip("[]()")
        if not re.fullmatch(r"\d+(\s*,\s*\d+)+", s):
            return None
        return [int(p.strip()) for p in s.split(",")]

    @staticmethod
    def _matches(cite: SurfaceCitation, entry: Bibliography) -> bool:
        if cite.normalized_year and cite.normalized_year != entry.year:
            return False
        cite_surname = cite.normalized_first_author.lower().strip()
        if not cite_surname:
            return False
        entry_surname = entry.authors[0].family.lower().strip() if entry.authors else ""
        return cite_surname == entry_surname

    def _execute(self, payload: A04Input) -> A04Output:
        mappings: list[CrossRefMapping] = []
        expansions: list[tuple[str, list[int]]] = []
        cited_idxs: set[int] = set()
        orphans: list[str] = []

        for cite in payload.in_text_citations:
            rng = self._expand_range(cite.surface)
            if rng is None:
                rng = self._expand_list(cite.surface)
            if rng is not None:
                expansions.append((cite.surface, rng))
                for ref_num in rng:
                    idx = ref_num - 1
                    if 0 <= idx < len(payload.bibliography_entries):
                        cited_idxs.add(idx)
                mappings.append(CrossRefMapping(surface=cite.surface, matched_entry_idx=None))
                continue

            if cite.inferred_ref_num is not None:
                idx = cite.inferred_ref_num - 1
                if 0 <= idx < len(payload.bibliography_entries):
                    cited_idxs.add(idx)
                    mappings.append(
                        CrossRefMapping(surface=cite.surface, matched_entry_idx=idx)
                    )
                    continue

            matched: int | None = None
            for i, entry in enumerate(payload.bibliography_entries):
                if self._matches(cite, entry):
                    matched = i
                    cited_idxs.add(i)
                    break
            mappings.append(CrossRefMapping(surface=cite.surface, matched_entry_idx=matched))
            if matched is None:
                orphans.append(cite.surface)

        orphan_bib = [
            i for i in range(len(payload.bibliography_entries)) if i not in cited_idxs
        ]

        return A04Output(
            mappings=mappings,
            expanded_range_citations=expansions,
            orphan_in_text=orphans,
            orphan_bib_indices=orphan_bib,
        )

    def _verify(self, payload: A04Input, output: A04Output) -> None:
        if len(output.mappings) != len(payload.in_text_citations):
            self.fail("mappings length != in_text_citations length")
        for m in output.mappings:
            if m.matched_entry_idx is not None and not (
                0 <= m.matched_entry_idx < len(payload.bibliography_entries)
            ):
                self.fail(f"mapping idx {m.matched_entry_idx} out of bounds")
        for surface, expansion in output.expanded_range_citations:
            if not expansion or any(r < 1 for r in expansion):
                self.fail(f"range expansion {surface!r} invalid")
