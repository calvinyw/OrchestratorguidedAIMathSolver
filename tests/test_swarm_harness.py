from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swarm_harness.codex_cli import (  # noqa: E402
    CodexCLIBackend,
    CodexCLIConfig,
    MockBackend,
    capacity_error_message,
    extract_capacity_error,
    parse_codex_jsonl,
)
from swarm_harness.cli import build_parser, run_solve, _capacity_fallback_models  # noqa: E402
from swarm_harness.harness_io import read_problems, write_batch_outputs  # noqa: E402
from swarm_harness.orchestrator import (  # noqa: E402
    SwarmOrchestrator,
    _compact_orchestrator_transcript,
    _valid_orchestrator,
    load_workflow_config,
    settings_from_config,
)
from swarm_harness.subagents import SubagentRegistry  # noqa: E402
from swarm_harness.tools import OrchestratorTools, _real_code  # noqa: E402
from swarm_harness.records import AgentCallResult, Problem  # noqa: E402
from swarm_harness.prompts import extract_graph_builder_output  # noqa: E402
from swarm_harness.util import read_json, write_json  # noqa: E402


CAPACITY_STDOUT = (
    '{"type":"thread.started","thread_id":"t1"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"Selected model is at capacity. Please try a different model."}\n'
    '{"type":"turn.failed","error":{"message":"Selected model is at capacity. Please try a different model."}}\n'
)


