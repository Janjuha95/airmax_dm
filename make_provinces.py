"""make_provinces.py — OUTPUT 3: executive province-level choropleth.

Fetches (and caches) Belgian province polygons, assigns each in-window measurement to a
province by point-in-polygon, aggregates per (province, pollutant), and renders one
discrete choropleth layer per pollutant (mutually exclusive).
"""

import sys
import os
import json
import urllib.request
from collections import defaultdict

import folium
from shapely.geometry import shape, Point
from shapely.prepared import prep

from db import connect
import mapcommon as mc

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "belgium_provinces_choropleth.html"
# NOTE: the brief's file (be-provinces-unk*.geo.json) is a Highcharts map whose geometry is
# in PROJECTED units, not lon/lat — so lon/lat point-in-polygon matches nothing. We use the
# same repo's true-WGS84 provinces file (real EPSG:4326 coords). It has 11 clean areas and no
# UNK feature, so there is nothing to discard; the UNK-drop logic below is kept and is a no-op.
GEOJSON_URL = ("https://raw.githubusercontent.com/mathiasleroy/belgium-geographic-data/"
               "master/dist/polygons/geojson/Belgium.provinces.WGS84.geojson")

# Preference order when auto-detecting the human-readable province-name property.
NAME_KEY_PRIORITY = ["name", "NAME", "Name", "province", "PROVINCE", "NameDUT", "NameENG",
                     "NameFRE", "NameGER", "NAME_1", "prov_name", "NAAM"]
GEOJSON_FILE = "belgium_provinces.geojson"


