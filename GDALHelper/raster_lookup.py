from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Optional

from GDALHelper.reclassify import TILE_SIZE_DEFAULT
from GDALHelper.utils import COMPRESS_DEFAULT
import numpy as np
from tqdm import tqdm

ALPHA_OUTPUT_DTYPE = "uint8"
ALPHA_ON_DEFAULT = 255
ALPHA_OFF_DEFAULT = 0
SRC_BAND_INDEX = 1
DST_BAND_INDEX = 1
VALID_TIFF_EXTENSIONS = frozenset({".tif", ".tiff"})
PROGRESS_MIN_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class RasterOutputSpec:
    """Generic single-band GeoTIFF output settings."""

    dtype: str
    nodata: int | float
    tile_size: int = TILE_SIZE_DEFAULT
    compress: str = COMPRESS_DEFAULT
    creation_options: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True, slots=True)
class AlphaOutputSpec:
    """Optional validity-alpha output settings."""

    path: Path
    on: int = ALPHA_ON_DEFAULT
    off: int = ALPHA_OFF_DEFAULT
    sparse_ok: bool = True


@dataclass(frozen=True, slots=True)
class IntegerLutMapper:
    """Map non-negative integer raster keys through a dense lookup table.

    ``mapped`` is independent of the LUT value. A valid table mapping may
    therefore legitimately produce the same numeric value used for output
    nodata/default; callers should still avoid choosing such a nodata value
    because GIS readers will normally interpret that value as nodata.
    """

    lut: np.ndarray
    mapped: np.ndarray
    default_value: int | float
    input_nodata: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.lut.ndim != 1:
            raise ValueError("lut must be one-dimensional.")
        if self.mapped.ndim != 1:
            raise ValueError("mapped must be one-dimensional.")
        if self.lut.shape != self.mapped.shape:
            raise ValueError("lut and mapped must have identical shapes.")
        if self.mapped.dtype != np.bool_:
            raise ValueError("mapped must use boolean dtype.")

    @property
    def output_dtype(self) -> np.dtype:
        return self.lut.dtype

    def source_nodata_mask(self, data: np.ndarray) -> Optional[np.ndarray]:
        codes = self.input_nodata
        if not codes:
            return None
        if len(codes) == 1:
            return data == codes[0]
        if len(codes) == 2:
            first, second = codes
            return (data == first) | (data == second)
        if len(codes) == 3:
            first, second, third = codes
            return (data == first) | (data == second) | (data == third)
        if len(codes) == 4:
            first, second, third, fourth = codes
            return ((data == first) | (data == second) | (data == third) | (data == fourth))
        return np.isin(data, np.asarray(codes, dtype=np.int64))

    def map_block_into(
            self, data: np.ndarray, *, output: np.ndarray, matched: np.ndarray, ) -> None:
        """Map one source block into preallocated output and validity arrays."""
        output.fill(self.default_value)
        matched.fill(False)

        eligible = (data >= 0) & (data < self.lut.size)
        nodata_mask = self.source_nodata_mask(data)
        if nodata_mask is not None:
            eligible &= ~nodata_mask

        if not eligible.any():
            return

        source_indexes = data[eligible].astype(np.intp, copy=False)
        eligible_mapped = self.mapped[source_indexes]
        if not eligible_mapped.any():
            return

        eligible_rows, eligible_cols = np.nonzero(eligible)
        mapped_rows = eligible_rows[eligible_mapped]
        mapped_cols = eligible_cols[eligible_mapped]
        mapped_indexes = source_indexes[eligible_mapped]

        output[mapped_rows, mapped_cols] = self.lut[mapped_indexes]
        matched[mapped_rows, mapped_cols] = True


BlockObserver = Callable[[np.ndarray, np.ndarray], None]


