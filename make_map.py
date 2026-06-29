"""make_map.py — interactive Folium map of the current 3-hour air-quality snapshot.

Reads current_air_quality_3h, pivots to one entry per city, and renders a self-contained
HTML map: one toggleable pollutant layer (FeatureGroup), each a CircleMarker per city
colored by a branca colormap scaled to that pollutant's P5–P95. PM2.5 is shown by default.
"""

import sys
import math
from collections import defaultdict
from datetime import timezone

import folium
from branca.colormap import LinearColormap
from branca.element import MacroElement
from jinja2 import Template

from db import connect

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "belgium_air_quality_map.html"
POLLUTANT_ORDER = ["pm25", "pm10", "o3", "no2", "so2", "co"]
DEFAULT_LAYER = "pm25"
PALETTE = ["#1a9641", "#a6d96a", "#ffffbf", "#fdae61", "#d7191c"]  # green (good) -> red (bad)


class BindColormap(MacroElement):
    """Show a colormap legend only while its FeatureGroup layer is enabled."""

    def __init__(self, layer, colormap, show):
        super().__init__()
        self.layer = layer
        self.colormap = colormap
        self.show = show
        self._template = Template("""
        {% macro script(this, kwargs) %}
            {{this.colormap.get_name()}}.svg[0][0].style.display = '{{ "block" if this.show else "none" }}';
            {{this._parent.get_name()}}.on('overlayadd', function (e) {
                if (e.layer == {{this.layer.get_name()}}) {
                    {{this.colormap.get_name()}}.svg[0][0].style.display = 'block'; }});
            {{this._parent.get_name()}}.on('overlayremove', function (e) {
                if (e.layer == {{this.layer.get_name()}}) {
                    {{this.colormap.get_name()}}.svg[0][0].style.display = 'none'; }});
        {% endmacro %}
        """)


def percentile(sorted_vals, p):
    """Linear-interpolated percentile of a pre-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def load_view():
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT city, parameter, unit, avg_value, measurement_count,
               location_count, latitude, longitude, window_start, window_end
        FROM current_air_quality_3h
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def pivot(rows):
    """rows -> {city: {lat, lon, pollutants: {param: {avg, unit, count}}}}, plus the window."""
    cities = {}
    window = (None, None)
    for city, param, unit, avg, mcount, lcount, lat, lon, ws, we in rows:
        if city is None:
            continue
        window = (ws, we)
        c = cities.setdefault(city, {"lats": [], "lons": [], "pollutants": {}})
        if lat is not None:
            c["lats"].append(float(lat))
        if lon is not None:
            c["lons"].append(float(lon))
        c["pollutants"][param] = {"avg": float(avg), "unit": unit, "count": mcount}
    for c in cities.values():
        c["lat"] = sum(c["lats"]) / len(c["lats"]) if c["lats"] else None
        c["lon"] = sum(c["lons"]) / len(c["lons"]) if c["lons"] else None
    return cities, window


def popup_html(city, c):
    body = "".join(
        f"<tr><td>{p}</td>"
        f"<td style='text-align:right;padding-left:10px'>{c['pollutants'][p]['avg']:.3f} {c['pollutants'][p]['unit']}</td>"
        f"<td style='text-align:right;padding-left:10px'>{c['pollutants'][p]['count']}</td></tr>"
        for p in sorted(c["pollutants"])
    )
    return (
        f"<div style='font-family:sans-serif'><b>{city}</b>"
        f"<table style='border-collapse:collapse;font-size:12px;margin-top:4px'>"
        f"<tr><th style='text-align:left'>pollutant</th><th>avg</th><th>n</th></tr>"
        f"{body}</table></div>"
    )


def main():
    cities, (ws, we) = pivot(load_view())

    param_values = defaultdict(list)
    param_unit = {}
    for c in cities.values():
        for p, info in c["pollutants"].items():
            param_values[p].append(info["avg"])
            param_unit.setdefault(p, info["unit"])

    present = [p for p in POLLUTANT_ORDER if p in param_values]
    present += [p for p in param_values if p not in present]

    colormaps, ranges = {}, {}
    for p in present:
        vals = sorted(param_values[p])
        lo, hi = percentile(vals, 5), percentile(vals, 95)
        if hi <= lo:
            hi = lo + (abs(lo) * 1e-6 or 1e-6)
        cm = LinearColormap(PALETTE, vmin=lo, vmax=hi)
        cm.caption = f"{p} ({param_unit[p]}) - 3h avg, P5-P95"
        colormaps[p] = cm
        ranges[p] = dict(p5=lo, p95=hi, vmin=min(vals), vmax=max(vals), n=len(vals))

    m = folium.Map(location=[50.5, 4.5], zoom_start=8, tiles="CartoDB positron", control_scale=True)

    for p in present:
        cm = colormaps[p]
        lo, hi = ranges[p]["p5"], ranges[p]["p95"]
        fg = folium.FeatureGroup(name=f"{p} ({param_unit[p]})", show=(p == DEFAULT_LAYER))
        for city, c in cities.items():
            if p not in c["pollutants"] or c["lat"] is None:
                continue
            v = min(max(c["pollutants"][p]["avg"], lo), hi)
            folium.CircleMarker(
                location=[c["lat"], c["lon"]],
                radius=6, weight=0.6, color="#444",
                fill=True, fill_color=cm(v)[:7], fill_opacity=0.85,
                tooltip=city,
                popup=folium.Popup(popup_html(city, c), max_width=320),
            ).add_to(fg)
        fg.add_to(m)
        cm.add_to(m)
        BindColormap(fg, cm, show=(p == DEFAULT_LAYER)).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    ws_s = ws.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    we_s = we.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    title = (
        "<div style='position:fixed;top:10px;left:60px;z-index:9999;background:white;"
        "padding:8px 12px;border:1px solid #ccc;border-radius:4px;font-family:sans-serif;"
        "box-shadow:0 1px 4px rgba(0,0,0,.3)'>"
        "<div style='font-size:15px;font-weight:bold'>Current air quality &mdash; 3-hour average</div>"
        f"<div style='font-size:12px;color:#555'>Window (UTC): {ws_s} &rarr; {we_s}</div>"
        "<div style='font-size:11px;color:#b00'>Simulated OpenAQ data &mdash; concentrations may be unrealistic</div>"
        "</div>"
    )
    m.get_root().html.add_child(folium.Element(title))
    m.save(OUT)

    print(f"Saved {OUT}")
    print(f"Cities plotted: {len(cities)}")
    print(f"Window (UTC): {ws_s} -> {we_s}")
    print("Per-pollutant color scale (P5-P95) and full value range:")
    for p in present:
        r = ranges[p]
        print(f"  {p:5} ({param_unit[p]:5}) cities={r['n']:4}  "
              f"scale[P5-P95]=[{r['p5']:.3f}, {r['p95']:.3f}]  "
              f"full[min-max]=[{r['vmin']:.3f}, {r['vmax']:.3f}]")


if __name__ == "__main__":
    main()
