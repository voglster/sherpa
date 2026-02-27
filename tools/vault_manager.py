#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
name: vault_manager
description: Manage secrets in the Sherpa vault (~/.sherpa/vault.json).
categories: [sherpa, secrets, configuration]
usage: |
  set <KEY> <VALUE>
  get <KEY>
  list
  delete <KEY>
"""

import argparse
import json
import sys
from pathlib import Path

VAULT_DIR = Path.home() / ".sherpa"
VAULT_PATH = VAULT_DIR / "vault.json"


def _load_vault() -> dict:
    if VAULT_PATH.exists():
        return json.loads(VAULT_PATH.read_text())
    return {}


def _save_vault(vault: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    VAULT_PATH.write_text(json.dumps(vault, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Manage secrets in the Sherpa vault.")
    sub = parser.add_subparsers(dest="command")

    set_p = sub.add_parser("set", help="Set a secret")
    set_p.add_argument("key", help="Secret key name")
    set_p.add_argument("value", help="Secret value")

    get_p = sub.add_parser("get", help="Get a secret")
    get_p.add_argument("key", help="Secret key name")

    sub.add_parser("list", help="List all secret keys")

    del_p = sub.add_parser("delete", help="Delete a secret")
    del_p.add_argument("key", help="Secret key name")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "set":
        vault = _load_vault()
        vault[args.key] = args.value
        _save_vault(vault)
        print(json.dumps({"status": "ok", "key": args.key}))

    elif args.command == "get":
        vault = _load_vault()
        value = vault.get(args.key)
        if value is None:
            print(f"Key not found: {args.key}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"key": args.key, "value": value}))

    elif args.command == "list":
        vault = _load_vault()
        print(json.dumps({"keys": sorted(vault.keys())}))

    elif args.command == "delete":
        vault = _load_vault()
        if args.key not in vault:
            print(f"Key not found: {args.key}", file=sys.stderr)
            sys.exit(1)
        del vault[args.key]
        _save_vault(vault)
        print(json.dumps({"status": "ok", "key": args.key, "action": "deleted"}))


if __name__ == "__main__":
    main()
