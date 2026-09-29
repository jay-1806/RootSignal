"""Service topology: who calls whom, and what each service depends on.

Hard-coded for the demo system. In a real deployment this would come from a service
catalog or be inferred from traces. The engine only uses it through these functions.
"""

from enum import StrEnum


class Capability(StrEnum):
    DATABASE = "database"  # owns a DB connection pool
    CACHE = "cache"  # uses a Redis cache


# service -> services it calls
CALLS: dict[str, list[str]] = {
    "gateway": ["checkout", "inventory"],
    "checkout": ["inventory"],
    "inventory": [],
}

CAPABILITIES: dict[str, set[Capability]] = {
    "gateway": set(),
    "checkout": set(),
    "inventory": {Capability.DATABASE, Capability.CACHE},
}


def known_services() -> list[str]:
    return sorted(CALLS)


def is_known(service: str) -> bool:
    return service in CALLS


def downstream(service: str) -> list[str]:
    """All services `service` depends on, directly or indirectly, nearest first."""
    seen: list[str] = []
    queue = list(CALLS.get(service, []))
    while queue:
        current = queue.pop(0)
        if current not in seen:
            seen.append(current)
            queue.extend(CALLS.get(current, []))
    return seen


def depth(service: str) -> int:
    """Longest call chain below `service`. Leaf services (0) are the most 'root-cause-like'."""
    children = CALLS.get(service, [])
    return 0 if not children else 1 + max(depth(c) for c in children)


def has(service: str, capability: Capability) -> bool:
    return capability in CAPABILITIES.get(service, set())


def describe() -> dict[str, dict[str, list[str]]]:
    """Compact form for LLM prompts."""
    return {
        s: {"calls": CALLS[s], "capabilities": sorted(CAPABILITIES.get(s, set()))}
        for s in known_services()
    }
