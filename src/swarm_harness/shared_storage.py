from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from swarm_harness.util import append_jsonl, read_json, safe_id, utc_now, write_json


MEMORY_ACTIONS = {
    "fact_upsert",
    "fact_delete",
    "implication_upsert",
    "implication_delete",
}


class GlobalFactGraph:
    """Per-run shared store for asserted true facts and their implications."""

    def __init__(self, run_dir: Path) -> None:
        self.root = run_dir / "global_facts"
        self.graph_path = self.root / "graph.json"
        self.history_path = self.root / "history.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.graph_path.exists():
            write_json(self.graph_path, self._empty_graph())

    @staticmethod
    def _empty_graph() -> dict[str, Any]:
        return {"version": 1, "facts": {}, "implications": {}}

    def read(self) -> dict[str, Any]:
        try:
            raw = read_json(self.graph_path)
        except (FileNotFoundError, json.JSONDecodeError):
            raw = self._empty_graph()
        if not isinstance(raw, dict):
            raw = self._empty_graph()
        facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
        implications = raw.get("implications") if isinstance(raw.get("implications"), dict) else {}
        return {"version": 1, "facts": facts, "implications": implications}

    def apply(self, action: dict[str, Any], *, author: str) -> dict[str, Any]:
        atype = str(action.get("type") or "")
        if atype not in MEMORY_ACTIONS:
            return {"ok": False, "action": atype, "error": f"Unknown fact-graph action {atype!r}."}

        graph = self.read()
        now = utc_now()
        if atype == "fact_upsert":
            receipt = self._upsert_fact(graph, action, author=author, now=now)
        elif atype == "fact_delete":
            receipt = self._delete_fact(graph, action)
        elif atype == "implication_upsert":
            receipt = self._upsert_implication(graph, action, author=author, now=now)
        else:
            receipt = self._delete_implication(graph, action)

        receipt = {"action": atype, **receipt}
        if receipt.get("ok"):
            write_json(self.graph_path, graph)
        append_jsonl(
            self.history_path,
            {**receipt, "author": author, "request": action, "at": now},
        )
        return receipt

    def prompt_view(self, *, max_chars: int = 32_000) -> dict[str, Any]:
        """Return a bounded view; the prompt also names graph_path for exact reads."""
        graph = self.read()
        facts: list[dict[str, Any]] = []
        for raw in graph["facts"].values():
            if not isinstance(raw, dict):
                continue
            facts.append(
                {
                    "fact_id": raw.get("fact_id"),
                    "kind": raw.get("kind"),
                    "statement": _excerpt(str(raw.get("statement") or ""), 900),
                    "justification": _excerpt(str(raw.get("justification") or ""), 700),
                    "updated_by": raw.get("updated_by"),
                }
            )
        implications: list[dict[str, Any]] = []
        for raw in graph["implications"].values():
            if not isinstance(raw, dict):
                continue
            implications.append(
                {
                    "implication_id": raw.get("implication_id"),
                    "premise_fact_ids": raw.get("premise_fact_ids") or [],
                    "conclusion_fact_id": raw.get("conclusion_fact_id"),
                    "justification": _excerpt(str(raw.get("justification") or ""), 500),
                }
            )

        view: dict[str, Any] = {
            "graph_file": str(self.graph_path.resolve()),
            "history_file": str(self.history_path.resolve()),
            "fact_count": len(facts),
            "implication_count": len(implications),
            "facts": facts,
            "implications": implications,
        }
        while len(json.dumps(view, ensure_ascii=False, default=str)) > max_chars:
            if len(facts) > 1:
                facts.pop(0)
                view["facts_omitted"] = int(view.get("facts_omitted") or 0) + 1
            elif len(implications) > 1:
                implications.pop(0)
                view["implications_omitted"] = int(view.get("implications_omitted") or 0) + 1
            else:
                break
        return view

    def checkpoint(self, step: int) -> Path:
        path = self.root / "checkpoints" / f"step_{max(0, int(step)):02d}.json"
        write_json(path, self.read())
        return path

    def restore_before_step(self, from_step: int) -> bool:
        """Restore the last completed-step snapshot when resuming an earlier prefix."""
        completed_step = max(0, int(from_step) - 1)
        path = self.root / "checkpoints" / f"step_{completed_step:02d}.json"
        if not path.exists():
            return False
        graph = read_json(path)
        if not isinstance(graph, dict):
            return False
        write_json(self.graph_path, graph)
        append_jsonl(
            self.history_path,
            {"action": "restore_checkpoint", "step": completed_step, "at": utc_now()},
        )
        return True

    def _upsert_fact(
        self,
        graph: dict[str, Any],
        action: dict[str, Any],
        *,
        author: str,
        now: str,
    ) -> dict[str, Any]:
        statement = str(action.get("statement") or "").strip()
        justification = str(action.get("justification") or "").strip()
        if not statement:
            return {"ok": False, "error": "fact_upsert requires a non-empty statement."}
        if not justification:
            return {"ok": False, "error": "fact_upsert requires a non-empty justification."}

        requested_id = str(action.get("fact_id") or "").strip()
        fact_id = safe_id(requested_id, fallback="") if requested_id else _content_id("fact", statement)
        if not fact_id:
            fact_id = _content_id("fact", statement)
        kind = str(action.get("fact_kind") or "derived").strip().lower()
        if kind not in {"hypothesis", "definition", "derived", "external"}:
            kind = "derived"

        facts = graph["facts"]
        previous = facts.get(fact_id) if isinstance(facts.get(fact_id), dict) else None
        created_at = previous.get("created_at") if previous else now
        created_by = previous.get("created_by") if previous else author
        revision = int(previous.get("revision") or 0) + 1 if previous else 1
        facts[fact_id] = {
            "fact_id": fact_id,
            "kind": kind,
            "statement": statement,
            "justification": justification,
            "created_at": created_at,
            "created_by": created_by,
            "updated_at": now,
            "updated_by": author,
            "revision": revision,
        }
        return {"ok": True, "fact_id": fact_id, "operation": "updated" if previous else "created"}

    @staticmethod
    def _delete_fact(graph: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        fact_id = safe_id(str(action.get("fact_id") or ""), fallback="")
        reason = str(action.get("reason") or "").strip()
        if not fact_id or fact_id not in graph["facts"]:
            return {"ok": False, "fact_id": fact_id, "error": f"Unknown fact {fact_id!r}."}
        if not reason:
            return {"ok": False, "fact_id": fact_id, "error": "fact_delete requires a reason."}
        del graph["facts"][fact_id]
        removed_edges = []
        for implication_id, implication in list(graph["implications"].items()):
            if not isinstance(implication, dict):
                continue
            premises = implication.get("premise_fact_ids") or []
            if fact_id == implication.get("conclusion_fact_id") or fact_id in premises:
                removed_edges.append(implication_id)
                del graph["implications"][implication_id]
        return {
            "ok": True,
            "fact_id": fact_id,
            "operation": "deleted",
            "removed_implication_ids": removed_edges,
            "reason": reason,
        }

    @staticmethod
    def _upsert_implication(
        graph: dict[str, Any],
        action: dict[str, Any],
        *,
        author: str,
        now: str,
    ) -> dict[str, Any]:
        raw_premises = action.get("premise_fact_ids")
        if not isinstance(raw_premises, list):
            return {"ok": False, "error": "implication_upsert requires premise_fact_ids to be a list."}
        premises = [safe_id(str(value), fallback="") for value in raw_premises]
        premises = [value for value in premises if value]
        conclusion = safe_id(str(action.get("conclusion_fact_id") or ""), fallback="")
        justification = str(action.get("justification") or "").strip()
        missing = [fact_id for fact_id in [*premises, conclusion] if fact_id not in graph["facts"]]
        if not premises:
            return {"ok": False, "error": "implication_upsert requires at least one premise fact."}
        if not conclusion:
            return {"ok": False, "error": "implication_upsert requires a conclusion fact."}
        if missing:
            return {"ok": False, "error": f"Unknown fact ids in implication: {sorted(set(missing))}."}
        if conclusion in premises:
            return {"ok": False, "error": "An implication cannot use its conclusion as a premise."}
        if not justification:
            return {"ok": False, "error": "implication_upsert requires a non-empty justification."}

        requested_id = str(action.get("implication_id") or "").strip()
        identity = json.dumps([sorted(set(premises)), conclusion], ensure_ascii=False)
        implication_id = safe_id(requested_id, fallback="") if requested_id else _content_id("imp", identity)
        if not implication_id:
            implication_id = _content_id("imp", identity)
        implications = graph["implications"]
        previous = implications.get(implication_id) if isinstance(implications.get(implication_id), dict) else None
        created_at = previous.get("created_at") if previous else now
        created_by = previous.get("created_by") if previous else author
        revision = int(previous.get("revision") or 0) + 1 if previous else 1
        implications[implication_id] = {
            "implication_id": implication_id,
            "premise_fact_ids": sorted(set(premises)),
            "conclusion_fact_id": conclusion,
            "justification": justification,
            "created_at": created_at,
            "created_by": created_by,
            "updated_at": now,
            "updated_by": author,
            "revision": revision,
        }
        return {
            "ok": True,
            "implication_id": implication_id,
            "operation": "updated" if previous else "created",
        }

    @staticmethod
    def _delete_implication(graph: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        implication_id = safe_id(str(action.get("implication_id") or ""), fallback="")
        reason = str(action.get("reason") or "").strip()
        if not implication_id or implication_id not in graph["implications"]:
            return {
                "ok": False,
                "implication_id": implication_id,
                "error": f"Unknown implication {implication_id!r}.",
            }
        if not reason:
            return {"ok": False, "implication_id": implication_id, "error": "implication_delete requires a reason."}
        del graph["implications"][implication_id]
        return {
            "ok": True,
            "implication_id": implication_id,
            "operation": "deleted",
            "reason": reason,
        }


def memory_updates_from_response(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    updates: list[dict[str, Any]] = []
    declared = raw.get("memory_updates")
    if isinstance(declared, list):
        updates.extend(item for item in declared if isinstance(item, dict))
    actions = raw.get("actions")
    if isinstance(actions, list):
        updates.extend(
            item
            for item in actions
            if isinstance(item, dict) and str(item.get("type") or "") in MEMORY_ACTIONS
        )
    return updates


def update_agent_index(run_dir: Path, record: dict[str, Any]) -> None:
    index_path = run_dir / "agents" / "index.json"
    try:
        raw = read_json(index_path)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {"version": 1, "calls": []}
    calls = raw.get("calls") if isinstance(raw, dict) and isinstance(raw.get("calls"), list) else []
    call_id = str(record.get("call_id") or "")
    calls = [item for item in calls if not isinstance(item, dict) or str(item.get("call_id") or "") != call_id]
    calls.append(record)
    write_json(index_path, {"version": 1, "calls": calls})


def agent_index_prompt_view(run_dir: Path, *, max_chars: int = 32_000) -> dict[str, Any]:
    index_path = run_dir / "agents" / "index.json"
    try:
        raw = read_json(index_path)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {"version": 1, "calls": []}
    calls = raw.get("calls") if isinstance(raw, dict) and isinstance(raw.get("calls"), list) else []
    visible = list(calls)
    view: dict[str, Any] = {
        "index_file": str(index_path.resolve()),
        "agent_files_root": str((run_dir / "agents").resolve()),
        "call_count": len(calls),
        "calls": visible,
    }
    while len(json.dumps(view, ensure_ascii=False, default=str)) > max_chars and visible:
        visible.pop(0)
        view["calls_omitted"] = int(view.get("calls_omitted") or 0) + 1
    return view


def _content_id(prefix: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
