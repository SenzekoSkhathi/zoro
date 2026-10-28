"""Generate Zoro's app icon — an arc-reactor orb matching the HUD.

Produces:
  zoro-icon.png            1024x1024 master (source for `tauri icon`)
  web/app/icon.png         512x512   Next.js App Router favicon

Re-run after tweaking to regenerate both:
    .venv/Scripts/python.exe scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
CYAN = (79, 227, 255)
INK = (234, 251, 255)
BG = (4, 8, 13)


def render(size: int) -> Image.Image:
    # Supersample 2x then downscale for crisp anti-aliased edges.
    s = size * 2
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square instrument backdrop.
    radius = int(s * 0.20)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=(*BG, 255))
    # Faint inner border (HUD frame feel).
    inset = int(s * 0.06)
    d.rounded_rectangle(
        [inset, inset, s - 1 - inset, s - 1 - inset],
        radius=int(radius * 0.7),
        outline=(*CYAN, 40),
        width=max(2, s // 360),
    )

    cx = cy = s // 2

    # Concentric rings: outer faint → inner bright.
    for frac, alpha, wfrac in (
        (0.40, 70, 0.012),
        (0.31, 120, 0.012),
        (0.23, 175, 0.014),
    ):
        r = int(s * frac)
        w = max(2, int(s * wfrac))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(*CYAN, alpha), width=w)

    # Tick marks around the outer ring for the targeting-reticle look.
    import math

    tr = int(s * 0.40)
    for deg in range(0, 360, 30):
        a = math.radians(deg)
        x1 = cx + int(math.cos(a) * (tr - s * 0.018))
        y1 = cy + int(math.sin(a) * (tr - s * 0.018))
        x2 = cx + int(math.cos(a) * (tr + s * 0.018))
        y2 = cy + int(math.sin(a) * (tr + s * 0.018))
        d.line([x1, y1, x2, y2], fill=(*CYAN, 90), width=max(2, s // 360))

    # Soft glow behind the core.
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = int(s * 0.16)
    gd.ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=(*CYAN, 255))
    glow = glow.filter(ImageFilter.GaussianBlur(int(s * 0.07)))
    img = Image.alpha_composite(img, glow)

    # Core: bright white-cyan center with a cyan rim.
    d = ImageDraw.Draw(img)
    core = int(s * 0.095)
    d.ellipse([cx - core, cy - core, cx + core, cy + core], fill=(*CYAN, 255))
    inner = int(core * 0.62)
    d.ellipse([cx - inner, cy - inner, cx + inner, cy + inner], fill=(*INK, 255))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    master = render(1024)
    master.save(ROOT / "zoro-icon.png")
    web_icon_dir = ROOT / "web" / "app"
    web_icon_dir.mkdir(parents=True, exist_ok=True)
    render(512).save(web_icon_dir / "icon.png")
    # Multi-resolution .ico for the browser + general Windows use.
    master.save(
        ROOT / "web" / "app" / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("wrote zoro-icon.png (1024), web/app/icon.png (512), web/app/favicon.ico")


if __name__ == "__main__":
    main()
