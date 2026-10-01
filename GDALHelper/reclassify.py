from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Optional

from GDALHelper.utils import TILE_SIZE_DEFAULT, COMPRESS_DEFAULT
import numpy as np
from tqdm import tqdm

RECLASS_OUTPUT_DTYPE = "uint8"
ALPHA_OUTPUT_DTYPE = "uint8"
ALPHA_ON_DEFAULT = 255
ALPHA_OFF_DEFAULT = 0
DEFAULT_VALUE_DEFAULT = 0
NODATA_VALUE_DEFAULT = 0
SRC_BAND_INDEX = 1
DST_BAND_INDEX = 1
MAX_CLASSES = 255
MAX_CONFIGURED_CATEGORY_ID = 10_000_000
VALID_TIFF_EXTENSIONS = frozenset({".tif", ".tiff"})
PROGRESS_MIN_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ReclassRule:
    """A reclassification rule.

    Attributes:
        name: Human-friendly class name used in diagnostics.
        value: Output value assigned when any source ID matches.
        ids: Source category IDs mapped to this class.
    """

    name: str
    value: int
    ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SourceCheck:
    """Expected category-system fingerprint for the source raster."""

    category: str
    required_ids: tuple[int, ...] = ()
    expected_any_ids: tuple[int, ...] = ()
    minimum_expected_any: int = 0


@dataclass(frozen=True, slots=True)
class ReclassOptions:
    """Validated reclassification options.

    Attributes:
        tile_size: GeoTIFF tile width and height in pixels.
        compress: GeoTIFF compression method.
        default_value: Output value assigned to valid source pixels that do not match a class.
        nodata_value: Output value assigned to source nodata pixels and stored as GeoTIFF nodata.
        input_nodata: Source category values treated as nodata.
        write_alpha: Whether to write a sidecar alpha raster.
        alpha_output: Optional explicit path for the alpha raster.
        alpha_on: Alpha value assigned to valid source pixels, including defaulted pixels.
        alpha_off: Alpha value assigned to source nodata pixels.
        alpha_sparse_ok: Whether sparse GeoTIFF tiles may be used for alpha output.
        report_unmapped: Whether to report unmatched source category IDs.
        max_unmapped_ids: Maximum number of distinct unmatched IDs to report.
    """

    tile_size: int = TILE_SIZE_DEFAULT
    compress: str = COMPRESS_DEFAULT
    default_value: int = DEFAULT_VALUE_DEFAULT
    nodata_value: int = NODATA_VALUE_DEFAULT
    input_nodata: tuple[int, ...] = ()
    write_alpha: bool = False
    alpha_output: Optional[Path] = None
    alpha_on: int = ALPHA_ON_DEFAULT
    alpha_off: int = ALPHA_OFF_DEFAULT
    alpha_sparse_ok: bool = True
    report_unmapped: bool = False
    max_unmapped_ids: int = 50
    source_check: Optional[SourceCheck] = None


@dataclass(frozen=True, slots=True)
class RasterOutputSpec:
    """Generic single-band GeoTIFF output settings.

    This class deliberately contains no reclassification semantics. It can be
    reused by other integer-key lookup operations, such as extracting a raster
    attribute from an external DBF lookup table.

    Attributes:
        dtype: NumPy/Rasterio output dtype.
        nodata: Output nodata value.
        tile_size: GeoTIFF tile width and height.
        compress: GeoTIFF compression method.
        creation_options: Additional Rasterio/GDAL creation options.
    """

    dtype: str
    nodata: int | float
    tile_size: int = TILE_SIZE_DEFAULT
    compress: str = COMPRESS_DEFAULT
    creation_options: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True, slots=True)
class AlphaOutputSpec:
    """Optional validity-alpha output settings.

    Alpha is intentionally modeled separately from LUT mapping. A future table
    lookup may legitimately map values to zero, so validity must not be inferred
    from ``output != default_value``.

    Attributes:
        path: Alpha GeoTIFF path.
        on: Value written for valid source pixels, including unmatched/defaulted pixels.
        off: Value written for source nodata pixels.
        sparse_ok: Whether sparse GeoTIFF tiles may be used.
    """

    path: Path
    on: int = ALPHA_ON_DEFAULT
    off: int = ALPHA_OFF_DEFAULT
    sparse_ok: bool = True


