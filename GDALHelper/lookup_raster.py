from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    from .raster_lookup import (COMPRESS_DEFAULT, TILE_SIZE_DEFAULT, IntegerLutMapper,
                                RasterOutputSpec, UnmappedIdCollector, normalize_source_nodata,
                                validate_integer_source_raster, write_integer_lut_transform, )
except ImportError:  # Allows direct execution outside a package.
    from raster_lookup import (COMPRESS_DEFAULT, TILE_SIZE_DEFAULT, IntegerLutMapper,
                               RasterOutputSpec, UnmappedIdCollector, normalize_source_nodata,
                               validate_integer_source_raster, write_integer_lut_transform, )

DEFAULT_MAX_LOOKUP_KEY = 10_000_000
DEFAULT_MAX_UNMAPPED_IDS = 50
SUPPORTED_TABLE_EXTENSIONS = frozenset({".dbf", ".csv"})


@dataclass(frozen=True, slots=True)
class LookupRasterOptions:
    """Options for table-driven raster attribute lookup."""

    tile_size: int = TILE_SIZE_DEFAULT
    compress: str = COMPRESS_DEFAULT
    default_value: Optional[int | float] = None
    output_dtype: Optional[str] = None
    input_nodata: tuple[int, ...] = ()
    report_unmapped: bool = False
    max_unmapped_ids: int = DEFAULT_MAX_UNMAPPED_IDS
    max_lookup_key: int = DEFAULT_MAX_LOOKUP_KEY


def _read_lookup_table(
        table_path: Path, *, key_field: str, value_field: str, ) -> pd.DataFrame:
    """Read only the two columns needed to construct the raster LUT."""
    if not table_path.exists():
        raise FileNotFoundError(f"Lookup table not found: {table_path}")
    if table_path.is_dir():
        raise ValueError(f"Lookup table path is a directory: {table_path}")

    suffix = table_path.suffix.lower()
    if suffix not in SUPPORTED_TABLE_EXTENSIONS:
        raise ValueError(
            "Lookup table must be a .dbf or .csv file. "
            f"Got: {table_path}"
        )

    if key_field == value_field:
        raise ValueError("key_field and value_field must be different fields.")

    if suffix == ".dbf":
        try:
            import pyogrio
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "pyogrio is required to read DBF lookup tables "
                "(pip install pyogrio)."
            ) from exc

        try:
            frame = pyogrio.read_dataframe(
                table_path, columns=[key_field, value_field], read_geometry=False, )
        except Exception as exc:
            raise RuntimeError(
                f"Unable to read DBF lookup table '{table_path}': {exc}"
            ) from exc
    else:
        try:
            frame = pd.read_csv(
                table_path, usecols=[key_field, value_field], )
        except ValueError as exc:
            raise ValueError(
                f"CSV does not contain both '{key_field}' and '{value_field}'."
            ) from exc

    missing_fields = [field for field in (key_field, value_field) if field not in frame.columns]
    if missing_fields:
        raise ValueError(
            f"Lookup table is missing required field(s): {missing_fields}. "
            f"Available fields: {list(frame.columns)}"
        )

    if frame.empty:
        raise ValueError("Lookup table contains no rows.")

    return frame[[key_field, value_field]].copy()


def _normalize_integer_keys(series: pd.Series, *, key_field: str) -> np.ndarray:
    """Validate and return lookup keys as int64."""
    if series.isna().any():
        count = int(series.isna().sum())
        raise ValueError(
            f"Lookup key field '{key_field}' contains {count} null value(s)."
        )

    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.isna().any():
        bad = series[numeric.isna()].head(5).tolist()
        raise ValueError(
            f"Lookup key field '{key_field}' must contain integers. "
            f"Example invalid values: {bad}"
        )

    numeric_values = numeric.to_numpy()
    if not np.isfinite(numeric_values).all():
        raise ValueError(
            f"Lookup key field '{key_field}' contains non-finite values."
        )

    integer_values = numeric_values.astype(np.int64)
    if not np.array_equal(numeric_values, integer_values):
        bad_mask = numeric_values != integer_values
        bad = numeric_values[bad_mask][:5].tolist()
        raise ValueError(
            f"Lookup key field '{key_field}' must contain whole-number raster IDs. "
            f"Example invalid values: {bad}"
        )

    if (integer_values < 0).any():
        bad = integer_values[integer_values < 0][:5].tolist()
        raise ValueError(
            f"Lookup key field '{key_field}' must contain non-negative IDs. "
            f"Example invalid values: {bad}"
        )

    return integer_values


