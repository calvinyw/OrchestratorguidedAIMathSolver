from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from swarm_harness.latex_artifacts import fallback_latex_document
from swarm_harness.records import AgentCallResult, TokenUsage
from swarm_harness.util import first_json_object, utc_now, write_json


class AgentBackend(Protocol):
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
        ...


# Tried when the requested model is at capacity. Prefer a broadly available, simpler model.
DEFAULT_CAPACITY_FALLBACK_MODELS: tuple[str, ...] = ("gpt-5.4",)


@dataclass(frozen=True)
class CodexCLIConfig:
    executable: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str = "read-only"
    ignore_user_config: bool = True
    ephemeral: bool = True
    skip_git_repo_check: bool = True
    json_events: bool = True
    extra_args: tuple[str, ...] = ()
    capacity_fallback_models: tuple[str, ...] = DEFAULT_CAPACITY_FALLBACK_MODELS


class CodexCLIBackend:
    """Run one Codex CLI worker per agent call."""

    def __init__(self, config: CodexCLIConfig | None = None) -> None:
        self.config = config or CodexCLIConfig()

    def build_command(
        self,
        *,
        workspace: Path,
        last_message_path: Path,
        schema_path: Path | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> list[str]:
        cfg = self.config
        cmd = [cfg.executable, "exec"]
        if cfg.ignore_user_config:
            cmd.append("--ignore-user-config")
        if cfg.ephemeral:
            cmd.append("--ephemeral")
        if cfg.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")
        if cfg.json_events:
            cmd.append("--json")
        selected_model = model or cfg.model
        selected_reasoning_effort = reasoning_effort or cfg.reasoning_effort
        if selected_model:
            cmd.extend(["-m", selected_model])
        if selected_reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{selected_reasoning_effort}"'])
        if cfg.sandbox in {"bypass", "docker-bypass"}:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif cfg.sandbox and cfg.sandbox != "none":
            cmd.extend(["--sandbox", cfg.sandbox])
        cmd.extend(["-C", str(workspace)])
        cmd.extend(["-o", str(last_message_path)])
        if schema_path is not None:
            cmd.extend(["--output-schema", str(schema_path)])
        cmd.extend(cfg.extra_args)
        cmd.append("-")
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
        workspace.mkdir(parents=True, exist_ok=True)
        workspace = workspace.resolve()
        (workspace / "prompt.md").write_text(prompt, encoding="utf-8")

        requested_model = model or self.config.model
        start = time.monotonic()
        started_at = utc_now()
        result = await self._run_once(
            role=role,
            call_id=call_id,
            prompt=prompt,
            workspace=workspace,
            schema_path=schema_path,
            timeout_s=timeout_s,
            model=requested_model,
            reasoning_effort=reasoning_effort,
            attempt_label="primary",
        )
        result.model_requested = requested_model
        result.model_used = requested_model
        result.started_at = started_at

        capacity_message = capacity_error_message(result)
        if capacity_message is None:
            result.duration_seconds = time.monotonic() - start
            result.finished_at = utc_now()
            write_json(workspace / "call.json", result.to_trace_json())
            return result

        fallback_models = [
            candidate
            for candidate in self.config.capacity_fallback_models
            if candidate and candidate != requested_model
        ]
        attempts: list[dict[str, Any]] = [
            {
                "model": requested_model,
                "ok": False,
                "error": capacity_message,
                "returncode": result.returncode,
            }
        ]
        write_json(
            workspace / "capacity_retry.json",
            {
                "requested_model": requested_model,
                "capacity_message": capacity_message,
                "fallback_models": fallback_models,
                "attempts": attempts,
            },
        )

        for index, fallback_model in enumerate(fallback_models, start=1):
            fallback = await self._run_once(
                role=role,
                call_id=call_id,
                prompt=prompt,
                workspace=workspace,
                schema_path=schema_path,
                timeout_s=timeout_s,
                model=fallback_model,
                reasoning_effort=reasoning_effort,
                attempt_label=f"fallback-{index:02d}-{fallback_model}",
            )
            fallback.model_requested = requested_model
            fallback.model_used = fallback_model
            fallback.model_fallback_used = True
            fallback.model_fallback_reason = (
                f"Requested model {requested_model!r} was at capacity "
                f"({capacity_message}); retried with {fallback_model!r}."
            )
            fallback.started_at = started_at
            attempts.append(
                {
                    "model": fallback_model,
                    "ok": fallback.ok,
                    "error": fallback.error or capacity_error_message(fallback),
                    "returncode": fallback.returncode,
                }
            )
            write_json(
                workspace / "capacity_retry.json",
                {
                    "requested_model": requested_model,
                    "capacity_message": capacity_message,
                    "fallback_models": fallback_models,
                    "attempts": attempts,
                    "model_used": fallback_model if fallback.ok else None,
                },
            )
            if fallback.ok or capacity_error_message(fallback) is None:
                fallback.duration_seconds = time.monotonic() - start
                fallback.finished_at = utc_now()
                write_json(workspace / "call.json", fallback.to_trace_json())
                return fallback
            result = fallback

        result.duration_seconds = time.monotonic() - start
        result.finished_at = utc_now()
        if result.error is None and capacity_message:
            result.error = capacity_message
        write_json(workspace / "call.json", result.to_trace_json())
        return result

    async def _run_once(
        self,
        *,
        role: str,
        call_id: str,
        prompt: str,
        workspace: Path,
        schema_path: Path | None,
        timeout_s: int,
        model: str | None,
        reasoning_effort: str | None,
        attempt_label: str,
    ) -> AgentCallResult:
        last_message_path = workspace / "last_message.txt"
        # Avoid treating a previous attempt's last_message as this call's output.
        if last_message_path.exists():
            last_message_path.unlink()
        for stale_name in ("parsed.json", "response.md"):
            stale_path = workspace / stale_name
            if stale_path.exists():
                stale_path.unlink()

        cmd = self.build_command(
            workspace=workspace,
            last_message_path=last_message_path,
            schema_path=schema_path,
            model=model,
            reasoning_effort=reasoning_effort,
        )

        started_at = utc_now()
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        timed_out = False
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=timeout_s,
            )
        except TimeoutError:
            timed_out = True
            proc.kill()
            stdout_raw, stderr_raw = await proc.communicate()

        finished_at = utc_now()
        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        (workspace / "stdout.jsonl").write_text(stdout, encoding="utf-8")
        (workspace / "stderr.txt").write_text(stderr, encoding="utf-8")
        (workspace / f"stdout.{attempt_label}.jsonl").write_text(stdout, encoding="utf-8")
        (workspace / f"stderr.{attempt_label}.txt").write_text(stderr, encoding="utf-8")

        returncode = proc.returncode if proc.returncode is not None else -1
        content = ""
        if last_message_path.exists() and returncode == 0 and not timed_out:
            content = last_message_path.read_text(encoding="utf-8", errors="replace")
        elif stdout.strip():
            content = stdout
        (workspace / "response.md").write_text(content, encoding="utf-8")

        parsed = first_json_object(content)
        if parsed is not None and not _looks_like_codex_error_event(parsed):
            write_json(workspace / "parsed.json", parsed)
        else:
            parsed = None
            parsed_path = workspace / "parsed.json"
            if parsed_path.exists():
                parsed_path.unlink()

        usage = parse_codex_jsonl(stdout)
        error = None
        if timed_out:
            error = f"Codex worker timed out after {timeout_s}s."
        elif returncode != 0:
            capacity = extract_capacity_error(stdout, stderr, content)
            error = capacity or f"Codex worker exited with status {returncode}."

        return AgentCallResult(
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
            model_requested=model,
            model_used=model,
        )


