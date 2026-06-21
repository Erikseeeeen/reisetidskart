# Reisetidskart

The browser displays a 60-minute travel-time raster calculated by R5. Routing
data is split into overlapping regions so all mainland Norwegian cities can be
supported without loading the full Norwegian OSM and Entur GTFS feeds into one
JVM. Compact priority regions keep Oslo and Trondheim responsive; broader
regions provide coverage elsewhere.

## Build the regional data

Place the national source files in `data/norway/`:

- one `*.osm.pbf` file;
- one Entur GTFS `*.zip` file.

Install `osmium-tool`, then run:

```powershell
py -m travel_time_engine.build_regions
```

This creates `data/regions/regions.json` and one directory per routing region.
The operation reads the large GTFS stop-times file twice and may take a while.
It is an offline data-preparation step, not part of server startup. Use
`--skip-osm` or `--skip-gtfs` to rebuild only one side.
Pass `--region trondheim` to rebuild only one region.

The GTFS filter keeps every complete trip that calls at a stop inside a
region's buffered data bounds. It also keeps referenced stops, routes,
agencies, services, frequencies and transfers. `shapes.txt` is deliberately
excluded: it is several gigabytes uncompressed and contains display geometry
that R5 does not require for routing.

## Run

```powershell
py -m pip install -r requirements.txt
py -m travel_time_engine.server
py -m http.server 8000
```

Then open <http://127.0.0.1:8000>. Zoom out to move around Norway; clicking a
city while zoomed out zooms into routing scale and calculates its raster.

When `data/regions/regions.json` exists it is selected automatically. Override
the location with `R5_DATA_DIR` if needed:

```powershell
$env:R5_DATA_DIR = "D:\routing-data\regions"
py -m travel_time_engine.server
```

Only one regional R5 network is retained at a time. Switching regions therefore
has a longer first request, but avoids the temporary two-network memory spike
that an ordinary least-recently-used cache would create on a 16 GB machine.
R5 also writes a compiled network cache under the user's local application-data
directory. The first request for a newly built region can take several minutes;
later server starts reuse that compiled cache.