@dataclass(frozen=True, slots=True)
class IntegerLutMapper:
    """Map integer raster keys through a dense lookup table.

    ``mapped`` records whether each LUT entry is valid independently of the LUT
    value itself. This keeps the mapper generic: a legitimate mapped value may
    equal the configured default value.

    Attributes:
        lut: Dense array indexed by non-negative source category IDs.
        mapped: Boolean array identifying LUT entries with defined mappings.
        default_value: Value assigned to valid but unmapped source pixels.
        nodata_value: Value assigned to source nodata pixels.
        input_nodata: Source values treated as nodata.
    """

    lut: np.ndarray
    mapped: np.ndarray
    default_value: int | float
    nodata_value: int | float
    input_nodata: tuple[int, ...]

    def __post_init__(self) -> None:
        """Validate LUT invariants."""
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
        """Return the NumPy dtype produced by this mapper."""
        return self.lut.dtype

    def source_nodata_mask(self, data: np.ndarray) -> Optional[np.ndarray]:
        """Return a mask of configured source nodata pixels.

        Args:
            data: Integer source raster block.

        Returns:
            Boolean nodata mask, or ``None`` when no nodata values are configured.
        """
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
        """Map one integer source block into preallocated arrays.

        Args:
            data: Source raster block containing integer category IDs.
            output: Preallocated output array.
            matched: Preallocated boolean array receiving successful-map status.

        Notes:
            Negative values and values beyond the LUT are treated as unmapped.
            Configured nodata values are always treated as unmapped.
        """
        output.fill(self.default_value)
        matched.fill(False)

        eligible = (data >= 0) & (data < self.lut.size)
        nodata_mask = self.source_nodata_mask(data)
        if nodata_mask is not None:
            output[nodata_mask] = self.nodata_value
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


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML configuration file.

    Args:
        path: YAML file path.

    Returns:
        Parsed top-level mapping.

    Raises:
        FileNotFoundError: If the file does not exist.
        RuntimeError: If PyYAML is unavailable.
        ValueError: If the YAML root is not a mapping.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    import yaml

    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError("Config YAML must parse to a mapping/object at the top level.")
    return config


def _validate_uint8(name: str, value: int) -> None:
    """Validate that a value fits in an unsigned 8-bit output."""
    if not 0 <= value <= np.iinfo(np.uint8).max:
        raise ValueError(f"{name} must be within [0, 255]. Got {value}.")


def _parse_input_nodata(raw_value: object) -> tuple[int, ...]:
    """Parse and normalize configured source nodata values."""
    raw_values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
    try:
        values = tuple(int(value) for value in raw_values)
    except (TypeError, ValueError) as exc:
        raise ValueError("options.input_nodata must contain only integers.") from exc
    return tuple(sorted(set(values)))