class MockBackend:
    """Deterministic backend for tests and smoke runs."""

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
        del schema_path, timeout_s
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "prompt.md").write_text(prompt, encoding="utf-8")
        started_at = utc_now()
        start = time.monotonic()
        parsed = self._response_for(role, call_id, prompt)
        content = json.dumps(parsed, indent=2)
        (workspace / "last_message.txt").write_text(content, encoding="utf-8")
        (workspace / "response.md").write_text(content, encoding="utf-8")
        write_json(workspace / "parsed.json", parsed)
        selected_model = model or "mock"
        result = AgentCallResult(
            role=role,
            call_id=call_id,
            workspace=workspace,
            command=["mock-codex", role],
            prompt=prompt,
            content=content,
            parsed=parsed,
            usage=TokenUsage(input_tokens=len(prompt.split()), output_tokens=len(content.split()), n_turns=1),
            started_at=started_at,
            finished_at=utc_now(),
            duration_seconds=time.monotonic() - start,
            model_requested=selected_model,
            model_used=selected_model,
        )
        write_json(workspace / "call.json", result.to_trace_json())
        return result

    def _response_for(self, role: str, call_id: str, prompt: str) -> dict[str, Any]:
        problem = _extract_tag(prompt, "problem") or "the problem"
        if role == "orchestrator":
            return _mock_orchestrator_decision(prompt, problem)
        if role == "tex_writer":
            solution = _extract_tag(prompt, "solution_markdown") or _mock_math_fragment(problem)
            return {
                "task_id": "solution-tex",
                "title": "TeX solution artifact",
                "latex_document": fallback_latex_document(problem, solution),
                "confidence": 0.9,
                "notes": ["Mock TeX writer generated a deterministic document."],
            }
        if role == "graph_builder":
            task_id = _extract_tag(prompt, "task_id") or call_id
            return _mock_graph_builder_graph(problem, task_id)
        if role in {"solver", "refiner", "custom"}:
            task_id = _extract_tag(prompt, "task_id") or call_id
            title = _extract_tag(prompt, "task_title") or role.title()
            fragment = _mock_math_fragment(problem)
            return {
                "task_id": task_id,
                "title": title,
                "approach": "Use a standard contradiction/direct-proof structure and state the key invariant clearly.",
                "answer_fragment": fragment,
                "confidence": 0.82,
                "assumptions": [],
            }
        if role == "proof_writer":
            return _mock_proof_writer_decision(prompt, call_id, problem)
        if role == "lemma_prover":
            return _mock_lemma_prover_decision(prompt, call_id, problem)
        if role == "term_definer":
            return _mock_term_definer_decision(prompt, call_id, problem)
        if role == "counterexample_finder":
            return _mock_counterexample_finder_decision(prompt, call_id, problem)
        if role == "final_proof_writer":
            task_id = _extract_tag(prompt, "task_id") or call_id
            return {
                "task_id": task_id,
                "title": "Final LaTeX proof",
                "approach": "Combine prior lemma and proof-writer LaTeX fragments into one document.",
                "latex_document": _mock_latex_document(problem),
                "confidence": 0.84,
                "assumptions": [],
            }
        if role == "final_critiquer":
            return {
                "approved": True,
                "score": 0.88,
                "strongest_task_id": "final-proof",
                "issues": [],
                "revision_tasks": [],
            }
        if role == "summarizer":
            return {
                "answer": _mock_math_fragment(problem),
                "confidence": 0.86,
                "reasoning_summary": "Synthesized the proof draft with the critique and retained only supported steps.",
                "caveats": [],
                "sources": ["proof-writer", "critiquer"],
            }
        if role == "critic":
            return {
                "approved": True,
                "score": 0.86,
                "strongest_task_id": "main-proof",
                "issues": [],
                "revision_tasks": [],
            }
        return {"message": "mock response"}


