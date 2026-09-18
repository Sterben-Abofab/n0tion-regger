"""Friendly model name → Notion internal model id.

Notion ships internal model ids like ``apricot-sorbet-high`` /
``avocado-froyo-medium`` and rotates them periodically. We keep a
hard-coded :data:`DEFAULT_MODEL_MAP` of ids we've confirmed via live
captures, plus a fuzzy fallback for unknown aliases.

When Notion adds new models or renames an id, run
``notion-agent models refresh`` — it hits ``/api/v3/getAvailableModels``,
writes the friendly-name → internal-id mapping to a user-editable file
(default ``~/.notionagents/models.json``), and :func:`resolve_model`
will prefer that user map over :data:`DEFAULT_MODEL_MAP` next call.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_USER_MODELS_PATH = Path.home() / ".notionagents" / "models.json"

# Friendly alias → Notion internal id.
#   - opus-4.8: live-captured 2026-05-28 (TP-Link space, `models refresh`)
#   - opus-4.7: live-captured 2026-05-15 (TP-Link space, chat panel UI)
#   - others: notion_manager Go src `DefaultModelMap` snapshot (2026-05)
DEFAULT_MODEL_MAP: dict[str, str] = {
    "opus-4.8":         "ambrosia-tart-high",
    "opus-4.7":         "apricot-sorbet-high",
    "opus-4.6":         "avocado-froyo-medium",
    "sonnet-4.6":       "almond-croissant-low",
    "haiku-4.5":        "anthropic-haiku-4.5",
    "gpt-5.2":          "oatmeal-cookie",
    "gpt-5.4":          "oval-kumquat-medium",
    "gemini-2.5-flash": "vertex-gemini-2.5-flash",
    "gemini-3-flash":   "gingerbread",
    "minimax-m2.5":     "fireworks-minimax-m2.5",
}

# Anthropic SDK id → our friendly alias.
ANTHROPIC_ALIASES: dict[str, str] = {
    "claude-opus-4-8":   "opus-4.8",
    "claude-opus-4-7":   "opus-4.7",
    "claude-opus-4-6":   "opus-4.6",
    "claude-opus-4-5":   "opus-4.6",
    "claude-sonnet-4-6": "sonnet-4.6",
    "claude-sonnet-4-5": "sonnet-4.6",
    "claude-haiku-4-5":  "haiku-4.5",
}


def resolve_alias(notion_id: str, *, user_map: dict[str, str] | None = None) -> str | None:
    """Reverse :func:`resolve_model`: Notion internal id → friendly alias.

    Returns ``None`` when the id isn't in the effective map (e.g. Notion
    rotated to a fresh id since the last ``models refresh``). Used by the
    CLI to attach a human-readable ``model_alias`` field to ``chat --json``
    output so agents don't have to guess that ``apricot-sorbet-high``
    means ``opus-4.7``.
    """
    effective = {**DEFAULT_MODEL_MAP, **(user_map or {})}
    for alias, mid in effective.items():
        if mid == notion_id:
            return alias
    return None


def resolve_model(model: str, *, user_map: dict[str, str] | None = None) -> str:
    """Map friendly / Anthropic alias → Notion internal id.

    Falls back to family-keyword fuzzy match when the alias is unknown.
    Returns the input unchanged if nothing matched — Notion will then
    reject explicitly, which is friendlier than us guessing wrong.

    ``user_map`` (typically loaded from ``~/.notionagents/models.json``
    via :func:`load_user_model_map`) overrides :data:`DEFAULT_MODEL_MAP`
    entries: that's how ``notion-agent models refresh`` propagates fresh
    ids without a code release. Both the lookup keys (aliases) and the
    values (Notion internal ids) participate in passthrough detection.
    """
    user_map = user_map or {}
    effective = {**DEFAULT_MODEL_MAP, **user_map}

    if not model:
        return effective["opus-4.8"]

    if model in effective:
        return effective[model]
    if model in effective.values():
        return model
    if model in ANTHROPIC_ALIASES:
        return effective[ANTHROPIC_ALIASES[model]]

    # Strip date suffix: "claude-opus-4-7-20260301" → "claude-opus-4-7".
    if "-2" in model:
        idx = model.rfind("-2")
        if idx > 0 and len(model) - idx >= 9:
            stripped = model[:idx]
            if stripped in ANTHROPIC_ALIASES:
                return effective[ANTHROPIC_ALIASES[stripped]]
            if stripped in effective:
                return effective[stripped]

    lower = model.lower()
    for keyword, alias in (("opus", "opus-4.8"), ("sonnet", "sonnet-4.6"), ("haiku", "haiku-4.5")):
        if keyword in lower:
            log.warning("model fuzzy fallback %r → %s", model, alias)
            return effective[alias]

    log.warning("model %r unresolved — passing through unchanged", model)
    return model


# --------------------------------------------------------------------------- #
# /getAvailableModels → friendly_alias → internal_id map
# --------------------------------------------------------------------------- #

def parse_available_models(response: dict[str, Any]) -> dict[str, str]:
    """Convert ``/api/v3/getAvailableModels`` JSON into an alias map.

    The friendly alias is derived from ``modelMessage`` by lowercasing
    and replacing spaces with dashes — e.g. ``"Opus 4.7"`` →
    ``"opus-4.7"``, matching the convention in
    :data:`DEFAULT_MODEL_MAP`. Disabled models and entries missing
    either field are skipped.
    """
    out: dict[str, str] = {}
    for entry in response.get("models") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("isDisabled"):
            continue
        msg = entry.get("modelMessage")
        mid = entry.get("model")
        if not isinstance(msg, str) or not isinstance(mid, str):
            continue
        alias = msg.strip().lower().replace(" ", "-")
        if alias:
            out[alias] = mid
    return out


# --------------------------------------------------------------------------- #
# User model map persistence (~/.notionagents/models.json by default)
# --------------------------------------------------------------------------- #

def load_user_model_map(path: Path | str | None = None) -> dict[str, str]:
    """Read a saved alias map. Returns ``{}`` when missing or malformed.

    On disk format::

        {
          "friendly_aliases": {"opus-4.7": "apricot-sorbet-high", ...},
          "updated_at":       "2026-05-16T07:30:00+00:00"
        }
    """
    p = Path(path or DEFAULT_USER_MODELS_PATH).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("user model map at %s unreadable: %s", p, e)
        return {}
    aliases = data.get("friendly_aliases")
    if not isinstance(aliases, dict):
        return {}
    return {k: v for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str)}


def save_user_model_map(
    map_data: dict[str, str],
    path: Path | str | None = None,
) -> Path:
    """Write a map to disk; creates parent dirs. Returns the resolved path."""
    p = Path(path or DEFAULT_USER_MODELS_PATH).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "friendly_aliases": dict(sorted(map_data.items())),
        "updated_at":       datetime.now(UTC).isoformat(timespec="seconds"),
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p
