from __future__ import annotations

import json
from typing import Any

from swarm_harness.records import Problem


ORCHESTRATOR_COMMANDS = """
Available orchestrator commands (Kimi-style):
- search(query): look up references or background material
- browse(url): fetch a web page for citations or definitions
- Graph_builder(prompt, task_id?): ask the standard Graph_builder to decompose the proof into one
  or more directed acyclic graphs of statements (different proof approaches when useful). Each node
  is a statement to prove; its incoming dependencies are the prior statements it may use as inputs.
- lemma_prover(prompt, task_id?, graph_id?, node_id?, model?, reasoning_effort?): ask the standard lemma prover to prove one
  lemma or conclusion node from a chosen Graph_builder DAG (use graph_id when multiple approaches
  were returned, and node_id when possible)
- proof_writer(statement, prompt?, task_id?, model?, reasoning_effort?): ask the standard proof writer to write a rigorous
  LaTeX proof of the given statement (not the whole problem unless that is the statement)
- final_proof_writer(prompt?, task_id?, model?, reasoning_effort?): ask the standard final proof writer to combine prior
  lemma/proof-writer LaTeX into one complete LaTeX document proving the original problem
- term_definer(term, prompt?, task_id?): ask the standard term definer to figure out what a single
  term or phrase from the problem statement means (its precise mathematical definition in context).
  Use this early when the problem uses notation or terminology you are unsure about.
- summarizer(prompt, task_id?): ask the standard summarizer to synthesize prior work
- code(instruction, language?): run a short snippet to verify a computation. Supported
  languages: python (default), sage (SageMath), macaulay2 (Macaulay2). Use SageMath for
  symbolic algebra and number theory; Macaulay2 for commutative algebra and Groebner bases;
  Python for general numeric or scripting checks.
- create_subagent(name, system_prompt): spin up a specialized subagent on demand
- assign_task(agent, prompt, model?, reasoning_effort?): delegate a subtask to a subagent you created
- critiquer(statement, proof, prompt?, task_id?): ask the standard critiquer to review a proof of
  the given statement and find gaps or revision tasks
- final_critiquer(proof, prompt?, task_id?): ask the standard final critiquer to review the final
  LaTeX proof document for whether it completely proves the original problem statement
- counterexample_finder(statement, prompt?, task_id?): ask the standard counterexample finder to
  stress-test a statement by actively searching for a counterexample (useful when a proof keeps
  failing or a critiquer rejects it—checks whether the statement itself might be false)
- finish(answer, confidence, reasoning_summary, caveats, sources): return the final answer
""".strip()

# Harmonic Aristotle (disabled until configured):
# - Aristotle_advisor(prompt, task_id?): read Aristotle docs and recommend whether/how to call Aristotle.
# - aristotle(prompt, mode?, ...): call the Harmonic Aristotle CLI for Lean formalization or sorry filling.


def agent_prompt(spec_system_prompt: str, task_prompt: str) -> str:
    return f"{spec_system_prompt.strip()}\n\n{task_prompt.strip()}\n"