def _validate_unique_keys(keys: np.ndarray, *, key_field: str) -> None:
    """Fail when the external table contains duplicate raster keys."""
    unique, counts = np.unique(keys, return_counts=True)
    duplicates = unique[counts > 1]
    if duplicates.size:
        sample = duplicates[:10].tolist()
        raise ValueError(
            f"Lookup key field '{key_field}' contains duplicate IDs. "
            f"Examples: {sample}"
        )


def _numeric_values(
        series: pd.Series, *, value_field: str, ) -> tuple[np.ndarray, np.ndarray]:
    """Return numeric values plus a mask identifying rows with usable values.

    Null table attributes are deliberately treated as undefined mappings rather
    than as output values.
    """
    non_null = ~series.isna()
    if not non_null.any():
        raise ValueError(
            f"Lookup value field '{value_field}' contains no non-null values."
        )

    converted = pd.to_numeric(series[non_null], errors="coerce")
    invalid = converted.isna()
    if invalid.any():
        bad = series[non_null][invalid].head(5).tolist()
        raise ValueError(
            f"Lookup value field '{value_field}' must be numeric. "
            f"Example invalid values: {bad}"
        )

    values = converted.to_numpy()
    if not np.isfinite(values).all():
        raise ValueError(
            f"Lookup value field '{value_field}' contains infinite values."
        )

    return values, non_null.to_numpy(dtype=np.bool_)


def _values_are_integral(values: np.ndarray) -> bool:
    if np.issubdtype(values.dtype, np.integer):
        return True
    return bool(np.equal(values, np.trunc(values)).all())


def _smallest_integer_dtype(minimum: int, maximum: int) -> np.dtype:
    """Choose the smallest practical Rasterio/GDAL integer dtype."""
    if minimum >= 0:
        for dtype in (np.uint8, np.uint16, np.uint32, np.uint64):
            if maximum <= np.iinfo(dtype).max:
                return np.dtype(dtype)
    else:
        for dtype in (np.int8, np.int16, np.int32, np.int64):
            info = np.iinfo(dtype)
            if minimum >= info.min and maximum <= info.max:
                return np.dtype(dtype)

    raise ValueError(
        f"Integer output range [{minimum}, {maximum}] cannot be represented."
    )


def _choose_default_and_dtype(
        values: np.ndarray, *, requested_default: Optional[int | float],
        requested_dtype: Optional[str], ) -> tuple[int | float, np.dtype]:
    """Choose an output nodata/default value and output dtype safely."""
    integral = _values_are_integral(values)

    if requested_dtype is not None:
        try:
            dtype = np.dtype(requested_dtype)
        except TypeError as exc:
            raise ValueError(f"Invalid output dtype: {requested_dtype}") from exc

        if dtype.kind not in {"i", "u", "f"}:
            raise ValueError(
                "output_dtype must be an integer or floating-point dtype."
            )
        if dtype.kind in {"i", "u"} and not integral:
            raise ValueError(
                f"Selected output dtype {dtype} is integer but lookup values "
                "contain fractional values."
            )
    else:
        if integral:
            int_values = values.astype(np.int64)
            value_min = int(int_values.min())
            value_max = int(int_values.max())

            if requested_default is None:
                if not np.any(int_values == 0):
                    default_value: int | float = 0
                else:
                    default_value = -1
            else:
                if isinstance(requested_default, float) and not requested_default.is_integer():
                    raise ValueError(
                        "default_value must be integral for an integer lookup field."
                    )
                default_value = int(requested_default)

            dtype = _smallest_integer_dtype(
                min(value_min, int(default_value)), max(value_max, int(default_value)), )
            return default_value, dtype

        dtype = np.dtype(np.float64)

    if requested_default is None:
        default_value = np.nan if dtype.kind == "f" else 0
    else:
        default_value = requested_default

    if dtype.kind in {"i", "u"}:
        if isinstance(default_value, float) and not default_value.is_integer():
            raise ValueError(
                f"default_value {default_value} is not valid for integer dtype {dtype}."
            )
        default_value = int(default_value)
        info = np.iinfo(dtype)
        if default_value < info.min or default_value > info.max:
            raise ValueError(
                f"default_value {default_value} cannot be represented by {dtype}."
            )

        integer_values = values.astype(np.int64)
        if integer_values.min() < info.min or integer_values.max() > info.max:
            raise ValueError(
                f"Lookup values cannot be represented by requested dtype {dtype}."
            )
    else:
        default_value = float(default_value)
        cast_values = values.astype(dtype)
        if not np.isfinite(cast_values).all():
            raise ValueError(
                f"Lookup values cannot be represented by requested dtype {dtype}."
            )

    return default_value, dtype