class UnmappedIdCollector:
    """Collect a bounded sample of source keys that were not mapped."""

    def __init__(self, *, input_nodata: Sequence[int], limit: int) -> None:
        if limit <= 0:
            raise ValueError("limit must be > 0.")
        self._input_nodata = tuple(input_nodata)
        self._limit = limit
        self._values: set[int] = set()

    @property
    def values(self) -> list[int]:
        return sorted(self._values)

    def observe(self, data: np.ndarray, matched: np.ndarray) -> None:
        if len(self._values) >= self._limit:
            return

        unmatched = ~matched
        if self._input_nodata:
            unmatched &= ~np.isin(
                data, np.asarray(self._input_nodata, dtype=np.int64), )
        if not unmatched.any():
            return

        remaining = self._limit - len(self._values)
        for value in np.unique(data[unmatched])[:remaining]:
            self._values.add(int(value))


def validate_integer_source_raster(source, *, operation_name: str) -> None:
    """Validate that source band 1 contains integer lookup keys."""
    source_dtype = np.dtype(source.dtypes[SRC_BAND_INDEX - 1])
    if not np.issubdtype(source_dtype, np.integer):
        raise ValueError(
            f"The {operation_name} source must use an integer data type because "
            f"pixel values are interpreted as lookup keys. Band 1 uses {source_dtype}."
        )


def source_nodata_value(source) -> Optional[int]:
    """Return band 1 nodata as an integer, failing if metadata is not integral."""
    value = source.nodata
    if value is None:
        return None
    integer_value = int(value)
    if value != integer_value:
        raise ValueError(
            f"Band 1 nodata must be an integer lookup key. Got {value}."
        )
    return integer_value


def normalize_source_nodata(
        source, configured_nodata: Sequence[int], ) -> tuple[int, ...]:
    """Combine configured source nodata with source metadata nodata."""
    values = set(configured_nodata)
    metadata_nodata = source_nodata_value(source)
    if metadata_nodata is not None:
        values.add(metadata_nodata)
    return tuple(sorted(values))


def _prepare_gtiff_profile(
        source_profile: Mapping[str, Any], spec: RasterOutputSpec, ) -> dict[str, Any]:
    profile: dict[str, Any] = {
        **source_profile, "driver": "GTiff", "dtype": spec.dtype, "count": 1, "nodata": spec.nodata,
        "compress": spec.compress, "tiled": True, "blockxsize": spec.tile_size,
        "blockysize": spec.tile_size,
    }
    if spec.creation_options:
        profile.update(spec.creation_options)
    return profile


