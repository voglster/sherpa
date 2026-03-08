#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["lancedb", "openai", "pyarrow", "pandas", "httpx"]
# ///
"""
name: notes_search
description: Semantic search across markdown notes using LanceDB vector embeddings.
categories: [notes, search, knowledge, ai, embeddings]
secrets:
  - LITELLM_API_URL
  - LITELLM_API_KEY
usage: |
  search --query "how does auth work" [--top-k 5]
  index [--clear]
  add --content "some important fact" [--source "meeting-2024-01"]
  status
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

DB_PATH = Path.home() / ".sherpa" / "notes.lance"
SKIP_HASHES_PATH = Path.home() / ".sherpa" / "notes_skip_hashes.json"
TABLE_NAME = "notes"
EMBEDDING_MODEL = "ollama/nomic-embed-text:latest"
EMBEDDING_DIM = 768
VAULT_PATH = Path.home() / ".sherpa" / "vault.json"

SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
    pa.field("text", pa.string()),
    pa.field("source", pa.string()),
    pa.field("content_hash", pa.string()),
    pa.field("indexed_at", pa.string()),
])

FILTER_SYSTEM_PROMPT = """\
You are a note classifier. Given the content of a markdown file, decide whether it should be indexed for semantic search.

DECISION criteria:
- INDEX: ideas, decisions, how-tos, meeting notes, project context, technical explanations, personal insights
- SKIP: empty files, boilerplate, templates, changelogs, tables of contents, auto-generated content, pure link lists

Respond in this exact format:

DECISION: INDEX or SKIP
IDEAS:
- bullet point 1
- bullet point 2
...

