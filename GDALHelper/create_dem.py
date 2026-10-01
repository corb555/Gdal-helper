from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


VRT_PREFIX = "vrt_"

# Canonical create-dem CLI names that belong to gdalbuildvrt.
# The same names drive:
#   1) argparse registration
#   2) CreateDEMConfig partitioning
#   3) command construction
VRT_ARGUMENTS = {
    "resolution": {
        "flags": ("-vrt_resolution",),
        "kwargs": {
            "dest": f"{VRT_PREFIX}resolution",
            "help": "gdalbuildvrt resolution policy.",
        },
        "gdal_flag": "-resolution",
    },
    "vrtnodata": {
        "flags": ("-vrt_vrtnodata",),
        "kwargs": {
            "dest": f"{VRT_PREFIX}vrtnodata",
            "help": "gdalbuildvrt VRT NoData value.",
        },
        "gdal_flag": "-vrtnodata",
    },
    "strict": {
        "flags": ("-vrt_strict",),
        "kwargs": {
            "dest": f"{VRT_PREFIX}strict",
            "action": "store_true",
            "help": "Pass -strict to gdalbuildvrt.",
        },
        "gdal_flag": "-strict",
    },
}


def create_dem_add_args(parser: argparse.ArgumentParser) -> None:
    """Register the create-dem CLI in one place."""

    parser.add_argument(
        "inputs",
        nargs="+",
        help="Source DEM raster(s).",
    )
    parser.add_argument(
        "output",
        help="Output DEM raster.",
    )

    vrt_group = parser.add_argument_group("VRT")
    for spec in VRT_ARGUMENTS.values():
        vrt_group.add_argument(*spec["flags"], **spec["kwargs"])

    warp_group = parser.add_argument_group("Warp")
    warp_group.add_argument(
        "-r",
        "--resampling",
        help="gdalwarp resampling method.",
    )
    warp_group.add_argument(
        "-t_srs",
        dest="t_srs",
        help="Target spatial reference passed to gdalwarp -t_srs.",
    )
    warp_group.add_argument(
        "-te",
        type=float,
        nargs=4,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="Optional target extent. If omitted, use the full source extent.",
    )
    warp_group.add_argument(
        "-tr",
        type=float,
        nargs=2,
        metavar=("XRES", "YRES"),
        help="Optional target pixel resolution.",
    )
    warp_group.add_argument(
        "-co",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="gdalwarp creation option. May be repeated.",
    )
    warp_group.add_argument(
        "-wo",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="gdalwarp warp option. May be repeated.",
    )
    warp_group.add_argument(
        "-to",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="gdalwarp transformer option. May be repeated.",
    )
    warp_group.add_argument(
        "-dstnodata",
        help="Destination NoData value.",
    )
    warp_group.add_argument(
        "-dstalpha",
        action="store_true",
        help="Create a destination alpha band.",
    )
    warp_group.add_argument(
        "-multi",
        action="store_true",
        help="Enable gdalwarp multithreaded I/O/processing.",
    )
    warp_group.add_argument(
        "-overwrite",
        action="store_true",
        help="Allow replacement of an existing output DEM.",
    )
    warp_group.add_argument(
        "-config",
        action="append",
        nargs=2,
        default=[],
        metavar=("NAME", "VALUE"),
        help="GDAL configuration option. May be repeated.",
    )

    validation_group = parser.add_argument_group("Validation")
    validation_group.add_argument(
        "-min_bytes",
        type=int,
        default=50000,
        help="Optional minimum output file size in bytes.",
    )
    validation_group.add_argument(
        "-min_pixels",
        type=int,
        default=100000,
        help="Optional minimum output pixel count.",
    )
    validation_group.add_argument(
        "-min_coverage",
        type=float,
        default=99.0,
        help="Optional minimum valid-data coverage percentage.",
    )
    validation_group.add_argument(
        "-ignore_warnings",
        action="store_true",
        help="Report quality warnings but continue where possible.",
    )


@dataclass(frozen=True)
class CreateDEMConfig:
    inputs: tuple[Path, ...]
    output: Path
    vrt_options: dict[str, object]
    resampling: str | None
    t_srs: str | None
    te: tuple[float, float, float, float] | None
    tr: tuple[float, float] | None
    creation_options: tuple[str, ...]
    warp_options: tuple[str, ...]
    transformer_options: tuple[str, ...]
    dst_nodata: str | None
    dst_alpha: bool
    multi: bool
    overwrite: bool
    gdal_config: tuple[tuple[str, str], ...]
    min_bytes: int | None
    min_pixels: int | None
    min_coverage: float | None
    ignore_warnings: bool

    def __init__(self, args: argparse.Namespace):
        object.__setattr__(self, "inputs", tuple(Path(p) for p in args.inputs))
        object.__setattr__(self, "output", Path(args.output))
        object.__setattr__(self, "vrt_options", _extract_vrt_options(args))
        object.__setattr__(self, "resampling", args.resampling)
        object.__setattr__(self, "t_srs", args.t_srs)
        object.__setattr__(self, "te", tuple(args.te) if args.te else None)
        object.__setattr__(self, "tr", tuple(args.tr) if args.tr else None)
        object.__setattr__(self, "creation_options", tuple(args.co))
        object.__setattr__(self, "warp_options", tuple(args.wo))
        object.__setattr__(self, "transformer_options", tuple(args.to))
        object.__setattr__(self, "dst_nodata", args.dstnodata)
        object.__setattr__(self, "dst_alpha", bool(args.dstalpha))
        object.__setattr__(self, "multi", bool(args.multi))
        object.__setattr__(self, "overwrite", bool(args.overwrite))
        object.__setattr__(self, "gdal_config", tuple(tuple(v) for v in args.config))
        object.__setattr__(self, "min_bytes", args.min_bytes)
        object.__setattr__(self, "min_pixels", args.min_pixels)
        object.__setattr__(self, "min_coverage", args.min_coverage)
        object.__setattr__(self, "ignore_warnings", bool(args.ignore_warnings))