def _build_table_mapper(
        frame: pd.DataFrame, *, key_field: str, value_field: str, input_nodata: tuple[int, ...],
        default_value: Optional[int | float], output_dtype: Optional[str], max_lookup_key: int,
) -> \
tuple[IntegerLutMapper, int]:
    """Build the shared IntegerLutMapper from a DBF/CSV table."""
    keys = _normalize_integer_keys(frame[key_field], key_field=key_field)
    _validate_unique_keys(keys, key_field=key_field)

    if keys.size == 0:
        raise ValueError("Lookup table contains no keys.")

    highest_key = int(keys.max())
    if highest_key > max_lookup_key:
        raise ValueError(
            f"Highest lookup key {highest_key} exceeds the configured safety "
            f"limit of {max_lookup_key}. A dense LUT would be too large."
        )

    values, value_present = _numeric_values(
        frame[value_field], value_field=value_field, )
    chosen_default, dtype = _choose_default_and_dtype(
        values, requested_default=default_value, requested_dtype=output_dtype, )

    # A nodata/default value that is also a legitimate mapped value makes the
    # resulting GeoTIFF ambiguous to GIS readers, even though the mapper itself
    # tracks validity independently.
    comparable_values = values.astype(dtype)
    if dtype.kind == "f" and np.isnan(chosen_default):
        collision = False
    else:
        collision = bool(np.any(comparable_values == chosen_default))
    if collision:
        raise ValueError(
            f"Output nodata/default value {chosen_default} also occurs in "
            f"'{value_field}'. Choose a different default_value."
        )

    lut = np.full(highest_key + 1, chosen_default, dtype=dtype)
    mapped = np.zeros(highest_key + 1, dtype=np.bool_)

    valid_keys = keys[value_present]
    valid_values = frame.loc[value_present, value_field]
    numeric_valid_values = pd.to_numeric(valid_values, errors="raise").to_numpy()
    lut[valid_keys] = numeric_valid_values.astype(dtype)
    mapped[valid_keys] = True

    null_value_rows = int((~value_present).sum())

    return (IntegerLutMapper(
        lut=lut, mapped=mapped, default_value=chosen_default, input_nodata=input_nodata, ),
            null_value_rows,)


def lookup_raster(
        src_path: str | Path, out_path: str | Path, *, table_path: str | Path, key_field: str,
        value_field: str, options: Optional[LookupRasterOptions] = None, ) -> None:
    """Create a raster by looking up each integer source value in a table.

    Args:
        src_path: Integer source raster whose pixel values are lookup keys.
        out_path: Output single-band GeoTIFF.
        table_path: DBF or CSV lookup table.
        key_field: Table field corresponding to source raster values.
        value_field: Numeric table field to write to the output raster.
        options: Optional processing settings.
    """
    import rasterio

    opts = options or LookupRasterOptions()
    if opts.tile_size <= 0:
        raise ValueError("tile_size must be > 0.")
    if opts.max_unmapped_ids <= 0:
        raise ValueError("max_unmapped_ids must be > 0.")
    if opts.max_lookup_key <= 0:
        raise ValueError("max_lookup_key must be > 0.")

    src = Path(src_path)
    table = Path(table_path)
    out = Path(out_path)

    frame = _read_lookup_table(
        table, key_field=key_field, value_field=value_field, )

    try:
        with rasterio.open(src) as source:
            validate_integer_source_raster(source, operation_name="lookup_raster")
            normalized_nodata = normalize_source_nodata(
                source, opts.input_nodata, )

        mapper, null_value_rows = _build_table_mapper(
            frame, key_field=key_field, value_field=value_field, input_nodata=normalized_nodata,
            default_value=opts.default_value, output_dtype=opts.output_dtype,
            max_lookup_key=opts.max_lookup_key, )

        collector = (UnmappedIdCollector(
            input_nodata=normalized_nodata,
            limit=opts.max_unmapped_ids, ) if opts.report_unmapped else None)

        write_integer_lut_transform(
            src_path=src, out_path=out, mapper=mapper, output_spec=RasterOutputSpec(
                dtype=mapper.output_dtype.name, nodata=mapper.default_value, tile_size=opts.tile_size,
                compress=opts.compress, ), progress_description=f"Looking up {value_field}",
            block_observer=collector.observe if collector is not None else None, )

        print(
            f"*️⃣ Lookup: {key_field} → {value_field}; "
            f"{int(mapper.mapped.sum())} mapped table keys; "
            f"output dtype {mapper.output_dtype.name}; "
            f"nodata {mapper.default_value}"
        )
        if null_value_rows:
            print(
                f"*️⃣ Table rows with null '{value_field}' treated as unmapped: "
                f"{null_value_rows}"
            )
        if collector is not None:
            print(
                f"*️⃣ Unmapped source IDs "
                f"(sample up to {opts.max_unmapped_ids}): {collector.values}"
            )
        print(f"✅ Wrote lookup raster: {out_path}")
    except Exception:
        out.unlink(missing_ok=True)
        raise


