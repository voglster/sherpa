#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Compare JSON and TOON token counts for captured sherpa tool output.

This measures REAL stdout captured from sherpa tools (not a re-encode of a
payload.json), via the LiteLLM proxy's /utils/token_counter endpoint, under
two different tokenizers, so the result can be checked for tokenizer
artifacts rather than trusting a single engine.

Credentials come from ~/.sherpa/vault.json: LITELLM_API_URL, LITELLM_API_KEY.

Run:
  uv run --script scripts/measure_toon.py \\
      --pair youtube-detail 19 /tmp/axi-payloads/youtube-detail.json /tmp/axi-payloads/youtube-detail.toon \\
      --pair jira-typical 20 /tmp/axi-payloads/jira-typical.json /tmp/axi-payloads/jira-typical.toon

Each --pair is NAME ROWS JSON_FILE TOON_FILE. ROWS is recorded verbatim in
the report for audit — pass whatever row count you observed for that
payload (e.g. "1 detail + 18 chapters", "20", "100").

To re-run against a real Anthropic tokenizer instead of the proxy: swap
count_tokens() for anthropic.Anthropic().messages.count_tokens() and drop
the TOKENIZER_MODELS/vault plumbing. Everything else — the --pair capture
format, the report table — stays the same.
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"
TOKENIZER_MODELS = ["ollama/glm-4.7-flash", "claude-sonnet-4-5"]


def load_vault() -> dict:
    if VAULT_PATH.exists():
        return json.loads(VAULT_PATH.read_text())
    return {}


def require_secrets(vault: dict) -> tuple[str, str]:
    url = vault.get("LITELLM_API_URL")
    key = vault.get("LITELLM_API_KEY")
    missing = [name for name, value in (("LITELLM_API_URL", url), ("LITELLM_API_KEY", key)) if not value]
    if missing:
        for name in missing:
            print(f"MISSING_SECRET: {name}", file=sys.stderr)
        sys.exit(1)
    return url.rstrip("/"), key


def count_tokens(client: httpx.Client, url: str, key: str, model: str, text: str) -> tuple[int, str]:
    response = client.post(
        f"{url}/utils/token_counter",
        json={"model": model, "messages": [{"role": "user", "content": text}]},
        headers={"Authorization": f"Bearer {key}"},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["total_tokens"], data["tokenizer_type"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pair",
        nargs=4,
        action="append",
        metavar=("NAME", "ROWS", "JSON_FILE", "TOON_FILE"),
        required=True,
        help="A captured payload pair to compare: NAME ROWS JSON_FILE TOON_FILE",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    vault = load_vault()
    url, key = require_secrets(vault)

    measurements = []
    with httpx.Client() as client:
        for name, rows, json_path, toon_path in args.pair:
            as_json = Path(json_path).read_text()
            as_toon = Path(toon_path).read_text()

            per_model = {}
            for model in TOKENIZER_MODELS:
                json_tokens, tokenizer_type = count_tokens(client, url, key, model, as_json)
                toon_tokens, tokenizer_type_toon = count_tokens(client, url, key, model, as_toon)
                assert tokenizer_type == tokenizer_type_toon
                saved = (json_tokens - toon_tokens) / json_tokens
                per_model[model] = {
                    "tokenizer_type": tokenizer_type,
                    "json_tokens": json_tokens,
                    "toon_tokens": toon_tokens,
                    "saved": f"{saved:.1%}",
                }

            measurements.append({"payload": name, "rows": rows, "models": per_model})

    print(json.dumps(measurements, indent=2))


if __name__ == "__main__":
    main()
