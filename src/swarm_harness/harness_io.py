from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from swarm_harness.latex_artifacts import compile_tex_to_pdf, fallback_latex_document
from swarm_harness.records import Problem
from swarm_harness.util import append_jsonl, read_json, safe_id, write_json


def read_problems(input_path: Path) -> list[Problem]:
    raw = read_json(input_path)
    if isinstance(raw, dict):
        items = raw.get("problems")
        if items is None:
            items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("Input JSON must be a problem object, a list, or an object with a problems list.")

    if not isinstance(items, list):
        raise ValueError("The problems field must be a list.")

    problems: list[Problem] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Problem {index} must be an object.")
        raw_id = str(item.get("id") or item.get("problem_id") or f"problem-{index}")
        statement = (
            item.get("latex")
            or item.get("statement")
            or item.get("problem")
            or item.get("text")
            or item.get("prompt")
        )
        if not statement:
            raise ValueError(f"Problem {raw_id!r} is missing latex/statement/problem/text.")
        metadata = {key: value for key, value in item.items() if key not in {"id", "problem_id", "latex", "statement", "problem", "text", "prompt"}}
        problems.append(Problem(id=safe_id(raw_id, fallback=f"problem-{index}"), statement=str(statement), metadata=metadata))
    return problems


def write_batch_outputs(output_dir: Path, results: list[dict[str, Any]], warnings: list[str] | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    solutions = []
    for result in results:
        problem_id = str(result.get("problem_id") or "problem")
        answer = str(result.get("answer") or "")
        output_stem = safe_id(problem_id)
        markdown_path = output_dir / f"{output_stem}.md"
        tex_path = output_dir / f"{output_stem}.tex"
        pdf_path = output_dir / f"{output_stem}.pdf"
        solutions.append(
            {
                "id": problem_id,
                "status": result.get("status", "partial"),
                "answer": answer,
                "confidence": result.get("confidence", 0.0),
                "run_id": result.get("run_id"),
                "run_dir": result.get("run_dir"),
                "solution_md_path": str(markdown_path),
                "solution_tex_path": str(tex_path),
                "solution_pdf_path": str(pdf_path),
            }
        )
        write_json(output_dir / f"{output_stem}.json", result)
        markdown_path.write_text(answer.rstrip() + "\n", encoding="utf-8")
        source_tex_raw = result.get("solution_tex_path")
        source_pdf_raw = result.get("solution_pdf_path")
        source_tex = Path(str(source_tex_raw)) if source_tex_raw else None
        source_pdf = Path(str(source_pdf_raw)) if source_pdf_raw else None
        if source_tex is not None and source_tex.is_file():
            shutil.copyfile(source_tex, tex_path)
        else:
            statement = str(result.get("problem_statement") or result.get("statement") or problem_id)
            tex_path.write_text(fallback_latex_document(statement, answer), encoding="utf-8")
        if source_pdf is not None and source_pdf.is_file():
            shutil.copyfile(source_pdf, pdf_path)
        else:
            compile_result = compile_tex_to_pdf(tex_path)
            write_json(output_dir / f"{output_stem}_pdf_compile.json", compile_result)

    write_json(output_dir / "solutions.json", solutions)
    write_json(
        output_dir / "run_summary.json",
        {
            "status": "done",
            "problem_count": len(results),
            "warnings": warnings or [],
            "solutions_path": str(output_dir / "solutions.json"),
        },
    )

    usage_path = output_dir / "token_usage.jsonl"
    if usage_path.exists():
        usage_path.unlink()
    for result in results:
        append_jsonl(
            usage_path,
            {
                "problem_id": result.get("problem_id"),
                "run_id": result.get("run_id"),
                **(result.get("usage") if isinstance(result.get("usage"), dict) else {}),
            },
        )
