# KimiStyle MathSolver

A local agentic math harness inspired by Kimi K2.5 Agent Swarm, but with each worker implemented as a `codex exec` terminal call instead of a Kimi model call.

The orchestrator is **self-directed**: instead of a hard-coded pipeline, an `orchestrator` agent decides the workflow at runtime. Each step it can:

1. `create_subagent(name, system_prompt)` — spin up a specialized subagent on demand.
2. `assign_task(agent, prompt)` — delegate a subtask (assignments in the same step run in parallel).
3. `archive_subagent(name)` / `activate_subagent(name)` — keep completed specialists out of the
   active prompt working set without deleting their persisted definitions.
4. `Graph_builder(prompt, task_id?)` — produce one or more ProofFlow-style directed acyclic graphs of statements to prove (alternative approaches when useful), where incoming arrows identify the prior statements each node may use as inputs.
5. `proof_writer(statement, prompt?, task_id?)` / `final_proof_writer(prompt?, task_id?)` / `summarizer(prompt, task_id?)` / `critiquer(statement, proof, prompt?, task_id?)` / `final_critiquer(proof, prompt?, task_id?)` — run standard math-swarm task agents when those common roles are useful. Prior proof writers output LaTeX in `answer_fragment`; `final_proof_writer` assembles a complete LaTeX document.
6. `search` / `browse` / `code` — gather or verify evidence with tools.
7. `fact_upsert` / `fact_delete` / `implication_upsert` / `implication_delete` — maintain the
   shared true-facts and implications graph.
8. `finish(...)` — return the final answer once the aggregated work is rigorous.

There are no predefined stages; the orchestrator chooses how to decompose the problem, which subagents or standard task agents to invoke, what to parallelize, and when to stop (bounded by `max_steps` and `max_runtime_minutes`).

The implementation mirrors the useful ProofCouncil pattern: terminal agents are subprocesses, prompts are piped through stdin, Codex JSONL is captured for usage, and all artifacts are written to inspectable run directories.

## Shared Storage

Each run exposes three logical global storage areas to the orchestrator and every math agent:

1. `transcript.json` is the compact chronological log. Agent results contain a bounded summary
   and an `output_file` pointer instead of duplicating the complete output.
2. `agents/` contains each call's complete prompt, response, parsed JSON, canonical `output.json`,
   and call metadata. `agents/index.json` gives the orchestrator and workers a bounded catalog of
   those files so they can inspect detailed prior work when the transcript summary is insufficient.
3. `global_facts/graph.json` stores shared true-fact nodes and implication hyperedges. Every agent
   receives a bounded graph view and can declare audited edits. `global_facts/history.jsonl` records
   all accepted and rejected edits, while `global_facts/checkpoints/` keeps the graph aligned with
   step-based resume.

Facts record a `statement`, a `justification`, and a kind (`hypothesis`, `definition`, `derived`, or
`external`). An implication connects one or more `premise_fact_ids` to a `conclusion_fact_id` and
records its justification. Deleting a fact also deletes every implication incident to it. The store
is collaboratively maintained rather than verifier-gated: agents are instructed to correct or
delete entries when later work exposes an error.

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
  -m gpt-6-astra \
  -c 'model_reasoning_effort="high"' \
  --sandbox read-only -C <agent-workspace> -o <last-message-file> \
  --output-schema <schema> -
```

Override the default model (`gpt-6-astra`) with `--model` or `CODEX_SWARM_MODEL`.
Override the default worker reasoning effort (`high`) with `--reasoning-effort` or
`CODEX_SWARM_REASONING_EFFORT`. The orchestrator loop ignores the worker effort default and
always runs at `xhigh` (extra high).

## Claude backend

`--backend claude` runs each worker as a Claude Code CLI call instead of a Codex one.
Requires a logged-in `claude` CLI (`claude` then `/login`, or `ANTHROPIC_API_KEY`).

```bash
python3 scripts/run_swarm.py solve \
  --backend claude \
  --problem-id sqrt2 \
  --problem-text "Prove that sqrt(2) is irrational."
