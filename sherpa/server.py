"""Sherpa MCP server — exposes tool_search for discovering tools."""

from fastmcp import FastMCP

from sherpa.indexer import get_all_tools, index_if_changed

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
    scored = []
    for tool in tools:
        s = _score_tool(tool, tokens)
        if s > 0:
            scored.append((s, tool))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:10]

    results = [
        {
            "name": t["name"],
            "description": t["description"],
            "categories": t["categories"],
            "path": t["path"],
            "secrets": t.get("secrets", []),
        }
        for _, t in top
    ]

    return {"results": results, "total": len(scored)}


if __name__ == "__main__":
    mcp.run()
