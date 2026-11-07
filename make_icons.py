# make_icons.py
"""
Генерирует app.ico (для .exe) и static/favicon.ico (для браузера)
в ретро-стиле панели: тёмный фон + янтарный broadcast-символ.

Запуск:  python make_icons.py
Требует: pip install pillow
"""
import os
from PIL import Image, ImageDraw

# Палитра из style.css
BG_MID = (46, 35, 24, 255)        # #2e2318
BG_DARK = (31, 22, 16, 255)       # #1f1610
AMBER = (212, 168, 106, 255)      # #d4a86a
AMBER_BRIGHT = (255, 220, 138, 255)  # #ffdc8a

SIZE = 512
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]


def build_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Внешнее кольцо (янтарь)
    draw.ellipse(
        [6, 6, SIZE - 6, SIZE - 6],
        fill=BG_MID,
        outline=AMBER,
        width=22,
    )

    # Внутренняя тонкая окружность — лёгкая «шкала»
    draw.ellipse([70, 70, SIZE - 70, SIZE - 70],
                 outline=(80, 60, 40, 180), width=4)

    # Источник сигнала (точка) — в нижней трети
    cx, cy = SIZE // 2, SIZE - 150
    dot_r = 32
    draw.ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
        fill=AMBER_BRIGHT,
    )

    # Три дуги — «волны» радио (открыты вверх)
    for r, w in [(110, 20), (175, 20), (240, 20)]:
        bbox = [cx - r, cy - r, cx + r, cy + r]
        # 210° → 330° по PIL (0° = 3 часа, по часовой): верхняя половина
        draw.arc(bbox, start=210, end=330, fill=AMBER, width=w)

    return img


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(base_dir, "static")
    os.makedirs(static_dir, exist_ok=True)

    img = build_icon()

    app_ico = os.path.join(base_dir, "app.ico")
    fav_ico = os.path.join(static_dir, "favicon.ico")
    png_256 = os.path.join(static_dir, "favicon-256.png")

    img.save(app_ico, format="ICO", sizes=ICO_SIZES)
    img.save(fav_ico, format="ICO", sizes=ICO_SIZES)
    img.save(png_256, format="PNG")

    print(f"✅ {app_ico}  ({os.path.getsize(app_ico) // 1024} КБ)")
    print(f"✅ {fav_ico}  ({os.path.getsize(fav_ico) // 1024} КБ)")
    print(f"✅ {png_256}  ({os.path.getsize(png_256) // 1024} КБ)")


if __name__ == "__main__":
    main()