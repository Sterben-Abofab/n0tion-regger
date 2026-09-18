"""notion-agent-cli — call Notion's ✦ AI endpoint from Python / the shell.

Public API:

- :class:`NotionAgentClient` — async client for one round-trip to
  ``/api/v3/runInferenceTranscript``. Loads a credential file, builds
  the chat-panel-equivalent payload, streams the NDJSON response.
- :class:`ChatResponse` / :class:`TokenUsage` — response dataclasses.
- :class:`NotionAgentError` — single error type for transport / auth /
  Notion-side failures. CLI translates this to a non-zero exit code.

Library callers will spend most of their time on those four names; the
sub-modules (``transcript``, ``ndjson``, ``account``, ``models``) are
useful for tests + bootstrap helpers and remain importable.
"""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from notion_agent_cli.exceptions import ErrorCode, NotionAgentError
from notion_agent_cli.provider import NotionAgentClient
from notion_agent_cli.types import ChatResponse, TokenUsage

__all__ = [
    "ChatResponse",
    "ErrorCode",
    "NotionAgentClient",
    "NotionAgentError",
    "TokenUsage",
]

try:
    # Single source of truth: read the installed distribution's metadata so
    # `--version` and `notion-agent-cli.__version__` can never drift from
    # the wheel's `pyproject.toml`. The historical literal here once shipped
    # 0.1.2 stamped as 0.1.0 because nobody remembered to bump it.
    __version__ = _pkg_version("notion-agent-cli")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
