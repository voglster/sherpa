#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
name: youtube
description: Fetch YouTube transcripts, metadata, and chapters via yt-dlp. Writes a timestamped markdown transcript to cache and returns its path.
categories: [youtube, video, transcript, research, read]
usage: |
  transcript <URL_OR_VIDEO_ID> [--refresh] [--interval 30] [--no-auto-update]
  info <URL_OR_VIDEO_ID> [--no-auto-update]
  version
  update
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".sherpa" / "cache" / "youtube"
UPGRADE_MARKER = CACHE_DIR / ".last-upgrade-attempt"
UPGRADE_COOLDOWN_SECONDS = 6 * 60 * 60

CUE_TIMING = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s+-->")
INLINE_TAG = re.compile(r"<[^>]*>")
CUE_INDEX = re.compile(r"^\d+$")

STALENESS_SIGNATURES = (
    "nsig extraction failed",
    "signature extraction failed",
    "unable to extract player",
    "unable to extract yt initial data",
    "failed to extract any player response",
    "please report this issue on",
)


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_vtt(vtt: str) -> list[tuple[float, str]]:
    """Flatten a WebVTT track into (start_seconds, text) with rolling repeats removed.

    Auto-generated tracks repeat the settled line at the top of the next cue and
    emit 10ms filler cues, so only a cue's final line is ever new content.
    """
    cues: list[tuple[float, str]] = []
    start: float | None = None
    lines: list[str] = []
    previous_blank = True

    def flush() -> None:
        if start is None:
            return
        newest = next(
            (INLINE_TAG.sub("", line).strip() for line in reversed(lines) if INLINE_TAG.sub("", line).strip()),
            "",
        )
        if newest and (not cues or cues[-1][1] != newest):
            cues.append((start, newest))

    for raw in vtt.splitlines():
        timing = CUE_TIMING.match(raw.strip())
        if timing:
            flush()
            hours, minutes, secs, millis = (int(g) for g in timing.groups())
            start = hours * 3600 + minutes * 60 + secs + millis / 1000
            lines = []
        elif start is not None and not (previous_blank and CUE_INDEX.match(raw.strip())):
            lines.append(raw)
        previous_blank = not raw.strip()

    flush()
    return cues


def pick_track(
    manual: dict | None, automatic: dict | None
) -> tuple[str, bool] | None:
    """Choose an English track, preferring human captions, then the original ASR."""
    for source, is_auto in ((manual or {}, False), (automatic or {}, True)):
        english = [lang for lang in source if lang == "en" or lang.startswith("en-")]
        if not english:
            continue
        english.sort(key=lambda lang: (lang != "en", lang != "en-orig", lang))
        preferred = "en-orig" if is_auto and "en-orig" in english else english[0]
        return preferred, is_auto
    return None


def render_transcript(cues: list[tuple[float, str]], interval: int = 30) -> str:
    paragraphs: list[str] = []
    bucket: list[str] = []
    bucket_start = 0.0

    for start, text in cues:
        if not bucket:
            bucket_start = start
        elif start - bucket_start >= interval:
            paragraphs.append(f"[{format_timestamp(bucket_start)}] {' '.join(bucket)}")
            bucket = []
            bucket_start = start
        bucket.append(text)

    if bucket:
        paragraphs.append(f"[{format_timestamp(bucket_start)}] {' '.join(bucket)}")
    return "\n\n".join(paragraphs)


def render_document(
    meta: dict, track: tuple[str, bool], cues: list[tuple[float, str]], interval: int = 30
) -> str:
    lang, is_auto = track
    chapters = meta.get("chapters") or []
    upload = meta.get("upload_date") or ""
    formatted_upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}" if len(upload) == 8 else upload

    front = [
        "---",
        f'title: "{meta.get("title", "")}"',
        f'channel: "{meta.get("channel") or meta.get("uploader") or ""}"',
        f"video_id: {meta.get('id', '')}",
        f"url: {meta.get('webpage_url', '')}",
        f"duration: {format_timestamp(meta.get('duration') or 0)}",
        f"upload_date: {formatted_upload}",
        f"captions: {lang} ({'auto-generated' if is_auto else 'manual'})",
        "---",
        "",
        f"# {meta.get('title', '')}",
        "",
    ]

    if chapters:
        front.append("## Chapters")
        front.append("")
        front += [
            f"- [{format_timestamp(chapter.get('start_time') or 0)}] {chapter.get('title', '')}"
            for chapter in chapters
        ]
        front.append("")

    front.append("## Transcript")
    front.append("")
    return "\n".join(front) + render_transcript(cues, interval) + "\n"