def _parse_input_nodata(values: Optional[Sequence[str]]) -> tuple[int, ...]:
    if not values:
        return ()
    parsed: list[int] = []
    for raw in values:
        for item in raw.split(","):
            item = item.strip()
            if item:
                parsed.append(int(item))
    return tuple(sorted(set(parsed)))


def _parse_default_value(raw: Optional[str]) -> Optional[int | float]:
    if raw is None:
        return None
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"Invalid numeric default value: {raw}"
            ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Create a GeoTIFF by replacing integer source pixel values with "
                     "numeric attributes from a DBF or CSV lookup table.")
    )
    parser.add_argument("input", type=Path, help="Input integer raster.")
    parser.add_argument("output", type=Path, help="Output GeoTIFF.")
    parser.add_argument(
        "--table", required=True, type=Path, help="Lookup table (.dbf or .csv).", )
    parser.add_argument(
        "--key-field", required=True, help="Table field matching source raster pixel values.", )
    parser.add_argument(
        "--value-field", required=True, help="Numeric table field written to the output raster.", )
    parser.add_argument(
        "--default-value", type=_parse_default_value, default=None,
        help=("Output value/nodata used for source IDs absent from the table. "
              "When omitted, a non-conflicting value is selected automatically."), )
    parser.add_argument(
        "--dtype", dest="output_dtype", default=None,
        help="Optional NumPy/Rasterio output dtype, e.g. uint8, uint16, int32.", )
    parser.add_argument(
        "--input-nodata", action="append", default=None,
        help="Additional source nodata ID(s), comma-separated or repeated.", )
    parser.add_argument(
        "--tile-size", type=int, default=TILE_SIZE_DEFAULT,
        help=f"Output GeoTIFF block size. Default: {TILE_SIZE_DEFAULT}.", )
    parser.add_argument(
        "--compress", default=COMPRESS_DEFAULT,
        help=f"GeoTIFF compression. Default: {COMPRESS_DEFAULT}.", )
    parser.add_argument(
        "--report-unmapped", action="store_true",
        help="Report a bounded sample of source IDs absent from the lookup table.", )
    parser.add_argument(
        "--max-unmapped-ids", type=int, default=DEFAULT_MAX_UNMAPPED_IDS,
        help=("Maximum distinct unmapped source IDs to report. "
              f"Default: {DEFAULT_MAX_UNMAPPED_IDS}."), )
    parser.add_argument(
        "--max-lookup-key", type=int, default=DEFAULT_MAX_LOOKUP_KEY,
        help=("Safety limit for the highest dense LUT key. "
              f"Default: {DEFAULT_MAX_LOOKUP_KEY}."), )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    lookup_raster(
        args.input, args.output, table_path=args.table, key_field=args.key_field,
        value_field=args.value_field, options=LookupRasterOptions(
            tile_size=args.tile_size, compress=args.compress, default_value=args.default_value,
            output_dtype=args.output_dtype, input_nodata=_parse_input_nodata(args.input_nodata),
            report_unmapped=args.report_unmapped, max_unmapped_ids=args.max_unmapped_ids,
            max_lookup_key=args.max_lookup_key, ), )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
