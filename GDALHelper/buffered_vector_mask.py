"""Create an aligned binary raster mask from buffered vector features.

The source vector is spatially filtered to the template vicinity, transformed to
an appropriate metric working CRS, buffered in meters, transformed to the
template CRS, and rasterized directly onto the template grid.

Overlapping buffers are intentionally not dissolved. Rasterization provides the
effective union at pixel resolution.
"""

from __future__ import annotations

import argparse
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from GDALHelper.utils import ConfigurationError, FileError


_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_CREATION_OPTIONS = {
    "TILED": "YES",
    "COMPRESS": "DEFLATE",
    "PREDICTOR": "1",
}

CommandRunner = Callable[[list[str]], None]
VerbosePrinter = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class BufferRule:
    """One attribute-specific buffer rule."""

    field: str
    value: str
    meters: float


@dataclass(frozen=True, slots=True)
class BufferedVectorMaskConfig:
    """Validated command configuration for buffered vector mask creation."""

    source: Path
    template: Path
    output: Path
    layer: str
    buffer: float | None
    buffer_rules: tuple[BufferRule, ...]
    working_srs: str | None
    all_touched: bool
    creation_options: tuple[str, ...]

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "BufferedVectorMaskConfig":
        """Build and validate configuration from parsed command-line arguments."""
        constant_buffer, rules = _parse_buffer_spec(args.buffer, args.buffer_rule)
        creation_options = _parse_creation_options(args.co)

        return cls(
            source=Path(args.source),
            template=Path(args.template),
            output=Path(args.output),
            layer=args.layer,
            buffer=constant_buffer,
            buffer_rules=tuple(rules),
            working_srs=args.working_srs,
            all_touched=bool(args.all_touched),
            creation_options=tuple(creation_options),
        )


def buffered_vector_mask_add_args(parser: argparse.ArgumentParser) -> None:
    """Register CLI arguments for ``buffered_vector_mask``."""
    parser.add_argument("source", help="Source vector datasource, e.g. a GeoPackage.")
    parser.add_argument(
        "template",
        help="Raster whose CRS, extent, dimensions, and grid are matched.",
    )
    parser.add_argument("output", help="Output uint8 binary mask raster.")
    parser.add_argument(
        "--layer",
        required=True,
        help="Source vector layer name.",
    )

    buffer_group = parser.add_mutually_exclusive_group(required=True)
    buffer_group.add_argument(
        "--buffer",
        type=float,
        help="Constant buffer distance in meters for every selected feature.",
    )
    buffer_group.add_argument(
        "--buffer-rule",
        action="append",
        nargs=3,
        metavar=("FIELD", "VALUE", "METERS"),
        help=(
            "Attribute-specific buffer rule. Repeat as needed, for example: "
            "--buffer-rule highway motorway 60. If multiple rules match a feature, "
            "the largest buffer distance is used."
        ),
    )

    parser.add_argument(
        "--working-srs",
        help=(
            "Projected CRS used for buffering. If omitted, a local Azimuthal "
            "Equidistant CRS centered on the template is generated automatically."
        ),
    )
    parser.add_argument(
        "--all-touched",
        action="store_true",
        help="Burn every pixel touched by a buffered polygon rather than only pixel centers.",
    )
    parser.add_argument(
        "--co",
        action="append",
        metavar="NAME=VALUE",
        help="GeoTIFF creation option. Can be specified multiple times.",
    )


