# Implementation Plan: Project Sherpa (Lean Edition)

## 1. The "Lean" Architecture

Instead of a heavy MCP that manages file writes, we leverage the fact that Claude Code runs directly on the repository. The MCP server is reduced to a high-speed **Discovery and Execution** engine.

### Core MCP Tools (The Bridge):

- **`tool_search(query)`**: (Tier 1) Queries the local document-store (TinyDB) for relevant scripts based on intent.
- **`tool_inspect(name)`**: (Tier 2) Returns the YAML/Markdown manifest (docstrings, inputs, secrets) for a specific tool.
- **`tool_exec(name, params)`**: (Tier 3) The runner that injects secrets from the vault and executes via `uv`.

## 2. The Development Flow (Inside the Repo)

Since Claude is running in the repo, the workflow is:

1. **Creation:** Claude writes a `.py` file to `tools/` using native file system access, following the rules in `SHERPA_STANDARDS.md`.
2. **Indexing:** A background process (or the MCP on startup) detects the new file, parses the docstring, and updates the `metadata.json` (TinyDB).
3. **Usage:** Claude uses `tool_search` to find its new creation and `tool_exec` to run it.

## 3. Phase 1: Foundation & Standards

- **Standards:** Establish `SHERPA_STANDARDS.md` (The Contract).
- **The Vault:** Create a simple JSON vault utility for managing API keys.
- **The Indexer:** Build a small Python script that uses `ast` to parse `tools/` and populate `metadata.json` using TinyDB.

## 4. Phase 2: The Lean MCP Server

- **Server:** Build a `fastmcp` server that serves the TinyDB store and provides the `uv` execution wrapper.
- **Secret Injection:** Ensure the `tool_exec` command automatically maps vault keys to environment variables.

## 5. Phase 3: Dogfooding & Maintenance

- **System Tools:** Claude creates `reindex.py` and `vault_manager.py` within the `tools/` folder.
- **Capabilities:** Claude creates the first functional tools (Jira, Slack, Sentry).

## 6. Technical Requirements

- **Storage:** **TinyDB** (JSON file-based storage) for all metadata to ensure readability and zero-setup.
- **Execution:** All tools *must* be valid `uv` scripts (PEP 723) to ensure zero-config execution.