"""W8 — Manifest Assembly.

A34 Manifest Schema Validator (Deterministic — NOT an LLM)
A35 Human Escalation Packetizer (LLM)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from cite_forge.agents.base import DeterministicAgent
from cite_forge.schemas import (
    SCHEMA_VERSION,
    AtomicUnit,
    Citation,
    ConvergenceVerdict,
    Escalation,
    Manifest,
    RetractionStatus,
    RunMetadata,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A34: Manifest Schema Validator ----------


class A34Input(_Frozen):
    metadata: RunMetadata
    units: list[AtomicUnit]
    citations: list[Citation]
    escalations: list[Escalation]
    allow_retracted: bool = False


class A34Output(_Frozen):
    manifest_json: str
    citations_admitted: int
    citations_excluded: int
    exclusion_reasons: list[str]


class ManifestSchemaValidator(DeterministicAgent[A34Input, A34Output]):
    """Refuse-to-emit invariants:
      - SCHEMA_VERSION mismatch → refuse
      - Any citation with RETRACTED status without `allow_retracted` → refuse
      - Any citation with overall_confidence < 0.85 → exclude from manifest
        (admitted to ESCALATIONS only)
      - Any required field missing → pydantic refuses construction
    """

    name = "A34_manifest_validator"
    wave = "W8"

    MIN_CONFIDENCE_FOR_MANIFEST = 0.85

    def _execute(self, payload: A34Input) -> A34Output:
        if payload.metadata.schema_version != SCHEMA_VERSION:
            self.fail(
                f"schema_version {payload.metadata.schema_version!r} != "
                f"current {SCHEMA_VERSION!r}"
            )

        admitted: list[Citation] = []
        excluded: list[str] = []
        for c in payload.citations:
            if c.integrity.retraction_status == RetractionStatus.RETRACTED and not payload.allow_retracted:
                excluded.append(
                    f"ref {c.ref_num}: RETRACTED (use --allow-retracted to include)"
                )
                continue
            if c.overall_confidence < self.MIN_CONFIDENCE_FOR_MANIFEST:
                excluded.append(
                    f"ref {c.ref_num}: confidence {c.overall_confidence:.2f} < "
                    f"{self.MIN_CONFIDENCE_FOR_MANIFEST}"
                )
                continue
            if c.convergence_verdict != ConvergenceVerdict.CONVERGED:
                excluded.append(
                    f"ref {c.ref_num}: verdict {c.convergence_verdict.value}"
                )
                continue
            admitted.append(c)

        try:
            manifest = Manifest(
                metadata=payload.metadata,
                units=payload.units,
                citations=admitted,
                escalations=payload.escalations,
            )
        except ValidationError as e:
            self.fail(f"manifest construction failed: {e}")

        return A34Output(
            manifest_json=manifest.model_dump_json(indent=2),
            citations_admitted=len(admitted),
            citations_excluded=len(payload.citations) - len(admitted),
            exclusion_reasons=excluded,
        )

    def _verify(self, payload: A34Input, output: A34Output) -> None:
        # Round-trip: emitted JSON must re-parse to an identical Manifest.
        try:
            roundtrip = Manifest.model_validate_json(output.manifest_json)
        except ValidationError as e:
            self.fail(f"emitted manifest fails round-trip parse: {e}")
        if roundtrip.metadata.schema_version != SCHEMA_VERSION:
            self.fail("round-trip schema_version drift")
        if output.citations_admitted + output.citations_excluded != len(payload.citations):
            self.fail("admitted + excluded != input count")


# ---------- A35: Human Escalation Packetizer ----------


class A35Input(_Frozen):
    escalations: list[Escalation]
    manuscript_path: str
    run_dir: str


class A35Output(_Frozen):
    escalations_md: str
    missing_md: str
    failures_json: str


class HumanEscalationPacketizer(DeterministicAgent[A35Input, A35Output]):
    """Pure-Python templating. Renders ESCALATIONS.md with the four required
    headings per packet so A49 validator passes.
    """

    name = "A35_human_escalation_packetizer"
    wave = "W8"

    def _execute(self, payload: A35Input) -> A35Output:
        lines: list[str] = [
            f"# Escalations for {payload.manuscript_path}",
            "",
            f"Trail: `{payload.run_dir}`",
            f"Total escalations: {len(payload.escalations)}",
            "",
        ]
        missing_lines: list[str] = [
            f"# Missing citations for {payload.manuscript_path}",
            "",
            "Citations where no PDF could be retrieved.",
            "",
        ]
        for i, esc in enumerate(payload.escalations, start=1):
            anchor = esc.human_packet_anchor
            lines.append(f"## Escalation {i} {anchor}".strip())
            lines.append(f"- Severity: **{esc.severity}**")
            if esc.ref_num is not None:
                lines.append(f"- ref_num: {esc.ref_num}")
            if esc.unit_id is not None:
                lines.append(f"- unit_id: {esc.unit_id}")
            lines.append("")
            lines.append("## Context")
            lines.append(esc.reason)
            lines.append("")
            lines.append("## Conflicting evidence")
            if esc.conflicting_agents:
                for ag in esc.conflicting_agents:
                    lines.append(f"- {ag}")
            else:
                lines.append("- (none recorded)")
            lines.append("")
            lines.append("## Question")
            lines.append(esc.question_for_human)
            lines.append("")
            lines.append("## Candidates")
            if esc.candidate_pdfs:
                for p in esc.candidate_pdfs:
                    lines.append(f"- `{p}`")
            else:
                lines.append("- (no candidate PDFs)")
            lines.append("")
            if "missing_pdf" in esc.reason.lower() or "no PDF" in esc.reason.lower():
                missing_lines.append(
                    f"- ref {esc.ref_num}: {esc.reason}"
                )

        return A35Output(
            escalations_md="\n".join(lines),
            missing_md="\n".join(missing_lines),
            failures_json="{}",
        )

    def _verify(self, payload: A35Input, output: A35Output) -> None:
        for esc in payload.escalations:
            if esc.human_packet_anchor not in output.escalations_md:
                self.fail(
                    f"escalation anchor {esc.human_packet_anchor!r} not in escalations_md"
                )
            if not esc.question_for_human.strip():
                self.fail("escalation has empty question_for_human")

    def _verify(self, payload: A35Input, output: A35Output) -> None:
        for esc in payload.escalations:
            if esc.human_packet_anchor not in output.escalations_md:
                self.fail(
                    f"escalation anchor {esc.human_packet_anchor!r} not in escalations_md"
                )
            if not esc.question_for_human.strip():
                self.fail("escalation has empty question_for_human")
