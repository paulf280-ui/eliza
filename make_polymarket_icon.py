"""Generate a Polymarket Bot desktop icon — PF Capital prediction market aesthetic."""
import math
from PIL import Image, ImageDraw, ImageFont

def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

def make_frame(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = size

    # Background: rounded square, deep navy
    radius = s // 5
    bg_color = (6, 8, 24, 255)
    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=bg_color)

    # Gradient border glow: purple → blue (Polymarket colours)
    steps = max(120, s * 3)
    cx, cy = s / 2, s / 2
    r_outer = s / 2 - 1
    for i in range(steps):
        angle = 2 * math.pi * i / steps
        t = (math.sin(angle) + 1) / 2
        col = lerp_color((139, 92, 246), (59, 130, 246), t) + (220,)
        x = cx + r_outer * math.cos(angle)
        y = cy + r_outer * math.sin(angle)
        bw = max(1, s // 48)
        draw.ellipse([x - bw, y - bw, x + bw, y + bw], fill=col)

    # Inner glow ring
    glow_r = s // 2 - max(3, s // 18)
    for i in range(steps):
        angle = 2 * math.pi * i / steps
        t = (math.cos(angle * 2) + 1) / 2
        col = lerp_color((139, 92, 246), (59, 130, 246), t) + (60,)
        x = cx + glow_r * math.cos(angle)
        y = cy + glow_r * math.sin(angle)
        bw = max(1, s // 64)
        draw.ellipse([x - bw, y - bw, x + bw, y + bw], fill=col)

    # ── Prediction chart bars (Polymarket logo style) ────────────────────────
    pad   = s * 0.18
    chart_l = pad
    chart_r = s - pad
    chart_b = s * 0.72
    chart_t = s * 0.28

    bar_data = [0.35, 0.55, 0.45, 0.75, 0.60, 0.85]
    n   = len(bar_data)
    gap = (chart_r - chart_l) / n
    bw  = gap * 0.55
    chart_h = chart_b - chart_t

    for i, val in enumerate(bar_data):
        x0 = chart_l + i * gap + (gap - bw) / 2
        x1 = x0 + bw
        h  = chart_h * val
        y0 = chart_b - h
        y1 = chart_b

        # Bar gradient: purple top → blue bottom
        steps_b = max(4, int(h))
        for j in range(steps_b):
            t   = j / max(1, steps_b - 1)
            col = lerp_color((139, 92, 246), (59, 130, 246), t) + (230,)
            yy  = y0 + t * h
            draw.rectangle([x0, yy, x1, yy + max(1, h / steps_b)], fill=col)

        # Bright top cap
        draw.rectangle([x0, y0, x1, y0 + max(2, s // 40)], fill=(200, 180, 255, 255))

    # Baseline
    draw.line([chart_l, chart_b + 1, chart_r, chart_b + 1], fill=(80, 80, 140, 180), width=max(1, s // 64))

    # Rising trend line over the bars
    points = []
    for i, val in enumerate(bar_data):
        x = chart_l + i * gap + gap / 2
        y = chart_b - (chart_h * val)
        points.append((x, y))

    lw = max(1, s // 40)
    for i in range(len(points) - 1):
        t   = i / (len(points) - 2)
        col = lerp_color((200, 180, 255), (100, 200, 255), t) + (255,)
        draw.line([points[i], points[i + 1]], fill=col, width=lw)

    # Dot at last (peak) point
    px, py = points[-1]
    dr = max(2, s // 24)
    draw.ellipse([px - dr, py - dr, px + dr, py + dr], fill=(255, 255, 255, 255))
    draw.ellipse([px - dr + 1, py - dr + 1, px + dr - 1, py + dr - 1], fill=(139, 92, 246, 255))

    # ── "PM" label at bottom ─────────────────────────────────────────────────
    label = "PM"
    font_size = max(8, s // 6)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    bbox  = draw.textbbox((0, 0), label, font=font)
    tw    = bbox[2] - bbox[0]
    th    = bbox[3] - bbox[1]
    tx    = (s - tw) / 2
    ty    = chart_b + (s - chart_b - th) / 2 - max(2, s // 32)

    # Shadow
    draw.text((tx + 1, ty + 1), label, font=font, fill=(0, 0, 0, 160))
    # Gradient text simulation: draw twice
    draw.text((tx, ty), label, font=font, fill=(180, 160, 255, 255))

    return img


def main():
    sizes = [256, 128, 64, 48, 32, 16]
    frames = [make_frame(s) for s in sizes]

    out = "polymarket.ico"
    frames[0].save(
        out,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
