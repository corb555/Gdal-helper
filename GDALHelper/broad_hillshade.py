#!/usr/bin/env python3
"""
Generate a broad terrain hillshade for LandWeaver.

This module implements the broad-hillshade operation used by the gdal-helper ``broad_hillshade`` command as a LiteBuild
companion to ``gdaldem hillshade -igor``.

The two products have deliberately separate responsibilities:

- ``gdaldem hillshade -igor`` produces the detailed hillshade used for local
  terrain texture.
- This command produces only medium/broad terrain shading used for larger-scale
  geographic form and terrain massing.

LandWeaver combines the two source rasters with the ``terrain_shading`` factor
operation. The broad raster therefore follows a simple attenuation contract:

    255 = neutral / no additional shadow
      0 = maximum shadow

The generator intentionally emits shadow only. Terrain brighter than a flat
surface under the configured light direction remains neutral (255), because
LandWeaver's ``terrain_shading`` operation only needs the broad source to
contribute additional shadow beyond the detailed Igor layer.

Processing is windowed with halo padding so Gaussian smoothing does not create
internal tile seams.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Iterator

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import gaussian_filter
from tqdm import tqdm


DEFAULT_AZIMUTH_DEG = 315.0
DEFAULT_ALTITUDE_DEG = 35.0

DEFAULT_GAIN = 2.5

DEFAULT_MEDIUM_SIGMA_PX = 5.0
DEFAULT_BROAD_SIGMA_PX = 28.0
DEFAULT_MEDIUM_WEIGHT = 0.65
DEFAULT_BROAD_WEIGHT = 0.35

DEFAULT_TILE_SIZE_PX = 2048

GAUSSIAN_CONTEXT_SIGMAS = 4.0
MIN_FILTER_WEIGHT = 1.0e-6


@dataclass(frozen=True, slots=True)
class BroadHillshadeConfig:
    """Configuration for wide terrain hillshade generation.

    This tool intentionally does not use slope-adaptive dual gain. The detailed
    Igor hillshade owns local terrain exaggeration. This companion layer uses a
    single fixed gain on smoothed DEM scales so it contributes coherent
    medium/broad geographic form without reintroducing fine-scale slope texture.

    Medium and broad lighting are calculated independently and blended with
    normalized weights.
    """

    azimuth_deg: float = DEFAULT_AZIMUTH_DEG
    altitude_deg: float = DEFAULT_ALTITUDE_DEG
    gain: float = DEFAULT_GAIN

    medium_sigma_px: float = DEFAULT_MEDIUM_SIGMA_PX
    broad_sigma_px: float = DEFAULT_BROAD_SIGMA_PX
    medium_weight: float = DEFAULT_MEDIUM_WEIGHT
    broad_weight: float = DEFAULT_BROAD_WEIGHT

    tile_size_px: int = DEFAULT_TILE_SIZE_PX

    def __post_init__(self) -> None:
        if not 0.0 < self.altitude_deg < 90.0:
            raise ValueError("altitude_deg must be in the range (0, 90).")

        if self.gain <= 0.0:
            raise ValueError("gain must be greater than zero.")

        if self.medium_sigma_px < 0.0 or self.broad_sigma_px < 0.0:
            raise ValueError("Relief scale sigmas cannot be negative.")

        if self.medium_weight < 0.0 or self.broad_weight < 0.0:
            raise ValueError("Relief scale weights cannot be negative.")

        if self.medium_weight + self.broad_weight <= 0.0:
            raise ValueError(
                "At least one medium/broad relief scale weight must be positive."
            )

        if self.tile_size_px <= 0:
            raise ValueError("tile_size_px must be greater than zero.")

    @property
    def normalized_weights(self) -> tuple[float, float]:
        """Return medium and broad weights normalized to sum to one."""
        total = self.medium_weight + self.broad_weight
        return (
            self.medium_weight / total,
            self.broad_weight / total,
        )

    @property
    def halo_px(self) -> int:
        """Return padding required by the enabled Gaussian scales."""
        max_sigma = max(self.medium_sigma_px, self.broad_sigma_px)
        return max(
            1,
            int(math.ceil(max_sigma * GAUSSIAN_CONTEXT_SIGMAS)),
        )


def _gaussian_filter_valid(
    data: np.ndarray,
    valid: np.ndarray,
    sigma: float,
) -> np.ndarray:
    """Gaussian-filter an array while reducing NoData contamination."""
    if sigma <= 0.0:
        return data.astype(np.float32, copy=True)

    weights = valid.astype(np.float32)

    numerator = gaussian_filter(
        np.where(valid, data, 0.0).astype(np.float32),
        sigma=sigma,
        mode="nearest",
    )
    denominator = gaussian_filter(
        weights,
        sigma=sigma,
        mode="nearest",
    )

    result = np.full(data.shape, np.nan, dtype=np.float32)
    np.divide(
        numerator,
        denominator,
        out=result,
        where=denominator > MIN_FILTER_WEIGHT,
    )
    return result


def _horn_gradients(
    dem: np.ndarray,
    cell_size_x: float,
    cell_size_y: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return Horn 3x3 terrain gradients."""
    padded = np.pad(dem, 1, mode="edge")

    z1 = padded[:-2, :-2]
    z2 = padded[:-2, 1:-1]
    z3 = padded[:-2, 2:]
    z4 = padded[1:-1, :-2]
    z6 = padded[1:-1, 2:]
    z7 = padded[2:, :-2]
    z8 = padded[2:, 1:-1]
    z9 = padded[2:, 2:]

    gx = (
        (z3 + 2.0 * z6 + z9)
        - (z1 + 2.0 * z4 + z7)
    ) / (8.0 * cell_size_x)

    # Raster rows increase toward the south, so this derivative is dz/dsouth.
    # Keep that sign here. With the lighting coordinate system used below
    # (+x east, +y north), the terrain normal's north component is +dz/dsouth.
    gy_south = (
        (z7 + 2.0 * z8 + z9)
        - (z1 + 2.0 * z2 + z3)
    ) / (8.0 * cell_size_y)

    return (
        gx.astype(np.float32, copy=False),
        gy_south.astype(np.float32, copy=False),
    )


