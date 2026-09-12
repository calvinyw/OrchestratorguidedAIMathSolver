from __future__ import annotations

from pathlib import Path

from swarm_harness.records import AgentCallResult


class ModelRoutingBackend:
    """Dispatch each agent call to a backend chosen by the requested model name.

    The orchestrator already emits an optional per-action ``model``; routing on that
    value lets one run mix Codex and Claude workers without any orchestrator change.
    Calls that name no model, or a model no route claims, go to ``default``.
    """

    def __init__(self, default: object, routes: dict[str, object]) -> None:
        if not routes:
            raise ValueError("ModelRoutingBackend requires at least one route.")
        self.default = default
        # Longest prefix first so "claude-opus" beats "claude" when both are routed.
        self.routes = dict(sorted(routes.items(), key=lambda kv: len(kv[0]), reverse=True))

    def backend_for(self, model: str | None) -> object:
        if not model:
            return self.default
        lowered = model.strip().lower()
        for prefix, backend in self.routes.items():
            if lowered.startswith(prefix.lower()):
                return backend
        return self.default

    def describe_routes(self) -> dict[str, str]:
        return {prefix: type(backend).__name__ for prefix, backend in self.routes.items()}

    async def run_agent(
        self,
        *,
        role: str,
        call_id: str,
        prompt: str,
        workspace: Path,
        schema_path: Path | None,
        timeout_s: int,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentCallResult:
        backend = self.backend_for(model)
        return await backend.run_agent(
            role=role,
            call_id=call_id,
            prompt=prompt,
            workspace=workspace,
            schema_path=schema_path,
            timeout_s=timeout_s,
            model=model,
            reasoning_effort=reasoning_effort,
        )
