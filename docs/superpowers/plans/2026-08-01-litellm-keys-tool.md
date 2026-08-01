# litellm_keys Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Sherpa CLI tool `litellm_keys` that mints LiteLLM proxy keys with a name + model permissions and places the secret into safe side-channel sinks (Sherpa vault, `.env`, 1Password, clipboard) without ever printing the raw secret to stdout or a shell command line.

**Architecture:** A single AXI-conformant tool at `tools/litellm_keys.py`. Thin stdlib-`urllib` HTTP layer against the LiteLLM admin API (`/key/generate`, `/key/list`, `/key/delete`); a set of pure, unit-tested "sink" helpers that place the secret in-process (so the value never appears in argv/transcript); and four subcommands (`generate`, `list`, `info`, `delete`) plus a no-args home view. Output goes through `sherpa.render.emit` (TOON), with `--json` for the raw shape.

**Tech Stack:** Python 3.11+, stdlib `urllib`/`subprocess`, `python-toon==0.1.3` via `sherpa.render`, `op` (1Password CLI) for the optional 1Password sink, pytest for unit tests.

## Global Constraints

- **AXI contract** (`docs/SHERPA_STANDARDS.md`): stdout is TOON via `emit()` only; every subcommand accepts `--json`; strict flag parsing via `parse_strict()`; no interactive prompts.
- **Exit codes:** `0` success/idempotent no-op; `1` uncooperative world (proxy unreachable, 5xx); `2` caller must fix flags or environment (unknown flag, missing secret, proxy 400/401/403).
- **Secret never on stdout or in a command line.** The raw `sk-...` value is printed to stdout ONLY when `--reveal` is passed. All sink placement happens in-process (write files directly; pipe to `op`/clipboard via **stdin**) — never shell out with the secret as an argument.
- **Missing secret protocol:** `MISSING_SECRET: <KEY>` on stderr AND structured `error:`/`help:` on stdout via `fail(..., usage=True)`, exit 2.
- **PEP 723 header** pinning `python-toon==0.1.3`. Preamble exactly:
  ```python
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
  from sherpa.render import bin_line, emit, fail, parse_strict, truncate
  ```
- **Docstring YAML:** `name: litellm_keys` (must match filename), `categories: [ai, llm, admin, secrets]`, `secrets: [LITELLM_MASTER_KEY, LITELLM_API_URL]`, `axi: true`.
- **Config secrets:** `LITELLM_API_URL` (proxy base), `LITELLM_MASTER_KEY` (admin key). The consumer `LITELLM_API_KEY` is NOT used by this tool.
- **`help` values in payloads are always lists of strings.**
- **Run tests with:** `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`

### Verified LiteLLM API shapes (probed live 2026-08-01, proxy `https://llm.jc.turbo.inc`)

- `POST /key/generate` body `{"key_alias": str, "models": [str], "metadata": {"note": str}}` → 200 dict including `key` (raw secret `sk-...`), `token` (sha256 hash id), `key_name` (masked `sk-...wTDg`), `key_alias`, `models`, `metadata`, `created_at`, `expires`, `spend`.
- `GET /key/list?return_full_object=true&size=N` → `{"keys": [{"token","key_name","key_alias","spend","models","metadata","created_at", ...}]}`. Used to resolve alias→token (info/delete never need the raw secret).
- `POST /key/delete` body `{"keys": ["<token-or-secret>"]}` → `{"deleted_keys": [...]}`.
- Admin auth: `Authorization: Bearer <LITELLM_MASTER_KEY>`. A scoped consumer key gets 403 on these endpoints.
- 1Password sink shape (verified): `op item create --vault Private [--account my.1password.com] --format json` reading a JSON template of `{"title","category":"API_CREDENTIAL","fields":[{"id":"credential","type":"CONCEALED",...}, ...]}` from **stdin** (no `--template` flag — it conflicts with piped stdin).

---

### Task 1: Scaffold, config, and home view

**Files:**
- Create: `tools/litellm_keys.py`
- Test: `tests/test_litellm_keys.py`

