"""Persistent per-thread state for continuation chats.

Each successful :meth:`NotionAgentClient.complete` writes a small JSON
file under ``~/.notionagents/threads/<thread-id>.json`` carrying the
ids and timestamp the next turn needs to rebuild a partial transcript
Notion will recognize as a continuation of the same thread.

Schema mirrors what ``transcript.build_partial_transcript`` consumes
(see ``docs/01-notion-chat-protocol.md §6``):

- ``config_id`` / ``context_id``    — reused from turn-1 transcript
- ``original_datetime``             — reused from turn-1 context
- ``notion_model``                  — locked to turn-1 choice
- ``updated_config_ids``            — one fresh uuid per past turn

Per-file (vs single ``threads.json`` map) is intentional: simpler
atomicity, no contention between concurrent CLI invocations, and
``rm <id>.json`` is the obvious way to forget a thread.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from notion_agent_cli.exceptions import ErrorCode, NotionAgentError

DEFAULT_THREAD_STATE_DIR = Path.home() / ".notionagents" / "threads"


@dataclass(slots=True)
class ThreadState:
    thread_id:          str
    config_id:          str
    context_id:         str
    original_datetime:  str
    notion_model:       str
    updated_config_ids: list[str] = field(default_factory=list)
    last_activity_iso:  str = ""
    # Workflow-mode runs (`runs start`) need to keep workflow_id on
    # continuation so the partial transcript still files under the
    # right workflow. Empty for the chat-panel default-AI path.
    workflow_id:        str = ""


def thread_state_path(thread_id: str, base_dir: Path | None = None) -> Path:
    return (base_dir or DEFAULT_THREAD_STATE_DIR) / f"{thread_id}.json"


def save_thread_state(state: ThreadState, base_dir: Path | None = None) -> Path:
    p = thread_state_path(state.thread_id, base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(asdict(state), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return p


def load_thread_state(thread_id: str, base_dir: Path | None = None) -> ThreadState:
    p = thread_state_path(thread_id, base_dir)
    if not p.exists():
        raise NotionAgentError(
            f"no saved state for thread {thread_id!r} at {p}; either this "
            "thread was never started by this CLI (state is only written "
            "after a successful chat), or the state file was removed. "
            "Drop --thread-id to start a fresh thread.",
            code=ErrorCode.THREAD_STATE_MISSING,
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise NotionAgentError(
            f"thread state at {p} is malformed: {e}",
            code=ErrorCode.THREAD_STATE_MALFORMED,
        ) from e
    return ThreadState(**data)