def orchestrator_prompt(
    problem: Problem,
    transcript: list[dict[str, Any]],
    registered_agents: list[dict[str, Any]],
    step: int,
    max_steps: int,
    role_description: str,
    *,
    force_finish: bool = False,
) -> str:
    force_note = ""
    if force_finish:
        force_note = (
            "\n<force_finish>You have reached the step or time budget. This step you MUST emit exactly "
            "one finish action with your best answer.</force_finish>"
        )
    return f"""You are the ORCHESTRATOR of a self-directed math-solving swarm (Kimi-style).

{role_description}

There is NO predefined pipeline. You decide the entire workflow. The standard
Graph_builder/lemma_prover/proof_writer/final_proof_writer/summarizer/critiquer/final_critiquer/
counterexample_finder commands are optional conveniences, not required stages.
Decompose the problem into heterogeneous subtasks, create specialized subagents on demand,
assign work to them (multiple assignments in one step run in parallel), use tools to gather
or verify evidence, and finish once you have a rigorous, well-supported answer.

{ORCHESTRATOR_COMMANDS}

Respond with only JSON matching the provided schema: a short "thought" and an "actions" list.
Guidelines:
- Create a subagent before assigning it a task; reference it by the exact name you gave it.
- The standard roles are optional building blocks, not a required sequence. Pick whatever
  structure fits the problem—including fully custom subagents—and skip or reorder any of them.
- Worker agent calls (assign_task and standard-task actions) default to gpt-5.6-sol with high
  reasoning when you omit model/reasoning_effort. You may override per task.
- Reserve reasoning_effort="ultra" (with gpt-5.6-sol) for genuinely difficult mathematical
  reasoning: lemma_prover, proof_writer, final_proof_writer, and assign_task calls whose main job is
  proving nontrivial lemmas or constructing hard proof steps.
- For other worker tasks, the high default is often sufficient; pick a different model or
  reasoning effort only when you have a specific reason.
- If a requested model is at capacity, the harness automatically retries that same prompt on a
  simpler fallback model. Transcript results then include model_requested, model_used, and
  model_fallback_used=true (with model_fallback_reason). Treat model_used as the model that
  actually produced the output; if a fallback ran, weigh the result accordingly and re-run on a
  stronger model later if the work is critical.
- One pattern you may use if (and only if) it fits: Graph_builder can decompose a proof into one
  or more ProofFlow-style dependency DAGs (e.g. alternative approaches); pick a graph_id and use
  lemma_prover to prove individual nodes (in parallel where dependencies allow); proof_writer can
  draft a rigorous LaTeX proof of a stated claim;
  final_proof_writer can assemble prior LaTeX proofs into one complete document for the original
  problem; and summarizer/critiquer/final_critiquer can synthesize or check the work. Treat this
  as one option among many, not a prescribed workflow.
- When you use proof_writer, supply the exact statement to prove.
- When you use critiquer, supply the exact statement and the proof draft to review.
- When the problem uses unfamiliar or ambiguous terminology/notation, use term_definer early to pin
  down what a specific term means before decomposing or proving; supply the exact term.
- When you use final_proof_writer, call it after lemma_prover/proof_writer work so the transcript
  contains LaTeX proof fragments to combine.
- When you use final_critiquer, supply the LaTeX document produced by final_proof_writer.
- If an approach is not progressing (the same node keeps resisting proof, lemmas will not compose,
  or you are repeating failed attempts), it is often worth trying a different approach instead of
  pushing the stuck path: ask Graph_builder for alternative DAGs, switch graph_id, or prove a
  different set of lemmas.
- If a critiquer (or final_critiquer) rejects a proof, either send it back to proof_writer/
  lemma_prover to rewrite the proof addressing the specific issues raised, or use
  counterexample_finder to check whether the statement being proved might actually be false. If it
  appears false, revise the target statement or decomposition rather than trying to prove a false
  claim.
- Prefer spawning a few complementary specialists (e.g. a prover and an independent checker)
  and running them in parallel over doing everything yourself.
- Use search/browse/code only when they add real value.
- Emit "finish" alone, in its own step, when the aggregated work is correct and complete.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<registered_subagents>
{json.dumps(registered_agents, indent=2, ensure_ascii=False)}
</registered_subagents>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
<step>{step}</step>
<max_steps>{max_steps}</max_steps>{force_note}
"""


PROOF_WRITER_HELPER_COMMANDS = """
Available helper sub-subagents (invoke via JSON actions while you work):
- browse(url): delegate to a browse helper to fetch a web page for references, definitions, or citations
- code(instruction, language?): delegate to a code helper to verify a computation. Supported
  languages: python (default), sage (SageMath), macaulay2 (Macaulay2). Use SageMath for symbolic
  algebra and number theory; Macaulay2 for commutative algebra and Groebner bases; Python for
  general numeric or scripting checks.
- finish(...): return your final proof draft with task_id, title, approach, answer_fragment
  (LaTeX proof body), confidence, and assumptions
""".strip()


