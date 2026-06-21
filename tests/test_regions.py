import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from travel_time_engine.build_regions import REGIONS, build_gtfs, manifest
from travel_time_engine.routing import (
    RoutingCancelled,
    _load_regions,
    _region_for_origin,
    compute_grid,
)
from travel_time_engine.server import _RequestTracker


def _csv_bytes(fieldnames, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


class RegionSelectionTest(unittest.TestCase):
    def test_major_cities_are_covered(self):
        cities = {
            "Oslo": (10.7528, 59.9111),
            "Kristiansand": (7.9956, 58.1467),
            "Stavanger": (5.7331, 58.9690),
            "Bergen": (5.3221, 60.3929),
            "Ålesund": (6.1495, 62.4722),
            "Trondheim": (10.3951, 63.4305),
            "Bodø": (14.4049, 67.2804),
            "Leknes": (13.6110, 68.1475),
            "Tromsø": (18.9553, 69.6492),
            "Alta": (23.2716, 69.9689),
            "Kirkenes": (30.0450, 69.7271),
        }
        for city, (longitude, latitude) in cities.items():
            with self.subTest(city=city):
                self.assertTrue(
                    any(
                        core[0] <= longitude <= core[2]
                        and core[1] <= latitude <= core[3]
                        for _, _, core, _ in REGIONS
                    )
                )

    def test_manifest_selects_region_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for item in manifest()["regions"]:
                (root / item["directory"]).mkdir()
            (root / "regions.json").write_text(
                json.dumps(manifest()), encoding="utf-8"
            )
            _load_regions.cache_clear()
            region = _region_for_origin(root, {"lng": 10.7528, "lat": 59.9111})
            self.assertEqual(region["id"], "oslo")
            region = _region_for_origin(root, {"lng": 10.3951, "lat": 63.4305})
            self.assertEqual(region["id"], "trondheim")


class CancellationTest(unittest.TestCase):
    def test_new_request_supersedes_queued_request(self):
        tracker = _RequestTracker()
        first = tracker.register("browser", 1)
        second = tracker.register("browser", 2)
        late_old_request = tracker.register("browser", 1)

        self.assertTrue(first())
        self.assertFalse(second())
        self.assertTrue(late_old_request())

    def test_cancelled_grid_does_not_import_or_start_r5(self):
        with self.assertRaises(RoutingCancelled):
            compute_grid(
                {"lat": 63.43, "lng": 10.39},
                {"south": 63, "west": 10, "north": 64, "east": 11},
                10,
                10,
                cancelled=lambda: True,
            )


class GtfsShardingTest(unittest.TestCase):
    def test_complete_trip_is_retained_without_shapes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.zip"
            outputs = [root / f"{region_id}.zip" for region_id, *_ in REGIONS]

            files = {
                "stops.txt": _csv_bytes(
                    ["stop_id", "stop_name", "stop_lat", "stop_lon"],
                    [
                        {"stop_id": "oslo", "stop_name": "Oslo", "stop_lat": "59.91", "stop_lon": "10.75"},
                        {"stop_id": "remote", "stop_name": "Remote", "stop_lat": "55", "stop_lon": "20"},
                    ],
                ),
                "stop_times.txt": _csv_bytes(
                    ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                    [
                        {"trip_id": "t1", "arrival_time": "08:00:00", "departure_time": "08:00:00", "stop_id": "oslo", "stop_sequence": "1"},
                        {"trip_id": "t1", "arrival_time": "12:00:00", "departure_time": "12:00:00", "stop_id": "remote", "stop_sequence": "2"},
                    ],
                ),
                "trips.txt": _csv_bytes(
                    ["route_id", "service_id", "trip_id", "shape_id"],
                    [{"route_id": "r1", "service_id": "s1", "trip_id": "t1", "shape_id": "shape1"}],
                ),
                "routes.txt": _csv_bytes(
                    ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
                    [{"route_id": "r1", "agency_id": "a1", "route_short_name": "1", "route_long_name": "Test", "route_type": "3"}],
                ),
                "agency.txt": _csv_bytes(
                    ["agency_id", "agency_name", "agency_url", "agency_timezone"],
                    [{"agency_id": "a1", "agency_name": "Test", "agency_url": "https://example.com", "agency_timezone": "Europe/Oslo"}],
                ),
                "calendar.txt": _csv_bytes(
                    ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"],
                    [{"service_id": "s1", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1", "start_date": "20260101", "end_date": "20261231"}],
                ),
                "shapes.txt": b"shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nshape1,59,10,1\n",
            }
            with zipfile.ZipFile(source, "w") as archive:
                for name, content in files.items():
                    archive.writestr(name, content)

            build_gtfs(source, outputs)
            south_east = outputs[
                next(
                    index
                    for index, (region_id, *_) in enumerate(REGIONS)
                    if region_id == "south-east"
                )
            ]
            with zipfile.ZipFile(south_east) as archive:
                self.assertNotIn("shapes.txt", archive.namelist())
                stops = archive.read("stops.txt").decode()
                self.assertIn("oslo", stops)
                self.assertIn("remote", stops)


if __name__ == "__main__":
    unittest.main()
