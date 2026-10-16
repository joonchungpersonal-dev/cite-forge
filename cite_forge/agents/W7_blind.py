"""W7 - Blind Verification.

A32 Blind Re-Derivation #1 - CrossRef bibliographic search, blind to the
    asserted DOI (APIAgent, deterministic search).
A33 Convergence Critic - DETERMINISTIC, not an LLM.
A36 Blind Re-Derivation #2 - PubMed title/author/year search, blind to the
    asserted DOI (APIAgent, deterministic search).
A37 Triangulation Critic - DETERMINISTIC; reports cross-source agreement.

The blind re-derivation agents independently re-resolve each citation from its
parsed bibliographic metadata (author / title / year), WITHOUT being told the
DOI the manuscript claims. Their findings are checked against the W3 gold-standard
multi-source consensus: a *conflict* (an independent search lands on a different
paper) is the real fraud/error signal; *corroboration* is independent
confirmation; *abstention* (no confident match) is inconclusive.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import APIAgent, DeterministicAgent
from cite_forge.schemas import Bibliography, ConvergenceVerdict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A32 / A36: Blind Re-Derivation ----------


class BlindReDerivationInput(_Frozen):
    ref_num: int
    bibliography: Bibliography


class BlindReDerivationOutput(_Frozen):
    doi: str | None
    pmid: str | None
    pmcid: str | None
    openalex_id: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    method: str = ""


class _BlindReDerivationBase(APIAgent[BlindReDerivationInput, BlindReDerivationOutput]):
    """Independent identity re-derivation, blind to the citation's asserted DOI.

    Re-resolves the paper purely from its parsed bibliographic metadata via a
    SINGLE source, never from the DOI the manuscript claims. The found identifier
    is later checked against the W3 multi-source consensus by A33.
    """

    def _verify(
        self,
        payload: BlindReDerivationInput,
        output: BlindReDerivationOutput,
    ) -> None:
        if output.confidence > 0.0:
            if not (output.doi or output.pmid or output.pmcid or output.openalex_id):
                self.fail("confidence > 0 but no identifier returned")


class BlindReDerivation1(_BlindReDerivationBase):
    """Blind re-derivation via CrossRef bibliographic search (DOI stripped)."""

    name = "A32_blind_rederivation_1"
    wave = "W7"

    def _execute(self, payload: BlindReDerivationInput) -> BlindReDerivationOutput:
        from cite_forge.agents.W2_identity import DOIResolutionAgent, IdentityQueryInput

        query = IdentityQueryInput(
            ref_num=payload.ref_num,
            bibliography=payload.bibliography,
            declared_doi=None,
        )
        out = DOIResolutionAgent(trail_dir=self.trail_dir)._execute(query)
        if out.selected_idx is not None:
            cand = out.candidates[out.selected_idx]
            return BlindReDerivationOutput(
                doi=cand.doi,
                pmid=None,
                pmcid=None,
                openalex_id=None,
                confidence=0.9,
                method="crossref_blind",
            )
        return BlindReDerivationOutput(
            doi=None,
            pmid=None,
            pmcid=None,
            openalex_id=None,
            confidence=0.0,
            method="crossref_blind",
        )


class BlindReDerivation2(_BlindReDerivationBase):
    """Blind re-derivation via PubMed title/author/year search (DOI stripped)."""

    name = "A36_blind_rederivation_2"
    wave = "W7"

    def _execute(self, payload: BlindReDerivationInput) -> BlindReDerivationOutput:
        from cite_forge.agents.W2_identity import IdentityQueryInput, PubMedAgent

        abstain = BlindReDerivationOutput(
            doi=None,
            pmid=None,
            pmcid=None,
            openalex_id=None,
            confidence=0.0,
            method="pubmed_blind",
        )

        query = IdentityQueryInput(
            ref_num=payload.ref_num,
            bibliography=payload.bibliography,
            declared_doi=None,
        )
        out = PubMedAgent(trail_dir=self.trail_dir)._execute(query)
        if not out.pmid:
            return abstain

        # PubMed's title search (esp. its title-only relax fallback) can surface
        # a wrong, often more recent, paper. Gate the hit by publication year so a
        # blind result is only reported when it confidently matches the citation;
        # otherwise abstain rather than manufacture a false identity conflict.
        if not self._year_matches(out.pmid, payload.bibliography.year):
            return abstain

        return BlindReDerivationOutput(
            doi=None,
            pmid=out.pmid,
            pmcid=out.pmcid,
            openalex_id=None,
            confidence=0.9,
            method="pubmed_blind",
        )

    @staticmethod
    def _year_matches(pmid: str, expected_year: int) -> bool:
        import re

        from cite_forge.http import cached_get_json

        status, data = cached_get_json(
            "pubmed",
            "esummary",
            f"id:{pmid}",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={"db": "pubmed", "id": pmid, "retmode": "json"},
        )
        if status != 200 or not data:
            return False
        rec = (data.get("result") or {}).get(str(pmid)) or {}
        pubdate = rec.get("pubdate") or rec.get("epubdate") or ""
        m = re.search(r"\d{4}", pubdate)
        if not m:
            return False
        return abs(int(m.group(0)) - expected_year) <= 1


# ---------- A33: Convergence Critic ----------


class A33Input(_Frozen):
    ref_num: int
    w3_consensus: BlindReDerivationOutput  # treating consensus as same shape
    a32_output: BlindReDerivationOutput
    a36_output: BlindReDerivationOutput
    a11_converged: bool
    w5_pdf_verified: bool
    w5_pdf_verified_page2: bool
    w4_retraction_known: bool
    w4_retraction_recheck_agrees: bool
    w6_context_captured: bool
    w6_function_confidence: float = Field(ge=0.0, le=1.0)
    w6_quote_required: bool
    w6_quote_verbatim_matched: bool
    gestalt_calibrated: bool
    attribution_inflation_score: float = Field(ge=0.0, le=1.0)
    causal_language_justified: bool
    all_numeric_claims_verified: bool


class A33Output(_Frozen):
    verdict: ConvergenceVerdict
    failing_clauses: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


class ConvergenceCritic(DeterministicAgent[A33Input, A33Output]):
    """The hard-coded master convergence rule. NOT an LLM.

    Identity convergence rests on the W3 gold-standard multi-source consensus
    (A11) plus independent blind re-derivation. PDF retrieval is tracked but
    decoupled: a missing OA PDF does NOT diverge identity - it is surfaced as a
    pending-retrieval escalation by the pipeline. Only a genuine cross-source
    identity conflict (or a retraction-recheck disagreement) yields DIVERGED.
    """

    name = "A33_convergence_critic"
    wave = "W7"

    @staticmethod
    def _ids_agree(a: BlindReDerivationOutput, b: BlindReDerivationOutput) -> bool:
        pairs = [
            (a.doi, b.doi),
            (a.pmid, b.pmid),
            (a.pmcid, b.pmcid),
            (a.openalex_id, b.openalex_id),
        ]
        for x, y in pairs:
            if x is not None and y is not None and x.lower() != y.lower():
                return False
        return any(
            x is not None and y is not None and x.lower() == y.lower()
            for x, y in pairs
        )

    @staticmethod
    def _agreement(
        consensus: BlindReDerivationOutput, blind: BlindReDerivationOutput
    ) -> str:
        """Compare a blind re-derivation against the W3 consensus anchor.

        Returns 'CORROBORATE' (shares >=1 identifier, none conflict),
        'CONFLICT' (a shared identifier differs), or 'ABSTAIN' (no overlap).
        """
        shared: list[tuple[str, str]] = []
        for field_name in ("doi", "pmid", "pmcid", "openalex_id"):
            x = getattr(consensus, field_name)
            y = getattr(blind, field_name)
            if x and y:
                shared.append((x.lower(), y.lower()))
        if not shared:
            return "ABSTAIN"
        return "CORROBORATE" if all(x == y for x, y in shared) else "CONFLICT"

    def _execute(self, payload: A33Input) -> A33Output:
        ag1 = self._agreement(payload.w3_consensus, payload.a32_output)
        ag2 = self._agreement(payload.w3_consensus, payload.a36_output)
        identity_corroborated = "CORROBORATE" in (ag1, ag2)

        # --- hard identity signals -> DIVERGED ---
        hard: list[str] = []
        if not payload.a11_converged:
            hard.append("a11_multi_source_not_converged")
        if ag1 == "CONFLICT":
            hard.append("blind1_conflicts_consensus")
        if ag2 == "CONFLICT":
            hard.append("blind2_conflicts_consensus")
        if not payload.w4_retraction_recheck_agrees:
            hard.append("retraction_recheck_disagrees")

        # --- soft claim-fidelity / corroboration signals -> UNCERTAIN ---
        soft: list[str] = []
        if ag1 == "ABSTAIN" and ag2 == "ABSTAIN":
            soft.append("blind_rederivation_inconclusive")
        if not payload.w4_retraction_known:
            soft.append("retraction_status_unknown")
        if not payload.w6_context_captured:
            soft.append("context_not_captured")
        if payload.w6_function_confidence < 0.7:
            soft.append(
                f"function_confidence={payload.w6_function_confidence:.2f} < 0.7"
            )
        if payload.w6_quote_required and not payload.w6_quote_verbatim_matched:
            soft.append("quote_required_but_not_verbatim_matched")
        if not payload.gestalt_calibrated:
            soft.append("gestalt_not_calibrated")
        if payload.attribution_inflation_score > 0.2:
            soft.append(
                f"attribution_inflation_score={payload.attribution_inflation_score:.2f} > 0.2"
            )
        if not payload.causal_language_justified:
            soft.append("causal_language_not_justified")
        if not payload.all_numeric_claims_verified:
            soft.append("numeric_claims_unverified")

        # --- PDF retrieval: non-blocking, surfaced for MISSING.md ---
        pdf_pending: list[str] = []
        if not payload.w5_pdf_verified:
            pdf_pending.append("missing_pdf")
        elif not payload.w5_pdf_verified_page2:
            pdf_pending.append("pdf_page2_unverified")

        if hard:
            return A33Output(
                verdict=ConvergenceVerdict.DIVERGED,
                failing_clauses=hard + soft + pdf_pending,
                confidence=0.0,
            )

        if not soft and identity_corroborated:
            # Identity multi-source converged AND independently corroborated.
            # PDF state is reported via the pipeline's retrieval escalation, so
            # failing_clauses stays empty to satisfy the CONVERGED invariant.
            return A33Output(
                verdict=ConvergenceVerdict.CONVERGED,
                failing_clauses=[],
                confidence=1.0 if payload.w5_pdf_verified else 0.9,
            )

        return A33Output(
            verdict=ConvergenceVerdict.UNCERTAIN,
            failing_clauses=soft + pdf_pending,
            confidence=max(0.0, 0.8 - 0.05 * len(soft)),
        )

    def _verify(self, payload: A33Input, output: A33Output) -> None:
        if output.verdict == ConvergenceVerdict.CONVERGED and output.failing_clauses:
            self.fail("CONVERGED with non-empty failing_clauses")
        if (
            output.verdict != ConvergenceVerdict.CONVERGED
            and not output.failing_clauses
        ):
            self.fail(f"{output.verdict} with empty failing_clauses")


# ---------- A37: Triangulation Critic ----------


class A37Input(_Frozen):
    ref_num: int
    w3_consensus: BlindReDerivationOutput
    a32_output: BlindReDerivationOutput
    a36_output: BlindReDerivationOutput


class A37Output(_Frozen):
    triangulated: bool
    matching_pairs: int  # 0..3 of (w3,a32), (w3,a36), (a32,a36)


class TriangulationCritic(DeterministicAgent[A37Input, A37Output]):
    """Reports cross-source agreement among W3 consensus and the two blind
    re-derivations. Informational - the binding rule lives in A33."""

    name = "A37_triangulation_critic"
    wave = "W7"

    def _execute(self, payload: A37Input) -> A37Output:
        ids_agree = ConvergenceCritic._ids_agree
        pairs = [
            ids_agree(payload.w3_consensus, payload.a32_output),
            ids_agree(payload.w3_consensus, payload.a36_output),
            ids_agree(payload.a32_output, payload.a36_output),
        ]
        return A37Output(triangulated=all(pairs), matching_pairs=sum(pairs))

    def _verify(self, payload: A37Input, output: A37Output) -> None:
        if output.triangulated and output.matching_pairs != 3:
            self.fail(
                f"triangulated=True but matching_pairs={output.matching_pairs} != 3"
            )
