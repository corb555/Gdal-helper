from __future__ import annotations

from pathlib import Path

from GDALHelper.utils import (
    ConfigurationError,
    FileError,
    _compute_pad,
)


SIGMA_DEFAULT = 1.0

ALPHA_OFF = 0
ALPHA_ON = 255


"""
Applies 'Edge Feathering' to a mask or alpha raster.

This command uses a Euclidean Distance Transform (EDT) to calculate a
Gaussian-style falloff based on proximity to the feature's edge.

It is a perimetric operation: the interior of the mask remains 100%
opaque (solid), while the edges fade smoothly into the background.

Usage:
  Use this for vignettes, map borders, or blending categorical
  polygons where you want a soft transition but a solid interior.
"""

def feather(args) -> None:
    import cv2
    import numpy as np
    import rasterio
    from rasterio.enums import ColorInterp
    from rasterio.errors import RasterioIOError
    from rasterio.windows import Window
    from tqdm import tqdm

    sigma = float(args.sigma)
    truncate = float(args.truncate)
    tile_size = int(args.tile_size)
    band_idx = int(args.band)

    if sigma <= 0:
        raise ConfigurationError(
            f"Feather sigma must be greater than 0; received {sigma}."
        )

    if truncate <= 0:
        raise ConfigurationError(
            f"Feather truncate must be greater than 0; received {truncate}."
        )

    if tile_size <= 0:
        raise ConfigurationError(
            f"Tile size must be greater than 0; received {tile_size}."
        )

    pad = _compute_pad(sigma, truncate)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        src = rasterio.open(args.input)
    except RasterioIOError as e:
        raise FileError(
            f"Could not open input raster '{args.input}': {e}"
        ) from e

    with src:
        if band_idx < 1 or band_idx > src.count:
            raise ConfigurationError(
                f"Requested band {band_idx}, but "
                f"'{args.input}' has {src.count} band(s)."
            )

        print(
            f"--- Edge-feather '{args.input}' "
            f"[Band {band_idx}] (sigma={sigma}) ---"
        )

        # Output is always a single-band uint8 mask/alpha raster.
        profile = src.profile.copy()
        profile.update(
            {
                "driver": "GTiff",
                "dtype": "uint8",
                "count": 1,
                "nodata": ALPHA_OFF,
                "compress": "deflate",
                "tiled": True,
                "blockxsize": tile_size,
                "blockysize": tile_size,
                "SPARSE_OK": "YES",
            }
        )

        if args.co:
            for opt in args.co:
                if "=" not in opt:
                    raise ConfigurationError(
                        f"Invalid creation option '{opt}'. "
                        "Expected KEY=VALUE."
                    )

                key, value = opt.split("=", 1)
                profile[key.lower()] = (
                    int(value) if value.isdigit() else value
                )

        # From here onward, any failure should remove a partial output.
        out_path.unlink(missing_ok=True)

        try:
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.colorinterp = (ColorInterp.alpha,)

                windows = [
                    window
                    for _, window in dst.block_windows(1)
                ]

                for window in tqdm(
                    windows,
                    desc="   Feathering",
                    leave=False,
                ):
                    height = int(window.height)
                    width = int(window.width)

                    read_window = Window(
                        col_off=window.col_off - pad,
                        row_off=window.row_off - pad,
                        width=width + 2 * pad,
                        height=height + 2 * pad,
                    )

                    data = src.read(
                        band_idx,
                        window=read_window,
                        boundless=True,
                        fill_value=ALPHA_OFF,
                    )

                    # Entire block and halo are background.
                    if not np.any(data):
                        continue

                    feature = data != ALPHA_OFF

                    # Entire block and halo are feature.
                    if feature.all():
                        dst.write(
                            np.full(
                                (height, width),
                                ALPHA_ON,
                                dtype=np.uint8,
                            ),
                            window=window,
                            indexes=1,
                        )
                        continue

                    # Distance from background pixels to the feature.
                    dist = cv2.distanceTransform(
                        (~feature).astype(np.uint8),
                        distanceType=cv2.DIST_L2,
                        maskSize=5,
                    )

                    alpha_f = (
                        np.exp(
                            -(dist ** 2)
                            / (2.0 * sigma ** 2)
                        )
                        * ALPHA_ON
                    )

                    # Feature interior remains fully opaque.
                    alpha_f[feature] = ALPHA_ON

                    result = np.clip(
                        alpha_f,
                        ALPHA_OFF,
                        ALPHA_ON,
                    ).astype(np.uint8)

                    # Remove the halo before writing the destination block.
                    final = result[
                        pad:pad + height,
                        pad:pad + width,
                    ]

                    dst.write(
                        final,
                        window=window,
                        indexes=1,
                    )

        except RasterioIOError as e:
            out_path.unlink(missing_ok=True)
            raise FileError(
                f"Could not create raster '{out_path}': {e}"
            ) from e

        except OSError as e:
            out_path.unlink(missing_ok=True)
            raise FileError(
                f"File error while creating '{out_path}': {e}"
            ) from e

        except Exception:
            # Do not disguise programming errors as file errors.
            # Remove the incomplete output and preserve the traceback.
            out_path.unlink(missing_ok=True)
            raise

    print(f"✅ Created Edge Feather: {out_path}")