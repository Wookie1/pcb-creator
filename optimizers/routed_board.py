"""Single source of truth for reading a routed-board dict.

The router writes statistics nested under ``routed["routing"]["statistics"]``.
Reading the top level instead silently yields zeros — a 100%-routed board
reported as 0% (which happened in cli --json-output and the vision pre-check).
Every consumer goes through these accessors so a new call site can't hand-roll
the navigation and reintroduce the wrong path.

Zero project imports on purpose: this is a leaf module importable from any
layer without creating a cycle.
"""

from __future__ import annotations


def routing_stats(routed: dict) -> dict:
    """The statistics dict of a routed board; ``{}`` if absent/malformed."""
    if not isinstance(routed, dict):
        return {}
    return routed.get("routing", {}).get("statistics", {})


def routing_completion(routed: dict) -> float:
    """Completion percentage of a routed board (0.0 if absent)."""
    return routing_stats(routed).get("completion_pct", 0) or 0.0
