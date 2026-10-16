"""LLM client with two backends.

Backend selection (priority):
  1. env CITE_FORGE_LLM_BACKEND = "claude_cli" or "anthropic"
  2. If unset: "anthropic" when ANTHROPIC_API_KEY is set, else "claude_cli"

Backends:
  - "anthropic": Anthropic SDK with structured tool-use. Requires API credits.
  - "claude_cli": subprocess `claude -p --json-schema ...` against the Claude Code
    CLI. Billed to the user's Claude Code subscription (NOT to API credits).
    Requires ANTHROPIC_API_KEY to be unset in the subprocess env so the CLI
    falls back to OAuth/keychain auth.

ARCHITECTURAL INVARIANTS:
  1. Every call returns a validated pydantic instance. Free-form text is not allowed.
  2. Token usage and cost are recorded for every call.
  3. JSON output is parsed into the requested pydantic class; ValidationError → hard fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Per-thread usage accumulator. call_structured() appends each call's usage here;
# the calling agent drains it (llm.collect_usage()) to stamp its trail record.
# LLM calls run on the main thread (parallelism in the pipeline is API-only), so
# thread-local attribution is correct.
_usage_state = threading.local()


def reset_usage() -> None:
    _usage_state.calls = []


def _accumulate_usage(model: str, tokens_in: int, tokens_out: int, cost: float) -> None:
    if not hasattr(_usage_state, "calls"):
        _usage_state.calls = []
    _usage_state.calls.append(
        {
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost,
        }
    )


def collect_usage() -> dict[str, Any]:
    """Drain and total the current thread's accumulated usage since last reset."""
    calls = getattr(_usage_state, "calls", [])
    _usage_state.calls = []
    return {
        "tokens_in": sum(c["tokens_in"] for c in calls),
        "tokens_out": sum(c["tokens_out"] for c in calls),
        "cost_usd": sum(c["cost_usd"] for c in calls),
        "n_calls": len(calls),
    }

# Pricing per 1M tokens (input / output) — claude pricing as of 2026-01
PRICING = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-7-1m": (30.0, 150.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

DEFAULT_MAX_TOKENS = 8192


class LLMResult:
    __slots__ = ("output", "raw_response", "tokens_in", "tokens_out", "cost_usd", "model")

    def __init__(
        self,
        output: BaseModel,
        raw_response: dict,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        model: str,
    ):
        self.output = output
        self.raw_response = raw_response
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_usd = cost_usd
        self.model = model


def _backend() -> str:
    """Backend is hard-locked to claude_cli per user policy.

    The anthropic SDK path is retained in code for reference but refused at
    runtime. To opt into the API path, set CITE_FORGE_LLM_BACKEND=anthropic
    AND CITE_FORGE_ALLOW_API=1 (both required).
    """
    explicit = os.environ.get("CITE_FORGE_LLM_BACKEND", "claude_cli")
    if explicit == "anthropic" and os.environ.get("CITE_FORGE_ALLOW_API") != "1":
        raise RuntimeError(
            "anthropic API backend is disabled per user policy "
            "(CITE_FORGE_ALLOW_API not set to '1'). "
            "Use claude_cli (default)."
        )
    return explicit


def _cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rates = PRICING.get(model, (3.0, 15.0))
    return (tokens_in * rates[0] + tokens_out * rates[1]) / 1_000_000


def _schema_for(cls: type[BaseModel]) -> dict[str, Any]:
    return cls.model_json_schema()


# ---------------------------------------------------------------------------
# claude_cli backend
# ---------------------------------------------------------------------------


def _claude_cli_path() -> str:
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("claude CLI not found on PATH")
    return path


