"""Sherpa tool indexer — parses YAML docstrings from tools/*.py and writes metadata.json."""

import ast
import os
from pathlib import Path

import yaml
from tinydb import TinyDB

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
WORKFLOWS_DIR = PROJECT_ROOT / "workflows"
DB_PATH = PROJECT_ROOT / "metadata.json"


def _parse_tool_metadata(filepath: Path) -> dict | None:
    """Extract YAML metadata from a tool's module docstring.

    Returns a dict with name, description, categories, secrets, path, mtime
    or None if the file has no valid YAML docstring.
    """
    try:
        source = filepath.read_text()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return None

    docstring = ast.get_docstring(tree)
    if not docstring:
        return None

    try:
        meta = yaml.safe_load(docstring)
    except yaml.YAMLError:
        return None

    if not isinstance(meta, dict):
        return None

    # Require name, description, categories
    if not all(k in meta for k in ("name", "description", "categories")):
        return None

    return {
        "name": meta["name"],
        "description": meta["description"],
        "categories": meta["categories"],
        "secrets": meta.get("secrets", []),
        "path": str(filepath.relative_to(PROJECT_ROOT)),
        "mtime": filepath.stat().st_mtime,
    }


def _parse_workflow_metadata(filepath: Path) -> dict | None:
    """Extract metadata from a workflow YAML file.

    Returns a dict with name, description, categories, steps, path, mtime
    or None if the file is invalid.
    """
    try:
        meta = yaml.safe_load(filepath.read_text())
    except (yaml.YAMLError, OSError):
        return None

    if not isinstance(meta, dict):
        return None

    if not all(k in meta for k in ("name", "description", "categories", "steps")):
        return None

    return {
        "name": meta["name"],
        "description": meta["description"],
        "categories": meta["categories"],
        "steps": meta["steps"],
        "path": str(filepath.relative_to(PROJECT_ROOT)),
        "mtime": filepath.stat().st_mtime,
    }


def index_all() -> dict:
    """Scan tools/*.py and workflows/*.yaml, truncate the TinyDB index, and rebuild it.

    Returns a stats dict: {"indexed": N, "skipped": N, "tools": [names], "workflows": [names]}.
    """
    db = TinyDB(DB_PATH)
    db.truncate()

    indexed = 0
    skipped = 0
    tool_names = []

    if TOOLS_DIR.is_dir():
        for py_file in sorted(TOOLS_DIR.glob("*.py")):
            meta = _parse_tool_metadata(py_file)
            if meta:
                db.insert(meta)
                tool_names.append(meta["name"])
                indexed += 1
            else:
                skipped += 1

    workflows_table = db.table("workflows")
    workflows_table.truncate()
    workflow_names = []

    if WORKFLOWS_DIR.is_dir():
        for yaml_file in sorted(WORKFLOWS_DIR.glob("*.yaml")):
            meta = _parse_workflow_metadata(yaml_file)
            if meta:
                workflows_table.insert(meta)
                workflow_names.append(meta["name"])
                indexed += 1
            else:
                skipped += 1

    return {
        "indexed": indexed,
        "skipped": skipped,
        "tools": tool_names,
        "workflows": workflow_names,
    }


def index_if_changed() -> bool:
    """Reindex if any tool or workflow file has changed since the last index.

    Compares file mtimes against stored mtimes in the DB.
    Returns True if reindexing occurred.
    """
    has_tools = TOOLS_DIR.is_dir()
    has_workflows = WORKFLOWS_DIR.is_dir()

    if not has_tools and not has_workflows:
        return False

    if not DB_PATH.exists():
        index_all()
        return True

    db = TinyDB(DB_PATH)

    # Check tools
    if has_tools:
        stored_tools = {rec["name"]: rec["mtime"] for rec in db.all()}
        for py_file in TOOLS_DIR.glob("*.py"):
            meta = _parse_tool_metadata(py_file)
            if meta is None:
                continue
            stored_mtime = stored_tools.pop(meta["name"], None)
            if stored_mtime is None or py_file.stat().st_mtime != stored_mtime:
                index_all()
                return True
        if stored_tools:
            index_all()
            return True

    # Check workflows
    if has_workflows:
        workflows_table = db.table("workflows")
        stored_workflows = {rec["name"]: rec["mtime"] for rec in workflows_table.all()}
        for yaml_file in WORKFLOWS_DIR.glob("*.yaml"):
            meta = _parse_workflow_metadata(yaml_file)
            if meta is None:
                continue
            stored_mtime = stored_workflows.pop(meta["name"], None)
            if stored_mtime is None or yaml_file.stat().st_mtime != stored_mtime:
                index_all()
                return True
        if stored_workflows:
            index_all()
            return True

    return False


def get_all_tools() -> list[dict]:
    """Return all tool records from the TinyDB index."""
    if not DB_PATH.exists():
        return []
    db = TinyDB(DB_PATH)
    return db.all()


def get_all_workflows() -> list[dict]:
    """Return all workflow records from the TinyDB index."""
    if not DB_PATH.exists():
        return []
    db = TinyDB(DB_PATH)
    return db.table("workflows").all()


if __name__ == "__main__":
    import json
    stats = index_all()
    print(json.dumps(stats))