def _mock_proof_writer_decision(prompt: str, call_id: str, problem: str) -> dict[str, Any]:
    helper_results = _extract_tag(prompt, "helper_results")
    task_id = _extract_tag(prompt, "task_id") or call_id
    statement = _extract_tag(prompt, "statement_to_prove") or problem
    fragment = _mock_math_fragment(statement)
    if helper_results and helper_results.strip() not in {"", "[]"}:
        return {
            "thought": "Helper sub-subagents returned browse and code results; finishing the proof draft.",
            "actions": [
                {
                    "type": "finish",
                    "task_id": task_id,
                    "title": "Main proof",
                    "approach": "Use a standard contradiction/direct-proof structure and state the key invariant clearly.",
                    "answer_fragment": fragment,
                    "confidence": 0.82,
                    "assumptions": [],
                }
            ],
        }
    return {
        "thought": "Gather a reference and verify a quick computation before drafting the proof.",
        "actions": [
            {
                "type": "browse",
                "url": "https://example.com/irrational-sqrt2",
            },
            {
                "type": "code",
                "instruction": "from math import isqrt\nprint(isqrt(2) ** 2)",
                "language": "python",
            },
        ],
    }


def _mock_counterexample_finder_decision(prompt: str, call_id: str, problem: str) -> dict[str, Any]:
    helper_results = _extract_tag(prompt, "helper_results")
    task_id = _extract_tag(prompt, "task_id") or call_id
    if helper_results and helper_results.strip() not in {"", "[]"}:
        return {
            "thought": "The computational search found no counterexample; the statement looks true.",
            "actions": [
                {
                    "type": "finish",
                    "task_id": task_id,
                    "title": "Counterexample search",
                    "approach": "Searched small and boundary cases for a counterexample.",
                    "answer_fragment": "LIKELY TRUE: no counterexample found after checking small cases.",
                    "confidence": 0.6,
                    "assumptions": [],
                }
            ],
        }
    return {
        "thought": "Search for a counterexample with a quick computation before concluding.",
        "actions": [
            {
                "type": "code",
                "instruction": "print('search for counterexample')",
                "language": "python",
            }
        ],
    }