If SKIP, omit the IDEAS section. Extract 3-8 key idea bullet points that capture the essential knowledge in the note. Each bullet should be a self-contained statement useful for semantic search."""


def _load_vault() -> dict:
    if VAULT_PATH.exists():
        return json.loads(VAULT_PATH.read_text())
    return {}


def _require_secrets(vault: dict) -> tuple[str, str]:
    url = vault.get("LITELLM_API_URL")
    key = vault.get("LITELLM_API_KEY")
    missing = []
    if not url:
        missing.append("LITELLM_API_URL")
    if not key:
        missing.append("LITELLM_API_KEY")
    if missing:
        for m in missing:
            print(f"MISSING_SECRET: {m}", file=sys.stderr)
        sys.exit(1)
    return url.rstrip("/"), key


def _make_client(vault: dict):
    import openai

    url, key = _require_secrets(vault)
    return openai.OpenAI(api_key=key, base_url=url)


def _get_notes_dir(vault: dict) -> Path:
    notes_dir = Path(vault.get("NOTES_DIR", "~/notes")).expanduser()
    if not notes_dir.is_dir():
        print(f"Notes directory not found: {notes_dir}", file=sys.stderr)
        sys.exit(2)
    return notes_dir


def _embed(vault: dict, texts: list[str]) -> list[list[float]]:
    """Call embeddings endpoint directly to avoid openai SDK sending encoding_format=base64."""
    import httpx

    url, key = _require_secrets(vault)
    resp = httpx.post(
        f"{url}/embeddings",
        json={"model": EMBEDDING_MODEL, "input": texts},
        headers={"Authorization": f"Bearer {key}"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return [item["embedding"] for item in data]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _filter_and_extract(client, model: str, content: str, filepath: str) -> tuple[bool, str]:
    truncated = content[:8000]
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": FILTER_SYSTEM_PROMPT},
            {"role": "user", "content": f"File: {filepath}\n\n{truncated}"},
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
    }
    if "qwen" in model.lower():
        kwargs["reasoning_effort"] = "none"
    response = client.chat.completions.create(**kwargs)
    text = response.choices[0].message.content or ""

    # Parse decision
    should_index = False
    ideas_text = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("DECISION:"):
            decision = stripped.split(":", 1)[1].strip().upper()
            should_index = decision == "INDEX"
            break

    if should_index and "IDEAS:" in text:
        ideas_section = text.split("IDEAS:", 1)[1].strip()
        ideas_text = ideas_section

    if should_index and not ideas_text:
        # Default to SKIP if we couldn't parse ideas
        return False, ""

    return should_index, ideas_text


def _load_skip_hashes() -> set[str]:
    if SKIP_HASHES_PATH.exists():
        return set(json.loads(SKIP_HASHES_PATH.read_text()))
    return set()


def _save_skip_hashes(hashes: set[str]) -> None:
    SKIP_HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKIP_HASHES_PATH.write_text(json.dumps(sorted(hashes)))


def _open_db():
    import lancedb

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(DB_PATH))


def _get_or_create_table(db):
    try:
        return db.open_table(TABLE_NAME)
    except Exception:
        return db.create_table(TABLE_NAME, schema=SCHEMA)


def cmd_search(args, vault):
    db = _open_db()
    try:
        table = db.open_table(TABLE_NAME)
    except Exception:
        print(json.dumps({"query": args.query, "results": [], "count": 0}))
        return

    query_vector = _embed(vault, [args.query])[0]
    results_df = table.search(query_vector).limit(args.top_k).to_pandas()

    results = []
    for _, row in results_df.iterrows():
        results.append({
            "text": row["text"],
            "source": row["source"],
            "distance": float(row["_distance"]),
        })

    print(json.dumps({"query": args.query, "results": results, "count": len(results)}))


def cmd_index(args, vault):
    model = vault.get("LITELLM_DEFAULT_MODEL")
    if not model:
        print("MISSING_SECRET: LITELLM_DEFAULT_MODEL", file=sys.stderr)
        sys.exit(1)

    client = _make_client(vault)
    notes_dir = _get_notes_dir(vault)
    db = _open_db()

    if args.clear:
        try:
            db.drop_table(TABLE_NAME)
        except Exception:
            pass
        table = db.create_table(TABLE_NAME, schema=SCHEMA)
        existing_hashes = set()
        skip_hashes = set()
        print("Cleared existing index.", file=sys.stderr)
    else:
        table = _get_or_create_table(db)
        try:
            existing_df = table.to_pandas()
            existing_hashes = set(existing_df["content_hash"].tolist())
        except Exception:
            existing_hashes = set()
        skip_hashes = _load_skip_hashes()

    indexed = 0
    skipped_unchanged = 0
    skipped_junk = 0
    errors = 0

    for md_file in sorted(notes_dir.rglob("*.md")):
        # Skip hidden dirs and template files
        parts = md_file.relative_to(notes_dir).parts
        if any(p.startswith(".") for p in parts):
            continue
        if md_file.name.startswith("_"):
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            print(f"Skipping {md_file}: {e}", file=sys.stderr)
            errors += 1
            continue

        if not content.strip():
            skipped_junk += 1
            continue

        ch = _content_hash(content)
        if ch in existing_hashes or ch in skip_hashes:
            skipped_unchanged += 1
            continue

        # LLM filter + extract
        try:
            should_index, ideas_text = _filter_and_extract(
                client, model, content, str(md_file.relative_to(notes_dir))
            )
        except Exception as e:
            print(f"LLM error for {md_file}: {e}", file=sys.stderr)
            errors += 1
            continue

        if not should_index:
            skip_hashes.add(ch)
            _save_skip_hashes(skip_hashes)
            skipped_junk += 1
            continue

        # Embed and add
        try:
            vectors = _embed(vault, [ideas_text])
        except Exception as e:
            print(f"Embedding error for {md_file}: {e}", file=sys.stderr)
            errors += 1
            continue

        table.add([{
            "vector": vectors[0],
            "text": ideas_text,
            "source": str(md_file),
            "content_hash": ch,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }])
        existing_hashes.add(ch)
        indexed += 1
        print(f"Indexed: {md_file.relative_to(notes_dir)}", file=sys.stderr)

    _save_skip_hashes(skip_hashes)
    total_in_db = table.count_rows()
    result = {
        "indexed": indexed,
        "skipped_unchanged": skipped_unchanged,
        "skipped_junk": skipped_junk,
        "errors": errors,
        "total_in_db": total_in_db,
    }
    print(json.dumps(result))


def cmd_add(args, vault):
    db = _open_db()
    table = _get_or_create_table(db)

    ch = _content_hash(args.content)

    # Check for dupe
    try:
        existing_df = table.to_pandas()
        if ch in existing_df["content_hash"].values:
            print(json.dumps({"status": "duplicate", "content_hash": ch}))
            return
    except Exception:
        pass

    vectors = _embed(vault, [args.content])
    source = args.source or "manual"

    table.add([{
        "vector": vectors[0],
        "text": args.content,
        "source": source,
        "content_hash": ch,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }])

    print(json.dumps({"status": "added", "source": source, "content_hash": ch}))


def cmd_status(args, vault):
    notes_dir_path = vault.get("NOTES_DIR", "~/notes")
    notes_dir = Path(notes_dir_path).expanduser()

    db = _open_db()
    try:
        table = db.open_table(TABLE_NAME)
        df = table.to_pandas()
        total = len(df)
        last_indexed = df["indexed_at"].max() if total > 0 else None
        sources = df["source"].nunique() if total > 0 else 0
    except Exception:
        total = 0
        last_indexed = None
        sources = 0

    print(json.dumps({
        "total_docs": total,
        "last_indexed": last_indexed,
        "sources_count": sources,
        "db_path": str(DB_PATH),
        "notes_dir": str(notes_dir),
        "notes_dir_exists": notes_dir.is_dir(),
    }))


def main():
    parser = argparse.ArgumentParser(
        description="Semantic search across markdown notes using LanceDB vector embeddings."
    )
    sub = parser.add_subparsers(dest="command")

    search_p = sub.add_parser("search", help="Semantic search notes")
    search_p.add_argument("--query", required=True, help="Search query")
    search_p.add_argument("--top-k", type=int, default=5, help="Number of results (default: 5)")

    index_p = sub.add_parser("index", help="Index markdown files from notes directory")
    index_p.add_argument("--clear", action="store_true", help="Clear index before re-indexing")

    add_p = sub.add_parser("add", help="Add content manually")
    add_p.add_argument("--content", required=True, help="Content to add")
    add_p.add_argument("--source", default=None, help="Source label (default: manual)")

    sub.add_parser("status", help="Show index status")

    args = parser.parse_args()
    vault = _load_vault()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        cmd_search(args, vault)
    elif args.command == "index":
        cmd_index(args, vault)
    elif args.command == "add":
        cmd_add(args, vault)
    elif args.command == "status":
        cmd_status(args, vault)


if __name__ == "__main__":
    main()
