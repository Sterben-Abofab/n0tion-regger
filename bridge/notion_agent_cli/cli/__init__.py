"""CLI surface — entry point is :mod:`notion_agent_cli.cli.__main__`.

The package exposes a single ``notion-agent`` console-script registered
in pyproject.toml. Subcommands live in this module; for now everything
fits in ``__main__.py``, but we'll split per-command files as they grow.
"""