def _parse_category_ids(name: str, raw_value: object) -> tuple[int, ...]:
    """Parse and validate category IDs used by source_check."""
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise ValueError(f"{name} must be a list of integers.")

    try:
        values = tuple(int(value) for value in raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only integers.") from exc

    if len(values) != len(set(values)):
        raise ValueError(f"{name} may not contain duplicate category IDs.")
    if any(value < 0 for value in values):
        raise ValueError(f"{name} category IDs must be >= 0.")
    if any(value > MAX_CONFIGURED_CATEGORY_ID for value in values):
        highest = max(values)
        raise ValueError(
            f"{name} contains category ID {highest}, which exceeds the safety "
            f"limit of {MAX_CONFIGURED_CATEGORY_ID}."
        )

    return values


def _parse_source_check(config: Mapping[str, Any]) -> Optional[SourceCheck]:
    """Parse the optional source category fingerprint."""
    raw = config.get("source_check")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("source_check must be a mapping.")

    category = str(raw.get("category", "")).strip()
    if not category:
        raise ValueError("source_check.category is required and must be non-empty.")

    required_ids = _parse_category_ids(
        "source_check.required_ids", raw.get("required_ids")
    )
    expected_any_ids = _parse_category_ids(
        "source_check.expected_any_ids", raw.get("expected_any_ids")
    )

    overlap = sorted(set(required_ids).intersection(expected_any_ids))
    if overlap:
        raise ValueError(
            "source_check IDs may not appear in both required_ids and "
            f"expected_any_ids. Duplicates: {overlap}."
        )

    minimum_expected_any = int(
        raw.get("minimum_expected_any", 1 if expected_any_ids else 0)
    )
    if minimum_expected_any < 0:
        raise ValueError("source_check.minimum_expected_any must be >= 0.")
    if minimum_expected_any > len(expected_any_ids):
        raise ValueError(
            "source_check.minimum_expected_any may not exceed the number of "
            "source_check.expected_any_ids."
        )
    if not required_ids and not expected_any_ids:
        raise ValueError(
            "source_check must configure at least one required_ids or expected_any_ids category."
        )

    return SourceCheck(
        category=category, required_ids=required_ids, expected_any_ids=expected_any_ids,
        minimum_expected_any=minimum_expected_any, )


def _parse_reclass_config(
        config: Mapping[str, Any], ) -> tuple[list[ReclassRule], ReclassOptions]:
    """Parse and validate the reclassification configuration.

    YAML schema::

        classes:
          - name: water
            ids: [7735]
            value: 10

        source_check:
          category: LANDFIRE_EVT
          required_ids: [7735]
          expected_any_ids: [7734, 9016, 9033, 9153, 9160]
          minimum_expected_any: 1

        options:
          default_value: 1
          nodata_value: 0
          input_nodata: [-9999, 32767]
          tile_size: 256
          compress: deflate
          alpha:
            enabled: true
            output: "/path/to/alpha.tif"
            on: 255
            off: 0
          report_unmapped:
            enabled: false
            max_ids: 50

    ``default_value`` is written for valid source pixels that do not match
    any configured class. ``nodata_value`` is written for source nodata pixels
    and is also stored as the output GeoTIFF nodata value. For backward
    compatibility, ``nodata_value`` defaults to ``default_value`` when omitted.

    Args:
        config: Parsed configuration mapping.

    Returns:
        Validated reclassification rules and options.

    Raises:
        ValueError: If the configuration is invalid.
    """
    raw_classes = config.get("classes")
    if not isinstance(raw_classes, list) or not raw_classes:
        raise ValueError("Config must include a non-empty 'classes' list.")
    if len(raw_classes) > MAX_CLASSES:
        raise ValueError(
            f"Too many classes ({len(raw_classes)}). Max supported is {MAX_CLASSES}."
        )

    options_raw = config.get("options") if isinstance(config.get("options"), dict) else {}
    alpha_raw = (options_raw.get("alpha") if isinstance(options_raw.get("alpha"), dict) else {})
    report_raw = (options_raw.get("report_unmapped") if isinstance(
        options_raw.get("report_unmapped"), dict
        ) else {})

    default_value = int(options_raw.get("default_value", DEFAULT_VALUE_DEFAULT))
    nodata_value = int(options_raw.get("nodata_value", default_value))

    source_check = _parse_source_check(config)
    input_nodata = _parse_input_nodata(options_raw.get("input_nodata", []))
    options = ReclassOptions(
        tile_size=int(options_raw.get("tile_size", TILE_SIZE_DEFAULT)),
        compress=str(options_raw.get("compress", COMPRESS_DEFAULT)), default_value=default_value,
        nodata_value=nodata_value, input_nodata=input_nodata,
        write_alpha=bool(alpha_raw.get("enabled", True)),
        alpha_output=(Path(str(alpha_raw["output"])) if alpha_raw.get("output") else None),
        alpha_on=int(alpha_raw.get("on", ALPHA_ON_DEFAULT)),
        alpha_off=int(alpha_raw.get("off", ALPHA_OFF_DEFAULT)),
        alpha_sparse_ok=bool(alpha_raw.get("sparse_ok", True)),
        report_unmapped=bool(report_raw.get("enabled", False)),
        max_unmapped_ids=int(report_raw.get("max_ids", 50)), source_check=source_check, )

    if options.tile_size <= 0:
        raise ValueError("options.tile_size must be > 0.")
    _validate_uint8("options.default_value", options.default_value)
    _validate_uint8("options.nodata_value", options.nodata_value)
    _validate_uint8("options.alpha.on", options.alpha_on)
    _validate_uint8("options.alpha.off", options.alpha_off)
    if options.max_unmapped_ids <= 0:
        raise ValueError("options.report_unmapped.max_ids must be > 0.")

    rules: list[ReclassRule] = []
    used_output_values: set[int] = set()
    source_id_owners: dict[int, str] = {}
    input_nodata_set = set(options.input_nodata)

    for index, item in enumerate(raw_classes):
        if not isinstance(item, dict):
            raise ValueError(
                f"classes[{index}] must be a mapping with keys: "
                "name, ids, and optional value."
            )

        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"classes[{index}].name is required and must be non-empty.")

        ids_raw = item.get("ids")
        if not isinstance(ids_raw, list) or not ids_raw:
            raise ValueError(
                f"classes[{index}].ids must be a non-empty list of integers."
            )

        try:
            source_ids = tuple(int(value) for value in ids_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"classes[{index}].ids must contain only integers."
            ) from exc

        if any(value < 0 for value in source_ids):
            raise ValueError(f"classes[{index}].ids must be >= 0.")
        if any(value > MAX_CONFIGURED_CATEGORY_ID for value in source_ids):
            highest = max(source_ids)
            raise ValueError(
                f"classes[{index}] contains category ID {highest}, which exceeds the "
                f"safety limit of {MAX_CONFIGURED_CATEGORY_ID}."
            )

        for source_id in source_ids:
            previous_owner = source_id_owners.get(source_id)
            if previous_owner is not None:
                raise ValueError(
                    f"Duplicate source category ID {source_id} appears in both "
                    f"'{previous_owner}' and '{name}'."
                )
            if source_id in input_nodata_set:
                raise ValueError(
                    f"Source category ID {source_id} in class '{name}' conflicts with "
                    "options.input_nodata."
                )
            source_id_owners[source_id] = name

        output_value = int(item.get("value", index + 1))
        _validate_uint8(f"classes[{index}].value", output_value)
        if output_value == options.default_value:
            raise ValueError(
                f"classes[{index}].value ({output_value}) conflicts with "
                f"options.default_value ({options.default_value})."
            )
        if output_value == options.nodata_value:
            raise ValueError(
                f"classes[{index}].value ({output_value}) conflicts with "
                f"options.nodata_value ({options.nodata_value})."
            )
        if output_value in used_output_values:
            raise ValueError(
                f"Duplicate output class value {output_value}; output values must be unique."
            )
        used_output_values.add(output_value)

        rules.append(ReclassRule(name=name, value=output_value, ids=source_ids))

    return rules, options


