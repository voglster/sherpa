#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-toon==0.1.3"]
# ///
"""
name: youtube
description: Fetch YouTube transcripts, metadata, and chapters via yt-dlp. Writes a timestamped markdown transcript to cache and returns its path.
categories: [youtube, video, transcript, research, read]
axi: true
usage: |
  transcript <URL_OR_VIDEO_ID> [--refresh] [--interval 30] [--no-auto-update] [--fields ...] [--json]
  info <URL_OR_VIDEO_ID> [--full] [--fields channel,upload_date,url,view_count] [--no-auto-update] [--json]
  version [--json]
  update [--json]
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.render import bin_line, emit, fail, parse_strict, truncate

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

EXTRA_INFO_FIELDS = ("channel", "upload_date", "url", "view_count")


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
        fail("yt-dlp is not installed", help="install with: uv tool install yt-dlp", usage=True)
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


DEPENDENCY_ERROR_SIGNATURES = (
    ("private video", "private video"),
    ("sign in to confirm your age", "age-restricted, requires sign-in"),
    ("sign in to confirm you're not a bot", "blocked by a bot-check, requires sign-in"),
    ("members-only content", "members-only video"),
    ("video is no longer available", "video unavailable"),
    ("video unavailable", "video unavailable"),
    ("this live stream recording is not available", "live stream recording unavailable"),
    ("copyright", "removed for a copyright claim"),
)


def translate_ytdlp_error(stderr: str, *, fallback: str) -> str:
    """Map a known dependency failure signature to an owned phrase.

    Never echoes the dependency's own formatted text (error-code prefixes,
    extractor names) to the caller — an unrecognized signature falls back to
    a generic, tool-agnostic phrase instead.
    """
    lowered = (stderr or "").lower()
    for signature, phrase in DEPENDENCY_ERROR_SIGNATURES:
        if signature in lowered:
            return phrase
    return fallback


def fetch_metadata(target: str, allow_auto_update: bool) -> tuple[dict, bool]:
    result, upgraded = run_ytdlp(
        ["--skip-download", "--dump-json", "--no-warnings", url_of(target)], allow_auto_update
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(result.stderr.strip()[-2000:], file=sys.stderr)
        video = video_id_of(target)
        reason = translate_ytdlp_error(result.stderr, fallback="metadata fetch failed")
        fail(
            f"could not fetch metadata for {video}: {reason}",
            help="sherpa youtube version to check for a stale fetcher, then sherpa youtube update",
        )
    return json.loads(result.stdout.splitlines()[0]), upgraded


def parse_fields(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {name.strip() for name in raw.split(",") if name.strip()}


def summarize(meta: dict, fields: set[str] | None = None) -> dict:
    payload = {
        "video_id": meta.get("id"),
        "title": meta.get("title"),
        "duration": format_timestamp(meta.get("duration") or 0),
        "chapters": [
            {"t": format_timestamp(chapter.get("start_time") or 0), "title": chapter.get("title")}
            for chapter in (meta.get("chapters") or [])
        ],
    }
    extra = {
        "channel": meta.get("channel") or meta.get("uploader"),
        "upload_date": meta.get("upload_date"),
        "url": meta.get("webpage_url"),
        "view_count": meta.get("view_count"),
    }
    for name in fields or ():
        if name in extra:
            payload[name] = extra[name]
    return payload


def build_info_payload(meta: dict, fields: set[str] | None, full: bool) -> dict:
    payload = summarize(meta, fields)
    description = meta.get("description") or ""
    if full:
        payload["description"] = description
        return payload

    preview, notice = truncate(description, 1000)
    payload["description"] = preview
    if notice:
        payload["description_notice"] = notice
        payload["help"] = [f"sherpa youtube info {meta.get('id')} --full for the complete description"]
    return payload


def require_track(meta: dict, video: str) -> tuple[str, bool]:
    track = pick_track(meta.get("subtitles"), meta.get("automatic_captions"))
    if not track:
        fail(
            f"no English captions available for {video}",
            help=f"sherpa youtube info {video} to check which languages exist",
        )
    return track


def cmd_info(args: argparse.Namespace) -> None:
    meta, upgraded = fetch_metadata(args.target, not args.no_auto_update)
    payload = build_info_payload(meta, parse_fields(args.fields), args.full)
    payload["auto_upgraded"] = upgraded
    emit(payload, as_json=args.json)


def cmd_transcript(args: argparse.Namespace) -> None:
    video = video_id_of(args.target)
    destination = CACHE_DIR / f"{video}.md"

    if destination.exists() and not args.refresh:
        emit(
            {
                "video_id": video,
                "path": str(destination),
                "cached": True,
                "words": len(destination.read_text().split()),
            },
            as_json=args.json,
        )
        return

    meta, upgraded = fetch_metadata(args.target, not args.no_auto_update)
    track = require_track(meta, video)

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
            print(result.stderr.strip()[-2000:], file=sys.stderr)
            reason = translate_ytdlp_error(result.stderr, fallback="caption download failed")
            fail(
                f"caption download for {video} produced no subtitle file: {reason}",
                help=f"sherpa youtube transcript {video} --refresh to retry",
            )
        cues = parse_vtt(tracks[0].read_text(encoding="utf-8", errors="replace"))

    if not cues:
        fail(
            f"caption track {lang} for {video} parsed to zero lines",
            help=f"sherpa youtube transcript {video} --refresh to retry",
        )

    document = render_document(meta, track, cues, args.interval)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")

    payload = summarize(meta, parse_fields(args.fields))
    payload.update(
        captions=f"{lang} ({'auto-generated' if track[1] else 'manual'})",
        words=len(document.split()),
        path=str(destination),
        cached=False,
        auto_upgraded=upgraded,
    )
    emit(payload, as_json=args.json)


def cmd_version(args: argparse.Namespace) -> None:
    age = ytdlp_age_days()
    emit(
        {
            "yt_dlp": ytdlp_version(),
            "path": str(Path(require_ytdlp()).resolve()),
            "age_days": round(age, 1) if age is not None else None,
            "stale_hint": age is not None and age > 30,
        },
        as_json=args.json,
    )


def cmd_update(args: argparse.Namespace) -> None:
    result = upgrade_ytdlp()
    emit(result, as_json=args.json)
    sys.exit(0 if result["ok"] else 1)


def cmd_home() -> None:
    cached = sorted(CACHE_DIR.glob("*.md"), key=lambda path: path.stat().st_mtime) if CACHE_DIR.exists() else []
    lines = [
        bin_line(sys.argv[0]),
        "description: Fetch YouTube transcripts, metadata, and chapters via yt-dlp.",
        f"cached_transcripts: {len(cached)}",
    ]
    if cached:
        lines.append("recent: " + ", ".join(path.stem for path in cached[-3:]))
    lines.append("hint: sherpa youtube info <VIDEO_ID_OR_URL> for metadata and chapters")
    lines.append("hint: sherpa youtube transcript <VIDEO_ID_OR_URL> for the full transcript")
    print("\n".join(lines))
    sys.exit(0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch YouTube transcripts, metadata, and chapters.")
    sub = parser.add_subparsers(dest="command")

    transcript = sub.add_parser("transcript", help="Fetch transcript to a cached markdown file")
    transcript.add_argument("target", help="YouTube URL or 11-character video ID")
    transcript.add_argument("--refresh", action="store_true", help="Re-fetch even if cached")
    transcript.add_argument("--interval", type=int, default=30, help="Seconds per timestamped paragraph")
    transcript.add_argument("--no-auto-update", action="store_true", help="Never auto-upgrade yt-dlp on failure")
    transcript.add_argument("--fields", help=f"Extra metadata fields to include: {', '.join(EXTRA_INFO_FIELDS)}")
    transcript.add_argument("--json", action="store_true", help="Emit the previous JSON shape")

    info = sub.add_parser("info", help="Fetch metadata and chapters only")
    info.add_argument("target", help="YouTube URL or 11-character video ID")
    info.add_argument("--no-auto-update", action="store_true", help="Never auto-upgrade yt-dlp on failure")
    info.add_argument("--full", action="store_true", help="Do not truncate the description")
    info.add_argument("--fields", help=f"Extra metadata fields to include: {', '.join(EXTRA_INFO_FIELDS)}")
    info.add_argument("--json", action="store_true", help="Emit the previous JSON shape")

    version = sub.add_parser("version", help="Report installed yt-dlp version and age")
    version.add_argument("--json", action="store_true", help="Emit the previous JSON shape")

    update = sub.add_parser("update", help="Upgrade yt-dlp to the latest release")
    update.add_argument("--json", action="store_true", help="Emit the previous JSON shape")

    subparsers = {"transcript": transcript, "info": info, "version": version, "update": update}
    args = parse_strict(parser, subparsers, argv)

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
            cmd_home()


if __name__ == "__main__":
    main()
