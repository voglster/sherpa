"""Sherpa MCP server — exposes tool_search for discovering tools and workflows."""

from fastmcp import FastMCP

from sherpa.indexer import get_all_tools, get_all_workflows, index_if_changed, PROJECT_ROOT

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
            "path": item["path"],
        }
        if item.get("usage"):
            entry["usage"] = item["usage"]
        if item_type == "workflow":
            entry["steps"] = item["steps"]
        results.append(entry)

    return {
        "usage": (
            "For tools: use the 'usage' field for CLI syntax, or run `uv run <base_path>/<path> --help`. "
            "For workflows: follow the steps in order, executing each tool as described."
        ),
        "base_path": str(PROJECT_ROOT),
        "results": results,
        "total": len(scored),
    }


if __name__ == "__main__":
    mcp.run()
