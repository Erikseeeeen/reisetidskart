"""Travel-time grid calculation backed by R5 via r5py."""

from __future__ import annotations

import datetime as dt
import functools
import gc
import json
import math
import os
import sys
import threading
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo


DATA_DIRECTORY_ENVIRONMENT_VARIABLE = "R5_DATA_DIR"
DEPARTURE_TIME_ENVIRONMENT_VARIABLE = "R5_DEPARTURE_TIME"
DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
DEFAULT_REGIONS_DIRECTORY = DATA_ROOT / "regions"
DEFAULT_DATA_DIRECTORY = DATA_ROOT
DEFAULT_R5_JAR = DATA_ROOT / "r5-norway.jar"
REGIONS_MANIFEST = "regions.json"
DEFAULT_TIME_ZONE = "Europe/Oslo"
DEFAULT_DEPARTURE_HOUR = 8
MAX_TRAVEL_TIME_MINUTES = 60
ROUTING_HORIZON_MINUTES = 70
UNREACHABLE_MINUTES = MAX_TRAVEL_TIME_MINUTES + 1
# Route a 200x150 grid for the 4:3 browser raster, then upsample. Asking R5
# for all 76,800 display pixels roughly doubles latency without a visible gain.
MAX_ROUTING_CELLS = 30_000
JUST_IN_TIME_PERCENTILE = 1
_R5_LOCK = threading.Lock()
_TRANSPORT_NETWORK = None
_TRANSPORT_NETWORK_DIRECTORY = None


class RoutingCancelled(Exception):
    """Raised when a newer browser request supersedes queued routing work."""


def _mercator_y(latitude: float) -> float:
    """Return Web Mercator Y for a latitude in degrees."""

    radians = math.radians(latitude)
    return math.log(math.tan(math.pi / 4 + radians / 2))


def _inverse_mercator_y(y: float) -> float:
    """Return latitude in degrees from Web Mercator Y."""

    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def _configure_java_runtime() -> None:
    """On Windows, point JPype at an installed JDK 21 when possible."""

    if os.name != "nt":
        return

    configured = os.environ.get("R5_JAVA_HOME") or os.environ.get("JAVA_HOME")
    candidates = [Path(configured)] if configured else []
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))

    for root in (
        program_files / "Eclipse Adoptium",
        program_files / "Microsoft",
        program_files / "Java",
    ):
        candidates.extend(sorted(root.glob("jdk-21*"), reverse=True))

    for candidate in candidates:
        jvm = candidate / "bin" / "server" / "jvm.dll"

        if jvm.is_file() and candidate.name.lower().startswith("jdk-21"):
            os.environ["JAVA_HOME"] = str(candidate)

            java_bin = str(candidate / "bin")
            path_entries = os.environ.get("PATH", "").split(os.pathsep)

            if java_bin not in path_entries:
                os.environ["PATH"] = os.pathsep.join([java_bin, *path_entries])

            return


def _routing_libraries():
    """Import the heavyweight routing stack only when it is needed."""

    _configure_java_runtime()
    _configure_r5_classpath()

    try:
        import geopandas
        import r5py
        import shapely
    except ImportError as error:
        raise RuntimeError(
            "The R5 travel-time engine is not installed. "
            "Run 'py -m pip install -r requirements.txt'."
        ) from error
    except Exception as error:
        if "jvm" in f"{type(error).__name__}: {error}".lower():
            raise RuntimeError(
                "r5py could not start Java. Install JDK 21 and set JAVA_HOME "
                "or R5_JAVA_HOME to its installation directory."
            ) from error

        raise

    return geopandas, r5py, shapely


def _configure_r5_classpath() -> None:
    """Use the locally patched R5 jar when it exists."""

    if not DEFAULT_R5_JAR.is_file():
        return

    option = "--r5-classpath"
    classpath = str(DEFAULT_R5_JAR)

    if option in sys.argv:
        return

    sys.argv.extend([option, classpath])