TERM_DEFINER_HELPER_COMMANDS = """
Available helper sub-subagents (invoke via JSON actions while you work):
- search(query): delegate to a search helper to find references, glossaries, or definitions
- browse(url): delegate to a browse helper to fetch a web page for an authoritative definition or citation
- code(instruction, language?): delegate to a code helper to check an example or computation. Supported
  languages: python (default), sage (SageMath), macaulay2 (Macaulay2).
- finish(...): return your final result with task_id, title, approach, answer_fragment (the definition
  of the term, in prose/LaTeX), confidence, and assumptions
""".strip()


ARISTOTLE_ADVISOR_HELPER_COMMANDS = """
Available helper sub-subagents (invoke via JSON actions while you work):
- search(query): delegate to a search helper to find current Aristotle SDK/API/CLI documentation
- browse(url): delegate to a browse helper to read Aristotle documentation pages
- code(instruction, language?): delegate to a code helper for lightweight local inspection only
- finish(...): return your recommendation with task_id, title, approach, answer_fragment,
  confidence, and assumptions
""".strip()


def aristotle_advisor_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
) -> str:
    return f"""You are the standard Aristotle advisor in a self-directed math-solving swarm.

Your job is to decide whether and exactly how this run should call Harmonic Aristotle. First read
current Aristotle API/SDK/CLI documentation using search and browse. Then recommend a concrete,
minimal Aristotle call only if it is appropriate for this problem and the available artifacts.

Respond with only JSON matching the provided schema: a short "thought" and an "actions" list.

{ARISTOTLE_ADVISOR_HELPER_COMMANDS}

Guidelines:
- Prefer Aristotle for Lean formalization, filling `sorry`s in an existing Lean project, or checking
  formal proof obligations. Do not recommend it just for ordinary informal proof drafting.
- Note that Aristotle jobs can take many hours; recommend using `wait=true` only when the run budget
  can tolerate it.
- If recommending `mode=submit`, identify the Lean project directory and the prompt to pass.
- If recommending `mode=formalize`, identify the source document and destination archive.
- State what must be checked after the run, especially searching returned Lean files for `sorry` or
  `admit` before treating the result as formally proved.
- If documentation is unavailable or access is unclear, recommend not calling Aristotle.
- Put your recommendation in answer_fragment. Include the proposed action fields: mode, prompt,
  project_dir or source_path, destination, wait, and expected runtime risk.
- Emit a single finish action (alone) once your recommendation is settled.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def term_definer_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
    *,
    term: str,
) -> str:
    return f"""You are the standard term definer in a self-directed math-solving swarm.

Your sole goal is to figure out what the term given in the term_to_define block below means,
precisely, as it is used in the problem. Determine its exact mathematical definition in this context (not a vague gloss),
note any standard notation, and flag it if the term is ambiguous or could mean different things in
different subfields. You may delegate lookups or checks to helper sub-subagents before you finish.
Respond with only JSON matching the provided schema: a short "thought" and an "actions" list.

{TERM_DEFINER_HELPER_COMMANDS}

Guidelines:
- Define exactly the given term; do not solve the problem or define unrelated terms.
- Put the definition in answer_fragment: a precise statement of what the term means in this context,
  including formal notation where helpful. Keep it self-contained.
