from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm_harness.codex_cli import AgentBackend, MockBackend
from swarm_harness.latex_artifacts import compile_tex_to_pdf, fallback_latex_document
from swarm_harness.prompts import (
    ORCHESTRATOR_COMMANDS,
    agent_prompt,
    agent_tools_step_prompt,
    aristotle_advisor_task_prompt,
    counterexample_finder_task_prompt,
    critiquer_task_prompt,
    final_critiquer_task_prompt,
    final_proof_writer_task_prompt,
    graph_builder_task_prompt,
    lemma_prover_task_prompt,
    orchestrator_prompt,
    proof_writer_task_prompt,
    subagent_task_prompt,
    summarizer_task_prompt,
    term_definer_task_prompt,
    tex_artifact_prompt,
)
from swarm_harness.records import AgentCallResult, Problem, TokenUsage
from swarm_harness.subagents import SubagentRegistry, SubagentSpec
from swarm_harness.tools import OrchestratorTools
from swarm_harness.util import append_jsonl, read_json, safe_id, utc_now, write_json


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_DIR = REPO_ROOT / "schemas"
ORCHESTRATOR_REASONING_EFFORT = "ultra"
ORCHESTRATOR_RECENT_STEPS_ON_RETRY = 8
ORCHESTRATOR_OLDER_CONTEXT_MAX_CHARS = 120_000

_TOOL_ACTIONS = {"search", "browse", "code", "aristotle"}
_STANDARD_TASK_ACTIONS = {
    "Graph_builder",
    "Aristotle_advisor",
    "lemma_prover",
    "proof_writer",
    "final_proof_writer",
    "summarizer",
    "critiquer",
    "final_critiquer",
    "counterexample_finder",
    "term_definer",
}
_MAX_AGENT_TOOL_STEPS = 5
_TEX_WRITER_SYSTEM_PROMPT = (
    "You are a careful mathematical TeX writer. Convert a solved problem and its Markdown "
    "solution into a clean, self-contained LaTeX article. Preserve the mathematics exactly, "
    "write readable TeX, and return only JSON matching the requested schema."
)
_STANDARD_TASK_SPECS = {
    "Graph_builder": {
        "name": "Graph_builder",
        "role": "graph_builder",
        "schema_name": "graph_builder.schema.json",
        "system_prompt": (
            "You are a Graph_builder for a math-solving swarm. Decompose a proof into one or more "
            "ProofFlow-style directed acyclic graphs of statements (alternative approaches when "
            "useful), where each node is provable from its incoming dependency nodes and standard "
            "facts."
        ),
    },
    "lemma_prover": {
        "name": "lemma-prover",
        "role": "lemma_prover",
        "schema_name": "solver.schema.json",
        "tools": ("browse", "code"),
        "system_prompt": (
            "You are a lemma prover for a math-solving swarm. Prove individual lemma or "
            "conclusion nodes from a chosen Graph_builder DAG (identified by graph_id when "
            "multiple approaches exist) using only their dependencies and standard facts. Write "
            "proofs in LaTeX in answer_fragment. You may delegate web lookups or computations "
            "to helper sub-subagents."
        ),
    },
    "proof_writer": {
        "name": "proof-writer",
        "role": "proof_writer",
        "schema_name": "solver.schema.json",
        "tools": ("browse", "code"),
        "system_prompt": (
            "You are a proof writer for a math-solving swarm. Given an explicit statement to "
            "prove, write a rigorous, readable LaTeX proof with clear hypotheses, justified "
            "implications, and an explicit conclusion. Put the proof body in answer_fragment "
            "as LaTeX. You may delegate web lookups or computations to helper sub-subagents."
        ),
    },
    "final_proof_writer": {
        "name": "final-proof-writer",
        "role": "final_proof_writer",
        "schema_name": "final_proof.schema.json",
        "system_prompt": (
            "You are a final proof writer for a math-solving swarm. Combine prior LaTeX proof "
            "fragments from lemma provers and proof writers into one complete LaTeX document "
            "that proves the original problem statement."
        ),
    },
    "summarizer": {
        "name": "summarizer",
        "role": "summarizer",
        "schema_name": "synthesis.schema.json",
        "system_prompt": (
            "You are a summarizer for a math-solving swarm. Synthesize prior work into "
            "a concise final answer, preserving caveats and citing the strongest sources."
        ),
    },
    "critiquer": {
        "name": "critiquer",
        "role": "critic",
        "schema_name": "critic.schema.json",
        "system_prompt": (
            "You are a critiquer for a math-solving swarm. Given an explicit statement and a "
            "proof draft, check whether the proof rigorously establishes the statement. Look for "
            "gaps, hidden assumptions, edge cases, and precise revision needs. Do not request "
            "re-computation of results already verified with code."
        ),
    },
    "final_critiquer": {
        "name": "final-critiquer",
        "role": "final_critiquer",
        "schema_name": "critic.schema.json",
        "system_prompt": (
            "You are a final critiquer for a math-solving swarm. Given the original problem "
            "statement and a complete LaTeX proof document, check whether the document "
            "rigorously proves the original statement. Look for gaps, missing lemmas, notation "
            "issues, and precise revision needs."
        ),
    },
    "counterexample_finder": {
        "name": "counterexample-finder",
        "role": "counterexample_finder",
        "schema_name": "solver.schema.json",
        "tools": ("browse", "code"),
        "system_prompt": (
            "You are a counterexample finder for a math-solving swarm. Given a statement, try to "
            "DISPROVE it by searching for counterexamples (small cases, boundary and degenerate "
            "cases) using code and references. Report a clear verdict (FALSE with a counterexample, "
            "LIKELY TRUE, or INCONCLUSIVE) in answer_fragment. You may delegate web lookups or "
            "computations to helper sub-subagents."
        ),
    },
    "term_definer": {
        "name": "term-definer",
        "role": "term_definer",
        "schema_name": "solver.schema.json",
        "tools": ("search", "browse", "code"),
        "system_prompt": (
            "You are a term definer for a math-solving swarm. Your sole goal is to figure out "
            "what a single term or phrase from the problem means, precisely, in context. Determine "
            "its exact mathematical definition and standard notation, and flag any ambiguity. Put "
            "the definition in answer_fragment. You may delegate web lookups or computations to "
            "helper sub-subagents."
        ),
    },
    "Aristotle_advisor": {
        "name": "aristotle-advisor",
        "role": "aristotle_advisor",
        "schema_name": "solver.schema.json",
        "tools": ("search", "browse", "code"),
        "system_prompt": (
            "You are an Aristotle advisor for a math-solving swarm. Before any Aristotle CLI/API "
            "call, read current Aristotle documentation, decide whether Aristotle is appropriate, "
            "and recommend the exact formalization or sorry-filling call only when justified."
        ),
    },
}


@dataclass(frozen=True)
class SwarmSettings:
    output_dir: Path
    workflow_config: dict[str, Any]
    schema_dir: Path = DEFAULT_SCHEMA_DIR
    max_parallel: int = 30
    max_steps: int = 40
    max_runtime_minutes: int = 600
    agent_timeout_s: int = 3000
    assign_task_timeout_s: int = 6000
    run_id: str | None = None
    tools_mock: bool | None = None
    aristotle_enabled: bool = False
    aristotle_executable: str = "aristotle"
    aristotle_timeout_s: int = 8 * 60 * 60
    codebase_dir: Path | None = None
    save_code: bool = False


@dataclass
class _RunContext:
    run_id: str
    run_dir: Path
    agents_dir: Path
    trace_path: Path
    problem: Problem
    registry: SubagentRegistry
    tools: OrchestratorTools
    call_counter: int = 0
    aristotle_advice_seen: bool = False


