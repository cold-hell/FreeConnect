# -*- coding: utf-8 -*-
"""Генерация icon.ico для FreeConnect: скруглённый квадрат с фирменным
градиентом (бирюза #37e0c4 -> фиолет #6b8cff) и белой молнией (логотип app)."""
from PIL import Image, ImageDraw
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "ui" / "icon.ico"
N = 256                      # мастер-размер
TEAL = (55, 224, 196)       # #37e0c4
PURPLE = (107, 140, 255)    # #6b8cff

# --- фон: диагональный градиент бирюза->фиолет ---
bg = Image.new("RGB", (N, N))
px = bg.load()
for y in range(N):
    for x in range(N):
        t = (x + y) / (2 * (N - 1))          # 0 в углу TL, 1 в углу BR
        px[x, y] = (
            round(TEAL[0] + (PURPLE[0] - TEAL[0]) * t),
            round(TEAL[1] + (PURPLE[1] - TEAL[1]) * t),
            round(TEAL[2] + (PURPLE[2] - TEAL[2]) * t),
        )
img = bg.convert("RGBA")

# --- маска скруглённого квадрата ---
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius=round(N * 0.22), fill=255)
img.putalpha(mask)

d = ImageDraw.Draw(img)

# --- молния (viewBox 24x24), центрируем вокруг (12,12) ---
pts24 = [(7, 2), (7, 13), (10, 13), (10, 22), (17, 10), (13, 10), (17, 2)]
scale = 8.4
cx = cy = 12
def sp(dx, dy):
    return [((x - cx) * scale + N / 2 + dx, (y - cy) * scale + N / 2 + dy) for x, y in pts24]

d.polygon(sp(6, 8), fill=(8, 17, 26, 70))   # мягкая тень
d.polygon(sp(0, 0), fill=(255, 255, 255, 255))  # белая молния

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(OUT, format="ICO", sizes=sizes)
print("icon saved:", OUT, "| sizes:", ",".join(f"{w}" for w, _ in sizes))