- Prefer authoritative sources; use search/browse when the term is non-standard or context-dependent.
- If the term has multiple plausible meanings, state each and say which best fits the problem.
- Record anything you had to assume about the intended meaning in assumptions.
- Emit a single finish action (alone) once the definition is settled.
- Do not invent tool output; wait for helper results before relying on them.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<term_to_define>
{term}
</term_to_define>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def proof_writer_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
    *,
    statement: str,
) -> str:
    return f"""You are the standard proof writer in a self-directed math-solving swarm.

Write a rigorous, readable LaTeX proof of the statement below. You may use the overall problem and
transcript for context, but your proof must establish the given statement—not some broader claim
unless it follows as a corollary. You may delegate evidence gathering or computation to helper
sub-subagents before you finish. Respond with only JSON matching the provided schema: a short
"thought" and an "actions" list.

{PROOF_WRITER_HELPER_COMMANDS}

Guidelines:
- Prove exactly the statement in <statement_to_prove>; do not substitute a different target.
- Put the proof in answer_fragment as valid LaTeX (e.g. \\begin{{proof}}...\\end{{proof}}).
- Use standard math-mode notation; do not wrap the fragment in a full document preamble.
- Use browse when you need an external reference, definition, or citation.
- Use code when a short computation would strengthen or verify a step.
- Emit a single finish action (alone) once the proof is complete.
- Do not invent tool output; wait for helper results before relying on them.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<statement_to_prove>
{statement}
</statement_to_prove>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def agent_tools_step_prompt(
    base_prompt: str,
    helper_results: list[dict[str, Any]],
    *,
    step: int,
    max_steps: int,
    force_finish: bool = False,
) -> str:
    force_note = ""
    if force_finish:
        force_note = (
            "\n<force_finish>You have reached the helper step budget. This step you MUST emit "
            "exactly one finish action with your best proof draft.</force_finish>"
        )
    return f"""{base_prompt.strip()}

<helper_results>
{json.dumps(helper_results, indent=2, ensure_ascii=False, default=str)}
</helper_results>
<helper_step>{step}</helper_step>
<max_helper_steps>{max_steps}</max_helper_steps>{force_note}
"""


def subagent_task_prompt(problem: Problem, agent_name: str, task_id: str, task_prompt: str) -> str:
    return f"""You are the subagent "{agent_name}" in a self-directed math-solving swarm.

Complete only your assigned task. Be rigorous and concise. Return only JSON matching the
provided schema (task_id, title, approach, answer_fragment, confidence, assumptions).

When a computation would help, say so explicitly and name the best tool: Python for general
checks, SageMath (sage) for symbolic algebra/number theory, or Macaulay2 (macaulay2) for
commutative algebra. The orchestrator can run code in any of these languages to verify your work.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<task>
{task_prompt}
</task>
"""


def graph_builder_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
) -> str:
    return f"""You are the standard Graph_builder in a self-directed math-solving swarm.

Following the ProofFlow idea, decompose the mathematical work into one or more directed acyclic
graphs (DAGs) of statements to prove. When the problem admits genuinely different proof strategies,
return multiple graphs in `graphs` (each with its own graph_id, title, and nodes). When one approach
is clearly best, a single graph is fine. Each node should be an atomic or near-atomic mathematical
statement. The node's dependencies are exactly the incoming arrows: the previous statements,
hypotheses, definitions, or standard facts that may be used as inputs to prove that node.

Return only JSON matching the provided schema. Requirements:
- Include at least one graph in `graphs`. Each graph must be acyclic and its topological_order must
  list every node id in that graph exactly once.
- Use distinct graph_id values when offering multiple approaches (e.g. contradiction, direct,
  induction).
- Keep dependencies minimal but sufficient within each graph.
- Use theorem_hypothesis nodes for assumptions from the problem, definition nodes for introduced
  objects, lemma nodes for intermediate statements, and conclusion nodes for final targets.
- State each node clearly enough that a later lemma_prover, proof_writer, or subagent can try to
  prove it using only its dependencies and standard mathematical facts.
