"""W3 — Reconciliation.

A11 Identity Convergence Critic (deterministic, fully implemented)
A12 Disambiguation Agent (LLM picks INDEX)
A13 Bibliographic Field Reconciliation Agent (deterministic field-by-field vote)
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import DeterministicAgent, LLMAgent
from cite_forge.llm import call_structured
from cite_forge.schemas import Bibliography


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A11: Identity Convergence Critic ----------


class W2AgentSummary(_Frozen):
    agent_name: str
    doi: str | None
    pmid: str | None
    pmcid: str | None
    openalex_id: str | None
    title: str | None


class A11Input(_Frozen):
    ref_num: int
    w2_summaries: list[W2AgentSummary]


class A11Output(_Frozen):
    consensus_doi: str | None
    consensus_pmid: str | None
    consensus_pmcid: str | None
    consensus_openalex_id: str | None
    converged: bool
    dissenting_agents: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


class IdentityConvergenceCritic(DeterministicAgent[A11Input, A11Output]):
    name = "A11_identity_convergence_critic"
    wave = "W3"

    @staticmethod
    def _consensus(values: list[str | None]) -> tuple[str | None, list[bool]]:
        nonnull = [v for v in values if v]
        if not nonnull:
            return None, [v is None for v in values]
        first = nonnull[0]
        agree_flags = [v is None or v == first for v in values]
        if not all(agree_flags):
            return None, agree_flags
        return first, agree_flags

    def _execute(self, payload: A11Input) -> A11Output:
        dois = [s.doi for s in payload.w2_summaries]
        pmids = [s.pmid for s in payload.w2_summaries]
        pmcids = [s.pmcid for s in payload.w2_summaries]
        oalex = [s.openalex_id for s in payload.w2_summaries]

        c_doi, doi_agree = self._consensus(dois)
        c_pmid, pmid_agree = self._consensus(pmids)
        c_pmcid, pmcid_agree = self._consensus(pmcids)
        c_oalex, oalex_agree = self._consensus(oalex)

        dissenting: set[str] = set()
        for i, s in enumerate(payload.w2_summaries):
            if not (doi_agree[i] and pmid_agree[i] and pmcid_agree[i] and oalex_agree[i]):
                dissenting.add(s.agent_name)

        converged = bool(c_doi or c_pmid or c_pmcid or c_oalex) and not dissenting
        confidence = (
            1.0
            if converged
            else max(0.0, 1.0 - len(dissenting) / max(1, len(payload.w2_summaries)))
        )

        return A11Output(
            consensus_doi=c_doi,
            consensus_pmid=c_pmid,
            consensus_pmcid=c_pmcid,
            consensus_openalex_id=c_oalex,
            converged=converged,
            dissenting_agents=sorted(dissenting),
            confidence=confidence,
        )

    def _verify(self, payload: A11Input, output: A11Output) -> None:
        if output.converged and output.dissenting_agents:
            self.fail("converged=True but dissenting_agents non-empty")
        if not output.converged and output.confidence > 0.95:
            self.fail("not converged but confidence > 0.95")


# ---------- A12: Disambiguation Agent ----------


class A12Input(_Frozen):
    ref_num: int
    bibliography: Bibliography
    candidates: list[W2AgentSummary]
    surrounding_claim: str


class A12Output(_Frozen):
    candidate_idx: int
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


_A12_SYSTEM = """You disambiguate which of several candidate identifiers matches the cited paper.

You are given:
- The bibliography entry (title, authors, year, journal).
- The surrounding claim from the manuscript.
- A list of candidate identifiers (DOI/PMID/OpenAlex) returned by different resolvers.

Pick the candidate index whose metadata best matches BOTH the bibliography entry AND the surrounding
claim. Be conservative: if multiple candidates could match, pick the one with the strongest
title + author + year agreement.

