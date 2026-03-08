# Project Sherpa

## Tool Discovery

Use the `tool_search` MCP tool to find available tools by keyword. Example: searching "secrets" will find the vault manager.

## Running Tools

Use the `tool_run` MCP tool to execute any discovered tool:

1. Inspect: `tool_run("tool_name", "--help")`
2. Execute: `tool_run("tool_name", "<args>")`

## Missing Secrets

If `tool_run` returns a missing-secret error, ask the user for the value, then store it:

```
tool_run("vault_manager", "set <KEY> <VALUE>")
```

Then retry the tool.

## Creating New Tools

Follow the standards in `docs/SHERPA_STANDARDS.md`. New tools placed in `tools/` are auto-indexed on the next `tool_search` call.