def create_buffered_vector_mask(
    config: BufferedVectorMaskConfig,
    *,
    run_command: CommandRunner,
    print_verbose: VerbosePrinter,
) -> None:
    """Create a binary raster mask aligned exactly to a template raster."""
    import rasterio
    from rasterio.warp import transform, transform_bounds

    _validate_paths(config)

    max_buffer = (
        config.buffer
        if config.buffer is not None
        else max(rule.meters for rule in config.buffer_rules)
    )

    with rasterio.open(config.template) as src:
        if src.crs is None:
            raise ConfigurationError(f"Template raster has no CRS: '{config.template}'")

        template_crs = src.crs
        template_srs = template_crs.to_string()
        bounds = src.bounds
        width = int(src.width)
        height = int(src.height)
        template_transform = src.transform

        center_x = (bounds.left + bounds.right) / 2.0
        center_y = (bounds.bottom + bounds.top) / 2.0
        lon_values, lat_values = transform(
            template_crs,
            "EPSG:4326",
            [center_x],
            [center_y],
        )
        center_lon = float(lon_values[0])
        center_lat = float(lat_values[0])

        west, south, east, north = transform_bounds(
            template_crs,
            "EPSG:4326",
            bounds.left,
            bounds.bottom,
            bounds.right,
            bounds.top,
            densify_pts=21,
        )

    working_srs = config.working_srs or (
        f"+proj=aeqd +lat_0={center_lat:.12f} +lon_0={center_lon:.12f} "
        "+datum=WGS84 +units=m +no_defs"
    )

    # Expand the source query so a centerline immediately outside the template can
    # still contribute a buffer that intersects the target grid.
    lat_pad = max_buffer / 110_574.0
    cos_lat = max(abs(math.cos(math.radians(center_lat))), 0.05)
    lon_pad = max_buffer / (111_320.0 * cos_lat)
    query_bounds = (
        west - lon_pad,
        south - lat_pad,
        east + lon_pad,
        north + lat_pad,
    )

    where_clause = _build_where_clause(config.buffer_rules) if config.buffer_rules else None
    buffer_expression = _build_buffer_expression(config.buffer, config.buffer_rules)

    config.output.parent.mkdir(parents=True, exist_ok=True)
    config.output.unlink(missing_ok=True)

    print_verbose(
        f"--- Creating buffered vector mask from '{config.source}' "
        f"to match '{config.template}' ---"
    )
    print_verbose(f"--- Buffer working CRS: {working_srs} ---")

    try:
        with tempfile.TemporaryDirectory(prefix="gdal_helper_buffer_mask_") as tmp_dir:
            tmp = Path(tmp_dir)
            working_vectors = tmp / "working.gpkg"
            buffered_working = tmp / "buffered_working.gpkg"
            buffered_target = tmp / "buffered_target.gpkg"

            clip_command = [
                "ogr2ogr",
                "-f", "GPKG",
                str(working_vectors),
                str(config.source),
                config.layer,
                "-spat",
                str(query_bounds[0]),
                str(query_bounds[1]),
                str(query_bounds[2]),
                str(query_bounds[3]),
                "-spat_srs", "EPSG:4326",
                "-t_srs", working_srs,
                "-nln", "source",
                "-nlt", "PROMOTE_TO_MULTI",
                "-lco", "GEOMETRY_NAME=geom",
            ]
            if where_clause:
                clip_command.extend(["-where", where_clause])

            run_command(clip_command)

            buffer_sql = (
                "SELECT ST_Buffer(geom, "
                f"{buffer_expression}) AS geom FROM source"
            )
            if where_clause:
                buffer_sql += f" WHERE {where_clause}"

            run_command([
                "ogr2ogr",
                "-f", "GPKG",
                str(buffered_working),
                str(working_vectors),
                "-dialect", "SQLite",
                "-sql", buffer_sql,
                "-nln", "buffered",
                "-nlt", "PROMOTE_TO_MULTI",
                "-lco", "GEOMETRY_NAME=geom",
            ])

            run_command([
                "ogr2ogr",
                "-f", "GPKG",
                str(buffered_target),
                str(buffered_working),
                "buffered",
                "-t_srs", template_srs,
                "-nln", "buffered",
                "-nlt", "PROMOTE_TO_MULTI",
                "-lco", "GEOMETRY_NAME=geom",
            ])

            rasterize_command = [
                "gdal_rasterize",
                "-burn", "1",
                "-init", "0",
                "-ot", "Byte",
                "-of", "GTiff",
                "-a_srs", template_srs,
                "-te",
                str(bounds.left),
                str(bounds.bottom),
                str(bounds.right),
                str(bounds.top),
                "-ts", str(width), str(height),
                "-l", "buffered",
            ]

            if config.all_touched:
                rasterize_command.append("-at")

            for option in config.creation_options:
                rasterize_command.extend(["-co", option])

            rasterize_command.extend([str(buffered_target), str(config.output)])
            run_command(rasterize_command)

        _validate_output(
            config.output,
            template_crs,
            template_transform,
            width,
            height,
        )

    except Exception:
        config.output.unlink(missing_ok=True)
        raise

    print_verbose(f"✅ Wrote aligned binary mask: {config.output}")