def _signed_direct_lighting(
    dem: np.ndarray,
    gain: float,
    valid: np.ndarray,
    cell_size_x: float,
    cell_size_y: float,
    azimuth_deg: float,
    altitude_deg: float,
) -> np.ndarray:
    """Calculate signed Lambertian illumination from one DEM scale.

    Azimuth follows the conventional GIS compass convention: 0=north,
    90=east, 180=south, 270=west. Therefore 315 degrees is northwest /
    upper-left illumination in a north-up raster.
    """
    gx, gy_south = _horn_gradients(
        dem,
        cell_size_x,
        cell_size_y,
    )

    gx *= gain
    gy_south *= gain

    nx = -gx
    ny = gy_south
    nz = np.ones_like(dem, dtype=np.float32)

    magnitude = np.sqrt(nx * nx + ny * ny + nz * nz)

    nx = np.divide(
        nx,
        magnitude,
        out=np.zeros_like(nx),
        where=magnitude > 0.0,
    )
    ny = np.divide(
        ny,
        magnitude,
        out=np.zeros_like(ny),
        where=magnitude > 0.0,
    )
    nz = np.divide(
        nz,
        magnitude,
        out=np.zeros_like(nz),
        where=magnitude > 0.0,
    )

    azimuth = np.radians(azimuth_deg)
    altitude = np.radians(altitude_deg)

    light_x = np.sin(azimuth) * np.cos(altitude)
    light_y = np.cos(azimuth) * np.cos(altitude)
    light_z = np.sin(altitude)

    illumination = (
        nx * light_x
        + ny * light_y
        + nz * light_z
    )
    illumination = np.clip(
        illumination,
        -1.0,
        1.0,
    ).astype(np.float32)

    illumination[~valid] = np.nan
    return illumination


def _shadow_only_attenuation(
    signed_lighting: np.ndarray,
    altitude_deg: float,
) -> np.ndarray:
    """Convert signed lighting to neutral-at-1 shadow attenuation.

    A flat surface receives ``sin(altitude)`` from Lambertian lighting.
    Illumination at or above that flat-surface reference is neutralized to 1.0.
    Only darker-than-flat illumination becomes shadow.

    The darkest possible signed illumination (-1) maps to 0.0.
    """
    flat_reference = math.sin(math.radians(altitude_deg))
    denominator = flat_reference + 1.0

    attenuation = (signed_lighting + 1.0) / denominator
    return np.clip(
        attenuation,
        0.0,
        1.0,
    ).astype(np.float32, copy=False)


def _build_broad_hillshade(
    dem: np.ndarray,
    valid: np.ndarray,
    cell_size_x: float,
    cell_size_y: float,
    config: BroadHillshadeConfig,
) -> np.ndarray:
    """Build medium/broad shadow attenuation for one padded window."""
    medium_weight, broad_weight = config.normalized_weights

    scales = (
        (config.medium_sigma_px, medium_weight),
        (config.broad_sigma_px, broad_weight),
    )

    signed_lighting = np.zeros(dem.shape, dtype=np.float32)

    for sigma, weight in scales:
        if weight <= 0.0:
            continue

        scaled_dem = _gaussian_filter_valid(
            dem,
            valid,
            sigma,
        )
        scaled_valid = valid & np.isfinite(scaled_dem)

        stable_dem = np.where(
            scaled_valid,
            scaled_dem,
            0.0,
        ).astype(np.float32)

        scale_lighting = _signed_direct_lighting(
            stable_dem,
            config.gain,
            scaled_valid,
            cell_size_x,
            cell_size_y,
            config.azimuth_deg,
            config.altitude_deg,
        )

        signed_lighting += (
            np.nan_to_num(scale_lighting, nan=0.0)
            * weight
        )

    attenuation = _shadow_only_attenuation(
        signed_lighting,
        config.altitude_deg,
    )
    attenuation[~valid] = np.nan
    return attenuation


