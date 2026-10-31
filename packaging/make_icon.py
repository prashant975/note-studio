"""NoteCraft's .ico for the launcher and the installer, drawn from the same shapes
as note_studio/static/icons/notecraft.svg (Pillow; the SVG stays the source)."""
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _gradient(size, stops, diagonal=True):
    """A linear gradient (top-left to bottom-right) through (offset, rgb) stops."""
    image = Image.new('RGB', (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1)) if diagonal else x / (size - 1)
            for (o1, c1), (o2, c2) in zip(stops, stops[1:]):
                if t <= o2 or (o2, c2) == stops[-1]:
                    f = 0 if o2 == o1 else min(max((t - o1) / (o2 - o1), 0), 1)
                    pixels[x, y] = tuple(round(a + (b - a) * f) for a, b in zip(c1, c2))
                    break
    return image


def _hex(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def draw(size=1024):
    s = size / 64
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    silver = _gradient(size, [(0, _hex('#f4f5f7')), (.52, _hex('#a9aeb7')), (1, _hex('#e6e8ec'))])
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([4 * s, 4 * s, 56 * s, 56 * s], radius=16 * s, fill=255)
    image.paste(silver, (0, 0), mask)
    core = _gradient(size, [(0, _hex('#3a3d44')), (.7, _hex('#111214')), (1, _hex('#111214'))])
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([9 * s, 9 * s, 51 * s, 51 * s], radius=12 * s, fill=255)
    image.paste(core, (0, 0), mask)
    glyph = _gradient(size, [(.1, _hex('#9da3ad')), (.45, _hex('#ffffff')), (.9, _hex('#c9cdd4'))], diagonal=False)
    mask = Image.new('L', (size, size), 0)
    n = [(21, 42), (21, 20), (24.6, 20), (36.4, 35.2), (36.4, 20), (40, 20), (40, 42), (36.4, 42), (24.6, 26.8), (24.6, 42)]
    ImageDraw.Draw(mask).polygon([(x * s, y * s) for x, y in n], fill=255)
    image.paste(glyph, (0, 0), mask)
    # The pen nib, rotated -45 degrees about (53, 53), purple with a white edge.
    nib = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(nib)
    # Path "M46 46h14v8a6 6 0 0 1-6 6h-2a6 6 0 0 1-6-6Z": square top, rounded bottom.
    # The 2.5 stroke is centred on it: white 1.25 outside, purple 1.25 inside.
    for grow, colour in ((1.25, (255, 255, 255)), (-1.25, _hex('#6a3cc8'))):
        box = [(46 - grow) * s, (46 - grow) * s, (60 + grow) * s, (60 + grow) * s]
        pen.rounded_rectangle(box, radius=(6 + grow) * s, fill=colour)
        pen.rectangle([box[0], box[1], box[2], 54 * s], fill=colour)
    nib = nib.rotate(45, center=(53 * s, 53 * s), resample=Image.BICUBIC)
    image = Image.alpha_composite(image, nib)
    return image


def write(target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    draw().resize((256, 256), Image.LANCZOS).save(target, format='ICO', sizes=SIZES)
    return target


if __name__ == '__main__':
    import sys
    print(write(sys.argv[1] if len(sys.argv) > 1 else 'notecraft.ico'))