def looks_stale(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(signature in lowered for signature in STALENESS_SIGNATURES)


def video_id_of(target: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", target):
        return target
    patterns = (
        r"(?:v=|/shorts/|/embed/|youtu\.be/|/live/)([A-Za-z0-9_-]{11})",
    )
    for pattern in patterns:
        found = re.search(pattern, target)
        if found:
            return found.group(1)
    return re.sub(r"[^A-Za-z0-9_-]", "_", target)[:40]


def url_of(target: str) -> str:
    if target.startswith("http"):
        return target
    return f"https://www.youtube.com/watch?v={video_id_of(target)}"


def require_ytdlp() -> str:
    binary = shutil.which("yt-dlp")
    if not binary:
        print("yt-dlp not found on PATH. Install with: uv tool install yt-dlp", file=sys.stderr)
        sys.exit(1)
    return binary


def ytdlp_version() -> str:
    return subprocess.run(
        [require_ytdlp(), "--version"], capture_output=True, text=True
    ).stdout.strip()


def ytdlp_age_days() -> float | None:
    """Days since the installed yt-dlp's date-based version string."""
    version = ytdlp_version()
    match = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version)
    if not match:
        return None
    released = time.mktime((int(match.group(1)), int(match.group(2)), int(match.group(3)), 0, 0, 0, 0, 0, -1))
    return (time.time() - released) / 86400


def upgrade_ytdlp() -> dict:
    before = ytdlp_version()
    if shutil.which("uv") and "uv/tools" in str(Path(require_ytdlp()).resolve()):
        command = ["uv", "tool", "upgrade", "yt-dlp"]
    else:
        command = [require_ytdlp(), "-U"]

    result = subprocess.run(command, capture_output=True, text=True)
    after = ytdlp_version()
    return {
        "command": " ".join(command),
        "before": before,
        "after": after,
        "upgraded": before != after,
        "ok": result.returncode == 0,
        "output": (result.stdout + result.stderr).strip()[-2000:],
    }


def upgrade_is_on_cooldown() -> bool:
    if not UPGRADE_MARKER.exists():
        return False
    return time.time() - UPGRADE_MARKER.stat().st_mtime < UPGRADE_COOLDOWN_SECONDS


def run_ytdlp(command_args: list[str], allow_auto_update: bool) -> tuple[subprocess.CompletedProcess, bool]:
    """Run yt-dlp, upgrading and retrying once if it fails the way a stale copy fails."""
    result = subprocess.run([require_ytdlp(), *command_args], capture_output=True, text=True)
    if result.returncode == 0 or not allow_auto_update:
        return result, False
    if not looks_stale(result.stderr) or upgrade_is_on_cooldown():
        return result, False

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    UPGRADE_MARKER.touch()
    print(f"yt-dlp looks stale ({ytdlp_version()}); upgrading and retrying once.", file=sys.stderr)
    upgrade_ytdlp()
    return subprocess.run([require_ytdlp(), *command_args], capture_output=True, text=True), True


def fetch_metadata(target: str, allow_auto_update: bool) -> tuple[dict, bool]:
    result, upgraded = run_ytdlp(
        ["--skip-download", "--dump-json", "--no-warnings", url_of(target)], allow_auto_update
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(f"Metadata fetch failed:\n{result.stderr.strip()[-2000:]}", file=sys.stderr)
        sys.exit(2)
    return json.loads(result.stdout.splitlines()[0]), upgraded


def summarize(meta: dict) -> dict:
    return {
        "video_id": meta.get("id"),
        "title": meta.get("title"),
        "channel": meta.get("channel") or meta.get("uploader"),
        "duration": format_timestamp(meta.get("duration") or 0),
        "upload_date": meta.get("upload_date"),
        "url": meta.get("webpage_url"),
        "view_count": meta.get("view_count"),
        "chapters": [
            {"t": format_timestamp(chapter.get("start_time") or 0), "title": chapter.get("title")}
            for chapter in (meta.get("chapters") or [])
        ],
    }


def cmd_info(args: argparse.Namespace) -> None:
    meta, upgraded = fetch_metadata(args.target, not args.no_auto_update)
    payload = summarize(meta)
    payload["description"] = (meta.get("description") or "")[:4000]
    payload["auto_upgraded"] = upgraded
    print(json.dumps(payload, indent=2))


def cmd_transcript(args: argparse.Namespace) -> None:
    video = video_id_of(args.target)
    destination = CACHE_DIR / f"{video}.md"

    if destination.exists() and not args.refresh:
        print(json.dumps({"video_id": video, "path": str(destination), "cached": True,
                          "words": len(destination.read_text().split())}, indent=2))
        return

    meta, upgraded = fetch_metadata(args.target, not args.no_auto_update)
    track = pick_track(meta.get("subtitles"), meta.get("automatic_captions"))
    if not track:
        print(f"No English captions available for {video}.", file=sys.stderr)
        sys.exit(2)

    lang, _ = track
    with tempfile.TemporaryDirectory() as workdir:
        result, retried = run_ytdlp(
            [
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs", lang,
                "--sub-format", "vtt",
                "--no-warnings",
                "-o", str(Path(workdir) / "track.%(ext)s"),
                url_of(args.target),
            ],
            not args.no_auto_update,
        )
        upgraded = upgraded or retried
        tracks = sorted(Path(workdir).glob("*.vtt"))
        if not tracks:
            print(f"Caption download produced no VTT file:\n{result.stderr.strip()[-2000:]}", file=sys.stderr)
            sys.exit(2)
        cues = parse_vtt(tracks[0].read_text(encoding="utf-8", errors="replace"))

    if not cues:
        print(f"Caption track {lang} parsed to zero lines.", file=sys.stderr)
        sys.exit(2)

    document = render_document(meta, track, cues, args.interval)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")

    payload = summarize(meta)
    payload.update(
        captions=f"{lang} ({'auto-generated' if track[1] else 'manual'})",
        words=len(document.split()),
        path=str(destination),
        cached=False,
        auto_upgraded=upgraded,
    )
    print(json.dumps(payload, indent=2))


def cmd_version(_: argparse.Namespace) -> None:
    age = ytdlp_age_days()
    print(json.dumps({
        "yt_dlp": ytdlp_version(),
        "path": str(Path(require_ytdlp()).resolve()),
        "age_days": round(age, 1) if age is not None else None,
        "stale_hint": age is not None and age > 30,
    }, indent=2))


def cmd_update(_: argparse.Namespace) -> None:
    result = upgrade_ytdlp()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch YouTube transcripts, metadata, and chapters.")
    sub = parser.add_subparsers(dest="command")

    transcript = sub.add_parser("transcript", help="Fetch transcript to a cached markdown file")
    transcript.add_argument("target", help="YouTube URL or 11-character video ID")
    transcript.add_argument("--refresh", action="store_true", help="Re-fetch even if cached")
    transcript.add_argument("--interval", type=int, default=30, help="Seconds per timestamped paragraph")
    transcript.add_argument("--no-auto-update", action="store_true", help="Never auto-upgrade yt-dlp on failure")

    info = sub.add_parser("info", help="Fetch metadata and chapters only")
    info.add_argument("target", help="YouTube URL or 11-character video ID")
    info.add_argument("--no-auto-update", action="store_true", help="Never auto-upgrade yt-dlp on failure")

    sub.add_parser("version", help="Report installed yt-dlp version and age")
    sub.add_parser("update", help="Upgrade yt-dlp to the latest release")

    args = parser.parse_args()

    match args.command:
        case "transcript":
            cmd_transcript(args)
        case "info":
            cmd_info(args)
        case "version":
            cmd_version(args)
        case "update":
            cmd_update(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
