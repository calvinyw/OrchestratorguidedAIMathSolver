from __future__ import annotations

import asyncio
import html
import re
import time
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from swarm_harness.util import append_jsonl, safe_id, utc_now, write_json


class OrchestratorTools:
    """Kimi-style orchestrator tools: search, browse, code, and optional Aristotle."""

    SUPPORTED_CODE_LANGUAGES = ("python", "sage", "macaulay2")

    def __init__(
        self,
        *,
        run_dir: Path,
        trace_path: Path,
        mock: bool = False,
        browse_timeout_s: int = 30,
        code_timeout_s: int = 60,
        aristotle_enabled: bool = False,
        aristotle_executable: str = "aristotle",
        aristotle_timeout_s: int = 8 * 60 * 60,
        codebase_dir: Path | None = None,
        save_code: bool = False,
    ) -> None:
        self.run_dir = run_dir
        self.trace_path = trace_path
        self.mock = mock
        self.browse_timeout_s = browse_timeout_s
        self.code_timeout_s = code_timeout_s
        self.aristotle_enabled = aristotle_enabled
        self.aristotle_executable = aristotle_executable
        self.aristotle_timeout_s = aristotle_timeout_s
        self.codebase_dir = codebase_dir.resolve() if codebase_dir is not None else None
        self.save_code = bool(save_code)
        if self.save_code and self.codebase_dir is None:
            raise ValueError("save_code requires a codebase_dir.")
        self.saved_code_dir = (
            self.codebase_dir / "swarm_code" / safe_id(run_dir.name)
            if self.save_code and self.codebase_dir is not None
            else None
        )
        self.tools_dir = run_dir / "tools"
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0

    def set_call_counter(self, value: int) -> None:
        self._counter = max(0, int(value))

    async def search(self, query: str) -> dict[str, Any]:
        """Search for information relevant to the orchestrator's current task."""
        call_id = self._next_call_id("search")
        started = time.monotonic()
        append_jsonl(
            self.trace_path,
            {"type": "orchestrator.search", "call_id": call_id, "query": query, "at": utc_now()},
        )
        if self.mock:
            result = _mock_search(query)
        else:
            result = await asyncio.to_thread(_real_search, query, self.browse_timeout_s)
        artifact = {
            "tool": "search",
            "call_id": call_id,
            "query": query,
            "result": result,
            "duration_seconds": time.monotonic() - started,
            "at": utc_now(),
        }
        write_json(self.tools_dir / f"{call_id}.json", artifact)
        append_jsonl(self.trace_path, {"type": "orchestrator.search.end", **artifact})
        return result

    async def browse(self, url: str) -> dict[str, Any]:
        """Fetch and summarize a web page."""
        call_id = self._next_call_id("browse")
        started = time.monotonic()
        append_jsonl(
            self.trace_path,
            {"type": "orchestrator.browse", "call_id": call_id, "url": url, "at": utc_now()},
        )
        if self.mock:
            result = _mock_browse(url)
        else:
            result = await asyncio.to_thread(_real_browse, url, self.browse_timeout_s)
        artifact = {
            "tool": "browse",
            "call_id": call_id,
            "url": url,
            "result": result,
            "duration_seconds": time.monotonic() - started,
            "at": utc_now(),
        }
        write_json(self.tools_dir / f"{call_id}.json", artifact)
        append_jsonl(self.trace_path, {"type": "orchestrator.browse.end", **artifact})
        return result

    async def code(
        self,
        instruction: str,
        *,
        language: str = "python",
        workspace: Path | None = None,
    ) -> dict[str, Any]:
        """Run a short snippet in an isolated workspace to verify a computation."""
        call_id = self._next_call_id("code")
        started = time.monotonic()
        normalized_language = normalize_code_language(language)
        if self.saved_code_dir is not None:
            workspace = self.saved_code_dir / call_id
        else:
            workspace = workspace or (self.tools_dir / call_id)
        workspace.mkdir(parents=True, exist_ok=True)
        script_path = _code_script_path(workspace, normalized_language)
        append_jsonl(
            self.trace_path,
            {
                "type": "orchestrator.code",
                "call_id": call_id,
                "instruction": instruction,
                "language": normalized_language,
                "workspace": str(workspace),
                "save_code": self.save_code,
                "saved_code_path": str(script_path) if self.save_code else None,
                "at": utc_now(),
            },
        )
        if self.mock:
            if self.save_code:
                _write_code_script(script_path, instruction)
            result = _mock_code(instruction, normalized_language)
            if self.save_code:
                result["saved_code_path"] = str(script_path)
        else:
            result = await _real_code(
                instruction, normalized_language, workspace, self.code_timeout_s, cwd=self.codebase_dir
            )
            if self.save_code:
                result["saved_code_path"] = str(script_path)
        artifact = {
            "tool": "code",
            "call_id": call_id,
            "instruction": instruction,
            "language": normalized_language,
            "workspace": str(workspace),
            "codebase": str(self.codebase_dir) if self.codebase_dir else None,
            "save_code": self.save_code,
            "saved_code_path": str(script_path) if self.save_code else None,
            "result": result,
            "duration_seconds": time.monotonic() - started,
            "at": utc_now(),
        }
        write_json(self.tools_dir / f"{call_id}.json", artifact)
        append_jsonl(self.trace_path, {"type": "orchestrator.code.end", **artifact})
        return result

    async def aristotle(
        self,
        prompt: str,
        *,
        mode: str = "submit",
        project_dir: Path | None = None,
        source_path: Path | None = None,
        destination: Path | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Run the optional Aristotle CLI for Lean formalization or sorry filling."""
        call_id = self._next_call_id("aristotle")
        started = time.monotonic()
        workspace = self.tools_dir / call_id
        workspace.mkdir(parents=True, exist_ok=True)
        append_jsonl(
            self.trace_path,
            {
                "type": "orchestrator.aristotle",
                "call_id": call_id,
                "mode": mode,
                "prompt": prompt,
                "project_dir": str(project_dir) if project_dir else None,
                "source_path": str(source_path) if source_path else None,
                "destination": str(destination) if destination else None,
                "wait": wait,
                "at": utc_now(),
            },
        )
        if self.mock:
            result = _mock_aristotle(prompt, mode)
        elif not self.aristotle_enabled:
            result = {
                "ok": False,
                "error": "Aristotle tool is disabled. Re-run with --enable-aristotle and ARISTOTLE_API_KEY set.",
            }
        else:
            result = await _real_aristotle(
                self.aristotle_executable,
                prompt,
                mode=mode,
                project_dir=project_dir,
                source_path=source_path,
                destination=destination,
                wait=wait,
                workspace=workspace,
                timeout_s=self.aristotle_timeout_s,
            )
        artifact = {
            "tool": "aristotle",
            "call_id": call_id,
            "mode": mode,
            "prompt": prompt,
            "project_dir": str(project_dir) if project_dir else None,
            "source_path": str(source_path) if source_path else None,
            "destination": str(destination) if destination else None,
            "wait": wait,
            "result": result,
            "duration_seconds": time.monotonic() - started,
            "at": utc_now(),
        }
        write_json(self.tools_dir / f"{call_id}.json", artifact)
        append_jsonl(self.trace_path, {"type": "orchestrator.aristotle.end", **artifact})
        return result

    def _next_call_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:03d}"


def _mock_search(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "results": [
            {
                "title": f"Reference for: {query}",
                "snippet": "Mock search result for offline tests and smoke runs.",
                "url": "https://example.com/mock",
            }
        ],
        "mock": True,
    }


def _mock_browse(url: str) -> dict[str, Any]:
    return {
        "url": url,
        "title": "Mock page",
        "content": f"Mock browse content for {url}.",
        "mock": True,
    }


def _mock_code(instruction: str, language: str = "python") -> dict[str, Any]:
    return {
        "instruction": instruction,
        "language": language,
        "stdout": "mock output",
        "stderr": "",
        "returncode": 0,
        "mock": True,
    }


def _mock_aristotle(prompt: str, mode: str) -> dict[str, Any]:
    return {
        "ok": True,
        "mode": mode,
        "prompt": prompt,
        "stdout": "mock Aristotle output",
        "stderr": "",
        "returncode": 0,
        "mock": True,
        "warning": "Mock mode does not call Harmonic Aristotle or verify Lean output.",
    }


def normalize_code_language(language: str | None) -> str:
    normalized = (language or "python").strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "python3": "python",
        "py": "python",
        "sagemath": "sage",
        "macaulay": "macaulay2",
        "m2": "macaulay2",
    }
    return aliases.get(normalized, normalized)


def _code_runner(language: str, script_path: Path) -> list[str]:
    if language == "python":
        return ["python3", str(script_path)]
    if language == "sage":
        return ["sage", str(script_path)]
    if language == "macaulay2":
        return ["M2", "--stop", str(script_path)]
    raise ValueError(
        f"Unsupported code language {language!r}. "
        f"Use one of: {', '.join(OrchestratorTools.SUPPORTED_CODE_LANGUAGES)}."
    )


def _code_script_path(workspace: Path, language: str) -> Path:
    extensions = {"python": ".py", "sage": ".sage", "macaulay2": ".m2"}
    extension = extensions.get(language)
    if extension is None:
        raise ValueError(
            f"Unsupported code language {language!r}. "
            f"Use one of: {', '.join(OrchestratorTools.SUPPORTED_CODE_LANGUAGES)}."
        )
    return workspace / f"snippet{extension}"


def _write_code_script(script_path: Path, instruction: str) -> None:
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(instruction.strip() + "\n", encoding="utf-8")


def _real_search(query: str, timeout_s: int) -> dict[str, Any]:
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    try:
        body = _fetch_url(url, timeout_s)
    except Exception as exc:  # noqa: BLE001 - surface network failures to orchestrator
        return {"query": query, "results": [], "error": str(exc)}

    results: list[dict[str, str]] = []
    for match in re.finditer(
        r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?'
        r'<a class="result__snippet".*?>(.*?)</a>',
        body,
        re.DOTALL,
    ):
        href, title, snippet = match.groups()
        results.append(
            {
                "title": _strip_html(title),
                "snippet": _strip_html(snippet),
                "url": href,
            }
        )
        if len(results) >= 5:
            break
    return {"query": query, "results": results}


def _real_browse(url: str, timeout_s: int) -> dict[str, Any]:
    try:
        body = _fetch_url(url, timeout_s)
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "title": "", "content": "", "error": str(exc)}

    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    title = _strip_html(title_match.group(1)) if title_match else ""
    content = _strip_html(body)
    if len(content) > 8000:
        content = content[:8000] + "..."
    return {"url": url, "title": title, "content": content}


async def _real_code(
    instruction: str, language: str, workspace: Path, timeout_s: int, *, cwd: Path | None = None
) -> dict[str, Any]:
    try:
        script_path = _code_script_path(workspace.resolve(), language)
    except ValueError:
        error = (
            f"Unsupported code language {language!r}. "
            f"Use one of: {', '.join(OrchestratorTools.SUPPORTED_CODE_LANGUAGES)}."
        )
        return {
            "instruction": instruction,
            "language": language,
            "stdout": "",
            "stderr": error,
            "returncode": -1,
            "error": error,
        }

    workspace = workspace.resolve()
    _write_code_script(script_path, instruction)
    command = _code_runner(language, script_path)
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd.resolve() if cwd is not None else workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        stdout_raw, stderr_raw = await proc.communicate()
        return {
            "instruction": instruction,
            "language": language,
            "stdout": stdout_raw.decode("utf-8", errors="replace"),
            "stderr": stderr_raw.decode("utf-8", errors="replace"),
            "returncode": -1,
            "error": f"Code execution timed out after {timeout_s}s.",
            "script_path": str(script_path),
        }
    return {
        "instruction": instruction,
        "language": language,
        "stdout": stdout_raw.decode("utf-8", errors="replace"),
        "stderr": stderr_raw.decode("utf-8", errors="replace"),
        "returncode": proc.returncode if proc.returncode is not None else -1,
        "script_path": str(script_path),
    }


async def _real_aristotle(
    executable: str,
    prompt: str,
    *,
    mode: str,
    project_dir: Path | None,
    source_path: Path | None,
    destination: Path | None,
    wait: bool,
    workspace: Path,
    timeout_s: int,
) -> dict[str, Any]:
    if not os.environ.get("ARISTOTLE_API_KEY"):
        return {"ok": False, "error": "ARISTOTLE_API_KEY is not set."}
    if shutil.which(executable) is None:
        return {"ok": False, "error": f"Aristotle executable {executable!r} was not found on PATH."}

    normalized_mode = (mode or "submit").strip().lower()
    if normalized_mode == "submit":
        if project_dir is None:
            return {"ok": False, "error": "aristotle submit requires project_dir."}
        command = [executable, "submit", prompt, "--project-dir", str(project_dir)]
    elif normalized_mode == "formalize":
        if source_path is None:
            return {"ok": False, "error": "aristotle formalize requires source_path."}
        command = [executable, "formalize", str(source_path)]
    else:
        return {"ok": False, "error": "Unsupported Aristotle mode. Use submit or formalize."}

    if wait:
        command.append("--wait")
    if destination is not None:
        command.extend(["--destination", str(destination)])

    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        stdout_raw, stderr_raw = await proc.communicate()
        return {
            "ok": False,
            "command": command,
            "stdout": stdout_raw.decode("utf-8", errors="replace"),
            "stderr": stderr_raw.decode("utf-8", errors="replace"),
            "returncode": -1,
            "error": f"Aristotle timed out after {timeout_s}s. These jobs can be very long.",
        }

    stdout = stdout_raw.decode("utf-8", errors="replace")
    stderr = stderr_raw.decode("utf-8", errors="replace")
    return {
        "ok": proc.returncode == 0,
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode if proc.returncode is not None else -1,
        "contains_unresolved_placeholders": _mentions_unresolved_lean_placeholder(stdout + "\n" + stderr),
        "warning": "Treat Aristotle output as a proof attempt until downloaded Lean files are checked for sorry/admit.",
    }


def _mentions_unresolved_lean_placeholder(text: str) -> bool:
    return bool(re.search(r"\b(sorry|admit)\b", text, re.IGNORECASE))


def _fetch_url(url: str, timeout_s: int) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "KimiStyle-MathSolver/0.1 (+orchestrator-tools)"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", cleaned)).strip()
