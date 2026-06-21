"""Build overlapping regional OSM and GTFS inputs for R5.

Run from the repository root:

    py -m travel_time_engine.build_regions
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
import zipfile
from contextlib import ExitStack
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "data" / "norway"
DEFAULT_DESTINATION = ROOT / "data" / "regions"

# Core rectangles cover mainland Norway without relying on a city list. Data
# rectangles overlap by roughly 200--300 km so a 60-minute search cannot reach
# the edge of its R5 graph in normal operation. Bounds are west,south,east,north.
REGIONS = (
    # Frequently used urban corridors get compact priority shards. They are
    # listed before the broad coverage regions because manifest order decides
    # which overlapping core is selected.
    (
        "oslo",
        "Oslo-regionen",
        (10.0, 59.55, 11.5, 60.3),
        (9.0, 58.8, 12.5, 60.9),
    ),
    (
        "trondheim",
        "Trondheim-regionen",
        (9.5, 63.0, 11.7, 64.15),
        (8.2, 62.3, 13.2, 64.8),
    ),
    ("south-west", "Sørvestlandet", (4.0, 57.5, 7.5, 62.0), (3.0, 56.5, 10.5, 64.0)),
    ("south-east", "Østlandet og Sørlandet", (7.5, 57.5, 13.0, 62.0), (4.5, 56.5, 15.5, 64.0)),
    ("central-west", "Vestlandet og Møre", (4.0, 62.0, 9.0, 65.0), (3.0, 60.0, 12.0, 67.0)),
    ("central-east", "Trøndelag", (9.0, 62.0, 15.0, 65.0), (6.0, 60.0, 18.0, 67.0)),
    ("nordland", "Nordland", (8.0, 65.0, 18.0, 68.0), (5.0, 63.0, 22.0, 70.0)),
    ("troms", "Troms og Lofoten", (10.0, 68.0, 22.0, 71.5), (7.0, 66.0, 26.0, 72.0)),
    ("finnmark", "Finnmark", (22.0, 68.0, 32.0, 71.5), (18.0, 66.0, 33.5, 72.0)),
)


def _inside(bounds, longitude, latitude):
    west, south, east, north = bounds
    return west <= longitude <= east and south <= latitude <= north


def _reader(archive, name):
    binary = archive.open(name)
    text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
    return binary, text, csv.DictReader(text)


def _writers(archives, name, fieldnames, stack):
    result = []
    for archive in archives:
        binary = stack.enter_context(archive.open(name, "w"))
        text = stack.enter_context(
            io.TextIOWrapper(binary, encoding="utf-8", newline="", write_through=True)
        )
        writer = csv.DictWriter(text, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        result.append(writer)
    return result


def _copy_filtered(source, outputs, name, mask_for_row):
    if name not in source.namelist():
        return

    binary, text, reader = _reader(source, name)
    try:
        with ExitStack() as stack:
            writers = _writers(outputs, name, reader.fieldnames, stack)
            for row in reader:
                mask = mask_for_row(row)
                for index, writer in enumerate(writers):
                    if mask & (1 << index):
                        writer.writerow(row)
    finally:
        text.close()
        binary.close()


def build_gtfs(source_path: Path, output_paths: list[Path], regions=REGIONS):
    """Spatially filter GTFS while retaining complete trips and references."""

    print("Reading stops and assigning trips to buffered regions...", flush=True)
    with zipfile.ZipFile(source_path) as source:
        stop_region_mask = {}
        binary, text, reader = _reader(source, "stops.txt")
        try:
            for row in reader:
                try:
                    longitude = float(row["stop_lon"])
                    latitude = float(row["stop_lat"])
                except (KeyError, ValueError):
                    continue
                mask = 0
                for index, (_, _, _, data_bounds) in enumerate(regions):
                    if _inside(data_bounds, longitude, latitude):
                        mask |= 1 << index
                stop_region_mask[row["stop_id"]] = mask
        finally:
            text.close()
            binary.close()

        trip_mask = {}
        binary, text, reader = _reader(source, "stop_times.txt")
        try:
            for row in reader:
                mask = stop_region_mask.get(row["stop_id"], 0)
                if mask:
                    trip_id = row["trip_id"]
                    trip_mask[trip_id] = trip_mask.get(trip_id, 0) | mask
        finally:
            text.close()
            binary.close()

        output_paths = list(output_paths)
        for path in output_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)

        with ExitStack() as stack:
            outputs = [
                stack.enter_context(
                    zipfile.ZipFile(
                        path,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                        compresslevel=6,
                        allowZip64=True,
                    )
                )
                for path in output_paths
            ]

            print("Writing regional stop times...", flush=True)
            included_stop_mask = {}
            binary, text, reader = _reader(source, "stop_times.txt")
            try:
                with ExitStack() as writer_stack:
                    writers = _writers(
                        outputs, "stop_times.txt", reader.fieldnames, writer_stack
                    )
                    for row in reader:
                        mask = trip_mask.get(row["trip_id"], 0)
                        if not mask:
                            continue
                        stop_id = row["stop_id"]
                        included_stop_mask[stop_id] = (
                            included_stop_mask.get(stop_id, 0) | mask
                        )
                        for index, writer in enumerate(writers):
                            if mask & (1 << index):
                                writer.writerow(row)
            finally:
                text.close()
                binary.close()

            route_mask = {}
            service_mask = {}

            def trip_filter(row):
                mask = trip_mask.get(row["trip_id"], 0)
                if mask:
                    route_id = row["route_id"]
                    service_id = row["service_id"]
                    route_mask[route_id] = route_mask.get(route_id, 0) | mask
                    service_mask[service_id] = service_mask.get(service_id, 0) | mask
                return mask

            _copy_filtered(source, outputs, "trips.txt", trip_filter)
            _copy_filtered(
                source,
                outputs,
                "stops.txt",
                lambda row: included_stop_mask.get(row["stop_id"], 0),
            )

            agency_mask = {}

            def route_filter(row):
                mask = route_mask.get(row["route_id"], 0)
                agency_id = row.get("agency_id")
                if mask and agency_id:
                    agency_mask[agency_id] = agency_mask.get(agency_id, 0) | mask
                return mask

            _copy_filtered(source, outputs, "routes.txt", route_filter)
            all_regions = (1 << len(regions)) - 1
            _copy_filtered(
                source,
                outputs,
                "agency.txt",
                lambda row: agency_mask.get(row.get("agency_id"), all_regions),
            )
            for name in ("calendar.txt", "calendar_dates.txt"):
                _copy_filtered(
                    source,
                    outputs,
                    name,
                    lambda row: service_mask.get(row["service_id"], 0),
                )
            _copy_filtered(
                source,
                outputs,
                "frequencies.txt",
                lambda row: trip_mask.get(row["trip_id"], 0),
            )
            _copy_filtered(
                source,
                outputs,
                "transfers.txt",
                lambda row: included_stop_mask.get(row["from_stop_id"], 0)
                & included_stop_mask.get(row["to_stop_id"], 0),
            )
            _copy_filtered(
                source, outputs, "feed_info.txt", lambda row: all_regions
            )

    print("GTFS regions complete (shapes.txt intentionally omitted).", flush=True)


def build_osm(source_path: Path, destination: Path, bounds):
    osmium = shutil.which("osmium")
    if not osmium:
        raise RuntimeError("osmium-tool is required to build regional OSM extracts")
    if sys.platform == "win32":
        # osmium dispatches subcommands from argv[0] and treats an upper-case
        # .EXE suffix returned by shutil.which as an unexpected command name.
        osmium = str(Path(osmium).with_suffix(".exe"))

    destination.parent.mkdir(parents=True, exist_ok=True)
    west, south, east, north = bounds
    subprocess.run(
        [
            osmium,
            "extract",
            "--bbox",
            f"{west},{south},{east},{north}",
            "--strategy",
            "complete_ways",
            "--overwrite",
            "--output",
            str(destination),
            str(source_path),
        ],
        check=True,
    )


def manifest():
    return {
        "version": 1,
        "regions": [
            {
                "id": region_id,
                "name": name,
                "directory": region_id,
                "core_bounds": list(core),
                "data_bounds": list(data),
            }
            for region_id, name, core, data in REGIONS
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--skip-osm", action="store_true")
    parser.add_argument("--skip-gtfs", action="store_true")
    parser.add_argument(
        "--region",
        action="append",
        choices=[region_id for region_id, *_ in REGIONS],
        help="Build only this region; may be repeated",
    )
    arguments = parser.parse_args(argv)
    selected_regions = tuple(
        region
        for region in REGIONS
        if not arguments.region or region[0] in arguments.region
    )

    osm_sources = sorted(arguments.source.glob("*.osm.pbf"))
    gtfs_sources = sorted(arguments.source.glob("*.zip"))
    if not arguments.skip_osm and not osm_sources:
        parser.error(f"No .osm.pbf found in {arguments.source}")
    if not arguments.skip_gtfs and not gtfs_sources:
        parser.error(f"No GTFS .zip found in {arguments.source}")

    arguments.destination.mkdir(parents=True, exist_ok=True)
    if not arguments.skip_osm:
        for region_id, name, _, data_bounds in selected_regions:
            print(f"Extracting OSM for {name}...", flush=True)
            build_osm(
                osm_sources[0],
                arguments.destination / region_id / f"{region_id}.osm.pbf",
                data_bounds,
            )

    if not arguments.skip_gtfs:
        build_gtfs(
            gtfs_sources[0],
            [
                arguments.destination / region_id / f"{region_id}-gtfs.zip"
                for region_id, *_ in selected_regions
            ],
            selected_regions,
        )

    manifest_path = arguments.destination / "regions.json"
    manifest_path.write_text(
        json.dumps(manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {manifest_path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