- Include a short proof_hint for lemma and conclusion nodes.
- Write a brief overall `summary` comparing or situating the offered approaches when there is more
  than one graph.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def summarizer_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
) -> str:
    return f"""You are the standard summarizer in a self-directed math-solving swarm.

Synthesize the strongest available work into a concise, rigorous answer. Use the transcript
as context, keep caveats explicit, and do not invent support not present in the work so far.
Return only JSON matching the provided schema (answer, confidence, reasoning_summary,
caveats, sources).

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def _graph_from_builder_output(output: dict[str, Any], *, graph_id: str | None = None) -> dict[str, Any] | None:
    """Select one graph from a Graph_builder output (multi-graph or legacy single-graph format)."""
    graphs = output.get("graphs")
    if isinstance(graphs, list) and graphs:
        if graph_id:
            for graph in graphs:
                if isinstance(graph, dict) and str(graph.get("graph_id") or "") == graph_id:
                    return graph
            return None
        if len(graphs) == 1 and isinstance(graphs[0], dict):
            return graphs[0]
        return None
    if isinstance(output.get("nodes"), list):
        return output
    return None


def extract_graph_builder_bundle(transcript: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most recent raw Graph_builder output from the orchestrator transcript."""
    for entry in reversed(transcript):
        for result in reversed(entry.get("results") or []):
            if str(result.get("action") or "") != "Graph_builder":
                continue
            output = result.get("output")
            if isinstance(output, dict) and (
                isinstance(output.get("graphs"), list) or isinstance(output.get("nodes"), list)
            ):
                return output
    return None


def extract_graph_builder_output(
    transcript: list[dict[str, Any]],
    *,
    graph_id: str | None = None,
) -> dict[str, Any] | None:
    """Return one Graph_builder DAG from the orchestrator transcript."""
    bundle = extract_graph_builder_bundle(transcript)
    if bundle is None:
        return None
    return _graph_from_builder_output(bundle, graph_id=graph_id)


def extract_lemma_proofs(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect prior lemma_prover outputs from the orchestrator transcript."""
    proofs: list[dict[str, Any]] = []
    for entry in transcript:
        for result in entry.get("results") or []:
            if str(result.get("action") or "") != "lemma_prover":
                continue
            output = result.get("output")
            if not isinstance(output, dict):
                continue
            proofs.append(
                {
                    "task_id": result.get("task_id"),
                    "node_id": output.get("node_id"),
                    "title": output.get("title"),
                    "answer_fragment": output.get("answer_fragment"),
                    "confidence": output.get("confidence"),
                }
            )
    return proofs


def _graph_node(graph: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("id") or "") == node_id:
            return node
    return None


def lemma_prover_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
    *,
    graph_id: str | None = None,
    node_id: str | None = None,
) -> str:
    bundle = extract_graph_builder_bundle(transcript)
    graph = extract_graph_builder_output(transcript, graph_id=graph_id)
    target_node: dict[str, Any] | None = None
    if graph is not None and node_id:
        target_node = _graph_node(graph, node_id)

    dependency_context: list[dict[str, Any]] = []
    if graph is not None and target_node is not None:
        nodes_by_id = {
            str(node.get("id") or ""): node
            for node in graph.get("nodes") or []
            if isinstance(node, dict)
        }
        for dep_id in target_node.get("dependencies") or []:
            dep = nodes_by_id.get(str(dep_id))
            if dep is not None:
                dependency_context.append(dep)

    proven_lemmas = extract_lemma_proofs(transcript)
    node_section = ""
    if target_node is not None:
        node_section = f"""
<target_node>
{json.dumps(target_node, indent=2, ensure_ascii=False)}
</target_node>
<dependency_nodes>
{json.dumps(dependency_context, indent=2, ensure_ascii=False)}
</dependency_nodes>
"""
    elif graph is not None:
        node_section = f"""
<proof_dag graph_id="{graph.get("graph_id", "")}">
{json.dumps(graph, indent=2, ensure_ascii=False, default=str)}
</proof_dag>
"""
    elif bundle is not None:
        node_section = f"""
<graph_builder_output>
{json.dumps(bundle, indent=2, ensure_ascii=False, default=str)}
</graph_builder_output>
"""

    return f"""You are the standard lemma prover in a self-directed math-solving swarm.

Prove exactly one lemma or conclusion node suggested by the Graph_builder. You may use only
the target node's dependency statements, prior lemma proofs listed below, and standard
mathematical facts. You may delegate web lookups or computations to helper sub-subagents
before you finish. Respond with only JSON matching the provided schema: a short "thought"
and an "actions" list.

{PROOF_WRITER_HELPER_COMMANDS}

Guidelines:
- Focus on the assigned node; do not reprove the entire theorem unless this node is the conclusion.
- When multiple proof DAGs were returned, use only the graph matching graph_id (if provided).
- Put the proof in answer_fragment as valid LaTeX (e.g. \\begin{{proof}}...\\end{{proof}}).
- Cite dependency nodes and prior lemma proofs explicitly when you use them.
- Use browse when you need an external reference; use code when a computation would verify a step.
- Emit a single finish action (alone) once the lemma proof is complete.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<graph_id>{graph_id or ""}</graph_id>
<node_id>{node_id or ""}</node_id>
<task>
{task_prompt}
</task>{node_section}
<proven_lemmas>
{json.dumps(proven_lemmas, indent=2, ensure_ascii=False, default=str)}
</proven_lemmas>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def critiquer_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
    *,
    statement: str,
    proof: str,
) -> str:
    return f"""You are the standard critiquer in a self-directed math-solving swarm.

Critique the proof below for whether it rigorously establishes the given statement. Check for
mathematical gaps, hidden assumptions, edge cases, and unsupported leaps. Prefer precise,
actionable revision tasks over vague concerns. Return only JSON matching the provided schema
(approved, score, strongest_task_id, issues, revision_tasks).

Guidelines:
- Evaluate whether the proof actually establishes <statement_to_prove>; do not critique a
  different claim.
- Focus on the supplied proof in <proof_to_critique>; use the overall problem and transcript
  only for context.
- Do not request re-computation of results already verified with code.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<statement_to_prove>
{statement}
</statement_to_prove>
<proof_to_critique>
{proof}
</proof_to_critique>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


COUNTEREXAMPLE_HELPER_COMMANDS = """
Available helper sub-subagents (invoke via JSON actions while you work):
- browse(url): delegate to a browse helper to look up definitions, known results, or counterexamples
- code(instruction, language?): delegate to a code helper to search for a counterexample numerically
  or symbolically. Supported languages: python (default), sage (SageMath), macaulay2 (Macaulay2).
