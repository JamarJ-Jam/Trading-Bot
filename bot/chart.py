"""Write a backtest's running profit as a self-contained HTML chart.

No extra packages: the page is plain SVG plus a few lines of JavaScript for
the hover readout. Open it in any browser (from WSL: explorer.exe <file>).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

# Categorical series colors, in fixed order (light, dark).
COLORS = [
    ("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#008300"),
    ("#6250d6", "#9085e9"), ("#e34948", "#e66767"),
]
W, H = 960, 440
PAD_L, PAD_R, PAD_T, PAD_B = 64, 120, 24, 40


def equity_series(trades) -> list[tuple[int, float]]:
    total, points = 0.0, []
    for t in trades:
        total += t.profit
        points.append((t.exit_epoch, round(total, 2)))
    return points


def _nice_step(span: float) -> float:
    raw = span / 5 or 1
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 0.1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def write_chart(path: Path, results: list[tuple[str, list]], title: str) -> None:
    series = [(sym, equity_series(trades)) for sym, trades in results][:len(COLORS)]
    points = [p for _, s in series for p in s]
    if not points:
        return
    t0, t1 = min(p[0] for p in points), max(p[0] for p in points)
    lo, hi = min(0.0, *(p[1] for p in points)), max(0.0, *(p[1] for p in points))
    step = _nice_step(hi - lo)
    lo, hi = step * (lo // step), step * -(-hi // step)
    t1 = t1 if t1 > t0 else t0 + 1
    hi = hi if hi > lo else lo + 1

    def x(t):
        return PAD_L + (t - t0) / (t1 - t0) * (W - PAD_L - PAD_R)

    def y(v):
        return PAD_T + (hi - v) / (hi - lo) * (H - PAD_T - PAD_B)

    parts = []
    v = lo
    while v <= hi + 1e-9:
        cls = "zero" if abs(v) < 1e-9 else "grid"
        parts.append(f'<line class="{cls}" x1="{PAD_L}" x2="{W - PAD_R}" '
                     f'y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        parts.append(f'<text class="axis" x="{PAD_L - 8}" y="{y(v) + 4:.1f}" '
                     f'text-anchor="end">{v:+.0f}</text>' if step >= 1 else
                     f'<text class="axis" x="{PAD_L - 8}" y="{y(v) + 4:.1f}" '
                     f'text-anchor="end">{v:+.1f}</text>')
        v += step
    y0, y1 = (datetime.fromtimestamp(t, timezone.utc).year for t in (t0, t1))
    year_step = max(1, (y1 - y0) // 8)
    for year in range(y0 + 1, y1 + 1, year_step):
        t = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
        if t0 <= t <= t1:
            parts.append(f'<text class="axis" x="{x(t):.1f}" y="{H - PAD_B + 20}" '
                         f'text-anchor="middle">{year}</text>')

    # End-of-line labels, nudged apart so close finishes stay readable.
    ends = sorted((y(pts[-1][1]), i) for i, (_, pts) in enumerate(series) if pts)
    label_y, last = {}, -1e9
    for ly, i in ends:
        last = max(ly, last + 15)
        label_y[i] = last

    data = []
    for i, (sym, pts) in enumerate(series):
        if not pts:
            continue
        d = " ".join(f"{'M' if j == 0 else 'L'}{x(t):.1f},{y(v):.1f}"
                     for j, (t, v) in enumerate(pts))
        parts.append(f'<path class="line s{i}" d="{d}"/>')
        lt, lv = pts[-1]
        parts.append(f'<text class="label" x="{x(lt) + 8:.1f}" y="{label_y[i] + 4:.1f}">'
                     f'{html.escape(sym)} {lv:+.2f}</text>')
        data.append({"sym": sym, "i": i,
                     "pts": [[round(x(t), 1), round(y(v), 1), t, v] for t, v in pts]})

    legend = "".join(
        f'<span class="key"><span class="swatch s{i}"></span>{html.escape(sym)}</span>'
        for i, (sym, _) in enumerate(series))
    light = "".join(f".s{i}{{--c:{c[0]}}}" for i, c in enumerate(COLORS))
    dark = "".join(f".s{i}{{--c:{c[1]}}}" for i, c in enumerate(COLORS))
    rows = "".join(
        f"<tr><td>{html.escape(sym)}</td><td>{len(trades)}</td>"
        f"<td>{sum(t.profit for t in trades):+.2f}</td></tr>"
        for sym, trades in results)

    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:light;--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e4e3df;--zero:#9a9893}}
{light}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#33332f;--zero:#6b6a64}}{dark}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,sans-serif}}
main{{max-width:{W}px;margin:0 auto}}
h1{{font-size:18px;margin:0 0 4px}} p{{color:var(--ink2);margin:0 0 12px}}
.legend{{display:flex;flex-wrap:wrap;gap:16px;margin:0 0 8px;color:var(--ink2)}}
.key{{display:inline-flex;align-items:center;gap:6px}}
.swatch{{width:12px;height:12px;border-radius:3px;background:var(--c)}}
svg{{width:100%;height:auto;display:block}}
.grid{{stroke:var(--grid);stroke-width:1}} .zero{{stroke:var(--zero);stroke-width:1}}
.axis{{fill:var(--ink2);font-size:12px}} .label{{fill:var(--ink);font-size:12px}}
.line{{fill:none;stroke:var(--c);stroke-width:2;stroke-linejoin:round}}
#cross{{stroke:var(--zero);stroke-width:1;visibility:hidden}}
#tip{{position:fixed;pointer-events:none;background:var(--bg);color:var(--ink);border:1px solid var(--grid);
border-radius:6px;padding:6px 8px;font-size:12px;visibility:hidden;white-space:nowrap}}
table{{border-collapse:collapse;margin-top:16px}} td,th{{padding:4px 12px 4px 0;text-align:left}}
th{{color:var(--ink2);font-weight:500}}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p>Running profit per symbol, in account currency, for a stake of 1. Each step is one closed trade.</p>
<div class="legend">{legend}</div>
<svg id="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(title)}">
{''.join(parts)}
<line id="cross" y1="{PAD_T}" y2="{H - PAD_B}"/>
<rect x="{PAD_L}" y="{PAD_T}" width="{W - PAD_L - PAD_R}" height="{H - PAD_T - PAD_B}" fill="transparent" id="hit"/>
</svg>
<div id="tip"></div>
<table><tr><th>Symbol</th><th>Trades</th><th>Net</th></tr>{rows}</table>
</main>
<script>
const data = {json.dumps(data)};
const svg = document.getElementById("chart"), cross = document.getElementById("cross"),
      tip = document.getElementById("tip");
document.getElementById("hit").addEventListener("mousemove", e => {{
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  const sx = pt.matrixTransform(svg.getScreenCTM().inverse()).x;
  let when = null, lines = [];
  for (const s of data) {{
    let best = null;
    for (const p of s.pts) {{ if (p[0] <= sx) best = p; else break; }}
    if (best) {{ lines.push(`<span class="swatch s${{s.i}}" style="display:inline-block;width:8px;height:8px"></span> <span style="color:var(--ink2)">${{s.sym}}</span> ${{best[3] >= 0 ? "+" : ""}}${{best[3].toFixed(2)}}`);
                 when = when === null ? best[2] : Math.max(when, best[2]); }}
  }}
  if (!lines.length) return;
  cross.setAttribute("x1", sx); cross.setAttribute("x2", sx); cross.style.visibility = "visible";
  tip.innerHTML = `<b>${{new Date(when * 1000).toISOString().slice(0, 10)}}</b><br>` + lines.join("<br>");
  tip.style.left = (e.clientX + 14) + "px"; tip.style.top = (e.clientY + 14) + "px";
  tip.style.visibility = "visible";
}});
document.getElementById("hit").addEventListener("mouseleave", () => {{
  cross.style.visibility = "hidden"; tip.style.visibility = "hidden";
}});
</script></body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
