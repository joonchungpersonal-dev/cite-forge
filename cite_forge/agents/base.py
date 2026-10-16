"""Agent base class with verifier-bypass-impossible pattern.

Architectural invariant: an agent CANNOT return output without its verifier
running first. This is enforced by structuring `Agent.run()` as @final
template-method that wraps `_execute()` + `_verify()`. Subclasses cannot
override `run()`.

The registry is the single source of truth for which agents exist. Agents
self-register on class definition via `__init_subclass__`. The pipeline
orchestrator dispatches by name; unregistered agents are unreachable.
"""

from __future__ import annotations

import abc
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, final

from pydantic import BaseModel

_persist_locks: dict[str, threading.Lock] = {}
_persist_locks_guard = threading.Lock()


def _lock_for_path(path: Path) -> threading.Lock:
    key = str(path)
    with _persist_locks_guard:
        lock = _persist_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _persist_locks[key] = lock
        return lock

I = TypeVar("I", bound=BaseModel)
O = TypeVar("O", bound=BaseModel)


@dataclass
class RunStats:
    """Process-wide tally of agent activity for a single pipeline run."""

    agent_calls: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


RUN_STATS = RunStats()


def reset_run_stats() -> None:
    """Reset the run tally. Call once at the start of a pipeline run."""
    global RUN_STATS
    RUN_STATS = RunStats()


class VerifierFailed(Exception):
    """Raised when an agent's output fails its hard-coded verifier."""

    def __init__(self, agent_name: str, reason: str, output: Any):
        super().__init__(f"{agent_name}: {reason}")
        self.agent_name = agent_name
        self.reason = reason
        self.output = output


class AgentRegistry:
    """Single global registry of all agents. Built at import time."""

    _by_name: ClassVar[dict[str, type[Agent]]] = {}
    _by_wave: ClassVar[dict[str, list[type[Agent]]]] = {}

    @classmethod
    def register(cls, agent_cls: type[Agent]) -> None:
        name = agent_cls.name
        if name in cls._by_name:
            existing = cls._by_name[name]
            if existing is not agent_cls:
                raise ValueError(
                    f"Agent name collision: {name} → "
                    f"{existing.__module__}.{existing.__qualname__} vs "
                    f"{agent_cls.__module__}.{agent_cls.__qualname__}"
                )
            return
        cls._by_name[name] = agent_cls
        cls._by_wave.setdefault(agent_cls.wave, []).append(agent_cls)

    @classmethod
    def by_name(cls, name: str) -> type[Agent]:
        if name not in cls._by_name:
            raise KeyError(f"Unknown agent: {name}")
        return cls._by_name[name]

    @classmethod
    def by_wave(cls, wave: str) -> list[type[Agent]]:
        return list(cls._by_wave.get(wave, []))

    @classmethod
    def all_names(cls) -> list[str]:
        return sorted(cls._by_name.keys())

    @classmethod
    def all_waves(cls) -> list[str]:
        return sorted(cls._by_wave.keys())


@dataclass
class AgentCallRecord:
    """One LLM/API call as persisted to the trail directory."""

    agent_name: str
    wave: str
    started_at: float
    finished_at: float | None = None
    model: str | None = None
    prompt_hash: str | None = None
    prompt: str | None = None
    response: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    verifier_passed: bool | None = None
    verifier_reason: str | None = None
    output_json: dict | None = None
    input_hash: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "wave": self.wave,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "input_hash": self.input_hash,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "verifier_passed": self.verifier_passed,
            "verifier_reason": self.verifier_reason,
            "output_json": self.output_json,
            "extra": self.extra,
        }