def _call_claude_cli(
    *,
    model: str,
    system: str,
    user: str,
    output_cls: type[T],
    max_tokens: int,
    retries: int,
) -> LLMResult:
    schema = _schema_for(output_cls)
    schema_str = json.dumps(schema)
    claude = _claude_cli_path()

    env = os.environ.copy()
    # Force subscription auth: unset API key so CLI uses OAuth/keychain
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    cmd = [
        claude,
        "-p",
        user,
        "--system-prompt",
        system,
        "--json-schema",
        schema_str,
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "",
        "--allow-dangerously-skip-permissions",
    ]

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.returncode != 0:
                last_err = RuntimeError(
                    f"claude CLI exit {proc.returncode}: {proc.stderr[-500:]}"
                )
                if attempt == retries - 1:
                    raise last_err
                time.sleep(2**attempt)
                continue
            envelope = json.loads(proc.stdout)
            if envelope.get("is_error"):
                api_err = envelope.get("api_error_status")
                result_msg = envelope.get("result", "")
                last_err = RuntimeError(
                    f"claude CLI api_error_status={api_err}: {result_msg}"
                )
                if attempt == retries - 1:
                    raise last_err
                time.sleep(2**attempt)
                continue
            structured = envelope.get("structured_output")
            if structured is None:
                last_err = RuntimeError(
                    f"claude CLI returned no structured_output. "
                    f"result: {envelope.get('result', '')[:500]}"
                )
                if attempt == retries - 1:
                    raise last_err
                time.sleep(2**attempt)
                continue
            try:
                output = output_cls.model_validate(structured)
            except ValidationError as e:
                last_err = RuntimeError(
                    f"Output failed {output_cls.__name__} validation: {e}\n"
                    f"raw structured_output: {json.dumps(structured, indent=2)[:2000]}"
                )
                if attempt == retries - 1:
                    raise last_err from e
                time.sleep(2**attempt)
                continue
            usage = envelope.get("usage") or {}
            tokens_in = (usage.get("input_tokens") or 0) + (
                usage.get("cache_creation_input_tokens") or 0
            )
            tokens_out = usage.get("output_tokens") or 0
            cost = envelope.get("total_cost_usd") or _cost(model, tokens_in, tokens_out)
            return LLMResult(
                output=output,
                raw_response={
                    "session_id": envelope.get("session_id"),
                    "stop_reason": envelope.get("stop_reason"),
                    "model": model,
                    "backend": "claude_cli",
                },
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                model=model,
            )
        except subprocess.TimeoutExpired as e:
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"claude CLI failed after {retries} attempts: {last_err}")


# ---------------------------------------------------------------------------
# anthropic SDK backend
# ---------------------------------------------------------------------------


def _call_anthropic(
    *,
    model: str,
    system: str,
    user: str,
    output_cls: type[T],
    tool_name: str,
    tool_description: str,
    max_tokens: int,
    cache_system: bool,
    cache_user_prefix: str | None,
    extra_user_blocks: list[dict] | None,
    retries: int,
) -> LLMResult:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (required for anthropic backend)")
    cli = anthropic.Anthropic(api_key=api_key)
    schema = _schema_for(output_cls)

    user_blocks: list[dict] = []
    if cache_user_prefix is not None:
        user_blocks.append(
            {
                "type": "text",
                "text": cache_user_prefix,
                "cache_control": {"type": "ephemeral"},
            }
        )
    user_blocks.append({"type": "text", "text": user})
    if extra_user_blocks:
        user_blocks.extend(extra_user_blocks)

    system_blocks: list[dict] | str
    if cache_system:
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system_blocks = system

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = cli.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_blocks,
                tools=[
                    {
                        "name": tool_name,
                        "description": tool_description,
                        "input_schema": schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user_blocks}],
            )
            break
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
    else:
        raise RuntimeError(f"LLM call failed after {retries} attempts: {last_err}")

    tool_block = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool_name:
            tool_block = block
            break
    if tool_block is None:
        raise RuntimeError(
            f"Model did not call {tool_name!r}; got: {[b.type for b in resp.content]}"
        )

    try:
        output = output_cls.model_validate(tool_block.input)
    except ValidationError as e:
        raise RuntimeError(
            f"Output failed {output_cls.__name__} validation: {e}\n"
            f"raw input: {json.dumps(tool_block.input, indent=2)[:2000]}"
        ) from e

    tokens_in = resp.usage.input_tokens + getattr(
        resp.usage, "cache_creation_input_tokens", 0
    )
    tokens_out = resp.usage.output_tokens
    cost = _cost(model, tokens_in, tokens_out)

    return LLMResult(
        output=output,
        raw_response={
            "id": resp.id,
            "model": resp.model,
            "stop_reason": resp.stop_reason,
            "backend": "anthropic",
        },
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        model=model,
    )


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def call_structured(
    *,
    model: str,
    system: str,
    user: str,
    output_cls: type[T],
    tool_name: str = "emit_output",
    tool_description: str = "Emit the structured output.",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    cache_system: bool = True,
    cache_user_prefix: str | None = None,
    extra_user_blocks: list[dict] | None = None,
    retries: int = 3,
) -> LLMResult:
    """One LLM call returning a validated `output_cls` instance.

    Backend selection via env CITE_FORGE_LLM_BACKEND (anthropic | claude_cli).
    """
    backend = _backend()
    if backend == "claude_cli":
        result = _call_claude_cli(
            model=model,
            system=system,
            user=user,
            output_cls=output_cls,
            max_tokens=max_tokens,
            retries=retries,
        )
    else:
        result = _call_anthropic(
            model=model,
            system=system,
            user=user,
            output_cls=output_cls,
            tool_name=tool_name,
            tool_description=tool_description,
            max_tokens=max_tokens,
            cache_system=cache_system,
            cache_user_prefix=cache_user_prefix,
            extra_user_blocks=extra_user_blocks,
            retries=retries,
        )
    _accumulate_usage(result.model, result.tokens_in, result.tokens_out, result.cost_usd)
    return result
