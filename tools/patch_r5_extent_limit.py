from __future__ import annotations

import os
import struct
import zipfile
from pathlib import Path


SOURCE_JAR = (
    Path(os.environ["LOCALAPPDATA"])
    / "r5py"
    / "r5-v7.5.1-r5py-all.jar"
)
DESTINATION_JAR = Path(__file__).resolve().parent.parent / "data" / "r5-norway.jar"
CLASS_NAME = "com/conveyal/r5/common/GeometryUtils.class"
OLD_LIMIT = 975_000.0
NEW_LIMIT = 10_000_000.0


def main() -> None:
    old_bytes = struct.pack(">d", OLD_LIMIT)
    new_bytes = struct.pack(">d", NEW_LIMIT)

    DESTINATION_JAR.parent.mkdir(parents=True, exist_ok=True)

    patched = False
    with zipfile.ZipFile(SOURCE_JAR, "r") as source:
        with zipfile.ZipFile(
            DESTINATION_JAR,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as destination:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == CLASS_NAME:
                    count = data.count(old_bytes)
                    if count != 1:
                        raise RuntimeError(
                            f"Expected one {OLD_LIMIT} constant in {CLASS_NAME}, "
                            f"found {count}"
                        )
                    data = data.replace(old_bytes, new_bytes)
                    patched = True
                destination.writestr(item, data)

    if not patched:
        raise RuntimeError(f"{CLASS_NAME} was not found in {SOURCE_JAR}")

    print(f"Wrote {DESTINATION_JAR}")
    print(f"Raised R5 WGS envelope area limit from {OLD_LIMIT:.0f} to {NEW_LIMIT:.0f} km2")


if __name__ == "__main__":
    main()
