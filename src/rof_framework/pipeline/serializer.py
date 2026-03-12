"""
pipeline/serializer.py
SnapshotSerializer – converts WorkflowGraph snapshots to/from RL text
and merges multiple snapshots together.
"""

from __future__ import annotations

import copy
import json
from typing import Optional

__all__ = [
    "SnapshotSerializer",
]


class SnapshotSerializer:
    """
    Converts WorkflowGraph snapshot dicts to RelateLang source and back,
    and merges multiple snapshots for cross-stage context injection.

    The canonical snapshot format (from WorkflowGraph.snapshot()):
        {
            "entities": {
                "Customer": {
                    "description": "A person who purchases products",
                    "attributes":  { "total_purchases": 15000 },
                    "predicates":  ["HighValue"]
                }
            },
            "goals": [
                { "expr": "determine Customer segment", "status": "ACHIEVED" }
            ]
        }
    """

    CONTEXT_HEADER = "// [Pipeline context – entities from prior stages]"

    # ------------------------------------------------------------------
    # snapshot → RL text
    # ------------------------------------------------------------------

    @classmethod
    def to_rl(
        cls,
        snapshot: dict,
        header: str = "",
        entity_filter: Optional[set[str]] = None,
        max_entities: int = 200,
    ) -> str:
        """
        Convert a snapshot dict into RelateLang attribute statements.

        Args:
            snapshot:       WorkflowGraph.snapshot() dict.
            header:         Optional comment header prepended to output.
            entity_filter:  If given, only emit RL for these entity names.
            max_entities:   Hard cap on entities to serialise (prevents overflow).

        Returns:
            Multi-line RL string ready to prepend to the next stage's source.
        """
        lines: list[str] = []
        if header or cls.CONTEXT_HEADER:
            lines.append(header or cls.CONTEXT_HEADER)

        entities = snapshot.get("entities", {})
        count = 0
        for entity_name, entity_data in entities.items():
            if entity_filter and entity_name not in entity_filter:
                continue
            if count >= max_entities:
                lines.append(f"// … ({len(entities) - count} entities truncated)")
                break

            desc = entity_data.get("description", "")
            if desc:
                lines.append(f'define {entity_name} as "{desc}".')

            for attr, val in entity_data.get("attributes", {}).items():
                if isinstance(val, str):
                    # Escape embedded quotes and newlines so multi-line values
                    # (e.g. rl_context strings from tool output) do not break
                    # the RL tokenizer, which splits statements on bare periods
                    # at the end of lines.
                    safe_val = val.replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
                    lines.append(f'{entity_name} has {attr} of "{safe_val}".')
                elif isinstance(val, bool):
                    lines.append(f"{entity_name} has {attr} of {str(val).lower()}.")
                elif isinstance(val, (int, float)):
                    lines.append(f"{entity_name} has {attr} of {val}.")
                else:
                    # Fallback: JSON-encode complex values as strings
                    safe_val = json.dumps(val).strip('"')
                    lines.append(f'{entity_name} has {attr} of "{safe_val}".')

            for pred in entity_data.get("predicates", []):
                safe_pred = pred.replace('"', '\\"')
                lines.append(f'{entity_name} is "{safe_pred}".')

            count += 1

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # merge snapshots
    # ------------------------------------------------------------------

    @classmethod
    def merge(cls, base: dict, update: dict) -> dict:
        """
        Merge *update* into *base* snapshot.

        Rules:
          - Entities in *update* that are not in *base* are added wholesale.
          - For entities present in both: attributes are merged (update wins
            on key collision); predicates are unioned (no duplicates).
          - Goals from *update* are appended (keeping *base* goals intact).
        """
        result = copy.deepcopy(base)

        for entity_name, entity_data in update.get("entities", {}).items():
            if entity_name not in result.get("entities", {}):
                result.setdefault("entities", {})[entity_name] = {
                    "description": "",
                    "attributes": {},
                    "predicates": [],
                }
            target = result["entities"][entity_name]

            # Normalise legacy / flat entities that may lack the structured keys
            target.setdefault("description", "")
            target.setdefault("attributes", {})
            target.setdefault("predicates", [])

            # Description: update wins if non-empty
            new_desc = entity_data.get("description", "")
            if new_desc:
                target["description"] = new_desc

            # Attributes: update wins on collision
            target["attributes"].update(entity_data.get("attributes", {}))

            # Predicates: union
            existing = set(target.get("predicates", []))
            for pred in entity_data.get("predicates", []):
                if pred not in existing:
                    target["predicates"].append(pred)
                    existing.add(pred)

        # Goals: append new ones (avoid exact duplicates by expr)
        existing_exprs = {g.get("expr") for g in result.get("goals", [])}
        for goal in update.get("goals", []):
            if goal.get("expr") not in existing_exprs:
                result.setdefault("goals", []).append(goal)
                existing_exprs.add(goal.get("expr"))

        return result

    @classmethod
    def empty(cls) -> dict:
        """Return an empty snapshot dict."""
        return {"entities": {}, "goals": []}
