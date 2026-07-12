#!/usr/bin/env python3
"""
Opdaterer busplanen for Villas Caravaning ud fra NAP/ALSA GTFS-data.

Output:
  docs/busplan-villas-caravaning.json

Secrets/env i GitHub Actions:
  NAP_API_KEY        Påkrævet for automatisk hentning fra NAP.
  NAP_FICHERO_ID    Valgfri. Hvis udfyldt, bruges dette fil-id direkte.
  LOCAL_GTFS_ZIP    Valgfri. Bruges kun til lokal test med en allerede downloadet GTFS-ZIP.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

NAP_BASE = "https://nap.transportes.gob.es"
DATASET_SEARCH_TERMS = ("alsa", "autobuses")
OUTPUT_PATH = Path("docs/busplan-villas-caravaning.json")
TIMEZONE = "Europe/Madrid"
DAYS_AHEAD = int(os.getenv("BUSPLAN_DAYS_AHEAD", "14"))

# Vi leder efter stoppestederne ved Villas Caravaning.
# I ALSA GTFS-filen har de bl.a. heddet:
# - Caravaning Camping
# - Camping Caravanning
TARGET_STOP_NAME_RE = re.compile(r"(caravaning|caravanning)", re.IGNORECASE)

# Sikkerhedsfilter: stop skal ligge omkring Villas Caravaning / Mar Menor.
TARGET_LAT_MIN, TARGET_LAT_MAX = 37.55, 37.70
TARGET_LON_MIN, TARGET_LON_MAX = -0.90, -0.65


def log(message: str) -> None:
    print(f"[busplan] {message}", flush=True)


def api_get_json(path: str, api_key: str, query: dict | None = None) -> dict:
    url = NAP_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={
        "ApiKey": api_key,
        "Accept": "application/json",
        "User-Agent": "villas-caravaning-busplan/1.0",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_alsa_file_id(api_key: str) -> tuple[str, dict]:
    """
    Finder et GTFS-fil-id for ALSA automatisk.
    Hvis NAP_FICHERO_ID er sat, bruges det direkte.
    """
    explicit = os.getenv("NAP_FICHERO_ID", "").strip()
    if explicit:
        log(f"Bruger NAP_FICHERO_ID={explicit}")
        return explicit, {"id": explicit, "source": "NAP_FICHERO_ID"}

    log("Søger efter ALSA-datasæt i NAP...")
    page = 1
    while page <= 20:
        data = api_get_json("/api/v2/conjunto-dato", api_key, {"page": page, "items": 1000})
        items = data.get("data") or []
        if not items:
            break

        for dataset in items:
            name = (dataset.get("nombre") or "").lower()
            desc = (dataset.get("descripcion") or "").lower()
            haystack = name + " " + desc
            if all(term in haystack for term in DATASET_SEARCH_TERMS):
                files = dataset.get("ficheros") or []
                valid_files = [f for f in files if f.get("esValido", True)]
                files_to_check = valid_files or files
                for f in files_to_check:
                    tipo = (f.get("nombreTipoFichero") or "").lower()
                    if "gtfs" in tipo or "zip" in tipo or not tipo:
                        file_id = str(f.get("id"))
                        if file_id and file_id != "None":
                            log(f"Fundet datasæt: {dataset.get('nombre')} · fil-id {file_id}")
                            return file_id, {"dataset": dataset, "file": f}
        page += 1

    raise RuntimeError(
        "Kunne ikke finde ALSA GTFS-filen automatisk. "
        "Sæt evt. NAP_FICHERO_ID som GitHub secret/variable."
    )


def download_gtfs_zip(api_key: str) -> Path:
    """
    Henter seneste GTFS-ZIP fra NAP og returnerer lokal filsti.
    """
    local_zip = os.getenv("LOCAL_GTFS_ZIP", "").strip()
    if local_zip:
        path = Path(local_zip)
        if not path.exists():
            raise FileNotFoundError(path)
        log(f"Bruger lokal GTFS-ZIP: {path}")
        return path

    file_id, meta = find_alsa_file_id(api_key)
    info = api_get_json(f"/api/v2/fichero/{file_id}/descarga", api_key)
    payload = info.get("data") or {}
    download_url = payload.get("enlaceDescarga")
    filename = payload.get("nombreFichero") or "alsa_gtfs.zip"
    if not download_url:
        raise RuntimeError(f"NAP returnerede ikke enlaceDescarga for fil-id {file_id}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="alsa_gtfs_"))
    zip_path = tmp_dir / filename
    log(f"Downloader GTFS-ZIP fra NAP: {filename}")
    req = urllib.request.Request(download_url, headers={"User-Agent": "villas-caravaning-busplan/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(zip_path, "wb") as f:
        f.write(resp.read())
    log(f"Downloadet: {zip_path}")
    return zip_path


def read_csv_from_zip(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def parse_gtfs_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y%m%d").date()


def gtfs_time_to_display(value: str) -> str:
    # GTFS kan bruge tider over 24:00. Til visning bruger vi modulo 24.
    h, m, s = [int(x) for x in value.split(":")]
    h = h % 24
    return f"{h:02d}:{m:02d}"


def service_active_on(service: dict, date: dt.date) -> bool:
    if not service:
        return False
    start = service.get("start_date")
    end = service.get("end_date")
    if start and date < start:
        return False
    if end and date > end:
        return False
    weekday_keys = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    return service.get(weekday_keys[date.weekday()], "0") == "1"


def build_busplan(zip_path: Path) -> dict:
    now_madrid = dt.datetime.now(ZoneInfo(TIMEZONE))
    today = now_madrid.date()
    dates = [today + dt.timedelta(days=i) for i in range(DAYS_AHEAD)]

    with zipfile.ZipFile(zip_path) as zf:
        # 1) Stoppesteder
        stops = {}
        for row in read_csv_from_zip(zf, "stops.txt"):
            name = row.get("stop_name", "")
            try:
                lat = float(row.get("stop_lat") or "nan")
                lon = float(row.get("stop_lon") or "nan")
            except ValueError:
                continue
            if (
                TARGET_STOP_NAME_RE.search(name)
                and TARGET_LAT_MIN <= lat <= TARGET_LAT_MAX
                and TARGET_LON_MIN <= lon <= TARGET_LON_MAX
            ):
                stops[row["stop_id"]] = {
                    "stop_id": row["stop_id"],
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                }

        if not stops:
            raise RuntimeError("Fandt ingen relevante stoppesteder ved Villas Caravaning.")

        target_stop_ids = set(stops)
        log(f"Fundet stoppesteder: {', '.join(s['name'] for s in stops.values())}")

        # 2) Stop times for de relevante stop
        stop_times = []
        trip_ids = set()
        for row in read_csv_from_zip(zf, "stop_times.txt"):
            if row.get("stop_id") in target_stop_ids:
                stop_times.append(row)
                trip_ids.add(row.get("trip_id"))

        # 3) Trips
        trips = {}
        route_ids = set()
        service_ids = set()
        for row in read_csv_from_zip(zf, "trips.txt"):
            if row.get("trip_id") in trip_ids:
                trips[row["trip_id"]] = row
                route_ids.add(row.get("route_id"))
                service_ids.add(row.get("service_id"))

        # 4) Routes
        routes = {}
        for row in read_csv_from_zip(zf, "routes.txt"):
            if row.get("route_id") in route_ids:
                routes[row["route_id"]] = row

        # 5) Calendar
        services = {}
        if "calendar.txt" in zf.namelist():
            for row in read_csv_from_zip(zf, "calendar.txt"):
                if row.get("service_id") in service_ids:
                    services[row["service_id"]] = {
                        **row,
                        "start_date": parse_gtfs_date(row["start_date"]),
                        "end_date": parse_gtfs_date(row["end_date"]),
                    }

        # 6) Calendar exceptions
        exceptions_by_service = {}
        if "calendar_dates.txt" in zf.namelist():
            for row in read_csv_from_zip(zf, "calendar_dates.txt"):
                sid = row.get("service_id")
                if sid in service_ids:
                    exceptions_by_service.setdefault(sid, {})[parse_gtfs_date(row["date"])] = row.get("exception_type")

        def is_active(service_id: str, date: dt.date) -> bool:
            active = service_active_on(services.get(service_id), date)
            ex = exceptions_by_service.get(service_id, {}).get(date)
            if ex == "1":
                active = True
            elif ex == "2":
                active = False
            return active

        # 7) Generér afgange for de næste dage
        days = []
        for date in dates:
            departures = []
            for st in stop_times:
                trip = trips.get(st.get("trip_id"))
                if not trip:
                    continue
                service_id = trip.get("service_id")
                if not is_active(service_id, date):
                    continue

                route = routes.get(trip.get("route_id"), {})
                departures.append({
                    "time": gtfs_time_to_display(st.get("departure_time") or st.get("arrival_time")),
                    "rawTime": st.get("departure_time") or st.get("arrival_time"),
                    "stopId": st.get("stop_id"),
                    "stopName": stops[st.get("stop_id")]["name"],
                    "routeShortName": route.get("route_short_name", ""),
                    "routeLongName": route.get("route_long_name", ""),
                    "headsign": trip.get("trip_headsign", ""),
                    "directionId": trip.get("direction_id", ""),
                    "tripId": st.get("trip_id"),
                    "serviceId": service_id,
                })

            departures.sort(key=lambda x: x["rawTime"])
            # Fjern duplikater, hvis samme tid/rute/headsign optræder flere gange.
            seen = set()
            unique = []
            for d in departures:
                key = (d["time"], d["stopName"], d["routeShortName"], d["headsign"], d["routeLongName"])
                if key not in seen:
                    seen.add(key)
                    unique.append(d)

            days.append({
                "date": date.isoformat(),
                "weekday": date.strftime("%A"),
                "departures": unique,
            })

        route_list = []
        for rid, route in sorted(routes.items(), key=lambda kv: (kv[1].get("route_short_name", ""), kv[1].get("route_long_name", ""))):
            route_list.append({
                "routeId": rid,
                "shortName": route.get("route_short_name", ""),
                "longName": route.get("route_long_name", ""),
                "url": route.get("route_url", ""),
            })

        return {
            "schema": "villas-caravaning-busplan",
            "version": 1,
            "generatedAt": now_madrid.isoformat(),
            "timezone": TIMEZONE,
            "validForDays": DAYS_AHEAD,
            "source": {
                "name": "NAP Transporte Multimodal / ALSA",
                "attribution": "Powered by MITRAMS",
                "license": "https://nap.transportes.gob.es/licencia-datos",
                "note": "Afgangstider vises som orienterende information baseret på officielle åbne data. Kontrollér altid aktuelle tider hos operatøren ved tvivl."
            },
            "stops": list(stops.values()),
            "routes": route_list,
            "days": days,
        }


def main() -> int:
    api_key = os.getenv("NAP_API_KEY", "").strip()
    if not api_key and not os.getenv("LOCAL_GTFS_ZIP"):
        raise RuntimeError("NAP_API_KEY mangler. Opret den som GitHub repository secret.")

    zip_path = download_gtfs_zip(api_key)
    busplan = build_busplan(zip_path)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(busplan, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Skrev {OUTPUT_PATH}")
    log(f"Dage: {len(busplan['days'])}, ruter: {len(busplan['routes'])}, stop: {len(busplan['stops'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
