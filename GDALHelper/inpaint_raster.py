"""Reconstruct masked pixels in a single-band raster from surrounding valid values.

The mask is aligned to the input raster and uses simple selection semantics:
zero preserves the source pixel; non-zero selects it for reconstruction.  An
optional ``--mask-value`` restricts selection to one mask value.

Supported methods:

``nearest``
    Propagate the value of the nearest valid donor pixel.  This preserves exact
    values and is the preferred method for categorical rasters.

``idw``
    Use GDAL/Rasterio FillNodata inverse-distance interpolation.

``telea`` / ``ns``
    Use OpenCV single-band inpainting on a float32 working array.

``biharmonic``
    Use scikit-image biharmonic interpolation.

The command preserves the source datatype on output.  Pixels outside the user
reconstruction mask are copied unchanged, including native NoData pixels.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from GDALHelper.utils import ConfigurationError, FileError


_METHODS = ("nearest", "idw", "telea", "ns", "biharmonic")
_UNFILLABLE_POLICIES = ("preserve", "nodata", "fail")
_DEFAULT_RADIUS = 3.0
_DEFAULT_MAX_SEARCH_DISTANCE = 100.0
_DEFAULT_SMOOTHING_ITERATIONS = 0
_DEFAULT_CREATION_OPTIONS = {
    "TILED": "YES",
    "COMPRESS": "DEFLATE",
}


@dataclass(frozen=True, slots=True)
class InpaintRasterConfig:
    """Validated configuration for one raster inpainting operation."""

    input: Path
    mask: Path
    output: Path
    method: str
    mask_value: float | None
    radius: float
    max_search_distance: float
    smoothing_iterations: int
    unfillable: str
    creation_options: tuple[str, ...]

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "InpaintRasterConfig":
        config = cls(
            input=Path(args.input),
            mask=Path(args.mask),
            output=Path(args.output),
            method=str(args.method).lower(),
            mask_value=args.mask_value,
            radius=float(args.radius),
            max_search_distance=float(args.max_search_distance),
            smoothing_iterations=int(args.smoothing_iterations),
            unfillable=str(args.unfillable).lower(),
            creation_options=tuple(args.co or ()),
        )
        _validate_config(config)
        return config


def inpaint_raster_add_args(parser: argparse.ArgumentParser) -> None:
    """Register CLI arguments for ``inpaint_raster``."""
    parser.add_argument("input", help="Input single-band raster.")
    parser.add_argument(
        "mask",
        help=(
            "Aligned mask raster. By default, zero preserves the input pixel and "
            "non-zero selects it for reconstruction."
        ),
    )
    parser.add_argument("output", help="Output reconstructed raster.")
    parser.add_argument(
        "--method",
        required=True,
        choices=_METHODS,
        help="Reconstruction method.",
    )
    parser.add_argument(
        "--mask-value",
        type=float,
        help="Only reconstruct mask pixels equal to this value.",
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=_DEFAULT_RADIUS,
        help=(
            "Local inpainting radius in pixels for telea/ns. "
            f"Default: {_DEFAULT_RADIUS:g}."
        ),
    )
    parser.add_argument(
        "--max-search-distance",
        type=float,
        default=_DEFAULT_MAX_SEARCH_DISTANCE,
        help=(
            "Maximum donor search distance in pixels for nearest/idw. "
            f"Default: {_DEFAULT_MAX_SEARCH_DISTANCE:g}."
        ),
    )
    parser.add_argument(
        "--smoothing-iterations",
        type=int,
        default=_DEFAULT_SMOOTHING_ITERATIONS,
        help=(
            "IDW post-fill 3x3 smoothing iterations. "
            f"Default: {_DEFAULT_SMOOTHING_ITERATIONS}."
        ),
    )
    parser.add_argument(
        "--unfillable",
        choices=_UNFILLABLE_POLICIES,
        default="preserve",
        help=(
            "Policy for selected pixels that cannot be reconstructed: "
            "preserve, nodata, or fail. Default: preserve."
        ),
    )
    parser.add_argument(
        "--co",
        action="append",
        metavar="NAME=VALUE",
        help="GeoTIFF creation option. Can be specified multiple times.",
    )


def inpaint_raster(
    config: InpaintRasterConfig,
    *,
    print_verbose=print,
) -> None:
    """Reconstruct selected pixels and write an aligned output raster."""
    import rasterio

    _validate_paths(config)

    print_verbose(
        f"--- Inpainting '{config.input}' using '{config.mask}' "
        f"with method '{config.method}' ---"
    )

    try:
        with rasterio.open(config.input) as src, rasterio.open(config.mask) as mask_src:
            _validate_source(src, config.input)
            _validate_mask_grid(src, mask_src, config.mask)

            source = src.read(1)
            raw_mask = mask_src.read(1, masked=False)
            reconstruct = _selection_mask(raw_mask, config.mask_value)
            source_valid = _source_valid_mask(source, src.nodata)
            donor_valid = (~reconstruct) & source_valid

            selected_count = int(np.count_nonzero(reconstruct))
            print_verbose(f"--- Selected pixels: {selected_count:,} ---")

            profile = src.profile.copy()
            profile.update(driver="GTiff")
            _apply_creation_options(profile, config.creation_options)

            if selected_count == 0:
                result = source.copy()
                fillable = np.zeros(source.shape, dtype=bool)
            else:
                result, fillable = _run_method(
                    source=source,
                    reconstruct=reconstruct,
                    donor_valid=donor_valid,
                    source_nodata=src.nodata,
                    config=config,
                )

            unfillable = reconstruct & ~fillable
            unfillable_count = int(np.count_nonzero(unfillable))
            if unfillable_count:
                _handle_unfillable(
                    result=result,
                    source=source,
                    unfillable=unfillable,
                    nodata=src.nodata,
                    config=config,
                    print_verbose=print_verbose,
                )

            result = _restore_dtype(result, source.dtype)
            # Outside the selected mask the output must be byte-for-byte/value-for-value
            # equivalent to the input array. This also preserves native NoData exactly.
            result[~reconstruct] = source[~reconstruct]

            config.output.parent.mkdir(parents=True, exist_ok=True)
            config.output.unlink(missing_ok=True)
            with rasterio.open(config.output, "w", **profile) as dst:
                dst.write(result, 1)

        print_verbose(
            f"✅ Wrote inpainted raster: {config.output} "
            f"({selected_count - unfillable_count:,}/{selected_count:,} selected pixels reconstructed)"
        )

    except Exception:
        config.output.unlink(missing_ok=True)
        raise


def _run_method(
    *,
    source: np.ndarray,
    reconstruct: np.ndarray,
    donor_valid: np.ndarray,
    source_nodata,
    config: InpaintRasterConfig,
) -> tuple[np.ndarray, np.ndarray]:
    if config.method == "nearest":
        return _nearest_fill(
            source,
            reconstruct,
            donor_valid,
            max_search_distance=config.max_search_distance,
        )
    if config.method == "idw":
        return _idw_fill(
            source,
            reconstruct,
            donor_valid,
            max_search_distance=config.max_search_distance,
            smoothing_iterations=config.smoothing_iterations,
        )
    if config.method == "telea":
        return _opencv_fill(
            source,
            reconstruct,
            donor_valid,
            radius=config.radius,
            algorithm="telea",
        )
    if config.method == "ns":
        return _opencv_fill(
            source,
            reconstruct,
            donor_valid,
            radius=config.radius,
            algorithm="ns",
        )
    if config.method == "biharmonic":
        return _biharmonic_fill(source, reconstruct, donor_valid)
    raise AssertionError(f"Unhandled inpaint method: {config.method}")


def _nearest_fill(
    source: np.ndarray,
    reconstruct: np.ndarray,
    donor_valid: np.ndarray,
    *,
    max_search_distance: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Propagate exact values from the nearest valid donor pixel."""
    from scipy.ndimage import distance_transform_edt

    result = source.copy()
    fillable = np.zeros(source.shape, dtype=bool)
    if not np.any(donor_valid):
        return result, fillable

    invalid = ~donor_valid
    distances, indices = distance_transform_edt(
        invalid,
        return_distances=True,
        return_indices=True,
    )

    can_fill = reconstruct & (distances <= max_search_distance)
    if np.any(can_fill):
        rows = indices[0][can_fill]
        cols = indices[1][can_fill]
        result[can_fill] = source[rows, cols]
        fillable[can_fill] = True
    return result, fillable