def _lighting_to_uint8(lighting: np.ndarray) -> np.ndarray:
    """Encode 0..1 attenuation as uint8 with 255 as neutral."""
    result = np.zeros(lighting.shape, dtype=np.uint8)
    valid = np.isfinite(lighting)

    if np.any(valid):
        result[valid] = np.round(
            np.clip(lighting[valid], 0.0, 1.0) * 255.0
        ).astype(np.uint8)

    return result


def _iter_output_windows(
    width: int,
    height: int,
    tile_size_px: int,
) -> Iterator[Window]:
    """Yield logical output windows covering the raster."""
    for row_off in range(0, height, tile_size_px):
        window_height = min(
            tile_size_px,
            height - row_off,
        )

        for col_off in range(0, width, tile_size_px):
            window_width = min(
                tile_size_px,
                width - col_off,
            )
            yield Window(
                col_off,
                row_off,
                window_width,
                window_height,
            )


def _expand_window(
    window: Window,
    halo_px: int,
    raster_width: int,
    raster_height: int,
) -> tuple[Window, tuple[slice, slice]]:
    """Expand a core window to available raster bounds and return crop slices."""
    col_start = max(
        0,
        int(window.col_off) - halo_px,
    )
    row_start = max(
        0,
        int(window.row_off) - halo_px,
    )
    col_end = min(
        raster_width,
        int(window.col_off + window.width) + halo_px,
    )
    row_end = min(
        raster_height,
        int(window.row_off + window.height) + halo_px,
    )

    padded = Window(
        col_start,
        row_start,
        col_end - col_start,
        row_end - row_start,
    )

    crop_row = int(window.row_off) - row_start
    crop_col = int(window.col_off) - col_start

    crop = (
        slice(
            crop_row,
            crop_row + int(window.height),
        ),
        slice(
            crop_col,
            crop_col + int(window.width),
        ),
    )

    return padded, crop


def _validate_dem(dem_src: rasterio.io.DatasetReader) -> None:
    """Validate the source DEM."""
    if dem_src.count < 1:
        raise ValueError(
            "DEM must contain at least one raster band."
        )


def generate_broad_hillshade(
    dem_path: Path,
    output_path: Path,
    config: BroadHillshadeConfig,
) -> None:
    """Generate a tiled medium/broad hillshade raster."""
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    output_path.unlink(
        missing_ok=True,
    )

    try:
        with rasterio.open(dem_path) as dem_src:
            _validate_dem(dem_src)

            cell_size_x = abs(
                float(dem_src.transform.a)
            )
            cell_size_y = abs(
                float(dem_src.transform.e)
            )

            if (
                cell_size_x <= 0.0
                or cell_size_y <= 0.0
            ):
                raise ValueError(
                    "DEM has invalid pixel dimensions."
                )

            profile = dem_src.profile.copy()
            profile.update(
                dtype="uint8",
                count=1,
                nodata=None,
                compress="DEFLATE",
                predictor=2,
                tiled=True,
                photometric="MINISBLACK",
            )

            windows = list(
                _iter_output_windows(
                    dem_src.width,
                    dem_src.height,
                    config.tile_size_px,
                )
            )

            print(
                f"Broad hillshade: "
                f"{dem_src.width}x{dem_src.height}, "
                f"tile={config.tile_size_px}px, "
                f"halo={config.halo_px}px, "
                f"windows={len(windows)}"
            )

            with rasterio.open(
                output_path,
                "w",
                **profile,
            ) as dst:
                total = len(windows)
                interval = 30.0 if total < 40 else 60.0
                for core_window in tqdm(
                    windows,
                    total=len(windows),
                    desc="Broad hillshade",
                    unit="window",
                    leave=False,
                    mininterval=interval,
                    bar_format="{desc}: {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
                ):
                    padded_window, crop = _expand_window(
                        core_window,
                        config.halo_px,
                        dem_src.width,
                        dem_src.height,
                    )

                    dem_masked = dem_src.read(
                        1,
                        window=padded_window,
                        masked=True,
                        out_dtype="float32",
                    )

                    dem = dem_masked.filled(
                        np.nan
                    ).astype(
                        np.float32,
                        copy=False,
                    )

                    valid = (
                        ~np.ma.getmaskarray(dem_masked)
                        & np.isfinite(dem)
                    )

                    if np.any(valid):
                        fallback = float(
                            np.nanmedian(dem[valid])
                        )

                        stable_dem = np.where(
                            valid,
                            dem,
                            fallback,
                        ).astype(np.float32)

                        padded_result = _build_broad_hillshade(
                            stable_dem,
                            valid,
                            cell_size_x,
                            cell_size_y,
                            config,
                        )
                        core_result = padded_result[crop]
                    else:
                        core_result = np.full(
                            (
                                int(core_window.height),
                                int(core_window.width),
                            ),
                            np.nan,
                            dtype=np.float32,
                        )

                    dst.write(
                        _lighting_to_uint8(core_result),
                        1,
                        window=core_window,
                    )

        print(
            f"Created {output_path}"
        )

    except Exception:
        output_path.unlink(
            missing_ok=True,
        )
        raise