**Interfaces:**
- Produces: `VAULT_PATH: Path`; `_load_vault() -> dict`; `_config() -> tuple[str, str]` returning `(base_url, master_key)` and invoking the missing-secret protocol if either is absent; `_home() -> None` printing the no-args view; `main(argv=None) -> None`.

- [ ] **Step 1: Create the tool file with header, docstring, imports, config, and home view**

```python
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
    subparsers = {}

    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        _home()
        return

    args = parse_strict(parser, subparsers, argv)
    if args.command is None:
        _home()
        return


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the home-view test**

```python
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "litellm_keys", Path(__file__).resolve().parent.parent / "tools" / "litellm_keys.py"
)
litellm_keys = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(litellm_keys)


def test_home_view_reports_proxy_and_master_status(capsys, tmp_path, monkeypatch):
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"LITELLM_API_URL": "https://llm.example"}))
    monkeypatch.setattr(litellm_keys, "VAULT_PATH", vault)
    litellm_keys._home()
    out = capsys.readouterr().out
    assert "proxy: https://llm.example" in out
    assert "master_key: MISSING" in out
    assert "bin:" in out
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`
Expected: PASS. Also verify by hand: `uv run tools/litellm_keys.py` prints the home view.

- [ ] **Step 4: Commit**

```bash
git add tools/litellm_keys.py tests/test_litellm_keys.py
git commit -m "feat(litellm_keys): scaffold tool with config loader and home view"
```

---

### Task 2: Sink helpers (the safety core)

**Files:**
- Modify: `tools/litellm_keys.py`
- Test: `tests/test_litellm_keys.py`

**Interfaces:**
- Produces:
  - `_derive_var(alias: str) -> str` → `LITELLM_KEY_<UPPERCASE_SLUG>`
  - `_mask(secret: str) -> str` → `sk-…<last4>`
  - `_env_upsert(text: str, var: str, value: str) -> str` (pure text transform)
  - `_vault_set(name: str, value: str, *, path: Path = VAULT_PATH) -> None`
  - `_op_template(title: str, value: str, note: str, hostname: str) -> dict`
  - `_op_create(template: dict, *, vault: str, account: str | None) -> str` (returns item id)
  - `_clip(value: str) -> str` (returns the clipboard tool used)
  - `_apply_sinks(args, secret, alias, note, hostname) -> list[dict]` returning destination descriptors `{"sink": str, "target": str}` (never containing the secret)

- [ ] **Step 1: Write failing tests for the pure sink helpers**

```python
def test_derive_var_slugifies_and_uppercases():
    assert litellm_keys._derive_var("my key!") == "LITELLM_KEY_MY_KEY"
    assert litellm_keys._derive_var("prod-2026") == "LITELLM_KEY_PROD_2026"


def test_mask_shows_only_last_four():
    assert litellm_keys._mask("sk-abcdEFGHwTDg") == "sk-…wTDg"
    assert "abcdEFGH" not in litellm_keys._mask("sk-abcdEFGHwTDg")


def test_env_upsert_appends_when_absent():
    assert litellm_keys._env_upsert("", "FOO", "bar") == "FOO=bar\n"
    assert litellm_keys._env_upsert("A=1\n", "FOO", "bar") == "A=1\nFOO=bar\n"


def test_env_upsert_replaces_in_place_and_preserves_others():
    text = "A=1\nFOO=old\nB=2\n"
    assert litellm_keys._env_upsert(text, "FOO", "new") == "A=1\nFOO=new\nB=2\n"


def test_env_upsert_matches_exported_var():
    assert litellm_keys._env_upsert("export FOO=old\n", "FOO", "new") == "FOO=new\n"


def test_vault_set_merges_and_persists(tmp_path):
    p = tmp_path / "vault.json"
    p.write_text(json.dumps({"EXISTING": "1"}))
    litellm_keys._vault_set("LITELLM_KEY_X", "sk-secret", path=p)
    data = json.loads(p.read_text())
    assert data == {"EXISTING": "1", "LITELLM_KEY_X": "sk-secret"}


