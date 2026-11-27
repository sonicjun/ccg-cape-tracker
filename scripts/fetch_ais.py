#!/usr/bin/env python3
"""
Near-real-time AIS position updater for the CCG 47-ft Cape Class fleet.

Connects to AISStream.io via WebSocket (with permessage-deflate compression),
subscribes to all verified Cape Class MMSIs for a bounded window (default 90s),
and persistently merges newly received PositionReports into data/positions.json
without erasing previously cached positions for silent/out-of-range vessels.

Supports --mock for offline/winter local development and full 3-tier UI testing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("fetch_ais")

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"

NAV_STATUS_MAP: dict[int, str] = {
    0: "Under way using engine",
    1: "At anchor",
    2: "Not under command",
    3: "Restricted manoeuvrability",
    4: "Constrained by her draught",
    5: "Moored",
    6: "Aground",
    7: "Engaged in fishing",
    8: "Under way sailing",
    11: "Power-driven vessel towing astern",
    12: "Power-driven vessel pushing/towing alongside",
    14: "AIS-SART / MOB / EPIRB active",
    15: "Undefined",
}


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_aisstream_timestamp(raw_ts: str | None) -> str:
    """Normalize AISStream MetaData.time_utc into ISO-8601 UTC string."""
    if not raw_ts:
        return iso_utc(datetime.now(timezone.utc))
    # Example: "2026-11-27 14:10:05.123456789 +0000 UTC"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", raw_ts.strip())
    if m:
        return f"{m.group(1)}T{m.group(2)}Z"
    return iso_utc(datetime.now(timezone.utc))


def load_fleet(vessels_path: Path) -> list[dict[str, Any]]:
    if not vessels_path.exists():
        raise FileNotFoundError(f"Fleet file not found: {vessels_path}")
    return json.loads(vessels_path.read_text(encoding="utf-8"))


def load_existing_positions(positions_path: Path) -> dict[str, Any]:
    """Load existing persistent cache if present; never discard prior vessel records."""
    if not positions_path.exists():
        return {"updated_at_utc": None, "source": "aisstream", "positions": {}}
    try:
        data = json.loads(positions_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("positions"), dict):
            return data
    except Exception as exc:
        logger.warning("Could not parse existing %s (%s); starting fresh cache.", positions_path, exc)
    return {"updated_at_utc": None, "source": "aisstream", "positions": {}}


async def collect_live_ais(
    api_key: str,
    mmsi_to_vessel: dict[str, dict[str, Any]],
    duration_sec: int,
) -> dict[str, dict[str, Any]]:
    """Connect to AISStream.io with permessage-deflate and collect latest PositionReport per MMSI."""
    import websockets

    mmsi_list = sorted(mmsi_to_vessel.keys())
    if not mmsi_list:
        logger.warning("No non-null MMSIs found in vessels.json; skipping WebSocket stream.")
        return {}

    subscribe_payload = {
        "APIKey": api_key,
        # Global bounding box ensures vessels in transit or refit anywhere are captured alongside MMSI filter
        "BoundingBoxes": [[[-90.0, -180.0], [90.0, 180.0]]],
        "FiltersShipMMSI": mmsi_list,
        "FilterMessageTypes": [
            "PositionReport",
            "StandardClassBPositionReport",
            "ExtendedClassBPositionReport",
        ],
    }

    updates: dict[str, dict[str, Any]] = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration_sec

    logger.info(
        "Connecting to %s (compression=deflate, MMSIs=%d, window=%ds)...",
        AISSTREAM_URL,
        len(mmsi_list),
        duration_sec,
    )

    while loop.time() < deadline:
        remaining = deadline - loop.time()
        if remaining <= 1.0:
            break
        try:
            async with websockets.connect(
                AISSTREAM_URL,
                compression="deflate",  # Enables permessage-deflate extension
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps(subscribe_payload))
                logger.info("Subscribed to AISStream PositionReport feed.")

                while True:
                    wait_for = deadline - loop.time()
                    if wait_for <= 0:
                        break
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=wait_for)
                    except asyncio.TimeoutError:
                        break

                    if isinstance(raw_msg, bytes):
                        raw_msg = raw_msg.decode("utf-8", errors="replace")

                    msg = json.loads(raw_msg)
                    if "error" in msg or "Error" in msg:
                        err_detail = msg.get("error") or msg.get("Error")
                        raise RuntimeError(f"AISStream server error: {err_detail}")

                    msg_type = msg.get("MessageType")
                    if msg_type not in {
                        "PositionReport",
                        "StandardClassBPositionReport",
                        "ExtendedClassBPositionReport",
                    }:
                        continue

                    meta = msg.get("MetaData") or {}
                    pr = (msg.get("Message") or {}).get(msg_type) or {}

                    mmsi_val = str(pr.get("UserID") or meta.get("MMSI") or "").strip()
                    if mmsi_val not in mmsi_to_vessel:
                        continue

                    lat = pr.get("Latitude", meta.get("latitude"))
                    lon = pr.get("Longitude", meta.get("longitude"))
                    if lat is None or lon is None:
                        continue
                    lat_f, lon_f = float(lat), float(lon)
                    # Reject AIS 91/181 'unavailable' or zeroed coordinates
                    if abs(lat_f) > 90.0 or abs(lon_f) > 180.0 or (lat_f == 0.0 and lon_f == 0.0):
                        continue

                    sog_raw = pr.get("Sog")
                    sog = round(float(sog_raw), 1) if sog_raw is not None and float(sog_raw) < 102.2 else 0.0

                    cog_raw = pr.get("Cog")
                    cog = (
                        round(float(cog_raw) % 360.0, 1)
                        if cog_raw is not None and 0.0 <= float(cog_raw) < 360.0
                        else 0.0
                    )

                    hdg_raw = pr.get("TrueHeading")
                    heading = (
                        int(hdg_raw)
                        if hdg_raw is not None and 0 <= int(hdg_raw) < 360
                        else None
                    )

                    nav_code = int(pr.get("NavigationalStatus", 15))
                    nav_status = NAV_STATUS_MAP.get(nav_code, "Undefined")
                    ts_utc = parse_aisstream_timestamp(meta.get("time_utc"))

                    vessel = mmsi_to_vessel[mmsi_val]
                    updates[mmsi_val] = {
                        "mmsi": mmsi_val,
                        "vessel_id": vessel["id"],
                        "lat": round(lat_f, 5),
                        "lon": round(lon_f, 5),
                        "sog": sog,
                        "cog": cog,
                        "heading": heading,
                        "nav_status": nav_status,
                        "timestamp_utc": ts_utc,
                    }
                    logger.info(
                        "AIS rx: %s (%s) @ %.4f, %.4f | SOG=%.1f kt | COG=%.1f°",
                        vessel["name"],
                        mmsi_val,
                        lat_f,
                        lon_f,
                        sog,
                        cog,
                    )
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            raise
        except Exception as exc:
            left = deadline - loop.time()
            if left <= 3.0:
                logger.warning("Stream ended near window completion: %s", exc)
                break
            logger.warning("WebSocket interrupted (%s); reconnecting (%.0fs left)...", exc, left)
            await asyncio.sleep(min(3.0, left))

    return updates


def generate_mock_positions(vessels: list[dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    """
    Generate realistic maritime mock telemetry across Atlantic (Halifax/PEI/NL),
    Central (Great Lakes/St. Lawrence), and Western (Vancouver Island/BC) waters.
    Exercises all 3 status tiers:
      - Active / Recent (< 2h): underway patrols + active moored vessels
      - Last Known (2h - 7d): coastal patrols & station moorings
      - Home Port Fallback (> 7d or no AIS record)
    """
    # Explicit offshore/channel patrol scenarios keyed by vessel id
    scenarios: dict[str, dict[str, Any]] = {
        # --- ACTIVE / RECENT (< 2 hours) ---
        # Atlantic: Underway near Halifax Harbour approaches, Northumberland Strait, Gulf of St. Lawrence
        "ccgs-cape-spry": {
            "lat": 44.4820, "lon": -63.4650, "sog": 19.4, "cog": 158.0, "heading": 160,
            "nav_status": "Under way using engine", "age_minutes": 6,
        },
        "ccgs-cap-nord": {
            "lat": 46.2980, "lon": -63.8420, "sog": 16.2, "cog": 245.0, "heading": 244,
            "nav_status": "Under way using engine", "age_minutes": 14,
        },
        "ccgs-cape-norman": {
            "lat": 50.7450, "lon": -57.4410, "sog": 21.0, "cog": 312.0, "heading": 310,
            "nav_status": "Under way using engine", "age_minutes": 22,
        },
        # Central: Underway on Lake Ontario, Lake Huron/Georgian Bay, Lake Erie, St. Lawrence Estuary
        "ccgs-cape-storm": {
            "lat": 43.3140, "lon": -79.1980, "sog": 18.7, "cog": 38.0, "heading": 40,
            "nav_status": "Under way using engine", "age_minutes": 8,
        },
        "ccgs-cape-hearne": {
            "lat": 44.1620, "lon": -76.5350, "sog": 14.5, "cog": 215.0, "heading": 213,
            "nav_status": "Under way using engine", "age_minutes": 19,
        },
        "ccgs-cape-commodore": {
            "lat": 45.3080, "lon": -81.7140, "sog": 22.1, "cog": 330.0, "heading": 328,
            "nav_status": "Under way using engine", "age_minutes": 11,
        },
        "ccgs-cap-d-espoir": {
            "lat": 48.1180, "lon": -69.6720, "sog": 15.8, "cog": 64.0, "heading": 65,
            "nav_status": "Under way using engine", "age_minutes": 27,
        },
        "ccgs-cape-lambton": {
            "lat": 42.9980, "lon": -82.4290, "sog": 0.0, "cog": 182.0, "heading": 182,
            "nav_status": "Moored", "age_minutes": 34,
        },
        # Western: Underway around Vancouver Island (Tofino, Victoria/Juan de Fuca, Bamfield, Campbell River)
        "ccgs-cape-ann": {
            "lat": 49.0860, "lon": -125.9840, "sog": 20.6, "cog": 232.0, "heading": 230,
            "nav_status": "Under way using engine", "age_minutes": 4,
        },
        "ccgs-cape-calvert": {
            "lat": 48.3740, "lon": -123.4310, "sog": 17.9, "cog": 262.0, "heading": 260,
            "nav_status": "Under way using engine", "age_minutes": 9,
        },
        "ccgs-cape-mckay": {
            "lat": 48.8580, "lon": -125.2120, "sog": 13.4, "cog": 295.0, "heading": 296,
            "nav_status": "Under way using engine", "age_minutes": 16,
        },
        "ccgs-cape-dauphin": {
            "lat": 50.0410, "lon": -125.2290, "sog": 11.2, "cog": 165.0, "heading": 164,
            "nav_status": "Under way using engine", "age_minutes": 42,
        },
        "ccgs-cape-st-james": {
            "lat": 49.2890, "lon": -123.1920, "sog": 0.1, "cog": 90.0, "heading": 90,
            "nav_status": "Moored", "age_minutes": 55,
        },

        # --- LAST KNOWN (2 hours to 7 days) ---
        "ccgs-cape-fox": {
            "lat": 49.1180, "lon": -58.4120, "sog": 8.4, "cog": 280.0, "heading": 278,
            "nav_status": "Under way using engine", "age_minutes": 60 * 4 + 15,  # ~4h ago
        },
        "ccgs-cap-rozier": {
            "lat": 49.0120, "lon": -64.3550, "sog": 0.0, "cog": 45.0, "heading": None,
            "nav_status": "Moored", "age_minutes": 60 * 7 + 30,  # ~7h ago
        },
        "ccgs-cap-tourmente": {
            "lat": 48.4920, "lon": -68.5320, "sog": 0.0, "cog": 310.0, "heading": 310,
            "nav_status": "Moored", "age_minutes": 60 * 14,  # 14h ago
        },
        "ccgs-cape-discovery": {
            "lat": 43.7510, "lon": -81.7520, "sog": 10.5, "cog": 195.0, "heading": 195,
            "nav_status": "Under way using engine", "age_minutes": 60 * 19,  # 19h ago
        },
        "ccgs-cape-dundas": {
            "lat": 42.0620, "lon": -83.1280, "sog": 0.0, "cog": 175.0, "heading": None,
            "nav_status": "Moored", "age_minutes": 60 * 28,  # 1d 4h ago
        },
        "ccgs-cape-edensaw": {
            "lat": 43.3010, "lon": -79.7950, "sog": 0.0, "cog": 120.0, "heading": 120,
            "nav_status": "Moored", "age_minutes": 60 * 39,  # 1d 15h ago
        },
        "ccgs-cape-rescue": {
            "lat": 42.7690, "lon": -80.1980, "sog": 6.2, "cog": 148.0, "heading": 150,
            "nav_status": "Under way using engine", "age_minutes": 60 * 52,  # 2d 4h ago
        },
        "ccgs-cape-mercy": {
            "lat": 43.9480, "lon": -78.1650, "sog": 0.0, "cog": 0.0, "heading": None,
            "nav_status": "Moored", "age_minutes": 60 * 76,  # 3d 4h ago
        },
        "ccgs-cape-caution": {
            "lat": 49.8740, "lon": -124.5680, "sog": 0.0, "cog": 212.0, "heading": 212,
            "nav_status": "Moored", "age_minutes": 60 * 5 + 20,  # 5h ago
        },
        "ccgs-cape-cockburn": {
            "lat": 49.3550, "lon": -124.3510, "sog": 0.1, "cog": 32.0, "heading": 32,
            "nav_status": "Moored", "age_minutes": 60 * 26,  # 1d 2h ago
        },
        "ccgs-cape-kuper": {
            "lat": 48.8510, "lon": -123.4890, "sog": 9.8, "cog": 134.0, "heading": 135,
            "nav_status": "Under way using engine", "age_minutes": 60 * 64,  # 2d 16h ago
        },
        "ccgs-cape-sutil": {
            "lat": 50.7280, "lon": -127.4820, "sog": 0.0, "cog": 15.0, "heading": None,
            "nav_status": "Moored", "age_minutes": 60 * 125,  # 5d 5h ago
        },

        # --- STALE (> 7 days -> triggers Home Port Fallback) ---
        # Remaining vessels have no entry in positions.json (also triggering Home Port Fallback)
        "ccgs-cape-chaillon": {
            "lat": 48.4192, "lon": -89.2189, "sog": 0.0, "cog": 0.0, "heading": None,
            "nav_status": "Moored", "age_minutes": 60 * 24 * 11,  # 11d ago (winter layup)
        },
        "ccgs-cape-palmerston": {
            "lat": 54.3122, "lon": -130.3275, "sog": 0.0, "cog": 0.0, "heading": None,
            "nav_status": "Moored", "age_minutes": 60 * 24 * 16,  # 16d ago
        },
        "ccgs-cap-aupaluk": {
            "lat": 46.8169, "lon": -71.2003, "sog": 0.0, "cog": 0.0, "heading": None,
            "nav_status": "Moored", "age_minutes": 60 * 24 * 21,  # 21d ago
        },
    }

    mock_positions: dict[str, dict[str, Any]] = {}
    for v in vessels:
        vid = v["id"]
        mmsi = v.get("mmsi")
        if not mmsi or vid not in scenarios:
            continue
        sc = scenarios[vid]
        ts = now - timedelta(minutes=sc["age_minutes"])
        mock_positions[mmsi] = {
            "mmsi": mmsi,
            "vessel_id": vid,
            "lat": sc["lat"],
            "lon": sc["lon"],
            "sog": sc["sog"],
            "cog": sc["cog"],
            "heading": sc["heading"],
            "nav_status": sc["nav_status"],
            "timestamp_utc": iso_utc(ts),
        }

    return mock_positions


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Update CCG Cape Class AIS positions cache.")
    parser.add_argument(
        "--duration",
        type=int,
        default=90,
        help="Bounded WebSocket listening window in seconds (default: 90).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Generate realistic simulated positions across Canadian waters for offline/UI testing.",
    )
    parser.add_argument(
        "--vessels",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "vessels.json",
        help="Path to vessels.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "positions.json",
        help="Path to persistent positions.json cache",
    )
    args = parser.parse_args()

    vessels = load_fleet(args.vessels)
    mmsi_to_vessel = {v["mmsi"]: v for v in vessels if v.get("mmsi")}

    # CRITICAL: Always load existing cache first so silent/out-of-range vessels retain last known position
    cache = load_existing_positions(args.output)
    merged_positions: dict[str, Any] = dict(cache.get("positions", {}))
    now = datetime.now(timezone.utc)

    if args.mock:
        logger.info("Running in --mock mode: generating simulated maritime positions...")
        updates = generate_mock_positions(vessels, now)
        source = "mock"
    else:
        api_key = os.environ.get("AISSTREAM_API_KEY", "").strip()
        if not api_key:
            logger.error(
                "AISSTREAM_API_KEY is not set. Export AISSTREAM_API_KEY, add it to .env, "
                "or run with --mock for local testing."
            )
            return 1
        updates = asyncio.run(collect_live_ais(api_key, mmsi_to_vessel, args.duration))
        source = "aisstream"
        # Do not carry simulated --mock coordinates into live production AIS cache
        if cache.get("source") == "mock":
            logger.info("Clearing prior --mock simulation cache before storing live AIS data.")
            merged_positions = {}

    # Merge only vessels that reported during this run; never delete older cached entries
    for mmsi, report in updates.items():
        merged_positions[mmsi] = report

    # Sort keys deterministically for clean git diffs
    sorted_positions = {k: merged_positions[k] for k in sorted(merged_positions.keys())}

    output_payload = {
        "updated_at_utc": iso_utc(now),
        "source": source,
        "positions": sorted_positions,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output_payload, indent=2) + "\n", encoding="utf-8")

    logger.info(
        "Updated %d vessel(s) this run | Total cached in %s: %d / %d fleet MMSIs.",
        len(updates),
        args.output,
        len(sorted_positions),
        len(mmsi_to_vessel),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
