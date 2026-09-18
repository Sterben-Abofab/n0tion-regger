"""Multi-workspace profile switching for ``notion-agent``.

A profile is just a named credential file under
``~/.notionagents/<name>.json``. The active credential
(``~/.notionagents/notion_account.json``) is a symlink pointing at one
of them; ``profile use <name>`` retargets the symlink so subsequent
``notion-agent chat`` / ``doctor`` etc. calls hit the chosen workspace.

Why a symlink (vs an env var / config field)?

- Zero coordination with the rest of the CLI: every subcommand already
  resolves ``--account`` to ``~/.notionagents/notion_account.json``;
  swapping the symlink is enough.
- Atomic on POSIX (``os.replace`` on a temp symlink → final name).
- ``ls -l ~/.notionagents/notion_account.json`` tells you which
  workspace is live without parsing JSON.
- No migration tooling needed when a user wants to "promote" their
  existing ``notion_account.json`` into a profile — see
  :func:`migrate_account_to_profile`.

Reserved names:

- ``notion_account`` cannot be used as a profile name (it would collide
  with the symlink target).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from notion_agent_cli.exceptions import ErrorCode, NotionAgentError

DEFAULT_PROFILE_DIR = Path.home() / ".notionagents"
DEFAULT_ACCOUNT_NAME = "notion_account.json"
RESERVED_NAMES = frozenset({"notion_account"})


@dataclass(slots=True, frozen=True)
class ProfileEntry:
    name: str
    path: Path
    is_active: bool


def _account_link_path(profile_dir: Path) -> Path:
    return profile_dir / DEFAULT_ACCOUNT_NAME


def _profile_path(name: str, profile_dir: Path) -> Path:
    return profile_dir / f"{name}.json"


def _validate_name(name: str) -> None:
    if not name:
        raise NotionAgentError(
            "profile name cannot be empty",
            code=ErrorCode.UNKNOWN,
        )
    if name in RESERVED_NAMES:
        raise NotionAgentError(
            f"profile name {name!r} is reserved — pick another",
            code=ErrorCode.UNKNOWN,
        )
    if "/" in name or "\\" in name or name.startswith("."):
        raise NotionAgentError(
            f"profile name {name!r} cannot contain path separators or start with '.'",
            code=ErrorCode.UNKNOWN,
        )


def list_profiles(
    profile_dir: Path | None = None,
) -> list[ProfileEntry]:
    """Return all profiles in ``profile_dir``, oldest-first by mtime.

    The active profile (the one ``notion_account.json`` links to) is
    flagged via :attr:`ProfileEntry.is_active`. The symlink target file
    itself is excluded from the list since it's the dispatch point, not
    a profile.
    """
    base = (profile_dir or DEFAULT_PROFILE_DIR).expanduser()
    if not base.exists():
        return []

    link = _account_link_path(base)
    active_target: Path | None = None
    if link.is_symlink():
        # Resolve relative to the link's parent so a relative target
        # ("personal.json") still picks the right file.
        active_target = (link.parent / os.readlink(link)).resolve()
    elif link.exists():
        active_target = link.resolve()

    out: list[ProfileEntry] = []
    for entry in sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime):
        if entry.name == DEFAULT_ACCOUNT_NAME:
            continue
        is_active = (
            active_target is not None
            and entry.resolve() == active_target
        )
        out.append(ProfileEntry(
            name=entry.stem,
            path=entry,
            is_active=is_active,
        ))
    return out


def current_profile(
    profile_dir: Path | None = None,
) -> ProfileEntry | None:
    """Return the active profile, or ``None`` when no symlink exists.

    A plain (non-symlink) ``notion_account.json`` still resolves to its
    own path — but since that path doesn't have a matching named
    profile, the result is ``None`` and the caller knows the operator
    hasn't adopted profiles yet.
    """
    for p in list_profiles(profile_dir):
        if p.is_active:
            return p
    return None


def use_profile(
    name: str,
    profile_dir: Path | None = None,
) -> Path:
    """Point ``notion_account.json`` at ``<name>.json``.

    Returns the symlink path on success. Raises
    :class:`NotionAgentError` when:

    - the requested profile doesn't exist
    - ``notion_account.json`` is a regular file (not a symlink) — the
      caller should migrate first via :func:`migrate_account_to_profile`
    """
    _validate_name(name)
    base = (profile_dir or DEFAULT_PROFILE_DIR).expanduser()
    target = _profile_path(name, base)
    if not target.exists():
        raise NotionAgentError(
            f"profile {name!r} not found at {target} — "
            "either create one or run `notion-agent init --account {target}`",
            code=ErrorCode.ACCOUNT_MISSING,
        )

    link = _account_link_path(base)
    if link.exists() and not link.is_symlink():
        raise NotionAgentError(
            f"{link} already exists as a regular file — move it to a "
            "named profile first (e.g. `mv notion_account.json default.json`) "
            "or pass --force to overwrite.",
            code=ErrorCode.ACCOUNT_INVALID,
        )

    # Atomic symlink swap: write to a temp name, then rename.
    tmp = link.with_name(link.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    # Relative target so the link survives a move of the .notionagents dir.
    tmp.symlink_to(target.name)
    os.replace(tmp, link)
    return link


def migrate_account_to_profile(
    new_name: str,
    profile_dir: Path | None = None,
) -> Path:
    """Promote an existing ``notion_account.json`` (a regular file) into
    a named profile and re-link.

    No-op when ``notion_account.json`` is already a symlink. Useful for
    operators upgrading from pre-profile setups.
    """
    _validate_name(new_name)
    base = (profile_dir or DEFAULT_PROFILE_DIR).expanduser()
    link = _account_link_path(base)
    new_path = _profile_path(new_name, base)
    if new_path.exists():
        raise NotionAgentError(
            f"profile {new_name!r} already exists at {new_path}",
            code=ErrorCode.ACCOUNT_INVALID,
        )
    if link.is_symlink():
        return link  # already migrated
    if not link.exists():
        raise NotionAgentError(
            f"no account file at {link} to migrate — run `notion-agent init` first",
            code=ErrorCode.ACCOUNT_MISSING,
        )
    link.rename(new_path)
    return use_profile(new_name, profile_dir=base)
