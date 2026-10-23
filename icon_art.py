"""Draws the app icon (a half-light, half-dark disc) so no image files need to be shipped."""
from PIL import Image, ImageDraw


def make_icon(size: int = 64) -> Image.Image:
    big = size * 4  # draw large, then downsample for smooth edges
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = big * 0.06
    box = [pad, pad, big - pad, big - pad]
    edge = (40, 40, 48, 255)
    draw.ellipse(box, fill=(255, 255, 255, 255))
    draw.pieslice(box, start=-90, end=90, fill=(32, 32, 40, 255))   # right half dark
    draw.ellipse(box, outline=edge, width=max(1, int(big * 0.05)))
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path: str = "icon.ico") -> None:
    make_icon(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
