#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai", "httpx"]
# ///
"""
name: image_gen
description: Generate images using Google Imagen via the Gemini API
categories: [image, genai, creative, imagen]
secrets:
  - GEMINI_API_KEY
usage: |
  generate --prompt 'a cat wearing a top hat' [--output cat.png] [--count 1] [--aspect 1:1]
  generate --prompt 'same scene but at sunset' --image reference.png [--reference-type subject] [--output out.png]
"""

import argparse
import json
import sys
from pathlib import Path

from google import genai
from google.genai import types

VAULT_PATH = Path.home() / ".sherpa" / "vault.json"


def _load_secret(key: str) -> str:
    vault = json.loads(VAULT_PATH.read_text()) if VAULT_PATH.exists() else {}
    value = vault.get(key)
    if not value:
        print(f"MISSING_SECRET: {key}", file=sys.stderr)
        sys.exit(1)
    return value


def cmd_generate(args: argparse.Namespace) -> None:
    api_key = _load_secret("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    reference_images = None
    if args.image:
        image_path = Path(args.image)
        if not image_path.exists():
            print(f"Image not found: {args.image}", file=sys.stderr)
            sys.exit(1)
        raw_ref = types.RawReferenceImage(
            reference_image=types.Image.from_file(str(image_path)),
            reference_id=0,
        )
        if args.reference_type == "style":
            ref_config = types.StyleReferenceConfig(reference_id=0)
        else:
            ref_config = types.SubjectReferenceConfig(reference_id=0)
        reference_images = [raw_ref, ref_config]
        print(f"Using reference image: {args.image} ({args.reference_type})", file=sys.stderr)

    print(f"Generating image: {args.prompt!r}", file=sys.stderr)
    config = types.GenerateImagesConfig(
        number_of_images=args.count,
        aspect_ratio=args.aspect,
    )
    if reference_images:
        config.reference_images = reference_images

    response = client.models.generate_images(
        model="imagen-4.0-generate-001",
        prompt=args.prompt,
        config=config,
    )

    if not response.generated_images:
        print("No images returned by the API", file=sys.stderr)
        sys.exit(2)

    outputs = []
    for i, img in enumerate(response.generated_images):
        if args.count == 1:
            filename = args.output
        else:
            stem = Path(args.output).stem
            suffix = Path(args.output).suffix
            filename = f"{stem}_{i + 1}{suffix}"

        img.image.save(filename)
        print(f"Saved: {filename}", file=sys.stderr)
        outputs.append(filename)

    print(json.dumps({"prompt": args.prompt, "files": outputs}))


def main():
    parser = argparse.ArgumentParser(description="Generate images using Google Imagen.")
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
