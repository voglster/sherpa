# Project Sherpa

## Tool Discovery

Use the `tool_search` MCP tool to find available tools by keyword. Example: searching "secrets" will find the vault manager.

## Running Tools

1. Inspect: `uv run tools/<name>.py --help`
2. Execute: `uv run tools/<name>.py <args>`

## Missing Secrets

If a tool reports `MISSING_SECRET: <KEY>`, ask the user for the value, then store it:

```
uv run tools/vault_manager.py set <KEY> <VALUE>
```

Then retry the tool.

## Creating New Tools

Follow the standards in `docs/SHERPA_STANDARDS.md`. New tools placed in `tools/` are auto-indexed on the next `tool_search` call.
