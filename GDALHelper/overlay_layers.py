"""Overlay categorical raster and vector layers onto a base raster.

The base raster defines the output grid. Overlay rasters must match that grid
exactly. GeoPackage overlays are rasterized onto the base grid before being
applied.

Overlays are applied from left to right. Later overlays therefore win where
multiple overlays write the same pixel.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from GDALHelper.utils import ConfigurationError, FileError

DEFAULT_ATTRIBUTE = "value"
TEMP_NODATA = -2147483648
TEMP_GDAL_TYPE = "Int32"
RASTER_SUFFIXES = {".tif", ".tiff"}
VECTOR_SUFFIXES = {".gpkg"}
TRANSFORM_TOLERANCE = 1e-9


def overlay_layers(args) -> None:
    """Overlay categorical layers onto a base raster.

    The base raster defines the output CRS, extent, resolution, dimensions,
    datatype, and nodata value. Raster overlays must already match the base
    grid exactly. GeoPackage overlays are rasterized to the base grid.

    Overlay pixels are copied when they are valid and, when ``values`` is
    supplied, their value is present in that whitelist. Overlays are applied
    in command-line order, so later overlays have precedence.

    Args:

    Raises:
        ConfigurationError: If an input is unsupported, incompatible, or has
            invalid arguments.
        FileError: If an input cannot be opened or an output cannot be written.
    """
    import rasterio

    base_path=args.input
    overlay_paths=args.overlays
    output_path=args.output
    values=args.values
    attribute=args.attribute
    creation_options=args.co

    base = Path(base_path)
    overlays = [Path(path) for path in overlay_paths]
    output = Path(output_path)
    selected_values = None if values is None else tuple(dict.fromkeys(values))

    _validate_paths(base, overlays, output)
    if any(path.suffix.lower() in VECTOR_SUFFIXES for path in overlays):
        _validate_attribute(attribute)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)

    try:
        with rasterio.open(base) as src:
            _validate_base(src)
            _validate_values_for_dtype(selected_values, src.dtypes[0])

            profile = src.profile.copy()
            profile.update(driver="GTiff")
            _apply_creation_options(profile, creation_options)

            with rasterio.open(output, "w+", **profile) as dst:
                _copy_base(src, dst)

                for overlay in overlays:
                    suffix = overlay.suffix.lower()

                    if suffix in RASTER_SUFFIXES:
                        _apply_raster_overlay(
                            dst=dst,
                            overlay_path=overlay,
                            selected_values=selected_values,
                        )
                    elif suffix in VECTOR_SUFFIXES:
                        _apply_vector_overlay(
                            dst=dst,
                            overlay_path=overlay,
                            selected_values=selected_values,
                            attribute=attribute,
                        )
                    else:
                        raise ConfigurationError(
                            f"Unsupported overlay type '{overlay.suffix}' for "
                            f"'{overlay}'. Supported overlays: GeoTIFF and GeoPackage."
                        )

    except Exception:
        output.unlink(missing_ok=True)
        raise


def _validate_paths(base: Path, overlays: Sequence[Path], output: Path) -> None:
    """Validate input and output paths."""
    if not base.is_file():
        raise FileError(f"Base raster not found: {base}")

    if not overlays:
        raise ConfigurationError("At least one overlay is required.")

    missing = [path for path in overlays if not path.is_file()]
    if missing:
        raise FileError(f"Overlay not found: {missing[0]}")

    input_paths = {base.resolve(), *(path.resolve() for path in overlays)}
    if output.resolve() in input_paths:
        raise ConfigurationError("Output must not overwrite an input file.")


def _validate_attribute(attribute: str) -> None:
    """Validate the vector attribute name."""
    if not attribute or not attribute.strip():
        raise ConfigurationError("--attribute must not be empty.")


def _validate_base(src) -> None:
    """Validate assumptions required by categorical overlay processing."""
    if src.count != 1:
        raise ConfigurationError(
            f"Base raster must have exactly one band; found {src.count}."
        )

    if src.crs is None:
        raise ConfigurationError("Base raster has no CRS.")

    if not np.issubdtype(np.dtype(src.dtypes[0]), np.number):
        raise ConfigurationError(
            f"Base raster datatype must be numeric; found {src.dtypes[0]}."
        )

    # gdal_rasterize can reproduce north-up grids exactly with -te/-ts.
    if abs(src.transform.b) > TRANSFORM_TOLERANCE or abs(src.transform.d) > TRANSFORM_TOLERANCE:
        raise ConfigurationError(
            "Rotated/skewed base rasters are not supported by overlay-layers."
        )


def _validate_values_for_dtype(
    values: Sequence[int] | None,
    dtype_name: str,
) -> None:
    """Ensure requested category values can be represented by the output dtype."""
    if values is None:
        return

    dtype = np.dtype(dtype_name)
    if not np.issubdtype(dtype, np.integer):
        return

    limits = np.iinfo(dtype)
    invalid = [value for value in values if value < limits.min or value > limits.max]
    if invalid:
        raise ConfigurationError(
            f"Overlay value {invalid[0]} cannot be represented by output dtype "
            f"{dtype.name}."
        )


def _apply_creation_options(profile: dict, options: Sequence[str] | None) -> None:
    """Apply ``KEY=VALUE`` GeoTIFF creation options to a Rasterio profile."""
    for option in options or ():
        if "=" not in option:
            raise ConfigurationError(
                f"Invalid creation option '{option}'. Expected KEY=VALUE."
            )

        key, value = option.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if not key or not value:
            raise ConfigurationError(
                f"Invalid creation option '{option}'. Expected KEY=VALUE."
            )

        profile[key] = int(value) if value.isdigit() else value


def _copy_base(src, dst) -> None:
    """Copy the base raster into the output without loading it all into memory."""
    for _, window in src.block_windows(1):
        dst.write(src.read(1, window=window), 1, window=window)


def _apply_raster_overlay(
    *,
    dst,
    overlay_path: Path,
    selected_values: Sequence[int] | None,
) -> None:
    """Apply one already-rasterized overlay to the destination."""
    import rasterio

    try:
        with rasterio.open(overlay_path) as overlay:
            _validate_overlay_grid(dst, overlay, overlay_path)

            for _, window in dst.block_windows(1):
                overlay_data = overlay.read(1, window=window, masked=True)
                mask = ~np.ma.getmaskarray(overlay_data)

                if selected_values is not None:
                    mask &= np.isin(overlay_data.data, selected_values)

                if not np.any(mask):
                    continue

                output_data = dst.read(1, window=window)
                output_data[mask] = overlay_data.data[mask]
                dst.write(output_data, 1, window=window)

    except rasterio.errors.RasterioIOError as exc:
        raise FileError(f"Could not read overlay raster '{overlay_path}': {exc}") from exc


def _validate_overlay_grid(dst, overlay, overlay_path: Path) -> None:
    """Require an overlay raster to match the base/output grid exactly."""
    if overlay.count != 1:
        raise ConfigurationError(
            f"Overlay raster '{overlay_path}' must have exactly one band; "
            f"found {overlay.count}."
        )

    if overlay.crs != dst.crs:
        raise ConfigurationError(
            f"Overlay raster CRS does not match base: {overlay_path}"
        )

    if overlay.width != dst.width or overlay.height != dst.height:
        raise ConfigurationError(
            f"Overlay raster dimensions do not match base: {overlay_path}"
        )

    if not overlay.transform.almost_equals(dst.transform):
        raise ConfigurationError(
            f"Overlay raster grid is not pixel-aligned with base: {overlay_path}"
        )


def _apply_vector_overlay(
    *,
    dst,
    overlay_path: Path,
    selected_values: Sequence[int] | None,
    attribute: str,
) -> None:
    """Rasterize one GeoPackage to the base grid, then apply it as an overlay."""
    with tempfile.TemporaryDirectory(prefix="gdal_helper_overlay_") as tmp_dir:
        rasterized = Path(tmp_dir) / "overlay.tif"
        _rasterize_geopackage(
            overlay_path=overlay_path,
            output_path=rasterized,
            dst=dst,
            attribute=attribute,
        )
        _apply_raster_overlay(
            dst=dst,
            overlay_path=rasterized,
            selected_values=selected_values,
        )


def _rasterize_geopackage(
    *,
    overlay_path: Path,
    output_path: Path,
    dst,
    attribute: str,
) -> None:
    """Rasterize a GeoPackage exactly onto the destination raster grid.

    GDAL is intentionally used here instead of introducing a second Python
    vector I/O dependency. The temporary raster inherits the vector CRS; the
    normal raster-grid validation then rejects a CRS mismatch rather than
    silently reprojecting categorical data.
    """
    bounds = dst.bounds

    command = [
        "gdal_rasterize",
        "-a",
        attribute,
        "-ot",
        TEMP_GDAL_TYPE,
        "-a_nodata",
        str(TEMP_NODATA),
        "-init",
        str(TEMP_NODATA),
        "-te",
        str(bounds.left),
        str(bounds.bottom),
        str(bounds.right),
        str(bounds.top),
        "-ts",
        str(dst.width),
        str(dst.height),
        "-of",
        "GTiff",
        str(overlay_path),
        str(output_path),
    ]

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise FileError(f"Could not execute gdal_rasterize: {exc}") from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise ConfigurationError(
            f"Could not rasterize GeoPackage '{overlay_path}': "
            f"{message or 'gdal_rasterize failed.'}"
        )