def load_geojson():
    if not os.path.exists(GEOJSON_FILE):
        req = urllib.request.Request(GEOJSON_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(GEOJSON_FILE, "wb") as f:
            f.write(data)
        print(f"Fetched and cached {GEOJSON_FILE} ({len(data)} bytes)")
    else:
        print(f"Using cached {GEOJSON_FILE}")
    with open(GEOJSON_FILE, encoding="utf-8") as f:
        return json.load(f)


def detect_name_key(features):
    """Auto-detect the property key holding the province name (all-unique non-empty strings)."""
    keys = set(features[0]["properties"])
    for f in features:
        keys &= set(f["properties"])

    def qualifies(k):
        vals = [f["properties"].get(k) for f in features]
        return all(isinstance(v, str) and v.strip() for v in vals) and len(set(vals)) == len(vals)

    qual = [k for k in keys if qualifies(k)]
    # 1) a qualifying key that marks a UNK feature is the documented name key
    for k in sorted(qual):
        if any("unk" in f["properties"][k].lower() for f in features):
            return k
    # 2) otherwise prefer a human-readable name key by priority
    for k in NAME_KEY_PRIORITY:
        if k in qual:
            return k
    # 3) fallback: first qualifying key
    return sorted(qual)[0] if qual else None


def clean_name(nm):
    return nm[len("Provincie "):] if nm and nm.startswith("Provincie ") else nm


def main():
    gj = load_geojson()
    feats = gj["features"]
    name_key = detect_name_key(feats)

    provinces, dropped = [], []
    for f in feats:
        nm = f["properties"].get(name_key)
        if nm and "unk" in nm.lower():
            dropped.append(nm)
            continue
        provinces.append((clean_name(nm), prep(shape(f["geometry"])), f["geometry"]))
    print(f"Province-name key: '{name_key}'. Areas kept: {len(provinces)}, "
          f"dropped (UNK): {dropped or 'none (this WGS84 file has no UNK feature)'}")

    # pull in-window measurements
    conn = connect()
    start, anchor = mc.get_window(conn)
    cur = conn.cursor()
    cur.execute("""SELECT parameter, unit, value, latitude, longitude, location_id
                   FROM measurements WHERE date_utc > %s AND date_utc <= %s""", (start, anchor))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    agg = defaultdict(lambda: {"sum": 0.0, "n": 0, "locs": set()})
    allvals = defaultdict(list)
    units = {}
    unassigned = 0
    for param, unit, value, lat, lon, loc_id in rows:
        v = float(value)
        allvals[param].append(v)
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
        a["sum"] += v
        a["n"] += 1
        a["locs"].add(loc_id)

    present = mc.present_pollutants(set(allvals))

    m = folium.Map(location=[50.6, 4.6], zoom_start=8, tiles="CartoDB positron", control_scale=True)
    layers, breakpoints, prov_avgs = [], {}, {}
    for p in present:
        # Colour by the distribution of the PROVINCE AVERAGES (not individual sensors), so the
        # 11 provinces spread across the bands instead of all landing in one. Scale = their min-max.
        pavgs = {nm: agg[(nm, p)]["sum"] / agg[(nm, p)]["n"]
                 for nm, _, _ in provinces if agg.get((nm, p)) and agg[(nm, p)]["n"]}
        if not pavgs:
            continue
        lo, hi = min(pavgs.values()), max(pavgs.values())
        cm, index = mc.step_colormap(list(pavgs.values()),
                                     f"{p} ({units[p]}) - province avg {lo:.1f}-{hi:.1f}", lo=lo, hi=hi)
        breakpoints[p] = index
        prov_avgs[p] = list(pavgs.values())
        feats_p = []
        for nm, _, geom in provinces:
            if nm in pavgs:
                avg, a = pavgs[nm], agg[(nm, p)]
                props = {"name": nm, "value": f"{avg:.2f} {units[p]}", "n": a["n"],
                         "sensors": len(a["locs"]), "_color": mc.color_from(index, avg)}
            else:
                props = {"name": nm, "value": "no data", "n": 0, "sensors": 0, "_color": "#dddddd"}
            feats_p.append({"type": "Feature", "geometry": geom, "properties": props})
        gjson = folium.GeoJson(
            {"type": "FeatureCollection", "features": feats_p},
            name=f"{p} ({units[p]})", show=(p == mc.DEFAULT_POLLUTANT),
            style_function=lambda feat: {"fillColor": feat["properties"]["_color"],
                                         "color": "#888", "weight": 1, "fillOpacity": 0.78},
            highlight_function=lambda feat: {"weight": 2.5, "color": "#333"},
            tooltip=folium.GeoJsonTooltip(
                fields=["name", "value", "n", "sensors"],
                aliases=["Province", "Avg", "measurements (n)", "sensors"], sticky=True),
        )
        gjson.add_to(m)
        cm.add_to(m)
        mc.BindColormap(gjson, cm, show=(p == mc.DEFAULT_POLLUTANT)).add_to(m)
        layers.append(gjson)

    mc.add_grouped_control(m, layers)
    m.get_root().html.add_child(
        mc.title_box("Current air quality by province &mdash; 3h average", start, anchor))
    m.save(OUT)

    # ---- report ----
    print(f"\nSaved {OUT}  (window UTC {mc.fmt_utc(start)} -> {mc.fmt_utc(anchor)})")
    print(f"Rows in window: {len(rows)} | unassigned (no province): {unassigned}")
    prov_names = [nm for nm, _, _ in provinces]
    hdr = "province".ljust(20) + "".join(p.rjust(16) for p in present)
    print(hdr)
    for nm in prov_names:
        line = nm[:19].ljust(20)
        for p in present:
            a = agg.get((nm, p))
            line += (f"{a['sum']/a['n']:.1f}(n{a['n']},s{len(a['locs'])})".rjust(16)) if a and a["n"] else "-".rjust(16)
        print(line)
    print("\nSpread of province averages (max - min) — small = uniform/simulated:")
    for p in present:
        a = prov_avgs[p]
        if a:
            print(f"  {p:5} ({units[p]:5}) provinces={len(a):2}  "
                  f"min={min(a):.2f}  max={max(a):.2f}  spread={max(a)-min(a):.2f}")


if __name__ == "__main__":
    main()
