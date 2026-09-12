from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm_harness.codex_cli import _run_codex_subprocess_sync as _run_cli_subprocess_sync
from swarm_harness.records import AgentCallResult, TokenUsage
from swarm_harness.util import first_json_object, utc_now, write_json


# Claude Code retries these itself via --fallback-model, so the backend does not
# reimplement the capacity-retry loop that CodexCLIBackend needs.
DEFAULT_CLAUDE_FALLBACK_MODELS: tuple[str, ...] = ("claude-sonnet-5",)

# gpt-6-astra exposes low/medium/high/xhigh/max/ultra; Claude exposes low/medium/high/xhigh/max.
_EFFORT_ALIASES = {"minimal": "low", "none": "low", "ultra": "max"}
_CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True)
class ClaudeCLIConfig:
    executable: str = "claude"
    model: str | None = None
    reasoning_effort: str | None = None
    # Mapped onto Claude's permission/tool flags; see _sandbox_args.
    sandbox: str = "read-only"
    # --bare forces ANTHROPIC_API_KEY auth and never reads OAuth/keychain, so it
    # is opt-in: leaving it False lets subscription logins work.
    bare: bool = False
    extra_args: tuple[str, ...] = ()
    fallback_models: tuple[str, ...] = DEFAULT_CLAUDE_FALLBACK_MODELS


def _normalize_effort(value: str | None) -> str | None:
    if not value:
        return None
    lowered = str(value).strip().lower()
    lowered = _EFFORT_ALIASES.get(lowered, lowered)
    return lowered if lowered in _CLAUDE_EFFORTS else None


def _sandbox_args(sandbox: str) -> list[str]:
    """Map the harness's Codex-shaped sandbox setting onto Claude's flags."""

    if sandbox in {"bypass", "docker-bypass"}:
        return ["--dangerously-skip-permissions"]
    if sandbox in {"workspace-write", "danger-full-access"}:
        return ["--permission-mode", "acceptEdits"]
    if sandbox and sandbox != "none":
        # read-only: let workers read and run checks, but never mutate the tree.
        return ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    return []


class ClaudeCLIBackend:
    """Run one Claude Code CLI worker per agent call."""

    def __init__(self, config: ClaudeCLIConfig | None = None) -> None:
        self.config = config or ClaudeCLIConfig()

    def build_command(
        self,
        *,
        schema_path: Path | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        cfg = self.config
        cmd = [cfg.executable, "-p", "--output-format", "json"]
        if cfg.bare:
            cmd.append("--bare")

        selected_model = model or cfg.model
        if selected_model:
            cmd.extend(["--model", selected_model])

        effort = _normalize_effort(reasoning_effort or cfg.reasoning_effort)
        if effort:
            cmd.extend(["--effort", effort])

        fallbacks = [m for m in cfg.fallback_models if m and m != selected_model]
        if fallbacks:
            cmd.extend(["--fallback-model", ",".join(fallbacks)])

        # Codex takes a schema file path; Claude takes the schema inline.
        if schema_path is not None and schema_path.exists():
            cmd.extend(["--json-schema", schema_path.read_text(encoding="utf-8")])

        cmd.extend(_sandbox_args(cfg.sandbox))
        cmd.extend(cfg.extra_args)
        return cmd

    async def run_agent(
        self,
        *,
        role: str,
        call_id: str,
        prompt: str,
        workspace: Path,
        schema_path: Path | None,
        timeout_s: int,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentCallResult:
        import asyncio

        workspace.mkdir(parents=True, exist_ok=True)
        workspace = workspace.resolve()
        (workspace / "prompt.md").write_text(prompt, encoding="utf-8")

        for stale_name in ("parsed.json", "response.md"):
            stale_path = workspace / stale_name
            if stale_path.exists():
                stale_path.unlink()

        requested_model = model or self.config.model
        cmd = self.build_command(
            schema_path=schema_path,
            model=requested_model,
            reasoning_effort=reasoning_effort,
        )

        started_at = utc_now()
        start = time.monotonic()
        returncode, stdout_raw, stderr_raw, timed_out = await asyncio.to_thread(
            _run_cli_subprocess_sync,
            cmd,
            cwd=str(workspace),
            env=os.environ.copy(),
            prompt=prompt,
            timeout_s=timeout_s,
        )
        finished_at = utc_now()

        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        (workspace / "stdout.json").write_text(stdout, encoding="utf-8")
        (workspace / "stderr.txt").write_text(stderr, encoding="utf-8")

        returncode = returncode if returncode is not None else -1
        envelope = first_json_object(stdout)
        content, parsed = _extract_content(envelope, stdout)
        (workspace / "response.md").write_text(content, encoding="utf-8")

        if parsed is not None:
            write_json(workspace / "parsed.json", parsed)

        usage = parse_claude_json(envelope)
        model_used = _model_used(envelope) or requested_model

        error = None
        if timed_out:
            error = f"Claude worker timed out after {timeout_s}s."
        elif envelope is not None and envelope.get("is_error"):
            # An auth or API failure still exits 0 with the message in `result`.
            error = str(envelope.get("result") or "Claude worker reported an error.")
        elif returncode != 0:
            error = f"Claude worker exited with status {returncode}."

        result = AgentCallResult(
            role=role,
            call_id=call_id,
            workspace=workspace,
            command=cmd,
            prompt=prompt,
            content=content,
            parsed=parsed,
            usage=usage,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=time.monotonic() - start,
            model_requested=requested_model,
            model_used=model_used,
        )
        if requested_model and model_used and model_used != requested_model:
            result.model_fallback_used = True
            result.model_fallback_reason = (
                f"Claude fell back from {requested_model!r} to {model_used!r}."
            )
        write_json(workspace / "call.json", result.to_trace_json())
        return result


def _extract_content(
    envelope: dict[str, Any] | None, stdout: str
) -> tuple[str, dict[str, Any] | None]:
    """Pull the agent's answer out of the --output-format json envelope."""

    if envelope is None:
        # Envelope itself failed to parse; keep raw stdout for inspection.
        return stdout, None

    for key in ("structured_output", "structured_result"):
        candidate = envelope.get(key)
        if isinstance(candidate, dict):
            return json.dumps(candidate, ensure_ascii=False), candidate

    raw = envelope.get("result")
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False), raw
    content = "" if raw is None else str(raw)
    if envelope.get("is_error"):
        return content, None
    return content, first_json_object(content)


def _model_used(envelope: dict[str, Any] | None) -> str | None:
    if not envelope:
        return None
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        # Keyed by model name; the last entry is the model that produced the answer.
        return str(list(model_usage.keys())[-1])
    return None


def parse_claude_json(envelope: dict[str, Any] | None) -> TokenUsage:
    """Map Claude's usage block onto the harness's TokenUsage."""

    usage = TokenUsage()
    if not envelope:
        return usage
    raw = envelope.get("usage")
    if not isinstance(raw, dict):
        return usage
    usage.input_tokens = int(raw.get("input_tokens") or 0)
    usage.cached_input_tokens = int(raw.get("cache_read_input_tokens") or 0)
    usage.output_tokens = int(raw.get("output_tokens") or 0)
    details = raw.get("output_tokens_details")
    if isinstance(details, dict):
        usage.reasoning_output_tokens = int(details.get("thinking_tokens") or 0)
    usage.n_turns = int(envelope.get("num_turns") or 0)
    return usage
