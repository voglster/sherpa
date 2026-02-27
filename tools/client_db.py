#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymongo"]
# ///
"""
name: client_db
description: Search client instances by name and query their MongoDB databases (find, count, aggregate, list collections)
categories: [database, mongodb, clients, debugging]
secrets:
  - MONGO_RO_USERNAME
  - MONGO_RO_PASSWORD
usage: |
  search <QUERY>
  collections --instance <KEY>
  find --instance <KEY> --db <DB> --collection <COLL> [--filter '{}'] [--projection '{}'] [--limit 10] [--sort '{}'] [--rw]
  count --instance <KEY> --db <DB> --collection <COLL> [--filter '{}'] [--rw]
  aggregate --instance <KEY> --db <DB> --collection <COLL> --pipeline '[...]' [--rw]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pymongo import MongoClient

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


def _load_vault() -> dict:
    return json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}


def _load_secret(key: str) -> str:
    value = _load_vault().get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Gravi helpers
# ---------------------------------------------------------------------------

def _gravi_instances() -> list[dict]:
    """Get all instances from gravi CLI."""
    result = subprocess.run(
        ["gravi", "instances", "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"gravi instances failed: {result.stderr}", file=sys.stderr)
        sys.exit(2)
    data = json.loads(result.stdout)
    return data.get("instances", data) if isinstance(data, dict) else data


def _gravi_config(instance: str) -> dict:
    """Get config for a specific instance from gravi CLI."""
    result = subprocess.run(
        ["gravi", "config", instance, "--format", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"gravi config failed for '{instance}': {result.stderr}", file=sys.stderr)
        sys.exit(2)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def _tokenize(s: str) -> set[str]:
    """Split a string into lowercase tokens on common delimiters."""
    import re
    return set(re.split(r"[\s_\-./]+", s.lower())) - {""}


def _fuzzy_score(query: str, candidate: str) -> float:
    """Score how well query matches candidate. Higher is better, 0 means no match."""
    q = query.lower()
    c = candidate.lower()

    # Exact match
    if q == c:
        return 100.0

    # Starts with
    if c.startswith(q):
        return 80.0

    # Contains as substring
    if q in c:
        return 60.0

    # Token overlap
    q_tokens = _tokenize(query)
    c_tokens = _tokenize(candidate)
    if q_tokens and c_tokens:
        overlap = len(q_tokens & c_tokens)
        if overlap > 0:
            return 40.0 * (overlap / len(q_tokens))

    return 0.0


def _match_instance(query: str, instance: dict) -> float:
    """Score an instance against a query, checking key and name."""
    key_score = _fuzzy_score(query, instance.get("key", ""))
    name_score = _fuzzy_score(query, instance.get("name", ""))
    return max(key_score, name_score)


# ---------------------------------------------------------------------------
# MongoDB connection
# ---------------------------------------------------------------------------

def _resolve_db_name(config: dict, logical_name: str) -> str:
    """Resolve a logical db name (e.g. 'backend') to actual db name via gravi config."""
    dbs = config.get("dbs", {})
    if logical_name in dbs:
        return dbs[logical_name]
    # Maybe they passed the actual db name directly
    if logical_name in dbs.values():
        return logical_name
    available = list(dbs.keys())
    print(
        f"Unknown database '{logical_name}'. Available: {available}",
        file=sys.stderr,
    )
    sys.exit(1)


def _connect(config: dict, db_name: str, rw: bool = False) -> MongoClient:
    """Create a MongoClient for the given config and database."""
    conn_str = config.get("conn_str", "")
    if not conn_str:
        print("No conn_str found in gravi config", file=sys.stderr)
        sys.exit(2)

    if rw:
        username = _load_secret("MONGO_RW_USERNAME")
        password = _load_secret("MONGO_RW_PASSWORD")
    else:
        username = _load_secret("MONGO_RO_USERNAME")
        password = _load_secret("MONGO_RO_PASSWORD")

    # conn_str from gravi may be a full URI (mongodb+srv://host/) or just a host.
    # Either way, inject credentials and target db.
    from urllib.parse import quote_plus, urlparse

    user_encoded = quote_plus(username)
    pass_encoded = quote_plus(password)

    if conn_str.startswith("mongodb"):
        parsed = urlparse(conn_str)
        # Rebuild with credentials injected
        uri = f"{parsed.scheme}://{user_encoded}:{pass_encoded}@{parsed.hostname}{parsed.path or '/'}{db_name}?authSource=admin"
    else:
        uri = f"mongodb+srv://{user_encoded}:{pass_encoded}@{conn_str}/{db_name}?authSource=admin"

    print(f"Connecting to {db_name}...", file=sys.stderr)
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    # Force connection check
    client.admin.command("ping")
    return client


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> None:
    """Fuzzy-search instances by name/key."""
    instances = _gravi_instances()
    scored = []
    for inst in instances:
        score = _match_instance(args.query, inst)
        if score > 0:
            scored.append((score, inst))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:5]

    results = []
    for score, inst in top:
        results.append({
            "key": inst.get("key"),
            "name": inst.get("name"),
            "type": inst.get("type"),
            "url": inst.get("url"),
        })

    print(json.dumps({"query": args.query, "results": results}, indent=2))


def cmd_config(args: argparse.Namespace) -> None:
    """Show connection info and available databases for an instance."""
    config = _gravi_config(args.instance)
    output = {
        "instance": args.instance,
        "conn_str": config.get("conn_str"),
        "dbs": config.get("dbs", {}),
    }
    print(json.dumps(output, indent=2))


def cmd_collections(args: argparse.Namespace) -> None:
    """List collections in a database."""
    config = _gravi_config(args.instance)
    actual_db = _resolve_db_name(config, args.db)

    client = _connect(config, actual_db, rw=args.rw)
    try:
        db = client[actual_db]
        collections = sorted(db.list_collection_names())
        print(json.dumps({
            "instance": args.instance,
            "db": args.db,
            "actual_db": actual_db,
            "collections": collections,
        }, indent=2))
    finally:
        client.close()


def cmd_find(args: argparse.Namespace) -> None:
    """Query documents from a collection."""
    config = _gravi_config(args.instance)
    actual_db = _resolve_db_name(config, args.db)

    filter_doc = json.loads(args.filter) if args.filter else {}
    projection = json.loads(args.projection) if args.projection else None
    sort_spec = json.loads(args.sort) if args.sort else None

    client = _connect(config, actual_db, rw=args.rw)
    try:
        db = client[actual_db]
        coll = db[args.collection]

        cursor = coll.find(filter_doc, projection)
        if sort_spec:
            cursor = cursor.sort(list(sort_spec.items()))
        cursor = cursor.limit(args.limit)

        docs = []
        for doc in cursor:
            docs.append(_serialize_doc(doc))

        print(json.dumps({
            "instance": args.instance,
            "db": args.db,
            "collection": args.collection,
            "count": len(docs),
            "documents": docs,
        }, indent=2))
    finally:
        client.close()


def cmd_count(args: argparse.Namespace) -> None:
    """Count documents matching a filter."""
    config = _gravi_config(args.instance)
    actual_db = _resolve_db_name(config, args.db)

    filter_doc = json.loads(args.filter) if args.filter else {}

    client = _connect(config, actual_db, rw=args.rw)
    try:
        db = client[actual_db]
        coll = db[args.collection]
        count = coll.count_documents(filter_doc)

        print(json.dumps({
            "instance": args.instance,
            "db": args.db,
            "collection": args.collection,
            "filter": filter_doc,
            "count": count,
        }, indent=2))
    finally:
        client.close()


def cmd_aggregate(args: argparse.Namespace) -> None:
    """Run an aggregation pipeline."""
    config = _gravi_config(args.instance)
    actual_db = _resolve_db_name(config, args.db)

    pipeline = json.loads(args.pipeline)
    if not isinstance(pipeline, list):
        print("--pipeline must be a JSON array", file=sys.stderr)
        sys.exit(1)

    client = _connect(config, actual_db, rw=args.rw)
    try:
        db = client[actual_db]
        coll = db[args.collection]
        results = [_serialize_doc(doc) for doc in coll.aggregate(pipeline)]

        print(json.dumps({
            "instance": args.instance,
            "db": args.db,
            "collection": args.collection,
            "count": len(results),
            "results": results,
        }, indent=2))
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_doc(doc: dict) -> dict:
    """Make a MongoDB document JSON-serializable."""
    from bson import ObjectId
    from datetime import datetime

    def _convert(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return obj.hex()
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_convert(i) for i in obj]
        return obj

    return _convert(doc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Search client instances and query their MongoDB databases.",
    )
    sub = parser.add_subparsers(dest="command")

    # search
    p = sub.add_parser("search", help="Fuzzy-search instances by name")
    p.add_argument("query", help="Search query (e.g. 'aero test')")

    # config
    p = sub.add_parser("config", help="Show connection info and databases for an instance")
    p.add_argument("--instance", required=True, help="Exact instance key")

    # collections
    p = sub.add_parser("collections", help="List collections in a database")
    p.add_argument("--instance", required=True, help="Exact instance key")
    p.add_argument("--db", required=True, help="Logical database name (e.g. 'backend')")
    p.add_argument("--rw", action="store_true", help="Use read-write credentials")

    # find
    p = sub.add_parser("find", help="Query documents from a collection")
    p.add_argument("--instance", required=True, help="Exact instance key")
    p.add_argument("--db", required=True, help="Logical database name")
    p.add_argument("--collection", required=True, help="Collection name")
    p.add_argument("--filter", default=None, help="Query filter as JSON (default: {})")
    p.add_argument("--projection", default=None, help="Field projection as JSON")
    p.add_argument("--limit", type=int, default=10, help="Max documents to return (default: 10)")
    p.add_argument("--sort", default=None, help="Sort spec as JSON (e.g. '{\"created\": -1}')")
    p.add_argument("--rw", action="store_true", help="Use read-write credentials")

    # count
    p = sub.add_parser("count", help="Count documents matching a filter")
    p.add_argument("--instance", required=True, help="Exact instance key")
    p.add_argument("--db", required=True, help="Logical database name")
    p.add_argument("--collection", required=True, help="Collection name")
    p.add_argument("--filter", default=None, help="Query filter as JSON (default: {})")
    p.add_argument("--rw", action="store_true", help="Use read-write credentials")

    # aggregate
    p = sub.add_parser("aggregate", help="Run an aggregation pipeline")
    p.add_argument("--instance", required=True, help="Exact instance key")
    p.add_argument("--db", required=True, help="Logical database name")
    p.add_argument("--collection", required=True, help="Collection name")
    p.add_argument("--pipeline", required=True, help="Aggregation pipeline as JSON array")
    p.add_argument("--rw", action="store_true", help="Use read-write credentials")

    args = parser.parse_args()

    match args.command:
        case "search":
            cmd_search(args)
        case "config":
            cmd_config(args)
        case "collections":
            cmd_collections(args)
        case "find":
            cmd_find(args)
        case "count":
            cmd_count(args)
        case "aggregate":
            cmd_aggregate(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