class SwarmOrchestrator:
    """Self-directed Kimi-style orchestrator.

    The orchestrator agent decides the workflow at runtime. There are no built-in
    subagents or fixed stages: it spawns specialized subagents, assigns work to them
    (in parallel), uses tools, and finishes when it has a rigorous answer.
    """

    def __init__(self, backend: AgentBackend, settings: SwarmSettings) -> None:
        self.backend = backend
        self.settings = settings
        self._ctx: _RunContext | None = None

    @property
    def run_context(self) -> _RunContext | None:
        return self._ctx

    # -- Kimi-style commands -------------------------------------------------

    def create_subagent(
        self,
        name: str,
        system_prompt: str,
        *,
        role: str = "custom",
        schema_name: str = "solver.schema.json",
        tools: tuple[str, ...] | list[str] = (),
    ) -> SubagentSpec:
        """Spin up a specialized subagent the orchestrator can assign work to."""
        ctx = self._require_context()
        spec = ctx.registry.create_subagent(
            name,
            system_prompt,
            role=role,
            schema_name=schema_name,
            tools=tools,
        )
        append_jsonl(
            ctx.trace_path,
            {
                "type": "orchestrator.create_subagent",
                "name": spec.name,
                "role": spec.role,
                "schema_name": spec.schema_name,
                "tools": list(spec.tools),
                "at": utc_now(),
            },
        )
        return spec

    async def assign_task(
        self,
        agent: str | SubagentSpec,
        prompt: str,
        *,
        call_id: str | None = None,
        schema_name: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentCallResult:
        """Delegate a unit of work to a named subagent."""
        ctx = self._require_context()
        spec = ctx.registry.resolve(agent)
        call_id = call_id or self._next_call_id(spec.name)
        if spec.tools:
            return await self._assign_task_with_tools(
                spec,
                prompt,
                call_id=call_id,
                schema_name=schema_name or spec.schema_name,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        full_prompt = agent_prompt(spec.system_prompt, prompt)
        return await self._call(
            role=spec.role,
            call_id=call_id,
            prompt=full_prompt,
            workspace=ctx.agents_dir / call_id,
            schema_name=schema_name or spec.schema_name,
            trace_path=ctx.trace_path,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_s=self.settings.assign_task_timeout_s,
        )

    async def _assign_task_with_tools(
        self,
        spec: SubagentSpec,
        prompt: str,
        *,
        call_id: str,
        schema_name: str,
        model: str | None,
        reasoning_effort: str | None,
    ) -> AgentCallResult:
        ctx = self._require_context()
        base_prompt = agent_prompt(spec.system_prompt, prompt)
        helper_results: list[dict[str, Any]] = []
        last_call: AgentCallResult | None = None
        max_steps = _MAX_AGENT_TOOL_STEPS

        for step in range(1, max_steps + 1):
            force_finish = step == max_steps
            step_prompt = agent_tools_step_prompt(
                base_prompt,
                helper_results,
                step=step,
                max_steps=max_steps,
                force_finish=force_finish,
            )
            step_call_id = f"{call_id}-helper-{step:02d}"
            last_call = await self._call(
                role=spec.role,
                call_id=step_call_id,
                prompt=step_prompt,
                workspace=ctx.agents_dir / step_call_id,
                schema_name="agent_tools.schema.json",
                trace_path=ctx.trace_path,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout_s=self.settings.assign_task_timeout_s,
            )
            decision = _valid_agent_tools(last_call.parsed)
            finish = _extract_solver_finish(decision.get("actions") or [])
            if finish is not None:
                code_items = [item for item in helper_results if item.get("action") == "code"]
                if code_items:
                    finish = {**finish, "code_verifications": code_items}
                return AgentCallResult(
                    role=last_call.role,
                    call_id=call_id,
                    workspace=ctx.agents_dir / call_id,
                    command=last_call.command,
                    prompt=base_prompt,
                    content=last_call.content,
                    parsed=finish,
                    usage=last_call.usage,
                    returncode=last_call.returncode,
                    stdout=last_call.stdout,
                    stderr=last_call.stderr,
                    error=last_call.error,
                    started_at=last_call.started_at,
                    finished_at=last_call.finished_at,
                    duration_seconds=last_call.duration_seconds,
                    model_requested=last_call.model_requested,
                    model_used=last_call.model_used,
                    model_fallback_used=last_call.model_fallback_used,
                    model_fallback_reason=last_call.model_fallback_reason,
                )

            tool_actions = [
                action
                for action in decision.get("actions") or []
                if isinstance(action, dict) and str(action.get("type") or "") in spec.tools
            ]
            if not tool_actions:
                break

            for action in tool_actions:
                atype = str(action.get("type") or "")
                output = await self._run_tool(atype, action)
                helper_results.append(
                    {
                        "step": step,
                        "action": atype,
                        "request": action,
                        "output": output.get("output"),
                    }
                )

        if last_call is None:
            raise RuntimeError(f"Agent {spec.name!r} with tools did not produce a response.")
        parsed = last_call.parsed if isinstance(last_call.parsed, dict) else None
        return AgentCallResult(
            role=last_call.role,
            call_id=call_id,
            workspace=ctx.agents_dir / call_id,
            command=last_call.command,
            prompt=base_prompt,
            content=last_call.content,
            parsed=parsed,
            usage=last_call.usage,
            returncode=last_call.returncode,
            stdout=last_call.stdout,
            stderr=last_call.stderr,
            error=last_call.error,
            started_at=last_call.started_at,
            finished_at=last_call.finished_at,
            duration_seconds=last_call.duration_seconds,
            model_requested=last_call.model_requested,
            model_used=last_call.model_used,
            model_fallback_used=last_call.model_fallback_used,
            model_fallback_reason=last_call.model_fallback_reason,
        )

    async def search(self, query: str) -> dict[str, Any]:
        """Search for references or background material."""
        return await self._require_context().tools.search(query)

    async def browse(self, url: str) -> dict[str, Any]:
        """Fetch a web page."""
        return await self._require_context().tools.browse(url)

    async def code(
        self,
        instruction: str,
        *,
        language: str = "python",
        workspace: Path | None = None,
    ) -> dict[str, Any]:
        """Run a short snippet to verify a computation."""
        return await self._require_context().tools.code(
            instruction,
            language=language,
            workspace=workspace,
        )

    async def proof_writer(
        self,
        statement: str,
        prompt: str = "",
        *,
        task_id: str = "proof",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard proof writer to prove the given statement."""
        return await self._run_standard_task(
            "proof_writer",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
            statement=statement,
        )

    async def final_proof_writer(
        self,
        prompt: str = "",
        *,
        task_id: str = "final-proof",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard final proof writer to assemble a complete LaTeX proof document."""
        return await self._run_standard_task(
            "final_proof_writer",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
        )

    async def Graph_builder(
        self,
        prompt: str,
        *,
        task_id: str = "proof-dag",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard Graph_builder to produce a ProofFlow-style proof DAG."""
        return await self._run_standard_task(
            "Graph_builder",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
        )

    async def lemma_prover(
        self,
        prompt: str,
        *,
        task_id: str = "lemma",
        graph_id: str | None = None,
        node_id: str | None = None,
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard lemma prover to prove one Graph_builder node."""
        return await self._run_standard_task(
            "lemma_prover",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
            graph_id=graph_id,
            node_id=node_id,
        )

    async def summarizer(
        self,
        prompt: str,
        *,
        task_id: str = "summary",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard summarizer to synthesize prior work."""
        return await self._run_standard_task(
            "summarizer",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
        )

    async def critiquer(
        self,
        statement: str,
        proof: str,
        prompt: str = "",
        *,
        task_id: str = "critique",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard critiquer to review a proof of the given statement."""
        return await self._run_standard_task(
            "critiquer",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
            statement=statement,
            proof=proof,
        )

    async def final_critiquer(
        self,
        proof: str,
        prompt: str = "",
        *,
        task_id: str = "final-critique",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard final critiquer to review the assembled LaTeX proof document."""
        return await self._run_standard_task(
            "final_critiquer",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
            proof=proof,
        )

    async def counterexample_finder(
        self,
        statement: str,
        prompt: str = "",
        *,
        task_id: str = "counterexample",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard counterexample finder to stress-test a statement."""
        return await self._run_standard_task(
            "counterexample_finder",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
            statement=statement,
        )

    async def term_definer(
        self,
        term: str,
        prompt: str = "",
        *,
        task_id: str = "term",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard term definer to figure out what a term in the problem means."""
        return await self._run_standard_task(
            "term_definer",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
            term=term,
        )

    async def Aristotle_advisor(
        self,
        prompt: str = "",
        *,
        task_id: str = "aristotle-advice",
        call_id: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> AgentCallResult:
        """Ask the standard Aristotle advisor to read docs and recommend a call."""
        return await self._run_standard_task(
            "Aristotle_advisor",
            prompt,
            task_id=task_id,
            call_id=call_id,
            transcript=transcript or [],
        )

    # -- Main entry point ----------------------------------------------------

    async def solve(self, problem: Problem) -> dict[str, Any]:
        run_id = self.settings.run_id or f"{safe_id(problem.id)}-{uuid.uuid4().hex[:8]}"
        run_dir = self.settings.output_dir / "workflow_runs" / run_id
        agents_dir = run_dir / "agents"
        run_dir.mkdir(parents=True, exist_ok=True)
        trace_path = run_dir / "events.jsonl"
        self._ctx = self._init_run_context(
            run_id=run_id,
            run_dir=run_dir,
            agents_dir=agents_dir,
            trace_path=trace_path,
            problem=problem,
        )
        started_at = utc_now()

        workflow = self.settings.workflow_config
        append_jsonl(trace_path, {"type": "run.start", "run_id": run_id, "problem_id": problem.id, "at": started_at})
        write_json(
            run_dir / "input.json",
            {
                "id": problem.id,
                "statement": problem.statement,
                "metadata": problem.metadata,
                "codebase": str(self.settings.codebase_dir) if self.settings.codebase_dir else None,
                "save_code": self.settings.save_code,
            },
        )
        write_json(run_dir / "workflow_config.json", workflow)
        write_json(run_dir / "orchestrator_commands.json", {"commands": ORCHESTRATOR_COMMANDS})
        write_json(run_dir / "subagents.json", self._ctx.registry.to_json())

        return await self._run_loop(
            problem,
            transcript=[],
            start_step=1,
            started_at=started_at,
            reuse_decision=False,
        )

    async def resume(
        self,
        run_dir: Path,
        *,
        from_step: int,
        reuse_decision: bool = False,
    ) -> dict[str, Any]:
        """Continue an existing run starting at orchestrator step `from_step`.

        Prior transcript entries with step < `from_step` are kept. The loop then
        calls the orchestrator at `from_step` (or replays `orchestrator_step_NN.json`
        when `reuse_decision` is true) and continues until finish or budget limits.
        """
        run_dir = run_dir.expanduser().resolve()
        if from_step < 1:
            raise ValueError("from_step must be >= 1")
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run directory not found: {run_dir}")

        problem = load_problem_from_run(run_dir)
        transcript = load_transcript_before_step(run_dir, from_step)
        run_id = run_dir.name
        agents_dir = run_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        trace_path = run_dir / "events.jsonl"
        registry = self._load_registry_for_resume(run_dir, transcript)
        self._ctx = self._init_run_context(
            run_id=run_id,
            run_dir=run_dir,
            agents_dir=agents_dir,
            trace_path=trace_path,
            problem=problem,
            registry=registry,
        )
        self._restore_run_counters(self._ctx)
        self._ctx.aristotle_advice_seen = _transcript_saw_aristotle_advisor(transcript)
        started_at = utc_now()
        append_jsonl(
            trace_path,
            {
                "type": "run.resume",
                "run_id": run_id,
                "problem_id": problem.id,
                "from_step": from_step,
                "reuse_decision": reuse_decision,
                "kept_steps": [entry.get("step") for entry in transcript],
                "at": started_at,
            },
        )
        write_json(run_dir / "transcript.json", transcript)
        write_json(run_dir / "subagents.json", self._ctx.registry.to_json())
        return await self._run_loop(
            problem,
            transcript=transcript,
            start_step=from_step,
            started_at=started_at,
            reuse_decision=reuse_decision,
        )

    async def _run_loop(
        self,
        problem: Problem,
        *,
        transcript: list[dict[str, Any]],
        start_step: int,
        started_at: str,
        reuse_decision: bool,
    ) -> dict[str, Any]:
        ctx = self._require_context()
        run_dir = ctx.run_dir
        run_id = ctx.run_id
        trace_path = ctx.trace_path
        start = time.monotonic()
        final: dict[str, Any] | None = None
        max_steps = max(1, self.settings.max_steps)
        max_runtime_s = max(1, self.settings.max_runtime_minutes) * 60
        step = max(0, start_step - 1)
        first_step = True

        while step < max_steps and final is None and time.monotonic() - start < max_runtime_s:
            step += 1
            if first_step and reuse_decision:
                decision = _load_orchestrator_decision(run_dir, step)
                first_step = False
            else:
                decision = await self._orchestrator_step(problem, transcript, step, max_steps)
                first_step = False
            results, finish = await self._execute_actions(problem, transcript, decision.get("actions") or [], step)
            transcript.append(
                {
                    "step": step,
                    "thought": str(decision.get("thought") or ""),
                    "results": results,
                }
            )
            write_json(run_dir / "transcript.json", transcript)
            write_json(run_dir / "subagents.json", self._ctx.registry.to_json())
            if finish is not None:
                final = finish

        if final is None:
            step += 1
            decision = await self._orchestrator_step(problem, transcript, step, max_steps, force_finish=True)
            results, finish = await self._execute_actions(
                problem,
                transcript,
                decision.get("actions") or [],
                step,
            )
            transcript.append(
                {
                    "step": step,
                    "thought": str(decision.get("thought") or ""),
                    "results": results,
                }
            )
            write_json(run_dir / "transcript.json", transcript)
            final = finish or _fallback_finish(transcript)

        write_json(run_dir / "transcript.json", transcript)
        write_json(run_dir / "subagents.json", self._ctx.registry.to_json())

        solution_md_path = run_dir / "solution.md"
        solution_tex_path = run_dir / "solution.tex"
        solution_pdf_path = run_dir / "solution.pdf"
        solution_md_path.write_text(str(final.get("answer", "")).rstrip() + "\n", encoding="utf-8")
        tex_result = await self._write_solution_tex_artifact(
            problem,
            solution_md_path=solution_md_path,
            solution_tex_path=solution_tex_path,
        )
        compile_result = compile_tex_to_pdf(solution_tex_path)
        write_json(run_dir / "solution_pdf_compile.json", compile_result)
        compile_summary = _compile_result_summary(compile_result)

        total_usage = _sum_usage(run_dir)
        finished_at = utc_now()
        result = {
            "run_id": run_id,
            "problem_id": problem.id,
            "problem_statement": problem.statement,
            "status": "done" if final.get("answer") else "partial",
            "answer": final.get("answer", ""),
            "confidence": final.get("confidence", 0.0),
            "reasoning_summary": final.get("reasoning_summary", ""),
            "caveats": final.get("caveats", []),
            "sources": final.get("sources", []),
            "steps": step,
            "subagents": self._ctx.registry.to_json(),
            "transcript": transcript,
            "usage": total_usage.to_json(),
            "run_dir": str(run_dir),
            "solution_md_path": str(solution_md_path),
            "solution_tex_path": str(solution_tex_path),
            "solution_pdf_path": str(solution_pdf_path),
            "solution_tex": tex_result,
            "solution_pdf_compile": compile_summary,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": time.monotonic() - start,
        }
        write_json(run_dir / "final.json", result)
        append_jsonl(trace_path, {"type": "run.end", "run_id": run_id, "status": result["status"], "at": finished_at})
        self._ctx = None
        return result

    async def _write_solution_tex_artifact(
        self,
        problem: Problem,
        *,
        solution_md_path: Path,
        solution_tex_path: Path,
    ) -> dict[str, Any]:
        ctx = self._require_context()
        solution_markdown = solution_md_path.read_text(encoding="utf-8")
        call_id = self._next_call_id("tex-writer")
        call = await self._call(
            role="tex_writer",
            call_id=call_id,
            prompt=agent_prompt(_TEX_WRITER_SYSTEM_PROMPT, tex_artifact_prompt(problem, solution_markdown)),
            workspace=ctx.agents_dir / call_id,
            schema_name="tex_artifact.schema.json",
            trace_path=ctx.trace_path,
        )

        parsed = call.parsed if isinstance(call.parsed, dict) else {}
        latex_document = parsed.get("latex_document")
        fallback_reason = None
        if not isinstance(latex_document, str) or not _looks_like_latex_document(latex_document):
            fallback_reason = "tex_writer did not return a complete LaTeX document; used local fallback wrapper."
            latex_document = fallback_latex_document(problem.statement, solution_markdown)

        solution_tex_path.write_text(latex_document.rstrip() + "\n", encoding="utf-8")
        artifact = {
            "ok": fallback_reason is None and call.ok,
            "call_id": call.call_id,
            "fallback_used": fallback_reason is not None,
            "fallback_reason": fallback_reason,
            "confidence": parsed.get("confidence"),
            "notes": parsed.get("notes") if isinstance(parsed.get("notes"), list) else [],
            "tex_path": str(solution_tex_path),
            "error": call.error,
        }
        write_json(solution_tex_path.with_name("solution_tex_artifact.json"), artifact)
        append_jsonl(ctx.trace_path, {"type": "tex_artifact.write", **artifact, "at": utc_now()})
        return artifact

    # -- Decision loop -------------------------------------------------------

    async def _orchestrator_step(
        self,
        problem: Problem,
        transcript: list[dict[str, Any]],
        step: int,
        max_steps: int,
        *,
        force_finish: bool = False,
    ) -> dict[str, Any]:
        ctx = self._require_context()
        call_id = f"orchestrator-{step:02d}"
        prompt = orchestrator_prompt(
            problem,
            transcript,
            ctx.registry.to_public_list(),
            step,
            max_steps,
            _role_description(self.settings.workflow_config, "orchestrator"),
            force_finish=force_finish,
        )
        call = await self._call(
            role="orchestrator",
            call_id=call_id,
            prompt=prompt,
            workspace=ctx.agents_dir / call_id,
            schema_name="orchestrator.schema.json",
            trace_path=ctx.trace_path,
            reasoning_effort=ORCHESTRATOR_REASONING_EFFORT,
        )
        if (
            not call.ok
            and _is_input_too_large(call)
            and len(transcript) > ORCHESTRATOR_RECENT_STEPS_ON_RETRY
        ):
            compacted_transcript, compaction = _compact_orchestrator_transcript(
                transcript,
                run_dir=ctx.run_dir,
                keep_recent_steps=ORCHESTRATOR_RECENT_STEPS_ON_RETRY,
                max_older_chars=ORCHESTRATOR_OLDER_CONTEXT_MAX_CHARS,
            )
            retry_prompt = orchestrator_prompt(
                problem,
                compacted_transcript,
                ctx.registry.to_public_list(),
                step,
                max_steps,
                _role_description(self.settings.workflow_config, "orchestrator"),
                force_finish=force_finish,
            )
            if len(retry_prompt) < len(prompt):
                retry_call_id = f"{call_id}-context-retry"
                append_jsonl(
                    ctx.trace_path,
                    {
                        "type": "orchestrator.context_compacted",
                        "step": step,
                        "reason": "input_too_large",
                        "original_call_id": call_id,
                        "retry_call_id": retry_call_id,
                        "original_prompt_chars": len(prompt),
                        "retry_prompt_chars": len(retry_prompt),
                        **compaction,
                        "at": utc_now(),
                    },
                )
                call = await self._call(
                    role="orchestrator",
                    call_id=retry_call_id,
                    prompt=retry_prompt,
                    workspace=ctx.agents_dir / retry_call_id,
                    schema_name="orchestrator.schema.json",
                    trace_path=ctx.trace_path,
                    reasoning_effort=ORCHESTRATOR_REASONING_EFFORT,
                )
        if not call.ok:
            detail = _agent_call_failure_detail(call)
            raise RuntimeError(f"Orchestrator call {call.call_id} failed: {detail}")
        decision = _valid_orchestrator(call.parsed)
        write_json(ctx.run_dir / f"orchestrator_step_{step:02d}.json", decision)
        return decision

    async def _execute_actions(
        self,
        problem: Problem,
        transcript: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        step: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        results: list[dict[str, Any]] = []
        finish: dict[str, Any] | None = None
        agent_actions: list[tuple[str, dict[str, Any]]] = []

        for action in actions:
            if not isinstance(action, dict):
                continue
            atype = str(action.get("type") or "")
            if atype == "create_subagent":
                spec = self.create_subagent(
                    str(action.get("name") or "subagent"),
                    str(action.get("system_prompt") or ""),
                )
                results.append({"action": "create_subagent", "name": spec.name})
            elif atype == "assign_task":
                agent_actions.append((atype, action))
            elif atype in _STANDARD_TASK_ACTIONS:
                agent_actions.append((atype, action))
            elif atype in _TOOL_ACTIONS:
                results.append(await self._run_tool(atype, action))
            elif atype == "finish":
                finish = _valid_finish(action)
                results.append({"action": "finish", "answer_preview": finish["answer"][:200]})

        if agent_actions:
            results.extend(await self._run_agent_actions(problem, transcript, agent_actions, step))

        return results, finish

    async def _run_tool(self, atype: str, action: dict[str, Any]) -> dict[str, Any]:
        if atype == "search":
            output = await self.search(str(action.get("query") or ""))
        elif atype == "browse":
            output = await self.browse(str(action.get("url") or ""))
        elif atype == "code":
            output = await self.code(
                str(action.get("instruction") or ""),
                language=str(action.get("language") or "python"),
            )
        else:
            ctx = self._require_context()
            if not ctx.aristotle_advice_seen:
                return {
                    "action": atype,
                    "output": {
                        "ok": False,
                        "error": (
                            "Run Aristotle_advisor in an earlier step before calling aristotle. "
                            "The advisor must read current Aristotle docs and recommend the call."
                        ),
                    },
                }
            output = await ctx.tools.aristotle(
                str(action.get("prompt") or ""),
                mode=str(action.get("mode") or "submit"),
                project_dir=_optional_path(action.get("project_dir")),
                source_path=_optional_path(action.get("source_path")),
                destination=_optional_path(action.get("destination")),
                wait=bool(action.get("wait") if action.get("wait") is not None else True),
            )
        return {"action": atype, "output": _trim_tool_output(output)}

    async def _run_agent_actions(
        self,
        problem: Problem,
        transcript: list[dict[str, Any]],
        agent_actions: list[tuple[str, dict[str, Any]]],
        step: int,
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(max(1, self.settings.max_parallel))

        async def run_one(index: int, atype: str, action: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                if atype == "assign_task":
                    return await self._run_assignment_action(problem, action, step, index)
                return await self._run_standard_task_action(atype, action, step, index, transcript)

        return list(
            await asyncio.gather(
                *(run_one(i, atype, action) for i, (atype, action) in enumerate(agent_actions, start=1))
            )
        )

    async def _run_assignment_action(
        self,
        problem: Problem,
        action: dict[str, Any],
        step: int,
        index: int,
    ) -> dict[str, Any]:
        agent = str(action.get("agent") or "")
        task_id = safe_id(str(action.get("task_id") or f"task-{index}"), fallback=f"task-{index}")
        call_id = f"assign-{step:02d}-{index:02d}-{task_id}"
        task_prompt = subagent_task_prompt(problem, agent, task_id, str(action.get("prompt") or ""))
        model = _optional_text(action.get("model"))
        reasoning_effort = _optional_text(action.get("reasoning_effort"))
        try:
            call = await self.assign_task(
                agent,
                task_prompt,
                call_id=call_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except KeyError:
            return {
                "action": "assign_task",
                "agent": agent,
                "task_id": task_id,
                "error": f"Unknown subagent {agent!r}. Create it before assigning work.",
            }
        return _agent_result_entry(
            action="assign_task",
            agent=agent,
            task_id=task_id,
            call_id=call_id,
            call=call,
        )

    async def _run_standard_task_action(
        self,
        atype: str,
        action: dict[str, Any],
        step: int,
        index: int,
        transcript: list[dict[str, Any]],
    ) -> dict[str, Any]:
        task_id = safe_id(str(action.get("task_id") or atype), fallback=atype)
        spec_info = _STANDARD_TASK_SPECS[atype]
        agent_name = str(spec_info["name"])
        call_id = f"{agent_name}-{step:02d}-{index:02d}-{task_id}"
        node_id = str(action.get("node_id") or "").strip() or None
        graph_id = str(action.get("graph_id") or "").strip() or None
        statement = str(action.get("statement") or "").strip() or None
        proof = str(action.get("proof") or "").strip() or None
        term = str(action.get("term") or "").strip() or None
        model = _optional_text(action.get("model"))
        reasoning_effort = _optional_text(action.get("reasoning_effort"))
        if atype == "proof_writer" and not statement:
            return {
                "action": atype,
                "task_id": task_id,
                "error": "proof_writer requires a non-empty statement to prove.",
            }
        if atype == "critiquer":
            if not statement:
                return {
                    "action": atype,
                    "task_id": task_id,
                    "error": "critiquer requires a non-empty statement to critique.",
                }
            if not proof:
                return {
                    "action": atype,
                    "task_id": task_id,
                    "error": "critiquer requires a non-empty proof to critique.",
                }
        if atype == "final_critiquer" and not proof:
            return {
                "action": atype,
                "task_id": task_id,
                "error": "final_critiquer requires a non-empty LaTeX proof document to critique.",
            }
        if atype == "counterexample_finder" and not statement:
            return {
                "action": atype,
                "task_id": task_id,
                "error": "counterexample_finder requires a non-empty statement to check.",
            }
        if atype == "term_definer" and not term:
            return {
                "action": atype,
                "task_id": task_id,
                "error": "term_definer requires a non-empty term to define.",
            }
        try:
            call = await self._run_standard_task(
                atype,
                str(action.get("prompt") or ""),
                task_id=task_id,
                call_id=call_id,
                transcript=transcript,
                graph_id=graph_id,
                node_id=node_id,
                statement=statement,
                proof=proof,
                term=term,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except KeyError as exc:
            return {"action": atype, "task_id": task_id, "error": str(exc)}
        output = call.parsed if isinstance(call.parsed, dict) else {"content": call.content}
        if atype == "Aristotle_advisor":
            self._require_context().aristotle_advice_seen = True
        if node_id and isinstance(output, dict) and "node_id" not in output:
            output = {**output, "node_id": node_id}
        if graph_id and isinstance(output, dict) and "graph_id" not in output:
            output = {**output, "graph_id": graph_id}
        return _agent_result_entry(
            action=atype,
            agent=agent_name,
            task_id=task_id,
            call_id=call_id,
            call=call,
            output=output,
        )

    async def _run_standard_task(
        self,
        atype: str,
        prompt: str,
        *,
        task_id: str,
        call_id: str | None,
        transcript: list[dict[str, Any]],
        node_id: str | None = None,
        graph_id: str | None = None,
        statement: str | None = None,
        proof: str | None = None,
        term: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentCallResult:
        spec = self._ensure_standard_subagent(atype)
        normalized_task_id = safe_id(task_id, fallback=atype)
        task_prompt = self._standard_task_prompt(
            atype,
            spec,
            normalized_task_id,
            prompt,
            transcript,
            node_id=node_id,
            graph_id=graph_id,
            statement=statement,
            proof=proof,
            term=term,
        )
        call = await self.assign_task(
            spec,
            task_prompt,
            call_id=call_id or self._next_call_id(spec.name),
            schema_name=spec.schema_name,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if node_id and isinstance(call.parsed, dict) and "node_id" not in call.parsed:
            return AgentCallResult(
                role=call.role,
                call_id=call.call_id,
                workspace=call.workspace,
                command=call.command,
                prompt=call.prompt,
                content=call.content,
                parsed={**call.parsed, "node_id": node_id},
                usage=call.usage,
                returncode=call.returncode,
                stdout=call.stdout,
                stderr=call.stderr,
                error=call.error,
                started_at=call.started_at,
                finished_at=call.finished_at,
                duration_seconds=call.duration_seconds,
                model_requested=call.model_requested,
                model_used=call.model_used,
                model_fallback_used=call.model_fallback_used,
                model_fallback_reason=call.model_fallback_reason,
            )
        return call

    def _ensure_standard_subagent(self, atype: str) -> SubagentSpec:
        ctx = self._require_context()
        spec_info = _STANDARD_TASK_SPECS.get(atype)
        if spec_info is None:
            raise KeyError(f"Unknown standard task action: {atype}")
        name = str(spec_info["name"])
        existing = ctx.registry.get(name)
        if existing is not None:
            return existing
        return self.create_subagent(
            name,
            str(spec_info["system_prompt"]),
            role=str(spec_info["role"]),
            schema_name=str(spec_info["schema_name"]),
            tools=tuple(spec_info.get("tools") or ()),
        )

    def _standard_task_prompt(
        self,
        atype: str,
        spec: SubagentSpec,
        task_id: str,
        prompt: str,
        transcript: list[dict[str, Any]],
        *,
        node_id: str | None = None,
        graph_id: str | None = None,
        statement: str | None = None,
        proof: str | None = None,
        term: str | None = None,
    ) -> str:
        ctx = self._require_context()
        if atype == "lemma_prover":
            return lemma_prover_task_prompt(
                ctx.problem,
                task_id,
                prompt,
                transcript,
                graph_id=graph_id,
                node_id=node_id,
            )
        if atype == "proof_writer":
            if not statement:
                raise ValueError("proof_writer requires a non-empty statement to prove.")
            return proof_writer_task_prompt(
                ctx.problem,
                task_id,
                prompt,
                transcript,
                statement=statement,
            )
        if atype == "Graph_builder":
            return graph_builder_task_prompt(ctx.problem, task_id, prompt, transcript)
        if atype == "summarizer":
            return summarizer_task_prompt(ctx.problem, task_id, prompt, transcript)
        if atype == "final_proof_writer":
            return final_proof_writer_task_prompt(ctx.problem, task_id, prompt, transcript)
        if atype == "critiquer":
            if not statement:
                raise ValueError("critiquer requires a non-empty statement to critique.")
            if not proof:
                raise ValueError("critiquer requires a non-empty proof to critique.")
            return critiquer_task_prompt(
                ctx.problem,
                task_id,
                prompt,
                transcript,
                statement=statement,
                proof=proof or "",
            )
        if atype == "final_critiquer":
            if not proof:
                raise ValueError("final_critiquer requires a non-empty LaTeX proof document to critique.")
            return final_critiquer_task_prompt(
                ctx.problem,
                task_id,
                prompt,
                transcript,
                proof=proof,
            )
        if atype == "counterexample_finder":
            if not statement:
                raise ValueError("counterexample_finder requires a non-empty statement to check.")
            return counterexample_finder_task_prompt(
                ctx.problem,
                task_id,
                prompt,
                transcript,
                statement=statement,
            )
        if atype == "term_definer":
            if not term:
                raise ValueError("term_definer requires a non-empty term to define.")
            return term_definer_task_prompt(
                ctx.problem,
                task_id,
                prompt,
                transcript,
                term=term,
            )
        if atype == "Aristotle_advisor":
            return aristotle_advisor_task_prompt(ctx.problem, task_id, prompt, transcript)
        raise KeyError(f"Unknown standard task action: {atype}")

    # -- Internals -----------------------------------------------------------

    def _init_run_context(
        self,
        *,
        run_id: str,
        run_dir: Path,
        agents_dir: Path,
        trace_path: Path,
        problem: Problem,
        registry: SubagentRegistry | None = None,
    ) -> _RunContext:
        if registry is None:
            registry = SubagentRegistry(persist_path=run_dir / "subagents.json")
        else:
            registry.persist_path = run_dir / "subagents.json"
        tools_mock = self.settings.tools_mock
        if tools_mock is None:
            tools_mock = isinstance(self.backend, MockBackend)
        return _RunContext(
            run_id=run_id,
            run_dir=run_dir,
            agents_dir=agents_dir,
            trace_path=trace_path,
            problem=problem,
            registry=registry,
            tools=OrchestratorTools(
                run_dir=run_dir,
                trace_path=trace_path,
                mock=tools_mock,
                aristotle_enabled=self.settings.aristotle_enabled,
                aristotle_executable=self.settings.aristotle_executable,
                aristotle_timeout_s=self.settings.aristotle_timeout_s,
                codebase_dir=self.settings.codebase_dir,
                save_code=self.settings.save_code,
            ),
        )

    def _load_registry_for_resume(
        self,
        run_dir: Path,
        transcript: list[dict[str, Any]],
    ) -> SubagentRegistry:
        path = run_dir / "subagents.json"
        if path.exists():
            return SubagentRegistry.from_file(path)
        registry = SubagentRegistry(persist_path=path)
        for entry in transcript:
            for result in entry.get("results") or []:
                if not isinstance(result, dict) or result.get("action") != "create_subagent":
                    continue
                name = str(result.get("name") or "").strip()
                if name and registry.get(name) is None:
                    registry.create_subagent(name, f"Resumed subagent {name}.")
        return registry

    def _restore_run_counters(self, ctx: _RunContext) -> None:
        ctx.call_counter = _max_suffix_counter(ctx.agents_dir)
        ctx.tools.set_call_counter(_max_tool_counter(ctx.run_dir / "tools"))

    def _require_context(self) -> _RunContext:
        if self._ctx is None:
            raise RuntimeError("Orchestrator commands require an active solve() run.")
        return self._ctx

    def _next_call_id(self, prefix: str) -> str:
        ctx = self._require_context()
        ctx.call_counter += 1
        return f"{safe_id(prefix)}-{ctx.call_counter:03d}"

    async def _call(
        self,
        *,
        role: str,
        call_id: str,
        prompt: str,
        workspace: Path,
        schema_name: str,
        trace_path: Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_s: int | None = None,
    ) -> AgentCallResult:
        if self.settings.codebase_dir is not None:
            saved_code_dir = self._require_context().tools.saved_code_dir
            code_note = (
                f" Code-tool scripts are preserved under {saved_code_dir}."
                if saved_code_dir is not None
                else ""
            )
            prompt = (
                f"\n<local_codebase path=\"{self.settings.codebase_dir}\">\n"
                "This repository is available for inspection and counterexample searches. "
                "Read, run, and EDIT its code when that helps build or improve a counterexample search. "
                "Keep edits scoped to that search and report the files changed. The code tool runs with this "
                f"directory as its working directory.{code_note}\n"
                "</local_codebase>\n\n"
                + prompt
            )
        append_jsonl(trace_path, {"type": "agent.start", "role": role, "call_id": call_id, "at": utc_now()})
        result = await self.backend.run_agent(
            role=role,
            call_id=call_id,
            prompt=prompt,
            workspace=workspace,
            schema_path=self.settings.schema_dir / schema_name,
            timeout_s=timeout_s or self.settings.agent_timeout_s,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        append_jsonl(trace_path, {"type": "agent.end", **result.to_trace_json(), "at": utc_now()})
        return result


def load_workflow_config(path: Path) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Workflow config must be a JSON object: {path}")
    return data


def settings_from_config(output_dir: Path, workflow_config: dict[str, Any], **overrides: Any) -> SwarmSettings:
    max_parallel = int(overrides.get("max_parallel") or workflow_config.get("max_parallel") or 30)
    max_steps = int(overrides.get("max_steps") or workflow_config.get("max_steps") or 40)
    max_runtime_minutes = int(
        overrides.get("max_runtime_minutes") or workflow_config.get("max_runtime_minutes") or 600
    )
    agent_timeout_s = int(overrides.get("agent_timeout_s") or workflow_config.get("agent_timeout_s") or 3000)
    assign_task_timeout_s = int(
        overrides.get("assign_task_timeout_s") or workflow_config.get("assign_task_timeout_s") or 6000
    )
    aristotle_timeout_s = int(
        overrides.get("aristotle_timeout_s") or workflow_config.get("aristotle_timeout_s") or 8 * 60 * 60
    )
    return SwarmSettings(
        output_dir=output_dir,
        workflow_config=workflow_config,
        max_parallel=max_parallel,
        max_steps=max_steps,
        max_runtime_minutes=max_runtime_minutes,
        agent_timeout_s=agent_timeout_s,
        assign_task_timeout_s=assign_task_timeout_s,
        run_id=overrides.get("run_id"),
        aristotle_enabled=bool(overrides.get("aristotle_enabled") or workflow_config.get("aristotle_enabled")),
        aristotle_executable=str(
            overrides.get("aristotle_executable") or workflow_config.get("aristotle_executable") or "aristotle"
        ),
        aristotle_timeout_s=aristotle_timeout_s,
        codebase_dir=overrides.get("codebase_dir"),
        save_code=bool(overrides.get("save_code") or workflow_config.get("save_code")),
    )


def load_problem_from_run(run_dir: Path) -> Problem:
    path = run_dir / "input.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing input.json in run directory: {run_dir}")
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"input.json must be a JSON object: {path}")
    statement = str(data.get("statement") or "").strip()
    if not statement:
        raise ValueError(f"input.json has an empty statement: {path}")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return Problem(id=safe_id(str(data.get("id") or run_dir.name)), statement=statement, metadata=metadata)


def load_transcript_before_step(run_dir: Path, from_step: int) -> list[dict[str, Any]]:
    """Load transcript entries with step < from_step, rebuilding from artifacts if needed."""
    if from_step <= 1:
        return []
    path = run_dir / "transcript.json"
    if path.exists():
        raw = read_json(path)
        if not isinstance(raw, list):
            raise ValueError(f"transcript.json must be a JSON array: {path}")
        kept = [
            entry
            for entry in raw
            if isinstance(entry, dict) and int(entry.get("step") or 0) < from_step
        ]
        _validate_transcript_prefix(kept, from_step, source=str(path))
        return kept
    rebuilt = rebuild_transcript_before_step(run_dir, from_step)
    _validate_transcript_prefix(rebuilt, from_step, source=f"rebuilt from {run_dir}")
    return rebuilt


def rebuild_transcript_before_step(run_dir: Path, from_step: int) -> list[dict[str, Any]]:
    """Best-effort transcript rebuild from orchestrator_step_NN.json + agent/tool artifacts."""
    transcript: list[dict[str, Any]] = []
    for step in range(1, from_step):
        decision_path = run_dir / f"orchestrator_step_{step:02d}.json"
        if not decision_path.exists():
            raise FileNotFoundError(
                f"Cannot rebuild transcript through step {step - 1}: missing {decision_path.name}. "
                "Need transcript.json or completed orchestrator_step files for prior steps."
            )
        decision = _valid_orchestrator(read_json(decision_path))
        results = _rebuild_step_results(run_dir, decision.get("actions") or [], step)
        transcript.append(
            {
                "step": step,
                "thought": str(decision.get("thought") or ""),
                "results": results,
            }
        )
    return transcript


def _validate_transcript_prefix(transcript: list[dict[str, Any]], from_step: int, *, source: str) -> None:
    expected = list(range(1, from_step))
    found = sorted(int(entry.get("step") or 0) for entry in transcript)
    if found != expected:
        raise ValueError(
            f"Resume from step {from_step} needs completed steps {expected}; "
            f"found {found} in {source}."
        )


def _load_orchestrator_decision(run_dir: Path, step: int) -> dict[str, Any]:
    path = run_dir / f"orchestrator_step_{step:02d}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"--reuse-decision requires {path.name} in the run directory, but it was not found."
        )
    decision = _valid_orchestrator(read_json(path))
    write_json(path, decision)
    return decision


def _rebuild_step_results(run_dir: Path, actions: list[dict[str, Any]], step: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    agent_index = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        atype = str(action.get("type") or "")
        if atype == "create_subagent":
            name = safe_id(str(action.get("name") or "subagent"), fallback="subagent")
            results.append({"action": "create_subagent", "name": name})
        elif atype == "assign_task":
            agent_index += 1
            task_id = safe_id(str(action.get("task_id") or f"task-{agent_index}"), fallback=f"task-{agent_index}")
            call_id = f"assign-{step:02d}-{agent_index:02d}-{task_id}"
            results.append(_rebuild_agent_result("assign_task", call_id, run_dir, agent=str(action.get("agent") or ""), task_id=task_id))
        elif atype in _STANDARD_TASK_ACTIONS:
            agent_index += 1
            spec_info = _STANDARD_TASK_SPECS[atype]
            agent_name = str(spec_info["name"])
            task_id = safe_id(str(action.get("task_id") or atype), fallback=atype)
            call_id = f"{agent_name}-{step:02d}-{agent_index:02d}-{task_id}"
            result = _rebuild_agent_result(atype, call_id, run_dir, agent=agent_name, task_id=task_id)
            results.append(result)
        elif atype in _TOOL_ACTIONS:
            results.append(_rebuild_tool_result(atype, action, run_dir))
        elif atype == "finish":
            finish = _valid_finish(action)
            results.append({"action": "finish", "answer_preview": finish["answer"][:200]})
    return results


def _rebuild_agent_result(
    action: str,
    call_id: str,
    run_dir: Path,
    *,
    agent: str,
    task_id: str,
) -> dict[str, Any]:
    parsed_path = run_dir / "agents" / call_id / "parsed.json"
    if not parsed_path.exists():
        raise FileNotFoundError(
            f"Cannot rebuild step result for {call_id}: missing {parsed_path}. "
            "Wait for that agent to finish, or resume from an earlier completed step."
        )
    parsed = read_json(parsed_path)
    output = parsed if isinstance(parsed, dict) else {"content": parsed}
    return {
        "action": action,
        "agent": agent,
        "task_id": task_id,
        "call_id": call_id,
        "output": output,
    }


def _rebuild_tool_result(atype: str, action: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    tools_dir = run_dir / "tools"
    if not tools_dir.is_dir():
        raise FileNotFoundError(
            f"Cannot rebuild {atype} tool result: missing tools/ directory under {run_dir}."
        )
    needle_key = {
        "search": "query",
        "browse": "url",
        "code": "instruction",
        "aristotle": "prompt",
    }.get(atype)
    needle = str(action.get(needle_key) or "") if needle_key else ""
    matches: list[Path] = []
    for path in sorted(tools_dir.glob(f"{atype}-*.json")):
        try:
            artifact = read_json(path)
        except Exception:
            continue
        if not isinstance(artifact, dict):
            continue
        if needle and str(artifact.get(needle_key) or "") != needle:
            continue
        matches.append(path)
    if not matches:
        raise FileNotFoundError(
            f"Cannot rebuild {atype} tool result for {needle_key}={needle!r}: no matching tools/{atype}-*.json."
        )
    artifact = read_json(matches[-1])
    output = artifact.get("result") if isinstance(artifact, dict) else None
    if not isinstance(output, dict):
        output = artifact if isinstance(artifact, dict) else {"content": artifact}
    return {"action": atype, "output": _trim_tool_output(output)}


def _transcript_saw_aristotle_advisor(transcript: list[dict[str, Any]]) -> bool:
    for entry in transcript:
        for result in entry.get("results") or []:
            if isinstance(result, dict) and result.get("action") == "Aristotle_advisor":
                return True
    return False


def _max_suffix_counter(agents_dir: Path) -> int:
    if not agents_dir.is_dir():
        return 0
    max_n = 0
    for path in agents_dir.iterdir():
        if not path.is_dir():
            continue
        match = re.search(r"-(\d{3})$", path.name)
        if match:
            max_n = max(max_n, int(match.group(1)))
    return max_n


def _max_tool_counter(tools_dir: Path) -> int:
    if not tools_dir.is_dir():
        return 0
    max_n = 0
    for path in tools_dir.glob("*.json"):
        match = re.search(r"-(\d+)\.json$", path.name)
        if match:
            max_n = max(max_n, int(match.group(1)))
    return max_n


def _role_description(workflow: dict[str, Any], key: str) -> str:
    raw = workflow.get(key)
    if isinstance(raw, dict):
        return str(raw.get("description") or "")
    return ""


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else None


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _agent_result_entry(
    *,
    action: str,
    agent: str,
    task_id: str,
    call_id: str,
    call: AgentCallResult,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a transcript result that includes which model actually ran the call."""
    entry: dict[str, Any] = {
        "action": action,
        "agent": agent,
        "task_id": task_id,
        "call_id": call_id,
        "output": output if output is not None else (
            call.parsed if isinstance(call.parsed, dict) else {"content": call.content}
        ),
        **call.model_metadata(),
    }
    if not call.ok:
        entry["ok"] = False
        if call.error:
            entry["error"] = call.error
    return entry


def _valid_orchestrator(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Orchestrator response must be a JSON object.")
    actions = raw.get("actions")
    if not isinstance(actions, list):
        raise ValueError("Orchestrator response must contain an actions list.")
    clean_actions = [action for action in actions if isinstance(action, dict) and action.get("type")]
    thought = str(raw.get("thought") or "")
    if not thought.strip() and not clean_actions:
        raise ValueError("Orchestrator response cannot be an empty decision.")
    return {"thought": thought, "actions": clean_actions}


def _is_input_too_large(call: AgentCallResult) -> bool:
    text = "\n".join(
        part
        for part in (call.error, call.stderr, call.stdout, call.content)
        if isinstance(part, str) and part
    ).lower()
    return "input_too_large" in text or (
        "input exceeds the maximum length" in text or "input exceeds maximum length" in text
    )


def _agent_call_failure_detail(call: AgentCallResult) -> str:
    if _is_input_too_large(call) and call.stderr.strip():
        return call.stderr.strip()
    return call.error or f"Codex worker exited with status {call.returncode}."


def _compact_orchestrator_transcript(
    transcript: list[dict[str, Any]],
    *,
    run_dir: Path,
    keep_recent_steps: int = ORCHESTRATOR_RECENT_STEPS_ON_RETRY,
    max_older_chars: int = ORCHESTRATOR_OLDER_CONTEXT_MAX_CHARS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Condense older steps while retaining the newest orchestrator steps verbatim."""

    recent_count = min(max(0, keep_recent_steps), len(transcript))
    split_at = len(transcript) - recent_count
    older = transcript[:split_at]
    recent = transcript[split_at:]
    if not older:
        return list(transcript), {
            "older_steps_condensed": 0,
            "recent_steps_preserved": recent_count,
            "older_context_chars_before": 0,
            "older_context_chars_after": 0,
        }

    max_older_chars = max(4_000, max_older_chars)
    per_step_budget = max(800, max_older_chars // len(older) - 32)
    compacted_older = [
        _compact_old_orchestrator_step(entry, max_chars=per_step_budget, run_dir=run_dir)
        for entry in older
    ]
    if _context_chars(compacted_older) > max_older_chars:
        compacted_older = [
            _index_old_orchestrator_step(entry, max_chars=per_step_budget, run_dir=run_dir)
            for entry in older
        ]

    older_step_numbers = [entry.get("step") for entry in older]
    recent_step_numbers = [entry.get("step") for entry in recent]
    marker = {
        "context_compaction": {
            "reason": "The original orchestrator prompt exceeded the backend input limit.",
            "older_steps_condensed": older_step_numbers,
            "recent_steps_preserved_verbatim": recent_step_numbers,
            "full_transcript_path": str(run_dir / "transcript.json"),
            "note": (
                "Older agent results retain their identifiers, semantic summary fields, and bounded "
                "proof excerpts. Consult the full transcript or listed agent artifact when exact older "
                "details are necessary."
            ),
        }
    }
    compacted = [marker, *compacted_older, *recent]
    return compacted, {
        "older_steps_condensed": len(older),
        "recent_steps_preserved": len(recent),
        "older_context_chars_before": _context_chars(older),
        "older_context_chars_after": _context_chars(compacted_older),
        "full_transcript_path": str(run_dir / "transcript.json"),
    }


def _compact_old_orchestrator_step(
    entry: dict[str, Any],
    *,
    max_chars: int,
    run_dir: Path,
) -> dict[str, Any]:
    if _context_chars(entry) <= max_chars:
        return entry

    results = [result for result in entry.get("results") or [] if isinstance(result, dict)]
    thought_budget = min(1_500, max(200, max_chars // 5))
    result_budget = max(250, (max_chars - thought_budget - 300) // max(1, len(results)))
    compacted = {
        "step": entry.get("step"),
        "thought": _middle_excerpt(str(entry.get("thought") or ""), thought_budget),
        "results": [
            _compact_old_result(result, max_chars=result_budget, run_dir=run_dir)
            for result in results
        ],
        "older_context_condensed": True,
    }
    if _context_chars(compacted) <= max_chars:
        return compacted
    return _index_old_orchestrator_step(entry, max_chars=max_chars, run_dir=run_dir)


def _compact_old_result(
    result: dict[str, Any],
    *,
    max_chars: int,
    run_dir: Path,
) -> dict[str, Any]:
    if _context_chars(result) <= max_chars:
        return result

    metadata_keys = (
        "action",
        "name",
        "agent",
        "task_id",
        "call_id",
        "graph_id",
        "node_id",
        "ok",
        "error",
        "model_requested",
        "model_used",
        "model_fallback_used",
        "model_fallback_reason",
    )
    compacted = {key: result[key] for key in metadata_keys if key in result}
    call_id = str(result.get("call_id") or "").strip()
    if call_id:
        compacted["full_output_artifact"] = str(run_dir / "agents" / call_id / "parsed.json")

    output = result.get("output")
    if output is None:
        return compacted
    output_budget = max(100, max_chars - _context_chars(compacted) - 80)
    compacted["output_summary"] = _compact_agent_output(output, max_chars=output_budget)
    if _context_chars(compacted) <= max_chars:
        return compacted

    compacted.pop("output_summary", None)
    remaining = max(40, max_chars - _context_chars(compacted) - 40)
    compacted["output_excerpt"] = _middle_excerpt(_json_text(output), remaining)
    return compacted


def _compact_agent_output(output: Any, *, max_chars: int) -> Any:
    if _context_chars(output) <= max_chars:
        return output
    if not isinstance(output, dict):
        return _middle_excerpt(_json_text(output), max_chars)

    summary_keys = (
        "task_id",
        "title",
        "approach",
        "confidence",
        "assumptions",
        "approved",
        "score",
        "strongest_task_id",
        "summary",
        "reasoning_summary",
        "caveats",
        "sources",
        "issues",
        "revision_tasks",
    )
    summary: dict[str, Any] = {}
    for key in summary_keys:
        if key not in output:
            continue
        remaining = max_chars - _context_chars(summary) - len(key) - 12
        if remaining < 40:
            break
        candidate = _compact_context_value(output[key], min(1_200, remaining))
        trial = {**summary, key: candidate}
        if _context_chars(trial) <= max_chars:
            summary = trial

    for key in ("answer_fragment", "latex_document", "answer", "content", "graphs", "nodes"):
        if key not in output:
            continue
        remaining = max_chars - _context_chars(summary) - len(key) - 24
        if remaining < 80:
            break
        trial = {
            **summary,
            f"{key}_excerpt": _middle_excerpt(_json_text(output[key]), remaining),
        }
        if _context_chars(trial) <= max_chars:
            summary = trial
        break

    if summary:
        return summary
    return _middle_excerpt(_json_text(output), max_chars)


def _index_old_orchestrator_step(
    entry: dict[str, Any],
    *,
    max_chars: int,
    run_dir: Path,
) -> dict[str, Any]:
    identities = []
    for result in entry.get("results") or []:
        if not isinstance(result, dict):
            continue
        identity = {
            key: result[key]
            for key in ("action", "name", "agent", "task_id", "call_id", "ok", "error")
            if key in result
        }
        call_id = str(result.get("call_id") or "").strip()
        if call_id:
            identity["full_output_artifact"] = str(run_dir / "agents" / call_id / "parsed.json")
        identities.append(identity)

    indexed = {
        "step": entry.get("step"),
        "thought": _middle_excerpt(str(entry.get("thought") or ""), min(500, max_chars // 3)),
        "result_index": identities,
        "older_context_condensed": True,
    }
    if _context_chars(indexed) <= max_chars:
        return indexed
    available = max(80, max_chars - 250)
    return {
        "step": entry.get("step"),
        "result_index_excerpt": _middle_excerpt(_json_text(identities), available),
        "older_context_condensed": True,
    }


def _compact_context_value(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return _middle_excerpt(value, max_chars)
    if _context_chars(value) <= max_chars:
        return value
    return _middle_excerpt(_json_text(value), max_chars)


def _middle_excerpt(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 40:
        return text[:max_chars]
    marker = f"\n... [{len(text) - max_chars} chars omitted] ...\n"
    available = max_chars - len(marker)
    if available <= 0:
        return text[:max_chars]
    head = (available * 2) // 3
    tail = available - head
    return text[:head] + marker + text[-tail:]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _context_chars(value: Any) -> int:
    return len(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _valid_finish(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "answer": str(action.get("answer") or ""),
        "confidence": _float_between(action.get("confidence"), default=0.0),
        "reasoning_summary": str(action.get("reasoning_summary") or ""),
        "caveats": action.get("caveats") if isinstance(action.get("caveats"), list) else [],
        "sources": action.get("sources") if isinstance(action.get("sources"), list) else [],
    }


def _valid_agent_tools(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    actions = raw.get("actions")
    if not isinstance(actions, list):
        actions = []
    clean_actions = [action for action in actions if isinstance(action, dict) and action.get("type")]
    return {"thought": str(raw.get("thought") or ""), "actions": clean_actions}


def _extract_solver_finish(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for action in actions:
        if str(action.get("type") or "") != "finish":
            continue
        return {
            "task_id": str(action.get("task_id") or ""),
            "title": str(action.get("title") or ""),
            "approach": str(action.get("approach") or ""),
            "answer_fragment": str(action.get("answer_fragment") or ""),
            "confidence": _float_between(action.get("confidence"), default=0.0),
            "assumptions": action.get("assumptions") if isinstance(action.get("assumptions"), list) else [],
        }
    return None


def _fallback_finish(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    answer = ""
    for entry in reversed(transcript):
        for result in entry.get("results", []):
            output = result.get("output")
            if isinstance(output, dict):
                fragment = output.get("answer_fragment") or output.get("latex_document") or output.get("answer")
                if fragment:
                    answer = str(fragment)
                    break
        if answer:
            break
    return {
        "answer": answer or "No answer produced.",
        "confidence": 0.0,
        "reasoning_summary": "Fallback: the orchestrator did not emit a finish action within the step budget.",
        "caveats": ["Automatic fallback answer assembled from the last subagent output."],
        "sources": [],
    }


def _looks_like_latex_document(text: str) -> bool:
    return (
        "\\documentclass" in text
        and "\\begin{document}" in text
        and "\\end{document}" in text
        and "Question" in text
        and "Solution" in text
    )


def _compile_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(result.get("ok")),
        "engine": result.get("engine"),
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "pdf_path": result.get("pdf_path"),
        "error": result.get("error"),
    }


def _trim_tool_output(output: dict[str, Any], *, limit: int = 1200) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    for key, value in output.items():
        if isinstance(value, str) and len(value) > limit:
            trimmed[key] = value[:limit] + "..."
        else:
            trimmed[key] = value
    return trimmed


def _float_between(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _sum_usage(run_dir: Path) -> TokenUsage:
    total = TokenUsage()
    for call_path in sorted((run_dir / "agents").glob("*/call.json")):
        try:
            raw = read_json(call_path)
        except (OSError, ValueError):
            continue
        usage = raw.get("usage") if isinstance(raw, dict) else None
        if not isinstance(usage, dict):
            continue
        total.merge(
            TokenUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                reasoning_output_tokens=int(usage.get("reasoning_output_tokens") or 0),
                n_turns=int(usage.get("n_turns") or 0),
            )
        )
    return total
