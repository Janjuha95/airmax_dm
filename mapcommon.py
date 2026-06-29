"""mapcommon.py — shared helpers for the Step-5 readability maps.

Provides: the shared 3-hour window, a colour-blind-safe 5-class palette, discrete
quantile StepColormaps, a legend-to-layer binder that works with GroupedLayerControl,
title/popup builders, the view loader, and the exclusive grouped layer control.
"""

import math
import bisect
from datetime import timedelta, timezone

import folium
from folium.plugins import GroupedLayerControl
from branca.colormap import StepColormap
from branca.element import MacroElement
from jinja2 import Template

# ColorBrewer 5-class YlGnBu — colour-blind-safe sequential, light(low) -> dark(high).
PALETTE = ["#ffffcc", "#a1dab4", "#41b6c4", "#2c7fb8", "#253494"]

POLLUTANT_ORDER = ["pm25", "pm10", "o3", "no2", "so2", "co"]
DEFAULT_POLLUTANT = "pm25"


# ---------------------------------------------------------------- window / time
def get_window(conn):
    """Return (window_start, anchor) where anchor = max(date_utc), start = anchor - 3h."""
    cur = conn.cursor()
    cur.execute("SELECT max(date_utc) FROM measurements")
    anchor = cur.fetchone()[0]
    cur.close()
    return anchor - timedelta(hours=3), anchor


def fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- statistics
def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _strictly_increasing(idx):
    out = [idx[0]]
    for x in idx[1:]:
        out.append(x if x > out[-1] else out[-1] + 1e-9)
    return out


def step_colormap(values, caption, lo=None, hi=None):
    """5 discrete quantile bands. Returns (StepColormap, index[6]).

    Band edges are the 20/40/60/80 percentiles of `values`; outer bounds are
    lo/hi if given (e.g. P5/P95) else min/max.
    """
    vals = sorted(float(v) for v in values)
    vmin = lo if lo is not None else vals[0]
    vmax = hi if hi is not None else vals[-1]
    breaks = [percentile(vals, p) for p in (20, 40, 60, 80)]
    index = _strictly_increasing([vmin] + breaks + [vmax])
    cm = StepColormap(PALETTE, index=index, vmin=index[0], vmax=index[-1], caption=caption)
    return cm, index


def color_from(index, value):
    """Discrete colour for value using the 4 inner breakpoints of `index`."""
    i = min(bisect.bisect_right(index[1:-1], value), len(PALETTE) - 1)
    return PALETTE[i]


# ---------------------------------------------------------------- legend binding
class BindColormap(MacroElement):
    """Show a colormap legend only while its layer is on. Works with LayerControl
    and GroupedLayerControl (binds to both overlay* and layer* map events)."""

    def __init__(self, layer, colormap, show):
        super().__init__()
        self.layer = layer
        self.colormap = colormap
        self.show = show
        self._template = Template("""
        {% macro script(this, kwargs) %}
            var _disp_{{this.get_name()}} = function (on) {
                {{this.colormap.get_name()}}.svg[0][0].style.display = on ? 'block' : 'none'; };
            _disp_{{this.get_name()}}({{ 'true' if this.show else 'false' }});
            {% for ev in ['overlayadd', 'layeradd'] %}
            {{this._parent.get_name()}}.on('{{ev}}', function (e) {
                if (e.layer == {{this.layer.get_name()}}) { _disp_{{this.get_name()}}(true); }});
            {% endfor %}
            {% for ev in ['overlayremove', 'layerremove'] %}
            {{this._parent.get_name()}}.on('{{ev}}', function (e) {
                if (e.layer == {{this.layer.get_name()}}) { _disp_{{this.get_name()}}(false); }});
            {% endfor %}
        {% endmacro %}
        """)


# ---------------------------------------------------------------- HTML bits
def title_box(title, ws, we):
    return folium.Element(
        "<div style='position:fixed;top:10px;left:60px;z-index:9999;background:white;"
        "padding:8px 12px;border:1px solid #ccc;border-radius:4px;font-family:sans-serif;"
        "box-shadow:0 1px 4px rgba(0,0,0,.3)'>"
        f"<div style='font-size:15px;font-weight:bold'>{title}</div>"
        f"<div style='font-size:12px;color:#555'>Window (UTC): {fmt_utc(ws)} &rarr; {fmt_utc(we)}</div>"
        "<div style='font-size:11px;color:#b00'>Simulated data &mdash; concentrations may be unrealistic</div>"
        "</div>"
    )


def popup_table(name, pollutants):
    body = "".join(
        f"<tr><td>{p}</td>"
        f"<td style='text-align:right;padding-left:10px'>{pollutants[p]['avg']:.3f} {pollutants[p]['unit']}</td>"
        f"<td style='text-align:right;padding-left:10px'>{pollutants[p]['count']}</td></tr>"
        for p in sorted(pollutants)
    )
    return folium.Popup(
        f"<div style='font-family:sans-serif'><b>{name}</b>"
        "<table style='border-collapse:collapse;font-size:12px;margin-top:4px'>"
        "<tr><th style='text-align:left'>pollutant</th><th>avg</th><th>n</th></tr>"
        f"{body}</table></div>",
        max_width=320,
    )


def value_label(avg):
    """A DivIcon HTML string: value with a white halo so it reads over any tile/marker."""
    txt = f"{avg:.0f}" if abs(avg) >= 100 else f"{avg:.1f}"
    return (
        "<div style='font-size:11px;font-weight:700;color:#111;white-space:nowrap;"
        "text-shadow:-1px -1px 0 #fff,1px -1px 0 #fff,-1px 1px 0 #fff,1px 1px 0 #fff'>"
        f"{txt}</div>"
    )


def add_grouped_control(m, fgs, group_title="Pollutant", collapsed=False):
    """Exclusive (radio) grouped control: only one pollutant layer visible at a time."""
    GroupedLayerControl(groups={group_title: fgs}, exclusive_groups=True,
                        collapsed=collapsed).add_to(m)


# ---------------------------------------------------------------- view loader
def load_view(conn):
    """current_air_quality_3h -> ({city: {lat, lon, pollutants}}, (window_start, window_end))."""
    cur = conn.cursor()
    cur.execute("""
        SELECT city, parameter, unit, avg_value, measurement_count, latitude, longitude,
               window_start, window_end
        FROM current_air_quality_3h
    """)
    rows = cur.fetchall()
    cur.close()
    cities, window = {}, (None, None)
    for city, param, unit, avg, cnt, lat, lon, ws, we in rows:
        if city is None:
            continue
        window = (ws, we)
        c = cities.setdefault(city, {"lats": [], "lons": [], "pollutants": {}})
        if lat is not None:
            c["lats"].append(float(lat))
        if lon is not None:
            c["lons"].append(float(lon))
        c["pollutants"][param] = {"avg": float(avg), "unit": unit, "count": cnt}
    for c in cities.values():
        c["lat"] = sum(c["lats"]) / len(c["lats"]) if c["lats"] else None
        c["lon"] = sum(c["lons"]) / len(c["lons"]) if c["lons"] else None
    return cities, window


def present_pollutants(value_keys):
    """Pollutants present, ordered canonically with the default first."""
    present = [p for p in POLLUTANT_ORDER if p in value_keys]
    present += [p for p in value_keys if p not in present]
    if DEFAULT_POLLUTANT in present:
        present.remove(DEFAULT_POLLUTANT)
        present.insert(0, DEFAULT_POLLUTANT)
    return present