def _set_java_context_class_loader() -> None:
    """Let Java worker threads spawned from Python request threads see R5."""

    try:
        import jpype

        if not jpype.isJVMStarted():
            return

        thread = jpype.JClass("java.lang.Thread")
        class_loader = jpype.JClass("java.lang.ClassLoader")
        thread.currentThread().setContextClassLoader(
            class_loader.getSystemClassLoader()
        )
    except (ImportError, RuntimeError):
        pass


def _data_directory() -> Path:
    configured = os.environ.get(DATA_DIRECTORY_ENVIRONMENT_VARIABLE)
    if configured:
        path = Path(configured).expanduser()
    elif (
        DEFAULT_R5_JAR.is_file()
        and any(DEFAULT_DATA_DIRECTORY.glob("*.osm.pbf"))
        and any(DEFAULT_DATA_DIRECTORY.glob("*.zip"))
    ):
        path = DEFAULT_DATA_DIRECTORY
    elif (DEFAULT_REGIONS_DIRECTORY / REGIONS_MANIFEST).is_file():
        path = DEFAULT_REGIONS_DIRECTORY
    else:
        path = DEFAULT_DATA_DIRECTORY
    path = path.resolve()

    if not path.is_dir():
        raise FileNotFoundError(
            f"R5 data directory does not exist: {path}. Set "
            f"{DATA_DIRECTORY_ENVIRONMENT_VARIABLE} to a directory containing "
            "one .osm.pbf file and one or more GTFS .zip files."
        )

    return path


def _contains(bounds, longitude: float, latitude: float) -> bool:
    west, south, east, north = bounds
    return west <= longitude <= east and south <= latitude <= north


@functools.lru_cache(maxsize=4)
def _load_regions(data_directory: Path) -> tuple[dict, ...]:
    """Load a routing-region manifest, or wrap a legacy directory."""

    manifest_path = data_directory / REGIONS_MANIFEST

    if not manifest_path.is_file():
        return (
            {
                "id": data_directory.name,
                "name": data_directory.name,
                "core_bounds": [-180, -90, 180, 90],
                "data_bounds": [-180, -90, 180, 90],
                "directory": data_directory,
            },
        )

    with manifest_path.open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)

    regions = []
    for item in manifest.get("regions", []):
        region_id = item["id"]
        core_bounds = item["core_bounds"]
        data_bounds = item["data_bounds"]

        if len(core_bounds) != 4 or len(data_bounds) != 4:
            raise ValueError(f"Region {region_id!r} must have four-value bounds")

        region_directory = (data_directory / item.get("directory", region_id)).resolve()
        if not region_directory.is_dir():
            raise FileNotFoundError(
                f"Routing data for region {region_id!r} does not exist: "
                f"{region_directory}. Run 'py -m travel_time_engine.build_regions'."
            )

        regions.append(
            {
                "id": region_id,
                "name": item.get("name", region_id),
                "core_bounds": core_bounds,
                "data_bounds": data_bounds,
                "directory": region_directory,
            }
        )

    if not regions:
        raise ValueError(f"No regions are defined in {manifest_path}")

    return tuple(regions)


def _region_for_origin(data_directory: Path, origin) -> dict:
    longitude = float(origin["lng"])
    latitude = float(origin["lat"])

    for region in _load_regions(data_directory):
        if _contains(region["core_bounds"], longitude, latitude):
            return region

    raise ValueError(
        f"No routing region covers {latitude:.5f}, {longitude:.5f}. "
        f"Check {data_directory / REGIONS_MANIFEST}."
    )


