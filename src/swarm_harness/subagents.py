from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swarm_harness.util import read_json, safe_id, write_json


@dataclass(frozen=True)
class SubagentSpec:
    """A named subagent the orchestrator can spawn or assign work to."""

    name: str
    system_prompt: str
    role: str = "custom"
    schema_name: str = "solver.schema.json"
    tools: tuple[str, ...] = ()


class SubagentRegistry:
    """In-memory registry of orchestrator subagents, persisted per run."""

    def __init__(self, persist_path: Any | None = None) -> None:
        self._agents: dict[str, SubagentSpec] = {}
        self._persist_path = persist_path

    @property
    def persist_path(self) -> Any | None:
        return self._persist_path

    @persist_path.setter
    def persist_path(self, path: Any | None) -> None:
        self._persist_path = path

    def create_subagent(
        self,
        name: str,
        system_prompt: str,
        *,
        role: str = "custom",
        schema_name: str = "solver.schema.json",
        tools: tuple[str, ...] | list[str] = (),
    ) -> SubagentSpec:
        agent_name = safe_id(name, fallback="subagent")
        spec = SubagentSpec(
            name=agent_name,
            system_prompt=system_prompt.strip(),
            role=role,
            schema_name=schema_name,
            tools=tuple(tools),
        )
        self._agents[agent_name] = spec
        self._persist()
        return spec

    def get(self, name: str) -> SubagentSpec | None:
        return self._agents.get(safe_id(name, fallback=name))

    def resolve(self, agent: str | SubagentSpec) -> SubagentSpec:
        if isinstance(agent, SubagentSpec):
            return agent
        spec = self.get(agent)
        if spec is None:
            raise KeyError(f"Unknown subagent: {agent}")
        return spec

    def list_agents(self) -> list[SubagentSpec]:
        return list(self._agents.values())

    def to_public_list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "role": spec.role,
                "tools": list(spec.tools),
                "system_prompt": spec.system_prompt,
            }
            for spec in self._agents.values()
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            spec.name: {
                "name": spec.name,
                "role": spec.role,
                "schema_name": spec.schema_name,
                "tools": list(spec.tools),
                "system_prompt": spec.system_prompt,
            }
            for spec in self._agents.values()
        }

    def load_from_json(self, data: dict[str, Any]) -> None:
        """Replace registry contents from a `subagents.json`-shaped object."""
        if not isinstance(data, dict):
            raise ValueError("Subagent registry JSON must be an object.")
        agents: dict[str, SubagentSpec] = {}
        for key, raw in data.items():
            if not isinstance(raw, dict):
                continue
            name = safe_id(str(raw.get("name") or key), fallback="subagent")
            agents[name] = SubagentSpec(
                name=name,
                system_prompt=str(raw.get("system_prompt") or "").strip(),
                role=str(raw.get("role") or "custom"),
                schema_name=str(raw.get("schema_name") or "solver.schema.json"),
                tools=tuple(raw.get("tools") or ()),
            )
        self._agents = agents

    @classmethod
    def from_json(
        cls,
        data: dict[str, Any],
        *,
        persist_path: Path | None = None,
    ) -> SubagentRegistry:
        registry = cls(persist_path=persist_path)
        registry.load_from_json(data)
        return registry

    @classmethod
    def from_file(cls, path: Path) -> SubagentRegistry:
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError(f"Subagent registry must be a JSON object: {path}")
        return cls.from_json(data, persist_path=path)

    def _persist(self) -> None:
        if self._persist_path is not None:
            write_json(self._persist_path, self.to_json())
