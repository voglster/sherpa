#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==0.1.3"]
# ///
"""
name: litellm_keys
description: Mint LiteLLM proxy keys with model permissions and place the secret into safe sinks (vault, .env, 1Password, clipboard).
categories: [ai, llm, admin, secrets]
axi: true
secrets:
  - LITELLM_API_URL
  - LITELLM_MASTER_KEY
usage: |
  generate --name <alias> [--models m1,m2] [--note "..."]
           [--vault [NAME]] [--env-file PATH [--var NAME]] [--op [ITEM]] [--clip] [--reveal] [--json]
  list [--fields models,spend,created] [--json]
  info <alias|hash> [--json]
  delete <alias|hash> [--json]
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


def _load_vault() -> dict:
    return json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}


def _config() -> tuple[str, str]:
    vault = _load_vault()
    url = vault.get("LITELLM_API_URL")
    key = vault.get("LITELLM_MASTER_KEY")
    missing = [n for n, v in (("LITELLM_API_URL", url), ("LITELLM_MASTER_KEY", key)) if not v]
    if missing:
        for name in missing:
            print(f"MISSING_SECRET: {name}", file=sys.stderr)
        fail(
            f"missing secret {missing[0]}",
            help=f"sherpa vault_manager set {missing[0]} <value>",
            usage=True,
        )
    return url.rstrip("/"), key


# --- secret sinks (all placement happens in-process; the raw value never
# --- reaches argv or the transcript) ---------------------------------------


def _derive_var(alias: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", alias).strip("_").upper()
    return f"LITELLM_KEY_{slug}"


def _mask(secret: str) -> str:
    tail = secret[-4:] if len(secret) >= 4 else secret
    return f"sk-…{tail}"


def _env_upsert(text: str, var: str, value: str) -> str:
    line = f"{var}={value}"
    out, replaced = [], False
    for raw in text.splitlines():
        stripped = raw.lstrip()
        candidate = stripped[len("export "):] if stripped.startswith("export ") else stripped
        name = candidate.split("=", 1)[0].strip() if "=" in candidate else None
        if name == var:
            out.append(line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(line)
    result = "\n".join(out)
    if not result.endswith("\n"):
        result += "\n"
    return result


def _vault_set(name: str, value: str, *, path: Path | None = None) -> None:
    path = path or VAULT_PATH
    vault = json.loads(path.read_text()) if path.exists() else {}
    vault[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(vault, indent=2))


def _op_template(title: str, value: str, note: str, hostname: str) -> dict:
    return {
        "title": title,
        "category": "API_CREDENTIAL",
        "fields": [
            {"id": "credential", "type": "CONCEALED", "label": "credential", "value": value},
            {"id": "hostname", "type": "STRING", "label": "hostname", "value": hostname},
            {"id": "notesPlain", "type": "STRING", "label": "notesPlain", "value": note},
        ],
    }


def _op_create(template: dict, *, vault: str = "Private", account: str | None = None) -> str:
    cmd = ["op", "item", "create", "--vault", vault, "--format", "json"]
    if account:
        cmd += ["--account", account]
    try:
        proc = subprocess.run(cmd, input=json.dumps(template), capture_output=True, text=True)
    except FileNotFoundError:
        fail("1Password CLI (op) not found", help="install op, then: op signin", usage=True)
    if proc.returncode != 0:
        fail(
            f"1Password item create failed: {proc.stderr.strip()[:200]}",
            help="ensure op is signed in: op signin",
            usage=True,
        )
    return json.loads(proc.stdout).get("id", "")


def _clip(value: str) -> str:
    for tool in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["pbcopy"]):
        try:
            subprocess.run(tool, input=value, text=True, check=True, capture_output=True)
            return tool[0]
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as exc:
            fail(f"clipboard copy via {tool[0]} failed: {exc.stderr.strip()[:120]}")
    fail("no clipboard tool found", help="install wl-clipboard or xclip", usage=True)


def _apply_sinks(args, secret: str, alias: str, note: str, hostname: str) -> list[dict]:
    destinations: list[dict] = []
    used_explicit = any([args.vault is not None, args.env_file, args.op is not None, args.clip])

    if args.vault is not None or not used_explicit:
        name = args.vault if isinstance(args.vault, str) and args.vault else _derive_var(alias)
        _vault_set(name, secret)
        destinations.append({"sink": "vault", "target": name})

    if args.env_file:
        path = Path(args.env_file).expanduser()
        var = args.var or _derive_var(alias)
        text = path.read_text() if path.exists() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_env_upsert(text, var, secret))
        destinations.append({"sink": "env", "target": f"{path}:{var}"})

    if args.op is not None:
        title = args.op if isinstance(args.op, str) and args.op else f"LiteLLM key: {alias}"
        item_id = _op_create(_op_template(title, secret, note, hostname), account="my.1password.com")
        destinations.append({"sink": "1password", "target": f"{title} ({item_id})"})

    if args.clip:
        tool = _clip(secret)
        destinations.append({"sink": "clipboard", "target": tool})

    return destinations


# --- LiteLLM admin client ---------------------------------------------------


def _api(url: str, key: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code in (400, 401, 403):
            fail(
                f"proxy rejected request ({exc.code}): {detail}",
                help="verify LITELLM_MASTER_KEY has admin rights on the proxy",
                usage=True,
            )
        fail(f"proxy error {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        fail(f"cannot reach proxy: {exc.reason}")


def _generate(url: str, key: str, alias: str, models: list[str], note: str) -> dict:
    body: dict = {"key_alias": alias, "models": models or []}
    if note:
        body["metadata"] = {"note": note}
    return _api(url, key, "/key/generate", "POST", body)


def _list_keys(url: str, key: str, page_size: int = 100) -> list[dict]:
    keys: list[dict] = []
    page = 1
    while True:
        resp = _api(
            url, key, f"/key/list?return_full_object=true&size={page_size}&page={page}"
        )
        keys.extend(resp.get("keys", []))
        total_pages = resp.get("total_pages")
        if total_pages is not None:
            if page >= total_pages:
                break
        elif len(resp.get("keys", [])) < page_size:
            break
        page += 1
    return keys


def _resolve(url: str, key: str, ident: str) -> dict | None:
    for record in _list_keys(url, key):
        if record.get("key_alias") == ident or record.get("token") == ident:
            return record
        if record.get("key_name", "").endswith(ident):
            return record
    return None


def _delete(url: str, key: str, token: str) -> dict:
    return _api(url, key, "/key/delete", "POST", {"keys": [token]})


# --- subcommands ------------------------------------------------------------


def cmd_generate(args) -> None:
    url, key = _config()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    result = _generate(url, key, args.name, models, args.note)
    secret = result["key"]
    destinations = _apply_sinks(args, secret, args.name, args.note, url)
    payload = {
        "alias": args.name,
        "key": _mask(secret),
        "models": result.get("models") or ["*all*"],
        "token": result.get("token", ""),
        "destinations": destinations,
        "help": [
            f"sherpa litellm_keys info {args.name}",
            f"sherpa litellm_keys delete {args.name}",
        ],
    }
    if args.reveal:
        payload["secret"] = secret
    emit(payload, as_json=args.json)


def _summarize(record: dict) -> dict:
    return {
        "alias": record.get("key_alias") or "(none)",
        "models": record.get("models") or ["*all*"],
        "spend": record.get("spend", 0.0),
        "created": (record.get("created_at") or "")[:10],
    }


def cmd_list(args) -> None:
    url, key = _config()
    records = _list_keys(url, key)
    rows = [_summarize(r) for r in records]
    payload = {
        "count": f"{len(rows)} of {len(rows)} total",
        "keys": rows,
        "help": ["sherpa litellm_keys info <ALIAS>"],
    }
    emit(payload, as_json=args.json)


def cmd_info(args) -> None:
    url, key = _config()
    record = _resolve(url, key, args.ident)
    if record is None:
        emit({"ident": args.ident, "found": False}, as_json=args.json)
        return
    note = (record.get("metadata") or {}).get("note", "")
    payload = {
        "alias": record.get("key_alias") or "(none)",
        "key_name": record.get("key_name", ""),
        "models": record.get("models") or ["*all*"],
        "note": note,
        "spend": record.get("spend", 0.0),
        "created": (record.get("created_at") or "")[:19],
        "expires": record.get("expires") or "never",
    }
    emit(payload, as_json=args.json)


def cmd_delete(args) -> None:
    url, key = _config()
    record = _resolve(url, key, args.ident)
    if record is None:
        emit({"ident": args.ident, "deleted": False, "note": "no such key"}, as_json=args.json)
        return
    _delete(url, key, record["token"])
    emit(
        {
            "ident": args.ident,
            "deleted": True,
            "alias": record.get("key_alias") or "(none)",
            "help": ["sherpa litellm_keys list"],
        },
        as_json=args.json,
    )


def _home() -> None:
    vault = _load_vault()
    url = vault.get("LITELLM_API_URL", "(unset)")
    has_master = "present" if vault.get("LITELLM_MASTER_KEY") else "MISSING"
    print(bin_line(Path(__file__).resolve()))
    print("description: Mint LiteLLM proxy keys and place the secret into safe sinks.")
    print(f"proxy: {url}")
    print(f"master_key: {has_master}")
    print("hint: sherpa litellm_keys generate --name <alias>")
    print("hint: sherpa litellm_keys list")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="litellm_keys", add_help=True)
    sub = parser.add_subparsers(dest="command")
    subparsers: dict[str, argparse.ArgumentParser] = {}

    g = sub.add_parser("generate")
    g.add_argument("--name", required=True)
    g.add_argument("--models", default="")
    g.add_argument("--note", default="")
    g.add_argument("--vault", nargs="?", const="", default=None)
    g.add_argument("--env-file", dest="env_file", default=None)
    g.add_argument("--var", default=None)
    g.add_argument("--op", nargs="?", const="", default=None)
    g.add_argument("--clip", action="store_true")
    g.add_argument("--reveal", action="store_true")
    g.add_argument("--json", action="store_true")
    subparsers["generate"] = g

    ls = sub.add_parser("list")
    ls.add_argument("--fields", default="")
    ls.add_argument("--json", action="store_true")
    subparsers["list"] = ls

    inf = sub.add_parser("info")
    inf.add_argument("ident")
    inf.add_argument("--json", action="store_true")
    subparsers["info"] = inf

    dl = sub.add_parser("delete")
    dl.add_argument("ident")
    dl.add_argument("--json", action="store_true")
    subparsers["delete"] = dl

    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        _home()
        return

    args = parse_strict(parser, subparsers, argv)
    if args.command is None:
        _home()
        return
    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "info":
        cmd_info(args)
    elif args.command == "delete":
        cmd_delete(args)


if __name__ == "__main__":
    main()