Return your confidence ∈ [0, 1]. If < 0.85, downstream code routes to a human — that's okay; do
not inflate confidence to avoid escalation."""


class DisambiguationAgent(LLMAgent[A12Input, A12Output]):
    name = "A12_disambiguation"
    wave = "W3"
    model = "claude-sonnet-4-6"

    def _execute(self, payload: A12Input) -> A12Output:
        if not payload.candidates:
            # Verifier will reject candidate_idx=0 with no candidates; surface explicitly
            self.fail("no candidates supplied")
        if len(payload.candidates) == 1:
            return A12Output(
                candidate_idx=0,
                confidence=0.9,
                rationale="single candidate; no disambiguation needed",
            )
        user = (
            f"Bibliography entry: {payload.bibliography.model_dump_json()}\n\n"
            f"Surrounding claim: {payload.surrounding_claim}\n\n"
            f"Candidates (0-indexed):\n"
            + "\n".join(
                f"  [{i}] {c.model_dump_json()}" for i, c in enumerate(payload.candidates)
            )
        )
        result = call_structured(
            model=self.model,
            system=_A12_SYSTEM,
            user=user,
            output_cls=A12Output,
            tool_name="pick_candidate",
            tool_description="Pick the best candidate index.",
            max_tokens=2000,
        )
        return result.output

    def _verify(self, payload: A12Input, output: A12Output) -> None:
        if not (0 <= output.candidate_idx < len(payload.candidates)):
            self.fail(f"candidate_idx {output.candidate_idx} out of bounds")
        if output.confidence < 0.85:
            self.fail(
                f"confidence {output.confidence:.2f} < 0.85 — must route to human queue"
            )


# ---------- A13: Bibliographic Field Reconciliation ----------


class FieldDiscrepancy(_Frozen):
    field: Literal[
        "title", "journal", "volume", "issue", "pages", "year", "publisher", "issn"
    ]
    values_per_source: dict[str, str | None]
    consensus_value: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class A13Input(_Frozen):
    ref_num: int
    bibliography_from_draft: Bibliography
    crossref_metadata: dict
    pubmed_metadata: dict
    openalex_metadata: dict


class A13Output(_Frozen):
    reconciled_bibliography: Bibliography
    discrepancies: list[FieldDiscrepancy]


def _norm(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    return " ".join(s.split()).lower()


def _vote(values: dict[str, str | None]) -> tuple[str | None, float]:
    """Return (consensus_normalized_value, confidence)."""
    nonnull = [_norm(v) for v in values.values() if _norm(v)]
    if not nonnull:
        return None, 0.0
    c = Counter(nonnull)
    most, count = c.most_common(1)[0]
    return most, count / len(values)


class BibliographicFieldReconciliationAgent(DeterministicAgent[A13Input, A13Output]):
    name = "A13_bib_field_reconciliation"
    wave = "W3"

    FIELDS: tuple[
        Literal["title", "journal", "volume", "issue", "pages", "year", "publisher", "issn"],
        ...,
    ] = ("title", "journal", "volume", "issue", "pages", "year", "publisher", "issn")

    def _execute(self, payload: A13Input) -> A13Output:
        bib = payload.bibliography_from_draft
        sources: dict[str, dict] = {
            "draft": bib.model_dump(),
            "crossref": payload.crossref_metadata,
            "pubmed": payload.pubmed_metadata,
            "openalex": payload.openalex_metadata,
        }
        discrepancies: list[FieldDiscrepancy] = []
        reconciled = bib.model_dump()
        for field in self.FIELDS:
            vals: dict[str, str | None] = {}
            for src, d in sources.items():
                raw = d.get(field)
                if isinstance(raw, (int, float)):
                    raw = str(raw)
                if isinstance(raw, list):
                    raw = raw[0] if raw else None
                vals[src] = raw if isinstance(raw, str) else None
            consensus, conf = _vote(vals)
            if not consensus:
                continue
            normalized_counts = Counter(_norm(v) for v in vals.values() if v)
            if len(normalized_counts) > 1:
                discrepancies.append(
                    FieldDiscrepancy(
                        field=field,
                        values_per_source=vals,
                        consensus_value=consensus
                        if normalized_counts[consensus] >= 2
                        else None,
                        confidence=conf,
                    )
                )
            # For year, prefer numeric agreement
            if field == "year":
                year_consensus = None
                for v in vals.values():
                    if v and v.isdigit():
                        year_consensus = int(v)
                        break
                if year_consensus is not None:
                    reconciled["year"] = year_consensus
        try:
            reconciled_bib = Bibliography(**reconciled)
        except Exception:
            reconciled_bib = bib
        return A13Output(
            reconciled_bibliography=reconciled_bib, discrepancies=discrepancies
        )

    def _verify(self, payload: A13Input, output: A13Output) -> None:
        for d in output.discrepancies:
            if d.consensus_value is not None:
                non_null = [_norm(v) for v in d.values_per_source.values() if _norm(v)]
                if non_null.count(_norm(d.consensus_value)) < 2:
                    self.fail(
                        f"field {d.field}: consensus {d.consensus_value!r} has <2 source backing"
                    )
