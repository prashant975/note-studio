"""Hand-drawn-style doodle icons for the chemistry notes PDF renderer.

Contract used by the main renderer:
    ICONS: list[str]                      -- all available icon names
    draw_icon(pdf, name, x, y, size)      -- draw icon centered at (x, y) in a size x size mm box
    pick_icon(text) -> str | None         -- keyword-match a heading to an icon name

Only fpdf2 vector primitives are used (no text, no images).  Every icon is a
small colourful margin doodle: dark ink outline + 1-2 pastel accent colours,
with a little deliberate wobble/asymmetry for a hand-drawn feel.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# palette
# --------------------------------------------------------------------------- #
OUTLINE = (45, 55, 90)      # dark ink
PINK = (214, 64, 110)
BLUE = (40, 90, 180)
GREEN = (30, 140, 80)
AMBER = (235, 160, 30)
PURPLE = (120, 70, 170)
TEAL = (20, 140, 150)

P_PINK = (248, 190, 209)
P_BLUE = (186, 210, 246)
P_GREEN = (185, 228, 202)
P_AMBER = (252, 222, 156)
P_PURPLE = (219, 199, 241)
P_TEAL = (176, 226, 228)
CREAM = (255, 251, 240)
WHITE = (255, 255, 255)

ICONS: list[str] = [
    "flask", "test_tube", "atom", "bulb", "battery", "bolt", "drop",
    "fire", "magnet", "book", "gear", "balance", "star", "heart_ecg",
    # bigger "sticker" doodles (meant for ~11-16 mm)
    "scientist", "doctor", "microscope", "dna", "rocket", "globe",
]

SKIN = P_AMBER             # pastel skin tone for the character stickers


# --------------------------------------------------------------------------- #
# small helpers (all coordinates are relative to icon centre, in units of size)
# --------------------------------------------------------------------------- #
def _pen(pdf, draw=None, fill=None, lw=None):
    if draw is not None:
        pdf.set_draw_color(*draw)
    if fill is not None:
        pdf.set_fill_color(*fill)
    if lw is not None:
        pdf.set_line_width(lw)


def _ell(pdf, cx, cy, rx, ry, style):
    pdf.ellipse(cx - rx, cy - ry, 2 * rx, 2 * ry, style=style)


def _dot(pdf, cx, cy, r, style="F"):
    _ell(pdf, cx, cy, r, r, style)


def _bez(pdf, p0, p1, p2, p3):
    """Stroke one cubic bezier, tolerant of fpdf2 API differences."""
    if hasattr(pdf, "curve"):
        pdf.curve(p0[0], p0[1], p1[0], p1[1], p2[0], p2[1], p3[0], p3[1])
    else:
        pdf.bezier([p0, p1, p2, p3], closed=False, style="D")


def _poly(pdf, cx, cy, s, pts, style):
    pdf.polygon([(cx + px * s, cy + py * s) for (px, py) in pts], style=style)


def _lines(pdf, cx, cy, s, pts):
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        pdf.line(cx + ax * s, cy + ay * s, cx + bx * s, cy + by * s)


# --------------------------------------------------------------------------- #
# icons -- each draws centred at (cx, cy) inside a s x s box
# --------------------------------------------------------------------------- #
def _flask(pdf, cx, cy, s):
    lw = s * 0.055
    # liquid (teal), drawn first so the ink outline sits on top
    _pen(pdf, draw=TEAL, fill=P_TEAL, lw=lw * 0.6)
    _poly(pdf, cx, cy, s, [(-0.215, 0.12), (-0.325, 0.35), (-0.27, 0.42),
                           (0.28, 0.42), (0.335, 0.35), (0.22, 0.12)], "F")
    pdf.line(cx - 0.215 * s, cy + 0.12 * s, cx + 0.22 * s, cy + 0.12 * s)
    # bubbles
    _pen(pdf, draw=TEAL, lw=lw * 0.55)
    _dot(pdf, cx + 0.03 * s, cy - 0.02 * s, 0.035 * s, "D")
    _dot(pdf, cx - 0.08 * s, cy - 0.22 * s, 0.025 * s, "D")
    # flask outline (Erlenmeyer, slightly lopsided)
    _pen(pdf, draw=OUTLINE, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.09, -0.44), (-0.095, -0.12), (-0.325, 0.35),
                           (-0.27, 0.42), (0.28, 0.42), (0.335, 0.35),
                           (0.095, -0.13), (0.09, -0.44)], "D")
    pdf.line(cx - 0.14 * s, cy - 0.44 * s, cx + 0.13 * s, cy - 0.445 * s)  # rim


def _test_tube(pdf, cx, cy, s):
    lw = s * 0.055
    hw, yb = 0.11, 0.30                       # tube half-width, bottom of straight part
    k = hw * 4 / 3                            # bezier semicircle depth
    # pink liquid
    _pen(pdf, fill=P_PINK)
    pdf.rect(cx - hw * s, cy - 0.02 * s, 2 * hw * s, (yb + 0.02) * s, style="F")
    _dot(pdf, cx, cy + yb * s, hw * s, "F")
    _pen(pdf, draw=PINK, lw=lw * 0.6)
    pdf.line(cx - hw * s, cy - 0.02 * s, cx + hw * s, cy - 0.015 * s)
    _dot(pdf, cx + 0.02 * s, cy + 0.10 * s, 0.028 * s, "D")
    _dot(pdf, cx - 0.04 * s, cy + 0.22 * s, 0.022 * s, "D")
    # glass outline
    _pen(pdf, draw=OUTLINE, lw=lw)
    pdf.line(cx - hw * s, cy - 0.42 * s, cx - hw * s, cy + yb * s)
    pdf.line(cx + hw * s, cy - 0.43 * s, cx + hw * s, cy + yb * s)
    _bez(pdf, (cx - hw * s, cy + yb * s),
         (cx - hw * s, cy + (yb + k) * s),
         (cx + hw * s, cy + (yb + k) * s),
         (cx + hw * s, cy + yb * s))
    _ell(pdf, cx + 0.005 * s, cy - 0.425 * s, 0.135 * s, 0.035 * s, "D")  # rim
    # graduation ticks
    _pen(pdf, draw=OUTLINE, lw=lw * 0.55)
    pdf.line(cx + hw * s, cy - 0.28 * s, cx + (hw - 0.05) * s, cy - 0.28 * s)
    pdf.line(cx + hw * s, cy - 0.16 * s, cx + (hw - 0.05) * s, cy - 0.165 * s)


def _atom(pdf, cx, cy, s):
    lw = s * 0.045
    a, b = 0.90 * s, 0.34 * s                 # orbit ellipse width / height
    x0, y0 = cx - a / 2, cy - b / 2
    for colour, inc in ((BLUE, 8), (PURPLE, 68), (TEAL, 128)):
        _pen(pdf, draw=colour, lw=lw)
        pdf.arc(x0, y0, a, 0, 360, b, inclination=inc)
    # nucleus
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw)
    _dot(pdf, cx, cy, 0.085 * s, "DF")
    # electrons
    _pen(pdf, fill=PINK)
    _dot(pdf, cx + 0.33 * s, cy - 0.22 * s, 0.045 * s, "F")
    _dot(pdf, cx - 0.36 * s, cy + 0.16 * s, 0.045 * s, "F")
    _dot(pdf, cx + 0.05 * s, cy + 0.38 * s, 0.045 * s, "F")


def _bulb(pdf, cx, cy, s):
    lw = s * 0.055
    # rays
    _pen(pdf, draw=AMBER, lw=lw * 0.8)
    for (ax, ay, bx, by) in ((-0.34, -0.36, -0.25, -0.27), (0.0, -0.48, 0.0, -0.38),
                             (0.35, -0.35, 0.26, -0.26), (-0.44, -0.06, -0.33, -0.06),
                             (0.44, -0.09, 0.33, -0.08)):
        pdf.line(cx + ax * s, cy + ay * s, cx + bx * s, cy + by * s)
    # glass
    _pen(pdf, draw=OUTLINE, fill=P_AMBER, lw=lw)
    _dot(pdf, cx, cy - 0.06 * s, 0.24 * s, "DF")
    # filament
    _pen(pdf, draw=OUTLINE, lw=lw * 0.6)
    _lines(pdf, cx, cy, s, [(-0.09, 0.14), (-0.05, 0.03), (0.0, 0.12),
                            (0.05, 0.02), (0.09, 0.13)])
    # base
    _pen(pdf, draw=OUTLINE, fill=P_PURPLE, lw=lw * 0.8)
    pdf.rect(cx - 0.105 * s, cy + 0.20 * s, 0.21 * s, 0.17 * s, style="DF",
             round_corners=True, corner_radius=0.04 * s)
    pdf.line(cx - 0.09 * s, cy + 0.285 * s, cx + 0.09 * s, cy + 0.29 * s)


def _battery(pdf, cx, cy, s):
    lw = s * 0.055
    # terminal nub + body
    _pen(pdf, draw=OUTLINE, fill=OUTLINE, lw=lw)
    pdf.rect(cx + 0.335 * s, cy - 0.075 * s, 0.09 * s, 0.15 * s, style="F",
             round_corners=True, corner_radius=0.02 * s)
    _pen(pdf, draw=OUTLINE, fill=CREAM, lw=lw)
    pdf.rect(cx - 0.42 * s, cy - 0.20 * s, 0.77 * s, 0.41 * s, style="DF",
             round_corners=True, corner_radius=0.05 * s)
    # charge bars: two full (green), one faint (pastel)
    _pen(pdf, fill=GREEN)
    pdf.rect(cx - 0.345 * s, cy - 0.125 * s, 0.175 * s, 0.26 * s, style="F",
             round_corners=True, corner_radius=0.03 * s)
    pdf.rect(cx - 0.125 * s, cy - 0.13 * s, 0.175 * s, 0.26 * s, style="F",
             round_corners=True, corner_radius=0.03 * s)
    _pen(pdf, fill=P_GREEN)
    pdf.rect(cx + 0.095 * s, cy - 0.125 * s, 0.175 * s, 0.26 * s, style="F",
             round_corners=True, corner_radius=0.03 * s)
    # little + spark above the terminal
    _pen(pdf, draw=AMBER, lw=lw * 0.75)
    pdf.line(cx + 0.31 * s, cy - 0.37 * s, cx + 0.41 * s, cy - 0.37 * s)
    pdf.line(cx + 0.36 * s, cy - 0.42 * s, cx + 0.36 * s, cy - 0.32 * s)


def _bolt(pdf, cx, cy, s):
    lw = s * 0.055
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.02, -0.46), (0.21, -0.44), (0.05, -0.10),
                           (0.24, -0.09), (-0.16, 0.46), (-0.05, 0.06),
                           (-0.24, 0.05)], "DF")
    # motion sparks
    _pen(pdf, draw=PINK, lw=lw * 0.7)
    pdf.line(cx - 0.38 * s, cy - 0.30 * s, cx - 0.27 * s, cy - 0.25 * s)
    pdf.line(cx - 0.43 * s, cy - 0.13 * s, cx - 0.31 * s, cy - 0.11 * s)


def _drop(pdf, cx, cy, s):
    lw = s * 0.055
    r, yc = 0.29, 0.14                        # bottom circle radius / centre
    k = r * 4 / 3
    # water fill (circle + shoulder triangle, same colour so they merge)
    _pen(pdf, fill=P_BLUE)
    _dot(pdf, cx, cy + yc * s, r * s, "F")
    _poly(pdf, cx, cy, s, [(0.02, -0.45), (-r + 0.015, yc), (r - 0.015, yc)], "F")
    # shine
    _pen(pdf, fill=WHITE)
    _ell(pdf, cx - 0.115 * s, cy + 0.13 * s, 0.045 * s, 0.085 * s, "F")
    _dot(pdf, cx - 0.09 * s, cy + 0.29 * s, 0.028 * s, "F")
    # outline: two flanks + bottom semicircle
    _pen(pdf, draw=BLUE, lw=lw)
    _bez(pdf, (cx + 0.02 * s, cy - 0.45 * s), (cx - 0.09 * s, cy - 0.24 * s),
         (cx - r * s, cy - 0.05 * s), (cx - r * s, cy + yc * s))
    _bez(pdf, (cx - r * s, cy + yc * s), (cx - r * s, cy + (yc + k) * s),
         (cx + r * s, cy + (yc + k) * s), (cx + r * s, cy + yc * s))
    _bez(pdf, (cx + r * s, cy + yc * s), (cx + r * s, cy - 0.06 * s),
         (cx + 0.12 * s, cy - 0.25 * s), (cx + 0.02 * s, cy - 0.45 * s))


def _fire(pdf, cx, cy, s):
    lw = s * 0.055
    # outer flame (amber) with a little side flick, wobbly on purpose
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw)
    _poly(pdf, cx, cy, s, [(0.07, -0.46), (0.17, -0.26), (0.28, -0.05),
                           (0.30, 0.16), (0.19, 0.36), (0.0, 0.44),
                           (-0.20, 0.37), (-0.30, 0.13), (-0.27, -0.07),
                           (-0.20, -0.30), (-0.10, -0.13), (-0.02, -0.27)],
          "DF")
    # inner flame (pink)
    _pen(pdf, fill=PINK)
    _poly(pdf, cx, cy, s, [(0.02, -0.08), (0.12, 0.07), (0.15, 0.21),
                           (0.03, 0.32), (-0.11, 0.27), (-0.15, 0.10),
                           (-0.04, -0.02)], "F")
    # hot core
    _pen(pdf, fill=CREAM)
    _dot(pdf, cx - 0.005 * s, cy + 0.21 * s, 0.055 * s, "F")


def _magnet(pdf, cx, cy, s):
    import math
    lw = s * 0.055
    R, ri, yc, yl = 0.42, 0.17, 0.02, 0.30    # outer/inner radius, arch centre y, leg end
    pts = []
    for deg in range(180, -1, -30):           # outer arch, left -> right
        a = math.radians(deg)
        pts.append((R * math.cos(a), yc - R * math.sin(a)))
    pts += [(R, yl), (ri, yl)]                # right leg
    for deg in range(0, 181, 30):             # inner arch, right -> left
        a = math.radians(deg)
        pts.append((ri * math.cos(a), yc - ri * math.sin(a)))
    pts += [(-ri, yl), (-R, yl)]              # left leg
    _pen(pdf, draw=OUTLINE, fill=PINK, lw=lw)
    _poly(pdf, cx, cy, s, pts, "DF")
    # pole tips
    _pen(pdf, draw=OUTLINE, fill=P_BLUE, lw=lw * 0.8)
    pdf.rect(cx - R * s, cy + yl * s, (R - ri) * s, 0.15 * s, style="DF")
    pdf.rect(cx + ri * s, cy + yl * s, (R - ri) * s, 0.15 * s, style="DF")
    # attraction sparks by the arch
    _pen(pdf, draw=BLUE, lw=lw * 0.7)
    pdf.line(cx - 0.30 * s, cy - 0.47 * s, cx - 0.25 * s, cy - 0.40 * s)
    pdf.line(cx + 0.02 * s, cy - 0.49 * s, cx + 0.02 * s, cy - 0.42 * s)
    pdf.line(cx + 0.31 * s, cy - 0.46 * s, cx + 0.26 * s, cy - 0.39 * s)


def _book(pdf, cx, cy, s):
    lw = s * 0.05
    # purple cover (open-book silhouette)
    _pen(pdf, draw=OUTLINE, fill=PURPLE, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.46, -0.32), (-0.46, 0.30), (0.0, 0.43),
                           (0.46, 0.28), (0.46, -0.33), (0.0, -0.20)], "DF")
    # pages
    _pen(pdf, draw=OUTLINE, fill=CREAM, lw=lw * 0.8)
    _poly(pdf, cx, cy, s, [(-0.40, -0.28), (-0.40, 0.24), (-0.02, 0.35),
                           (-0.02, -0.165)], "DF")
    _poly(pdf, cx, cy, s, [(0.40, -0.29), (0.40, 0.22), (0.02, 0.35),
                           (0.02, -0.165)], "DF")
    # text scribbles
    _pen(pdf, draw=BLUE, lw=lw * 0.6)
    pdf.line(cx - 0.32 * s, cy - 0.14 * s, cx - 0.10 * s, cy - 0.085 * s)
    pdf.line(cx - 0.32 * s, cy - 0.03 * s, cx - 0.10 * s, cy + 0.025 * s)
    pdf.line(cx - 0.32 * s, cy + 0.08 * s, cx - 0.16 * s, cy + 0.125 * s)
    pdf.line(cx + 0.10 * s, cy - 0.09 * s, cx + 0.32 * s, cy - 0.15 * s)
    pdf.line(cx + 0.10 * s, cy + 0.02 * s, cx + 0.32 * s, cy - 0.04 * s)
    pdf.line(cx + 0.10 * s, cy + 0.13 * s, cx + 0.26 * s, cy + 0.085 * s)


def _gear(pdf, cx, cy, s):
    import math
    lw = s * 0.05
    r_in, r_out = 0.295, 0.44
    pts = []
    for k in range(8):
        base = k * 45 + 7                     # +7deg: slightly askew, hand-drawn
        for (dd, r) in ((-15, r_in), (-10, r_out), (10, r_out), (15, r_in)):
            a = math.radians(base + dd)
            pts.append((r * math.cos(a), -r * math.sin(a)))
    _pen(pdf, draw=OUTLINE, fill=P_TEAL, lw=lw)
    _poly(pdf, cx, cy, s, pts, "DF")
    # hub
    _pen(pdf, draw=OUTLINE, fill=CREAM, lw=lw)
    _dot(pdf, cx + 0.005 * s, cy - 0.005 * s, 0.115 * s, "DF")
    _pen(pdf, fill=TEAL)
    _dot(pdf, cx + 0.005 * s, cy - 0.005 * s, 0.035 * s, "F")


def _balance(pdf, cx, cy, s):
    lw = s * 0.05
    # base + post
    _pen(pdf, draw=OUTLINE, fill=P_PURPLE, lw=lw * 0.8)
    _poly(pdf, cx, cy, s, [(-0.15, 0.46), (0.15, 0.46), (0.075, 0.35),
                           (-0.075, 0.35)], "DF")
    _pen(pdf, draw=OUTLINE, lw=lw)
    pdf.line(cx, cy + 0.35 * s, cx + 0.01 * s, cy - 0.28 * s)
    # beam, gently tilted
    pdf.line(cx - 0.35 * s, cy - 0.315 * s, cx + 0.35 * s, cy - 0.25 * s)
    # strings
    _pen(pdf, draw=OUTLINE, lw=lw * 0.6)
    pdf.line(cx - 0.35 * s, cy - 0.315 * s, cx - 0.46 * s, cy - 0.10 * s)
    pdf.line(cx - 0.35 * s, cy - 0.315 * s, cx - 0.24 * s, cy - 0.10 * s)
    pdf.line(cx + 0.35 * s, cy - 0.25 * s, cx + 0.24 * s, cy - 0.035 * s)
    pdf.line(cx + 0.35 * s, cy - 0.25 * s, cx + 0.46 * s, cy - 0.035 * s)
    # pans
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw * 0.8)
    _poly(pdf, cx, cy, s, [(-0.47, -0.10), (-0.23, -0.10), (-0.35, 0.02)], "DF")
    _poly(pdf, cx, cy, s, [(0.23, -0.035), (0.47, -0.035), (0.35, 0.085)], "DF")
    # pivot
    _pen(pdf, fill=PINK)
    _dot(pdf, cx, cy - 0.283 * s, 0.045 * s, "F")


def _star(pdf, cx, cy, s):
    import math
    lw = s * 0.055
    pts = []
    for k in range(5):
        for (off, r) in ((0, 0.45), (36, 0.19)):
            a = math.radians(90 + 6 + k * 72 + off)
            pts.append((r * math.cos(a), -r * math.sin(a)))
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw)
    _poly(pdf, cx, cy, s, pts, "DF")
    # sparkles in two free corners
    _pen(pdf, draw=PINK, lw=lw * 0.7)
    pdf.line(cx - 0.40 * s, cy - 0.42 * s, cx - 0.30 * s, cy - 0.42 * s)
    pdf.line(cx - 0.35 * s, cy - 0.47 * s, cx - 0.35 * s, cy - 0.37 * s)
    pdf.line(cx + 0.38 * s, cy - 0.36 * s, cx + 0.46 * s, cy - 0.36 * s)
    pdf.line(cx + 0.42 * s, cy - 0.40 * s, cx + 0.42 * s, cy - 0.32 * s)


def _heart_ecg(pdf, cx, cy, s):
    lw = s * 0.055
    # solid pink heart (two lobes + wedge)
    _pen(pdf, fill=PINK)
    _dot(pdf, cx - 0.185 * s, cy - 0.14 * s, 0.20 * s, "F")
    _dot(pdf, cx + 0.185 * s, cy - 0.145 * s, 0.20 * s, "F")
    _poly(pdf, cx, cy, s, [(-0.36, -0.05), (0.36, -0.05), (0.0, 0.42)], "F")
    # ink outline (four beziers, from the top notch around)
    _pen(pdf, draw=OUTLINE, lw=lw)
    _bez(pdf, (cx, cy - 0.10 * s), (cx - 0.04 * s, cy - 0.36 * s),
         (cx - 0.37 * s, cy - 0.37 * s), (cx - 0.38 * s, cy - 0.11 * s))
    _bez(pdf, (cx - 0.38 * s, cy - 0.11 * s), (cx - 0.385 * s, cy + 0.10 * s),
         (cx - 0.17 * s, cy + 0.24 * s), (cx, cy + 0.42 * s))
    _bez(pdf, (cx, cy - 0.10 * s), (cx + 0.04 * s, cy - 0.37 * s),
         (cx + 0.375 * s, cy - 0.375 * s), (cx + 0.385 * s, cy - 0.115 * s))
    _bez(pdf, (cx + 0.385 * s, cy - 0.115 * s), (cx + 0.39 * s, cy + 0.095 * s),
         (cx + 0.17 * s, cy + 0.245 * s), (cx, cy + 0.42 * s))
    # ECG trace across the middle
    _pen(pdf, draw=WHITE, lw=lw * 0.75)
    _lines(pdf, cx, cy, s, [(-0.28, 0.02), (-0.12, 0.02), (-0.05, -0.12),
                            (0.035, 0.16), (0.095, 0.02), (0.27, 0.015)])


# --------------------------------------------------------------------------- #
# sticker doodles -- bigger scene/character pieces (drawn at ~11-16 mm)
# --------------------------------------------------------------------------- #
def _face(pdf, cx, cy, s, hx, hy, mirror=False):
    """Shared chibi face: eyes, blush, smile.  (hx, hy) = head centre offset."""
    lw = s * 0.05
    d = -1.0 if mirror else 1.0
    _pen(pdf, fill=P_PINK)
    _dot(pdf, cx + (hx - d * 0.10) * s, cy + (hy + 0.035) * s, 0.030 * s, "F")
    _dot(pdf, cx + (hx + d * 0.095) * s, cy + (hy + 0.030) * s, 0.030 * s, "F")
    _pen(pdf, fill=OUTLINE)
    _dot(pdf, cx + (hx - 0.055) * s, cy + (hy - 0.015) * s, 0.026 * s, "F")
    _dot(pdf, cx + (hx + 0.055) * s, cy + (hy - 0.020) * s, 0.026 * s, "F")
    _pen(pdf, draw=OUTLINE, lw=lw * 0.6)
    _bez(pdf, (cx + (hx - 0.055) * s, cy + (hy + 0.065) * s),
         (cx + (hx - 0.02) * s, cy + (hy + 0.105) * s),
         (cx + (hx + 0.02) * s, cy + (hy + 0.105) * s),
         (cx + (hx + 0.05) * s, cy + (hy + 0.060) * s))


def _scientist(pdf, cx, cy, s):
    lw = s * 0.05
    # white lab coat + sleeves
    _pen(pdf, draw=OUTLINE, fill=WHITE, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.22, -0.05), (-0.29, 0.45), (0.28, 0.45),
                           (0.21, -0.06)], "DF")
    _poly(pdf, cx, cy, s, [(-0.254, -0.052), (-0.334, 0.248), (-0.247, 0.272),
                           (-0.167, -0.028)], "DF")          # arm hanging down
    _poly(pdf, cx, cy, s, [(0.162, -0.063), (0.287, -0.268), (0.363, -0.222),
                           (0.238, -0.017)], "DF")           # arm raised
    # hands
    _pen(pdf, draw=OUTLINE, fill=SKIN, lw=lw * 0.7)
    _dot(pdf, cx - 0.283 * s, cy + 0.288 * s, 0.050 * s, "DF")
    _dot(pdf, cx + 0.336 * s, cy - 0.256 * s, 0.052 * s, "DF")
    # test tube held up, pink liquid
    _pen(pdf, draw=OUTLINE, fill=WHITE, lw=lw * 0.8)
    pdf.rect(cx + 0.278 * s, cy - 0.470 * s, 0.115 * s, 0.205 * s, style="DF",
             round_corners=True, corner_radius=0.045 * s)
    _pen(pdf, fill=PINK)
    pdf.rect(cx + 0.293 * s, cy - 0.375 * s, 0.086 * s, 0.075 * s, style="F")
    _pen(pdf, fill=WHITE)
    _dot(pdf, cx + 0.322 * s, cy - 0.340 * s, 0.018 * s, "F")
    _pen(pdf, draw=OUTLINE, lw=lw * 0.6)
    pdf.line(cx + 0.278 * s, cy - 0.432 * s, cx + 0.393 * s, cy - 0.438 * s)
    # collar + coat details
    _pen(pdf, draw=OUTLINE, fill=P_BLUE, lw=lw * 0.7)
    _poly(pdf, cx, cy, s, [(-0.16, -0.055), (0.01, 0.105), (0.0, -0.045)], "DF")
    _poly(pdf, cx, cy, s, [(0.155, -0.065), (0.01, 0.105), (0.02, -0.045)], "DF")
    _pen(pdf, draw=OUTLINE, lw=lw * 0.55)
    pdf.line(cx + 0.012 * s, cy + 0.105 * s, cx + 0.025 * s, cy + 0.45 * s)
    pdf.rect(cx - 0.235 * s, cy + 0.185 * s, 0.135 * s, 0.105 * s, style="D")
    _pen(pdf, fill=TEAL)
    _dot(pdf, cx + 0.055 * s, cy + 0.17 * s, 0.024 * s, "F")
    _dot(pdf, cx + 0.062 * s, cy + 0.29 * s, 0.024 * s, "F")
    _pen(pdf, draw=PINK, lw=lw * 0.7)
    pdf.line(cx - 0.20 * s, cy + 0.155 * s, cx - 0.185 * s, cy + 0.245 * s)
    # neck, hair, head
    _pen(pdf, draw=OUTLINE, fill=SKIN, lw=lw * 0.7)
    pdf.rect(cx - 0.075 * s, cy - 0.135 * s, 0.11 * s, 0.10 * s, style="DF")
    _pen(pdf, fill=OUTLINE)
    _dot(pdf, cx - 0.02 * s, cy - 0.285 * s, 0.175 * s, "F")
    _poly(pdf, cx, cy, s, [(0.04, -0.42), (0.16, -0.47), (0.10, -0.36)], "F")
    _pen(pdf, draw=OUTLINE, fill=SKIN, lw=lw * 0.8)
    _dot(pdf, cx - 0.02 * s, cy - 0.235 * s, 0.150 * s, "DF")
    _face(pdf, cx, cy, s, -0.02, -0.235)


def _doctor(pdf, cx, cy, s):
    lw = s * 0.05
    # scrubs + sleeves
    _pen(pdf, draw=OUTLINE, fill=P_BLUE, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.235, -0.04), (-0.30, 0.45), (0.30, 0.45),
                           (0.225, -0.05)], "DF")
    _poly(pdf, cx, cy, s, [(-0.268, -0.042), (-0.356, 0.250), (-0.271, 0.276),
                           (-0.183, -0.016)], "DF")
    _poly(pdf, cx, cy, s, [(0.191, -0.056), (0.286, 0.246), (0.371, 0.220),
                           (0.276, -0.082)], "DF")
    _pen(pdf, draw=OUTLINE, fill=SKIN, lw=lw * 0.7)
    _dot(pdf, cx - 0.312 * s, cy + 0.290 * s, 0.050 * s, "DF")
    _dot(pdf, cx + 0.330 * s, cy + 0.262 * s, 0.050 * s, "DF")
    # V-neck
    _poly(pdf, cx, cy, s, [(-0.10, -0.045), (0.015, 0.115), (0.115, -0.055)],
          "DF")
    # red cross badge, low on the left panel
    _pen(pdf, draw=PINK, lw=lw * 0.85)
    pdf.line(cx - 0.215 * s, cy + 0.265 * s, cx - 0.125 * s, cy + 0.26 * s)
    pdf.line(cx - 0.17 * s, cy + 0.218 * s, cx - 0.17 * s, cy + 0.308 * s)
    # stethoscope: tube round the neck, stem, chest piece
    _pen(pdf, draw=OUTLINE, lw=lw * 1.25)
    _bez(pdf, (cx - 0.17 * s, cy - 0.05 * s), (cx - 0.215 * s, cy + 0.20 * s),
         (cx + 0.22 * s, cy + 0.22 * s), (cx + 0.19 * s, cy - 0.06 * s))
    _bez(pdf, (cx + 0.006 * s, cy + 0.144 * s), (cx + 0.08 * s, cy + 0.21 * s),
         (cx + 0.10 * s, cy + 0.25 * s), (cx + 0.158 * s, cy + 0.30 * s))
    _pen(pdf, fill=OUTLINE)
    _dot(pdf, cx - 0.170 * s, cy - 0.055 * s, 0.032 * s, "F")
    _dot(pdf, cx + 0.190 * s, cy - 0.065 * s, 0.032 * s, "F")
    _pen(pdf, draw=OUTLINE, fill=P_TEAL, lw=lw * 0.8)
    _dot(pdf, cx + 0.175 * s, cy + 0.335 * s, 0.078 * s, "DF")
    _pen(pdf, fill=WHITE)
    _dot(pdf, cx + 0.175 * s, cy + 0.335 * s, 0.038 * s, "F")
    # neck, hair, head
    _pen(pdf, draw=OUTLINE, fill=SKIN, lw=lw * 0.7)
    pdf.rect(cx - 0.055 * s, cy - 0.135 * s, 0.11 * s, 0.105 * s, style="DF")
    _pen(pdf, fill=OUTLINE)
    _dot(pdf, cx + 0.005 * s, cy - 0.285 * s, 0.175 * s, "F")
    _pen(pdf, draw=OUTLINE, fill=SKIN, lw=lw * 0.8)
    _dot(pdf, cx + 0.005 * s, cy - 0.235 * s, 0.150 * s, "DF")
    _pen(pdf, fill=OUTLINE)                       # side-swept fringe
    _poly(pdf, cx, cy, s, [(-0.145, -0.275), (-0.05, -0.365), (0.115, -0.335),
                           (0.145, -0.245), (0.09, -0.30), (-0.03, -0.31),
                           (-0.10, -0.225)], "F")
    _face(pdf, cx, cy, s, 0.005, -0.225, mirror=True)


def _microscope(pdf, cx, cy, s):
    lw = s * 0.05
    # foot
    _pen(pdf, draw=OUTLINE, fill=P_PURPLE, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.36, 0.46), (0.30, 0.46), (0.26, 0.35),
                           (-0.30, 0.35)], "DF")
    # curved arm / limb
    _pen(pdf, draw=OUTLINE, fill=PURPLE, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.02, -0.33), (0.17, -0.29), (0.245, 0.08),
                           (0.17, 0.36), (0.03, 0.36), (0.115, 0.10),
                           (0.05, -0.20), (-0.02, -0.22)], "DF")
    # body tube + objective
    _pen(pdf, draw=OUTLINE, fill=P_BLUE, lw=lw)
    pdf.rect(cx - 0.185 * s, cy - 0.34 * s, 0.15 * s, 0.30 * s, style="DF",
             round_corners=True, corner_radius=0.03 * s)
    _pen(pdf, draw=OUTLINE, fill=BLUE, lw=lw * 0.8)
    _poly(pdf, cx, cy, s, [(-0.175, -0.05), (-0.045, -0.05), (-0.075, 0.03),
                           (-0.145, 0.03)], "DF")
    # eyepiece, leaning back over the tube
    _pen(pdf, draw=OUTLINE, fill=P_AMBER, lw=lw * 0.9)
    _poly(pdf, cx, cy, s, [(-0.175, -0.305), (-0.045, -0.305), (-0.10, -0.455),
                           (-0.225, -0.43)], "DF")
    _pen(pdf, draw=OUTLINE, lw=lw * 0.55)
    pdf.line(cx - 0.215 * s, cy - 0.385 * s, cx - 0.09 * s, cy - 0.405 * s)
    # stage + slide
    _pen(pdf, draw=OUTLINE, fill=P_TEAL, lw=lw * 0.9)
    pdf.rect(cx - 0.34 * s, cy + 0.035 * s, 0.44 * s, 0.09 * s, style="DF")
    _pen(pdf, draw=OUTLINE, fill=WHITE, lw=lw * 0.55)
    pdf.rect(cx - 0.30 * s, cy - 0.025 * s, 0.21 * s, 0.062 * s, style="DF")
    _pen(pdf, fill=PINK)
    _dot(pdf, cx - 0.20 * s, cy + 0.007 * s, 0.022 * s, "F")
    # focus knob
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw * 0.8)
    _dot(pdf, cx + 0.195 * s, cy - 0.005 * s, 0.068 * s, "DF")
    _pen(pdf, fill=OUTLINE)
    _dot(pdf, cx + 0.195 * s, cy - 0.005 * s, 0.020 * s, "F")
    # little "look!" sparkle up by the eyepiece
    _pen(pdf, draw=AMBER, lw=lw * 0.5)
    pdf.line(cx - 0.41 * s, cy - 0.30 * s, cx - 0.345 * s, cy - 0.30 * s)
    pdf.line(cx - 0.378 * s, cy - 0.333 * s, cx - 0.378 * s, cy - 0.268 * s)


def _dna(pdf, cx, cy, s):
    import math
    lw = s * 0.05
    amp, top, half = 0.27, -0.45, 0.45           # amplitude, top y, half period

    def sx(y, phase):
        return amp * math.sin(math.pi * (y - top) / half + phase)

    # rungs first, so the strands sit on top of them
    rung_ys = (-0.315, -0.15, 0.15, 0.315)
    _pen(pdf, draw=PURPLE, lw=lw * 0.85)
    for y in rung_ys:
        x = sx(y, 0.0)
        pdf.line(cx + x * s, cy + y * s, cx - x * s, cy + y * s)
    _pen(pdf, draw=P_PURPLE, lw=lw * 0.85)   # one paler rung: hand-drawn feel
    x = sx(-0.225, 0.0)
    pdf.line(cx + x * s, cy - 0.225 * s, cx - x * s, cy - 0.225 * s)
    # two strands: half-sine arches approximated by cubic beziers
    k = amp * 4.0 / 3.0
    for colour, sign in ((BLUE, 1.0), (PINK, -1.0)):
        _pen(pdf, draw=colour, lw=lw * 1.5)
        for i in range(2):
            y0 = top + i * half
            d = sign * (1.0 if i == 0 else -1.0)
            _bez(pdf, (cx, cy + y0 * s),
                 (cx + d * k * s, cy + (y0 + half / 3) * s),
                 (cx + d * k * s, cy + (y0 + 2 * half / 3) * s),
                 (cx, cy + (y0 + half) * s))
    # end caps
    _pen(pdf, fill=OUTLINE)
    for y in (top, 0.0, -top):
        _dot(pdf, cx, cy + y * s, 0.030 * s, "F")


def _rocket(pdf, cx, cy, s):
    lw = s * 0.05
    # flame (behind the nozzle)
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw * 0.8)
    _poly(pdf, cx, cy, s, [(-0.085, 0.21), (0.09, 0.21), (0.16, 0.29),
                           (0.085, 0.32), (0.06, 0.44), (-0.01, 0.48),
                           (-0.075, 0.42), (-0.095, 0.31), (-0.165, 0.27)],
          "DF")
    _pen(pdf, fill=PINK)
    _poly(pdf, cx, cy, s, [(-0.05, 0.245), (0.05, 0.245), (0.045, 0.33),
                           (-0.005, 0.41), (-0.05, 0.32)], "F")
    # fins
    _pen(pdf, draw=OUTLINE, fill=PINK, lw=lw)
    _poly(pdf, cx, cy, s, [(-0.14, -0.01), (-0.34, 0.17), (-0.14, 0.15)], "DF")
    _poly(pdf, cx, cy, s, [(0.15, -0.02), (0.35, 0.16), (0.15, 0.15)], "DF")
    # nozzle
    _pen(pdf, draw=OUTLINE, fill=P_PURPLE, lw=lw * 0.9)
    _poly(pdf, cx, cy, s, [(-0.11, 0.14), (0.115, 0.14), (0.085, 0.23),
                           (-0.08, 0.23)], "DF")
    # body + nose cone
    _pen(pdf, draw=OUTLINE, fill=CREAM, lw=lw)
    _poly(pdf, cx, cy, s, [(0.0, -0.46), (0.145, -0.19), (0.15, 0.15),
                           (-0.14, 0.15), (-0.15, -0.20)], "DF")
    _pen(pdf, draw=OUTLINE, fill=PINK, lw=lw * 0.9)
    _poly(pdf, cx, cy, s, [(0.0, -0.46), (0.145, -0.19), (-0.15, -0.20)], "DF")
    # porthole
    _pen(pdf, draw=OUTLINE, fill=P_BLUE, lw=lw)
    _dot(pdf, cx + 0.005 * s, cy - 0.045 * s, 0.085 * s, "DF")
    _pen(pdf, fill=WHITE)
    _dot(pdf, cx - 0.025 * s, cy - 0.072 * s, 0.028 * s, "F")
    # rivets
    _pen(pdf, fill=TEAL)
    _dot(pdf, cx - 0.10 * s, cy + 0.075 * s, 0.020 * s, "F")
    _dot(pdf, cx + 0.105 * s, cy + 0.065 * s, 0.020 * s, "F")
    # motion sparkles
    _pen(pdf, draw=TEAL, lw=lw * 0.55)
    for (px, py, r) in ((-0.385, -0.30, 0.036), (0.35, -0.335, 0.031),
                        (-0.42, 0.045, 0.027)):
        pdf.line(cx + (px - r) * s, cy + py * s, cx + (px + r) * s, cy + py * s)
        pdf.line(cx + px * s, cy + (py - r) * s, cx + px * s, cy + (py + r) * s)


def _globe(pdf, cx, cy, s):
    lw = s * 0.05
    gy, R = -0.045, 0.375
    # stand
    _pen(pdf, draw=OUTLINE, fill=AMBER, lw=lw * 0.9)
    _poly(pdf, cx, cy, s, [(-0.17, 0.46), (0.17, 0.46), (0.10, 0.38),
                           (-0.10, 0.38)], "DF")
    _pen(pdf, draw=OUTLINE, lw=lw)
    pdf.line(cx, cy + 0.38 * s, cx + 0.005 * s, cy + 0.30 * s)
    # ocean
    _pen(pdf, fill=P_BLUE)
    _dot(pdf, cx, cy + gy * s, R * s, "F")
    # continents
    _pen(pdf, draw=GREEN, fill=GREEN, lw=lw * 0.5)
    _poly(pdf, cx, cy, s, [(-0.30, -0.14), (-0.20, -0.26), (-0.05, -0.24),
                           (-0.02, -0.10), (-0.10, 0.0), (-0.24, 0.02),
                           (-0.31, -0.05)], "DF")
    _poly(pdf, cx, cy, s, [(0.04, 0.02), (0.18, -0.04), (0.29, 0.06),
                           (0.24, 0.20), (0.10, 0.20), (0.03, 0.12)], "DF")
    _dot(pdf, cx + 0.185 * s, cy - 0.215 * s, 0.045 * s, "F")
    # meridians + equator
    _pen(pdf, draw=BLUE, lw=lw * 0.6)
    pdf.line(cx + 0.005 * s, cy + (gy - R + 0.01) * s,
             cx, cy + (gy + R - 0.01) * s)
    _ell(pdf, cx, cy + gy * s, 0.20 * s, R * s, "D")
    _ell(pdf, cx, cy + gy * s, R * s, 0.125 * s, "D")
    _ell(pdf, cx - 0.005 * s, cy - 0.235 * s, 0.285 * s, 0.065 * s, "D")
    # rim
    _pen(pdf, draw=OUTLINE, lw=lw)
    _dot(pdf, cx, cy + gy * s, R * s, "D")


_DRAWERS = {
    "flask": _flask,
    "test_tube": _test_tube,
    "atom": _atom,
    "bulb": _bulb,
    "battery": _battery,
    "bolt": _bolt,
    "drop": _drop,
    "fire": _fire,
    "magnet": _magnet,
    "book": _book,
    "gear": _gear,
    "balance": _balance,
    "star": _star,
    "heart_ecg": _heart_ecg,
    "scientist": _scientist,
    "doctor": _doctor,
    "microscope": _microscope,
    "dna": _dna,
    "rocket": _rocket,
    "globe": _globe,
}


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def draw_icon(pdf, name: str, x: float, y: float, size: float) -> None:
    """Draw icon ``name`` centred at (x, y), fitting a size x size mm box.

    Unknown names are silently ignored.  Colour / line state is changed
    freely; the caller is expected to reset it afterwards.
    """
    fn = _DRAWERS.get(name)
    if fn is None:
        return
    fn(pdf, x, y, size)


_KEYWORD_MAP: list[tuple[tuple[str, ...], str]] = [
    (("chemical bonding", "molecular structure", "covalent bond", "ionic bond"), "atom"),
    (("electrolysis", "cell", "battery", "electrode"), "battery"),
    (("acid", "base", "solution", "titration"), "flask"),
    (("atom", "orbital", "electron", "quantum"), "atom"),
    (("energy", "enthalpy", "current", "charge"), "bolt"),
    (("light", "photo"), "bulb"),
    (("equilibrium",), "balance"),
    (("thermo", "heat", "combustion"), "fire"),
    (("magnet",), "magnet"),
    (("water", "liquid", "hydro"), "drop"),
    (("organic", "reaction", "mechanism"), "test_tube"),
    (("law", "rule", "theory"), "book"),
    (("kinetics", "rate"), "gear"),
    (("mole", "calculation", "formula"), "star"),
    # sticker doodles -- listed last so the chemistry rules above keep priority
    # ("organic" stays test_tube, "orbital" stays atom, an electrochemical
    #  "cell" stays battery).
    (("biology", "cell", "organ", "blood", "human", "medical"), "doctor"),
    (("microscope", "microscopy", "observation", "slide"), "microscope"),
    (("dna", "gene", "genetics", "chromosome", "heredity"), "dna"),
    (("space", "planet", "orbit", "gravity", "motion", "rocket"), "rocket"),
    (("earth", "geography", "map", "history", "world", "climate"), "globe"),
    (("scientist", "experiment", "lab", "discovery"), "scientist"),
]

_SUBJECT_ICON_POOLS = {
    "chemistry": ("flask", "test_tube", "atom", "battery", "balance"),
    "physics": ("bolt", "bulb", "magnet", "battery", "rocket", "atom"),
    "biology": ("dna", "microscope", "doctor", "heart_ecg", "drop"),
    "mathematics": ("balance", "gear", "star", "book"),
    "other": ("book", "star", "bulb"),
}


def subject_fallback_icon(subject: str) -> str:
    """Return the first neutral or subject-safe header fallback icon."""
    normalized_subject = subject.strip().casefold()
    if normalized_subject == "math":
        normalized_subject = "mathematics"
    pool = _SUBJECT_ICON_POOLS.get(
        normalized_subject, _SUBJECT_ICON_POOLS["other"],
    )
    return pool[0]


def pick_icons(text: str, count: int = 2, subject: str = "") -> list[str]:
    """Return distinct, topical icons using phrase and whole-token matching.

    A subject restricts choices to its safe icon pool; an empty subject keeps
    the original topic-only selection behavior.
    """
    if not text or count <= 0:
        return []
    normalized_subject = subject.strip().casefold()
    if normalized_subject == "math":
        normalized_subject = "mathematics"
    subject_pool = (
        _SUBJECT_ICON_POOLS.get(normalized_subject, _SUBJECT_ICON_POOLS["other"])
        if normalized_subject
        else None
    )
    low = text.casefold()
    tokens = set(re.findall(r"[a-z0-9]+", low))
    choices: list[str] = []
    for keywords, icon in _KEYWORD_MAP:
        matched = any(
            phrase in low if " " in phrase else phrase in tokens
            for phrase in keywords
        )
        if matched and (subject_pool is None or icon in subject_pool) and icon not in choices:
            choices.append(icon)

    # "Chemical Bonding" deserves a chemistry pair, not an accidental
    # substring hit such as the star from "molecular".
    if subject_pool is None and (
        ("chemical" in tokens and "bonding" in tokens) or "molecular structure" in low
    ):
        choices = ["atom"] + [icon for icon in choices if icon != "atom"]
        if not any(icon in {"flask", "test_tube", "battery", "bolt"} for icon in choices):
            choices.append("flask")

    fallbacks = subject_pool or ("flask", "atom", "bulb", "book", "star")
    for icon in fallbacks:
        if len(choices) >= count:
            break
        if icon not in choices:
            choices.append(icon)
    return choices[:count]


def pick_icon(text: str) -> "str | None":
    """Compatibility wrapper for single section-heading doodles."""
    icons = pick_icons(text, count=1)
    return icons[0] if icons else None
