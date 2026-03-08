#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["openai"]
# ///
"""
name: ask_ai
description: Query AI models via LiteLLM proxy — send prompts and list available models.
categories: [ai, llm, chat]
secrets:
  - LITELLM_API_URL
  - LITELLM_API_KEY
usage: |
  models
  default [MODEL]
  ask [-m MODEL] [-s SYSTEM] [-t TEMP] [--max-tokens N] [--think|--no-think] PROMPT
"""

import argparse
import json
import sys
from pathlib import Path

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


def _load_vault() -> dict:
    if VAULT_PATH.exists():
        return json.loads(VAULT_PATH.read_text())
    return {}


def _save_vault(vault: dict) -> None:
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VAULT_PATH.write_text(json.dumps(vault, indent=2))


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


def cmd_models(args, vault):
    client = _make_client(vault)
    models = client.models.list()
    names = sorted([m.id for m in models.data])
    print(json.dumps({"models": names, "count": len(names)}))


def cmd_default(args, vault):
    if args.model:
        vault["LITELLM_DEFAULT_MODEL"] = args.model
        _save_vault(vault)
        print(json.dumps({"default_model": args.model, "status": "set"}))
    else:
        current = vault.get("LITELLM_DEFAULT_MODEL")
        if current:
            print(json.dumps({"default_model": current}))
        else:
            print(json.dumps({"default_model": None, "hint": "Set with: ask_ai default <MODEL>"}))


def cmd_ask(args, vault):
    client = _make_client(vault)

    model = args.model or vault.get("LITELLM_DEFAULT_MODEL")
    if not model:
        print(json.dumps({"error": "No model specified and no default set", "hint": "Use --model or set a default: ask_ai default <MODEL>"}))
        sys.exit(1)

    # Read prompt from stdin if "-" or if no prompt and stdin is piped
    prompt = args.prompt
    if prompt == "-" or (not prompt and not sys.stdin.isatty()):
        prompt = sys.stdin.read().strip()
    if not prompt:
        print(json.dumps({"error": "No prompt provided"}))
        sys.exit(1)

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }

    # Thinking control via reasoning_effort
    # --no-think -> "none" (disables thinking, saves tokens)
    # --think    -> "high" (enables thinking/reasoning)
    # default: qwen models get "none", others left unset
    think = args.think
    if think is None and "qwen" in model.lower():
        think = False
    if think is not None:
        kwargs["reasoning_effort"] = "high" if think else "none"

    response = client.chat.completions.create(**kwargs)

    choice = response.choices[0]
    usage = response.usage

    result = {
        "model": response.model or model,
        "content": choice.message.content,
        "usage": {
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
        },
    }

    # Include thinking content if present (returned by LiteLLM as reasoning_content)
    reasoning = getattr(choice.message, "reasoning_content", None)
    if reasoning:
        result["thinking"] = reasoning

    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description="Query AI models via LiteLLM proxy.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("models", help="List available models")

    default_parser = sub.add_parser("default", help="Get or set the default model")
    default_parser.add_argument("model", nargs="?", help="Model to set as default")

    ask_parser = sub.add_parser("ask", help="Send a prompt to an AI model")
    ask_parser.add_argument("prompt", nargs="?", default=None, help="The prompt (use '-' for stdin)")
    ask_parser.add_argument("-m", "--model", default=None, help="Model to use (overrides default)")
    ask_parser.add_argument("-s", "--system", default=None, help="System prompt")
    ask_parser.add_argument("-t", "--temperature", type=float, default=0.0, help="Temperature (default: 0)")
    ask_parser.add_argument("--max-tokens", type=int, default=4096, help="Max tokens (default: 4096)")
    think_group = ask_parser.add_mutually_exclusive_group()
    think_group.add_argument("--think", dest="think", action="store_true", default=None, help="Enable thinking/reasoning mode")
    think_group.add_argument("--no-think", dest="think", action="store_false", help="Disable thinking (default for qwen models)")

    args = parser.parse_args()
    vault = _load_vault()

    if args.command == "models":
        cmd_models(args, vault)
    elif args.command == "default":
        cmd_default(args, vault)
    elif args.command == "ask":
        cmd_ask(args, vault)


if __name__ == "__main__":
    main()