def _mock_term_definer_decision(prompt: str, call_id: str, problem: str) -> dict[str, Any]:
    helper_results = _extract_tag(prompt, "helper_results")
    task_id = _extract_tag(prompt, "task_id") or call_id
    term = _extract_tag(prompt, "term_to_define") or "the term"
    if helper_results and helper_results.strip() not in {"", "[]"}:
        return {
            "thought": "Search results returned a definition; finishing with the term's meaning.",
            "actions": [
                {
                    "type": "finish",
                    "task_id": task_id,
                    "title": f"Definition of {term}",
                    "approach": "Consulted references to pin down the precise meaning in context.",
                    "answer_fragment": (
                        f"In the context of this problem, \"{term}\" refers to the standard "
                        "mathematical object of that name, taken with its usual definition and notation."
                    ),
                    "confidence": 0.8,
                    "assumptions": [
                        "Assumed the term carries its standard meaning in the problem's subfield."
                    ],
                }
            ],
        }
    return {
        "thought": "Look up an authoritative definition of the term before finishing.",
        "actions": [
            {
                "type": "search",
                "query": f"definition of {term} in mathematics",
            },
        ],
    }


def _mock_lemma_prover_decision(prompt: str, call_id: str, problem: str) -> dict[str, Any]:
    task_id = _extract_tag(prompt, "task_id") or call_id
    node_id = _extract_tag(prompt, "node_id") or task_id
    target = _extract_tag(prompt, "target_node")
    title = f"Lemma {node_id}"
    if target:
        try:
            parsed = json.loads(target)
            if isinstance(parsed, dict):
                title = str(parsed.get("statement") or title)[:120]
        except json.JSONDecodeError:
            pass
    fragment = _mock_math_fragment(problem)
    return {
        "thought": f"Prove node {node_id} using its dependencies and prior lemmas.",
        "actions": [
            {
                "type": "finish",
                "task_id": task_id,
                "title": title,
                "approach": "Apply the Graph_builder proof hint and cite dependency nodes.",
                "answer_fragment": fragment,
                "confidence": 0.8,
                "assumptions": [],
            }
        ],
    }


