"""make_major_cities.py — OUTPUT 1 (Step 6A/6B): headline map of 15 major Belgian cities.

For each city x pollutant, average all in-window measurements from sensors within ~15 km of
the city centroid. Markers in 5 discrete quantile bands, mutually exclusive pollutant layers,
permanent "{City} {value}" labels. Overlays an INDICATIVE WHO-2021 guideline flag (red ring
for cities above the selected pollutant's guideline) and writes who_exceedance_summary.csv.
"""

import sys
import csv
from collections import defaultdict

import folium

from db import connect
import mapcommon as mc

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = "belgium_major_cities.html"
WHO_CSV = "who_exceedance_summary.csv"
CITIES = [
    ("Brussels", 50.85, 4.35), ("Antwerp", 51.22, 4.40), ("Ghent", 51.05, 3.72),
    ("Charleroi", 50.41, 4.44), ("Liège", 50.63, 5.57), ("Bruges", 51.21, 3.22),
    ("Namur", 50.47, 4.87), ("Leuven", 50.88, 4.70), ("Mons", 50.45, 3.95),
    ("Mechelen", 51.03, 4.48), ("Kortrijk", 50.83, 3.27), ("Hasselt", 50.93, 5.34),
    ("Ostend", 51.23, 2.92), ("Aalst", 50.94, 4.04), ("Genk", 50.97, 5.50),
]
BOX_DLAT, BOX_DLON = 0.135, 0.215  # ~15 km box

# WHO 2021 short-term guideline values. NOTE: these are 8h (o3) / 24h (rest) guidelines;
# we compare our 3-hour average to them as an INDICATIVE flag only, not formal compliance.
WHO_2021 = {"pm25": 15, "pm10": 45, "no2": 25, "so2": 40, "o3": 100, "co": 4}


def aggregate(rows):
    """Bin in-window rows into city x pollutant {avg, unit, count}; return (data, missing)."""
    acc = {name: defaultdict(lambda: {"sum": 0.0, "n": 0, "unit": None}) for name, _, _ in CITIES}
    for param, unit, value, lat, lon in rows:
        if lat is None or lon is None:
            continue
        for name, clat, clon in CITIES:
            if abs(lat - clat) < BOX_DLAT and abs(lon - clon) < BOX_DLON:
                a = acc[name][param]
                a["sum"] += float(value)
                a["n"] += 1
                a["unit"] = unit
    data = {name: {"lat": lat, "lon": lon, "pollutants": {}} for name, lat, lon in CITIES}
    missing = []
    for name, _, _ in CITIES:
        for p in mc.POLLUTANT_ORDER:
            a = acc[name].get(p)
            if a and a["n"] > 0:
                data[name]["pollutants"][p] = {"avg": a["sum"] / a["n"], "unit": a["unit"], "count": a["n"]}
            else:
                missing.append((name, p))
    return data, missing


def who_status(param, avg):
    """Return (guideline, ratio, 'ABOVE'|'within') or (None, None, None) if no guideline."""
    g = WHO_2021.get(param)
    if g is None:
        return None, None, None
    return g, avg / g, ("ABOVE" if avg > g else "within")


def city_label(name, avg):
    txt = f"{name} {avg:.0f}" if abs(avg) >= 100 else f"{name} {avg:.1f}"
    return (
        "<div style='font-size:11px;font-weight:700;color:#111;white-space:nowrap;"
        "text-shadow:-1px -1px 0 #fff,1px -1px 0 #fff,-1px 1px 0 #fff,1px 1px 0 #fff'>"
        f"{txt}</div>"
    )


def city_popup(name, pollutants):
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
        "(indicative: 3h avg vs 8–24h guideline). Simulated data.</div></div>",
        max_width=380,
    )


def build_map(data, present):
    m = folium.Map(location=[50.6, 4.6], zoom_start=8, tiles="CartoDB positron", control_scale=True)
    fgs = []
    for p in present:
        vals = [data[n]["pollutants"][p]["avg"] for n, _, _ in CITIES if p in data[n]["pollutants"]]
        unit = next(data[n]["pollutants"][p]["unit"] for n, _, _ in CITIES if p in data[n]["pollutants"])
        cm, index = mc.step_colormap(vals, f"{p} ({unit}) - 3h avg, quantile bands")
        fg = folium.FeatureGroup(name=f"{p} ({unit})", show=(p == mc.DEFAULT_POLLUTANT))
        for name, _, _ in CITIES:
            pdat = data[name]["pollutants"].get(p)
            if not pdat:
                continue
            _, ratio, st = who_status(p, pdat["avg"])
            above = st == "ABOVE"
            tip = f"{name}: {pdat['avg']:.1f} {pdat['unit']}" + (" ⚠ above WHO" if above else "")
            folium.CircleMarker(
                [data[name]["lat"], data[name]["lon"]], radius=12,
                weight=(3 if above else 1), color=("#d7191c" if above else "#333"),
                fill=True, fill_color=mc.color_from(index, pdat["avg"]), fill_opacity=0.9,
                tooltip=tip, popup=city_popup(name, data[name]["pollutants"]),
            ).add_to(fg)
            folium.Marker(
                [data[name]["lat"], data[name]["lon"]],
                icon=folium.DivIcon(icon_size=(0, 0), icon_anchor=(-10, 6),
                                    html=city_label(name, pdat["avg"])),
            ).add_to(fg)
        fg.add_to(m)
        cm.add_to(m)
        mc.BindColormap(fg, cm, show=(p == mc.DEFAULT_POLLUTANT)).add_to(m)
        fgs.append(fg)

    mc.add_grouped_control(m, fgs)
    m.get_root().html.add_child(
        mc.title_box("Current air quality in major Belgian cities &mdash; 3h average", START, ANCHOR))
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;top:92px;left:60px;z-index:9999;background:white;padding:5px 10px;"
        "border:1px solid #ccc;border-radius:4px;font-family:sans-serif;font-size:11px;"
        "box-shadow:0 1px 4px rgba(0,0,0,.3)'>"
        "<span style='display:inline-block;width:9px;height:9px;border:3px solid #d7191c;"
        "border-radius:50%;vertical-align:middle'></span> exceeds WHO 2021 guideline (indicative)</div>"))
    m.save(OUT)