def _load_transport_network(data_directory: Path):
    """Keep one R5 region resident, releasing it before loading another."""

    global _TRANSPORT_NETWORK, _TRANSPORT_NETWORK_DIRECTORY

    if (
        _TRANSPORT_NETWORK is not None
        and _TRANSPORT_NETWORK_DIRECTORY == data_directory
    ):
        return _TRANSPORT_NETWORK

    # functools.lru_cache builds a replacement before evicting the old value,
    # briefly requiring enough RAM for two R5 networks. Drop the old Java
    # object first, which matters on the 16 GiB target machine.
    _TRANSPORT_NETWORK = None
    _TRANSPORT_NETWORK_DIRECTORY = None
    gc.collect()

    try:
        import jpype

        if jpype.isJVMStarted():
            jpype.JClass("java.lang.System").gc()
    except (ImportError, RuntimeError):
        pass

    _, r5py, _ = _routing_libraries()
    _set_java_context_class_loader()
    osm_files = sorted(data_directory.glob("*.osm.pbf"))
    gtfs_files = sorted(data_directory.glob("*.zip"))

    if not osm_files:
        raise FileNotFoundError(f"No .osm.pbf file found in {data_directory}")

    if not gtfs_files:
        raise FileNotFoundError(f"No GTFS .zip file found in {data_directory}")

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="R5 reported the following issues with GTFS file.*",
            category=RuntimeWarning,
        )

        network = r5py.TransportNetwork(
            osm_files[0],
            gtfs_files,
            allow_errors=True,
        )

    _TRANSPORT_NETWORK = network
    _TRANSPORT_NETWORK_DIRECTORY = data_directory
    return network


def _parse_departure_time(value: dt.datetime | str | None) -> dt.datetime:
    """Return a naive Oslo wall-clock datetime, as expected by R5."""

    if value is None:
        value = os.environ.get(DEPARTURE_TIME_ENVIRONMENT_VARIABLE)

    if value is None:
        return dt.datetime.now(ZoneInfo(DEFAULT_TIME_ZONE)).replace(
            hour=DEFAULT_DEPARTURE_HOUR,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=None,
        )

    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                "departure_time must be an ISO 8601 date and time"
            ) from error

    if not isinstance(value, dt.datetime):
        raise TypeError("departure_time must be a datetime, ISO 8601 string, or None")

    if value.tzinfo is not None:
        value = value.astimezone(ZoneInfo(DEFAULT_TIME_ZONE)).replace(tzinfo=None)

    return value


def _transport_modes(r5py, mode: str):
    normalized = mode.strip().lower().replace("-", "_")

    if normalized in {"public_transport", "transit"}:
        return [r5py.TransportMode.TRANSIT, r5py.TransportMode.WALK]

    if normalized in {"walk", "walking"}:
        return [r5py.TransportMode.WALK]

    if normalized in {"bicycle", "cycling"}:
        return [r5py.TransportMode.BICYCLE]

    if normalized in {"car", "driving"}:
        return [r5py.TransportMode.CAR]

    raise ValueError(f"Unsupported transport mode: {mode}")


def _routing_dimensions(width: int, height: int) -> tuple[int, int]:
    """Choose a manageable R5 grid while preserving the output aspect ratio."""

    if width * height <= MAX_ROUTING_CELLS:
        return width, height

    scale = math.sqrt(MAX_ROUTING_CELLS / (width * height))

    return max(2, round(width * scale)), max(2, round(height * scale))


def _routing_points(origin, bounds, width, height, geopandas, shapely):
    origins = geopandas.GeoDataFrame(
        {
            "id": ["origin"],
            "geometry": [shapely.Point(origin["lng"], origin["lat"])],
        },
        crs="EPSG:4326",
    )

    north_y = _mercator_y(bounds["north"])
    south_y = _mercator_y(bounds["south"])
    mercator_span = north_y - south_y
    longitude_span = bounds["east"] - bounds["west"]

    ids = []
    points = []

    for row in range(height):
        y = north_y - ((row + 0.5) / height) * mercator_span
        latitude = _inverse_mercator_y(y)

        for column in range(width):
            longitude = bounds["west"] + ((column + 0.5) / width) * longitude_span

            ids.append(str(row * width + column))
            points.append(shapely.Point(longitude, latitude))

    destinations = geopandas.GeoDataFrame(
        {"id": ids, "geometry": points},
        crs="EPSG:4326",
    )

    return origins, destinations


def _matrix_minutes(travel_times, cell_count: int) -> list[float]:
    """Convert R5's sparse matrix to a row-major grid with NaN gaps."""

    minutes = [math.nan] * cell_count
    column = f"travel_time_p{JUST_IN_TIME_PERCENTILE}"

    for destination_id, travel_time in zip(
        travel_times["to_id"],
        travel_times[column],
    ):
        try:
            value = float(travel_time)
            index = int(destination_id)
        except (TypeError, ValueError):
            continue

        if 0 <= index < cell_count and math.isfinite(value):
            minutes[index] = max(0.0, value)

    return minutes


