from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def compile_tex_to_pdf(tex_path: Path, *, timeout_s: int = 180) -> dict[str, Any]:
    tex_path = tex_path.resolve()
    if not tex_path.exists():
        return {"ok": False, "error": f"TeX file does not exist: {tex_path}", "pdf_path": str(tex_path.with_suffix(".pdf"))}

    latexmk = shutil.which("latexmk")
    if latexmk:
        command = [
            latexmk,
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            f"-outdir={tex_path.parent}",
            tex_path.name,
        ]
        return _run_compile(command, tex_path=tex_path, timeout_s=timeout_s, engine="latexmk")

    pdflatex = shutil.which("pdflatex")
    if pdflatex:
        command = [
            pdflatex,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            f"-output-directory={tex_path.parent}",
            tex_path.name,
        ]
        first = _run_compile(command, tex_path=tex_path, timeout_s=timeout_s, engine="pdflatex")
        if not first["ok"]:
            return first
        second = _run_compile(command, tex_path=tex_path, timeout_s=timeout_s, engine="pdflatex")
        return {**second, "rerun": True}

    return {
        "ok": False,
        "engine": None,
        "command": [],
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "pdf_path": str(tex_path.with_suffix(".pdf")),
        "error": "No TeX compiler found. Install latexmk or pdflatex to compile solution.tex.",
    }


def fallback_latex_document(problem_statement: str, solution_markdown: str) -> str:
    return (
        "\\documentclass[11pt]{article}\n"
        "\\usepackage[utf8]{inputenc}\n"
        "\\usepackage[T1]{fontenc}\n"
        "\\usepackage{amsmath,amssymb,amsthm}\n"
        "\\usepackage[margin=1in]{geometry}\n"
        "\\begin{document}\n"
        "\\section*{Question}\n"
        f"{_latex_text(problem_statement)}\n\n"
        "\\section*{Solution}\n"
        f"{_markdownish_to_latex(solution_markdown)}\n"
        "\\end{document}\n"
    )


def _run_compile(command: list[str], *, tex_path: Path, timeout_s: int, engine: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=tex_path.parent,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "engine": engine,
            "command": command,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "pdf_path": str(tex_path.with_suffix(".pdf")),
            "error": f"TeX compilation timed out after {timeout_s}s.",
        }

    pdf_path = tex_path.with_suffix(".pdf")
    ok = completed.returncode == 0 and pdf_path.exists()
    return {
        "ok": ok,
        "engine": engine,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "pdf_path": str(pdf_path),
        "error": None if ok else f"{engine} failed with status {completed.returncode}.",
    }


def _markdownish_to_latex(text: str) -> str:
    blocks: list[str] = []
    in_code = False
    code_lines: list[str] = []

    for raw_line in text.rstrip().splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                blocks.append("\\begin{verbatim}\n" + "\n".join(code_lines) + "\n\\end{verbatim}")
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            blocks.append("")
            continue
        if line.startswith("\\"):
            blocks.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            command = "subsection" if len(heading.group(1)) <= 2 else "subsubsection"
            blocks.append(f"\\{command}*{{{_latex_text(heading.group(2))}}}")
            continue
        if "\\" in line or "$" in line:
            blocks.append(_strip_markdown_emphasis(line))
            continue
        blocks.append(_latex_text(line))

    if in_code:
        blocks.append("\\begin{verbatim}\n" + "\n".join(code_lines) + "\n\\end{verbatim}")
    return "\n".join(blocks).rstrip() + "\n"


def _latex_text(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _strip_markdown_emphasis(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    return text