- finish(...): return your verdict with task_id, title, approach, answer_fragment (the verdict plus
  any explicit counterexample), confidence, and assumptions
""".strip()


def counterexample_finder_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
    *,
    statement: str,
) -> str:
    return f"""You are the standard counterexample finder in a self-directed math-solving swarm.

Your job is to stress-test the statement below by actively trying to DISPROVE it. You are not
trying to prove it—you are trying to break it. Search for counterexamples (small cases, boundary
and degenerate cases, extreme parameters) using code or references before you conclude. Respond
with only JSON matching the provided schema: a short "thought" and an "actions" list.

{COUNTEREXAMPLE_HELPER_COMMANDS}

Guidelines:
- Prefer concrete computational search (code) for a counterexample whenever the statement is
  checkable on small or random instances.
- In answer_fragment, begin with a clear verdict, exactly one of: "FALSE" (with an explicit
  counterexample), "LIKELY TRUE" (no counterexample found after a genuine search), or
  "INCONCLUSIVE" (could not check meaningfully).
- If FALSE, give the smallest/clearest counterexample and explain precisely how it violates the
  statement.
- Set confidence to reflect how thorough the search actually was, not what you hope is true.
- Do not invent tool output; wait for helper results before relying on them.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<statement_to_check>
{statement}
</statement_to_check>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def extract_prior_proofs(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect prior lemma_prover and proof_writer LaTeX outputs from the transcript."""
    proofs: list[dict[str, Any]] = []
    for entry in transcript:
        for result in entry.get("results") or []:
            action = str(result.get("action") or "")
            if action not in {"lemma_prover", "proof_writer"}:
                continue
            output = result.get("output")
            if not isinstance(output, dict):
                continue
            latex_proof = output.get("answer_fragment")
            if not latex_proof:
                continue
            proofs.append(
                {
                    "action": action,
                    "task_id": result.get("task_id"),
                    "node_id": output.get("node_id"),
                    "title": output.get("title"),
                    "approach": output.get("approach"),
                    "latex_proof": latex_proof,
                    "confidence": output.get("confidence"),
                }
            )
    return proofs


def final_proof_writer_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
) -> str:
    prior_proofs = extract_prior_proofs(transcript)
    return f"""You are the standard final proof writer in a self-directed math-solving swarm.

Combine the prior LaTeX proof fragments below into one complete, self-contained LaTeX document
that proves the original problem statement. Fill any gaps needed for a reader to follow the full
argument from hypotheses to conclusion. Return only JSON matching the provided schema (task_id,
title, approach, latex_document, confidence, assumptions).

Guidelines:
- The latex_document must be a compilable LaTeX article (include \\documentclass, amsmath/amsthm
  or equivalent, \\begin{{document}} ... \\end{{document}}).
- State the original problem clearly as the main theorem or proposition being proved.
- Reuse, reorganize, and connect prior lemma/proof-writer fragments; do not silently drop steps.
- Resolve notation consistently across fragments and add brief transitions where needed.
- Do not invent new mathematics beyond what is needed to stitch the prior work together.

<problem_id>{problem.id}</problem_id>
<original_statement>
{problem.statement}
</original_statement>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<task>
{task_prompt}
</task>
<prior_latex_proofs>
{json.dumps(prior_proofs, indent=2, ensure_ascii=False, default=str)}
</prior_latex_proofs>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def final_critiquer_task_prompt(
    problem: Problem,
    task_id: str,
    task_prompt: str,
    transcript: list[dict[str, Any]],
    *,
    proof: str,
) -> str:
    return f"""You are the standard final critiquer in a self-directed math-solving swarm.

Critique whether the LaTeX proof document below rigorously establishes the original problem
statement. Check for mathematical gaps, missing lemmas, hidden assumptions, edge cases, notation
inconsistencies, and unsupported leaps. Prefer precise, actionable revision tasks over vague
concerns. Return only JSON matching the provided schema (approved, score, strongest_task_id,
issues, revision_tasks).

Guidelines:
- Evaluate whether the document proves <original_statement>; do not critique a different claim.
- Focus on the supplied LaTeX in <final_latex_proof>; use the transcript only for context.
- Check that prior lemma/proof fragments were integrated faithfully, not oversimplified away.
- Do not request re-computation of results already verified with code.

<problem_id>{problem.id}</problem_id>
<original_statement>
{problem.statement}
</original_statement>
<problem>
{problem.statement}
</problem>
<task_id>{task_id}</task_id>
<final_latex_proof>
{proof}
</final_latex_proof>
<task>
{task_prompt}
</task>
<transcript>
{json.dumps(transcript, indent=2, ensure_ascii=False, default=str)}
</transcript>
"""


def tex_artifact_prompt(problem: Problem, solution_markdown: str) -> str:
    return f"""Convert the final saved Markdown solution into a polished, compilable LaTeX article.

Return only JSON matching the provided schema (task_id, title, latex_document, confidence, notes).

Requirements for latex_document:
- It must be a complete LaTeX document with \\documentclass, needed packages, \\begin{{document}},
  and \\end{{document}}.
- It must start the body with a clear TeX version of the original question under a heading such
  as \\section*{{Question}}.
- The question section must preserve the meaning of the original statement. Convert plain-text
  math notation into TeX notation where appropriate.
- The question section must be followed by a \\section*{{Solution}} containing a rigorous TeX
  writeup of the solution.md content.
- Clean up Markdown syntax, but do not change the mathematical content or add unsupported claims.
- Prefer standard packages such as amsmath, amssymb, amsthm, and geometry.

<problem_id>{problem.id}</problem_id>
<problem>
{problem.statement}
</problem>
<solution_markdown>
{solution_markdown}
</solution_markdown>
"""
