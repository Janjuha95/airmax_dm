"""The three Folium maps: major-cities headline, sensor-coverage, and province choropleth.

All three use mutually-exclusive pollutant layers (radio), 5 discrete colour-blind-safe
quantile bands, and per-layer legends bound to layer visibility.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import urllib.request
from collections import defaultdict
from datetime import timezone

import folium
from branca.colormap import StepColormap
from branca.element import MacroElement
from folium.plugins import GroupedLayerControl
from jinja2 import Template
from shapely.geometry import Point, shape
from shapely.prepared import prep

from . import config
from .aggregate import in_window_measurements, major_city_aggregates, view_rows
from .db import connection
from .who import who_status

log = logging.getLogger(__name__)

NAME_KEY_PRIORITY = ["name", "NAME", "Name", "province", "PROVINCE", "NameDUT", "NameENG",
                     "NameFRE", "NameGER", "NAME_1", "prov_name", "NAAM"]


# ------------------------------------------------------------------ statistics / colours
def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _strictly_increasing(idx: list[float]) -> list[float]:
    out = [idx[0]]
    for x in idx[1:]:
        out.append(x if x > out[-1] else out[-1] + 1e-9)
    return out


def step_colormap(values, caption: str, lo: float | None = None, hi: float | None = None):
    """5 discrete quantile bands. Returns (StepColormap, index[6])."""
    vals = sorted(float(v) for v in values)
    vmin = lo if lo is not None else vals[0]
    vmax = hi if hi is not None else vals[-1]
    breaks = [percentile(vals, p) for p in (20, 40, 60, 80)]
    index = _strictly_increasing([vmin] + breaks + [vmax])
    cm = StepColormap(config.PALETTE, index=index, vmin=index[0], vmax=index[-1], caption=caption)
    return cm, index


def color_from(index: list[float], value: float) -> str:
    return config.PALETTE[min(bisect.bisect_right(index[1:-1], value), len(config.PALETTE) - 1)]


# ------------------------------------------------------------------ legend binding
class BindColormap(MacroElement):
    """Show a colormap legend only while its layer is on (works with GroupedLayerControl)."""

    def __init__(self, layer, colormap, show: bool):
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


# ------------------------------------------------------------------ HTML bits
def title_box(title: str, ws, we) -> folium.Element:
    fmt = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return folium.Element(
        "<div style='position:fixed;top:10px;left:60px;z-index:9999;background:white;"
        "padding:8px 12px;border:1px solid #ccc;border-radius:4px;font-family:sans-serif;"
        "box-shadow:0 1px 4px rgba(0,0,0,.3)'>"
        f"<div style='font-size:15px;font-weight:bold'>{title}</div>"
        f"<div style='font-size:12px;color:#555'>Window (UTC): {fmt(ws)} &rarr; {fmt(we)}</div>"
        "<div style='font-size:11px;color:#b00'>Simulated data &mdash; concentrations may be unrealistic</div>"
        "</div>"
    )


def popup_table(name: str, pollutants: dict) -> folium.Popup:
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
        f"{body}</table></div>", max_width=320)


def _halo(txt: str) -> str:
    return ("<div style='font-size:11px;font-weight:700;color:#111;white-space:nowrap;"
            "text-shadow:-1px -1px 0 #fff,1px -1px 0 #fff,-1px 1px 0 #fff,1px 1px 0 #fff'>"
            f"{txt}</div>")


def city_label(name: str, avg: float) -> str:
    return _halo(f"{name} {avg:.0f}" if abs(avg) >= 100 else f"{name} {avg:.1f}")


def city_popup_who(name: str, pollutants: dict) -> folium.Popup:
    body = ""
    for p in sorted(pollutants):
        d = pollutants[p]
        g, ratio, st = who_status(p, d["avg"])
        who = f"{g} {d['unit']}" if g is not None else "-"
        flag = f"{st} {ratio:.2f}×" if g is not None else "-"
        col = "#b00" if st == "ABOVE" else "#333"
        body += (f"<tr><td>{p}</td>"
                 f"<td style='text-align:right;padding-left:8px'>{d['avg']:.2f} {d['unit']}</td>"
                 f"<td style='text-align:right;padding-left:8px'>{d['count']}</td>"
                 f"<td style='text-align:right;padding-left:8px'>{who}</td>"
                 f"<td style='text-align:right;padding-left:8px;color:{col};font-weight:600'>{flag}</td></tr>")
    return folium.Popup(
        f"<div style='font-family:sans-serif'><b>{name}</b>"
        "<table style='border-collapse:collapse;font-size:12px;margin-top:4px'>"
        "<tr><th style='text-align:left'>poll.</th><th>3h avg</th><th>n</th><th>WHO</th><th>flag</th></tr>"
        f"{body}</table>"
        "<div style='font-size:10px;color:#777;margin-top:3px'>WHO 2021 short-term guideline "
        "(indicative: 3h avg vs 8–24h guideline). Simulated data.</div></div>", max_width=380)


def add_grouped_control(m, layers, title="Pollutant") -> None:
    GroupedLayerControl(groups={title: layers}, exclusive_groups=True, collapsed=False).add_to(m)


def present_pollutants(keys) -> list[str]:
    present = [p for p in config.POLLUTANT_ORDER if p in keys]
    present += [p for p in keys if p not in present]
    if config.DEFAULT_POLLUTANT in present:
        present.remove(config.DEFAULT_POLLUTANT)
        present.insert(0, config.DEFAULT_POLLUTANT)
    return present


def _base_map():
    return folium.Map(location=list(config.MAP_CENTER), zoom_start=config.MAP_ZOOM,
                      tiles=config.MAP_TILES, control_scale=True)


def _pivot_view(rows):
    cities, window = {}, (None, None)
    for city, param, unit, avg, cnt, _lc, lat, lon, ws, we in rows:
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


# ------------------------------------------------------------------ map 1: major cities
def build_major_cities() -> None:
    with connection() as conn:
        data, missing, (start, anchor) = major_city_aggregates(conn)
    present = present_pollutants({p for _n in data for p in data[_n]["pollutants"]})
    m = _base_map()
    layers = []
    for p in present:
        vals = [data[n]["pollutants"][p]["avg"] for n, _, _ in config.TARGET_CITIES
                if p in data[n]["pollutants"]]
        unit = next(data[n]["pollutants"][p]["unit"] for n, _, _ in config.TARGET_CITIES
                    if p in data[n]["pollutants"])
        cm, index = step_colormap(vals, f"{p} ({unit}) - 3h avg, quantile bands")
        fg = folium.FeatureGroup(name=f"{p} ({unit})", show=(p == config.DEFAULT_POLLUTANT))
        for name, _, _ in config.TARGET_CITIES:
            pdat = data[name]["pollutants"].get(p)
            if not pdat:
                continue
            above = who_status(p, pdat["avg"])[2] == "ABOVE"
            tip = f"{name}: {pdat['avg']:.1f} {pdat['unit']}" + (" ⚠ above WHO" if above else "")
            folium.CircleMarker(
                [data[name]["lat"], data[name]["lon"]], radius=12,
                weight=(3 if above else 1), color=("#d7191c" if above else "#333"),
                fill=True, fill_color=color_from(index, pdat["avg"]), fill_opacity=0.9,
                tooltip=tip, popup=city_popup_who(name, data[name]["pollutants"]),
            ).add_to(fg)
            folium.Marker([data[name]["lat"], data[name]["lon"]],
                          icon=folium.DivIcon(icon_size=(0, 0), icon_anchor=(-10, 6),
                                              html=city_label(name, pdat["avg"]))).add_to(fg)
        fg.add_to(m)
        cm.add_to(m)
        BindColormap(fg, cm, show=(p == config.DEFAULT_POLLUTANT)).add_to(m)
        layers.append(fg)

    add_grouped_control(m, layers)
    m.get_root().html.add_child(
        title_box("Current air quality in major Belgian cities &mdash; 3h average", start, anchor))
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;top:92px;left:60px;z-index:9999;background:white;padding:5px 10px;"
        "border:1px solid #ccc;border-radius:4px;font-family:sans-serif;font-size:11px;"
        "box-shadow:0 1px 4px rgba(0,0,0,.3)'>"
        "<span style='display:inline-block;width:9px;height:9px;border:3px solid #d7191c;"
        "border-radius:50%;vertical-align:middle'></span> exceeds WHO 2021 guideline (indicative)</div>"))
    m.save(str(config.MAJOR_CITIES_HTML))
    log.info("Saved %s (%d cities, %d missing pairs)", config.MAJOR_CITIES_HTML,
             len(config.TARGET_CITIES), len(missing))


# ------------------------------------------------------------------ map 2: sensor coverage
def build_coverage() -> None:
    with connection() as conn:
        cities, (ws, we) = _pivot_view(view_rows(conn))
    param_values, param_unit = defaultdict(list), {}
    for c in cities.values():
        for p, info in c["pollutants"].items():
            param_values[p].append(info["avg"])
            param_unit.setdefault(p, info["unit"])
    present = present_pollutants(set(param_values))

    m = _base_map()
    layers = []
    for p in present:
        cm, index = step_colormap(param_values[p], f"{p} ({param_unit[p]}) - 3h avg, quantile bands")
        fg = folium.FeatureGroup(name=f"{p} ({param_unit[p]})", show=(p == config.DEFAULT_POLLUTANT))
        for city, c in cities.items():
            if p not in c["pollutants"] or c["lat"] is None:
                continue
            folium.CircleMarker(
                [c["lat"], c["lon"]], radius=5, weight=1, color="#ffffff",
                fill=True, fill_color=color_from(index, c["pollutants"][p]["avg"]), fill_opacity=0.6,
                tooltip=city, popup=popup_table(city, c["pollutants"]),
            ).add_to(fg)
        fg.add_to(m)
        cm.add_to(m)
        BindColormap(fg, cm, show=(p == config.DEFAULT_POLLUTANT)).add_to(m)
        layers.append(fg)

    add_grouped_control(m, layers)
    m.get_root().html.add_child(title_box("Current air quality &mdash; 3h average (sensor coverage)", ws, we))
    m.save(str(config.COVERAGE_HTML))
    log.info("Saved %s (%d cities)", config.COVERAGE_HTML, len(cities))


# ------------------------------------------------------------------ map 3: province choropleth
def _load_provinces_geojson() -> dict:
    cache = config.PROVINCES_GEOJSON_CACHE
    if not cache.exists():
        req = urllib.request.Request(config.PROVINCES_GEOJSON_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            cache.write_bytes(r.read())
        log.info("Fetched and cached %s", cache)
    return json.loads(cache.read_text(encoding="utf-8"))


def _detect_name_key(features) -> str:
    keys = set(features[0]["properties"])
    for f in features:
        keys &= set(f["properties"])

    def qualifies(k):
        vals = [f["properties"].get(k) for f in features]
        return all(isinstance(v, str) and v.strip() for v in vals) and len(set(vals)) == len(vals)

    qual = [k for k in keys if qualifies(k)]
    for k in sorted(qual):  # a key marking a UNK feature is the documented name key
        if any("unk" in f["properties"][k].lower() for f in features):
            return k
    for k in NAME_KEY_PRIORITY:
        if k in qual:
            return k
    return sorted(qual)[0] if qual else ""


def _clean_name(nm: str) -> str:
    return nm[len("Provincie "):] if nm and nm.startswith("Provincie ") else nm


def build_provinces() -> dict:
    feats = _load_provinces_geojson()["features"]
    name_key = _detect_name_key(feats)
    provinces, dropped = [], []
    for f in feats:
        nm = f["properties"].get(name_key)
        if nm and "unk" in nm.lower():
            dropped.append(nm)
            continue
        provinces.append((_clean_name(nm), prep(shape(f["geometry"])), f["geometry"]))
    log.info("Province name key '%s': kept %d areas, dropped UNK %s", name_key, len(provinces),
             dropped or "(none)")

    with connection() as conn:
        rows, start, anchor = in_window_measurements(conn)
    agg = defaultdict(lambda: {"sum": 0.0, "n": 0, "locs": set()})
    units, unassigned = {}, 0
    for param, unit, value, lat, lon, loc_id in rows:
        units[param] = unit
        if lat is None or lon is None:
            unassigned += 1
            continue
        pt = Point(lon, lat)  # GeoJSON order is [lon, lat]
        hit = next((nm for nm, pg, _ in provinces if pg.contains(pt)), None)
        if hit is None:
            unassigned += 1
            continue
        a = agg[(hit, param)]
        a["sum"] += float(value)
        a["n"] += 1
        a["locs"].add(loc_id)

    present = present_pollutants(set(units))
    m = _base_map()
    layers, spreads = [], {}
    for p in present:
        # colour by the distribution of the PROVINCE AVERAGES (not individual sensors)
        pavgs = {nm: agg[(nm, p)]["sum"] / agg[(nm, p)]["n"]
                 for nm, _, _ in provinces if agg.get((nm, p)) and agg[(nm, p)]["n"]}
        if not pavgs:
            continue
        lo, hi = min(pavgs.values()), max(pavgs.values())
        spreads[p] = (lo, hi)
        cm, index = step_colormap(list(pavgs.values()),
                                  f"{p} ({units[p]}) - province avg {lo:.1f}-{hi:.1f}", lo=lo, hi=hi)
        feats_p = []
        for nm, _, geom in provinces:
            if nm in pavgs:
                a = agg[(nm, p)]
                props = {"name": nm, "value": f"{pavgs[nm]:.2f} {units[p]}", "n": a["n"],
                         "sensors": len(a["locs"]), "_color": color_from(index, pavgs[nm])}
            else:
                props = {"name": nm, "value": "no data", "n": 0, "sensors": 0, "_color": "#dddddd"}
            feats_p.append({"type": "Feature", "geometry": geom, "properties": props})
        gj = folium.GeoJson(
            {"type": "FeatureCollection", "features": feats_p},
            name=f"{p} ({units[p]})", show=(p == config.DEFAULT_POLLUTANT),
            style_function=lambda feat: {"fillColor": feat["properties"]["_color"],
                                         "color": "#888", "weight": 1, "fillOpacity": 0.78},
            highlight_function=lambda feat: {"weight": 2.5, "color": "#333"},
            tooltip=folium.GeoJsonTooltip(fields=["name", "value", "n", "sensors"],
                                          aliases=["Province", "Avg", "measurements (n)", "sensors"],
                                          sticky=True))
        gj.add_to(m)
        cm.add_to(m)
        BindColormap(gj, cm, show=(p == config.DEFAULT_POLLUTANT)).add_to(m)
        layers.append(gj)

    add_grouped_control(m, layers)
    m.get_root().html.add_child(title_box("Current air quality by province &mdash; 3h average", start, anchor))
    m.save(str(config.PROVINCES_HTML))
    log.info("Saved %s (%d areas, %d unassigned rows)", config.PROVINCES_HTML, len(provinces), unassigned)
    return spreads


def build_all() -> None:
    build_major_cities()
    build_coverage()
    build_provinces()
