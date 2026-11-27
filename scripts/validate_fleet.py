#!/usr/bin/env python3
"""
Validate data/vessels.json for schema compliance, Canadian coordinate bounds,
strict 47-ft Cape Class filtering, and duplicate/missing MMSIs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REQUIRED_TOP_KEYS = {
    "id",
    "name",
    "mmsi",
    "region",
    "home_port",
    "home_lat",
    "home_lon",
    "year_built",
    "specs",
    "equipment",
    "source_url",
}

VALID_REGIONS = {"Atlantic", "Central", "Western"}
REQUIRED_SPECS_KEYS = {"length_m", "max_speed_kt", "range_nm", "crew"}
REQUIRED_EQUIP_KEYS = {"propulsion", "navigation", "communications"}
EXCLUDED_NAMES = {
    "cape roger",
    "cape light",
    "cape hurd",
    "cap-aux-meules",
    "courtenay bay",
    "sambro",
    "westport",
}
MMSI_RE = re.compile(r"^316\d{6}$")


def validate_fleet(path: Path) -> int:
    if not path.exists():
        print(f"[ERROR] Fleet file not found: {path}", file=sys.stderr)
        return 1

    try:
        vessels = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON in {path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(vessels, list) or len(vessels) == 0:
        print("[ERROR] Root JSON element must be a non-empty array of vessels.", file=sys.stderr)
        return 1

    errors: list[str] = []
    warnings: list[str] = []

    if not (34 <= len(vessels) <= 38):
        warnings.append(f"Expected ~36 Cape Class vessels, found {len(vessels)}.")

    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_mmsis: dict[str, str] = {}
    missing_mmsis: list[str] = []
    region_counts = {r: 0 for r in sorted(VALID_REGIONS)}

    for idx, v in enumerate(vessels):
        prefix = f"Vessel[{idx}]"
        if not isinstance(v, dict):
            errors.append(f"{prefix}: Entry must be an object.")
            continue

        vid = v.get("id")
        vname = v.get("name", f"<unnamed-{idx}>")
        prefix = f"{vname} ({vid})"

        missing_keys = REQUIRED_TOP_KEYS - set(v.keys())
        if missing_keys:
            errors.append(f"{prefix}: Missing required keys: {sorted(missing_keys)}")
            continue

        # ID & Name checks
        if not isinstance(vid, str) or not vid.strip():
            errors.append(f"{prefix}: 'id' must be a non-empty string.")
        elif vid in seen_ids:
            errors.append(f"{prefix}: Duplicate vessel id '{vid}'.")
        else:
            seen_ids.add(vid)

        if not isinstance(vname, str) or not vname.strip():
            errors.append(f"{prefix}: 'name' must be a non-empty string.")
        else:
            norm_name = vname.lower().replace("ccgs ", "").strip()
            if norm_name in EXCLUDED_NAMES:
                errors.append(f"{prefix}: Excluded non-Cape-Class vessel '{vname}' present in dataset.")
            if vname in seen_names:
                errors.append(f"{prefix}: Duplicate vessel name '{vname}'.")
            seen_names.add(vname)

        # MMSI validation
        mmsi = v.get("mmsi")
        if mmsi is None:
            missing_mmsis.append(vname)
        elif not isinstance(mmsi, str) or not MMSI_RE.match(mmsi):
            errors.append(
                f"{prefix}: Invalid MMSI '{mmsi}' (must be null or 9-digit Canadian MMSI '316xxxxxx')."
            )
        else:
            if mmsi in seen_mmsis:
                errors.append(
                    f"{prefix}: Duplicate MMSI '{mmsi}' (already assigned to {seen_mmsis[mmsi]})."
                )
            else:
                seen_mmsis[mmsi] = vname

        # Region validation
        region = v.get("region")
        if region not in VALID_REGIONS:
            errors.append(f"{prefix}: Invalid region '{region}' (expected one of {sorted(VALID_REGIONS)}).")
        else:
            region_counts[region] += 1

        # Home port & Canadian maritime coordinate bounds
        home_port = v.get("home_port")
        if not isinstance(home_port, str) or not home_port.strip():
            errors.append(f"{prefix}: 'home_port' must be a non-empty string.")

        lat = v.get("home_lat")
        lon = v.get("home_lon")
        if not isinstance(lat, (int, float)) or isinstance(lat, bool) or not (41.5 <= float(lat) <= 75.0):
            errors.append(f"{prefix}: 'home_lat' {lat!r} out of Canadian bounds [41.5, 75.0].")
        if not isinstance(lon, (int, float)) or isinstance(lon, bool) or not (-135.0 <= float(lon) <= -52.0):
            errors.append(f"{prefix}: 'home_lon' {lon!r} out of Canadian bounds [-135.0, -52.0].")

        # Year built
        year_built = v.get("year_built")
        if not isinstance(year_built, int) or isinstance(year_built, bool) or not (1996 <= year_built <= 2015):
            errors.append(f"{prefix}: 'year_built' {year_built!r} outside Cape Class build years [1996, 2015].")

        # Specs
        specs = v.get("specs")
        if not isinstance(specs, dict):
            errors.append(f"{prefix}: 'specs' must be an object.")
        else:
            missing_specs = REQUIRED_SPECS_KEYS - set(specs.keys())
            if missing_specs:
                errors.append(f"{prefix}: 'specs' missing keys: {sorted(missing_specs)}")
            else:
                if float(specs["length_m"]) != 14.6:
                    errors.append(
                        f"{prefix}: 'specs.length_m' is {specs['length_m']}, expected 14.6 m for 47-ft Cape Class."
                    )
                if not isinstance(specs["max_speed_kt"], (int, float)) or specs["max_speed_kt"] <= 0:
                    errors.append(f"{prefix}: Invalid 'specs.max_speed_kt': {specs['max_speed_kt']!r}")
                if not isinstance(specs["range_nm"], (int, float)) or specs["range_nm"] <= 0:
                    errors.append(f"{prefix}: Invalid 'specs.range_nm': {specs['range_nm']!r}")
                if not isinstance(specs["crew"], int) or specs["crew"] <= 0:
                    errors.append(f"{prefix}: Invalid 'specs.crew': {specs['crew']!r}")

        # Equipment
        equip = v.get("equipment")
        if not isinstance(equip, dict):
            errors.append(f"{prefix}: 'equipment' must be an object.")
        else:
            missing_eq = REQUIRED_EQUIP_KEYS - set(equip.keys())
            if missing_eq:
                errors.append(f"{prefix}: 'equipment' missing keys: {sorted(missing_eq)}")
            else:
                for k in REQUIRED_EQUIP_KEYS:
                    items = equip[k]
                    if not isinstance(items, list) or not all(isinstance(x, str) and x.strip() for x in items):
                        errors.append(f"{prefix}: 'equipment.{k}' must be a list of non-empty strings.")
                if isinstance(equip.get("propulsion"), list) and len(equip["propulsion"]) == 0:
                    warnings.append(f"{prefix}: 'equipment.propulsion' is empty.")

        # Source URL
        source_url = v.get("source_url")
        if not isinstance(source_url, str) or not source_url.startswith(
            "https://inter-j02.dfo-mpo.gc.ca/fdat/vessels/"
        ):
            errors.append(f"{prefix}: 'source_url' must be an official FDAT vessel URL, got {source_url!r}.")

    print("=" * 64)
    print("CCG 47-ft Cape Class Fleet Validation Summary")
    print("=" * 64)
    print(f"File             : {path}")
    print(f"Total Vessels    : {len(vessels)}")
    print(
        f"By Region        : Atlantic={region_counts['Atlantic']}, "
        f"Central={region_counts['Central']}, Western={region_counts['Western']}"
    )
    print(f"Verified MMSIs   : {len(seen_mmsis)} / {len(vessels)}")
    print(f"Missing MMSIs    : {len(missing_mmsis)}")
    if missing_mmsis:
        print("  Unverified/Missing MMSI vessels (populate manually if available):")
        for name in missing_mmsis:
            print(f"    - {name}")

    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  [WARN] {w}")

    if errors:
        print(f"\nValidation FAILED with {len(errors)} error(s):", file=sys.stderr)
        for err in errors:
            print(f"  [ERROR] {err}", file=sys.stderr)
        return 1

    print("\n[OK] All schema, coordinate, class-filter, and MMSI checks passed!")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CCG Cape Class vessels.json registry.")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "vessels.json",
        help="Path to vessels.json (default: data/vessels.json)",
    )
    args = parser.parse_args()
    return validate_fleet(args.file)


if __name__ == "__main__":
    sys.exit(main())
