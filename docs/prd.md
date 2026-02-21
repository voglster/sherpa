# PRD: Project Sherpa (Progressive Tool Discovery)

## 1. Executive Summary

**Sherpa** is a framework that enables LLM agents (like Claude Code) to autonomously navigate, utilize, and expand a local library of automation tools. By utilizing **Progressive Discoverability**, it solves the "Capability Paradox"—giving agents infinite potential tools without overwhelming their reasoning capacity or context window.

## 2. Problem Statement

Current LLM agent workflows suffer from:

- **The Context Ceiling:** Every new tool added to a system prompt consumes tokens and reduces the model's "attention" on the actual task.
- **The Capability Gap:** Agents are often stuck with a static set of tools; they cannot easily "learn" or "build" a new integration when they hit a wall.
- **Credential Friction:** Managing API keys across dozens of disparate scripts is disorganized and insecure.

## 3. Product Principles

1. **Pull, Don't Push:** The agent is given a single "search" tool. It must ask for what it needs rather than being force-fed every definition.
2. **Native Execution:** The agent runs tools directly via `uv run` — no wrapper, no proxy. The MCP is only a search index, not an execution engine.
3. **Convention Over Configuration:** All tools follow the same standard pattern (`SHERPA_STANDARDS.md`), so the agent knows how to inspect (`--help`) and run any tool without special instructions.
4. **Autonomous Evolution:** The framework must treat the agent as a "Senior Developer" capable of writing, testing, and registering its own tools.
5. **No-Ceremony Storage:** Use document-oriented, file-based storage (TinyDB) to keep the system state human-readable and easy to debug.

## 4. Functional Requirements

### 4.1 Discovery (The Search Index)

- **Intent-to-Tool Mapping:** The MCP provides a single `tool_search(query)` interface that accepts natural language keywords and returns a ranked list of relevant scripts.
- **Lazy Indexing:** The index is rebuilt on-demand when `tool_search` is called and files have changed (based on mtime). No background process, no startup step.
- **Embedding-Ready:** The index stores tool descriptions and metadata in a structure that supports future embedding-based semantic search (local or remote models). V1 uses keyword/fuzzy matching.
- **Document-Based Metadata:** Search results are served from a flat-file JSON database (TinyDB), tracking tool name, description, categories, required secrets, and last-used status.

### 4.2 Inspection & Execution (Native Agent Capabilities)

The agent handles inspection and execution directly — no MCP tools needed:

- **Inspection:** `uv run tools/<name>.py --help` — every tool must implement a standard `--help` that outputs a concise usage summary (args, required secrets, examples).
- **Execution:** `uv run tools/<name>.py <args>` — every tool is a valid PEP 723 uv script with inline dependencies.
- **Isolation:** `uv` provides automatic dependency isolation per-script. No venv management needed.
- **Error Convention:** Tools exit 0 on success, non-zero on failure, with structured stderr output so the agent can distinguish "I used this wrong" from "the API is down."

## 5. Secret & Configuration Management

- **The Vault:** A simple JSON file (`~/.sherpa/vault.json`) that tools read directly via a shared helper module.
- **Fail-Loud:** If a tool needs a secret that's missing, it prints a clear error message (e.g., `MISSING_SECRET: JIRA_TOKEN`). The agent sees this, asks the user, and can store it via a vault utility tool.
- **Scope:** Support for global secrets (used by many tools) and tool-specific secrets, both in the same vault file.

## 6. Tool Lifecycle (The Agent-Dev Loop)

- **Self-Registration:** The agent writes a `.py` file to `tools/` following `SHERPA_STANDARDS.md`. The lazy indexer picks it up on next search.
- **Iterative Refinement:** The agent reads, modifies, and re-tests its own tools using native file access and `uv run`.

## 7. User Experience

- **The "Invisible" Setup:** A user drops a script into `tools/`, and the agent finds it on next search.
- **The "Helper" Experience:** When a secret is missing, the tool tells the agent exactly what's needed, and the agent asks the user directly.

## 8. Success Metrics

- **Context Efficiency:** Initial system prompt size remains constant regardless of tool count (only `tool_search` is registered).
- **Agent Autonomy:** The agent successfully creates and registers a working tool for a new task without user code-writing.
