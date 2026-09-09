"""Tiny YAML-frontmatter reader.

Intentionally NOT PyYAML: phase 1 avoids third-party dependencies, and the
frontmatter we need is a flat mapping of scalars plus simple string lists:

    ---
    name: k8s-review
    description: Review Kubernetes manifests...
    files_changed:
      - "a.yaml"
      - "b.yaml"
    ---

Anything more exotic is out of scope; swap this module for PyYAML the moment
a real Agent Skills implementation needs the full grammar.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

DELIMITER = "---"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(text: str) -> Tuple[Dict[str, Any], str]:
    """Return (frontmatter_mapping, body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIMITER:
        return {}, text

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == DELIMITER:
            end = index
            break
    if end is None:
        return {}, text

    meta: Dict[str, Any] = {}
    current_key = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("- ") and current_key is not None:
            # `key:` with an empty value followed by `- item` lines is a list.
            if not isinstance(meta.get(current_key), list):
                meta[current_key] = []
            meta[current_key].append(_unquote(stripped[2:]))
            continue
        if ":" not in stripped:
            # continuation of a folded scalar (e.g. a wrapped description)
            if current_key and isinstance(meta.get(current_key), str):
                meta[current_key] = (meta[current_key] + " " + stripped).strip()
            continue
        key, _, value = stripped.partition(":")
        current_key = key.strip()
        value = value.strip()
        meta[current_key] = _unquote(value) if value else ""

    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body