def _build_reclass_mapper(
        rules: Sequence[ReclassRule], *, default_value: int, nodata_value: int,
        input_nodata: tuple[int, ...], ) -> IntegerLutMapper:
    """Build a uint8 LUT mapper from configured reclassification rules.

    Args:
        rules: Validated reclassification rules.
        default_value: Value assigned to valid but unmapped source IDs.
        nodata_value: Value assigned to source nodata pixels.
        input_nodata: Source values treated as nodata.

    Returns:
        Generic integer LUT mapper configured for reclassification.
    """
    highest_configured_id = max(source_id for rule in rules for source_id in rule.ids)
    lut = np.full(
        highest_configured_id + 1, default_value, dtype=np.dtype(RECLASS_OUTPUT_DTYPE), )
    mapped = np.zeros(highest_configured_id + 1, dtype=np.bool_)

    for rule in rules:
        source_ids = np.asarray(rule.ids, dtype=np.intp)
        lut[source_ids] = rule.value
        mapped[source_ids] = True

    return IntegerLutMapper(
        lut=lut, mapped=mapped, default_value=default_value, nodata_value=nodata_value,
        input_nodata=input_nodata, )


def _derive_alpha_path(output_path: Path, alpha_output: Optional[Path]) -> Path:
    """Derive the alpha output path when one is not explicitly configured."""
    return alpha_output if alpha_output is not None else output_path.with_name(
        f"{output_path.stem}_alpha{output_path.suffix}"
    )