def _safe_unlink(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        raise RuntimeError(f"❌ Refusing to delete directory: {path}")
    path.unlink()


def _resolve_output_paths(
        *, src_path: str | Path, out_path: Path, auxiliary_paths: Sequence[Path] = (), ) -> tuple[
    Path, tuple[Path, ...]]:
    source = Path(src_path).expanduser()
    if not source.exists():
        raise RuntimeError(f"❌ Input raster does not exist: {source}")
    if source.is_dir():
        raise RuntimeError(f"❌ Input raster path is a directory: {source}")

    source_resolved = source.resolve(strict=True)
    raw_outputs = (out_path, *auxiliary_paths)
    resolved_outputs: list[Path] = []

    for output in raw_outputs:
        candidate = output.expanduser()
        if candidate.exists() and candidate.is_dir():
            raise RuntimeError(f"❌ Output path is a directory: {candidate}")

        resolved = candidate.resolve(strict=False)
        if resolved.suffix.lower() not in VALID_TIFF_EXTENSIONS:
            raise RuntimeError(
                f"❌ Output must use a .tif or .tiff extension: {resolved}"
            )
        if resolved == source_resolved:
            raise RuntimeError(f"❌ Output path equals input path: {resolved}")
        if resolved in resolved_outputs:
            raise RuntimeError(f"❌ Duplicate output path: {resolved}")
        resolved_outputs.append(resolved)

    return resolved_outputs[0], tuple(resolved_outputs[1:])


def _prepare_output_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_unlink(path)


def _block_window_total(dataset, band_index: int = DST_BAND_INDEX) -> Optional[int]:
    try:
        block_height, block_width = dataset.block_shapes[band_index - 1]
        return math.ceil(dataset.height / block_height) * math.ceil(
            dataset.width / block_width
        )
    except Exception:
        return None


def write_integer_lut_transform(
        *, src_path: str | Path, out_path: Path, mapper: IntegerLutMapper,
        output_spec: RasterOutputSpec, alpha_spec: Optional[AlphaOutputSpec] = None,
        progress_description: str = "Mapping raster",
        block_observer: Optional[BlockObserver] = None, ) -> None:
    """Apply an integer LUT to a raster and write the result block-by-block.

    This function contains no reclassification or table semantics. The caller
    decides how the LUT is built and what a mapped value represents.
    """
    import rasterio
    from rasterio.enums import ColorInterp

    if np.dtype(output_spec.dtype) != mapper.output_dtype:
        raise ValueError(
            "output_spec.dtype must match mapper LUT dtype: "
            f"{output_spec.dtype} != {mapper.output_dtype}"
        )
    if output_spec.tile_size <= 0:
        raise ValueError("tile_size must be > 0.")

    auxiliary_paths = (alpha_spec.path,) if alpha_spec is not None else ()
    output_resolved, resolved_auxiliary = _resolve_output_paths(
        src_path=src_path, out_path=out_path, auxiliary_paths=auxiliary_paths, )
    alpha_resolved = resolved_auxiliary[0] if resolved_auxiliary else None

    _prepare_output_path(output_resolved)
    if alpha_resolved is not None:
        _prepare_output_path(alpha_resolved)

    with rasterio.open(src_path) as source:
        validate_integer_source_raster(source, operation_name="lookup")
        source_profile = source.profile.copy()
        destination_profile = _prepare_gtiff_profile(source_profile, output_spec)

        alpha_profile: Optional[dict[str, Any]] = None
        if alpha_spec is not None:
            if not 0 <= alpha_spec.on <= 255 or not 0 <= alpha_spec.off <= 255:
                raise ValueError("Alpha on/off values must be within [0, 255].")
            alpha_creation_options = ({"SPARSE_OK": "YES"} if alpha_spec.sparse_ok else None)
            alpha_profile = _prepare_gtiff_profile(
                source_profile, RasterOutputSpec(
                    dtype=ALPHA_OUTPUT_DTYPE, nodata=alpha_spec.off,
                    tile_size=output_spec.tile_size, compress=output_spec.compress,
                    creation_options=alpha_creation_options, ), )

        tile_size = int(output_spec.tile_size)
        output_buffer = np.empty(
            (tile_size, tile_size), dtype=mapper.output_dtype, )
        matched_buffer = np.empty((tile_size, tile_size), dtype=np.bool_)
        alpha_buffer = (
            np.empty((tile_size, tile_size), dtype=np.uint8) if alpha_spec is not None else None)

        with rasterio.open(output_resolved, "w", **destination_profile) as destination:
            destination_alpha = (rasterio.open(
                alpha_resolved, "w", **alpha_profile
                ) if alpha_resolved is not None and alpha_profile is not None else None)
            if destination_alpha is not None:
                destination_alpha.colorinterp = (ColorInterp.alpha,)

            try:
                windows = (window for _, window in destination.block_windows(DST_BAND_INDEX))
                total = _block_window_total(destination, DST_BAND_INDEX)

                for window in tqdm(
                        windows, total=total, unit="block", desc=progress_description,
                        mininterval=PROGRESS_MIN_INTERVAL_SECONDS, ):
                    height = int(window.height)
                    width = int(window.width)

                    source_data = source.read(SRC_BAND_INDEX, window=window)
                    output_view = output_buffer[:height, :width]
                    matched_view = matched_buffer[:height, :width]

                    mapper.map_block_into(
                        source_data, output=output_view, matched=matched_view, )

                    if block_observer is not None:
                        block_observer(source_data, matched_view)

                    destination.write(
                        output_view, window=window, indexes=DST_BAND_INDEX, )

                    if destination_alpha is not None and alpha_buffer is not None:
                        alpha_view = alpha_buffer[:height, :width]
                        alpha_view.fill(alpha_spec.off)
                        alpha_view[matched_view] = alpha_spec.on
                        destination_alpha.write(
                            alpha_view, window=window, indexes=DST_BAND_INDEX, )
            finally:
                if destination_alpha is not None:
                    destination_alpha.close()
