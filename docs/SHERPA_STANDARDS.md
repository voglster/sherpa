# Sherpa Tool Standards

Every tool in `tools/` must follow this contract.

## File Structure

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]  # PEP 723 inline deps — list only what the tool needs
# ///
"""
name: my_tool_name
description: One-line summary of what this tool does.
categories: [category1, category2]
secrets:
  - SECRET_KEY_NAME
usage: |
  subcommand <POSITIONAL_ARG> [--flag VALUE]
"""

import argparse
import json
import sys

def main():
    parser = argparse.ArgumentParser(description="One-line summary.")
    # For tools with subcommands, use add_subparsers instead
    parser.add_argument("--arg", required=True, help="Describe the arg")
    args = parser.parse_args()

    # Do work...
    print(json.dumps({"result": "value"}))

if __name__ == "__main__":
    main()
```

## Docstring Format

The module docstring is YAML with these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Snake-case identifier. **Must match filename** without `.py`. |
| `description` | Yes | One-line human-readable summary. Searched by `tool_search`. |
| `categories` | Yes | List of lowercase tags for search grouping. |
| `secrets` | No | List of vault key names the tool requires. |
| `usage` | No | Multi-line CLI usage examples showing subcommands and args. Returned in `tool_search` results. |

## Conventions

- **Shebang:** `#!/usr/bin/env python3`
- **PEP 723:** Every tool must have an inline script metadata block (`# /// script` ... `# ///`), even if `dependencies` is empty.
- **Args:** Use `argparse`. `--help` is automatic.
- **Output:** Print results as JSON to stdout. Human-readable status messages to stderr.
- **Exit codes:**
  - `0` — Success
  - `1` — Usage error (bad args, missing secrets)
  - `2` — Runtime error (API down, auth failed)
- **Secrets:** Read `~/.sherpa/vault.json` directly:
  ```python
  vault_path = Path.home() / ".sherpa" / "vault.json"
  vault = json.loads(vault_path.read_text()) if vault_path.exists() else {}
  token = vault.get("MY_SECRET")
  if not token:
      print("MISSING_SECRET: MY_SECRET", file=sys.stderr)
      sys.exit(1)
  ```

## New Tool Checklist

1. Create `tools/<name>.py` following the template above.
2. Ensure `name` in YAML docstring matches the filename (without `.py`).
3. Add only the deps the tool needs to the PEP 723 block.
4. Test: `uv run tools/<name>.py --help`
5. Test: run with valid args, verify JSON stdout.
6. The tool will be auto-indexed on the next `tool_search` call.

---

## Workflows

Workflows are YAML files in `workflows/` that describe multi-step processes chaining multiple tools together. They are **informational** — Claude reads the steps and executes them; there is no automated runner. Workflows are indexed alongside tools and appear in `tool_search` results.

### YAML Schema

```yaml
name: my_workflow_name
description: One-line summary of what this workflow accomplishes.
categories: [category1, category2]
steps:
  - tool: tool_name
    action: "What to do with this tool"
    args: "--flag <placeholder>"
  - tool: null
    action: "A reasoning or decision step (no tool invoked)"
```

### Field Reference

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Snake-case identifier. **Must match filename** without `.yaml`. |
| `description` | Yes | One-line human-readable summary. Searched by `tool_search`. |
| `categories` | Yes | List of lowercase tags for search grouping. |
| `steps` | Yes | Ordered list of step objects. |

### Step Fields

| Field | Required | Description |
|-------|----------|-------------|
| `tool` | Yes | Tool name (string) or `null` for reasoning/decision steps. |
| `action` | Yes | Human-readable description of what to do in this step. |
| `args` | No | CLI args template. Use `<placeholder>` for values to be filled in at runtime. |

### Conventions

- **Filename must match name:** `workflows/my_workflow.yaml` → `name: my_workflow`
- **`tool: null`** for reasoning steps: analysis, decisions, branching logic
- **Keep steps atomic:** each step should do one thing
- **Placeholders:** use `<angle_brackets>` for values that vary per invocation

### New Workflow Checklist

1. Create `workflows/<name>.yaml` following the schema above.
2. Ensure `name` matches the filename (without `.yaml`).
3. Include at least `name`, `description`, `categories`, and `steps`.
4. Verify each `tool` reference matches an existing tool name (or use `null`).
5. Test: `uv run tools/reindex.py` and confirm the workflow appears.
6. Test: search for a relevant keyword via `tool_search` and verify the workflow is returned.