def _prepare_gtiff_profile(
        source_profile: Mapping[str, Any], spec: RasterOutputSpec, ) -> dict[str, Any]:
    """Prepare a generic single-band tiled GeoTIFF profile.

    Args:
        source_profile: Rasterio source profile supplying georeferencing and dimensions.
        spec: Output settings.

    Returns:
        Rasterio profile for the destination GeoTIFF.
    """
    profile: dict[str, Any] = {
        **source_profile, "driver": "GTiff", "dtype": spec.dtype, "count": 1, "nodata": spec.nodata,
        "compress": spec.compress, "tiled": True, "blockxsize": spec.tile_size,
        "blockysize": spec.tile_size,
    }
    if spec.creation_options:
        profile.update(spec.creation_options)
    return profile


def _resolve_output_paths(
        *, src_path: str | Path, out_path: Path, auxiliary_paths: Sequence[Path] = (), ) -> tuple[
    Path, tuple[Path, ...]]:
    """Validate raster output paths and prevent destructive collisions.

    Args:
        src_path: Existing input raster path.
        out_path: Main output GeoTIFF path.
        auxiliary_paths: Optional additional GeoTIFF output paths.

    Returns:
        Resolved main output and auxiliary paths.

    Raises:
        RuntimeError: If an input/output path is invalid or collides.
    """
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
    """Create the output directory and remove a prior file safely."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_unlink(path)


def _block_window_total(dataset, band_index: int = DST_BAND_INDEX) -> Optional[int]:
    """Compute the number of block windows without materializing the iterator."""
    try:
        block_height, block_width = dataset.block_shapes[band_index - 1]
        return math.ceil(dataset.height / block_height) * math.ceil(
            dataset.width / block_width
        )
    except Exception:
        return None


def _safe_unlink(path: Path) -> None:
    """Delete an existing file while refusing to delete a directory."""
    if not path.exists():
        return
    if path.is_dir():
        raise RuntimeError(f"❌ Refusing to delete directory: {path}")
    path.unlink()


def _validate_integer_source_raster(source, *, operation_name: str) -> None:
    """Validate that source band 1 contains integer lookup keys.

    Args:
        source: Open Rasterio source dataset.
        operation_name: Human-friendly operation name for diagnostics.

    Raises:
        ValueError: If source band 1 is not integer-valued.
    """
    source_dtype = np.dtype(source.dtypes[SRC_BAND_INDEX - 1])
    if not np.issubdtype(source_dtype, np.integer):
        raise ValueError(
            f"The {operation_name} source must use an integer data type because "
            f"pixel values are interpreted as lookup keys. Band 1 uses {source_dtype}."
        )


def _source_nodata_value(source) -> Optional[int]:
    """Return band 1 nodata as an integer, failing if metadata is not integral."""
    source_nodata = source.nodata
    if source_nodata is None:
        return None

    integer_nodata = int(source_nodata)
    if source_nodata != integer_nodata:
        raise ValueError(
            f"Band 1 nodata must be an integer category value. Got {source_nodata}."
        )
    return integer_nodata


def _normalize_source_nodata(
        source, configured_nodata: Sequence[int], ) -> tuple[int, ...]:
    """Combine configured and source-metadata nodata values."""
    values = set(configured_nodata)
    source_nodata = _source_nodata_value(source)
    if source_nodata is not None:
        values.add(source_nodata)
    return tuple(sorted(values))


def _count_source_categories(
        source, category_ids: Sequence[int], ) -> dict[int, int]:
    """Count selected category IDs in band 1 without loading the full raster."""
    targets = tuple(sorted(set(category_ids)))
    counts = {category_id: 0 for category_id in targets}
    if not targets:
        return counts

    for _, window in source.block_windows(SRC_BAND_INDEX):
        data = source.read(SRC_BAND_INDEX, window=window)
        for category_id in targets:
            counts[category_id] += int(np.count_nonzero(data == category_id))

    return counts


def _validate_source_categories(
        source, source_check: Optional[SourceCheck], ) -> None:
    """Validate that the source resembles the configured category system."""
    if source_check is None:
        return

    counts = _count_source_categories(
        source, source_check.required_ids + source_check.expected_any_ids, )

    for category_id in source_check.required_ids:
        matches = counts[category_id]
        threshold = 1
        if matches < threshold:
            raise ValueError(
                "Category validation error. "
                f"Category {category_id}: Matches:{matches} Threshold:{threshold}. "
                f"Verify this uses '{source_check.category}' categories."
            )

    expected_present = sum(
        counts[category_id] >= 1 for category_id in source_check.expected_any_ids
    )
    if expected_present < source_check.minimum_expected_any:
        raise ValueError(
            "Category validation error. "
            f"Expected categories present:{expected_present} "
            f"Threshold:{source_check.minimum_expected_any}. "
            f"Expected IDs:{list(source_check.expected_any_ids)}. "
            f"Verify this uses '{source_check.category}' categories."
        )


def _validate_rules_do_not_use_nodata(
        rules: Sequence[ReclassRule], input_nodata: Sequence[int], ) -> None:
    """Fail when a configured source category is also treated as source nodata."""
    nodata_values = set(input_nodata)
    for rule in rules:
        conflicts = sorted(nodata_values.intersection(rule.ids))
        if conflicts:
            raise ValueError(
                f"Class '{rule.name}' uses source category IDs also configured as "
                f"nodata: {conflicts}."
            )


def _write_integer_lut_transform(
        *, src_path: str | Path, out_path: Path, mapper: IntegerLutMapper,
        output_spec: RasterOutputSpec, alpha_spec: Optional[AlphaOutputSpec] = None,
        progress_description: str = "Mapping raster",
        block_observer: Optional[BlockObserver] = None, ) -> None:
    """Apply an integer LUT to a raster and write the result block-by-block.

    This is the reusable raster engine shared by reclassification and future
    table-driven attribute extraction. It has no knowledge of YAML rules, DBF
    fields, category names, or other mapping-source semantics.

    Args:
        src_path: Source integer raster.
        out_path: Destination GeoTIFF.
        mapper: Integer-key lookup mapper.
        output_spec: Main output settings.
        alpha_spec: Optional validity-alpha output.
        progress_description: Progress-bar description.
        block_observer: Optional callback receiving ``(source_data, matched_mask)``
            for command-specific diagnostics such as reporting unmapped IDs.
    """
    import rasterio
    from rasterio.enums import ColorInterp

    auxiliary_paths = (alpha_spec.path,) if alpha_spec is not None else ()
    output_resolved, resolved_auxiliary = _resolve_output_paths(
        src_path=src_path, out_path=out_path, auxiliary_paths=auxiliary_paths, )
    alpha_resolved = resolved_auxiliary[0] if resolved_auxiliary else None

    _prepare_output_path(output_resolved)
    if alpha_resolved is not None:
        _prepare_output_path(alpha_resolved)

    with rasterio.open(src_path) as source:
        _validate_integer_source_raster(source, operation_name="lookup")
        source_profile = source.profile.copy()

        destination_profile = _prepare_gtiff_profile(source_profile, output_spec)

        alpha_profile: Optional[dict[str, Any]] = None
        if alpha_spec is not None:
            alpha_creation_options: dict[str, Any] = (
                {"SPARSE_OK": "YES"} if alpha_spec.sparse_ok else {})
            alpha_profile = _prepare_gtiff_profile(
                source_profile, RasterOutputSpec(
                    dtype=ALPHA_OUTPUT_DTYPE, nodata=alpha_spec.off,
                    tile_size=output_spec.tile_size, compress=output_spec.compress,
                    creation_options=alpha_creation_options or None, ), )

        tile_size = int(output_spec.tile_size)
        output_buffer = np.empty(
            (tile_size, tile_size), dtype=mapper.output_dtype, )
        matched_buffer = np.empty((tile_size, tile_size), dtype=np.bool_)
        alpha_buffer = (
            np.empty((tile_size, tile_size), dtype=np.uint8) if alpha_spec is not None else None)

        with rasterio.open(
                output_resolved, "w", **destination_profile, ) as destination:
            destination_alpha = (rasterio.open(
                alpha_resolved, "w", **alpha_profile
                ) if alpha_resolved is not None and alpha_profile is not None else None)
            if destination_alpha is not None:
                destination_alpha.colorinterp = (ColorInterp.alpha,)

            try:
                windows = (window for _, window in destination.block_windows(DST_BAND_INDEX))
                total = _block_window_total(
                    destination, band_index=DST_BAND_INDEX, )

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
                        alpha_view.fill(alpha_spec.on)
                        nodata_mask = mapper.source_nodata_mask(source_data)
                        if nodata_mask is not None:
                            alpha_view[nodata_mask] = alpha_spec.off
                        destination_alpha.write(
                            alpha_view, window=window, indexes=DST_BAND_INDEX, )
            finally:
                if destination_alpha is not None:
                    destination_alpha.close()


class _UnmappedIdCollector:
    """Collect a bounded sample of unmapped source category IDs."""

    def __init__(self, *, input_nodata: Sequence[int], limit: int) -> None:
        """Initialize the collector.

        Args:
            input_nodata: Source values excluded from unmapped diagnostics.
            limit: Maximum number of distinct IDs to retain.
        """
        self._input_nodata = tuple(input_nodata)
        self._limit = limit
        self._values: set[int] = set()

    @property
    def values(self) -> list[int]:
        """Return collected IDs in ascending order."""
        return sorted(self._values)

    def observe(self, data: np.ndarray, matched: np.ndarray) -> None:
        """Collect unmatched IDs from one processed block."""
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


def _reclassify_and_output(
        src_path: str, out_path: Path, alpha_path: Optional[Path], rules: Sequence[ReclassRule],
        options: ReclassOptions, ) -> None:
    """Reclassify band 1 and write class and optional alpha GeoTIFFs.

    The command-specific work performed here is limited to:

    * normalizing source nodata;
    * validating class rules against nodata;
    * building a uint8 reclassification LUT;
    * configuring optional alpha and unmapped-ID reporting; and
    * invoking the shared integer-LUT raster transform.

    Args:
        src_path: Source categorical raster.
        out_path: Output class GeoTIFF.
        alpha_path: Optional alpha output path.
        rules: Validated reclassification rules.
        options: Validated reclassification options.
    """
    import rasterio

    with rasterio.open(src_path) as source:
        _validate_integer_source_raster(source, operation_name="reclassify")
        normalized_nodata = _normalize_source_nodata(source, options.input_nodata)
        _validate_source_categories(source, options.source_check)

    _validate_rules_do_not_use_nodata(rules, normalized_nodata)

    mapper = _build_reclass_mapper(
        rules, default_value=options.default_value, nodata_value=options.nodata_value,
        input_nodata=normalized_nodata, )

    output_spec = RasterOutputSpec(
        dtype=RECLASS_OUTPUT_DTYPE, nodata=options.nodata_value, tile_size=options.tile_size,
        compress=options.compress, )

    alpha_spec = (AlphaOutputSpec(
        path=alpha_path, on=options.alpha_on, off=options.alpha_off,
        sparse_ok=options.alpha_sparse_ok, ) if alpha_path is not None else None)

    unmapped_collector = (_UnmappedIdCollector(
        input_nodata=normalized_nodata,
        limit=options.max_unmapped_ids, ) if options.report_unmapped else None)

    _write_integer_lut_transform(
        src_path=src_path, out_path=out_path, mapper=mapper, output_spec=output_spec,
        alpha_spec=alpha_spec, progress_description="Reclassifying",
        block_observer=(unmapped_collector.observe if unmapped_collector is not None else None), )

    if unmapped_collector is not None:
        print(
            "*️⃣ Unmapped source category IDs "
            f"(sample up to {options.max_unmapped_ids}): "
            f"{unmapped_collector.values}"
        )
