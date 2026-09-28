"""Port-as-identity: each gate listener maps to a fixed agent_id, so most
tools need zero code changes to be identified — just point their base URL at
a different port. See ports.yaml and GATE_MIGRATION_PLAN.md Step 2."""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from app.config import PORTS_YAML_PATH


@dataclass(frozen=True)
class PortIdentity:
    agent_id: str
    require_run_id: bool
    enforcement: str = "enforce"  # "enforce" | "log_only"
    default_task_class: str | None = None  # free-tier task-class gate, see chat.py

    @property
    def log_only(self) -> bool:
        return self.enforcement == "log_only"


def load_ports() -> dict[int, PortIdentity]:
    raw = yaml.safe_load(PORTS_YAML_PATH.read_text())
    return {
        int(port): PortIdentity(
            spec["agent_id"],
            bool(spec["require_run_id"]),
            spec.get("enforcement", "enforce"),
            spec.get("default_task_class"),
        )
        for port, spec in raw.items()
    }


_PORTS = load_ports()


def identity_for_port(port: int) -> PortIdentity | None:
    return _PORTS.get(port)


def all_ports() -> list[int]:
    return sorted(_PORTS.keys())


def reload() -> None:
    """Test hook — re-reads ports.yaml from PORTS_YAML_PATH."""
    global _PORTS
    _PORTS = load_ports()