def _mock_orchestrator_decision(prompt: str, problem: str) -> dict[str, Any]:
    """Deterministic orchestrator that uses the standard tasks, then finishes."""

    force_finish = "<force_finish>" in prompt
    try:
        step = int(_extract_tag(prompt, "step") or "1")
    except ValueError:
        step = 1

    if force_finish or step >= 4:
        return {
            "thought": "The lemma proofs, critiquer, and summarizer agree, so I can finalize the answer.",
            "actions": [
                {
                    "type": "finish",
                    "answer": _mock_math_fragment(problem),
                    "confidence": 0.86,
                    "reasoning_summary": "Aggregated parallel lemma proofs, critique, and summary from the standard task agents.",
                    "caveats": [],
                    "sources": ["lemma-prover", "critiquer", "summarizer"],
                }
            ],
        }

    if step == 3:
        return {
            "thought": "Summarize the lemma proofs and critique into a final candidate solution before finishing.",
            "actions": [
                {
                    "type": "summarizer",
                    "task_id": "solution-summary",
                    "prompt": "Synthesize the parallel lemma proofs and critique into a concise final solution.",
                }
            ],
        }

    if step == 2:
        return {
            "thought": "Prove the Graph_builder lemma nodes in parallel before synthesis.",
            "actions": [
                {
                    "type": "lemma_prover",
                    "task_id": "lemma-l1",
                    "graph_id": "contradiction",
                    "node_id": "l1",
                    "prompt": "Prove node l1 from the proof DAG.",
                },
                {
                    "type": "lemma_prover",
                    "task_id": "lemma-l2",
                    "graph_id": "contradiction",
                    "node_id": "l2",
                    "prompt": "Prove node l2 from the proof DAG.",
                },
                {
                    "type": "lemma_prover",
                    "task_id": "lemma-l3",
                    "graph_id": "contradiction",
                    "node_id": "l3",
                    "prompt": "Prove node l3 from the proof DAG.",
                },
                {
                    "type": "critiquer",
                    "task_id": "proof-critique",
                    "statement": "sqrt(2) is irrational.",
                    "proof": _mock_math_fragment("sqrt(2) is irrational."),
                    "prompt": "Identify gaps, hidden assumptions, and edge cases in this proof.",
                },
            ],
        }

    return {
        "thought": "First ask the Graph_builder for a proof DAG, then prove its lemmas in parallel.",
        "actions": [
            {
                "type": "Graph_builder",
                "task_id": "proof-dag",
                "prompt": "Decompose the problem into a ProofFlow-style DAG of statements to prove.",
            },
        ],
    }


def parse_codex_jsonl(text: str) -> TokenUsage:
    usage = TokenUsage()
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        raw_usage = event.get("usage")
        if not isinstance(raw_usage, dict):
            continue
        usage.input_tokens += int(raw_usage.get("input_tokens") or 0)
        usage.cached_input_tokens += int(raw_usage.get("cached_input_tokens") or 0)
        usage.output_tokens += int(raw_usage.get("output_tokens") or 0)
        usage.reasoning_output_tokens += int(raw_usage.get("reasoning_output_tokens") or 0)
        usage.n_turns += 1
    return usage


def capacity_error_message(result: AgentCallResult) -> str | None:
    """Return a capacity-error message if this call failed because the model was full."""
    if result.ok:
        return None
    return extract_capacity_error(result.stdout, result.stderr, result.content, result.error)


def extract_capacity_error(*texts: str | None) -> str | None:
    """Find a model-capacity error in Codex JSONL, stderr, or free-form text."""
    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                if _looks_like_capacity_text(stripped):
                    return stripped
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                if _looks_like_capacity_text(stripped):
                    return stripped
                continue
            message = _capacity_message_from_event(event)
            if message:
                return message
        if _looks_like_capacity_text(text):
            # Prefer a short single-line summary when the blob is large JSONL.
            for line in text.splitlines():
                if _looks_like_capacity_text(line):
                    return line.strip()
            return "Selected model is at capacity. Please try a different model."
    return None


