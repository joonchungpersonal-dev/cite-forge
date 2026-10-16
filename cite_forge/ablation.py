"""Ablation infrastructure — cache-backed, ~$0 cost.

Once cite-forge has run on a manuscript, every agent's output is persisted
to the per-citation trail directory. Ablation reads those trails and
replays the convergence rule with one (or many) agent outputs set to
abstain. No new LLM/API calls are issued.

Computes:
  - Single-agent LOO contribution
  - Pair contributions
  - Per-agent "binding fraction": how often this agent was the worst link
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cite_forge.pipeline import (
    ConvergenceInputs,
    is_converged,
)

# =====================================================================
#  TRAIL READER
# =====================================================================


def _load_agent_call(trail_dir: Path, agent_name: str) -> dict | None:
    path = trail_dir / f"{agent_name}.json"
    if not path.exists():
        return None
    blob = json.loads(path.read_text())
    calls = blob.get("calls", [])
    if not calls:
        return None
    return calls[-1].get("output_json")


def _load_citation_outputs(trail_dir: Path, agent_names: list[str]) -> dict[str, dict | None]:
    return {name: _load_agent_call(trail_dir, name) for name in agent_names}


# =====================================================================
#  CONVERGENCE-INPUT BUILDER
# =====================================================================


# Maps each `ConvergenceInputs` field to (agent_name, json_path) it should read from.
# Used both for normal build and for ablation (set field to abstain value when
# the corresponding agent is "dropped").
CONVERGENCE_FIELD_SOURCES: dict[str, tuple[str, str]] = {
    "w3_consensus_doi": ("A11_identity_convergence_critic", "consensus_doi"),
    "w3_consensus_pmid": ("A11_identity_convergence_critic", "consensus_pmid"),
    "w3_consensus_pmcid": ("A11_identity_convergence_critic", "consensus_pmcid"),
    "blind1_doi": ("A32_blind_rederivation_1", "doi"),
    "blind1_pmid": ("A32_blind_rederivation_1", "pmid"),
    "blind1_pmcid": ("A32_blind_rederivation_1", "pmcid"),
    "blind2_doi": ("A36_blind_rederivation_2", "doi"),
    "blind2_pmid": ("A36_blind_rederivation_2", "pmid"),
    "blind2_pmcid": ("A36_blind_rederivation_2", "pmcid"),
    "retraction_known": ("A14_retraction_erratum", "status"),  # known if not None
    "retraction_recheck_agrees": ("A42_retraction_recheck", "agrees_with_earlier"),
    "pdf_verified": ("A27_pdf_verification", "verified"),
    "pdf_verified_page2": ("A38_page2_pdf_verifier", "verified"),
    "context_captured": ("A28_citation_context", "sentence_verbatim"),
    "function_confidence": ("A29_citation_function", "confidence"),
    "quote_verbatim_matched": ("A31_quote_verification", "match"),
    "gestalt_calibrated": ("A51_paragraph_gestalt", "gestalt.verdict"),
    "attribution_inflation_score": ("A53_attribution_inflation", "inflation.inflation_score"),
    "causal_language_justified": ("A55_causal_language_audit", "audit.justified"),
    "all_numeric_claims_verified": ("A56_numeric_claim_verifier", "claims"),
    "no_direction_flips": ("A56_numeric_claim_verifier", "claims"),
    "narrative_sound": ("A57_narrative_coherence", "narrative.verdict"),
    "integrity_audit_passed": ("A50_manifest_integrity", "fails_threshold"),
}


def _get(d: dict | None, path: str) -> Any:
    if d is None:
        return None
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def build_convergence_inputs(
    citation_trail_dir: Path,
    drop_agents: frozenset[str] = frozenset(),
) -> ConvergenceInputs:
    """Build convergence inputs from a citation's trail directory.
    Any agent in `drop_agents` is treated as abstaining (its contributions
    become None / False, which the convergence rule treats as a failing clause).
    """
    outputs: dict[str, dict | None] = {}
    used_agents = {src for src, _ in CONVERGENCE_FIELD_SOURCES.values()}
    for agent_name in used_agents:
        if agent_name in drop_agents:
            outputs[agent_name] = None
            continue
        outputs[agent_name] = _load_agent_call(citation_trail_dir, agent_name)

    def field_value(field_name: str) -> Any:
        agent, json_path = CONVERGENCE_FIELD_SOURCES[field_name]
        return _get(outputs[agent], json_path)

    # Derived fields
    retraction_known = field_value("retraction_known") is not None
    quote_match = field_value("quote_verbatim_matched")
    quote_verbatim_matched = quote_match == "VERBATIM"

    gestalt_verdict = field_value("gestalt_calibrated")
    gestalt_calibrated = gestalt_verdict == "CALIBRATED"

    narrative_verdict = field_value("narrative_sound")
    narrative_sound = narrative_verdict == "SOUND"

    claims = field_value("all_numeric_claims_verified") or []
    all_numeric_verified = all(
        c.get("match") in ("VERBATIM", "ROUNDED", "CONVERTED")
        for c in claims
    )
    no_direction_flips = all(c.get("match") != "DIRECTION_FLIP" for c in claims)

    integrity_failed = field_value("integrity_audit_passed")
    integrity_audit_passed = integrity_failed is False  # A50 reports `fails_threshold`

    return ConvergenceInputs(
        w3_consensus_doi=field_value("w3_consensus_doi"),
        w3_consensus_pmid=field_value("w3_consensus_pmid"),
        w3_consensus_pmcid=field_value("w3_consensus_pmcid"),
        blind1_doi=field_value("blind1_doi"),
        blind1_pmid=field_value("blind1_pmid"),
        blind1_pmcid=field_value("blind1_pmcid"),
        blind2_doi=field_value("blind2_doi"),
        blind2_pmid=field_value("blind2_pmid"),
        blind2_pmcid=field_value("blind2_pmcid"),
        retraction_known=retraction_known,
        retraction_recheck_agrees=bool(field_value("retraction_recheck_agrees")),
        pdf_verified=bool(field_value("pdf_verified")),
        pdf_verified_page2=bool(field_value("pdf_verified_page2")),
        context_captured=field_value("context_captured") is not None,
        function_confidence=float(field_value("function_confidence") or 0.0),
        quote_required=False,  # caller can override
        quote_verbatim_matched=quote_verbatim_matched,
        gestalt_calibrated=gestalt_calibrated,
        attribution_inflation_score=float(
            field_value("attribution_inflation_score") or 0.0
        ),
        causal_language_justified=bool(field_value("causal_language_justified")),
        all_numeric_claims_verified=all_numeric_verified,
        no_direction_flips=no_direction_flips,
        narrative_sound=narrative_sound,
        integrity_audit_passed=integrity_audit_passed,
    )


# =====================================================================
#  ABLATION
# =====================================================================


@dataclass
class CitationConvergence:
    ref_num: int
    converged: bool
    failing_clauses: list[str]


@dataclass
class AblationResult:
    agents_dropped: tuple[str, ...]
    per_citation: list[CitationConvergence] = field(default_factory=list)
    n_converged: int = 0
    n_total: int = 0

    @property
    def convergence_rate(self) -> float:
        return self.n_converged / self.n_total if self.n_total else 0.0


def _run_one(run_dir: Path, drop_agents: frozenset[str]) -> AblationResult:
    citations_dir = run_dir / "citations"
    result = AblationResult(agents_dropped=tuple(sorted(drop_agents)))
    for cdir in sorted(citations_dir.iterdir()):
        if not cdir.is_dir():
            continue
        try:
            ref_num = int(cdir.name)
        except ValueError:
            continue
        ci = build_convergence_inputs(cdir, drop_agents=drop_agents)
        converged, failing = is_converged(ci)
        result.per_citation.append(
            CitationConvergence(
                ref_num=ref_num, converged=converged, failing_clauses=failing
            )
        )
        result.n_total += 1
        if converged:
            result.n_converged += 1
    return result


# =====================================================================
#  PUBLIC API
# =====================================================================


def leave_one_out(run_dir: Path, agent_names: list[str] | None = None) -> dict[str, AblationResult]:
    """Drop each agent in turn; return convergence-rate per drop.
    Cost: zero LLM/API calls; all from trail.
    """
    if agent_names is None:
        agent_names = sorted({src for src, _ in CONVERGENCE_FIELD_SOURCES.values()})
    baseline = _run_one(run_dir, frozenset())
    out: dict[str, AblationResult] = {"__baseline__": baseline}
    for name in agent_names:
        out[name] = _run_one(run_dir, frozenset({name}))
    return out


def pair_ablation(
    run_dir: Path, agent_names: list[str] | None = None
) -> dict[tuple[str, str], AblationResult]:
    """Drop every pair of agents. C(n,2) results, all cache-backed."""
    if agent_names is None:
        agent_names = sorted({src for src, _ in CONVERGENCE_FIELD_SOURCES.values()})
    out: dict[tuple[str, str], AblationResult] = {}
    for a, b in itertools.combinations(agent_names, 2):
        out[(a, b)] = _run_one(run_dir, frozenset({a, b}))
    return out


def binding_fraction(run_dir: Path) -> dict[str, float]:
    """For each agent, fraction of citations where it appears in the
    failing_clauses of the LOO drop. Approximation of "how often is this
    agent load-bearing?"
    """
    loo = leave_one_out(run_dir)
    n_total = loo["__baseline__"].n_total or 1
    out: dict[str, float] = {}
    for agent, result in loo.items():
        if agent == "__baseline__":
            continue
        load_bearing = sum(
            1
            for cc in result.per_citation
            if not cc.converged
            and any(agent.lower().replace("a", "") in fc for fc in cc.failing_clauses)
        )
        out[agent] = load_bearing / n_total
    return out