class CodexCLITests(unittest.TestCase):
    def test_codex_command_uses_exec_json_schema_and_stdin(self) -> None:
        backend = CodexCLIBackend(CodexCLIConfig(model="gpt-test", reasoning_effort="low"))
        cmd = backend.build_command(
            workspace=Path("/tmp/work"),
            last_message_path=Path("/tmp/work/last.txt"),
            schema_path=Path("/tmp/schema.json"),
        )

        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--json", cmd)
        self.assertIn("--output-schema", cmd)
        self.assertIn("gpt-test", cmd)
        self.assertIn('model_reasoning_effort="low"', cmd)
        self.assertEqual(cmd[-1], "-")

    def test_codex_command_allows_per_call_model_overrides(self) -> None:
        backend = CodexCLIBackend(CodexCLIConfig(model="gpt-default", reasoning_effort="ultra"))
        cmd = backend.build_command(
            workspace=Path("/tmp/work"),
            last_message_path=Path("/tmp/work/last.txt"),
            schema_path=None,
            model="gpt-specialist",
            reasoning_effort="high",
        )

        self.assertIn("gpt-specialist", cmd)
        self.assertNotIn("gpt-default", cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertNotIn('model_reasoning_effort="ultra"', cmd)

    def test_codex_command_adds_optional_codebase(self) -> None:
        backend = CodexCLIBackend(CodexCLIConfig(extra_args=("--add-dir", "/tmp/codebase")))
        cmd = backend.build_command(
            workspace=Path("/tmp/work"),
            last_message_path=Path("/tmp/work/last.txt"),
            schema_path=None,
        )
        self.assertIn("--add-dir", cmd)
        self.assertIn("/tmp/codebase", cmd)

    def test_parse_codex_usage_jsonl(self) -> None:
        usage = parse_codex_jsonl(
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":3,'
            '"output_tokens":7,"reasoning_output_tokens":2}}\n'
            '{"type":"other"}\n'
        )

        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.cached_input_tokens, 3)
        self.assertEqual(usage.output_tokens, 7)
        self.assertEqual(usage.reasoning_output_tokens, 2)
        self.assertEqual(usage.n_turns, 1)

    def test_extract_capacity_error_from_jsonl(self) -> None:
        message = extract_capacity_error(CAPACITY_STDOUT)
        self.assertIsNotNone(message)
        self.assertIn("at capacity", message.lower())

    def test_capacity_error_message_ignores_successful_calls(self) -> None:
        result = AgentCallResult(
            role="custom",
            call_id="ok",
            workspace=Path("/tmp"),
            command=["codex"],
            prompt="hi",
            content='{"ok":true}',
            parsed={"ok": True},
            returncode=0,
            stdout=CAPACITY_STDOUT,
        )
        self.assertIsNone(capacity_error_message(result))

    def test_run_agent_retries_capacity_error_with_fallback_model(self) -> None:
        backend = CodexCLIBackend(
            CodexCLIConfig(
                model="gpt-5.6-sol",
                capacity_fallback_models=("gpt-5.4",),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "agent"
            workspace.mkdir()
            attempts: list[str | None] = []

            async def fake_run_once(**kwargs):
                attempts.append(kwargs.get("model"))
                model = kwargs.get("model")
                if model == "gpt-5.6-sol":
                    return AgentCallResult(
                        role="custom",
                        call_id="capacity-test",
                        workspace=workspace,
                        command=["codex", "-m", "gpt-5.6-sol"],
                        prompt="Prove it.",
                        content=CAPACITY_STDOUT,
                        parsed=None,
                        returncode=1,
                        stdout=CAPACITY_STDOUT,
                        error="Selected model is at capacity. Please try a different model.",
                        model_requested=model,
                        model_used=model,
                    )
                return AgentCallResult(
                    role="custom",
                    call_id="capacity-test",
                    workspace=workspace,
                    command=["codex", "-m", "gpt-5.4"],
                    prompt="Prove it.",
                    content='{"task_id":"ok","answer_fragment":"done"}',
                    parsed={"task_id": "ok", "answer_fragment": "done"},
                    returncode=0,
                    stdout='{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
                    model_requested=model,
                    model_used=model,
                )

            with patch.object(backend, "_run_once", side_effect=fake_run_once):
                result = asyncio.run(
                    backend.run_agent(
                        role="custom",
                        call_id="capacity-test",
                        prompt="Prove it.",
                        workspace=workspace,
                        schema_path=None,
                        timeout_s=30,
                        model="gpt-5.6-sol",
                    )
                )

            self.assertEqual(attempts, ["gpt-5.6-sol", "gpt-5.4"])
            self.assertTrue(result.ok)
            self.assertEqual(result.model_requested, "gpt-5.6-sol")
            self.assertEqual(result.model_used, "gpt-5.4")
            self.assertTrue(result.model_fallback_used)
            self.assertIn("at capacity", (result.model_fallback_reason or "").lower())
            retry_meta = read_json(workspace / "capacity_retry.json")
            self.assertEqual(retry_meta["requested_model"], "gpt-5.6-sol")
            self.assertEqual(retry_meta["model_used"], "gpt-5.4")
            call_meta = read_json(workspace / "call.json")
            self.assertEqual(call_meta["model_used"], "gpt-5.4")
            self.assertTrue(call_meta["model_fallback_used"])

    def test_parser_defaults_reasoning_effort_to_medium(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            args = build_parser().parse_args(
                ["solve", "--backend", "mock", "--problem-text", "Prove something."]
            )

        self.assertEqual(args.reasoning_effort, "medium")

    def test_parser_defaults_model_to_gpt_56_sol(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            args = build_parser().parse_args(
                ["solve", "--backend", "mock", "--problem-text", "Prove something."]
            )

        self.assertEqual(args.model, "gpt-5.6-sol")

    def test_parser_capacity_fallback_models(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            args = build_parser().parse_args(
                [
                    "solve",
                    "--backend",
                    "mock",
                    "--problem-text",
                    "Prove something.",
                    "--capacity-fallback-model",
                    "gpt-5.4",
                    "--capacity-fallback-model",
                    "gpt-5.3-codex",
                ]
            )
        self.assertEqual(_capacity_fallback_models(args), ("gpt-5.4", "gpt-5.3-codex"))

    def test_parser_aristotle_is_opt_in_with_long_timeout(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            args = build_parser().parse_args(
                ["solve", "--backend", "mock", "--problem-text", "Prove something."]
            )

        self.assertFalse(args.enable_aristotle)
        self.assertEqual(args.aristotle_executable, "aristotle")
        self.assertEqual(args.aristotle_timeout_s, 8 * 60 * 60)

    def test_parser_save_code_is_opt_in(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            default_args = build_parser().parse_args(
                ["solve", "--backend", "mock", "--problem-text", "Prove something."]
            )
            enabled_args = build_parser().parse_args(
                ["solve", "--backend", "mock", "--problem-text", "Prove something.", "--save-code"]
            )

        self.assertFalse(default_args.save_code)
        self.assertTrue(enabled_args.save_code)


class SwarmHarnessTests(unittest.TestCase):
    @staticmethod
    def _large_transcript(step_count: int = 10) -> list[dict]:
        return [
            {
                "step": step,
                "thought": f"thought-{step}",
                "results": [
                    {
                        "action": "assign_task",
                        "agent": f"agent-{step}",
                        "task_id": f"task-{step}",
                        "call_id": f"assign-{step:02d}-01-task-{step}",
                        "output": {
                            "task_id": f"task-{step}",
                            "title": f"Result {step}",
                            "approach": f"Approach for step {step}",
                            "answer_fragment": f"proof-{step}-" + ("x" * 8_000),
                            "confidence": 0.9,
                            "assumptions": [f"assumption-{step}"],
                        },
                        "model_requested": "gpt-test",
                        "model_used": "gpt-test",
                    }
                ],
            }
            for step in range(1, step_count + 1)
        ]

    def test_empty_orchestrator_decision_is_invalid(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty decision"):
            _valid_orchestrator({"thought": "", "actions": []})

    def test_context_compaction_preserves_latest_eight_steps_verbatim(self) -> None:
        transcript = self._large_transcript()
        run_dir = Path("/tmp/context-compaction-run")

        compacted, stats = _compact_orchestrator_transcript(
            transcript,
            run_dir=run_dir,
            keep_recent_steps=8,
            max_older_chars=4_000,
        )

        self.assertEqual(compacted[-8:], transcript[-8:])
        self.assertEqual(stats["older_steps_condensed"], 2)
        self.assertEqual(stats["recent_steps_preserved"], 8)
        self.assertLess(
            stats["older_context_chars_after"],
            stats["older_context_chars_before"],
        )
        self.assertIn("context_compaction", compacted[0])
        condensed_json = json.dumps(compacted[1:-8], ensure_ascii=False)
        self.assertIn("full_output_artifact", condensed_json)
        self.assertNotIn("x" * 8_000, condensed_json)

    def test_input_too_large_retries_same_step_with_compacted_context(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            problem = Problem(id="context-retry", statement="Prove something.")
            settings = settings_from_config(Path(temp_dir), workflow, run_id="context-retry")
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            run_dir = settings.output_dir / "workflow_runs" / "context-retry"
            run_dir.mkdir(parents=True, exist_ok=True)
            orchestrator._ctx = orchestrator._init_run_context(
                run_id="context-retry",
                run_dir=run_dir,
                agents_dir=run_dir / "agents",
                trace_path=run_dir / "events.jsonl",
                problem=problem,
            )
            transcript = self._large_transcript(step_count=25)
            calls = []

            async def context_retry_call(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return AgentCallResult(
                        role="orchestrator",
                        call_id=kwargs["call_id"],
                        workspace=kwargs["workspace"],
                        command=["codex"],
                        prompt=kwargs["prompt"],
                        content='{"type":"thread.started","thread_id":"t1"}',
                        parsed={"type": "thread.started", "thread_id": "t1"},
                        returncode=1,
                        stderr=(
                            "Input exceeds the maximum length of 1048576 characters "
                            '(input_error_code="input_too_large")'
                        ),
                        error="Codex worker exited with status 1.",
                    )
                return AgentCallResult(
                    role="orchestrator",
                    call_id=kwargs["call_id"],
                    workspace=kwargs["workspace"],
                    command=["codex"],
                    prompt=kwargs["prompt"],
                    content='{"thought":"Recovered","actions":[{"type":"finish"}]}',
                    parsed={"thought": "Recovered", "actions": [{"type": "finish"}]},
                    returncode=0,
                )

            with patch.object(orchestrator, "_call", side_effect=context_retry_call):
                decision = asyncio.run(orchestrator._orchestrator_step(problem, transcript, 26, 40))

            self.assertEqual(decision["thought"], "Recovered")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["call_id"], "orchestrator-26")
            self.assertEqual(calls[1]["call_id"], "orchestrator-26-context-retry")
            self.assertLess(len(calls[1]["prompt"]), len(calls[0]["prompt"]))
            self.assertIn("recent_steps_preserved_verbatim", calls[1]["prompt"])
            self.assertIn(transcript[-1]["results"][0]["output"]["answer_fragment"], calls[1]["prompt"])
            self.assertEqual(read_json(run_dir / "orchestrator_step_26.json"), decision)
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"type": "orchestrator.context_compacted"', events)
            self.assertIn('"recent_steps_preserved": 8', events)

    def test_failed_orchestrator_call_is_not_saved_as_a_decision(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            problem = Problem(id="failed-step", statement="Prove something.")
            settings = settings_from_config(Path(temp_dir), workflow, run_id="failed-step")
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            run_dir = settings.output_dir / "workflow_runs" / "failed-step"
            run_dir.mkdir(parents=True, exist_ok=True)
            orchestrator._ctx = orchestrator._init_run_context(
                run_id="failed-step",
                run_dir=run_dir,
                agents_dir=run_dir / "agents",
                trace_path=run_dir / "events.jsonl",
                problem=problem,
            )
            failed_call = AgentCallResult(
                role="orchestrator",
                call_id="orchestrator-01",
                workspace=run_dir / "agents" / "orchestrator-01",
                command=["codex"],
                prompt="too large",
                content='{"type":"thread.started","thread_id":"t1"}',
                parsed={"type": "thread.started", "thread_id": "t1"},
                returncode=1,
                error="Codex worker exited with status 1.",
            )

            async def fail_call(**kwargs):
                return failed_call

            with patch.object(orchestrator, "_call", side_effect=fail_call):
                with self.assertRaisesRegex(RuntimeError, "Orchestrator call orchestrator-01 failed"):
                    asyncio.run(orchestrator._orchestrator_step(problem, [], 1, 40))

            self.assertFalse((run_dir / "orchestrator_step_01.json").exists())

    def test_assignment_action_passes_per_call_model_overrides(self) -> None:
        class RecordingBackend(MockBackend):
            def __init__(self) -> None:
                self.last_kwargs = None

            async def run_agent(self, **kwargs):
                self.last_kwargs = kwargs
                return await super().run_agent(**kwargs)

        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            problem = Problem(id="override", statement="Prove a difficult lemma.")
            backend = RecordingBackend()
            settings = settings_from_config(Path(temp_dir), workflow, run_id="override-test")
            orchestrator = SwarmOrchestrator(backend, settings)
            run_dir = settings.output_dir / "workflow_runs" / "override-test"
            run_dir.mkdir(parents=True, exist_ok=True)
            orchestrator._ctx = orchestrator._init_run_context(
                run_id="override-test",
                run_dir=run_dir,
                agents_dir=run_dir / "agents",
                trace_path=run_dir / "events.jsonl",
                problem=problem,
            )
            orchestrator.create_subagent("specialist", "Prove difficult lemmas.")

            asyncio.run(
                orchestrator._run_assignment_action(
                    problem,
                    {
                        "agent": "specialist",
                        "task_id": "hard-lemma",
                        "prompt": "Prove it.",
                        "model": "gpt-specialist",
                        "reasoning_effort": "max",
                    },
                    step=1,
                    index=1,
                )
            )

            self.assertEqual(backend.last_kwargs["model"], "gpt-specialist")
            self.assertEqual(backend.last_kwargs["reasoning_effort"], "max")
            self.assertEqual(backend.last_kwargs["timeout_s"], 3600)

    def test_assignment_result_includes_model_used_and_fallback_metadata(self) -> None:
        class FallbackBackend(MockBackend):
            async def run_agent(self, **kwargs):
                result = await super().run_agent(**kwargs)
                result.model_requested = "gpt-5.6-sol"
                result.model_used = "gpt-5.4"
                result.model_fallback_used = True
                result.model_fallback_reason = (
                    "Requested model 'gpt-5.6-sol' was at capacity; retried with 'gpt-5.4'."
                )
                return result

        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            problem = Problem(id="fallback-meta", statement="Prove a difficult lemma.")
            settings = settings_from_config(Path(temp_dir), workflow, run_id="fallback-meta")
            orchestrator = SwarmOrchestrator(FallbackBackend(), settings)
            run_dir = settings.output_dir / "workflow_runs" / "fallback-meta"
            run_dir.mkdir(parents=True, exist_ok=True)
            orchestrator._ctx = orchestrator._init_run_context(
                run_id="fallback-meta",
                run_dir=run_dir,
                agents_dir=run_dir / "agents",
                trace_path=run_dir / "events.jsonl",
                problem=problem,
            )
            orchestrator.create_subagent("specialist", "Prove difficult lemmas.")

            entry = asyncio.run(
                orchestrator._run_assignment_action(
                    problem,
                    {
                        "agent": "specialist",
                        "task_id": "hard-lemma",
                        "prompt": "Prove it.",
                        "model": "gpt-5.6-sol",
                    },
                    step=1,
                    index=1,
                )
            )

            self.assertEqual(entry["model_requested"], "gpt-5.6-sol")
            self.assertEqual(entry["model_used"], "gpt-5.4")
            self.assertTrue(entry["model_fallback_used"])
            self.assertIn("at capacity", entry["model_fallback_reason"].lower())

    def test_mock_swarm_solves_and_writes_artifacts(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_from_config(Path(temp_dir), workflow, max_parallel=2)
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            result = asyncio.run(
                orchestrator.solve(
                    Problem(id="sqrt2", statement="Prove that sqrt(2) is irrational.")
                )
            )

            self.assertEqual(result["status"], "done")
            self.assertIn("\\sqrt{2}", result["answer"])
            run_dir = Path(result["run_dir"])
            self.assertTrue((run_dir / "events.jsonl").exists())
            self.assertTrue((run_dir / "solution.md").exists())
            self.assertTrue((run_dir / "solution.tex").exists())
            self.assertTrue((run_dir / "solution.pdf").exists())
            solution_tex = (run_dir / "solution.tex").read_text(encoding="utf-8")
            self.assertIn("\\documentclass", solution_tex)
            self.assertIn("Question", solution_tex)
            self.assertIn("Solution", solution_tex)
            self.assertIn("sqrt(2)", solution_tex)
            self.assertEqual((run_dir / "solution.pdf").read_bytes()[:5], b"%PDF-")
            self.assertEqual(result["solution_tex_path"], str(run_dir / "solution.tex"))
            self.assertEqual(result["solution_pdf_path"], str(run_dir / "solution.pdf"))
            self.assertTrue(result["solution_pdf_compile"]["ok"])
            self.assertTrue((run_dir / "subagents.json").exists())
            self.assertTrue((run_dir / "orchestrator_commands.json").exists())
            self.assertTrue((run_dir / "transcript.json").exists())
            commands = read_json(run_dir / "orchestrator_commands.json")["commands"]
            self.assertIn("lemma_prover", commands)
            self.assertIn("proof_writer", commands)
            self.assertIn("final_proof_writer", commands)
            self.assertIn("Graph_builder", commands)
            self.assertIn("summarizer", commands)
            self.assertIn("critiquer", commands)
            self.assertIn("final_critiquer", commands)

    def test_solve_accepts_plain_text_problem_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            problem_file = temp / "problem.txt"
            problem_file.write_text("Prove that sqrt(2) is irrational.\n", encoding="utf-8")
            args = build_parser().parse_args(
                [
                    "solve",
                    "--backend",
                    "mock",
                    "--workflow",
                    str(ROOT / "configs" / "workflows" / "math_swarm.json"),
                    "--output",
                    str(temp / "outputs"),
                    "--problem-id",
                    "sqrt2-file",
                    "--problem-file",
                    str(problem_file),
                ]
            )

            result = asyncio.run(run_solve(args))

            self.assertEqual(result["status"], "done")
            self.assertEqual(result["problem_id"], "sqrt2-file")
            run_dir = Path(result["run_dir"])
            stored_input = read_json(run_dir / "input.json")
            self.assertEqual(stored_input["statement"], "Prove that sqrt(2) is irrational.")
            self.assertIn("\\sqrt{2}", result["answer"])
            self.assertTrue((run_dir / "solution.tex").exists())
            self.assertTrue((run_dir / "solution.pdf").exists())

    def test_solve_codebase_is_saved_and_announced_to_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            codebase = temp / "cross-ratio-degrees"
            codebase.mkdir()
            args = build_parser().parse_args(
                [
                    "solve", "--backend", "mock", "--workflow", str(ROOT / "configs" / "workflows" / "math_swarm.json"),
                    "--output", str(temp / "outputs"), "--problem-text", "Test codebase access.",
                    "--codebase", str(codebase), "--save-code",
                ]
            )
            result = asyncio.run(run_solve(args))
            run_dir = Path(result["run_dir"])
            stored_input = read_json(run_dir / "input.json")
            self.assertEqual(stored_input["codebase"], str(codebase.resolve()))
            self.assertTrue(stored_input["save_code"])
            prompts = list((run_dir / "agents").glob("*/prompt.md"))
            self.assertTrue(prompts)
            self.assertIn(str(codebase.resolve()), prompts[0].read_text(encoding="utf-8"))
            self.assertIn("swarm_code", prompts[0].read_text(encoding="utf-8"))

    def test_orchestrator_is_self_directed_no_builtin_subagents(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            problem = Problem(id="sqrt2", statement="Prove that sqrt(2) is irrational.")
            settings = settings_from_config(Path(temp_dir), workflow, run_id="dyn-test")
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            run_dir = settings.output_dir / "workflow_runs" / "dyn-test"
            run_dir.mkdir(parents=True, exist_ok=True)
            ctx = orchestrator._init_run_context(
                run_id="dyn-test",
                run_dir=run_dir,
                agents_dir=run_dir / "agents",
                trace_path=run_dir / "events.jsonl",
                problem=problem,
            )
            # Nothing is registered until the orchestrator decides to spawn subagents.
            self.assertEqual(ctx.registry.list_agents(), [])

            result = asyncio.run(orchestrator.solve(problem))
            created = set(result["subagents"].keys())
            self.assertEqual(
                created,
                {"Graph_builder", "lemma-prover", "critiquer", "summarizer"},
            )
            self.assertGreaterEqual(result["steps"], 4)
            # The transcript records dynamic decomposition and standard task use.
            actions = [r["action"] for entry in result["transcript"] for r in entry["results"]]
            self.assertIn("Graph_builder", actions)
            self.assertIn("lemma_prover", actions)
            self.assertGreaterEqual(actions.count("lemma_prover"), 3)
            self.assertIn("critiquer", actions)
            self.assertIn("summarizer", actions)
            self.assertIn("finish", actions)

    def test_subagent_registry_create_and_resolve(self) -> None:
        registry = SubagentRegistry()
        spec = registry.create_subagent("gap-checker", "Check proofs for gaps.")
        self.assertEqual(spec.name, "gap-checker")
        self.assertEqual(registry.resolve("gap-checker").system_prompt, "Check proofs for gaps.")
        with self.assertRaises(KeyError):
            registry.resolve("missing")

    def test_orchestrator_tools_mock_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            trace_path = temp / "events.jsonl"
            tools = OrchestratorTools(run_dir=temp, trace_path=trace_path, mock=True)
            search = asyncio.run(tools.search("irrational sqrt 2"))
            browse = asyncio.run(tools.browse("https://example.com"))
            code = asyncio.run(tools.code("print(1+1)"))
            self.assertTrue(search["mock"])
            self.assertTrue(browse["mock"])
            self.assertTrue(code["mock"])
            self.assertTrue((temp / "tools" / "search-001.json").exists())

    def test_save_code_requires_a_codebase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            args = build_parser().parse_args(
                [
                    "solve",
                    "--backend",
                    "mock",
                    "--output",
                    temp_dir,
                    "--problem-text",
                    "Prove something.",
                    "--save-code",
                ]
            )
            args.codebase = None

            with self.assertRaisesRegex(ValueError, "--save-code requires --codebase"):
                asyncio.run(run_solve(args))

    def test_save_code_writes_and_runs_script_inside_codebase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            run_dir = temp / "run-example"
            codebase = temp / "codebase"
            codebase.mkdir()
            tools = OrchestratorTools(
                run_dir=run_dir,
                trace_path=run_dir / "events.jsonl",
                codebase_dir=codebase,
                save_code=True,
            )

            result = asyncio.run(tools.code("print(1 + 1)"))

            script_path = codebase.resolve() / "swarm_code" / "run-example" / "code-001" / "snippet.py"
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["stdout"].strip(), "2")
            self.assertEqual(result["saved_code_path"], str(script_path))
            self.assertEqual(script_path.read_text(encoding="utf-8"), "print(1 + 1)\n")
            artifact = read_json(run_dir / "tools" / "code-001.json")
            self.assertTrue(artifact["save_code"])
            self.assertEqual(artifact["saved_code_path"], str(script_path))

    def test_aristotle_tool_requires_prior_advisor(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            problem = Problem(id="formal", statement="Formalize a lemma in Lean.")
            settings = settings_from_config(Path(temp_dir), workflow, run_id="aristotle-guard")
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            run_dir = settings.output_dir / "workflow_runs" / "aristotle-guard"
            run_dir.mkdir(parents=True, exist_ok=True)
            orchestrator._ctx = orchestrator._init_run_context(
                run_id="aristotle-guard",
                run_dir=run_dir,
                agents_dir=run_dir / "agents",
                trace_path=run_dir / "events.jsonl",
                problem=problem,
            )

            blocked = asyncio.run(
                orchestrator._run_tool(
                    "aristotle",
                    {
                        "prompt": "Fill sorries",
                        "mode": "submit",
                        "project_dir": ".",
                        "wait": True,
                    },
                )
            )
            self.assertFalse(blocked["output"]["ok"])
            self.assertIn("Aristotle_advisor", blocked["output"]["error"])

            orchestrator._ctx.aristotle_advice_seen = True
            allowed = asyncio.run(
                orchestrator._run_tool(
                    "aristotle",
                    {
                        "prompt": "Fill sorries",
                        "mode": "submit",
                        "project_dir": ".",
                        "wait": True,
                    },
                )
            )
            self.assertTrue(allowed["output"]["mock"])

    def test_real_code_runs_from_relative_workspace(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            workspace = Path(os.path.relpath(Path(temp_dir) / "code-001", Path.cwd()))

            result = asyncio.run(_real_code("print(1 + 1)", "python", workspace, 5))

            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["stdout"].strip(), "2")

    def test_orchestrator_commands_require_active_run(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_from_config(Path(temp_dir), workflow)
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            with self.assertRaises(RuntimeError):
                orchestrator.create_subagent("x", "y")

    def test_orchestrator_kimi_commands(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            problem = Problem(id="sqrt2", statement="Prove that sqrt(2) is irrational.")
            settings = settings_from_config(Path(temp_dir), workflow, run_id="cmd-test")
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            run_dir = settings.output_dir / "workflow_runs" / "cmd-test"
            run_dir.mkdir(parents=True, exist_ok=True)
            orchestrator._ctx = orchestrator._init_run_context(
                run_id="cmd-test",
                run_dir=run_dir,
                agents_dir=run_dir / "agents",
                trace_path=run_dir / "events.jsonl",
                problem=problem,
            )

            spec = orchestrator.create_subagent("numeric-checker", "Verify numeric claims with short code.")
            self.assertEqual(spec.name, "numeric-checker")
            search = asyncio.run(orchestrator.search("sqrt 2 irrational proof"))
            code = asyncio.run(orchestrator.code("print(1 + 1)"))
            call = asyncio.run(orchestrator.assign_task("numeric-checker", "Check whether sqrt(2) is rational."))
            graph = asyncio.run(orchestrator.Graph_builder("Build a ProofFlow-style DAG."))
            lemma = asyncio.run(
                orchestrator.lemma_prover(
                    "Prove node l1 from the proof DAG.",
                    task_id="lemma-l1",
                    graph_id="contradiction",
                    node_id="l1",
                    transcript=[{"step": 1, "results": [{"action": "Graph_builder", "output": graph.parsed}]}],
                )
            )
            proof = asyncio.run(
                orchestrator.proof_writer(
                    "sqrt(2) is irrational.",
                    "Draft the main proof with a clear contradiction structure.",
                )
            )
            critique = asyncio.run(
                orchestrator.critiquer(
                    "sqrt(2) is irrational.",
                    (proof.parsed or {}).get("answer_fragment", ""),
                    "Check for hidden assumptions and unsupported leaps.",
                )
            )
            transcript = [
                {"step": 1, "results": [{"action": "lemma_prover", "task_id": "lemma-l1", "output": lemma.parsed}]},
                {"step": 2, "results": [{"action": "proof_writer", "task_id": "proof", "output": proof.parsed}]},
            ]
            final_proof = asyncio.run(
                orchestrator.final_proof_writer(
                    "Assemble a complete LaTeX proof of the original problem.",
                    transcript=transcript,
                )
            )
            final_critique = asyncio.run(
                orchestrator.final_critiquer(
                    (final_proof.parsed or {}).get("latex_document", ""),
                    "Check whether the LaTeX document completely proves the original statement.",
                    transcript=transcript,
                )
            )
            summary = asyncio.run(orchestrator.summarizer("Summarize the strongest answer."))

            self.assertTrue(search["mock"])
            self.assertTrue(code["mock"])
            self.assertTrue(call.ok)
            self.assertTrue(graph.ok)
            self.assertTrue(lemma.ok)
            self.assertTrue(proof.ok)
            self.assertTrue(critique.ok)
            self.assertTrue(final_proof.ok)
            self.assertTrue(final_critique.ok)
            self.assertTrue(summary.ok)
            proof_graph = graph.parsed or {}
            self.assertIn("graphs", proof_graph)
            self.assertGreaterEqual(len(proof_graph["graphs"]), 1)
            selected = proof_graph["graphs"][0]
            self.assertEqual(selected.get("final_node_ids"), ["c1"])
            node_ids = {node["id"] for node in selected["nodes"]}
            self.assertEqual(set(selected["topological_order"]), node_ids)
            positions = {node_id: index for index, node_id in enumerate(selected["topological_order"])}
            for node in selected["nodes"]:
                self.assertLessEqual(set(node["dependencies"]), node_ids)
                for dependency in node["dependencies"]:
                    self.assertLess(positions[dependency], positions[node["id"]])
            self.assertIn("answer_fragment", lemma.parsed or {})
            self.assertEqual((lemma.parsed or {}).get("node_id"), "l1")
            self.assertIn("answer_fragment", proof.parsed or {})
            self.assertIn("\\begin{proof}", (proof.parsed or {}).get("answer_fragment", ""))
            self.assertIn("approved", critique.parsed or {})
            self.assertIn("latex_document", final_proof.parsed or {})
            self.assertIn("\\documentclass", (final_proof.parsed or {}).get("latex_document", ""))
            self.assertIn("approved", final_critique.parsed or {})
            self.assertIn("answer", summary.parsed or {})
            lemma_prover_spec = orchestrator._ctx.registry.get("lemma-prover")
            self.assertIsNotNone(lemma_prover_spec)
            assert lemma_prover_spec is not None
            self.assertEqual(lemma_prover_spec.tools, ("browse", "code"))
            proof_writer_spec = orchestrator._ctx.registry.get("proof-writer")
            self.assertIsNotNone(proof_writer_spec)
            assert proof_writer_spec is not None
            self.assertEqual(proof_writer_spec.tools, ("browse", "code"))
            tool_artifacts = list((run_dir / "tools").glob("*.json"))
            self.assertTrue(any(artifact.name.startswith("browse-") for artifact in tool_artifacts))
            self.assertTrue(any(artifact.name.startswith("code-") for artifact in tool_artifacts))
            self.assertIn("numeric-checker", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertIn("lemma-prover", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertIn("Graph_builder", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertIn("proof-writer", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertIn("final-proof-writer", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertIn("summarizer", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertIn("critiquer", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertIn("final-critiquer", (run_dir / "subagents.json").read_text(encoding="utf-8"))
            self.assertTrue(any(path.name.startswith("search-") for path in (run_dir / "tools").glob("*.json")))

    def test_harness_io_accepts_problem_object_and_writes_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            input_path = temp / "input.json"
            write_json(input_path, {"id": "p", "latex": "Prove P."})
            problems = read_problems(input_path)
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0].id, "p")

            output_dir = temp / "out"
            write_batch_outputs(
                output_dir,
                [
                    {
                        "problem_id": "p",
                        "status": "done",
                        "answer": "Answer.",
                        "confidence": 1.0,
                        "run_id": "run",
                        "run_dir": str(temp / "run"),
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    }
                ],
            )
            solutions = read_json(output_dir / "solutions.json")
            self.assertEqual(solutions[0]["id"], "p")
            self.assertTrue((output_dir / "p.tex").exists())
            self.assertTrue((output_dir / "p.pdf").exists())
            self.assertIn("Question", (output_dir / "p.tex").read_text(encoding="utf-8"))
            self.assertIn("Solution", (output_dir / "p.tex").read_text(encoding="utf-8"))
            self.assertEqual((output_dir / "p.pdf").read_bytes()[:5], b"%PDF-")
            self.assertEqual(solutions[0]["solution_tex_path"], str(output_dir / "p.tex"))
            self.assertEqual(solutions[0]["solution_pdf_path"], str(output_dir / "p.pdf"))
            self.assertTrue((output_dir / "token_usage.jsonl").exists())

    def test_extract_graph_builder_output_selects_graph_id(self) -> None:
        transcript = [
            {
                "step": 1,
                "results": [
                    {
                        "action": "Graph_builder",
                        "output": {
                            "task_id": "proof-dag",
                            "title": "Options",
                            "summary": "Two approaches.",
                            "graphs": [
                                {
                                    "graph_id": "alpha",
                                    "title": "Alpha",
                                    "graph_summary": "First route.",
                                    "nodes": [{"id": "c1", "kind": "conclusion", "statement": "A", "dependencies": [], "proof_hint": ""}],
                                    "topological_order": ["c1"],
                                    "final_node_ids": ["c1"],
                                    "confidence": 0.7,
                                },
                                {
                                    "graph_id": "beta",
                                    "title": "Beta",
                                    "graph_summary": "Second route.",
                                    "nodes": [{"id": "c1", "kind": "conclusion", "statement": "B", "dependencies": [], "proof_hint": ""}],
                                    "topological_order": ["c1"],
                                    "final_node_ids": ["c1"],
                                    "confidence": 0.6,
                                },
                            ],
                        },
                    }
                ],
            }
        ]
        alpha = extract_graph_builder_output(transcript, graph_id="alpha")
        beta = extract_graph_builder_output(transcript, graph_id="beta")
        default = extract_graph_builder_output(transcript)
        self.assertIsNone(default)
        self.assertEqual((alpha or {}).get("title"), "Alpha")
        self.assertEqual((beta or {}).get("title"), "Beta")

    def test_subagent_registry_round_trip_json(self) -> None:
        registry = SubagentRegistry()
        registry.create_subagent("gap-checker", "Check proofs for gaps.", role="critic")
        restored = SubagentRegistry.from_json(registry.to_json())
        self.assertEqual(restored.resolve("gap-checker").system_prompt, "Check proofs for gaps.")
        self.assertEqual(restored.resolve("gap-checker").role, "critic")

    def test_resume_from_orchestrator_step_continues_run(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_from_config(Path(temp_dir), workflow, run_id="resume-mock", max_parallel=2)
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            first = asyncio.run(
                orchestrator.solve(Problem(id="sqrt2", statement="Prove that sqrt(2) is irrational."))
            )
            run_dir = Path(first["run_dir"])
            self.assertGreaterEqual(len(first["transcript"]), 4)

            # Truncate to step 1 and resume from step 2.
            write_json(run_dir / "transcript.json", [first["transcript"][0]])
            for path in run_dir.glob("orchestrator_step_0[2-9].json"):
                path.unlink()
            for path in run_dir.glob("final.json"):
                path.unlink()

            resumed = asyncio.run(orchestrator.resume(run_dir, from_step=2))
            self.assertEqual(resumed["status"], "done")
            self.assertEqual(Path(resumed["run_dir"]).resolve(), run_dir.resolve())
            steps = [entry["step"] for entry in resumed["transcript"]]
            self.assertEqual(steps[0], 1)
            self.assertIn(2, steps)
            self.assertIn("\\sqrt{2}", resumed["answer"])
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"type": "run.resume"', events)

    def test_resume_reuses_saved_orchestrator_decision(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = settings_from_config(Path(temp_dir), workflow, run_id="reuse-decision", max_parallel=2)
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            first = asyncio.run(
                orchestrator.solve(Problem(id="sqrt2", statement="Prove that sqrt(2) is irrational."))
            )
            run_dir = Path(first["run_dir"])
            write_json(run_dir / "transcript.json", [first["transcript"][0]])
            # Keep orchestrator_step_02.json; drop later steps' agent workspaces for a clean replay.
            for path in run_dir.glob("orchestrator_step_0[3-9].json"):
                path.unlink()
            (run_dir / "final.json").unlink(missing_ok=True)

            resumed = asyncio.run(orchestrator.resume(run_dir, from_step=2, reuse_decision=True))
            self.assertEqual(resumed["status"], "done")
            self.assertEqual(read_json(run_dir / "orchestrator_step_02.json")["thought"], first["transcript"][1]["thought"])

    def test_resume_rebuilds_transcript_from_create_subagent_step(self) -> None:
        workflow = load_workflow_config(ROOT / "configs" / "workflows" / "math_swarm.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            run_dir = temp / "workflow_runs" / "rebuild-step1"
            run_dir.mkdir(parents=True)
            write_json(
                run_dir / "input.json",
                {"id": "sqrt2", "statement": "Prove that sqrt(2) is irrational.", "metadata": {}},
            )
            write_json(
                run_dir / "orchestrator_step_01.json",
                {
                    "thought": "Spawn helpers.",
                    "actions": [
                        {
                            "type": "create_subagent",
                            "name": "helper",
                            "system_prompt": "Help prove things.",
                        }
                    ],
                },
            )
            write_json(
                run_dir / "subagents.json",
                {
                    "helper": {
                        "name": "helper",
                        "role": "custom",
                        "schema_name": "solver.schema.json",
                        "tools": [],
                        "system_prompt": "Help prove things.",
                    }
                },
            )
            settings = settings_from_config(temp, workflow, max_parallel=2)
            orchestrator = SwarmOrchestrator(MockBackend(), settings)
            result = asyncio.run(orchestrator.resume(run_dir, from_step=2))
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["transcript"][0]["results"][0]["action"], "create_subagent")
            self.assertEqual(result["transcript"][0]["results"][0]["name"], "helper")

    def test_cli_resume_flags_require_run_dir_and_step(self) -> None:
        with self.assertRaises(ValueError):
            args = build_parser().parse_args(
                ["solve", "--backend", "mock", "--resume-from-step", "2", "--problem-text", "x"]
            )
            asyncio.run(run_solve(args))


if __name__ == "__main__":
    unittest.main()
