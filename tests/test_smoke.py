"""Smoke tests — verify the scaffold is structurally sound.

These tests do NOT exercise LLM/API calls (those raise NotImplementedError
by design until wired). They exercise:
  - Schema construction + validation
  - Agent registry (all 58 agents register; no name collisions)
  - Verifier enforcement (Agent.run() cannot bypass _verify)
  - Convergence rule (hard-coded; pure Python)
  - Ablation module imports
  - Deterministic agents that have real _execute logic
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


# ---------- Registry ----------


def test_registry_contains_58_agents():
    from cite_forge.agents.base import AgentRegistry
    from cite_forge import agents  # noqa: F401  -- triggers registration

    assert len(AgentRegistry.all_names()) == 58


def test_registry_waves_present():
    from cite_forge.agents.base import AgentRegistry
    from cite_forge import agents  # noqa: F401

    expected_waves = {"W0", "W1", "W2", "W3", "W4", "W5", "W6", "W6_5", "W7", "W8"}
    assert set(AgentRegistry.all_waves()) == expected_waves


def test_registry_no_name_collisions():
    from cite_forge.agents.base import AgentRegistry
    from cite_forge import agents  # noqa: F401

    names = AgentRegistry.all_names()
    assert len(names) == len(set(names))


def test_registry_critical_agents_registered():
    """Convergence depends on these specific names — guard against rename drift."""
    from cite_forge.agents.base import AgentRegistry
    from cite_forge import agents  # noqa: F401

    must_exist = [
        "A01_atomic_decomposition",
        "A11_identity_convergence_critic",
        "A27_pdf_verification",
        "A32_blind_rederivation_1",
        "A33_convergence_critic",
        "A36_blind_rederivation_2",
        "A37_triangulation_critic",
        "A50_manifest_integrity",
        "A51_paragraph_gestalt",
        "A53_attribution_inflation",
        "A55_causal_language_audit",
        "A56_numeric_claim_verifier",
        "A57_narrative_coherence",
    ]
    for name in must_exist:
        AgentRegistry.by_name(name)  # raises KeyError if missing


# ---------- Schema invariants ----------


def test_schema_version_is_pinned():
    from cite_forge.schemas import SCHEMA_VERSION

    assert SCHEMA_VERSION == "1.0.0"


def test_schemas_are_frozen():
    """Frozen pydantic — any mutation must raise."""
    from cite_forge.schemas import CitationIdentity

    ident = CitationIdentity(consensus_confidence=0.9)
    with pytest.raises((TypeError, ValueError)):
        ident.consensus_confidence = 0.1  # type: ignore[misc]


def test_schemas_forbid_extra_fields():
    from pydantic import ValidationError
    from cite_forge.schemas import CitationIdentity

    with pytest.raises(ValidationError):
        CitationIdentity(consensus_confidence=0.9, made_up_field="x")  # type: ignore[call-arg]


def test_confidence_bounds_enforced():
    from pydantic import ValidationError
    from cite_forge.schemas import CitationIdentity

    with pytest.raises(ValidationError):
        CitationIdentity(consensus_confidence=1.5)
    with pytest.raises(ValidationError):
        CitationIdentity(consensus_confidence=-0.1)


# ---------- Convergence rule (hard-coded) ----------


def _all_good_inputs():
    from cite_forge.pipeline import ConvergenceInputs

    return ConvergenceInputs(
        w3_consensus_doi="10.1093/sleep/zsad048",
        w3_consensus_pmid="37000000",
        w3_consensus_pmcid="PMC10848217",
        blind1_doi="10.1093/sleep/zsad048",
        blind1_pmid="37000000",
        blind1_pmcid="PMC10848217",
        blind2_doi="10.1093/sleep/zsad048",
        blind2_pmid="37000000",
        blind2_pmcid="PMC10848217",
        retraction_known=True,
        retraction_recheck_agrees=True,
        pdf_verified=True,
        pdf_verified_page2=True,
        context_captured=True,
        function_confidence=0.92,
        quote_required=False,
        quote_verbatim_matched=True,
        gestalt_calibrated=True,
        attribution_inflation_score=0.05,
        causal_language_justified=True,
        all_numeric_claims_verified=True,
        no_direction_flips=True,
        narrative_sound=True,
        integrity_audit_passed=True,
    )


def test_convergence_all_good():
    from cite_forge.pipeline import is_converged

    converged, failing = is_converged(_all_good_inputs())
    assert converged is True
    assert failing == []


def test_convergence_attribution_inflation_blocks():
    from cite_forge.pipeline import ConvergenceInputs, is_converged

    base = _all_good_inputs()
    bad = ConvergenceInputs(**{**base.__dict__, "attribution_inflation_score": 0.45})
    converged, failing = is_converged(bad)
    assert converged is False
    assert any("attribution_inflation_score" in c for c in failing)


def test_convergence_pdf_unverified_blocks():
    from cite_forge.pipeline import ConvergenceInputs, is_converged

    base = _all_good_inputs()
    bad = ConvergenceInputs(**{**base.__dict__, "pdf_verified": False})
    converged, failing = is_converged(bad)
    assert converged is False
    assert "pdf_verification_failed" in failing


def test_convergence_blind_disagreement_blocks():
    from cite_forge.pipeline import ConvergenceInputs, is_converged

    base = _all_good_inputs()
    bad = ConvergenceInputs(**{**base.__dict__, "blind2_doi": "10.1093/sleep/wrong"})
    converged, failing = is_converged(bad)
    assert converged is False
    assert "w3_vs_blind2_id_disagreement" in failing
    assert "blind1_vs_blind2_id_disagreement" in failing


def test_convergence_function_confidence_threshold():
    from cite_forge.pipeline import ConvergenceInputs, is_converged

    base = _all_good_inputs()
    bad = ConvergenceInputs(**{**base.__dict__, "function_confidence": 0.69})
    converged, failing = is_converged(bad)
    assert converged is False
    assert any("function_confidence" in c for c in failing)


def test_convergence_direction_flip_blocks():
    from cite_forge.pipeline import ConvergenceInputs, is_converged

    base = _all_good_inputs()
    bad = ConvergenceInputs(**{**base.__dict__, "no_direction_flips": False})
    converged, failing = is_converged(bad)
    assert converged is False
    assert "numeric_direction_flip_detected" in failing


def test_worst_link_confidence():
    from cite_forge.pipeline import worst_link_confidence

    assert worst_link_confidence({"A01": 0.95, "A05": 0.88, "A27": 0.93}) == 0.88
    assert worst_link_confidence({}) == 0.0


# ---------- Verifier enforcement ----------


def test_agent_cannot_skip_verifier(tmp_path):
    """If _verify raises, run() raises — output never returned."""
    from pydantic import BaseModel, ConfigDict

    from cite_forge.agents.base import DeterministicAgent, VerifierFailed

    class _In(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        value: int

    class _Out(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        value: int

    class _ThrowingAgent(DeterministicAgent[_In, _Out]):
        name = "test_throwing_agent_xyz"
        wave = "W0"

        def _execute(self, payload: _In) -> _Out:
            return _Out(value=payload.value + 1)

        def _verify(self, payload: _In, output: _Out) -> None:
            if output.value != payload.value:
                self.fail(f"value flipped: {payload.value} -> {output.value}")

    agent = _ThrowingAgent(trail_dir=tmp_path)
    with pytest.raises(VerifierFailed):
        agent.run(_In(value=1))


def test_agent_persists_trail(tmp_path):
    from pydantic import BaseModel, ConfigDict

    from cite_forge.agents.base import DeterministicAgent

    class _In(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        value: int

    class _Out(BaseModel):
        model_config = ConfigDict(frozen=True, extra="forbid")
        value: int

    class _GoodAgent(DeterministicAgent[_In, _Out]):
        name = "test_good_agent_xyz"
        wave = "W0"

        def _execute(self, payload: _In) -> _Out:
            return _Out(value=payload.value)

        def _verify(self, payload: _In, output: _Out) -> None:
            if output.value != payload.value:
                self.fail("mismatch")

    agent = _GoodAgent(trail_dir=tmp_path)
    out = agent.run(_In(value=42))
    assert out.value == 42
    trail_file = tmp_path / "test_good_agent_xyz.json"
    assert trail_file.exists()


# ---------- Deterministic agents with real logic ----------


def test_a18_citation_cartel_detector():
    from cite_forge.agents.W4_integrity import A18Input, CitationCartelDetector
    from cite_forge.schemas import Author, Bibliography
    from pathlib import Path

    def entry(family: str, journal: str = "JournalX", year: int = 2020) -> Bibliography:
        return Bibliography(
            authors=[Author(name_verbatim=family, family=family, position=1)],
            title="T",
            journal=journal,
            year=year,
        )

    # 4 of 10 entries from same family → > 25% threshold
    entries = (
        [entry("Chung") for _ in range(4)]
        + [entry("Buysse") for _ in range(3)]
        + [entry("Wallace") for _ in range(3)]
    )

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        agent = CitationCartelDetector(trail_dir=Path(td))
        out = agent.run(A18Input(bibliography_entries=entries))
        author_flags = [f for f in out.flags if f.entity_type == "author"]
        assert any("Chung" in f.entity_value for f in author_flags)
        # Journal also concentrated > 25% (all same)
        journal_flags = [f for f in out.flags if f.entity_type == "journal"]
        assert any("JournalX" in f.entity_value for f in journal_flags)


def test_a44_topic_drift_detector():
    from cite_forge.agents.verification import A44Input, TopicDriftDetector
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        agent = TopicDriftDetector(trail_dir=Path(td))
        # Identical vectors → cosine 1.0, drift 0
        out = agent.run(
            A44Input(
                ref_num=1,
                source_topic_vector=[1.0, 0.0, 0.0],
                manuscript_topic_vector=[1.0, 0.0, 0.0],
            )
        )
        assert out.cosine == pytest.approx(1.0)
        assert out.drift_flagged is False

        # Orthogonal → cosine 0, drift 1
        out = agent.run(
            A44Input(
                ref_num=1,
                source_topic_vector=[1.0, 0.0, 0.0],
                manuscript_topic_vector=[0.0, 1.0, 0.0],
            )
        )
        assert out.cosine == pytest.approx(0.0)
        assert out.drift_flagged is True


# ---------- Ablation ----------


def test_ablation_module_imports():
    from cite_forge.ablation import (
        CONVERGENCE_FIELD_SOURCES,
    )

    # Convergence pulls from 16 distinct upstream agents
    used_agents = {src for src, _ in CONVERGENCE_FIELD_SOURCES.values()}
    assert len(used_agents) >= 10


# ---------- A33 ConvergenceCritic agent ----------


def test_a33_convergence_critic_converged(tmp_path):
    from cite_forge.agents.W7_blind import (
        A33Input,
        BlindReDerivationOutput,
        ConvergenceCritic,
    )

    common = BlindReDerivationOutput(
        doi="10.1/x",
        pmid="1",
        pmcid="PMC1",
        openalex_id="W1",
        confidence=1.0,
    )
    agent = ConvergenceCritic(trail_dir=tmp_path)
    out = agent.run(
        A33Input(
            ref_num=1,
            w3_consensus=common,
            a32_output=common,
            a36_output=common,
            a11_converged=True,
            w5_pdf_verified=True,
            w5_pdf_verified_page2=True,
            w4_retraction_known=True,
            w4_retraction_recheck_agrees=True,
            w6_context_captured=True,
            w6_function_confidence=0.9,
            w6_quote_required=False,
            w6_quote_verbatim_matched=True,
            gestalt_calibrated=True,
            attribution_inflation_score=0.05,
            causal_language_justified=True,
            all_numeric_claims_verified=True,
        )
    )
    from cite_forge.schemas import ConvergenceVerdict

    assert out.verdict == ConvergenceVerdict.CONVERGED
    assert out.failing_clauses == []


def _a33_inputs(**overrides):
    """Build an all-good A33Input, overriding individual fields per test."""
    from cite_forge.agents.W7_blind import A33Input, BlindReDerivationOutput

    consensus = BlindReDerivationOutput(
        doi="10.1/x", pmid="1", pmcid="PMC1", openalex_id="W1", confidence=1.0
    )
    base = dict(
        ref_num=1,
        w3_consensus=consensus,
        a32_output=BlindReDerivationOutput(
            doi="10.1/x", pmid=None, pmcid=None, openalex_id=None, confidence=0.9
        ),
        a36_output=BlindReDerivationOutput(
            doi=None, pmid="1", pmcid=None, openalex_id=None, confidence=0.9
        ),
        a11_converged=True,
        w5_pdf_verified=True,
        w5_pdf_verified_page2=True,
        w4_retraction_known=True,
        w4_retraction_recheck_agrees=True,
        w6_context_captured=True,
        w6_function_confidence=0.9,
        w6_quote_required=False,
        w6_quote_verbatim_matched=True,
        gestalt_calibrated=True,
        attribution_inflation_score=0.05,
        causal_language_justified=True,
        all_numeric_claims_verified=True,
    )
    base.update(overrides)
    return A33Input(**base)


def test_a33_partial_blind_ids_corroborate(tmp_path):
    # CrossRef blind yields only a DOI, PubMed blind only a PMID; each
    # corroborates the consensus on its own field -> CONVERGED.
    from cite_forge.agents.W7_blind import ConvergenceCritic
    from cite_forge.schemas import ConvergenceVerdict

    out = ConvergenceCritic(trail_dir=tmp_path).run(_a33_inputs())
    assert out.verdict == ConvergenceVerdict.CONVERGED


def test_a33_blind_conflict_diverges(tmp_path):
    # An independent re-derivation lands on a DIFFERENT DOI -> real conflict.
    from cite_forge.agents.W7_blind import BlindReDerivationOutput, ConvergenceCritic
    from cite_forge.schemas import ConvergenceVerdict

    out = ConvergenceCritic(trail_dir=tmp_path).run(
        _a33_inputs(
            a32_output=BlindReDerivationOutput(
                doi="10.9/WRONG",
                pmid=None,
                pmcid=None,
                openalex_id=None,
                confidence=0.9,
            )
        )
    )
    assert out.verdict == ConvergenceVerdict.DIVERGED
    assert "blind1_conflicts_consensus" in out.failing_clauses


def test_a33_missing_pdf_still_converges(tmp_path):
    # PDF decoupled from identity: a paywalled (unretrieved) PDF must NOT block
    # a citation whose identity is multi-source converged and corroborated.
    from cite_forge.agents.W7_blind import ConvergenceCritic
    from cite_forge.schemas import ConvergenceVerdict

    out = ConvergenceCritic(trail_dir=tmp_path).run(
        _a33_inputs(w5_pdf_verified=False, w5_pdf_verified_page2=False)
    )
    assert out.verdict == ConvergenceVerdict.CONVERGED
    assert out.confidence >= 0.85  # admitted to manifest; PDF routed to MISSING.md


def test_a33_blind_abstain_is_uncertain(tmp_path):
    # Both blind searches found nothing (e.g. a no-DOI book): no conflict, but
    # no independent corroboration either -> UNCERTAIN, escalate for review.
    from cite_forge.agents.W7_blind import BlindReDerivationOutput, ConvergenceCritic
    from cite_forge.schemas import ConvergenceVerdict

    empty = BlindReDerivationOutput(
        doi=None, pmid=None, pmcid=None, openalex_id=None, confidence=0.0
    )
    out = ConvergenceCritic(trail_dir=tmp_path).run(
        _a33_inputs(a32_output=empty, a36_output=empty)
    )
    assert out.verdict == ConvergenceVerdict.UNCERTAIN
    assert "blind_rederivation_inconclusive" in out.failing_clauses


def test_bracket_ref_nums_extraction():
    from cite_forge.pipeline import _bracket_ref_nums

    assert _bracket_ref_nums("[1]") == {1}
    assert _bracket_ref_nums("Meng et al. [1]") == {1}  # the linkage-bug case
    assert _bracket_ref_nums("[2, p. 12]") == {2}
    assert _bracket_ref_nums("[8,9,10]") == {8, 9, 10}
    assert _bracket_ref_nums("[5-7]") == {5, 6, 7}
    assert _bracket_ref_nums("[5–7]") == {5, 6, 7}
    assert _bracket_ref_nums("(Smith et al., 2020)") == set()
