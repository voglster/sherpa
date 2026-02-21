# Bootstrap Prompt

Paste this into a fresh Claude Code context from the `jim_tools/` repo root:

---

Read `docs/prd.md` and `docs/implementation-plan.md`. These are the spec and build plan for Project Sherpa — a lean MCP-based tool discovery system for Claude Code.

Execute Phase 1 then Phase 2 from the implementation plan:

**Phase 1 — Foundation & Standards:**
1. Write `docs/SHERPA_STANDARDS.md` — the full tool contract (expand section 4 of the impl plan into a standalone doc).
2. Create `sherpa/indexer.py` — the `ast`-based docstring parser that reads `tools/*.py`, extracts YAML docstrings, and writes `metadata.json` via TinyDB. Add `tinydb` to project deps.
3. Create `tools/vault_manager.py` — a standard Sherpa tool (PEP 723 uv script) for managing `~/.sherpa/vault.json` (subcommands: set, get, list, delete).
4. Create `tools/reindex.py` — a standard Sherpa tool that force-triggers the indexer.

**Phase 2 — The Lean MCP Server:**
5. Create `sherpa/server.py` — a `fastmcp` server exposing only `tool_search(query)`. Lazy reindexes on mtime changes. V1 keyword/fuzzy search. Add `fastmcp` to project deps.
6. Create `.mcp.json` at repo root to register the server.
7. Update `CLAUDE.md` with agent instructions for using Sherpa (when to search, how to run tools, how to handle missing secrets, how to create new tools).

Build each piece, test it, then move to the next. Don't commit until I've reviewed.
