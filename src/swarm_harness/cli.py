from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from swarm_harness.codex_cli import (
    DEFAULT_CAPACITY_FALLBACK_MODELS,
    CodexCLIBackend,
    CodexCLIConfig,
    MockBackend,
)
from swarm_harness.harness_io import read_problems, write_batch_outputs
from swarm_harness.orchestrator import SwarmOrchestrator, load_workflow_config, settings_from_config
from swarm_harness.records import Problem
from swarm_harness.util import safe_id


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKFLOW = REPO_ROOT / "configs" / "workflows" / "math_swarm.json"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "medium"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "solve":
            result = asyncio.run(run_solve(args))
            print(f"Wrote run artifacts to {result['run_dir']}")
            print(f"Wrote solution TeX to {result['solution_tex_path']}")
            compile_result = result.get("solution_pdf_compile") if isinstance(result.get("solution_pdf_compile"), dict) else {}
            if compile_result.get("ok"):
                print(f"Wrote compiled solution PDF to {result['solution_pdf_path']}")
            else:
                print(f"TeX PDF compilation did not finish: {compile_result.get('error') or 'unknown error'}")
            print(str(result.get("answer", "")).rstrip())
            return 0
        if args.command == "harness":
            results = asyncio.run(run_harness(args))
            print(f"Wrote batch artifacts for {len(results)} problem(s) to {args.output}")
            return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kimi-style Codex CLI math swarm harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    solve = sub.add_parser("solve", help="Solve one math problem.")
    add_common_args(solve, include_output=True)
    solve.add_argument("--problem-id", default="problem")
    problem_source = solve.add_mutually_exclusive_group(required=False)
    problem_source.add_argument("--problem-text", help="Problem statement to prove.")
    problem_source.add_argument("--problem-file", type=Path, help="UTF-8 text file containing the problem statement.")
    solve.add_argument(
        "--run-dir",
        type=Path,
        help="Existing workflow run directory to resume (loads input.json / transcript).",
    )
    solve.add_argument(
        "--resume-from-step",
        type=int,
        help="1-based orchestrator step to start from when resuming a run.",
    )
    solve.add_argument(
        "--reuse-decision",
        action="store_true",
        help="When resuming, replay orchestrator_step_NN.json for the first resumed step instead of re-calling the orchestrator.",
    )

    harness = sub.add_parser("harness", help="Run the /data/input -> /data/output batch harness.")
    add_common_args(harness, include_output=False)
    harness.add_argument(
        "--input",
        type=Path,
        default=Path(os.environ.get("SWARM_INPUT_PATH") or "/data/input/input.json"),
    )
    harness.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("SWARM_OUTPUT_DIR") or "/data/output"),
    )
    harness.add_argument("--problem-parallel", type=int, default=int(os.environ.get("SWARM_PROBLEM_PARALLEL") or "1"))
    return parser


def add_common_args(parser: argparse.ArgumentParser, *, include_output: bool) -> None:
    parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    if include_output:
        parser.add_argument("--output", "--output-dir", dest="output", type=Path, default=Path("outputs"))
    parser.add_argument("--backend", choices=["codex", "mock"], default=os.environ.get("SWARM_BACKEND") or "codex")
    parser.add_argument("--model", default=os.environ.get("CODEX_SWARM_MODEL") or DEFAULT_MODEL)
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("CODEX_SWARM_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT,
    )
    parser.add_argument(
        "--capacity-fallback-model",
        action="append",
        dest="capacity_fallback_models",
        default=None,
        help=(
            "Simpler model to retry when the requested model is at capacity. "
            "May be passed multiple times for a fallback chain. "
            "Defaults to gpt-5.4 (or CODEX_SWARM_CAPACITY_FALLBACK_MODELS, comma-separated)."
        ),
    )
    parser.add_argument("--codex-sandbox", default=os.environ.get("CODEX_SWARM_SANDBOX") or "read-only")
    parser.add_argument("--codex-executable", default=os.environ.get("CODEX_SWARM_EXECUTABLE") or "codex")
    parser.add_argument(
        "--codebase",
        type=Path,
        default=_default_codebase_from_env(),
        help="Optional local repository available to agents and used as the working directory for code checks.",
    )
    parser.add_argument(
        "--save-code",
        action="store_true",
        default=_env_flag("CODEX_SWARM_SAVE_CODE"),
        help=(
            "Save code(...) scripts under <codebase>/swarm_code/<run-id>/ instead of only "
            "under the run artifacts. Requires --codebase (or CODEX_SWARM_CODEBASE)."
        ),
    )
    parser.add_argument(
        "--enable-aristotle",
        action="store_true",
        default=_env_flag("SWARM_ENABLE_ARISTOTLE"),
        help="Enable optional Harmonic Aristotle CLI calls. Requires aristotlelib and ARISTOTLE_API_KEY.",
    )
    parser.add_argument("--aristotle-executable", default=os.environ.get("ARISTOTLE_EXECUTABLE") or "aristotle")
    parser.add_argument(
        "--aristotle-timeout-s",
        type=int,
        default=int(os.environ.get("ARISTOTLE_TIMEOUT_S") or str(8 * 60 * 60)),
        help="Timeout for one Aristotle CLI call. Defaults to 8 hours because formalization can be slow.",
    )
    parser.add_argument("--max-parallel", type=int)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-runtime-minutes", type=int)
    parser.add_argument("--agent-timeout-s", type=int)
    parser.add_argument("--assign-task-timeout-s", type=int)


