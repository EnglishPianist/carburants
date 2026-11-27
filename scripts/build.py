#!/usr/bin/env python3
"""Construit les données du site carburants.

1. Prix actuels : flux instantané officiel (donnees.roulez-eco.fr/opendata/instantane).
2. Statistiques (une fois par jour) : historique officiel de l'année en cours
   (donnees.roulez-eco.fr/opendata/annee/AAAA), 90 derniers jours :
   - niveau du prix actuel par rapport aux 90 derniers jours de la station,
   - variation sur 7 jours,
   - effet jour de la semaine (écart moyen au prix moyen de la semaine),
   - séries quotidiennes (station + médiane du département) pour le graphique.
3. Sortie : tuiles JSON de 0,25° x 0,25° dans _site/data/.

Usage : python build.py --out _site/data --cache cache
"""
import argparse
import datetime as dt
import gzip
import io
import json
import math
import os
import statistics
import sys
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
INSTANT_URL = "https://donnees.roulez-eco.fr/opendata/instantane"
YEAR_URL = "https://donnees.roulez-eco.fr/opendata/annee/{year}"
FUELS = ["Gazole", "E10", "SP98", "SP95", "E85", "GPLc"]
DAYS = 90          # profondeur de l'historique utilisé
TILE = 0.25        # taille des tuiles en degrés
UA = {"User-Agent": "carburants-github-pages/1.0 (projet perso)"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def download(url, tries=4):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  échec {url} ({e}), nouvel essai…")
            time.sleep(10 * (k + 1))
    raise RuntimeError(f"téléchargement impossible : {url} : {last}")


def open_xml(blob):
    """Renvoie un flux XML à partir d'un zip (ou d'un XML brut)."""
    if blob[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(blob))
        name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        return z.open(name)
    return io.BytesIO(blob)


def coord(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if abs(x) > 1000:      # format officiel : degrés x 100 000
        x /= 100000
    return x


def price(v):
    try:
        x = float(v.replace(",", "."))
    except (AttributeError, ValueError):
        return None
    if x > 100:            # anciens fichiers : millièmes d'euro
        x /= 1000
    if not 0.3 < x < 5:
        return None
    return round(x * 1000)  # stocké en millièmes d'euro (entier)


def parse_ts(v):
    """'2026-11-27T06:01:00' ou '2026-11-27 06:01:00' (heure de Paris) -> epoch."""
    if not v:
        return None
    v = v.strip().replace("T", " ")[:19]
    try:
        d = dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            d = dt.datetime.strptime(v[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return int(d.replace(tzinfo=PARIS).timestamp())


def nice(t):
    t = " ".join((t or "").split()).title()
    for w in ("De", "Du", "Des", "La", "Le", "Les", "Et", "Sur", "En", "Aux", "Au"):
        t = t.replace(f" {w} ", f" {w.lower()} ")
    return t.replace(" L'", " l'").replace(" D'", " d'")


def fuel_name(n):
    n = (n or "").strip()
    return {"SP95-E10": "E10", "GPL": "GPLc"}.get(n, n)


# ---------------------------------------------------------------- instantané

def parse_instant(stream):
    stations = {}
    for _, el in ET.iterparse(stream, events=("end",)):
        if el.tag != "pdv":
            continue
        lat, lon = coord(el.get("latitude")), coord(el.get("longitude"))
        sid = el.get("id")
        if not sid or lat is None or lon is None or not (-90 < lat < 90):
            el.clear()
            continue
        st = {
            "i": sid,
            "a": round(lat, 5),
            "o": round(lon, 5),
            "cp": (el.get("cp") or "").strip(),
            "ad": nice(el.findtext("adresse")),
            "v": nice(el.findtext("ville")),
            "p": {},
            "r": [],
        }
        h = el.find("horaires")
        if h is not None and h.get("automate-24-24") == "1":
            st["au"] = 1
        for p in el.findall("prix"):
            f = fuel_name(p.get("nom"))
            val, ts = price(p.get("valeur")), parse_ts(p.get("maj"))
            if f in FUELS and val and ts:
                st["p"][f] = [val, ts]
        for r in el.findall("rupture"):
            f = fuel_name(r.get("nom"))
            fin = r.get("fin")
            if f in FUELS and (not fin or (parse_ts(fin) or 0) > time.time()):
                st["r"].append(f)
                st["p"].pop(f, None)
        if el.find("fermeture") is not None and el.find("fermeture").get("type") == "D":
            el.clear()
            continue
        if st["p"] or st["r"]:
            stations[sid] = st
        el.clear()
    return stations


# ---------------------------------------------------------------- historique

def parse_history(stream, since_ts):
    """{(id, carburant): [(ts, prix), ...]} — garde aussi le dernier relevé
    avant `since_ts` pour connaître le prix en vigueur au début de la fenêtre."""
    rec = defaultdict(list)
    before = {}
    meta = {}
    n = 0
    for _, el in ET.iterparse(stream, events=("end",)):
        if el.tag != "pdv":
            continue
        sid = el.get("id")
        meta[sid] = (el.get("cp") or "").strip()
        for p in el.findall("prix"):
            f = fuel_name(p.get("nom"))
            if f not in FUELS:
                continue
            val, ts = price(p.get("valeur")), parse_ts(p.get("maj"))
            if not val or not ts:
                continue
            n += 1
            k = (sid, f)
            if ts < since_ts:
                if k not in before or before[k][0] < ts:
                    before[k] = (ts, val)
            else:
                rec[k].append((ts, val))
        el.clear()
    for k, v in before.items():
        rec[k].append(v)
    log(f"  historique : {n} relevés lus, {len(rec)} couples station/carburant")
    return rec, meta


def daily_series(points, days):
    """Prix en vigueur à midi (heure de Paris) pour chacun des `days` derniers jours."""
    points.sort()
    out = []
    j, cur = 0, None
    for d in days:
        noon = int(dt.datetime.combine(d, dt.time(12), PARIS).timestamp())
        while j < len(points) and points[j][0] <= noon:
            cur = points[j][1]
            j += 1
        out.append(cur)
    return out


def weekday_effect(series, days):
    """Écart moyen (en dixièmes de centime) entre le prix du jour et la moyenne
    glissante centrée sur 7 jours, par jour de la semaine (lundi=0)."""
    acc = defaultdict(list)
    for i in range(3, len(series) - 3):
        win = series[i - 3:i + 4]
        if any(x is None for x in win):
            continue
        m = sum(win) / 7
        acc[days[i].weekday()].append(series[i] - m)
    if any(len(acc[w]) < 5 for w in range(7)):
        return None
    return [round(sum(acc[w]) / len(acc[w])) for w in range(7)]  # millièmes = 0,1 ct


def compute_stats(blobs, today):
    days = [today - dt.timedelta(days=DAYS - 1 - i) for i in range(DAYS)]
    since = int(dt.datetime.combine(days[0] - dt.timedelta(days=1), dt.time(0), PARIS).timestamp())
    rec, meta = defaultdict(list), {}
    for b in blobs:
        r, m = parse_history(open_xml(b), since)
        for k, v in r.items():
            rec[k].extend(v)
        meta.update(m)

    stats, series = {}, {}
    per_dep = defaultdict(lambda: defaultdict(list))  # dep -> fuel -> [series]
    for (sid, f), pts in rec.items():
        s = daily_series(pts, days)
        if sum(x is not None for x in s) < 14:
            continue
        series.setdefault(sid, {})[f] = s
        dep = dep_of(meta.get(sid, ""))
        if dep:
            per_dep[dep][f].append(s)
        st = {}
        wd = weekday_effect(s, days)
        if wd:
            st["wd"] = wd
        stats.setdefault(sid, {})[f] = st

    dep_med = {}
    for dep, fuels in per_dep.items():
        for f, lst in fuels.items():
            if len(lst) < 3:
                continue
            med = []
            for i in range(DAYS):
                vals = [s[i] for s in lst if s[i] is not None]
                med.append(round(statistics.median(vals)) if len(vals) >= 3 else None)
            dep_med.setdefault(dep, {})[f] = med
    return {
        "date": today.isoformat(),
        "start": days[0].isoformat(),
        "stats": stats,
        "series": series,
        "dep": dep_med,
    }


def dep_of(cp):
    cp = (cp or "").strip()
    if len(cp) != 5:
        return None
    if cp.startswith("97"):
        return cp[:3]
    if cp.startswith("20"):
        return "2A" if int(cp) < 20200 else "2B"
    return cp[:2]


# ---------------------------------------------------------------- sortie

def tile_key(lat, lon):
    return f"{math.floor(lat / TILE)}_{math.floor(lon / TILE)}"


def build_output(stations, hist, out):
    os.makedirs(os.path.join(out, "t"), exist_ok=True)
    os.makedirs(os.path.join(out, "h"), exist_ok=True)
    stats = hist.get("stats", {}) if hist else {}
    series = hist.get("series", {}) if hist else {}
    dep_med = hist.get("dep", {}) if hist else {}

    tiles = defaultdict(list)
    htiles = defaultdict(lambda: {"s": {}, "dep": {}})
    for sid, st in stations.items():
        s_st = stats.get(sid, {})
        s_se = series.get(sid, {})
        extra = {}
        for f, (val, _ts) in st["p"].items():
            e = {}
            ser = [x for x in s_se.get(f, []) if x is not None]
            if len(ser) >= 14:
                below = sum(1 for x in ser if x < val)
                e["pct"] = round(100 * below / len(ser))
                e["lo"], e["hi"] = min(ser), max(ser)
                full = s_se[f]
                if len(full) >= 8 and full[-8] is not None:
                    e["d7"] = val - full[-8]
            if "wd" in s_st.get(f, {}):
                e["wd"] = s_st[f]["wd"]
            if e:
                extra[f] = e
        if extra:
            st = dict(st, s=extra)
        k = tile_key(st["a"], st["o"])
        tiles[k].append(st)
        if s_se:
            htiles[k]["s"][sid] = s_se
            dep = dep_of(st["cp"])
            if dep and dep in dep_med:
                htiles[k]["dep"][dep] = dep_med[dep]

    for k, lst in tiles.items():
        with open(os.path.join(out, "t", f"{k}.json"), "w") as fh:
            json.dump(lst, fh, separators=(",", ":"), ensure_ascii=False)
    for k, h in htiles.items():
        with open(os.path.join(out, "h", f"{k}.json"), "w") as fh:
            json.dump(h, fh, separators=(",", ":"), ensure_ascii=False)

    meta = {
        "generated": int(time.time()),
        "stations": len(stations),
        "tile": TILE,
        "tiles": sorted(tiles),
        "hist_start": hist.get("start") if hist else None,
        "hist_date": hist.get("date") if hist else None,
        "fuels": FUELS,
    }
    with open(os.path.join(out, "meta.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))
    log(f"  sortie : {len(stations)} stations, {len(tiles)} tuiles")
    return meta


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site/data")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--instant-file", help="fichier local (tests)")
    ap.add_argument("--year-file", action="append", help="fichier(s) d'historique local (tests)")
    ap.add_argument("--force-stats", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now(PARIS)
    today = now.date()

    log("Prix actuels…")
    blob = open(args.instant_file, "rb").read() if args.instant_file else download(INSTANT_URL)
    stations = parse_instant(open_xml(blob))
    log(f"  {len(stations)} stations")
    if len(stations) < 100 and not args.instant_file:
        raise SystemExit("flux instantané anormalement vide : on ne publie pas")

    os.makedirs(args.cache, exist_ok=True)
    cache_file = os.path.join(args.cache, "hist.json.gz")
    hist = None
    if os.path.exists(cache_file):
        with gzip.open(cache_file, "rt") as fh:
            hist = json.load(fh)
        log(f"  statistiques en cache du {hist.get('date')}")

    if args.force_stats or not hist or hist.get("date") != today.isoformat():
        log("Calcul des statistiques sur l'historique officiel…")
        try:
            if args.year_file:
                blobs = [open(p, "rb").read() for p in args.year_file]
            else:
                years = sorted({today.year, (today - dt.timedelta(days=DAYS + 2)).year})
                blobs = [download(YEAR_URL.format(year=y)) for y in years]
            hist = compute_stats(blobs, today)
            with gzip.open(cache_file, "wt") as fh:
                json.dump(hist, fh, separators=(",", ":"))
            log(f"  statistiques calculées pour {len(hist['series'])} stations")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                    fh.write("new_stats=true\n")
        except Exception as e:  # noqa: BLE001
            log(f"  ATTENTION : statistiques non recalculées ({e}) — on garde les anciennes")

    build_output(stations, hist, args.out)


if __name__ == "__main__":
    main()