def test_op_template_puts_secret_in_concealed_field():
    tpl = litellm_keys._op_template("T", "sk-secret", "a note", "https://llm.example")
    assert tpl["category"] == "API_CREDENTIAL"
    cred = next(f for f in tpl["fields"] if f["id"] == "credential")
    assert cred["type"] == "CONCEALED"
    assert cred["value"] == "sk-secret"
    assert any(f["id"] == "notesPlain" and f["value"] == "a note" for f in tpl["fields"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`
Expected: FAIL with `AttributeError` (helpers not defined yet).

- [ ] **Step 3: Implement the sink helpers**

Add to `tools/litellm_keys.py` (above `main`):

```python
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


def _vault_set(name: str, value: str, *, path: Path = VAULT_PATH) -> None:
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
```

Note: `args.vault`/`args.op` use argparse `nargs="?"` (Task 4), so `None` = flag absent, `""` = flag present with no value (use derived default), a string = explicit value.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/litellm_keys.py tests/test_litellm_keys.py
git commit -m "feat(litellm_keys): add secret sink helpers (vault, env, 1password, clip)"
```

---

### Task 3: LiteLLM admin client helpers

**Files:**
- Modify: `tools/litellm_keys.py`
- Test: `tests/test_litellm_keys.py`

**Interfaces:**
- Produces:
  - `_api(url, key, path, method="GET", body=None) -> dict` (raises via `fail` on HTTP/URL errors with correct exit codes)
  - `_generate(url, key, alias, models, note) -> dict`
  - `_list_keys(url, key, size=200) -> list[dict]`
  - `_resolve(url, key, ident) -> dict | None`
  - `_delete(url, key, token) -> dict`

- [ ] **Step 1: Write a failing test for alias/hash resolution (pure over an injected list)**

`_resolve` calls `_list_keys`; test by monkeypatching `_list_keys` so no network is touched.

```python
def test_resolve_matches_alias_then_token(monkeypatch):
    records = [
        {"key_alias": "prod", "token": "aaa", "key_name": "sk-...aaaa"},
        {"key_alias": "dev", "token": "bbbccc", "key_name": "sk-...bccc"},
    ]
    monkeypatch.setattr(litellm_keys, "_list_keys", lambda url, key, size=200: records)
    assert litellm_keys._resolve("u", "k", "prod")["token"] == "aaa"
    assert litellm_keys._resolve("u", "k", "bbbccc")["key_alias"] == "dev"
    assert litellm_keys._resolve("u", "k", "missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py::test_resolve_matches_alias_then_token -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement the client helpers**

```python
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


def _list_keys(url: str, key: str, size: int = 200) -> list[dict]:
    resp = _api(url, key, f"/key/list?return_full_object=true&size={size}")
    return resp.get("keys", [])


def _resolve(url: str, key: str, ident: str) -> dict | None:
    for record in _list_keys(url, key):
        if record.get("key_alias") == ident or record.get("token") == ident:
            return record
        if record.get("key_name", "").endswith(ident):
            return record
    return None


def _delete(url: str, key: str, token: str) -> dict:
    return _api(url, key, "/key/delete", "POST", {"keys": [token]})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/litellm_keys.py tests/test_litellm_keys.py
git commit -m "feat(litellm_keys): add LiteLLM admin client helpers"
```

---

### Task 4: `generate` subcommand

**Files:**
- Modify: `tools/litellm_keys.py`
- Test: `tests/test_litellm_keys.py`

**Interfaces:**
- Consumes: `_config`, `_generate`, `_apply_sinks`, `_mask`, `emit`.
- Produces: `cmd_generate(args) -> None`; the `generate` subparser wired into `main`.

- [ ] **Step 1: Register the subparser and implement `cmd_generate`**

In `main`, after `sub = parser.add_subparsers(...)`, add:

```python
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
```

And after `args = parse_strict(...)`:

```python
    if args.command == "generate":
        cmd_generate(args)
        return
```

Implement:

```python
def cmd_generate(args) -> None:
    url, key = _config()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    result = _generate(url, key, args.name, models, args.note)
    secret = result["key"]
    hostname = url
    destinations = _apply_sinks(args, secret, args.name, args.note, hostname)
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
```

- [ ] **Step 2: Write a test that generate never leaks the secret to stdout (network + sinks stubbed)**

```python
def test_generate_masks_secret_and_records_destinations(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("https://llm.example", "sk-master"))
    monkeypatch.setattr(
        litellm_keys, "_generate",
        lambda url, key, alias, models, note: {
            "key": "sk-realSECRETvalue", "token": "tok123", "models": models,
        },
    )
    vault = tmp_path / "vault.json"
    monkeypatch.setattr(litellm_keys, "VAULT_PATH", vault)

    args = argparse.Namespace(
        command="generate", name="prod", models="gpt-4o", note="",
        vault=None, env_file=None, var=None, op=None, clip=False,
        reveal=False, json=False,
    )
    litellm_keys.cmd_generate(args)
    out = capsys.readouterr().out
    assert "sk-realSECRETvalue" not in out
    assert "sk-…alue" in out
    assert json.loads(vault.read_text())["LITELLM_KEY_PROD"] == "sk-realSECRETvalue"
```

Add `import argparse` to the test file's imports if not present.

- [ ] **Step 3: Run tests**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`
Expected: PASS (the default-vault-sink path fires because no explicit sink was set).

- [ ] **Step 4: Commit**

```bash
git add tools/litellm_keys.py tests/test_litellm_keys.py
git commit -m "feat(litellm_keys): add generate subcommand with masked output"
```

---

### Task 5: `list` and `info` subcommands

**Files:**
- Modify: `tools/litellm_keys.py`
- Test: `tests/test_litellm_keys.py`

**Interfaces:**
- Consumes: `_config`, `_list_keys`, `_resolve`, `emit`, `truncate`.
- Produces: `cmd_list(args) -> None`; `cmd_info(args) -> None`; both subparsers wired.

- [ ] **Step 1: Register subparsers in `main`**

```python
    ls = sub.add_parser("list")
    ls.add_argument("--fields", default="")
    ls.add_argument("--json", action="store_true")
    subparsers["list"] = ls

    inf = sub.add_parser("info")
    inf.add_argument("ident")
    inf.add_argument("--json", action="store_true")
    subparsers["info"] = inf
```

And dispatch:

```python
    if args.command == "list":
        cmd_list(args)
        return
    if args.command == "info":
        cmd_info(args)
        return
```

- [ ] **Step 2: Implement `cmd_list` and `cmd_info`**

```python
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
        payload = {"ident": args.ident, "found": False}
        emit(payload, as_json=args.json)
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
```

- [ ] **Step 3: Write tests (network stubbed)**

```python
def test_list_summarizes_and_reports_totals(capsys, monkeypatch):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(
        litellm_keys, "_list_keys",
        lambda url, key, size=200: [
            {"key_alias": "prod", "models": ["gpt-4o"], "spend": 1.5, "created_at": "2026-08-01T10:00:00Z"},
        ],
    )
    litellm_keys.cmd_list(argparse.Namespace(command="list", fields="", json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == "1 of 1 total"
    assert payload["keys"][0]["alias"] == "prod"


def test_info_not_found_is_definitive(capsys, monkeypatch):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(litellm_keys, "_resolve", lambda url, key, ident: None)
    litellm_keys.cmd_info(argparse.Namespace(command="info", ident="ghost", json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ident": "ghost", "found": False}
```

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/litellm_keys.py tests/test_litellm_keys.py
git commit -m "feat(litellm_keys): add list and info subcommands"
```

---

### Task 6: `delete` subcommand + live round-trip verification

**Files:**
- Modify: `tools/litellm_keys.py`
- Test: `tests/test_litellm_keys.py`

**Interfaces:**
- Consumes: `_config`, `_resolve`, `_delete`, `emit`.
- Produces: `cmd_delete(args) -> None`; subparser wired.

- [ ] **Step 1: Register subparser and dispatch in `main`**

```python
    dl = sub.add_parser("delete")
    dl.add_argument("ident")
    dl.add_argument("--json", action="store_true")
    subparsers["delete"] = dl
```

```python
    if args.command == "delete":
        cmd_delete(args)
        return
```

- [ ] **Step 2: Implement `cmd_delete` (idempotent no-op when already gone)**

```python
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
```

- [ ] **Step 3: Write a delete unit test (network stubbed)**

```python
def test_delete_missing_key_is_noop(capsys, monkeypatch):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(litellm_keys, "_resolve", lambda url, key, ident: None)
    litellm_keys.cmd_delete(argparse.Namespace(command="delete", ident="ghost", json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["deleted"] is False


def test_delete_resolves_then_deletes_by_token(capsys, monkeypatch):
    calls = {}
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(litellm_keys, "_resolve", lambda url, key, ident: {"token": "tok9", "key_alias": "prod"})
    monkeypatch.setattr(litellm_keys, "_delete", lambda url, key, token: calls.setdefault("token", token))
    litellm_keys.cmd_delete(argparse.Namespace(command="delete", ident="prod", json=True))
    assert calls["token"] == "tok9"
    assert json.loads(capsys.readouterr().out)["deleted"] is True
```

- [ ] **Step 4: Run the full unit suite**

Run: `uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v`
Expected: all PASS.

- [ ] **Step 5: Live end-to-end round-trip against the real proxy**

This is the real-user verification (per the "reproduce end-to-end" rule). Run each and confirm:

```bash
# generate into a throwaway env file; confirm the secret is NOT in stdout
uv run tools/litellm_keys.py generate --name sherpa_e2e_delete_me \
  --models gpt-4o --note "e2e test" --env-file /tmp/e2e.env --var TEST_KEY
# -> shows alias, masked key (sk-…xxxx), destinations; NO raw sk- value

grep -q '^TEST_KEY=sk-' /tmp/e2e.env && echo "env sink OK"

uv run tools/litellm_keys.py info sherpa_e2e_delete_me   # shows note + models, no secret
uv run tools/litellm_keys.py list | grep sherpa_e2e_delete_me
uv run tools/litellm_keys.py delete sherpa_e2e_delete_me # deleted: true
uv run tools/litellm_keys.py info sherpa_e2e_delete_me   # found: false
rm -f /tmp/e2e.env
```

Confirm: the raw `sk-...` never appears in any command's stdout; the env file received the real key; the round-trip deletes cleanly.

- [ ] **Step 6: Verify tool indexing**

Run: `sherpa list | grep litellm_keys` (or `sherpa search litellm`). Confirm the tool is discoverable and `axi: true` is reflected.

- [ ] **Step 7: Commit**

```bash
git add tools/litellm_keys.py tests/test_litellm_keys.py
git commit -m "feat(litellm_keys): add delete subcommand; verified live round-trip"
```

---

## Self-Review

**Spec coverage:** generate (Task 4), list/info (Task 5), delete (Task 6), sinks vault/env/op/clip + default-vault (Task 2), masked-by-default + `--reveal` (Task 4), in-process secret placement (Task 2), master-key auth + MISSING_SECRET (Task 1/`_config`), AXI contract + TOON + `--json` + exit codes (all tasks), home view (Task 1), Phase 2 analytics explicitly deferred (spec). All covered.

**Type consistency:** `_apply_sinks` reads `args.vault/env_file/var/op/clip` exactly as declared by the Task 4 subparser (`nargs="?"` → `None`/`""`/str). `_resolve` returns a record dict with `token`, consumed by `cmd_delete`. `_generate` returns a dict with `key`/`token`/`models`, consumed by `cmd_generate`. Consistent.

**Placeholder scan:** No TBDs; every code step is concrete.
