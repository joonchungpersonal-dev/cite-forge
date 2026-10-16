"""Agent committees. See AGENTS.md for the full spec.

Importing this package triggers registration of every agent in every wave
into the global `AgentRegistry`. The pipeline orchestrator dispatches by
registry lookup; unregistered agents are unreachable.
"""

from cite_forge.agents.base import (
    Agent,
    AgentCallRecord,
    AgentRegistry,
    APIAgent,
    DeterministicAgent,
    LLMAgent,
    VerifierFailed,
)

# Side-effect imports: each wave module registers its agents on import.
from cite_forge.agents import (  # noqa: F401
    W0_decomposition,
    W1_discovery,
    W2_identity,
    W3_reconciliation,
    W4_integrity,
    W5_retrieval,
    W6_context,
    W6_5_gestalt,
    W7_blind,
    W8_manifest,
    verification,
)

__all__ = [
    "Agent",
    "APIAgent",
    "AgentCallRecord",
    "AgentRegistry",
    "DeterministicAgent",
    "LLMAgent",
    "VerifierFailed",
]
