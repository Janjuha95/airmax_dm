"""make_coverage_v2.py — OUTPUT 2: cleaned-up coverage map of all ~500 sensors/pollutant.

Same data as the v1 map (the view), but: 5 discrete quantile bands, small translucent
markers, and mutually-exclusive pollutant layers (radio) so layers never stack.
"""

import sys
from collections import defaultdict

import folium

from db import connect
import mapcommon as mc

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "belgium_air_quality_map_v2.html"


def main():
    conn = connect()
    cities, (ws, we) = mc.load_view(conn)
    conn.close()

    param_values = defaultdict(list)
    param_unit = {}
    for c in cities.values():
        for p, info in c["pollutants"].items():
            param_values[p].append(info["avg"])
            param_unit.setdefault(p, info["unit"])
    present = mc.present_pollutants(set(param_values))

    m = folium.Map(location=[50.6, 4.6], zoom_start=8, tiles="CartoDB positron", control_scale=True)
    fgs = []
    breakpoints = {}
    for p in present:
        cm, index = mc.step_colormap(param_values[p], f"{p} ({param_unit[p]}) - 3h avg, quantile bands")
        breakpoints[p] = index
        fg = folium.FeatureGroup(name=f"{p} ({param_unit[p]})", show=(p == mc.DEFAULT_POLLUTANT))
        for city, c in cities.items():
            if p not in c["pollutants"] or c["lat"] is None:
                continue
            folium.CircleMarker(
                [c["lat"], c["lon"]], radius=5, weight=1, color="#ffffff",
                fill=True, fill_color=mc.color_from(index, c["pollutants"][p]["avg"]), fill_opacity=0.6,
                tooltip=city, popup=mc.popup_table(city, c["pollutants"]),
            ).add_to(fg)
        fg.add_to(m)
        cm.add_to(m)
        mc.BindColormap(fg, cm, show=(p == mc.DEFAULT_POLLUTANT)).add_to(m)
        fgs.append(fg)

    mc.add_grouped_control(m, fgs)
    m.get_root().html.add_child(
        mc.title_box("Current air quality &mdash; 3h average (sensor coverage)", ws, we))
    m.save(OUT)

    print(f"Saved {OUT}  (window UTC {mc.fmt_utc(ws)} -> {mc.fmt_utc(we)})")
    print("5 quantile bands per pollutant (band edges = vmin, P20, P40, P60, P80, vmax):")
    for p in present:
        idx = breakpoints[p]
        print(f"  {p:5} ({param_unit[p]:5}) cities={len(param_values[p]):4}  edges=[" +
              ", ".join(f"{x:.3f}" for x in idx) + "]")


if __name__ == "__main__":
    main()
