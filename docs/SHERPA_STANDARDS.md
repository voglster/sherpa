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
"""

import argparse
import json
import sys

def main():
    parser = argparse.ArgumentParser(description="One-line summary.")
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
