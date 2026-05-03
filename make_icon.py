"""Generate a funky TraderBot desktop icon — PF Capital trading terminal aesthetic."""
import math
from PIL import Image, ImageDraw

def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

def make_frame(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = size

    # ── Background: rounded square with deep navy fill ──────────────────────
    radius = s // 5
    bg_color = (8, 11, 22, 255)
    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=bg_color)

    # ── Gradient border glow (emerald → cyan) ───────────────────────────────
    steps = max(120, s * 3)
    cx, cy = s / 2, s / 2
    r_outer = s / 2 - 1
    r_inner = s / 2 - max(2, s // 20)
    for i in range(steps):
        angle = 2 * math.pi * i / steps
        t = (math.sin(angle) + 1) / 2
        col = lerp_color((52, 211, 153), (34, 211, 238), t) + (200,)
        x = cx + r_outer * math.cos(angle)
        y = cy + r_outer * math.sin(angle)
        bw = max(1, s // 48)
        draw.ellipse([x - bw, y - bw, x + bw, y + bw], fill=col)

    # ── "PF" text rendered as bold geometric shapes ──────────────────────────
    # Use pixel-art style block letters so it looks crisp at all sizes
    unit = max(1, s // 32)

    def px(x_frac, y_frac, w_frac, h_frac, color):
        x0 = int(s * x_frac)
        y0 = int(s * y_frac)
        x1 = int(s * (x_frac + w_frac))
        y1 = int(s * (y_frac + h_frac))
        draw.rectangle([x0, y0, x1, y1], fill=color)

    # Gradient: left side emerald, right side cyan
    em = (52, 211, 153, 255)
    cy_col = (34, 211, 238, 255)
    mid = ((52 + 34) // 2, (211 + 211) // 2, (153 + 238) // 2, 255)

    # ── P (left half) ───────────────────────────────────────────────────────
    # Vertical stroke
    px(0.15, 0.20, 0.09, 0.60, em)
    # Top horizontal
    px(0.15, 0.20, 0.22, 0.09, em)
    # Mid horizontal
    px(0.15, 0.42, 0.22, 0.09, mid)
    # Right vertical of bowl (top half only)
    px(0.28, 0.20, 0.09, 0.31, mid)

    # ── F (right half) ──────────────────────────────────────────────────────
    # Vertical stroke
    px(0.58, 0.20, 0.09, 0.60, mid)
    # Top horizontal
    px(0.58, 0.20, 0.27, 0.09, cy_col)
    # Mid horizontal (shorter)
    px(0.58, 0.42, 0.20, 0.09, cy_col)

    # ── Mini candlestick chart (bottom strip) ────────────────────────────────
    candles = [
        (0.12, 0.78, 0.06, 0.10, (239, 68, 68, 200)),    # red
        (0.22, 0.74, 0.06, 0.14, (16, 185, 129, 200)),   # green
        (0.32, 0.76, 0.06, 0.12, (16, 185, 129, 200)),   # green
        (0.42, 0.73, 0.06, 0.15, (239, 68, 68, 200)),    # red
        (0.52, 0.70, 0.06, 0.18, (16, 185, 129, 200)),   # green
        (0.62, 0.68, 0.06, 0.20, (16, 185, 129, 200)),   # green
        (0.72, 0.65, 0.06, 0.23, (52, 211, 153, 220)),   # bright green (ripping)
        (0.82, 0.62, 0.06, 0.26, (34, 211, 238, 220)),   # cyan (mooning)
    ]
    for (xf, yf, wf, hf, col) in candles:
        px(xf, yf, wf, hf, col)

    # ── Inner glow on "PF" letters (soft bloom) ──────────────────────────────
    if s >= 64:
        glow_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        bloom_col = (52, 211, 153, 30)
        gd.rounded_rectangle(
            [int(s * 0.12), int(s * 0.17), int(s * 0.88), int(s * 0.60)],
            radius=s // 10, fill=bloom_col,
        )
        img = Image.alpha_composite(img, glow_layer)

    return img


# Generate a 256x256 master then let PIL downsample for the ICO
master = make_frame(256)

# ICO: save master as PNG at each size, then combine manually via struct
import struct, io

def png_bytes(img, size):
    img2 = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img2.save(buf, format="PNG")
    return buf.getvalue()

sizes = [16, 32, 48, 64, 128, 256]
png_list = [png_bytes(master, s) for s in sizes]

# Build ICO manually (header + directory + image data)
n = len(sizes)
header = struct.pack("<HHH", 0, 1, n)  # reserved, type=1 (ICO), count
dir_size = n * 16
data_offset = 6 + dir_size

directory = b""
image_data = b""
offset = data_offset
for i, (s, png) in enumerate(zip(sizes, png_list)):
    w = h = s if s < 256 else 0  # 256 encoded as 0 in ICO spec
    directory += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset)
    image_data += png
    offset += len(png)

out_path = "/home/paulf/eliza/traderbot.ico"
with open(out_path, "wb") as f:
    f.write(header + directory + image_data)

print(f"Icon saved to {out_path} ({len(header+directory+image_data):,} bytes, {n} sizes)")