def _capacity_message_from_event(event: Any) -> str | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    if event_type == "error":
        message = str(event.get("message") or "")
        if _looks_like_capacity_text(message):
            return message
    if event_type == "turn.failed":
        err = event.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            if _looks_like_capacity_text(message):
                return message
        elif isinstance(err, str) and _looks_like_capacity_text(err):
            return err
    if _looks_like_codex_error_event(event):
        message = str(event.get("message") or "")
        if _looks_like_capacity_text(message):
            return message
    return None


def _looks_like_codex_error_event(value: Any) -> bool:
    return isinstance(value, dict) and str(value.get("type") or "") in {"error", "turn.failed"}


def _looks_like_capacity_text(text: str) -> bool:
    lower = text.lower()
    return "at capacity" in lower or (
        "try a different model" in lower and "model" in lower
    )


def _extract_tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL)
    if not match:
        return None
    return match.group(1).strip()


def _mock_math_fragment(problem: str) -> str:
    lower = problem.lower()
    if "sqrt" in lower and "2" in lower and "irrational" in lower:
        return (
            "\\begin{proof}\n"
            "Assume for contradiction that $\\sqrt{2}=a/b$ in lowest terms with integers $a,b$ and $b\\neq 0$. "
            "Then $a^2=2b^2$, so $a$ is even; write $a=2k$. Then $4k^2=2b^2$, hence $b^2=2k^2$ and $b$ is even. "
            "This contradicts that $a/b$ was in lowest terms. Therefore $\\sqrt{2}$ is irrational.\n"
            "\\end{proof}"
        )
    if "infinitely many primes" in lower:
        return (
            "\\begin{proof}\n"
            "Assume there are finitely many primes $p_1,\\ldots,p_n$. The number $N=p_1\\cdots p_n+1$ is not "
            "divivisible by any $p_i$, so it has a prime divisor outside the list, a contradiction. "
            "Hence there are infinitely many primes.\n"
            "\\end{proof}"
        )
    return (
        "\\begin{proof}\n"
        "A complete solution should identify the main invariant, prove each implication from the hypotheses, "
        "and finish by explicitly deriving the requested conclusion.\n"
        "\\end{proof}"
    )


def _mock_latex_document(problem: str) -> str:
    body = _mock_math_fragment(problem)
    escaped = problem.replace("_", "\\_")
    return (
        "\\documentclass{article}\n"
        "\\usepackage{amsmath,amsthm}\n"
        "\\newtheorem{theorem}{Theorem}\n"
        "\\begin{document}\n"
        f"\\begin{{theorem}}\n{escaped}\n\\end{{theorem}}\n"
        f"{body}\n"
        "\\end{document}\n"
    )


