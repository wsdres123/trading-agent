from PIL import Image, ImageDraw, ImageFont
import os

FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
CHAR = "劫"
SIZES = {
    "favicon-16x16.png": 16,
    "favicon-32x32.png": 32,
    "apple-touch-icon.png": 180,
    "android-chrome-192x192.png": 192,
    "android-chrome-512x512.png": 512,
}

def make_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Black circular background
    margin = int(size * 0.06)
    draw.ellipse([margin, margin, size - margin, size - margin], fill=(10, 10, 10, 255))

    # Gold text with slight gradient effect
    try:
        font = ImageFont.truetype(FONT_PATH, int(size * 0.62))
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), CHAR, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) / 2 - bbox[0]
    y = (size - text_h) / 2 - bbox[1]

    # Draw subtle darker outline for embossed effect
    outline = max(1, int(size * 0.015))
    for dx in (-outline, 0, outline):
        for dy in (-outline, 0, outline):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), CHAR, font=font, fill=(120, 90, 30, 120))

    # Gold gradient: top lighter, bottom darker
    for i in range(size):
        ratio = i / size
        r = int(255 - ratio * 40)
        g = int(215 - ratio * 30)
        b = int(100 - ratio * 20)
        strip = Image.new("RGBA", (size, 1), (r, g, b, 255))
        img.paste(strip, (0, i), strip)

    # Mask the gradient to the text shape
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text((x, y), CHAR, font=font, fill=255)
    img.putalpha(mask)

    # Composite onto black circle background
    final = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    final_draw = ImageDraw.Draw(final)
    final_draw.ellipse([margin, margin, size - margin, size - margin], fill=(12, 12, 12, 255))
    final.paste(img, (0, 0), img)
    return final


def make_ico(sizes=[16, 32, 48]):
    images = [make_icon(s) for s in sizes]
    # Convert to RGBA with black background for ICO
    ico_images = []
    for im in images:
        bg = Image.new("RGBA", im.size, (10, 10, 10, 255))
        bg.paste(im, (0, 0), im)
        ico_images.append(bg.convert("RGBA"))
    return ico_images


if __name__ == "__main__":
    out_dir = os.path.dirname(os.path.abspath(__file__))
    for name, sz in SIZES.items():
        icon = make_icon(sz)
        icon.save(os.path.join(out_dir, name))
        print(f"Saved {name}")

    ico_images = make_ico([16, 32, 48])
    ico_path = os.path.join(out_dir, "favicon.ico")
    ico_images[0].save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    print("Saved favicon.ico")
