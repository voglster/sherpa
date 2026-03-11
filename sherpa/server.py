"""Sherpa MCP server — exposes tool_search and tool_run for discovering and executing tools."""

import json
import os
import shlex
import subprocess

from fastmcp import FastMCP
from pathlib import Path

from sherpa.indexer import get_all_tools, get_all_workflows, get_tool_by_name, index_if_changed, PROJECT_ROOT

mcp = FastMCP("sherpa")


def _score_tool(tool: dict, tokens: list[str]) -> float:
    """Score a tool against query tokens.

    Weights: name=3, categories=2, description=1.
    Exact word matches get a 1.5x bonus.
    """
    name = tool["name"].lower()
    categories = " ".join(tool.get("categories", [])).lower()
    description = tool.get("description", "").lower()
    name_words = set(name.replace("_", " ").split())
    cat_words = set(categories.replace("-", " ").split())
    desc_words = set(description.split())

    score = 0.0
    for token in tokens:
        # Substring matches
        if token in name:
            score += 3.0
            if token in name_words:
                score += 3.0 * 0.5  # 1.5x total via bonus
        if token in categories:
            score += 2.0
            if token in cat_words:
                score += 2.0 * 0.5
        if token in description:
            score += 1.0
            if token in desc_words:
                score += 1.0 * 0.5

    return score


@mcp.tool()
def tool_search(query: str) -> dict:
    """Search for Sherpa tools by keyword.

    Searches tool names, descriptions, and categories. Returns the top 10 matches
    ranked by relevance.
    """
    index_if_changed()

    tokens = [t.lower() for t in query.split() if t]
    if not tokens:
        return {"results": [], "total": 0}

    tools = get_all_tools()
    workflows = get_all_workflows()

    scored = []
    for tool in tools:
        s = _score_tool(tool, tokens)
        if s > 0:
            scored.append((s, "tool", tool))

    for wf in workflows:
        s = _score_tool(wf, tokens)
        if s > 0:
            scored.append((s, "workflow", wf))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:10]

    results = []
    for _, item_type, item in top:
        entry = {
            "name": item["name"],
            "description": item["description"],
            "type": item_type,
        }
        if item.get("usage"):
            entry["usage"] = item["usage"]
        if item_type == "workflow":
            entry["steps"] = item["steps"]
        results.append(entry)

    return {
        "usage": (
            "For tools: run via tool_run(tool_name, args). Use the 'usage' field for arg syntax, "
            "or call tool_run(name, '--help') for full help. "
            "For workflows: follow the steps in order, executing each tool via tool_run."
        ),
        "results": results,
        "total": len(scored),
    }


@mcp.tool()
def tool_run(tool_name: str, args: str = "", cwd: str = "") -> dict:
    """Run a Sherpa tool by name.

    Executes the tool as a subprocess and returns its output. Use tool_search
    to discover available tools first.

    Args:
        tool_name: Name of the tool to run.
        args: CLI arguments to pass to the tool.
        cwd: Optional caller working directory hint. Passed to the tool as
             SHERPA_CALLER_CWD so tools like lumbergh can detect the correct
             session even when executed from the MCP server's own directory.
    """
    index_if_changed()

    tool = get_tool_by_name(tool_name)
    if not tool:
        return {"success": False, "error": f"Tool '{tool_name}' not found", "hint": "Use tool_search to find available tools."}

    # Pre-check secrets
    required_secrets = tool.get("secrets", [])
    if required_secrets:
        vault_path = Path.home() / ".sherpa" / "vault.json"
        vault = json.loads(vault_path.read_text()) if vault_path.exists() else {}
        missing = [s for s in required_secrets if s not in vault]
        if missing:
            return {
                "success": False,
                "error": f"Missing secrets: {', '.join(missing)}",
                "hint": f"Set them with: tool_run('vault_manager', 'set <KEY> <VALUE>') for each missing key.",
            }

    cmd = ["uv", "run", str(PROJECT_ROOT / tool["path"])] + shlex.split(args)

    env = None
    if cwd:
        env = {**os.environ, "SHERPA_CALLER_CWD": cwd}

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=PROJECT_ROOT, env=env)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Tool '{tool_name}' timed out after 120 seconds", "tool": tool_name}

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    # Check for MISSING_SECRET in stderr as fallback
    if result.returncode != 0 and stderr:
        for line in stderr.splitlines():
            if line.startswith("MISSING_SECRET:"):
                key = line.split(":", 1)[1].strip()
                return {
                    "success": False,
                    "error": f"Missing secret: {key}",
                    "hint": f"Set it with: tool_run('vault_manager', 'set {key} <VALUE>')",
                    "tool": tool_name,
                }

    # Try to parse stdout as JSON
    output = stdout
    if stdout:
        try:
            output = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            pass

    return {
        "success": result.returncode == 0,
        "exit_code": result.returncode,
        "output": output,
        "stderr": stderr or None,
        "tool": tool_name,
    }


if __name__ == "__main__":
    mcp.run()