class Agent(Generic[I, O], abc.ABC):
    """Abstract base for all cite-forge agents.

    Subclasses MUST:
      - set class attrs `name` (unique) and `wave` ("W0".."W8" or "W6_5")
      - implement `_execute(payload) -> O`
      - implement `_verify(payload, output) -> None`  (raises on rejection)

    Subclasses MUST NOT override `run()`. It's @final.
    """

    name: ClassVar[str] = ""
    wave: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Abstract intermediate classes can opt out by leaving name="".
        if cls.name and not cls.__name__.startswith("_"):
            AgentRegistry.register(cls)

    def __init__(self, trail_dir: Path):
        if not self.name or not self.wave:
            raise TypeError(
                f"{self.__class__.__qualname__} must set `name` and `wave`."
            )
        self.trail_dir = trail_dir
        self.trail_dir.mkdir(parents=True, exist_ok=True)

    @final
    def run(self, payload: I) -> O:
        """Template method: execute, verify, persist. Cannot be overridden."""
        record = self._new_call()
        record.input_hash = self._hash(payload.model_dump_json())
        self._before_execute()
        try:
            output = self._execute(payload)
            self._after_execute(record)
            record.output_json = output.model_dump(mode="json")
            self._verify(payload, output)
            record.verifier_passed = True
        except VerifierFailed as e:
            record.verifier_passed = False
            record.verifier_reason = e.reason
            record.finished_at = time.time()
            self._persist_call(record)
            raise
        except Exception as e:
            record.verifier_passed = False
            record.verifier_reason = f"execute_raised: {type(e).__name__}: {e}"
            record.finished_at = time.time()
            self._persist_call(record)
            raise
        record.finished_at = time.time()
        self._persist_call(record)
        return output

    @abc.abstractmethod
    def _execute(self, payload: I) -> O:
        """Issue the LLM/API call. Subclass implements."""

    @abc.abstractmethod
    def _verify(self, payload: I, output: O) -> None:
        """Hard-coded verifier. Raise VerifierFailed on rejection."""

    def _before_execute(self) -> None:
        """Hook run immediately before `_execute`. Default no-op."""

    def _after_execute(self, record: AgentCallRecord) -> None:
        """Hook run immediately after `_execute`. Default no-op.

        Subclasses use this to stamp token/cost usage onto the call record.
        """

    def _new_call(self) -> AgentCallRecord:
        return AgentCallRecord(
            agent_name=self.name, wave=self.wave, started_at=time.time()
        )

    def _persist_call(self, record: AgentCallRecord) -> None:
        path = self.trail_dir / f"{self.name}.json"
        with _lock_for_path(path):
            if path.exists():
                try:
                    existing = json.loads(path.read_text())
                except json.JSONDecodeError:
                    existing = {"agent": self.name, "wave": self.wave, "calls": []}
                existing.setdefault("calls", []).append(record.to_dict())
            else:
                existing = {
                    "agent": self.name,
                    "wave": self.wave,
                    "calls": [record.to_dict()],
                }
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(existing, indent=2, default=str))
            tmp.replace(path)
        RUN_STATS.agent_calls += 1
        if record.cost_usd is not None or record.tokens_in is not None:
            RUN_STATS.llm_calls += 1
            RUN_STATS.tokens_in += record.tokens_in or 0
            RUN_STATS.tokens_out += record.tokens_out or 0
            RUN_STATS.cost_usd += record.cost_usd or 0.0

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def fail(self, reason: str, output: Any = None) -> None:
        """Convenience for verifiers."""
        raise VerifierFailed(self.name, reason, output)


class LLMAgent(Agent[I, O]):
    """Agent issuing a Claude (or other LLM) call with structured output."""

    model: ClassVar[str] = "claude-sonnet-4-6"

    def _new_call(self) -> AgentCallRecord:
        rec = super()._new_call()
        rec.model = self.model
        return rec

    def _before_execute(self) -> None:
        from cite_forge import llm

        llm.reset_usage()

    def _after_execute(self, record: AgentCallRecord) -> None:
        from cite_forge import llm

        usage = llm.collect_usage()
        if usage["n_calls"]:
            record.tokens_in = usage["tokens_in"]
            record.tokens_out = usage["tokens_out"]
            record.cost_usd = usage["cost_usd"]


class APIAgent(Agent[I, O]):
    """Agent issuing a non-LLM API call (CrossRef, PubMed, etc.).

    Lives in the agent system so its output is verified and trail-logged
    on the same plane as LLM agents.
    """


class DeterministicAgent(Agent[I, O]):
    """Pure-Python agent (no external call). Used for verifiers, cross-checks,
    and convergence rules. Still goes through the verifier pattern so the
    audit trail is uniform.
    """
