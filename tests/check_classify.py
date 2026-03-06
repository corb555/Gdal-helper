from __future__ import annotations

from pathlib import Path
from typing import Iterable, Set

import numpy as np
from pyproj import transform
import rasterio


def any_ids_west_of_x(
        src_path: Path,
        ids: Iterable[int],
        *,
        x_cutoff_src_crs: float,
        max_blocks: int = 2000,
) -> Set[int]:
    """Return which IDs are seen west of a source-CRS x cutoff.

    Args:
        src_path: EVT raster in its native CRS.
        ids: Category IDs to check.
        x_cutoff_src_crs: X threshold in src.crs coordinates (same units as src.transform).
        max_blocks: Safety cap.

    Returns:
        Set of IDs found in scanned blocks west of cutoff.
    """
    wanted = np.asarray(sorted(set(int(v) for v in ids)), dtype=np.int32)
    found: Set[int] = set()

    with rasterio.open(src_path) as src:
        tfm = src.transform

        blocks = 0
        for _, w in src.block_windows(1):
            if blocks >= max_blocks:
                break

            # Block center x in source CRS
            cx = (w.col_off + w.width / 2.0) * tfm.a + tfm.c
            if cx > x_cutoff_src_crs:
                continue

            data = src.read(1, window=w)

            # Cheap gate: if data range can't include any wanted value, skip
            dmin, dmax = int(data.min()), int(data.max())
            if dmax < wanted[0] or dmin > wanted[-1]:
                blocks += 1
                continue

            u = np.unique(data)
            hit = np.isin(wanted, u)
            if hit.any():
                for v in wanted[hit].tolist():
                    found.add(int(v))
                if len(found) == len(wanted):
                    break

            blocks += 1

    return found


if __name__ == "__main__":
    SRC = Path("elevation/LF2024_EVT_CONUS.tif")
    target = [4455, 4458, 4963]

    x3857 = [-9710700.0]
    y3857 = [4659178.5]  # mid-y of your crop box
    x5070, y5070 = transform("EPSG:3857", "EPSG:5070", x3857, y3857)
    x_cutoff_5070 = float(x5070[0])
    print("x_cutoff_5070 =", x_cutoff_5070)

    found = any_ids_west_of_x(SRC, target, x_cutoff_src_crs=x_cutoff_5070)
    print("Found west of cutoff:", sorted(found))
    print("Missing west of cutoff:", sorted(set(target) - found))
