#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai", "httpx"]
# ///
"""
name: image_gen
description: Generate images with Gemini (gemini-2.5-flash-image), from a text prompt or guided by a reference image.
categories: [image, genai, creative, gemini]
secrets:
  - GEMINI_API_KEY
usage: |
  generate --prompt 'a cat wearing a top hat' [--output cat.png] [--count 1] [--aspect 1:1]
  generate --prompt 'same scene but at sunset' --image reference.png [--reference-type subject] [--output out.png]
notes: |
  Reference images are passed alongside the prompt, so --reference-type only changes
  how the image is described to the model: `subject` says to keep what is in it,
  `style` says to borrow how it looks.
  --count issues one request per image; the model returns a single image per call.
operations:
  generate:
    tier: write
    argv: ["generate", "--prompt", "{prompt}"]
    optional:
      output: "--output"
      count: "--count"
      aspect: "--aspect"
      image: "--image"
      reference_type: "--reference-type"
    notes: "Bills the Gemini API and writes --output, which defaults to ./output.png and is overwritten."
"""

import argparse
import json
import logging
import mimetypes
import sys
from pathlib import Path

from google import genai
from google.genai import types

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"

MODEL = "gemini-2.5-flash-image"

REFERENCE_INSTRUCTION = {
    "subject": "Use the attached image as the subject.",
    "style": "Use the attached image as a style reference.",
}

# The SDK warns that automatic function calling belongs in Chat.send_message. We
# pass no tools, so there is no function calling to move anywhere.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _reference_part(path: Path, reference_type: str) -> types.Part:
    mime_type, _ = mimetypes.guess_type(path.name)
    if not (mime_type or "").startswith("image/"):
        print(f"Not a recognised image file: {path}", file=sys.stderr)
        sys.exit(1)
    print(f"Using reference image: {path} ({reference_type})", file=sys.stderr)
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)


def _output_path(output: str, index: int, count: int) -> str:
    if count == 1:
        return output
    stem, suffix = Path(output).stem, Path(output).suffix
    return f"{stem}_{index + 1}{suffix}"


def _extract_image(response) -> bytes:
    """The image bytes, or an explanation of why the model returned none."""
    candidate = (response.candidates or [None])[0]
    parts = candidate.content.parts if candidate and candidate.content else []
    for part in parts or []:
        if part.inline_data:
            return part.inline_data.data

    refusal = " ".join(part.text.strip() for part in parts or [] if part.text)
    reason = getattr(candidate, "finish_reason", None)
    detail = refusal or (f"finish_reason={reason}" if reason else "no reason given")
    print(f"No image returned: {detail}", file=sys.stderr)
    sys.exit(2)


def cmd_generate(args: argparse.Namespace) -> None:
    client = genai.Client(api_key=_load_secret("GEMINI_API_KEY"))

    contents = [args.prompt]
    if args.image:
        image_path = Path(args.image)
        if not image_path.exists():
            print(f"Image not found: {args.image}", file=sys.stderr)
            sys.exit(1)
        contents = [
            f"{REFERENCE_INSTRUCTION[args.reference_type]} {args.prompt}",
            _reference_part(image_path, args.reference_type),
        ]

    config = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio=args.aspect),
    )

    print(f"Generating image: {args.prompt!r}", file=sys.stderr)

    outputs = []
    for i in range(args.count):
        response = client.models.generate_content(model=MODEL, contents=contents, config=config)
        filename = _output_path(args.output, i, args.count)
        Path(filename).write_bytes(_extract_image(response))
        print(f"Saved: {filename}", file=sys.stderr)
        outputs.append(filename)

    print(json.dumps({"prompt": args.prompt, "model": MODEL, "files": outputs}))


def main():
    parser = argparse.ArgumentParser(description="Generate images using Gemini.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("generate", help="Generate an image from a text prompt")
    p.add_argument("--prompt", required=True, help="Text prompt describing the image")
    p.add_argument("--output", default="output.png", help="Output filename (default: output.png)")
    p.add_argument("--count", type=int, default=1, choices=[1, 2, 3, 4], help="Number of images (default: 1)")
    p.add_argument("--aspect", default="1:1", choices=["1:1", "3:4", "4:3", "9:16", "16:9"], help="Aspect ratio (default: 1:1)")
    p.add_argument("--image", default=None, help="Reference image path for image-guided generation")
    p.add_argument("--reference-type", default="subject", choices=["subject", "style"], help="How to use the reference image (default: subject)")

    args = parser.parse_args()

    match args.command:
        case "generate":
            cmd_generate(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
