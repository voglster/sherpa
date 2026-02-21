# Implementation Plan: Project Sherpa (Lean Edition)

## 1. The "Lean" Architecture

Instead of a heavy MCP that manages file writes, inspection, and execution, we leverage the fact that Claude Code runs directly on the repository. The MCP server is reduced to a **single-purpose search index**. The agent handles everything else natively.

### The Single MCP Tool:

- **`tool_search(query)`**: Queries the local metadata store (TinyDB) for relevant tools based on intent. Returns name, description, and file path. That's it.

### What the Agent Does Natively:

- **Inspect:** `uv run tools/<name>.py --help`
- **Execute:** `uv run tools/<name>.py <args>`
- **Create:** Writes `.py` files to `tools/` following `SHERPA_STANDARDS.md`
- **Iterate:** Reads and modifies tool source via normal file access

## 2. The Development Flow (Inside the Repo)

1. **Creation:** Claude writes a `.py` file to `tools/` using native file system access, following the rules in `SHERPA_STANDARDS.md`.
2. **Indexing:** On the next `tool_search` call, the MCP detects the new/changed file (via mtime comparison), parses its docstring with `ast`, and updates `metadata.json`.
3. **Usage:** Claude runs `uv run tools/<name>.py --help` to learn the interface, then `uv run tools/<name>.py <args>` to execute.

## 3. Repository Layout

```
jim_tools/
├── docs/
│   ├── prd.md
│   ├── implementation-plan.md
│   └── SHERPA_STANDARDS.md        # The tool contract
├── sherpa/
│   ├── __init__.py
│   ├── server.py                  # fastmcp server (tool_search)
│   └── indexer.py                 # ast-based docstring parser + TinyDB writer
├── tools/                         # All Sherpa tools live here
│   ├── vault_manager.py           # Manage secrets (dogfood tool)
│   └── reindex.py                 # Force reindex (dogfood tool)
├── metadata.json                  # TinyDB index (auto-generated, gitignored)
├── .mcp.json                      # Claude Code MCP registration
├── pyproject.toml                 # uv project (sherpa server deps)
└── CLAUDE.md                      # Agent instructions referencing SHERPA_STANDARDS.md
```

## 4. Tool Contract (for `SHERPA_STANDARDS.md`)

Every tool in `tools/` must follow this contract:

### 4.1 File Structure

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]  # PEP 723 inline deps
# ///
"""
name: jira_create_issue
description: Create a new Jira issue with summary and description.
categories: [jira, project-management, ticketing]
secrets:
  - JIRA_TOKEN
  - JIRA_URL
"""

import argparse
import json
import sys

def main():
    parser = argparse.ArgumentParser(description="Create a new Jira issue.")
    parser.add_argument("--summary", required=True, help="Issue summary")
    parser.add_argument("--description", default="", help="Issue description")
    parser.add_argument("--project", default="PROJ", help="Project key")
    args = parser.parse_args()

    # Read secrets from vault
    # ... (tools read ~/.sherpa/vault.json directly via json.load)

    # Output result as JSON to stdout
    print(json.dumps({"key": "PROJ-123", "url": "..."}))

if __name__ == "__main__":
    main()
```

### 4.2 Docstring Format (Parsed by Indexer)

The module docstring is YAML with these fields:

- **`name`** (required): Snake-case identifier, must match filename without `.py`
- **`description`** (required): One-line human-readable summary. This is what `tool_search` matches against.
- **`categories`** (required): List of lowercase tags for search grouping.
- **`secrets`** (optional): List of vault key names the tool requires.

### 4.3 Conventions

- **Args:** Use `argparse`. `--help` is free.
- **Output:** Print results as JSON to stdout. Human-readable messages to stderr.
- **Errors:** Exit 0 on success. Exit 1 for usage errors (bad args, missing secrets). Exit 2 for runtime errors (API down, auth failed).
- **Secrets:** Read `~/.sherpa/vault.json` directly. No shared import needed — just `json.load(open(Path.home() / ".sherpa" / "vault.json"))`. If a required secret is missing, print `MISSING_SECRET: <KEY_NAME>` to stderr and exit 1.

## 5. `tool_search` Response Schema

```json
{
  "results": [
    {
      "name": "jira_create_issue",
      "description": "Create a new Jira issue with summary and description.",
      "categories": ["jira", "project-management", "ticketing"],
      "path": "tools/jira_create_issue.py",
      "secrets": ["JIRA_TOKEN", "JIRA_URL"]
    }
  ],
  "total": 1
}
```

### Search Behavior (V1):

- Match `query` against `name`, `description`, and `categories` fields.
- Case-insensitive substring/fuzzy matching.
- Return top 10 results ranked by relevance.
- If no results, return empty list (not an error).

### Search (Future):

- Embed `description` + `categories` using a local or remote embedding model.
- Store embeddings alongside metadata in TinyDB.
- Cosine similarity search on query embedding vs stored embeddings.

## 6. MCP Registration (`.mcp.json`)

```json
{
  "mcpServers": {
    "sherpa": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", ".", "python", "-m", "sherpa.server"]
    }
  }
}
```

## 7. `CLAUDE.md` Agent Instructions

The project `CLAUDE.md` should instruct the agent:

- When you need a tool you don't have, use `tool_search` to find one.
- If no tool exists, create one in `tools/` following `docs/SHERPA_STANDARDS.md`.
- Run tools with `uv run tools/<name>.py --help` then `uv run tools/<name>.py <args>`.
- If a tool reports `MISSING_SECRET`, ask the user for the value and store it using `uv run tools/vault_manager.py set <KEY> <VALUE>`.

## 8. Phase 1: Foundation & Standards

- **`docs/SHERPA_STANDARDS.md`:** Write the full tool contract (section 4 above, expanded).
- **Vault:** Implement `~/.sherpa/vault.json` creation + `tools/vault_manager.py` (set/get/list/delete secrets).
- **Indexer (`sherpa/indexer.py`):** `ast`-based docstring parser that reads `tools/*.py`, extracts YAML docstrings, writes `metadata.json` via TinyDB.

## 9. Phase 2: The Lean MCP Server

- **Server (`sherpa/server.py`):** A `fastmcp` server exposing only `tool_search(query)`. On each call, checks tool file mtimes against stored mtimes and lazy-reindexes changed files before searching.
- **Search (V1):** Keyword/fuzzy matching on name + description + categories.
- **Registration:** `.mcp.json` at repo root.

## 10. Phase 3: Dogfooding

- **System Tools:** `tools/vault_manager.py` and `tools/reindex.py` — both built as standard Sherpa tools.
- **First Real Tools:** Claude creates functional tools for actual workflows (Jira, Slack, Sentry, etc.) to validate the full loop.
- **CLAUDE.md:** Write the agent instructions so a fresh context knows how to use the system.

## 11. Technical Requirements

- **Storage:** **TinyDB** (JSON file-based) for all metadata. Human-readable, zero-setup, git-friendly.
- **Execution:** All tools *must* be valid `uv` scripts (PEP 723 inline metadata) for zero-config dependency isolation.
- **Standard:** All tools *must* conform to `docs/SHERPA_STANDARDS.md`.
- **Python:** >= 3.11
- **MCP:** `fastmcp` library for the server.
