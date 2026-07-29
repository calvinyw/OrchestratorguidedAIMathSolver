# KimiStyle MathSolver

This repo is a small Python harness for running Codex CLI as a swarm of math-solving agents.

## Commands

- Run the mock smoke harness: `./smoke/run_local.sh`
- Run one mock problem: `python3 scripts/run_swarm.py solve --backend mock --problem-text "Prove that sqrt(2) is irrational."`
- Resume a run from orchestrator step N: `python3 scripts/run_swarm.py solve --backend mock --run-dir outputs/workflow_runs/<run_id> --resume-from-step N` (add `--reuse-decision` to replay the saved `orchestrator_step_NN.json` instead of re-calling the orchestrator)
- Run tests: `python3 -m unittest discover -s tests`

The package has no runtime dependencies outside the Python standard library. Real agent runs require the `codex` CLI to be installed and authenticated on the machine.

## Design Notes

- Keep agent outputs as JSON whenever possible. Codex calls should use `codex exec --json --output-schema ... -o ... -`.
- Preserve trace artifacts under `outputs/` or the configured harness output directory.
- Use the mock backend for tests and cheap smoke checks; do not make real Codex calls from tests.

## Orchestrator Commands

The orchestrator is **self-directed** (Kimi-style): there are no fixed
planner→solver→critic→synthesizer stages. `solve()` runs a decision loop where an `orchestrator`
agent chooses actions each step until it emits `finish`. During an active run, `SwarmOrchestrator`
exposes these commands:

- `search(query)` — web search (mocked with `MockBackend`, live DuckDuckGo HTML otherwise)
- `browse(url)` — fetch a page
- `code(instruction, language?)` — run a short snippet (`python`, `sage`, or `macaulay2`); normally
  saved in the run workspace, or under the selected codebase when `--save-code` is enabled
- `Aristotle_advisor(prompt, task_id?)` — read current Harmonic Aristotle docs and recommend whether/how to call Aristotle for Lean formalization or sorry filling; this must run before `aristotle`
- `aristotle(prompt, mode?, project_dir?, source_path?, destination?, wait?)` — optionally call the Aristotle CLI for long-running Lean formalization or sorry filling after advisor approval
- `create_subagent(name, system_prompt)` — spin up a specialized subagent on demand (persisted to `subagents.json`)
- `assign_task(agent, prompt)` — delegate work to a subagent it created (parallel within a step)
- `Graph_builder(prompt, task_id?)` — build one or more ProofFlow-style directed acyclic graphs of statements to prove (alternative approaches when useful); each node may use only the statements with arrows pointing into it as inputs
- `lemma_prover(prompt, task_id?, graph_id?, node_id?)` — prove one lemma or conclusion node from a chosen Graph_builder DAG; parallelize independent lemma nodes across actions in the same step
- `proof_writer(statement, prompt?, task_id?)` — ask the standard proof writer to prove the given statement in LaTeX
- `final_proof_writer(prompt?, task_id?)` — combine prior LaTeX proofs into one complete document for the original problem
- `term_definer(term, prompt?, task_id?)` — ask the standard term definer to figure out what a single term or phrase from the problem means (its precise definition in context); it can `search`/`browse`/`code` to pin down the meaning
- `summarizer(prompt, task_id?)` — ask the standard summarizer to synthesize prior work
- `critiquer(statement, proof, prompt?, task_id?)` — ask the standard critiquer to review a proof of the given statement
- `final_critiquer(proof, prompt?, task_id?)` — ask the standard final critiquer to review the assembled LaTeX proof document

The orchestrator decides the whole workflow: which subagents to spawn, how to decompose the
problem, whether to build a proof DAG, which standard task agents to invoke, what to run in
parallel, and when to finish.
Per-run artifacts include
`orchestrator_step_NN.json` (each decision) and `transcript.json` (actions + results;
checkpointed after every step so interrupted runs can resume).
`max_steps` (config or `--max-steps`) and `max_runtime_minutes` (config or `--max-runtime-minutes`) bound the loop.
Resume with `--run-dir` + `--resume-from-step N` to keep steps `< N` and continue the decision loop from step `N`.
