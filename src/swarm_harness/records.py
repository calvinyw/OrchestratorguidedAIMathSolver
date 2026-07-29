from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Problem:
    """A single math problem to solve."""

    id: str
    statement: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    n_turns: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_output_tokens += other.reasoning_output_tokens
        self.n_turns += other.n_turns
        return self

    def to_json(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "n_turns": self.n_turns,
        }


@dataclass
class AgentCallResult:
    """Result of one backend worker invocation."""

    role: str
    call_id: str
    workspace: Path
    command: list[str]
    prompt: str
    content: str
    parsed: dict[str, Any] | None
    usage: TokenUsage = field(default_factory=TokenUsage)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    model_requested: str | None = None
    model_used: str | None = None
    model_fallback_used: bool = False
    model_fallback_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error is None

    def to_trace_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "call_id": self.call_id,
            "workspace": str(self.workspace),
            "command": self.command,
            "returncode": self.returncode,
            "ok": self.ok,
            "error": self.error,
            "usage": self.usage.to_json(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "model_requested": self.model_requested,
            "model_used": self.model_used,
            "model_fallback_used": self.model_fallback_used,
            "model_fallback_reason": self.model_fallback_reason,
        }

    def model_metadata(self) -> dict[str, Any]:
        """Fields to surface in orchestrator transcript results."""
        meta: dict[str, Any] = {
            "model_requested": self.model_requested,
            "model_used": self.model_used,
        }
        if self.model_fallback_used:
            meta["model_fallback_used"] = True
            if self.model_fallback_reason:
                meta["model_fallback_reason"] = self.model_fallback_reason
        return meta

