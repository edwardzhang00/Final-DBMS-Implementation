"""Minimal SVG line charts for the reports (no plotting library needed).

One y axis, thin 2px lines, 8px markers with a 2px surface ring and a native
<title> tooltip, a legend plus direct labels at the line ends, and light and
dark palettes selected by prefers-color-scheme.
"""
import math

LIGHT = {"surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e3df",
         "series": ["#2a78d6", "#eb6834", "#1baf7a"]}
DARK = {"surface": "#1a1a19", "text": "#f0efec", "muted": "#c3c2b7", "grid": "#3a3936",
        "series": ["#3987e5", "#d95926", "#199e70"]}
SHAPES = ["circle", "square", "diamond"]


def palette_rules(palette):
    rules = ".bg{fill:%s}.t{fill:%s}.m{fill:%s}.g{stroke:%s}" % (palette["surface"], palette["text"],
                                                                 palette["muted"], palette["grid"])
    return rules + "".join(".s%d{stroke:%s}.f%d{fill:%s}.r%d{stroke:%s}" % (i, color, i, color, i, palette["surface"])
                           for i, color in enumerate(palette["series"]))


def style():
    return ("<style>text{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}"
            + palette_rules(LIGHT) + "@media (prefers-color-scheme: dark){" + palette_rules(DARK) + "}</style>")


def ticks(lo, hi, log):
    if log:
        out, power = [], 10 ** math.floor(math.log10(lo))
        while power <= hi * 1.0001:
            for multiple in (1, 2, 5):
                if lo <= power * multiple <= hi * 1.0001:
                    out.append(power * multiple)
            power *= 10
        return out
    step = 10 ** math.floor(math.log10((hi - lo) or 1))
    for multiple in (1, 2, 5, 10):
        if (hi - lo) / (step * multiple) <= 6:
            step *= multiple
            break
    start = math.ceil(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step) + 1)]


def marker(shape, x, y, cls, title):
    tip = f"<title>{title}</title>"
    if shape == "circle":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="f{cls} r{cls}" stroke-width="2">{tip}</circle>'
    if shape == "square":
        return (f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" rx="1" class="f{cls} r{cls}" '
                f'stroke-width="2">{tip}</rect>')
    points = f"{x:.1f},{y - 5:.1f} {x + 5:.1f},{y:.1f} {x:.1f},{y + 5:.1f} {x - 5:.1f},{y:.1f}"
    return f'<polygon points="{points}" class="f{cls} r{cls}" stroke-width="2">{tip}</polygon>'


def line_chart(path, title, x_label, y_label, series, log_x=False, log_y=False, width=720, height=420):
    """series: [(name, [(x, y), ...])]; at most three series."""
    left, right, top, bottom = 78, 120, 56, 56
    xs = [x for _, points in series for x, _ in points]
    ys = [y for _, points in series for _, y in points]
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = (min(ys), max(ys)) if log_y else (0, max(ys) * 1.05)

    def x_position(x):
        fraction = (math.log(x) - math.log(x_lo)) / (math.log(x_hi) - math.log(x_lo)) if log_x \
            else (x - x_lo) / ((x_hi - x_lo) or 1)
        return left + fraction * (width - left - right)

    def y_position(y):
        fraction = (math.log(y) - math.log(y_lo)) / ((math.log(y_hi) - math.log(y_lo)) or 1) if log_y \
            else (y - y_lo) / ((y_hi - y_lo) or 1)
        return height - bottom - fraction * (height - top - bottom)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
           f'height="{height}" role="img" aria-label="{title}">', style(),
           f'<rect class="bg" width="{width}" height="{height}"/>',
           f'<text class="t" x="{left}" y="24" font-size="15" font-weight="600">{title}</text>']
    for v in ticks(y_lo, y_hi, log_y):
        y = y_position(v)
        out.append(f'<line class="g" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" stroke-width="1"/>')
        out.append(f'<text class="m" x="{left - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end">{v:g}</text>')
    for v in sorted(set(xs)) if len(set(xs)) <= 12 else ticks(x_lo, x_hi, log_x):
        out.append(f'<text class="m" x="{x_position(v):.1f}" y="{height - bottom + 18}" font-size="11" '
                   f'text-anchor="middle">{v:g}</text>')
    out.append(f'<text class="m" x="{(left + width - right) / 2}" y="{height - 12}" font-size="12" '
               f'text-anchor="middle">{x_label}</text>')
    out.append(f'<text class="m" x="16" y="{(top + height - bottom) / 2}" font-size="12" text-anchor="middle" '
               f'transform="rotate(-90 16 {(top + height - bottom) / 2})">{y_label}</text>')
    for i, (name, points) in enumerate(series):
        path_data = " ".join(f"{'M' if k == 0 else 'L'}{x_position(x):.1f},{y_position(y):.1f}"
                             for k, (x, y) in enumerate(points))
        out.append(f'<path d="{path_data}" class="s{i}" fill="none" stroke-width="2" stroke-linejoin="round"/>')
        for x, y in points:
            out.append(marker(SHAPES[i], x_position(x), y_position(y), i, f"{name}: {x:g} → {y:g}"))
        last_x, last_y = points[-1]
        out.append(f'<text class="t" x="{x_position(last_x) + 10:.1f}" '
                   f'y="{y_position(last_y) + 4 + (i - 1) * 3:.1f}" font-size="11">{name}</text>')
    legend_x = left
    for i, (name, _) in enumerate(series):
        out.append(marker(SHAPES[i], legend_x + 6, 42, i, name))
        out.append(f'<text class="t" x="{legend_x + 16}" y="46" font-size="12">{name}</text>')
        legend_x += 22 + 7 * len(name) + 24
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