def _extract_vrt_options(args: argparse.Namespace) -> dict[str, object]:
    options: dict[str, object] = {}
    for name in VRT_ARGUMENTS:
        value = getattr(args, f"{VRT_PREFIX}{name}")
        if value is not None and value is not False:
            options[name] = value
    return options


def _build_vrt_command(
    config: CreateDEMConfig,
    vrt_path: Path,
) -> list[str]:
    command = ["gdalbuildvrt"]

    for name, spec in VRT_ARGUMENTS.items():
        if name not in config.vrt_options:
            continue

        value = config.vrt_options[name]
        flag = spec["gdal_flag"]

        if isinstance(value, bool):
            if value:
                command.append(flag)
        else:
            command.extend([flag, str(value)])

    command.append(str(vrt_path))
    command.extend(str(path) for path in config.inputs)
    return command


def _build_warp_command(
    config: CreateDEMConfig,
    vrt_path: Path,
) -> list[str]:
    command = ["gdalwarp", str(vrt_path), str(config.output)]

    if config.resampling:
        command.extend(["-r", config.resampling])

    if config.t_srs:
        command.extend(["-t_srs", config.t_srs])

    if config.te:
        command.extend(["-te", *(str(v) for v in config.te)])

    if config.tr:
        command.extend(["-tr", *(str(v) for v in config.tr)])

    for option in config.creation_options:
        command.extend(["-co", option])

    for option in config.warp_options:
        command.extend(["-wo", option])

    for option in config.transformer_options:
        command.extend(["-to", option])

    if config.dst_nodata is not None:
        command.extend(["-dstnodata", str(config.dst_nodata)])

    if config.dst_alpha:
        command.append("-dstalpha")

    if config.multi:
        command.append("-multi")

    if config.overwrite:
        command.append("-overwrite")

    for name, value in config.gdal_config:
        command.extend(["--config", name, value])

    return command


def _run_command(command: Sequence[str]) -> None:
    import subprocess

    subprocess.run(command, check=True)


def _print_validation_result(result) -> None:
    from GDALHelper.validate_dem import format_validation_result

    for line in format_validation_result(result):
        print(line)


def _run_pre_validation(config: CreateDEMConfig) -> None:
    """Validate source coverage before building the VRT or running gdalwarp."""
    from GDALHelper.validate_dem import (
        enforce_validation,
        validate_source_coverage,
    )

    result = validate_source_coverage(
        config.inputs,
        target_extent=config.te,
        target_srs=config.t_srs,
    )

    _print_validation_result(result)

    enforce_validation(
        result,
        ignore_warnings=config.ignore_warnings,
    )


def _run_validation(config: CreateDEMConfig) -> None:
    """Validate the generated DEM using the reusable validation module."""
    from GDALHelper.validate_dem import (
        enforce_validation,
        validate_raster,
    )

    result = validate_raster(
        config.output,
        min_bytes=config.min_bytes,
        min_pixels=config.min_pixels,
        min_coverage=config.min_coverage,
    )

    _print_validation_result(result)

    enforce_validation(
        result,
        ignore_warnings=config.ignore_warnings,
    )


def create_dem(config: CreateDEMConfig) -> None:
    """Create one DEM from one or more source rasters."""

    if not config.inputs:
        raise ValueError("At least one input DEM is required.")

    if config.output.exists() and not config.overwrite:
        raise FileExistsError(
            f"Output already exists: '{config.output}'. Use -overwrite to replace it."
        )

    config.output.parent.mkdir(parents=True, exist_ok=True)

    vrt_path = config.output.parent / f".{config.output.stem}.create_dem.vrt"

    try:
        _run_pre_validation(config)
        _run_command(_build_vrt_command(config, vrt_path))
        _run_command(_build_warp_command(config, vrt_path))
        _run_validation(config)
    except Exception:
        # A failed command must not leave a partial final DEM behind.
        config.output.unlink(missing_ok=True)
        raise
    finally:
        vrt_path.unlink(missing_ok=True)