def exceedance_rows(data):
    rows = []
    for name, _, _ in CITIES:
        polls = data[name]["pollutants"]
        per, exceeded = {}, []
        for p in mc.POLLUTANT_ORDER:
            d = polls.get(p)
            if not d:
                continue
            g, ratio, st = who_status(p, d["avg"])
            per[p] = (d["avg"], ratio, st)
            if st == "ABOVE":
                exceeded.append(p)
        rows.append({"city": name, "num": len(exceeded), "exceeded": exceeded, "per": per,
                     "pm25_ratio": per.get("pm25", (0, 0, ""))[1]})
    rows.sort(key=lambda r: (-r["num"], -r["pm25_ratio"]))
    return rows


def write_and_report_who(data):
    rows = exceedance_rows(data)
    with open(WHO_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        header = ["rank", "city", "num_exceeded", "exceeded"]
        for p in mc.POLLUTANT_ORDER:
            header += [f"{p}_avg", f"{p}_ratio", f"{p}_status"]
        w.writerow(header)
        for i, r in enumerate(rows, 1):
            line = [i, r["city"], r["num"], ";".join(r["exceeded"])]
            for p in mc.POLLUTANT_ORDER:
                if p in r["per"]:
                    avg, ratio, st = r["per"][p]
                    line += [f"{avg:.2f}", f"{ratio:.2f}", st]
                else:
                    line += ["", "", ""]
            w.writerow(line)
    print(f"\nWrote {WHO_CSV}. Ranked WHO-exceedance summary (indicative; simulated data):")
    print("rank  city".ljust(22) + "#exc  " + "  ".join(p.rjust(11) for p in mc.POLLUTANT_ORDER))
    for i, r in enumerate(rows, 1):
        line = f"{i:>2}  {r['city']:<14}".ljust(22) + f"{r['num']:>3}   "
        for p in mc.POLLUTANT_ORDER:
            avg, ratio, st = r["per"].get(p, (None, None, None))
            cell = f"{ratio:.2f}{'*' if st == 'ABOVE' else ' '}" if ratio is not None else "-"
            line += f"  {cell:>11}"
        print(line)
    print("(* = above WHO guideline; cell = avg/guideline ratio)")

    n = len(CITIES)
    print("\nHeadline counts (cities exceeding WHO 2021 short-term guideline):")
    for p in mc.POLLUTANT_ORDER:
        above = sum(1 for r in rows if r["per"].get(p, (None, None, None))[2] == "ABOVE")
        unit = next((data[c]["pollutants"][p]["unit"] for c, _, _ in CITIES if p in data[c]["pollutants"]), "")
        tag = "  <-- PM2.5" if p == "pm25" else ""
        print(f"  {above} of {n} exceed WHO {p} ({WHO_2021[p]} {unit}){tag}")


def main():
    global START, ANCHOR
    conn = connect()
    START, ANCHOR = mc.get_window(conn)
    cur = conn.cursor()
    cur.execute("""SELECT parameter, unit, value, latitude, longitude
                   FROM measurements WHERE date_utc > %s AND date_utc <= %s""", (START, ANCHOR))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    data, missing = aggregate(rows)
    present = mc.present_pollutants({p for n, _, _ in CITIES for p in data[n]["pollutants"]})
    build_map(data, present)

    print(f"Saved {OUT}  (window UTC {mc.fmt_utc(START)} -> {mc.fmt_utc(ANCHOR)})")
    hdr = "city".ljust(11) + "".join(p.rjust(13) for p in present)
    print(hdr)
    for name, _, _ in CITIES:
        line = name.ljust(11)
        for p in present:
            pdat = data[name]["pollutants"].get(p)
            line += (f"{pdat['avg']:.1f}(n={pdat['count']})".rjust(13)) if pdat else "-".rjust(13)
        print(line)
    print(f"\nMissing (no nearby sensor) — {len(missing)} pairs: " +
          (", ".join(f"{c}/{p}" for c, p in missing) if missing else "none"))

    write_and_report_who(data)


if __name__ == "__main__":
    main()
