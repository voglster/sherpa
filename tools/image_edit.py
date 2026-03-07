#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["Pillow"]
# ///
"""
name: image_edit
description: Image editing toolkit — crop, resize, beautify screenshots, optimize for web/slides. Browser chrome, shadows, rounded corners, gradient backgrounds, padding, and format conversion.
categories: [image, screenshot, design, web, slides, icons, editing]
usage: |
  crop <input> -o <output> --region X,Y,W,H
  resize <input> -o <output> [--width PX] [--height PX] [--scale 0.5] [--fit cover|contain|stretch]
  beautify <input> -o <output> [--title TEXT] [--background 'linear-gradient(135deg,#667eea,#764ba2)'] [--radius 12] [--shadow 40] [--padding 60] [--crop X,Y,W,H]
  annotate <input> -o <output> --region X,Y,W,H [--type oval|arrow|highlight] [--style sharpie|rigid] [--color '#ff3333'] [--width 4]
  convert <input> -o <output.webp> [--quality 85]
  icon <input> -o <output> --sizes 16,32,64,128 [--padding 10%]
  info <input>
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open(path: str) -> Image.Image:
    try:
        return Image.open(path).convert("RGBA")
    except Exception as e:
        print(f"Failed to open {path}: {e}", file=sys.stderr)
        sys.exit(2)


def _save(img: Image.Image, path: str, quality: int = 95) -> None:
    suffix = Path(path).suffix.lower()
    save_kwargs = {}
    if suffix in (".jpg", ".jpeg"):
        img = img.convert("RGB")
        save_kwargs["quality"] = quality
    elif suffix == ".webp":
        save_kwargs["quality"] = quality
    elif suffix == ".png":
        pass  # default is fine
    img.save(path, **save_kwargs)
    print(f"Saved: {path}", file=sys.stderr)


def _parse_region(s: str) -> tuple[int, int, int, int]:
    """Parse 'X,Y,W,H' into (left, top, right, bottom)."""
    parts = [int(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        print("Region must be X,Y,W,H", file=sys.stderr)
        sys.exit(1)
    x, y, w, h = parts
    return (x, y, x + w, y + h)


def _parse_color(s: str) -> tuple[int, ...]:
    """Parse hex color like #ff0000 or #ff0000aa."""
    s = s.lstrip("#")
    if len(s) == 6:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)
    elif len(s) == 8:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16))
    print(f"Invalid color: #{s}", file=sys.stderr)
    sys.exit(1)


def _parse_gradient(spec: str) -> tuple[float, list[tuple[int, ...]]]:
    """Parse 'linear-gradient(135deg,#667eea,#764ba2)' into angle + color stops."""
    m = re.match(r"linear-gradient\(\s*(\d+)deg\s*,\s*(.+)\s*\)", spec)
    if not m:
        print(f"Invalid gradient: {spec}", file=sys.stderr)
        sys.exit(1)
    angle = float(m.group(1))
    colors = [_parse_color(c.strip()) for c in m.group(2).split(",")]
    return angle, colors


def _make_gradient(width: int, height: int, angle: float, colors: list[tuple[int, ...]]) -> Image.Image:
    """Create a gradient image."""
    img = Image.new("RGBA", (width, height))
    rad = math.radians(angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    # Project corners to find gradient extent
    cx, cy = width / 2, height / 2
    corners = [(0, 0), (width, 0), (width, height), (0, height)]
    projections = [(x - cx) * cos_a + (y - cy) * sin_a for x, y in corners]
    min_p, max_p = min(projections), max(projections)
    span = max_p - min_p if max_p != min_p else 1

    c1, c2 = colors[0], colors[-1]
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            proj = (x - cx) * cos_a + (y - cy) * sin_a
            t = (proj - min_p) / span
            t = max(0.0, min(1.0, t))
            r = int(c1[0] + (c2[0] - c1[0]) * t)
            g = int(c1[1] + (c2[1] - c1[1]) * t)
            b = int(c1[2] + (c2[2] - c1[2]) * t)
            a = int(c1[3] + (c2[3] - c1[3]) * t)
            pixels[x, y] = (r, g, b, a)
    return img


def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    """Apply rounded corners via alpha mask."""
    mask = Image.new("L", img.size, 255)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)
    # Cut corners: draw black in the corners
    corner_mask = Image.new("L", img.size, 0)
    corner_draw = ImageDraw.Draw(corner_mask)
    corner_draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)
    img.putalpha(corner_mask)
    return img