async def run_solve(args: argparse.Namespace) -> dict[str, Any]:
    resume_dir = getattr(args, "run_dir", None)
    resume_from_step = getattr(args, "resume_from_step", None)
    reuse_decision = bool(getattr(args, "reuse_decision", False))
    if resume_dir is not None or resume_from_step is not None:
        if resume_dir is None or resume_from_step is None:
            raise ValueError("Resume requires both --run-dir and --resume-from-step.")
        if args.problem_text is not None or args.problem_file is not None:
            raise ValueError(
                "Do not pass --problem-text/--problem-file when resuming; "
                "the problem is loaded from the run directory."
            )
        if args.codebase is None:
            saved_codebase = _load_saved_codebase(Path(resume_dir))
            if saved_codebase is not None:
                args.codebase = saved_codebase
        if not args.save_code and _load_saved_save_code(Path(resume_dir)):
            args.save_code = True
        orchestrator = build_orchestrator(args)
        return await orchestrator.resume(
            Path(resume_dir),
            from_step=int(resume_from_step),
            reuse_decision=reuse_decision,
        )
    if reuse_decision:
        raise ValueError("--reuse-decision is only valid with --run-dir and --resume-from-step.")
    problem_text = load_problem_text(args.problem_text, args.problem_file)
    problem = Problem(id=safe_id(args.problem_id), statement=problem_text)
    orchestrator = build_orchestrator(args)
    return await orchestrator.solve(problem)


def load_problem_text(problem_text: str | None, problem_file: Path | None) -> str:
    if problem_text is not None and problem_file is not None:
        raise ValueError("Provide only one of --problem-text or --problem-file.")
    if problem_file is not None:
        problem_text = problem_file.read_text(encoding="utf-8")
    if problem_text is None or not problem_text.strip():
        raise ValueError("Provide a non-empty --problem-text or --problem-file.")
    return problem_text.strip()


async def run_harness(args: argparse.Namespace) -> list[dict[str, Any]]:
    problems = read_problems(args.input)
    args.output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, args.problem_parallel))

    async def solve_one(problem: Problem) -> dict[str, Any]:
        async with semaphore:
            child_args = argparse.Namespace(**vars(args))
            child_args.output = args.output
            orchestrator = build_orchestrator(child_args)
            return await orchestrator.solve(problem)

    results = await asyncio.gather(*(solve_one(problem) for problem in problems))
    write_batch_outputs(args.output, results)
    return results


def build_orchestrator(args: argparse.Namespace) -> SwarmOrchestrator:
    codebase = _resolve_codebase(args.codebase)
    save_code = bool(getattr(args, "save_code", False))
    if save_code and codebase is None:
        raise ValueError("--save-code requires --codebase (or CODEX_SWARM_CODEBASE).")
    workflow = load_workflow_config(args.workflow)
    backend = build_backend(args)
    settings = settings_from_config(
        args.output,
        workflow,
        max_parallel=args.max_parallel,
        max_steps=args.max_steps,
        max_runtime_minutes=args.max_runtime_minutes,
        agent_timeout_s=args.agent_timeout_s,
        assign_task_timeout_s=args.assign_task_timeout_s,
        aristotle_enabled=args.enable_aristotle,
        aristotle_executable=args.aristotle_executable,
        aristotle_timeout_s=args.aristotle_timeout_s,
        codebase_dir=codebase,
        save_code=save_code,
    )
    return SwarmOrchestrator(backend, settings)


def build_backend(args: argparse.Namespace) -> CodexCLIBackend | MockBackend:
    if args.backend == "mock":
        return MockBackend()
    return CodexCLIBackend(
        CodexCLIConfig(
            executable=args.codex_executable,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=args.codex_sandbox,
            extra_args=("--add-dir", str(_resolve_codebase(args.codebase))) if args.codebase else (),
            capacity_fallback_models=_capacity_fallback_models(args),
        )
    )


def _capacity_fallback_models(args: argparse.Namespace) -> tuple[str, ...]:
    explicit = getattr(args, "capacity_fallback_models", None)
    if explicit:
        return tuple(model for model in explicit if str(model).strip())
    env_value = os.environ.get("CODEX_SWARM_CAPACITY_FALLBACK_MODELS")
    if env_value is not None:
        return tuple(part.strip() for part in env_value.split(",") if part.strip())
    return DEFAULT_CAPACITY_FALLBACK_MODELS


def _default_codebase_from_env() -> Path | None:
    value = (os.environ.get("CODEX_SWARM_CODEBASE") or "").strip()
    return Path(value) if value else None


def _resolve_codebase(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"--codebase must name an existing directory: {resolved}")
    return resolved


def _load_saved_codebase(run_dir: Path) -> Path | None:
    input_path = run_dir.expanduser().resolve() / "input.json"
    if not input_path.is_file():
        return None
    import json

    raw = json.loads(input_path.read_text(encoding="utf-8"))
    value = raw.get("codebase") if isinstance(raw, dict) else None
    return Path(value) if isinstance(value, str) and value else None


def _load_saved_save_code(run_dir: Path) -> bool:
    input_path = run_dir.expanduser().resolve() / "input.json"
    if not input_path.is_file():
        return False
    import json

    raw = json.loads(input_path.read_text(encoding="utf-8"))
    return bool(raw.get("save_code")) if isinstance(raw, dict) else False


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