def _mock_graph_builder_graph(problem: str, task_id: str) -> dict[str, Any]:
    lower = problem.lower()
    if "sqrt" in lower and "2" in lower and "irrational" in lower:
        contradiction_nodes = [
            {
                "id": "h1",
                "kind": "theorem_hypothesis",
                "statement": "Assume for contradiction that sqrt(2) = a / b for integers a and b in lowest terms, with b nonzero.",
                "dependencies": [],
                "proof_hint": "This is the contradiction hypothesis.",
            },
            {
                "id": "l1",
                "kind": "lemma",
                "statement": "From sqrt(2) = a / b, derive a^2 = 2 * b^2.",
                "dependencies": ["h1"],
                "proof_hint": "Clear denominators and square both sides.",
            },
            {
                "id": "l2",
                "kind": "lemma",
                "statement": "If a^2 = 2 * b^2, then a is even.",
                "dependencies": ["l1"],
                "proof_hint": "Use the standard parity fact that an integer with even square is even.",
            },
            {
                "id": "l3",
                "kind": "lemma",
                "statement": "If a is even and a^2 = 2 * b^2, then b is even.",
                "dependencies": ["l1", "l2"],
                "proof_hint": "Write a = 2k, substitute, and divide by 2.",
            },
            {
                "id": "c1",
                "kind": "conclusion",
                "statement": "The assumption that a / b is in lowest terms contradicts a and b both being even; therefore sqrt(2) is irrational.",
                "dependencies": ["h1", "l2", "l3"],
                "proof_hint": "Use the contradiction to discharge the initial rationality assumption.",
            },
        ]
        prime_nodes = [
            {
                "id": "h1",
                "kind": "theorem_hypothesis",
                "statement": "Assume sqrt(2) = a / b in lowest terms with integers a, b and b nonzero.",
                "dependencies": [],
                "proof_hint": "Set up the rationality assumption.",
            },
            {
                "id": "l1",
                "kind": "lemma",
                "statement": "From sqrt(2) = a / b, deduce a^2 = 2 * b^2.",
                "dependencies": ["h1"],
                "proof_hint": "Square and clear denominators.",
            },
            {
                "id": "l2",
                "kind": "lemma",
                "statement": "The equation a^2 = 2 * b^2 forces 2 to divide a.",
                "dependencies": ["l1"],
                "proof_hint": "Use unique prime factorization or parity.",
            },
            {
                "id": "l3",
                "kind": "lemma",
                "statement": "After writing a = 2k, the equation yields b^2 = 2 * k^2, so 2 also divides b.",
                "dependencies": ["l1", "l2"],
                "proof_hint": "Substitute and divide by 2.",
            },
            {
                "id": "c1",
                "kind": "conclusion",
                "statement": "Both a and b are divisible by 2, contradicting lowest terms; hence sqrt(2) is irrational.",
                "dependencies": ["h1", "l2", "l3"],
                "proof_hint": "Close the contradiction.",
            },
        ]
        return {
            "task_id": task_id,
            "title": "Proof DAG options for irrationality of sqrt(2)",
            "summary": "Two standard routes: a parity-based contradiction proof and a prime-factorization contradiction proof.",
            "graphs": [
                {
                    "graph_id": "contradiction",
                    "title": "Parity contradiction proof",
                    "graph_summary": "Rationality implies both numerator and denominator are even.",
                    "nodes": contradiction_nodes,
                    "topological_order": ["h1", "l1", "l2", "l3", "c1"],
                    "final_node_ids": ["c1"],
                    "confidence": 0.84,
                },
                {
                    "graph_id": "prime_factorization",
                    "title": "Prime factorization contradiction proof",
                    "graph_summary": "Rationality forces 2 to divide both a and b in lowest terms.",
                    "nodes": prime_nodes,
                    "topological_order": ["h1", "l1", "l2", "l3", "c1"],
                    "final_node_ids": ["c1"],
                    "confidence": 0.8,
                },
            ],
        }

    generic_graph = {
        "graph_id": "default",
        "title": "Generic proof DAG",
        "graph_summary": "A minimal DAG that separates hypotheses, intermediate proof work, and the final conclusion.",
        "nodes": [
            {
                "id": "h1",
                "kind": "theorem_hypothesis",
                "statement": "Collect the hypotheses stated in the problem.",
                "dependencies": [],
                "proof_hint": "Restate the assumptions exactly.",
            },
            {
                "id": "l1",
                "kind": "lemma",
                "statement": "Derive the central intermediate claim from the hypotheses.",
                "dependencies": ["h1"],
                "proof_hint": "Use the main invariant or theorem suggested by the problem.",
            },
            {
                "id": "c1",
                "kind": "conclusion",
                "statement": "Use the intermediate claim to prove the requested conclusion.",
                "dependencies": ["l1"],
                "proof_hint": "Apply the lemma and explicitly close the final target.",
            },
        ],
        "topological_order": ["h1", "l1", "c1"],
        "final_node_ids": ["c1"],
        "confidence": 0.65,
    }
    return {
        "task_id": task_id,
        "title": "Generic proof DAG options",
        "summary": "A single default decomposition when no specialized structure is obvious.",
        "graphs": [generic_graph],
    }