def _add_shadow(img: Image.Image, shadow_size: int, color: tuple = (0, 0, 0, 80)) -> Image.Image:
    """Add a drop shadow behind img."""
    total_w = img.width + shadow_size * 2
    total_h = img.height + shadow_size * 2
    shadow = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))

    # Draw shadow rectangle
    shadow_layer = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow_layer)
    draw.rectangle(
        [shadow_size, shadow_size, shadow_size + img.width, shadow_size + img.height],
        fill=color,
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_size // 2))

    shadow = Image.alpha_composite(shadow, shadow_layer)
    shadow.paste(img, (shadow_size, shadow_size), img)
    return shadow


def _draw_traffic_lights(draw: ImageDraw.Draw, x: int, y: int, dot_r: int = 6, spacing: int = 20):
    """Draw macOS-style red/yellow/green dots."""
    colors = [(255, 95, 86), (255, 189, 46), (39, 201, 63)]
    for i, c in enumerate(colors):
        cx = x + i * spacing
        draw.ellipse([cx - dot_r, y - dot_r, cx + dot_r, y + dot_r], fill=c)


def _add_browser_chrome(img: Image.Image, title: str | None = None) -> Image.Image:
    """Add a macOS-style browser title bar above the image."""
    bar_height = 40
    dot_r = 6
    total = Image.new("RGBA", (img.width, img.height + bar_height), (0, 0, 0, 0))

    # Title bar background
    bar = Image.new("RGBA", (img.width, bar_height), (228, 228, 228, 255))
    bar_draw = ImageDraw.Draw(bar)

    # Traffic lights
    _draw_traffic_lights(bar_draw, x=18 + dot_r, y=bar_height // 2, dot_r=dot_r, spacing=20)

    # Title text
    if title:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
        except OSError:
            font = ImageFont.load_default()
        bbox = bar_draw.textbbox((0, 0), title, font=font)
        tw = bbox[2] - bbox[0]
        bar_draw.text(((img.width - tw) // 2, (bar_height - 14) // 2), title, fill=(100, 100, 100, 255), font=font)

    # Bottom border on title bar
    bar_draw.line([(0, bar_height - 1), (img.width, bar_height - 1)], fill=(200, 200, 200, 255))

    total.paste(bar, (0, 0))
    total.paste(img, (0, bar_height), img if img.mode == "RGBA" else None)
    return total


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_crop(args: argparse.Namespace) -> None:
    img = _open(args.input)
    region = _parse_region(args.region)
    img = img.crop(region)
    _save(img, args.output)
    print(json.dumps({"file": args.output, "size": list(img.size)}))


def cmd_resize(args: argparse.Namespace) -> None:
    img = _open(args.input)
    orig_w, orig_h = img.size

    if args.scale:
        new_w = int(orig_w * args.scale)
        new_h = int(orig_h * args.scale)
    elif args.width and args.height:
        new_w, new_h = args.width, args.height
    elif args.width:
        ratio = args.width / orig_w
        new_w = args.width
        new_h = int(orig_h * ratio)
    elif args.height:
        ratio = args.height / orig_h
        new_w = int(orig_w * ratio)
        new_h = args.height
    else:
        print("Provide --width, --height, or --scale", file=sys.stderr)
        sys.exit(1)

    if args.fit == "cover" and args.width and args.height:
        # Scale to cover, then center-crop
        scale = max(args.width / orig_w, args.height / orig_h)
        img = img.resize((int(orig_w * scale), int(orig_h * scale)), Image.LANCZOS)
        left = (img.width - args.width) // 2
        top = (img.height - args.height) // 2
        img = img.crop((left, top, left + args.width, top + args.height))
    elif args.fit == "contain" and args.width and args.height:
        # Scale to fit within bounds
        scale = min(args.width / orig_w, args.height / orig_h)
        img = img.resize((int(orig_w * scale), int(orig_h * scale)), Image.LANCZOS)
    else:
        img = img.resize((new_w, new_h), Image.LANCZOS)

    _save(img, args.output, quality=args.quality)
    print(json.dumps({"file": args.output, "size": list(img.size)}))


def cmd_beautify(args: argparse.Namespace) -> None:
    img = _open(args.input)

    # Optional crop first
    if args.crop:
        img = img.crop(_parse_region(args.crop))

    # Browser chrome
    if not args.no_chrome:
        img = _add_browser_chrome(img, title=args.title)

    # Rounded corners
    if args.radius > 0:
        img = _round_corners(img, args.radius)

    # Shadow
    if args.shadow > 0:
        img = _add_shadow(img, args.shadow)

    # Background
    pad = args.padding
    canvas_w = img.width + pad * 2
    canvas_h = img.height + pad * 2

    if args.background.startswith("linear-gradient"):
        angle, colors = _parse_gradient(args.background)
        canvas = _make_gradient(canvas_w, canvas_h, angle, colors)
    else:
        bg_color = _parse_color(args.background)
        canvas = Image.new("RGBA", (canvas_w, canvas_h), bg_color)

    canvas.paste(img, (pad, pad), img)
    _save(canvas, args.output)
    print(json.dumps({"file": args.output, "size": list(canvas.size)}))


def cmd_convert(args: argparse.Namespace) -> None:
    img = _open(args.input)
    _save(img, args.output, quality=args.quality)
    # File size
    size_bytes = Path(args.output).stat().st_size
    print(json.dumps({"file": args.output, "size": list(img.size), "bytes": size_bytes}))


def cmd_icon(args: argparse.Namespace) -> None:
    img = _open(args.input)
    sizes = [int(s.strip()) for s in args.sizes.split(",")]

    # Optional padding as percentage
    pad_pct = 0
    if args.padding:
        pad_pct = int(args.padding.rstrip("%")) / 100.0

    outputs = []
    stem = Path(args.output).stem
    suffix = Path(args.output).suffix or ".png"

    for sz in sizes:
        # Resize to fit within the icon area minus padding
        inner = int(sz * (1 - pad_pct * 2))
        resized = img.resize((inner, inner), Image.LANCZOS)

        # Center on transparent canvas
        canvas = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        offset = (sz - inner) // 2
        canvas.paste(resized, (offset, offset), resized)

        filename = f"{stem}_{sz}{suffix}"
        _save(canvas, filename)
        outputs.append(filename)

    print(json.dumps({"files": outputs, "sizes": sizes}))


def _draw_arrow(draw: ImageDraw.Draw, x1: int, y1: int, x2: int, y2: int,
                 color: tuple, width: int, head_size: int = 20) -> None:
    """Draw an arrow from (x1,y1) to (x2,y2) with an arrowhead."""
    draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
    angle = math.atan2(y2 - y1, x2 - x1)
    for side in (-1, 1):
        ha = angle + math.pi + side * 0.4
        hx = x2 + int(head_size * math.cos(ha))
        hy = y2 + int(head_size * math.sin(ha))
        draw.line([(x2, y2), (hx, hy)], fill=color, width=width)


# ---------------------------------------------------------------------------
# Sharpie-style drawing (hand-drawn, anti-aliased via supersampling)
# ---------------------------------------------------------------------------

_SUPERSAMPLE = 3  # draw at 3x then downscale for smooth anti-aliasing


def _wobble_points(points: list[tuple[float, float]], rng, amplitude: float = 3.0,
                   segments: int = 30) -> list[tuple[float, float]]:
    """Interpolate between points and add organic noise displacement."""
    if len(points) < 2:
        return points
    result = []
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        for s in range(segments):
            t = s / segments
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            # Perpendicular noise -- stronger in the middle of the segment
            bulge = math.sin(t * math.pi)
            nx = rng.gauss(0, amplitude * bulge)
            ny = rng.gauss(0, amplitude * bulge)
            result.append((x + nx, y + ny))
    result.append(points[-1])
    return result


def _sharpie_composite(img: Image.Image, draw_fn) -> None:
    """Draw on a supersampled overlay, blur slightly, then composite onto img."""
    ss = _SUPERSAMPLE
    w, h = img.size
    overlay = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    draw_fn(d, ss)
    # Slight blur at high res for ink bleed effect
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=ss * 0.8))
    # Downscale for anti-aliasing
    overlay = overlay.resize((w, h), Image.LANCZOS)
    img.paste(Image.alpha_composite(img, overlay))


def _smooth_noise(n_points: int, rng, amplitude: float = 3.0) -> list[float]:
    """Random walk smoothed by moving average — coherent displacement for strokes."""
    raw = [0.0]
    for _ in range(n_points - 1):
        raw.append(raw[-1] + rng.gauss(0, amplitude * 0.5))
    # Moving average with window of 5
    window = 5
    smoothed = []
    for i in range(len(raw)):
        lo = max(0, i - window // 2)
        hi = min(len(raw), i + window // 2 + 1)
        smoothed.append(sum(raw[lo:hi]) / (hi - lo))
    return smoothed


def _draw_sharpie_oval(img: Image.Image, bbox: tuple, color: tuple, width: int) -> None:
    """Draw a sharpie-style oval around a region with overshoot tails."""
    import random
    rng = random.Random(hash(bbox))

    # Auto-pad so the oval encloses the region
    left, top, right, bottom = bbox
    pad_x = max(12, (right - left) * 0.12)
    pad_y = max(12, (bottom - top) * 0.25)
    cx = (left + right) / 2
    cy = (top + bottom) / 2
    rx = (right - left) / 2 + pad_x
    ry = (bottom - top) / 2 + pad_y

    pass_alphas = [240, 200, 160]

    def draw_fn(d, ss):
        for pass_i in range(3):
            seed = rng.randint(0, 999999)
            pass_rng = random.Random(seed)
            # Slight global offset per pass
            off_x = pass_rng.gauss(0, 1.5) * ss
            off_y = pass_rng.gauss(0, 1.5) * ss

            n_points = 80
            overshoot = pass_rng.uniform(0.3, 0.5)
            total_angle = 2 * math.pi + overshoot

            # Generate smooth radial and tangential noise
            r_noise = _smooth_noise(n_points + 1, pass_rng, amplitude=width * ss * 0.4)
            t_noise = _smooth_noise(n_points + 1, pass_rng, amplitude=width * ss * 0.3)

            points = []
            for i in range(n_points + 1):
                angle = (i / n_points) * total_angle
                base_x = cx * ss + rx * ss * math.cos(angle) + off_x
                base_y = cy * ss + ry * ss * math.sin(angle) + off_y
                # Radial displacement
                nr = r_noise[i]
                base_x += nr * math.cos(angle)
                base_y += nr * math.sin(angle)
                # Tangential displacement
                nt = t_noise[i]
                base_x += nt * (-math.sin(angle))
                base_y += nt * math.cos(angle)
                points.append((base_x, base_y))

            c = (*color[:3], pass_alphas[pass_i])
            w = max(1, width * ss + pass_rng.randint(-1, 1))
            d.line(points, fill=c, width=w, joint="curve")

    _sharpie_composite(img, draw_fn)


def _draw_sharpie_arrow_v2(img: Image.Image, x1: int, y1: int, x2: int, y2: int,
                            color: tuple, width: int,
                            img_w: int = 0, img_h: int = 0) -> None:
    """Draw a sharpie-style arrow with curved shaft and filled arrowhead."""
    import random
    rng = random.Random(hash((x1, y1, x2, y2)))

    head_len = max(20, width * 6)
    head_half_w = max(10, width * 3)

    # Quadratic bezier control point — bow toward image center like a human would draw
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1
    perp_x, perp_y = -dy / length, dx / length

    # Determine which perpendicular direction points toward image center
    img_cx = img_w / 2 if img_w else mx
    img_cy = img_h / 2 if img_h else my
    to_center_dot = (img_cx - mx) * perp_x + (img_cy - my) * perp_y
    sign = 1 if to_center_dot >= 0 else -1

    ctrl_offset = length * rng.uniform(0.12, 0.20) * sign
    ctrl_x = mx + perp_x * ctrl_offset
    ctrl_y = my + perp_y * ctrl_offset

    def draw_fn(d, ss):
        # Generate bezier shaft points
        n_shaft = 40
        shaft_pts = []
        for i in range(n_shaft + 1):
            t = i / n_shaft
            bx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * ctrl_x + t ** 2 * x2
            by = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * ctrl_y + t ** 2 * y2
            shaft_pts.append((bx * ss, by * ss))

        # Multi-pass shaft with wobble
        for pass_i in range(3):
            pass_rng = random.Random(rng.randint(0, 999999))
            alpha = [240, 200, 160][pass_i]
            c = (*color[:3], alpha)
            w = max(1, width * ss + pass_rng.randint(-1, 1))
            wobbled = [(px + pass_rng.gauss(0, 0.8 * ss), py + pass_rng.gauss(0, 0.8 * ss))
                       for px, py in shaft_pts]
            d.line(wobbled, fill=c, width=w, joint="curve")

        # Filled arrowhead triangle
        angle = math.atan2((y2 - ctrl_y), (x2 - ctrl_x))
        tip_x, tip_y = x2 * ss, y2 * ss
        back_x = tip_x - head_len * ss * math.cos(angle)
        back_y = tip_y - head_len * ss * math.sin(angle)
        left_x = back_x + head_half_w * ss * (-math.sin(angle))
        left_y = back_y + head_half_w * ss * math.cos(angle)
        right_x = back_x - head_half_w * ss * (-math.sin(angle))
        right_y = back_y - head_half_w * ss * math.cos(angle)

        for pass_i in range(2):
            alpha = [250, 200][pass_i]
            c = (*color[:3], alpha)
            d.polygon([(tip_x, tip_y), (left_x, left_y), (right_x, right_y)], fill=c)

    _sharpie_composite(img, draw_fn)


def _auto_arrow_origin(img_w: int, img_h: int, left: int, top: int, right: int, bottom: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """Find best side for arrow origin. Returns (origin, target) — target is region edge."""
    import random
    rng = random.Random(hash((left, top, right, bottom)))

    cy = (top + bottom) / 2
    cx = (left + right) / 2
    region_h = bottom - top

    # Prefer left/right — only use top/bottom if horizontal space is tiny
    left_space = left
    right_space = img_w - right
    min_horiz = 60  # minimum pixels to consider a side viable

    if left_space >= min_horiz or right_space >= min_horiz:
        # Use whichever horizontal side has more space
        if left_space >= right_space:
            side = "left"
        else:
            side = "right"
    else:
        # Fall back to top/bottom
        if top >= (img_h - bottom):
            side = "top"
        else:
            side = "bottom"

    frac = rng.uniform(0.60, 0.75)
    perp_frac = rng.uniform(-0.20, 0.20)
    margin = 20

    if side == "left":
        ox = int(left - left_space * frac)
        oy = int(cy + perp_frac * region_h)
        tx, ty = left, int(cy)
    elif side == "right":
        ox = int(right + right_space * frac)
        oy = int(cy + perp_frac * region_h)
        tx, ty = right, int(cy)
    elif side == "top":
        ox = int(cx + perp_frac * (right - left))
        oy = int(top - top * frac)
        tx, ty = int(cx), top
    else:
        ox = int(cx + perp_frac * (right - left))
        oy = int(bottom + (img_h - bottom) * frac)
        tx, ty = int(cx), bottom

    ox = max(margin, min(img_w - margin, ox))
    oy = max(margin, min(img_h - margin, oy))

    return (ox, oy), (tx, ty)


def _draw_highlight_v2(img: Image.Image, bbox: tuple, color: tuple) -> None:
    """Draw a semi-transparent highlight rectangle (like a marker)."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    highlight_color = (*color[:3], 140)
    d.rectangle(bbox, fill=highlight_color)
    img.paste(Image.alpha_composite(img, overlay))


def cmd_annotate(args: argparse.Namespace) -> None:
    img = _open(args.input)
    color = _parse_color(args.color)
    w = args.width
    sharpie = args.style == "sharpie"
    ann_type = args.type

    for region_str in args.region:
        left, top, right, bottom = _parse_region(region_str)

        if ann_type == "oval":
            if sharpie:
                _draw_sharpie_oval(img, (left, top, right, bottom), color, w)
            else:
                draw = ImageDraw.Draw(img)
                draw.ellipse((left, top, right, bottom), outline=color, width=w)

        elif ann_type == "arrow":
            (ox, oy), (tx, ty) = _auto_arrow_origin(img.width, img.height, left, top, right, bottom)
            if sharpie:
                _draw_sharpie_arrow_v2(img, ox, oy, tx, ty, color, w, img.width, img.height)
            else:
                draw = ImageDraw.Draw(img)
                _draw_arrow(draw, ox, oy, tx, ty, color, w)

        elif ann_type == "highlight":
            _draw_highlight_v2(img, (left, top, right, bottom), color)

    _save(img, args.output)
    print(json.dumps({"file": args.output, "size": list(img.size)}))


def cmd_info(args: argparse.Namespace) -> None:
    img = Image.open(args.input)
    size_bytes = Path(args.input).stat().st_size
    print(json.dumps({
        "file": args.input,
        "format": img.format,
        "mode": img.mode,
        "width": img.width,
        "height": img.height,
        "bytes": size_bytes,
    }, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Image editing toolkit for web, slides, and docs.",
    )
    sub = parser.add_subparsers(dest="command")

    # crop
    p = sub.add_parser("crop", help="Crop an image to a region")
    p.add_argument("input", help="Input image path")
    p.add_argument("-o", "--output", required=True, help="Output image path")
    p.add_argument("--region", required=True, help="Crop region: X,Y,W,H")

    # resize
    p = sub.add_parser("resize", help="Resize an image")
    p.add_argument("input", help="Input image path")
    p.add_argument("-o", "--output", required=True, help="Output image path")
    p.add_argument("--width", type=int, default=None, help="Target width in pixels")
    p.add_argument("--height", type=int, default=None, help="Target height in pixels")
    p.add_argument("--scale", type=float, default=None, help="Scale factor (e.g. 0.5 for half)")
    p.add_argument("--fit", default="stretch", choices=["cover", "contain", "stretch"], help="Fit mode when both width and height are given")
    p.add_argument("--quality", type=int, default=95, help="Output quality for JPEG/WebP (default: 95)")

    # beautify
    p = sub.add_parser("beautify", help="Beautify a screenshot with chrome, shadow, and background")
    p.add_argument("input", help="Input image path")
    p.add_argument("-o", "--output", required=True, help="Output image path")
    p.add_argument("--title", default=None, help="Title text in the browser title bar")
    p.add_argument("--background", default="linear-gradient(135deg,#667eea,#764ba2)", help="Background color (#hex) or gradient")
    p.add_argument("--radius", type=int, default=12, help="Corner radius (default: 12)")
    p.add_argument("--shadow", type=int, default=40, help="Shadow size in pixels (default: 40, 0 to disable)")
    p.add_argument("--padding", type=int, default=60, help="Padding around the image (default: 60)")
    p.add_argument("--crop", default=None, help="Pre-crop region: X,Y,W,H")
    p.add_argument("--no-chrome", action="store_true", help="Skip the browser title bar")

    # annotate
    p = sub.add_parser("annotate", help="Region-based annotation — specify what to highlight, tool handles how")
    p.add_argument("input", help="Input image path")
    p.add_argument("-o", "--output", required=True, help="Output image path")
    p.add_argument("--region", action="append", required=True, help="Region to annotate: X,Y,W,H (repeatable)")
    p.add_argument("--type", default="oval", choices=["oval", "arrow", "highlight"], help="Annotation type (default: oval)")
    p.add_argument("--style", default="sharpie", choices=["sharpie", "rigid"], help="Drawing style (default: sharpie)")
    p.add_argument("--color", default="#ff3333", help="Annotation color (default: #ff3333)")
    p.add_argument("--width", type=int, default=4, help="Line width (default: 4)")

    # convert
    p = sub.add_parser("convert", help="Convert image format (png, jpg, webp)")
    p.add_argument("input", help="Input image path")
    p.add_argument("-o", "--output", required=True, help="Output path (format inferred from extension)")
    p.add_argument("--quality", type=int, default=85, help="Quality for JPEG/WebP (default: 85)")

    # icon
    p = sub.add_parser("icon", help="Generate icon sizes from a source image")
    p.add_argument("input", help="Input image path")
    p.add_argument("-o", "--output", required=True, help="Output base path (e.g. icon.png → icon_32.png)")
    p.add_argument("--sizes", required=True, help="Comma-separated sizes (e.g. 16,32,64,128)")
    p.add_argument("--padding", default=None, help="Padding as percentage (e.g. 10%%)")

    # info
    p = sub.add_parser("info", help="Show image metadata")
    p.add_argument("input", help="Input image path")

    args = parser.parse_args()

    match args.command:
        case "crop":
            cmd_crop(args)
        case "resize":
            cmd_resize(args)
        case "beautify":
            cmd_beautify(args)
        case "annotate":
            cmd_annotate(args)
        case "convert":
            cmd_convert(args)
        case "icon":
            cmd_icon(args)
        case "info":
            cmd_info(args)
        case _:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