def _idw_fill(
    source: np.ndarray,
    reconstruct: np.ndarray,
    donor_valid: np.ndarray,
    *,
    max_search_distance: float,
    smoothing_iterations: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Fill from surrounding donors with Rasterio/GDAL FillNodata IDW."""
    from rasterio.fill import fillnodata
    from scipy.ndimage import distance_transform_edt

    result = source.astype(np.float32, copy=True)
    fillable = np.zeros(source.shape, dtype=bool)
    if not np.any(donor_valid):
        return result, fillable

    # fillnodata uses mask > 0 as valid donors and mask == 0 as fill targets.
    # Native NoData and user-selected pixels are both excluded as original donors.
    donor_mask = donor_valid.astype(np.uint8)
    work = source.astype(np.float32, copy=True)
    work[~donor_valid] = 0.0

    filled = fillnodata(
        work,
        mask=donor_mask,
        max_search_distance=max_search_distance,
        smoothing_iterations=smoothing_iterations,
    )

    distances = distance_transform_edt(~donor_valid)
    can_fill = reconstruct & (distances <= max_search_distance) & np.isfinite(filled)
    result[can_fill] = filled[can_fill]
    fillable[can_fill] = True
    return result, fillable


def _opencv_fill(
    source: np.ndarray,
    reconstruct: np.ndarray,
    donor_valid: np.ndarray,
    *,
    radius: float,
    algorithm: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Fill continuous values with OpenCV Telea or Navier-Stokes inpainting."""
    import cv2

    result = source.astype(np.float32, copy=True)
    fillable = np.zeros(source.shape, dtype=bool)
    if not np.any(donor_valid):
        return result, fillable

    # OpenCV has no separate donor mask. Mask native NoData together with the
    # user-requested region so those pixels are never supplied as original data.
    cv_mask = (~donor_valid).astype(np.uint8) * 255
    work = source.astype(np.float32, copy=True)
    work[~donor_valid] = 0.0

    flag = cv2.INPAINT_TELEA if algorithm == "telea" else cv2.INPAINT_NS
    filled = cv2.inpaint(work, cv_mask, radius, flag)

    can_fill = reconstruct & np.isfinite(filled)
    result[can_fill] = filled[can_fill]
    fillable[can_fill] = True
    return result, fillable


def _biharmonic_fill(
    source: np.ndarray,
    reconstruct: np.ndarray,
    donor_valid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Fill continuous values with biharmonic surface interpolation."""
    from skimage.restoration import inpaint_biharmonic

    result = source.astype(np.float64, copy=True)
    fillable = np.zeros(source.shape, dtype=bool)
    if not np.any(donor_valid):
        return result, fillable

    # Native NoData is included in the solver mask so it cannot act as a donor,
    # but only user-selected pixels are copied back into the final result.
    solver_mask = ~donor_valid
    work = source.astype(np.float64, copy=True)
    work[~donor_valid] = 0.0

    filled = inpaint_biharmonic(
        work,
        solver_mask,
        channel_axis=None,
        split_into_regions=True,
    )

    can_fill = reconstruct & np.isfinite(filled)
    result[can_fill] = filled[can_fill]
    fillable[can_fill] = True
    return result, fillable


def _selection_mask(mask_data: np.ndarray, mask_value: float | None) -> np.ndarray:
    if mask_value is None:
        return mask_data != 0
    return mask_data == mask_value


def _source_valid_mask(source: np.ndarray, nodata) -> np.ndarray:
    valid = np.ones(source.shape, dtype=bool)
    if np.issubdtype(source.dtype, np.floating):
        valid &= np.isfinite(source)

    if nodata is not None:
        if isinstance(nodata, float) and math.isnan(nodata):
            valid &= ~np.isnan(source)
        else:
            valid &= source != nodata
    return valid


def _handle_unfillable(
    *,
    result: np.ndarray,
    source: np.ndarray,
    unfillable: np.ndarray,
    nodata,
    config: InpaintRasterConfig,
    print_verbose,
) -> None:
    count = int(np.count_nonzero(unfillable))

    rows, cols = np.nonzero(unfillable)
    bbox = (
        int(cols.min()),
        int(rows.min()),
        int(cols.max()),
        int(rows.max()),
    )
    message = (
        f"{count:,} selected pixel(s) could not be reconstructed; "
        f"pixel bounds=(x:{bbox[0]}..{bbox[2]}, y:{bbox[1]}..{bbox[3]})."
    )

    if config.unfillable == "fail":
        raise ConfigurationError(message)

    if config.unfillable == "nodata":
        if nodata is None:
            raise ConfigurationError(
                f"{message} --unfillable nodata requires the input raster to define NoData."
            )
        result[unfillable] = nodata
        print_verbose(f"⚠️ {message} Wrote source NoData value.")
        return

    result[unfillable] = source[unfillable]
    print_verbose(f"⚠️ {message} Preserved original values.")



def _restore_dtype(values: np.ndarray, dtype: np.dtype) -> np.ndarray:
    dtype = np.dtype(dtype)
    if np.issubdtype(dtype, np.integer):
        limits = np.iinfo(dtype)
        return np.clip(np.rint(values), limits.min, limits.max).astype(dtype)
    return values.astype(dtype, copy=False)


def _apply_creation_options(profile: dict, options: Sequence[str]) -> None:
    merged = dict(_DEFAULT_CREATION_OPTIONS)
    for option in options:
        if "=" not in option:
            raise ConfigurationError(
                f"Invalid creation option '{option}'. Expected NAME=VALUE."
            )
        key, value = option.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if not key or not value:
            raise ConfigurationError(
                f"Invalid creation option '{option}'. Expected NAME=VALUE."
            )
        merged[key] = value

    for key, value in merged.items():
        profile[key.lower()] = _coerce_creation_value(value)


def _coerce_creation_value(value: str):
    if value.isdigit():
        return int(value)
    upper = value.upper()
    if upper in {"YES", "TRUE"}:
        return True
    if upper in {"NO", "FALSE"}:
        return False
    return value


def _validate_config(config: InpaintRasterConfig) -> None:
    if config.method not in _METHODS:
        raise ConfigurationError(f"Unsupported --method: {config.method}")
    if config.radius <= 0:
        raise ConfigurationError("--radius must be greater than 0.")
    if config.max_search_distance <= 0:
        raise ConfigurationError("--max-search-distance must be greater than 0.")
    if config.smoothing_iterations < 0:
        raise ConfigurationError("--smoothing-iterations cannot be negative.")
    if config.unfillable not in _UNFILLABLE_POLICIES:
        raise ConfigurationError(f"Unsupported --unfillable policy: {config.unfillable}")


def _validate_paths(config: InpaintRasterConfig) -> None:
    if not config.input.is_file():
        raise FileError(f"Input raster not found: '{config.input}'")
    if not config.mask.is_file():
        raise FileError(f"Mask raster not found: '{config.mask}'")

    input_resolved = config.input.resolve()
    mask_resolved = config.mask.resolve()
    output_resolved = config.output.resolve()
    if output_resolved in {input_resolved, mask_resolved}:
        raise ConfigurationError("Output must not overwrite the input or mask raster.")


def _validate_source(src, path: Path) -> None:
    if src.count != 1:
        raise ConfigurationError(
            f"Input raster must have exactly one band; found {src.count}: '{path}'"
        )
    if src.crs is None:
        raise ConfigurationError(f"Input raster has no CRS: '{path}'")
    if not np.issubdtype(np.dtype(src.dtypes[0]), np.number):
        raise ConfigurationError(
            f"Input raster datatype must be numeric; found {src.dtypes[0]}."
        )


def _validate_mask_grid(src, mask_src, path: Path) -> None:
    if mask_src.count != 1:
        raise ConfigurationError(
            f"Mask raster must have exactly one band; found {mask_src.count}: '{path}'"
        )
    if mask_src.crs != src.crs:
        raise ConfigurationError(f"Mask CRS does not match input raster: '{path}'")
    if mask_src.width != src.width or mask_src.height != src.height:
        raise ConfigurationError(f"Mask dimensions do not match input raster: '{path}'")
    if not mask_src.transform.almost_equals(src.transform):
        raise ConfigurationError(f"Mask grid is not pixel-aligned with input raster: '{path}'")
