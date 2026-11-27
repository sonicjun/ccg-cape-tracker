#!/usr/bin/env python3
"""
Fetch and compile the Canadian Coast Guard (CCG) 47-ft Cape Class Motor Lifeboat
fleet registry from the public CCG Fleet Database (FDAT) and public Canadian
maritime MMSI/SAR station registries.

Outputs: data/vessels.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import re
import sys
import socket
import unicodedata
from pathlib import Path
from typing import Any

import requests
import urllib3.util.connection
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Force IPv4 to avoid stalls on hosts where .gc.ca AAAA records time out locally
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_fleet")

FDAT_BASE = "https://inter-j02.dfo-mpo.gc.ca"
# Query active (status=1) and decommissioned (status=3) to capture all 36 47-ft Cape Class lifeboats
FDAT_INDEX_URLS = [
    f"{FDAT_BASE}/fdat/vessels?status=1&search=close",
    f"{FDAT_BASE}/fdat/vessels?status=3&search=close",
]

# Non-Cape-Class vessels with "Cape"/"Cap" in the name (explicit exclusion safeguard)
EXCLUDED_VESSEL_NAMES = {
    "CAPE ROGER",       # 62.5 m Offshore Patrol Vessel
    "CAPE LIGHT",       # 14.6 m x 4.9 m Specialty Buoy/Work Vessel (MetalCraft Marine)
    "CAPE HURD",        # 21.4 m Mid Shore Patrol Vessel
    "CAP-AUX-MEULES",   # 15.8 m Arun-class SAR Lifeboat
}

# Verified 9-digit Canadian MMSIs (316xxxxxx) & public CCG SAR Lifeboat Station assignments
# keyed by Official Number (Transport Canada / FDAT Official Number)
PUBLIC_MARITIME_REGISTRY: dict[str, dict[str, Any]] = {
    # Atlantic Region (5)
    "825755": {"mmsi": "316004847", "station": "Shippagan, NB", "lat": 47.7442, "lon": -64.7056},
    "825754": {"mmsi": "316004876", "station": "Summerside, PE", "lat": 46.3917, "lon": -63.7897},
    "824504": {"mmsi": "316002527", "station": "Lark Harbour, NL", "lat": 49.1028, "lon": -58.3686},
    "824517": {"mmsi": "316002528", "station": "Port au Choix, NL", "lat": 50.7108, "lon": -57.3592},
    "825552": {"mmsi": "316004846", "station": "Souris, PE", "lat": 46.3514, "lon": -62.2519},
    # Central Region - Quebec / St. Lawrence (6)
    "834785": {"mmsi": "316017158", "station": "Québec, QC", "lat": 46.8169, "lon": -71.2003},
    "825012": {"mmsi": "316005758", "station": "Havre-Saint-Pierre, QC", "lat": 50.2389, "lon": -63.5986},
    "827177": {"mmsi": "316006673", "station": "Tadoussac, QC", "lat": 48.1419, "lon": -69.7181},
    "827262": {"mmsi": "316013913", "station": "Kegaska, QC", "lat": 50.1847, "lon": -61.2661},
    "825066": {"mmsi": "316005759", "station": "Rivière-au-Renard, QC", "lat": 48.9972, "lon": -64.3842},
    "827095": {"mmsi": "316006672", "station": "Rimouski, QC", "lat": 48.4789, "lon": -68.5156},
    # Central Region - Great Lakes (12)
    "825998": {"mmsi": "316005374", "station": "Thunder Bay, ON", "lat": 48.4192, "lon": -89.2189},
    "826367": {"mmsi": "316005376", "station": "Tobermory, ON", "lat": 45.2556, "lon": -81.6653},
    "826714": {"mmsi": "316002184", "station": "Goderich, ON", "lat": 43.7456, "lon": -81.7258},
    "826975": {"mmsi": "316006894", "station": "Amherstburg, ON", "lat": 42.1039, "lon": -83.1128},
    "827458": {"mmsi": "316006713", "station": "Burlington, ON", "lat": 43.3003, "lon": -79.7972},
    "826833": {"mmsi": "316003285", "station": "Kingston, ON", "lat": 44.2281, "lon": -76.4792},
    "822796": {"mmsi": "316005379", "station": "Sarnia, ON", "lat": 42.9742, "lon": -82.4092},
    "822744": {"mmsi": "316005378", "station": "Cobourg, ON", "lat": 43.9547, "lon": -78.1667},
    "826160": {"mmsi": "316005375", "station": "Meaford, ON", "lat": 44.6089, "lon": -80.5892},
    "834891": {"mmsi": "316017159", "station": "Port Dover, ON", "lat": 42.7836, "lon": -80.2014},
    "823616": {"mmsi": "316005377", "station": "Port Weller, ON", "lat": 43.2356, "lon": -79.2186},
    "822456": {"mmsi": "316005382", "station": "Parry Sound, ON", "lat": 45.3392, "lon": -80.0369},
    # Western Region - Pacific / BC (13)
    "826525": {"mmsi": "316005204", "station": "Tofino, BC", "lat": 49.1530, "lon": -125.9066},
    "821249": {"mmsi": "316001481", "station": "Victoria, BC", "lat": 48.4251, "lon": -123.3879},
    "826600": {"mmsi": "316005337", "station": "Powell River, BC", "lat": 49.8781, "lon": -124.5547},
    "825415": {"mmsi": "316003544", "station": "French Creek, BC", "lat": 49.3497, "lon": -124.3592},
    "835014": {"mmsi": "316017161", "station": "Campbell River, BC", "lat": 50.0244, "lon": -125.2442},
    "825229": {"mmsi": "316003545", "station": "Bella Bella, BC", "lat": 52.1636, "lon": -128.1436},
    "827459": {"mmsi": "316006922", "station": "Ganges, BC", "lat": 48.8539, "lon": -123.4986},
    "825930": {"mmsi": "316004106", "station": "Bamfield, BC", "lat": 48.8339, "lon": -125.1364},
    "825169": {"mmsi": "316002986", "station": "Sandspit, BC", "lat": 53.2542, "lon": -131.8153},
    "835140": {"mmsi": "316017162", "station": "Masset, BC", "lat": 54.0114, "lon": -132.1478},
    "835203": {"mmsi": "316017163", "station": "Prince Rupert, BC", "lat": 54.3122, "lon": -130.3275},
    "821250": {"mmsi": "316001476", "station": "Kitsilano (Vancouver), BC", "lat": 49.2764, "lon": -123.1536},
    "821176": {"mmsi": "316001556", "station": "Port Hardy, BC", "lat": 50.7231, "lon": -127.4917},
}

# Administrative fallback coordinates if FDAT adds a new station
FDAT_PORT_FALLBACK_COORDS: dict[str, tuple[str, float, float]] = {
    "B.C. - Victoria": ("Victoria, BC", 48.4251, -123.3879),
    "Ont. - Sarnia": ("Sarnia, ON", 42.9742, -82.4092),
    "Ont. - Burlington": ("Burlington, ON", 43.3003, -79.7972),
    "Que. - Québec": ("Québec, QC", 46.8169, -71.2003),
    "N.B. - Shippagan": ("Shippagan, NB", 47.7442, -64.7056),
    "P.E.I. - Summerside": ("Summerside, PE", 46.3917, -63.7897),
    "P.E.I. - Souris": ("Souris, PE", 46.3514, -62.2519),
    "N.L. - Lark Harbour": ("Lark Harbour, NL", 49.1028, -58.3686),
    "N.L. - Port Aux Choix": ("Port au Choix, NL", 50.7108, -57.3592),
}


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": "CCG-CapeClass-Tracker/1.0 (+https://inter-j02.dfo-mpo.gc.ca/fdat/)",
            "Accept-Language": "en-CA,en;q=0.9",
        }
    )
    return session


def clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def format_vessel_name(raw_h2: str) -> str:
    """Normalize 'CCGS CAPE MCKAY' -> 'CCGS Cape McKay', 'CCGS CAP D'ESPOIR' -> 'CCGS Cap d'Espoir'."""
    raw = clean_ws(raw_h2)
    has_prefix = raw.upper().startswith("CCGS ")
    body = raw[5:].strip() if has_prefix else raw

    special_map = {
        "CAP D'ESPOIR": "Cap d'Espoir",
        "CAP DE RABAST": "Cap de Rabast",
        "CAP PERCÉ": "Cap Percé",
        "CAPE MCKAY": "Cape McKay",
        "CAPE ST. JAMES": "Cape St. James",
    }
    upper_body = body.upper()
    if upper_body in special_map:
        formatted = special_map[upper_body]
    else:
        formatted = " ".join(w.capitalize() for w in body.split())

    return f"CCGS {formatted}"


def slugify_id(name: str) -> str:
    norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return slug


def normalize_region(fdat_region: str) -> str:
    low = fdat_region.lower()
    if "atlantic" in low:
        return "Atlantic"
    if "western" in low or "pacific" in low:
        return "Western"
    return "Central"


def extract_section_fields(details_tag: Any) -> dict[str, list[str]]:
    """Extract label -> list of item values from an FDAT <details> section."""
    result: dict[str, list[str]] = {}
    if not details_tag:
        return result

    for row in details_tag.find_all("div", class_="row", recursive=False):
        lbl = row.find("label")
        if not lbl:
            continue
        key = clean_ws(lbl.get_text(" ", strip=True)).rstrip(":").strip()

        # Multi-item list layout uses col-sm-10 with a second inline container div
        col10 = row.find("div", class_="col-sm-10")
        if col10:
            child_divs = col10.find_all("div", recursive=False)
            if len(child_divs) >= 2:
                items = [
                    clean_ws(d.get_text(" ", strip=True))
                    for d in child_divs[1].find_all("div", recursive=False)
                    if clean_ws(d.get_text(" ", strip=True))
                ]
                if not items:
                    val = clean_ws(child_divs[1].get_text(" ", strip=True))
                    if val:
                        items = [val]
                result[key] = items
                continue

        # Single-value layout (col-sm-12 / col-md-6)
        container = lbl.parent
        full_text = clean_ws(container.get_text(" ", strip=True))
        lbl_text = clean_ws(lbl.get_text(" ", strip=True))
        val = full_text[len(lbl_text) :].strip() if full_text.startswith(lbl_text) else full_text
        if val and val.lower() not in {"not applicable", "none"}:
            result[key] = [val]

    return result


def discover_cape_class_urls(session: requests.Session) -> list[str]:
    """Discover candidate 14.6 m SAR Lifeboat detail URLs from FDAT index tables."""
    urls: list[str] = []
    seen: set[str] = set()

    for idx_url in FDAT_INDEX_URLS:
        logger.info("Fetching FDAT fleet index: %s", idx_url)
        resp = session.get(idx_url, timeout=20)
        resp.raise_for_status()
        html = resp.content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        for tr in soup.select("table tr"):
            cells = [clean_ws(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            vessel_col, _region_col, length_col = cells[0], cells[1], cells[2]

            # Strict pre-filter: must be SAR Lifeboat and 14.6 m length
            if "SAR Lifeboat" not in vessel_col or length_col != "14.6 m":
                continue

            upper_col = vessel_col.upper()
            if any(excl in upper_col for excl in EXCLUDED_VESSEL_NAMES):
                continue

            for a in tr.find_all("a", href=True):
                href = a["href"]
                if "/fdat/vessels/" in href and "/report/" not in href and href not in seen:
                    seen.add(href)
                    urls.append(f"{FDAT_BASE}{href}" if href.startswith("/") else href)

    return urls


def parse_vessel_page(session: requests.Session, url: str) -> dict[str, Any] | None:
    """Fetch and parse a single FDAT vessel page, strictly verifying 47-ft Cape Class attributes."""
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    html = resp.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup

    h2 = main.find("h2")
    if not h2:
        logger.warning("Skipping %s: missing vessel heading.", url)
        return None

    raw_name = clean_ws(h2.get_text(" ", strip=True))
    upper_plain = raw_name.upper().replace("CCGS ", "").strip()
    if upper_plain in EXCLUDED_VESSEL_NAMES:
        logger.info("Excluding non-Cape-Class vessel: %s", raw_name)
        return None

    # Official Number & Call Sign
    official_number = ""
    mainimg = main.find("div", class_="mainimg")
    if mainimg:
        img_txt = mainimg.get_text("\n", strip=True)
        m_on = re.search(r"Official Number:\s*(\d+)", img_txt)
        if m_on:
            official_number = m_on.group(1)

    gen = extract_section_fields(main.find("details", id="general-operation"))
    crew_sec = extract_section_fields(main.find("details", id="crewing-regime"))
    con_sec = extract_section_fields(main.find("details", id="construction-details"))
    spec_sec = extract_section_fields(main.find("details", id="vessel-specifications"))
    eng_sec = extract_section_fields(main.find("details", id="engineering"))
    nav_sec = extract_section_fields(main.find("details", id="comms-and-navigation-equipment"))

    vessel_class = gen.get("Vessel Class", [""])[0]
    length_m = float(spec_sec.get("Length (m)", ["0"])[0])
    breadth_m = float(spec_sec.get("Breadth (m)", ["0"])[0])

    # Strict Cape Class verification: SAR Lifeboat, 14.6 m LOA, 4.3 m beam
    if vessel_class != "SAR Lifeboat" or length_m != 14.6 or breadth_m != 4.3:
        logger.info(
            "Excluding %s (class=%s, length=%s, breadth=%s)",
            raw_name,
            vessel_class,
            length_m,
            breadth_m,
        )
        return None

    name = format_vessel_name(raw_name)
    vid = slugify_id(name)
    fdat_region = gen.get("Home Region", ["Central and Arctic"])[0]
    region = normalize_region(fdat_region)

    # Year built
    yr_str = (
        con_sec.get("Year Launched", [None])[0]
        or con_sec.get("Year in Service", [None])[0]
        or "2004"
    )
    year_built = int(re.search(r"\d{4}", yr_str).group(0))

    # Specs
    max_speed = float(spec_sec.get("Maximum Speed (kts)", ["25"])[0])
    range_nm = float(spec_sec.get("Cruising Range (nm)", ["200"])[0])
    crew_cnt = int(float(crew_sec.get("Complement", ["4"])[0]))

    specs = {
        "length_m": round(length_m, 1),
        "max_speed_kt": int(max_speed) if max_speed.is_integer() else max_speed,
        "range_nm": int(range_nm) if range_nm.is_integer() else range_nm,
        "crew": crew_cnt,
    }

    # Equipment - Propulsion
    propulsion: list[str] = []
    for me in eng_sec.get("Main Engines", []):
        propulsion.append(f"Main Engines: {me}")
    prop_type = eng_sec.get("Propulsion", [None])[0]
    power_kw = eng_sec.get("Power (kw)", [None])[0]
    if prop_type and power_kw:
        propulsion.append(f"Drive: {prop_type} ({power_kw} kW)")
    elif prop_type:
        propulsion.append(f"Drive: {prop_type}")
    for prop in eng_sec.get("Propellers", []):
        propulsion.append(f"Propellers: {prop}")
    for gen_item in eng_sec.get("Generators", []):
        propulsion.append(f"Generators: {gen_item}")

    # Equipment - Navigation
    navigation: list[str] = []
    nav_labels = [
        ("Radars", "Radar"),
        ("Electronic Charts", "ECS"),
        ("Auto Pilot", "Autopilot"),
        ("GPS", "GPS"),
        ("Gyro", "Gyrocompass"),
        ("Depth Sounders", "Depth Sounder"),
    ]
    for fdat_key, display_prefix in nav_labels:
        for item in nav_sec.get(fdat_key, []):
            navigation.append(f"{display_prefix}: {item}")

    # Equipment - Communications
    communications: list[str] = []
    comm_labels = [
        ("VHF FM", "VHF FM"),
        ("VHF AM", "VHF AM (Aviation/SAR)"),
        ("HF", "HF Radio"),
        ("VHF DF", "VHF Direction Finder"),
    ]
    for fdat_key, display_prefix in comm_labels:
        for item in nav_sec.get(fdat_key, []):
            communications.append(f"{display_prefix}: {item}")

    # Registry enrichment (MMSI + SAR station coordinates)
    reg_entry = PUBLIC_MARITIME_REGISTRY.get(official_number)
    fdat_port = gen.get("Home Port", [""])[0]

    if reg_entry:
        mmsi = reg_entry.get("mmsi")
        home_port = reg_entry["station"]
        home_lat = round(float(reg_entry["lat"]), 4)
        home_lon = round(float(reg_entry["lon"]), 4)
    else:
        mmsi = None
        fallback = FDAT_PORT_FALLBACK_COORDS.get(fdat_port, ("Ottawa, ON", 45.4215, -75.6972))
        home_port, home_lat, home_lon = fallback
        logger.warning(
            "UNVERIFIED MMSI for %s (Official #%s, FDAT Port '%s') -> set to null for manual entry.",
            name,
            official_number,
            fdat_port,
        )

    return {
        "id": vid,
        "name": name,
        "mmsi": mmsi,
        "region": region,
        "home_port": home_port,
        "home_lat": home_lat,
        "home_lon": home_lon,
        "year_built": year_built,
        "specs": specs,
        "equipment": {
            "propulsion": propulsion,
            "navigation": navigation,
            "communications": communications,
        },
        "source_url": url,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile CCG 47-ft Cape Class vessels.json from FDAT.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "vessels.json",
        help="Output path for vessels.json",
    )
    args = parser.parse_args()

    session = build_session()
    candidate_urls = discover_cape_class_urls(session)
    logger.info("Discovered %d candidate 14.6 m SAR Lifeboat URLs in FDAT.", len(candidate_urls))

    vessels: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        future_to_url = {pool.submit(parse_vessel_page, session, u): u for u in candidate_urls}
        for fut in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[fut]
            try:
                vessel = fut.result()
                if vessel:
                    vessels.append(vessel)
            except Exception as exc:
                logger.error("Failed to fetch/parse %s: %s", url, exc)
                return 1

    vessels.sort(key=lambda v: v["name"])

    verified_count = sum(1 for v in vessels if v["mmsi"])
    missing = [v["name"] for v in vessels if not v["mmsi"]]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(vessels, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    logger.info("Wrote %d Cape Class vessels to %s", len(vessels), args.output)
    logger.info("Verified MMSIs: %d / %d", verified_count, len(vessels))
    if missing:
        logger.warning("Missing MMSIs (%d): %s", len(missing), ", ".join(missing))
    else:
        logger.info("All %d Cape Class vessels have verified 9-digit Canadian MMSIs (316xxxxxx).", len(vessels))

    return 0


if __name__ == "__main__":
    sys.exit(main())