def _validate_paths(config: BufferedVectorMaskConfig) -> None:
    """Validate source/template/output path relationships."""
    if not config.source.is_file():
        raise FileError(f"Source vector not found: '{config.source}'")
    if not config.template.is_file():
        raise FileError(f"Template raster not found: '{config.template}'")

    input_paths = {config.source.resolve(), config.template.resolve()}
    if config.output.resolve() in input_paths:
        raise ConfigurationError("Output must not overwrite an input file.")


def _parse_buffer_spec(
    constant_buffer: float | None,
    raw_rules: Sequence[Sequence[str]] | None,
) -> tuple[float | None, list[BufferRule]]:
    """Validate and normalize constant or attribute-specific buffer rules."""
    if constant_buffer is not None:
        distance = float(constant_buffer)
        if distance <= 0:
            raise ConfigurationError("--buffer must be greater than 0 meters.")
        return distance, []

    rules: list[BufferRule] = []
    for field, value, distance_text in raw_rules or ():
        if not _FIELD_PATTERN.fullmatch(field):
            raise ConfigurationError(
                f"Invalid --buffer-rule field '{field}'. Use a simple attribute name."
            )
        try:
            distance = float(distance_text)
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid buffer distance '{distance_text}' for {field}={value}."
            ) from exc
        if distance <= 0:
            raise ConfigurationError(
                f"Buffer distance for {field}={value} must be greater than 0 meters."
            )
        rules.append(BufferRule(field=field, value=value, meters=distance))

    if not rules:
        raise ConfigurationError("At least one --buffer-rule is required.")

    return None, rules


def _build_where_clause(rules: Sequence[BufferRule]) -> str:
    conditions = [_rule_condition(rule.field, rule.value) for rule in rules]
    return " OR ".join(f"({condition})" for condition in conditions)


def _build_buffer_expression(
    constant_buffer: float | None,
    rules: Sequence[BufferRule],
) -> str:
    if constant_buffer is not None:
        return _format_number(constant_buffer)

    expressions = [
        f"CASE WHEN {_rule_condition(rule.field, rule.value)} "
        f"THEN {_format_number(rule.meters)} ELSE 0 END"
        for rule in rules
    ]

    if len(expressions) == 1:
        return expressions[0]
    return f"MAX({', '.join(expressions)})"


def _rule_condition(field: str, value: str) -> str:
    escaped_value = value.replace("'", "''")
    return f'"{field}" = \'{escaped_value}\''


def _format_number(value: float) -> str:
    return format(value, ".15g")


def _parse_creation_options(options: Sequence[str] | None) -> list[str]:
    merged = dict(_DEFAULT_CREATION_OPTIONS)

    for option in options or ():
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

    return [f"{key}={value}" for key, value in merged.items()]


def _validate_output(output, template_crs, template_transform, width, height) -> None:
    """Validate mask datatype and exact template-grid alignment."""
    import rasterio

    try:
        with rasterio.open(output) as src:
            if src.count != 1:
                raise ConfigurationError(
                    f"Buffered vector mask must have one band; found {src.count}."
                )

            if src.dtypes[0] != "uint8":
                raise ConfigurationError(
                    f"Buffered vector mask must be uint8; found {src.dtypes[0]}."
                )

            if src.crs != template_crs:
                raise ConfigurationError(
                    "Buffered vector mask CRS does not match template."
                )

            if src.width != width or src.height != height:
                raise ConfigurationError(
                    "Buffered vector mask dimensions do not match template."
                )

            if not src.transform.almost_equals(template_transform):
                raise ConfigurationError(
                    "Buffered vector mask grid is not pixel-aligned with template."
                )

    except rasterio.errors.RasterioIOError as exc:
        raise FileError(
            f"Could not validate output raster '{output}': {exc}"
        ) from exc