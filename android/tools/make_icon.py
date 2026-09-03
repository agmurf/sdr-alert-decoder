"""Generate the ALERT Decoder launcher icon.

One set of numbers produces both the SVG (for visual review) and the Android
VectorDrawable, so the thing that was eyeballed is the thing that ships.

The glyph is a transmitting mast whose legs read as an "A": three concentric
waves over a splayed tower. Everything is auto-fitted into the adaptive-icon
safe zone - a 108dp canvas is mostly cropped, and art that runs to the edge
loses its feet under a circular mask.
"""
import io
import math

BLUE = "#10469E"
STROKE = 5.2
CX = 54.0                      # canvas centre
APEX = 45.0                    # wave origin / top of the mast
RADII = (10.5, 18.5, 26.5)     # wave radii, spaced so the gaps survive 32 px
TIP_ANGLE = -30                # where the wave tips stop, degrees below level
FOOT_Y = 92.0
FOOT_DX = 19.0
BAR_Y = 80.0
RING_OUTER = 5.6
RING_INNER = 2.0
SAFE = 68.0                    # target box, centred: the visible region


def arc(r):
    """Wave arc: left tip, up over the top, down to the right tip."""
    a = math.radians(TIP_ANGLE)
    dx, dy = r * math.cos(a), r * math.sin(a)
    x1, y1 = CX - dx, APEX - dy
    x2, y2 = CX + dx, APEX - dy
    return (f"M{x1:.2f},{y1:.2f} A{r},{r} 0 1 1 {x2:.2f},{y2:.2f}", x1, x2, APEX - r)


strokes, min_x, max_x, min_y = [], CX, CX, APEX
for r in RADII:
    d, x1, x2, top = arc(r)
    strokes.append(d)
    min_x, max_x, min_y = min(min_x, x1), max(max_x, x2), min(min_y, top)

strokes.append(f"M{CX},{APEX + RING_OUTER - 0.6:.2f} L{CX - FOOT_DX},{FOOT_Y}")
strokes.append(f"M{CX},{APEX + RING_OUTER - 0.6:.2f} L{CX + FOOT_DX},{FOOT_Y}")

# Crossbar meets the legs where they actually are at that height.
t = (BAR_Y - (APEX + RING_OUTER - 0.6)) / (FOOT_Y - (APEX + RING_OUTER - 0.6))
bar_dx = FOOT_DX * t
strokes.append(f"M{CX - bar_dx:.2f},{BAR_Y} L{CX + bar_dx:.2f},{BAR_Y}")

ring = (f"M{CX},{APEX} m{-RING_OUTER},0 "
        f"a{RING_OUTER},{RING_OUTER} 0 1 1 {2*RING_OUTER},0 "
        f"a{RING_OUTER},{RING_OUTER} 0 1 1 {-2*RING_OUTER},0 "
        f"M{CX},{APEX} m{-RING_INNER},0 "
        f"a{RING_INNER},{RING_INNER} 0 1 0 {2*RING_INNER},0 "
        f"a{RING_INNER},{RING_INNER} 0 1 0 {-2*RING_INNER},0")

# Fit: bounds include half a stroke on every side.
half = STROKE / 2
bx0, bx1 = min(min_x, CX - FOOT_DX) - half, max(max_x, CX + FOOT_DX) + half
by0, by1 = min_y - half, FOOT_Y + half
scale = min(SAFE / (bx1 - bx0), SAFE / (by1 - by0))
tx = CX - scale * (bx0 + bx1) / 2
ty = CX - scale * (by0 + by1) / 2
print(f"glyph {bx1-bx0:.1f} x {by1-by0:.1f} -> scale {scale:.3f}, "
      f"fits {SAFE} box centred on the canvas")

svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 108 108" '
       f'width="108" height="108">',
       f'<g transform="translate({tx:.3f},{ty:.3f}) scale({scale:.4f})">',
       f'<g fill="none" stroke="{BLUE}" stroke-width="{STROKE}" stroke-linecap="round">']
svg += [f'  <path d="{d}"/>' for d in strokes]
svg += ['</g>', f'<path fill="{BLUE}" fill-rule="evenodd" d="{ring}"/>',
        '</g>', '</svg>']
io.open("icon.svg", "w", encoding="utf-8").write("\n".join(svg))

vec = ['<vector xmlns:android="http://schemas.android.com/apk/res/android"',
       '    android:width="108dp"', '    android:height="108dp"',
       '    android:viewportWidth="108"', '    android:viewportHeight="108">',
       f'    <group android:scaleX="{scale:.4f}" android:scaleY="{scale:.4f}"',
       f'        android:translateX="{tx:.3f}" android:translateY="{ty:.3f}">']
for d in strokes:
    vec += ['        <path',
            f'            android:strokeColor="{BLUE}"',
            f'            android:strokeWidth="{STROKE}"',
            '            android:strokeLineCap="round"',
            '            android:fillColor="#00000000"',
            f'            android:pathData="{d}" />']
vec += ['        <path', f'            android:fillColor="{BLUE}"',
        '            android:fillType="evenOdd"',
        f'            android:pathData="{ring}" />',
        '    </group>', '</vector>']
io.open("ic_launcher_foreground.xml", "w", encoding="utf-8").write("\n".join(vec) + "\n")
print("wrote icon.svg and ic_launcher_foreground.xml")
