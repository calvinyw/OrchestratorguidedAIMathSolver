# Orchestrator-Guided AI Math Solver

A local agentic math harness where an orchestrator agent receives open-ended instructions and coordinates specialized subagents. Each worker is implemented as a `codex exec` terminal call rather than a direct model API call.

The orchestrator is **self-directed**: instead of a hard-coded pipeline, an `orchestrator` agent decides the workflow at runtime. Each step it can:

1. `create_subagent(name, system_prompt)` — spin up a specialized subagent on demand.
2. `assign_task(agent, prompt)` — delegate a subtask (assignments in the same step run in parallel).
3. `Graph_builder(prompt, task_id?)` — produce one or more ProofFlow-style directed acyclic graphs of statements to prove (alternative approaches when useful), where incoming arrows identify the prior statements each node may use as inputs.
4. `proof_writer(statement, prompt?, task_id?)` / `final_proof_writer(prompt?, task_id?)` / `summarizer(prompt, task_id?)` / `critiquer(statement, proof, prompt?, task_id?)` / `final_critiquer(proof, prompt?, task_id?)` — run standard math-swarm task agents when those common roles are useful. Prior proof writers output LaTeX in `answer_fragment`; `final_proof_writer` assembles a complete LaTeX document.
5. `search` / `browse` / `code` — gather or verify evidence with tools.
6. `Aristotle_advisor` / `aristotle` — optionally read current Harmonic Aristotle docs and then call Aristotle for Lean formalization or sorry filling.
7. `finish(...)` — return the final answer once the aggregated work is rigorous.

There are no predefined stages; the orchestrator chooses how to decompose the problem, which subagents or standard task agents to invoke, what to parallelize, and when to stop (bounded by `max_steps` and `max_runtime_minutes`).

The implementation mirrors the useful ProofCouncil pattern: terminal agents are subprocesses, prompts are piped through stdin, Codex JSONL is captured for usage, and all artifacts are written to inspectable run directories (including `orchestrator_step_NN.json` and `transcript.json`).

## Quick Start

Run a cheap smoke test with the deterministic mock backend:

```bash
./smoke/run_local.sh
```

Run one problem with the mock backend:

```bash
python3 scripts/run_swarm.py solve \
  --backend mock \
  --problem-id sqrt2 \
  --problem-text "Prove that sqrt(2) is irrational."
```

Or put the problem statement in a plain text file and pass that to the orchestrator:

```bash
python3 scripts/run_swarm.py solve \
  --backend mock \
  --problem-id sqrt2 \
  --problem-file smoke/problem.txt
```

Run one problem with real Codex CLI workers:

```bash
python3 scripts/run_swarm.py solve \
  --backend codex \
  --problem-id sqrt2 \
  --problem-text "Prove that sqrt(2) is irrational."
```

Using a text file with the real math-prover process looks like this:

```bash
python3 scripts/run_swarm.py solve \
  --backend codex \
  --problem-id my-proof \
  --problem-file path/to/problem.txt
```

The Codex backend calls:

```bash
codex exec --ignore-user-config --ephemeral --skip-git-repo-check --json \
  -m gpt-5.6-sol \
  -c 'model_reasoning_effort="medium"' \
  --sandbox read-only -C <agent-workspace> -o <last-message-file> \
  --output-schema <schema> -
```

Override the default model with `--model` or `CODEX_SWARM_MODEL`.
Override the default worker reasoning effort with `--reasoning-effort` or
`CODEX_SWARM_REASONING_EFFORT`.

## Optional local codebase

Give a real Codex run an existing repository to inspect and use for computational checks with
`--codebase`. Its absolute path is included in every agent prompt; agent terminals receive it via
Codex's `--add-dir`, and the swarm `code(...)` tool runs snippets with that repository as its
working directory. Snippets themselves are still saved under the run artifacts.

```bash
python3 scripts/run_swarm.py solve \
  --backend codex \
  --problem-id cerberus-recursions \
  --problem-file "input txts/cerberus_recurrence_proof.txt" \
  --codebase "/Users/calvinyost-wolff/Documents/GitHub/cross-ratio-degrees" \
  --codex-sandbox workspace-write
```

`workspace-write` is recommended: it lets workers create temporary search scripts while retaining
the normal sandbox. The default codebase is
`/Users/calvinyost-wolff/Documents/GitHub/cross-ratio-degrees` (override it with `--codebase` or
`CODEX_SWARM_CODEBASE`). Treat `--codebase` as a trusted repository, because Codex's `--add-dir`
grants workers write access to that directory. Workers are explicitly allowed to edit it when this
helps construct a counterexample search; use a clone if you want an extra safety boundary.

To keep every `code(...)` script in the codebase for later inspection or reuse, add `--save-code`:

```bash
python3 scripts/run_swarm.py solve \
  --backend codex \
  --problem-file path/to/problem.txt \
  --codebase path/to/repository \
  --save-code
```

`--save-code` requires a configured codebase. Scripts are stored by run and call under
`<codebase>/swarm_code/<run-id>/code-NNN/snippet.<ext>` while still being recorded in the normal
tool trace artifacts. Set `CODEX_SWARM_SAVE_CODE=1` to enable the same behavior through the
environment. Resumed runs retain the setting saved in their `input.json`.

The orchestrator decision loop always runs at ultra reasoning. Worker agents default to
gpt-5.6-sol at medium unless the orchestrator sets `model` and `reasoning_effort` on an
individual `assign_task` or standard task action. It is instructed to use ultra for difficult
mathematical work (lemma proving, proof writing, proof assembly).

If a worker call fails because the selected model is at capacity, the Codex backend automatically
retries the same prompt on a simpler fallback model (default `gpt-5.4`). Override the fallback
chain with repeated `--capacity-fallback-model` flags or
`CODEX_SWARM_CAPACITY_FALLBACK_MODELS` (comma-separated). Transcript results record
`model_requested`, `model_used`, and `model_fallback_used` so the orchestrator can see which
model actually produced the output.

## Optional Aristotle Integration

Harmonic Aristotle is optional and is not installed or called by default. It is best suited for
Lean formalization work, especially filling `sorry`s in an existing Lean project or formalizing a
document into Lean. It can take many hours.

Install the SDK/CLI in the environment you use for real runs:

```bash
uv pip install aristotlelib
export ARISTOTLE_API_KEY="your-api-key-here"
```

Then opt in for a run:

```bash
python3 scripts/run_swarm.py solve \
  --backend codex \
  --enable-aristotle \
  --aristotle-timeout-s 28800 \
  --problem-file path/to/problem.txt
```

You can also use environment variables:

```bash
export SWARM_ENABLE_ARISTOTLE=1
export ARISTOTLE_EXECUTABLE=aristotle
export ARISTOTLE_TIMEOUT_S=28800
```

The orchestrator is instructed, and the tool layer enforces, that an `Aristotle_advisor` task must
run before `aristotle`. The advisor should read current Aristotle docs, decide whether Aristotle is
appropriate, and propose the exact action fields: `mode`, `prompt`, `project_dir` or `source_path`,
`destination`, and `wait`.

Supported Aristotle modes:

- `submit`: runs `aristotle submit "<prompt>" --project-dir <project_dir> [--wait]`, intended for
  filling sorries in a Lean project.
- `formalize`: runs `aristotle formalize <source_path> [--wait] [--destination <destination>]`,
  intended for formalizing a source document.

Returned Aristotle artifacts should be treated as proof attempts until the Lean files are inspected
for unresolved `sorry` or `admit` placeholders and built with Lean/Lake.

## Harness Contract

The batch harness reads JSON from `/data/input/input.json` and writes results to `/data/output` by default. Override paths with CLI flags or environment variables:

```bash
python3 scripts/harness_entrypoint.py \
  --input smoke/input.json \
  --output smoke/output_local \
  --backend mock
```

Accepted input shapes:

```json
{
  "problems": [
    {
      "id": "sqrt2",
      "latex": "Prove that \\sqrt{2} is irrational."
    }
  ]
}
```

or a bare list of problem objects.

Outputs include:

- `solutions.json`
- `run_summary.json`
- `token_usage.jsonl`
- `<problem-id>.md`, `<problem-id>.tex`, and `<problem-id>.pdf` readable solution files
- `workflow_runs/<problem-id>-<run-id>/` with prompts, responses, parsed JSON, events, `solution.md`, `solution.tex`, and compiled `solution.pdf`

After `solution.md` is written, the harness asks a TeX writer agent to produce a full LaTeX
document with the question first and the solution second, then compiles it with `latexmk` or
`pdflatex` when one is available.

## Configuration

The default workflow lives at `configs/workflows/math_swarm.json`. It controls concurrency (`max_parallel`), the orchestrator's step budget (`max_steps`), the overall run time limit (`max_runtime_minutes`), the per-agent timeout, and the orchestrator's guiding description. JSON schemas for Codex structured outputs live in `schemas/` (`orchestrator.schema.json` for decisions, `graph_builder.schema.json` for proof DAGs, and `solver.schema.json` for proof-writing subagent outputs).

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use the mock backend and command-construction checks only; they do not call Codex.

## Inspiration

This harness is inspired by Kimi K2.5 Agent Swarm and other agentic harnesses that give an orchestrator agent open-ended control over many subagents instead of forcing a fixed pipeline. In those systems, the orchestrator decomposes complex tasks into heterogeneous subtasks, decides which tools and workers to invoke, and runs subagents concurrently. This repo implements that pattern locally, with Codex CLI processes as the subagents.