def _upsample_grid(
    source: list[float],
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> list[float]:
    """Bilinearly upsample finite values without spreading NaN gaps."""

    if (source_width, source_height) == (target_width, target_height):
        return list(source)

    result = [math.nan] * (target_width * target_height)

    for row in range(target_height):
        source_y = ((row + 0.5) * source_height / target_height) - 0.5
        source_y = min(source_height - 1, max(0.0, source_y))

        y0 = math.floor(source_y)
        y1 = min(y0 + 1, source_height - 1)
        y_weight = source_y - y0

        for column in range(target_width):
            source_x = ((column + 0.5) * source_width / target_width) - 0.5
            source_x = min(source_width - 1, max(0.0, source_x))

            x0 = math.floor(source_x)
            x1 = min(x0 + 1, source_width - 1)
            x_weight = source_x - x0

            samples = (
                (source[y0 * source_width + x0], (1 - x_weight) * (1 - y_weight)),
                (source[y0 * source_width + x1], x_weight * (1 - y_weight)),
                (source[y1 * source_width + x0], (1 - x_weight) * y_weight),
                (source[y1 * source_width + x1], x_weight * y_weight),
            )

            finite = [
                (value, weight)
                for value, weight in samples
                if math.isfinite(value)
            ]

            total_weight = sum(weight for _, weight in finite)

            if total_weight > 0:
                result[row * target_width + column] = sum(
                    value * weight for value, weight in finite
                ) / total_weight

    return result


def _finalize_grid(minutes, width, height):
    """Apply the 60-minute limit to final-resolution cells."""

    result = [UNREACHABLE_MINUTES] * (width * height)

    for index, value in enumerate(minutes):
        if math.isfinite(value) and value <= MAX_TRAVEL_TIME_MINUTES:
            result[index] = round(value, 3)

    return result


def compute_grid(
    origin,
    bounds,
    width,
    height,
    *,
    mode="public_transport",
    departure_time=None,
    transport_network=None,
    cancelled=None,
):
    """Return R5 travel times in north-west, row-major raster order."""

    cancelled = cancelled or (lambda: False)

    if cancelled():
        raise RoutingCancelled

    if not isinstance(width, int) or not isinstance(height, int):
        raise TypeError("width and height must be integers")

    if width < 2 or height < 2:
        raise ValueError("width and height must both be at least 2")

    geopandas, r5py, shapely = _routing_libraries()
    data_root = _data_directory()
    region = _region_for_origin(data_root, origin)
    data_directory = region["directory"]

    if cancelled():
        raise RoutingCancelled

    routing_width, routing_height = _routing_dimensions(width, height)

    origins, destinations = _routing_points(
        origin,
        bounds,
        routing_width,
        routing_height,
        geopandas,
        shapely,
    )
    departure = _parse_departure_time(departure_time)

    with _R5_LOCK:
        # Several HTTP threads may be waiting here. Only the newest request
        # from a browser should get to perform an expensive R5 calculation.
        if cancelled():
            raise RoutingCancelled

        _set_java_context_class_loader()

        if transport_network is None:
            transport_network = _load_transport_network(data_directory)

        if cancelled():
            raise RoutingCancelled

        travel_times = r5py.TravelTimeMatrix(
            transport_network,
            origins=origins,
            destinations=destinations,
            departure=departure,
            percentiles=[JUST_IN_TIME_PERCENTILE],
            transport_modes=_transport_modes(r5py, mode),
            max_time=dt.timedelta(minutes=ROUTING_HORIZON_MINUTES),
        )

    if cancelled():
        raise RoutingCancelled

    routed = _matrix_minutes(travel_times, routing_width * routing_height)

    upsampled = _upsample_grid(
        routed,
        routing_width,
        routing_height,
        width,
        height,
    )

    return _finalize_grid(
        upsampled,
        width,
        height,
    )