```

The Claude backend calls:

```bash
claude -p --output-format json \
  --model claude-sonnet-5 \
  --effort medium \
  --json-schema '<schema contents>' \
  --disallowedTools Edit Write NotebookEdit -
```

Differences from the Codex backend worth knowing:

- The schema is passed **inline** (`--json-schema`), not as a path. The same files in
  `schemas/` are used, read at call time.
- There is no `-o <last-message-file>`; the answer is the `result` field of the JSON
  envelope on stdout, saved to `response.md` as usual.
- Capacity fallback is delegated to Claude's own `--fallback-model` rather than the
  retry loop the Codex backend implements. Override with `--capacity-fallback-model`
  or `CLAUDE_SWARM_FALLBACK_MODELS`.
- `--codex-sandbox` is reused for permissions: `read-only` maps to
  `--disallowedTools Edit Write NotebookEdit`, `workspace-write` to
  `--permission-mode acceptEdits`, and `bypass` to `--dangerously-skip-permissions`.
- Model defaults to `claude-sonnet-5`; override with `--model` or `CLAUDE_SWARM_MODEL`.
  `--claude-executable` / `CLAUDE_SWARM_EXECUTABLE` selects the binary.
- `--claude-bare` runs workers with `--bare`. It requires `ANTHROPIC_API_KEY` because
  `--bare` never reads OAuth or keychain credentials, so subscription logins fail under it.

## Mixing Claude models into a Codex run

`--claude-models` (or `SWARM_CLAUDE_MODELS=1`) keeps Codex as the default worker backend but
lets the orchestrator send individual calls to Claude by naming a Claude model on an action:

```bash
python3 scripts/run_swarm.py solve \
  --claude-models \
  --problem-id sqrt2 \
  --problem-text "Prove that sqrt(2) is irrational."
```

Any action whose `model` starts with `claude` is routed to the Claude backend; everything else
goes to Codex. So the orchestrator emitting

```json
{"type": "critiquer", "statement": "...", "proof": "...", "model": "claude-sonnet-5"}
```

runs that one critique on Claude Sonnet 5 while the rest of the swarm stays on `gpt-6-astra`.
Claude-routed calls default to `reasoning_effort="medium"`; GPT workers default to `high`. The
orchestrator may override per call,
and Codex-only effort names are mapped (`ultra` becomes `max`, `minimal` becomes `low`).

The orchestrator is told about this only when the flag is on, so runs without it are unchanged.
The main use is an independent second opinion — an adversarial critiquer, or a parallel prover on a
hard lemma — where a different model family is valuable precisely because it fails differently.

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

The orchestrator decision loop defaults to `gpt-6-astra` at `xhigh` (extra high) reasoning.
Worker agents also run on `gpt-6-astra` but default to `high`, unless
the orchestrator sets `model` and `reasoning_effort` on an individual `assign_task` or standard
task action. It is instructed to use ultra for difficult mathematical work (lemma proving, proof
writing, proof assembly). The gpt-6-astra effort ladder is low, medium, high, xhigh, max, ultra.

If a worker call fails because the selected model is at capacity, the Codex backend automatically
retries the same prompt on a simpler fallback model (default `gpt-5.4`). Override the fallback
chain with repeated `--capacity-fallback-model` flags or
`CODEX_SWARM_CAPACITY_FALLBACK_MODELS` (comma-separated). Transcript results record
`model_requested`, `model_used`, and `model_fallback_used` so the orchestrator can see which
model actually produced the output.

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
- `workflow_runs/<problem-id>-<run-id>/` with the compact transcript, indexed agent files, shared
  fact graph, prompts, responses, events, `solution.md`, `solution.tex`, and compiled `solution.pdf`

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

## Notes On The Inspiration

Kimi K2.5's report describes Agent Swarm as a framework where an orchestrator decomposes complex tasks into heterogeneous subtasks and runs subagents concurrently. This repo implements that system idea locally with Codex CLI processes as the subagents.
