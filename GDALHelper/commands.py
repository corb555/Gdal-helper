
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import List

from GDALHelper.buffered_vector_mask import BufferedVectorMaskConfig, buffered_vector_mask_add_args, \
    create_buffered_vector_mask
from GDALHelper.color_ramp_hsv import new_color_ramp
from GDALHelper.feather_raster import SIGMA_DEFAULT, feather
from GDALHelper.inpaint_raster import inpaint_raster_add_args, InpaintRasterConfig, inpaint_raster
from GDALHelper.overlay_layers import overlay_layers
from GDALHelper.utils import Command, IOCommand, ConfigurationError, CommandError, FileError, \
    _get_image_dimensions, _get_raster_info, GAUSSIAN_TRUNCATE_DEFAULT, generate_fractal_noise, \
    BASE_SCALE_MIN_PIXELS, smoothstep01, ALPHA_MAX, MIN_FADE_PIXELS
from GDALHelper.git_utils import get_git_hash, set_tiff_version, get_tiff_version
from GDALHelper.lookup_raster import (COMPRESS_DEFAULT, DEFAULT_MAX_LOOKUP_KEY,
                                      DEFAULT_MAX_UNMAPPED_IDS, TILE_SIZE_DEFAULT,
                                      LookupRasterOptions, _parse_default_value,
                                      _parse_input_nodata, lookup_raster, )
from GDALHelper.manifest import generate_manifest
from GDALHelper.reclassify import (_parse_reclass_config, _derive_alpha_path,
                                   _reclassify_and_output, _load_yaml)
from GDALHelper.smooth_categories import smooth_categories_kernel
from GDALHelper.tile_reader import run_tiled_kernel

import numpy as np
from tqdm import tqdm


# ===================================================================
# Command Registry and argparse Subcommands
# ===================================================================

CommandType = type[Command]
REGISTERED_COMMANDS: dict[str, CommandType] = {}


def register_command(name: str):
    """Register a command class under one CLI subcommand name.

    The registry keeps command declaration decoupled from CLI parser creation.
    :func:`add_registered_subparsers` later uses this registry to build native
    argparse subparsers, so each command receives its own help, validation, and
    usage text.

    Args:
        name: User-facing subcommand name.

    Returns:
        Decorator that registers a :class:`Command` subclass.

    Raises:
        ValueError: If the command name is empty or already registered.
        TypeError: If the decorated class is not a :class:`Command` subclass.
    """
    command_name = name.strip()
    if not command_name:
        raise ValueError("Command name cannot be empty.")

    def decorator(cls: CommandType) -> CommandType:
        if not issubclass(cls, Command):
            raise TypeError(
                f"Registered command '{command_name}' must inherit from Command."
            )

        if command_name in REGISTERED_COMMANDS:
            existing = REGISTERED_COMMANDS[command_name]
            raise ValueError(
                f"Command '{command_name}' is already registered by "
                f"{existing.__name__}."
            )

        REGISTERED_COMMANDS[command_name] = cls
        return cls

    return decorator


def _command_summary(command_class: CommandType) -> str | None:
    """Return the first non-empty docstring line for argparse command help."""
    docstring = command_class.__doc__
    if not docstring:
        return None

    return next(
        (line.strip() for line in docstring.splitlines() if line.strip()),
        None,
    )


def add_registered_subparsers(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction:
    """Add all registered GDALHelper commands as argparse subparsers.

    The command registry remains the source of truth for available commands,
    while argparse owns command selection, command-specific help, argument
    validation, and usage reporting.

    Args:
        parser: Top-level ``gdal-helper`` argument parser.

    Returns:
        The argparse subparser action created for the registered commands.
    """
    subparsers = parser.add_subparsers(
        dest="command_name",
        metavar="COMMAND",
        required=True,
    )

    for name, command_class in sorted(
        REGISTERED_COMMANDS.items(),
        key=lambda item: item[0].casefold(),
    ):
        summary = _command_summary(command_class)
        command_parser = subparsers.add_parser(
            name,
            help=summary,
            description=summary,
        )
        command_class.add_arguments(command_parser)
        command_parser.set_defaults(command_class=command_class)

    return subparsers


def build_parser(
    prog: str = "gdal-helper",
    description: str = "Raster and GDAL workflow utilities.",
) -> argparse.ArgumentParser:
    """Build the top-level GDALHelper parser and all command subparsers.

    Args:
        prog: Program name shown in help and usage text.
        description: Top-level CLI description.

    Returns:
        Fully configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
    )
    add_registered_subparsers(parser)
    return parser


# Note: import large packages like rasterio, scipy, etc. lazily inside specific commands
# to avoid forcing users to install them if they only use other functions.

# ================================
# GDAL-Helper Commands
#    IOCommand(Command): transform Input -> Output
# ================================

@register_command("create_dem")
class CreateDEM(Command):
    """Create one validated DEM from one or more source rasters."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        from GDALHelper.create_dem import create_dem_add_args

        create_dem_add_args(parser)

    def execute(self) -> None:
        from GDALHelper.create_dem import CreateDEMConfig, create_dem

        config = CreateDEMConfig(self.args)

        try:
            create_dem(config)
        except FileExistsError as exc:
            # Existing output is an expected user-facing file condition, not
            # an unexpected programming error.
            raise FileError(str(exc)) from exc

        self.print_verbose(f"✅ Wrote validated DEM: {self.args.output}")
        
@register_command("broad_hillshade")
class BroadHillshade(Command):
    """Generate medium/broad terrain shading from a DEM.

    This is the wide-shading companion to ``gdaldem hillshade -igor``. Igor owns
    fine terrain detail; this command produces coherent medium/broad terrain
    massing for later combination by LandWeaver's ``terrain_shading`` operation.

    Output is a single-band uint8 attenuation raster where 255 is neutral / no
    additional shadow and 0 is maximum shadow.
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("dem", help="Source DEM.")
        parser.add_argument("output", help="Output uint8 broad hillshade raster.")

        light_group = parser.add_argument_group("Light direction")
        light_group.add_argument(
            "--azimuth",
            type=float,
            default=315.0,
            help="Light azimuth in degrees clockwise from north. Default: 315.",
        )
        light_group.add_argument(
            "--altitude",
            type=float,
            default=35.0,
            help="Light altitude in degrees above the horizon. Default: 35.",
        )
        light_group.add_argument(
            "--gain",
            type=float,
            default=2.5,
            help=(
                "Fixed terrain exaggeration applied to both smoothed lighting scales. "
                "Default: 2.5."
            ),
        )

        scale_group = parser.add_argument_group("Wide relief scales")
        scale_group.add_argument(
            "--medium-sigma",
            type=float,
            default=5.0,
            help="Gaussian sigma for medium-scale terrain form. Default: 5.",
        )
        scale_group.add_argument(
            "--broad-sigma",
            type=float,
            default=28.0,
            help="Gaussian sigma for broad-scale terrain form. Default: 28.",
        )
        scale_group.add_argument(
            "--medium-weight",
            type=float,
            default=0.65,
            help="Blend weight for medium-scale lighting. Default: 0.65.",
        )
        scale_group.add_argument(
            "--broad-weight",
            type=float,
            default=0.35,
            help="Blend weight for broad-scale lighting. Default: 0.35.",
        )

        processing_group = parser.add_argument_group("Processing")
        processing_group.add_argument(
            "--tile-size",
            type=int,
            default=2048,
            help="Logical output tile size in pixels. Default: 2048.",
        )

    def execute(self) -> None:
        # Lazy import keeps rasterio/scipy optional for commands that do not use them.
        from GDALHelper.broad_hillshade import BroadHillshadeConfig, generate_broad_hillshade

        config = BroadHillshadeConfig(
            azimuth_deg=self.args.azimuth,
            altitude_deg=self.args.altitude,
            gain=self.args.gain,
            medium_sigma_px=self.args.medium_sigma,
            broad_sigma_px=self.args.broad_sigma,
            medium_weight=self.args.medium_weight,
            broad_weight=self.args.broad_weight,
            tile_size_px=self.args.tile_size,
        )

        self.print_verbose(
            f"--- Broad hillshade: azimuth={config.azimuth_deg:.2f}, "
            f"altitude={config.altitude_deg:.2f}, gain={config.gain:.3f}, "
            f"medium={config.medium_sigma_px:.2f}px/{config.medium_weight:.3f}, "
            f"broad={config.broad_sigma_px:.2f}px/{config.broad_weight:.3f} ---"
        )

        generate_broad_hillshade(
            Path(self.args.dem),
            Path(self.args.output),
            config,
        )

        self.print_verbose(f"✅ Wrote broad hillshade raster: {self.args.output}")


@register_command("hillshade_blend")
class HillshadeBlend(Command):
    """Blend a grayscale hillshade onto a color relief using texture shading.

    The blend is primarily multiplicative:

        out = rgb * hill

    Shadow and highlight protection can reduce shading near tonal extremes
    to preserve color saturation and avoid harsh clipping.

    Optional hillshade tone mapping can be applied before blending.
    """

    @staticmethod
    def add_arguments(parser) -> None:
        parser.add_argument("hillshade", help="Input hillshade")
        parser.add_argument("color", help="Input color image (RGB or RGBA)")
        parser.add_argument("output", help="Output path")

        parser.add_argument(
            "--co",
            action="append",
            help="Creation option, e.g. COMPRESS=DEFLATE",
        )

        parser.add_argument(
            "--protect-shadows",
            type=float,
            default=0.2,
            help=(
                "Shadow protection strength in [0..1]. 0 disables. "
                "Typical: 0.2–0.6."
            ),
        )
        parser.add_argument(
            "--protect-highlights",
            type=float,
            default=0.10,
            help=(
                "Highlight protection strength in [0..1]. 0 disables. "
                "Typical: 0.05–0.25."
            ),
        )
        parser.add_argument(
            "--shadow-range",
            type=int,
            nargs=2,
            metavar=("START", "END"),
            default=[0, 60],
            help=(
                "Shadow protection ramp in byte space [0..255]. "
                "Full protection near START, fading to none by END."
            ),
        )
        parser.add_argument(
            "--highlight-range",
            type=int,
            nargs=2,
            metavar=("START", "END"),
            default=[220, 255],
            help=(
                "Highlight protection ramp in byte space [0..255]. "
                "No protection until START, reaching full protection by END."
            ),
        )

        parser.add_argument(
            "--hill-floor",
            type=float,
            default=0.0,
            help=(
                "Minimum hillshade brightness in [0..1]. "
                "0 leaves shadows unchanged."
            ),
        )
        parser.add_argument(
            "--hill-gamma",
            type=float,
            default=1.0,
            help=(
                "Gamma applied to normalized hillshade. "
                "1 leaves unchanged; >1 lifts shadows; <1 deepens shadows."
            ),
        )
        parser.add_argument(
            "--hill-ceil",
            type=float,
            default=1.0,
            help=(
                "Maximum hillshade brightness in [0..1]. "
                "1 leaves highlights unchanged."
            ),
        )
        parser.add_argument(
            "--shade-strength",
            type=float,
            default=0.8,
            help=(
                "Global hillshade strength in [0..1]. "
                "1 applies full shading; lower values reduce shading."
            ),
        )

    def execute(self) -> None:
        from GDALHelper.hillshade_blend import Hillshade
        hillshade = Hillshade(self.args)
        hillshade.blend()
        self.print_verbose(f"✅ Wrote hillshade raster: {self.args.output}")

@register_command("feather")
class FeatherRaster(IOCommand):
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

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        super(FeatherRaster, FeatherRaster).add_arguments(parser)

        parser.add_argument(
            "--sigma",
            type=float,
            default=SIGMA_DEFAULT,
            help=f"Gaussian falloff distance. Default: {SIGMA_DEFAULT}",
        )
        parser.add_argument(
            "--truncate",
            type=float,
            default=GAUSSIAN_TRUNCATE_DEFAULT,
            help=f"Distance multiplier used for tile padding. Default: {GAUSSIAN_TRUNCATE_DEFAULT}",
        )
        parser.add_argument(
            "--tile-size",
            type=int,
            default=TILE_SIZE_DEFAULT,
            help=f"Output tile size. Default: {TILE_SIZE_DEFAULT}",
        )
        parser.add_argument(
            "--band",
            type=int,
            default=1,
            help="Input band to use as the mask source. Default: 1",
        )
        parser.add_argument(
            "--co",
            action="append",
            help="Creation option, e.g. COMPRESS=DEFLATE",
        )

    def transform(self) -> None:
        feather(self.args)


@register_command("overlay_layers")
class OverlayLayers(IOCommand):
    """Overlay raster or vector layers onto a base raster.

    The base raster defines the output grid. Each overlay replaces matching
    pixels in the current result. Overlays are applied from left to right, so
    later overlays take precedence where layers overlap.

    GeoTIFF overlays must already match the base CRS, extent, resolution, and
    pixel alignment. GeoPackage overlays are rasterized onto the base grid.

    By default, every valid non-NoData overlay value is copied. If one or more
    ``--value`` arguments are supplied, only those values are copied.

    Examples:
        gdal-helper overlay_layers \
            EVT_themed.tif EVT_corrections.gpkg \
            -o EVT_corrected.tif \
            --value 1

        gdal-helper overlay_layers \
            base.tif corrections.gpkg extra.tif \
            -o output.tif \
            --value 1 --value 3
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        """Register command-line arguments.

        Args:
            parser: Parser used to register the command arguments.
        """
        parser.add_argument(
            "input",
            help="Base single-band raster defining the  grid.",
        )
        parser.add_argument(
            "overlays",
            nargs="+",
            help="GeoTIFF or GeoPackage overlays, applied left to right.",
        )
        parser.add_argument(
            "output",
            help="Output GeoTIFF.",
        )
        parser.add_argument(
            "--value",
            dest="values",
            action="append",
            type=int,
            help=(
                "Category value to copy from overlays. Repeat to allow multiple "
                "values. If omitted, all valid overlay values are copied."
            ),
        )
        parser.add_argument(
            "--attribute",
            help=(
                "GeoPackage attribute containing the category value. "
            ),
        )
        parser.add_argument(
            "--co",
            action="append",
            help="GeoTIFF creation option, e.g. COMPRESS=DEFLATE.",
        )

    def transform(self) -> None:
        """Run the overlay operation."""
        overlay_layers(self.args)
        self.print_verbose(f"✅ Wrote overlaid raster: {self.args.output}")

@register_command("buffered_vector_mask")
class BufferedVectorMask(Command):
    """Create an aligned binary raster mask from buffered vector features."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        buffered_vector_mask_add_args(parser)

    def execute(self) -> None:
        config = BufferedVectorMaskConfig.from_args(self.args)
        create_buffered_vector_mask(
            config,
            run_command=self._run_command,
            print_verbose=self.print_verbose,
        )


@register_command("inpaint_raster")
class InpaintRaster(Command):
    """Reconstruct selected raster pixels from surrounding valid values."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        inpaint_raster_add_args(parser)

    def execute(self) -> None:
        config = InpaintRasterConfig.from_args(self.args)
        inpaint_raster(config, print_verbose=self.print_verbose)




@register_command("adjust_color_file")
class AdjustColorFile(IOCommand):
    """Updates the HSV values in a GDALDEM color-relief color config file."""

    @staticmethod
    def add_arguments(parser):
        # Call parent to register 'input' and 'output'
        super(AdjustColorFile, AdjustColorFile).add_arguments(parser)

        parser.add_argument("--saturation", type=float, default=1.0, help="Saturation multiplier.")
        parser.add_argument(
            "--shadow-adjust", type=float, default=0.0, help="Brightness adjustment for shadows."
        )
        parser.add_argument(
            "--mid-adjust", type=float, default=0.0, help="Brightness adjustment for mid-tones."
        )
        parser.add_argument(
            "--highlight-adjust", type=float, default=0.0,
            help="Brightness adjustment for highlights."
        )
        parser.add_argument(
            "--min-hue", type=float, default=0.0, help="Minimum hue for adjustment range (0-360)."
        )
        parser.add_argument(
            "--max-hue", type=float, default=0.0, help="Maximum hue for adjustment range (0-360)."
        )
        parser.add_argument(
            "--target-hue", type=float, default=0.0, help="Target hue to shift towards (0-360)."
        )
        parser.add_argument(
            "--elev-adjust", type=float, default=1.0, help="Elevation multiplier."
        )

    def transform(self):
        new_color_ramp(
            self.args.input, self.args.output, saturation_multiplier=self.args.saturation,
            shadow_adjust=self.args.shadow_adjust, mid_adjust=self.args.mid_adjust,
            highlight_adjust=self.args.highlight_adjust, min_hue=self.args.min_hue,
            max_hue=self.args.max_hue, target_hue=self.args.target_hue,
            elev_adjust=self.args.elev_adjust
        )

@register_command("manifest")
class CreateManifest(Command):
    """Creates a reproducibility manifest for all rasters in a directory.

    Usage:
        gdal-helper manifest --dir ./inputs --output ./inputs/manifest.json
        gdal-helper manifest --dir ./inputs --output ./inputs/manifest.json --sources
        ./inputs/sources.json
    """

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("--dir", required=True, help="Directory to scan")
        parser.add_argument("--output", required=True, help="Path to save JSON manifest")
        parser.add_argument(
            "--sources", help="Optional JSON file mapping filenames to URL or provenance objects", )

    def run(self) -> None:
        input_dir = Path(self.args.dir)
        output_file = Path(self.args.output)
        manifest = generate_manifest(input_dir, self.args.sources)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        print(f"✅ Manifest saved to: {output_file}")


@register_command("create_subset")
class CreateSubset(IOCommand):
    """Extracts a smaller section from a large raster file."""

    @staticmethod
    def add_arguments(parser):
        super(CreateSubset, CreateSubset).add_arguments(parser)

        parser.add_argument(
            "--size", type=int, default=4000, help="The width and height of the preview crop."
        )
        parser.add_argument(
            "--x-anchor", type=float, default=0.5,
            help="Horizontal anchor for the crop (0=left, 0.5=center, 1=right)."
        )
        parser.add_argument(
            "--y-anchor", type=float, default=0.5,
            help="Vertical anchor for the crop (0=top, 0.5=center, 1=bottom)."
        )

    def transform(self):
        width, height = _get_image_dimensions(self.args.input)
        if self.args.size > min(width, height):
            x_offset, y_offset, w, h = 0, 0, width, height
        else:
            x_offset = int((width - self.args.size) * self.args.x_anchor)
            y_offset = int((height - self.args.size) * self.args.y_anchor)
            w, h = self.args.size, self.args.size
        command = ["gdal_translate", "-srcwin", str(x_offset), str(y_offset), str(w), str(h),
                   self.args.input, self.args.output]
        self._run_command(command)
        self.print_verbose("--- Subset created. ---")


@register_command("reclassify")
class Reclassify(IOCommand):
    """Reclassify a categorical raster into a compact uint8 class raster.

    The command reduces a large categorical raster into a small number of
    configured output classes.

    - The main output is a single-band uint8 GeoTIFF containing class values.
    - If enabled, a sidecar alpha raster contains 255 where a class matched
      and 0 where no class matched.

    See `_parse_reclass_config` for the YAML schema.
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--config", required=True, help="YAML config describing classes and options."
        )
        parser.add_argument("input", help="Source categorical raster.")
        parser.add_argument(
            "output", help="Output class raster (uint8). Alpha written separately if enabled."
        )

    def transform(self) -> None:
        """Orchestrate config parsing, validation, and output writing."""
        cfg = _load_yaml(Path(self.args.config))
        rules, options = _parse_reclass_config(cfg)

        out_path = Path(self.args.output)
        if options.write_alpha:
            alpha_path = _derive_alpha_path(out_path, options.alpha_output)
        else:
            alpha_path = None

        try:
            _reclassify_and_output(
                src_path=str(self.args.input), out_path=out_path, alpha_path=alpha_path,
                rules=rules, options=options
            )

            self.print_verbose(f"✅ Wrote class raster: {out_path}")
            if alpha_path is not None:
                self.print_verbose(f"✅ Wrote alpha raster: {alpha_path}")

        except Exception:
            out_path.unlink(missing_ok=True)
            if alpha_path is not None:
                alpha_path.unlink(missing_ok=True)
            raise


@register_command("lookup_raster")
class LookupRaster(IOCommand):
    """Create a raster by looking up source pixel values in a DBF or CSV table.

    Each integer source pixel is treated as a lookup key. The matching value
    from the selected table field is written to the output raster.

    Example:

        Value -> Lit_Val

    This is useful for categorical rasters whose pixel values reference an
    external attribute table, such as ArcGIS Raster Attribute Tables
    (`.vat.dbf`).
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--table", required=True, help="Lookup table (.dbf or .csv).", )
        parser.add_argument(
            "--key-field", required=True, help="Table field matching source raster pixel values.", )
        parser.add_argument(
            "--value-field", required=True,
            help="Numeric table field written to the output raster.", )
        parser.add_argument(
            "--default-value", type=_parse_default_value, default=None,
            help=("Output nodata value used when a source ID has no table value. "
                  "A non-conflicting value is selected automatically if omitted."), )
        parser.add_argument(
            "--dtype", dest="output_dtype", default=None,
            help="Optional output dtype, e.g. uint8, uint16, int32, float32.", )
        parser.add_argument(
            "--input-nodata", action="append", default=None,
            help="Additional source nodata ID(s), comma-separated or repeated.", )
        parser.add_argument(
            "--tile-size", type=int, default=TILE_SIZE_DEFAULT,
            help=f"Output GeoTIFF tile size. Default: {TILE_SIZE_DEFAULT}.", )
        parser.add_argument(
            "--compress", default=COMPRESS_DEFAULT,
            help=f"GeoTIFF compression. Default: {COMPRESS_DEFAULT}.", )
        parser.add_argument(
            "--report-unmapped", action="store_true",
            help="Report a sample of source IDs absent from the lookup table.", )
        parser.add_argument(
            "--max-unmapped-ids", type=int, default=DEFAULT_MAX_UNMAPPED_IDS,
            help=("Maximum number of unmapped source IDs to report. "
                  f"Default: {DEFAULT_MAX_UNMAPPED_IDS}."), )
        parser.add_argument(
            "--max-lookup-key", type=int, default=DEFAULT_MAX_LOOKUP_KEY,
            help=("Safety limit for the largest dense lookup key. "
                  f"Default: {DEFAULT_MAX_LOOKUP_KEY}."), )

        parser.add_argument("input", help="Source integer categorical raster.")
        parser.add_argument("output", help="Output raster.")

    def transform(self) -> None:
        """Orchestrate table lookup and output raster creation."""
        out_path = Path(self.args.output)

        options = LookupRasterOptions(
            tile_size=self.args.tile_size, compress=self.args.compress,
            default_value=self.args.default_value, output_dtype=self.args.output_dtype,
            input_nodata=_parse_input_nodata(self.args.input_nodata),
            report_unmapped=self.args.report_unmapped, max_unmapped_ids=self.args.max_unmapped_ids,
            max_lookup_key=self.args.max_lookup_key, )

        lookup_raster(
            src_path=self.args.input, out_path=out_path, table_path=self.args.table,
            key_field=self.args.key_field, value_field=self.args.value_field, options=options, )
        self.print_verbose(f"✅ Wrote lookup raster: {out_path}")



@register_command("smooth_categories")
class SmoothCategories(IOCommand):
    """
    Cleans and smooths categorical rasters using a neighborhood-aware
    'Winner-Take-All' algorithm.

    This command uses a YAML config to identify which
    categorical values should be smoothed and by how much.
    nodata_value
    """

    @staticmethod
    def add_arguments(parser):
        super(SmoothCategories, SmoothCategories).add_arguments(parser)
        parser.add_argument(
            "--config", type=str, required=True,
            help="Path to YAML file containing 'classes' with 'smoothing_radius'."
        )
        parser.add_argument(
            "--claim-threshold", type=float, default=0.2,
            help="Minimum support required for a theme to claim a pixel. Default: 0.2"
        )
        parser.add_argument(
            "--median-threshold", type=float, default=1.5,
            help="Radius threshold to trigger pre-smoothing median filter. Default: 1.5"
        )

    def transform(self):
        # 1. Parse the config using the standard reclass parser
        cfg = _load_yaml(Path(self.args.config))
        rules, options = _parse_reclass_config(cfg)

        # 2. Extract smoothing radii from the raw YAML classes
        # We map the 'value' (output ID) to the 'smoothing_radius'
        smooth_map = {}
        raw_classes = cfg.get("classes", [])

        for i, rule in enumerate(rules):
            # We match the rule to the raw item to get the radius
            raw_item = raw_classes[i]
            radius = float(raw_item.get("smoothing_radius", 0.0))
            smooth_map[rule.value] = radius

        # 3. Calculate required padding (Ghost Halo)
        max_sigma = max(smooth_map.values()) if smooth_map else 0
        pad_size = int(max_sigma * 3.0) + 2

        self.print_verbose(f"Smoothing {len(smooth_map)} categories. Max Sigma: {max_sigma}")

        # 4. Hand off to the Tiled Orchestrator
        run_tiled_kernel(
            input_path=self.args.input, output_path=self.args.output,
            kernel_fn=smooth_categories_kernel, pad=pad_size, tile_size=options.tile_size,
            # Logic Params
            smooth_map=smooth_map, background_id=options.default_value,
            claim_threshold=self.args.claim_threshold, median_threshold=self.args.median_threshold
        )


@register_command("publish")
class Publish(Command):
    """Publish a file locally or via SCP, optionally stamping Git version metadata first.

    Marker file is created  if the publish action completes successfully or --disable is set.
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("source_file", help="The local file to publish.")
        parser.add_argument("directory", help="The destination directory (local or remote).")

        parser.add_argument("--host", help="Optional: destination host (for scp).")
        parser.add_argument("--marker-file", help="Optional marker file path to create on success.")
        parser.add_argument(
            "--disable", action="store_true", help="Skip publish action (no copy/scp)."
        )

        parser.add_argument(
            "--stamp-version", action="store_true",
            help="Embed the current git commit hash into the source file before publishing.", )

        # Optional safety knobs (recommended defaults: safe + explicit)
        parser.add_argument(
            "--rename",
            help="Optional output filename at destination (defaults to source basename).", )
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Allow overwriting an existing destination file.", )

    def execute(self) -> None:
        src_path = Path(self.args.source_file)
        if not src_path.exists():
            raise FileError(f"Source file does not exist: '{src_path}'")

        dest_name = self.args.rename if self.args.rename else src_path.name
        if not dest_name:
            raise ConfigurationError("Destination filename resolved to an empty string.")

        if self.args.stamp_version:
            self._stamp_version_or_fail(src_path)

        if not self.args.disable:
            self._publish_or_fail(src_path, dest_name)
            self.print_verbose("--- Publish complete. ---")
        else:
            self.print_verbose(f"--- Publish is disabled for '{src_path}'. ---")

        if self.args.marker_file:
            self._create_marker_or_fail(Path(self.args.marker_file))

    def _stamp_version_or_fail(self, src_path: Path) -> None:
        self.print_verbose(f"--- Stamping version on '{src_path}' ---")
        git_hash = get_git_hash()
        if not git_hash:
            raise CommandError("Cannot stamp version: Git is unavailable or the source is not inside a Git repository.")

        set_tiff_version(str(src_path), git_hash)

    def _publish_or_fail(self, src_path: Path, dest_name: str) -> None:
        dest_dir = str(self.args.directory)

        if self.args.host and self.args.host != "None":
            self.print_verbose(f"--- Publishing '{src_path}' to remote host {self.args.host} ---")
            remote_target = f"{self.args.host}:{dest_dir.rstrip('/')}/{dest_name}"
            command = ["scp", str(src_path), remote_target]
            # We can't pre-check remote existence safely without ssh; rely on scp failure.
            self._run_command(command)
            return

        # Local copy
        dest_dir_path = Path(dest_dir)
        self.print_verbose(f"--- Publishing '{src_path}' to local directory '{dest_dir_path}' ---")
        if not dest_dir_path.exists():
            raise FileError(f"Destination directory does not exist: '{dest_dir_path}'")
        if not dest_dir_path.is_dir():
            raise FileError(f"Destination is not a directory: '{dest_dir_path}'")

        dest_path = dest_dir_path / dest_name
        if dest_path.exists() and not self.args.overwrite:
            raise FileError(f"Destination already exists: '{dest_path}'. Use --overwrite to replace it.")

        command = ["cp", str(src_path), str(dest_path)]
        self._run_command(command)

    def _create_marker_or_fail(self, marker_path: Path) -> None:
        self.print_verbose(f"--- Creating marker file at '{marker_path}' ---")
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.touch()
        except OSError as exc:
            raise FileError(
                f"Could not create marker file '{marker_path}': {exc}"
            ) from exc
        self.print_verbose("--- Marker file created. ---")


@register_command("add_version")
class AddVersion(Command):
    """Embeds the current git commit hash into a TIFF's metadata."""

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("target_file", help="The TIFF file to stamp with a version.")

    def execute(self):
        git_hash = get_git_hash()
        if not git_hash:
            raise CommandError(
                "Cannot stamp version: Git is unavailable or the target is not inside a Git repository."
            )

        self.print_verbose(
            f"--- Stamping version on '{self.args.target_file}' Version: {git_hash} ---"
        )

        try:
            set_tiff_version(self.args.target_file, git_hash)
        except RuntimeError as exc:
            raise FileError(
                f"Could not write version metadata to '{self.args.target_file}': {exc}"
            ) from exc

        self.print_verbose("--- Version stamping complete. ---")


@register_command("get_version")
class GetVersion(Command):
    """Reads the embedded version hash from a TIFF's metadata."""

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("target_file", help="The TIFF file to inspect.")

    def execute(self):
        # get_tiff_version returns None if file is not TIFF or has no tag
        version_hash = get_tiff_version(self.args.target_file)

        if version_hash:
            print(f"✅ Found Version: {version_hash}")
            if version_hash.endswith("-dirty"):
                print("   ⚠️  This file was built from a repository with uncommitted changes.")
        else:
            print(f"❌ No git version information found in '{self.args.target_file}'.")

@register_command("aligned_rasterize")
class AlignedRasterize(Command):
    """
    Rasterizes a vector layer so the output matches a template raster's
    SRS, extent, and resolution.

    The source vector must already use the same CRS as the template raster.
    """

    @staticmethod
    def add_arguments(parser):
        parser.add_argument(
            "source",
            help="The vector datasource to rasterize (e.g., a GeoPackage)."
        )
        parser.add_argument(
            "template",
            help="The raster file with the desired output grid."
        )
        parser.add_argument(
            "output",
            help="The path for the new, aligned raster output."
        )

        value_group = parser.add_mutually_exclusive_group(required=True)
        value_group.add_argument(
            "-a", "--attribute",
            help="Vector attribute whose value is burned into the raster."
        )
        value_group.add_argument(
            "--burn",
            type=float,
            help="Constant value to burn into all rasterized features."
        )

        parser.add_argument(
            "--init",
            type=float,
            default=0,
            help="Initial value for pixels not covered by features. Default: 0."
        )
        parser.add_argument(
            "--nodata",
            type=float,
            help="Set the output raster NoData value."
        )
        parser.add_argument(
            "--type",
            default="Byte",
            choices=[
                "Byte", "Int8", "UInt16", "Int16",
                "UInt32", "Int32", "UInt64", "Int64",
                "Float32", "Float64",
            ],
            help="Output raster datatype. Default: Byte."
        )
        parser.add_argument(
            "--layer",
            help="Input vector layer name."
        )
        parser.add_argument(
            "--all-touched",
            action="store_true",
            help="Burn all pixels touched by a polygon."
        )
        parser.add_argument(
            "--co",
            action="append",
            metavar="NAME=VALUE",
            help="Creation option for the output driver."
        )

    def execute(self):
        template_info = _get_raster_info(self.args.template)

        x_res, y_res = template_info["resolution"]
        xmin, ymin, xmax, ymax = template_info["extent"]
        srs_wkt = template_info["srs_wkt"]

        x_res = abs(x_res)
        y_res = abs(y_res)

        self.print_verbose(
            f"--- Rasterizing '{self.args.source}' "
            f"to match '{self.args.template}' ---"
        )

        command = [
            "gdal_rasterize",
            "-a_srs", srs_wkt,
            "-te",
            str(xmin), str(ymin), str(xmax), str(ymax),
            "-tr",
            str(x_res), str(y_res),
            "-ot", self.args.type,
            "-init", str(self.args.init),
        ]

        if self.args.attribute:
            command.extend(["-a", self.args.attribute])
        else:
            command.extend(["-burn", str(self.args.burn)])

        if self.args.nodata is not None:
            command.extend(["-a_nodata", str(self.args.nodata)])

        if self.args.layer:
            command.extend(["-l", self.args.layer])

        if self.args.all_touched:
            command.append("-at")

        if self.args.co:
            for option in self.args.co:
                command.extend(["-co", option])

        command.extend([
            self.args.source,
            self.args.output,
        ])

        self._run_command(command)

        self.print_verbose("--- Vector rasterized and aligned successfully. ---")


@register_command("align_raster")
class AlignRaster(Command):
    """
    Resamples a source raster to perfectly match a template raster's
    SRS, extent, and resolution.
    """

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("source", help="The raster file to be aligned (e.g., the mask).")
        parser.add_argument(
            "template", help="The raster file with the desired grid (e.g., the base DEM)."
        )
        parser.add_argument("output", help="The path for the new, aligned output file.")
        parser.add_argument(
            "-r", "--resampling-method", default="bilinear",
            help="Resampling method to use (e.g., near, bilinear, cubic). Default: bilinear."
        )
        parser.add_argument(
            "--co", action="append", metavar="NAME=VALUE",
            help="Creation option for the output driver (e.g., 'COMPRESS=JPEG'). Can be specified "
                 "multiple times."
        )

    def execute(self):
        template_info = _get_raster_info(self.args.template)
        x_res, y_res = template_info["resolution"]
        xmin, ymin, xmax, ymax = template_info["extent"]
        srs_wkt = template_info["srs_wkt"]

        self.print_verbose(f"--- Aligning '{self.args.source}' to match '{self.args.template}' ---")

        command = ["gdalwarp", "-t_srs", srs_wkt, "-te", str(xmin), str(ymin), str(xmax), str(ymax),
                   "-tr", str(x_res), str(y_res), "-r", self.args.resampling_method, ]

        if self.args.co:
            for option in self.args.co:
                command.extend(["-co", option])

        command.extend(
            ["-overwrite", self.args.source, self.args.output]
        )

        self._run_command(command)
        self.print_verbose("--- Raster aligned successfully. ---")


@register_command("masked_blend")
class MaskedBlend(Command):
    """
    Blends two layers using a mask (Windowed).
    """

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("layerA", help="Input layer A")
        parser.add_argument("layerB", help="Input layer B")
        parser.add_argument("mask", help="Mask")
        parser.add_argument("output", help="Output path")
        parser.add_argument("--co", action="append", help="Creation options")

    def execute(self):
        import rasterio

        self.print_verbose(f"--- Masked Blending  to {self.args.output} ---")

        try:
            with rasterio.open(self.args.layerA) as src_a, rasterio.open(
                    self.args.layerB
            ) as src_b, rasterio.open(self.args.mask) as src_m:

                # Validation
                if src_a.width != src_b.width:
                    raise ConfigurationError("Blend input dimensions do not match.")

                # Profile Setup
                profile = src_a.profile.copy()

                # User Options
                if self.args.co:
                    for opt in self.args.co:
                        if '=' in opt:
                            k, v = opt.split('=', 1)
                            profile[k.lower()] = v

                bands_count = src_a.count
                if bands_count == 1:
                    profile['photometric'] = 'MINISBLACK'
                else:
                    profile['photometric'] = 'RGB'

                Path(self.args.output).unlink(missing_ok=True)

                with rasterio.open(self.args.output, 'w', **profile) as dst:

                    # Get windows list for TQDM
                    windows = list(src_a.block_windows(1))
                    total = len(windows)

                    for _, window in tqdm(
                            windows, total=total, unit="block", desc="   Blending", leave=False,
                            mininterval=10.0
                    ):
                        # Read
                        a = src_a.read(window=window)
                        b = src_b.read(window=window)
                        m = src_m.read(1, window=window)  # Read mask as 2D

                        # Math
                        m_f = m.astype('float32') / 255.0
                        m_exp = m_f[None, :, :]  # Broadcast to bands

                        res = (a * m_exp) + (b * (1.0 - m_exp))
                        res_u8 = np.round(res).clip(0, 255).astype('uint8')

                        # Write
                        dst.write(res_u8, window=window)

            print(f"\n✅ Created {self.args.output}")

        except Exception:
            Path(self.args.output).unlink(missing_ok=True)
            raise


SIGMA_CUTOFF = 30.0
SCALE = 4  # Downsample by 4x (or 8x for sigma 80)


@register_command("haze")
class HazeRaster(IOCommand):
    """
    Performs a global 'Gaussian Convolution' (Low-Pass Filter) on a raster.

    This command applies a Gaussian blur across all bands.
    It replaces every pixel value with a weighted average of its neighborhood,
    effectively 'melting' both the interior and exterior of features. It is
    designed to remove high-frequency detail and create smooth, continuous surfaces.

    Usage:
      - Creating 'Hazy' geological transitions or atmospheric effects.
      - General-purpose low-pass filtering to reduce high-frequency data noise.
      - Preparing high-resolution data for use as a soft environmental signal.
      - Softening boundaries where a 'melted' look is preferred over
        a simple perimeter feather.

    Capabilities:
      - Supports multi-band (RGB/RGBA) and single-band rasters.
      - Supports multiple data types (Byte, Float32, etc.).
    """

    @staticmethod
    def add_arguments(parser):
        super(HazeRaster, HazeRaster).add_arguments(parser)
        parser.add_argument(
            "--sigma", type=float, default=2.0,
            help="Standard deviation for Gaussian kernel (in pixels). Default: 2.0"
        )
        parser.add_argument(
            "--normalize", action="store_true",
            help="If set, the output will be stretched back to the full 0-1 (or 0-255) range."
        )
        parser.add_argument(
            "--co", action="append",
            help="Creation options for the output driver (e.g., 'COMPRESS=DEFLATE')."
        )

    def _warn_about_integer_haze_input(self, src) -> None:
        """Warn when integer input is likely to lose the Gaussian transition."""
        from rasterio.enums import Resampling

        source_dtype = np.dtype(src.dtypes[0])
        if not np.issubdtype(source_dtype, np.integer):
            return

        sample_height = min(src.height, 512)
        sample_width = min(src.width, 512)
        sample = src.read(
            1, out_shape=(sample_height, sample_width), resampling=Resampling.nearest,
            masked=True, )

        valid = sample.compressed()
        if valid.size:
            sample_min = int(valid.min())
            sample_max = int(valid.max())

            if sample_max - sample_min <= 1 and sample_max <= 1:
                self.print_verbose(
                    "⚠️ Haze input has very low integer range "
                    f"(sample min={sample_min}, max={sample_max}, dtype={source_dtype.name}). "
                    "Gaussian blur produces fractional values that may be lost when "
                    f"written back to {source_dtype.name}. For a Byte processing mask, "
                    "consider using 0/255 values or a floating-point output."
                )

        # src.nodata = None

        """if src.nodata == 0:
            self.print_verbose(
                "⚠️ Haze input uses nodata=0. This haze operation reads the stored "
                "pixel values directly, so nodata pixels containing 0 participate "
                "numerically in the Gaussian blur. For a processing mask, consider "
                "using 0 as valid background and reserving nodata for areas outside "
                "the data domain."
            )"""

    def transform(self):
        import rasterio
        from rasterio.enums import Resampling
        from scipy.ndimage import gaussian_filter, zoom

        input_path = self.args.input
        output_path = self.args.output
        sigma = self.args.sigma

        if sigma <= 0:
            raise ConfigurationError("--sigma must be greater than 0.")

        with rasterio.open(input_path) as src:
            profile = src.profile.copy()
            bands = src.count
            height = src.height
            width = src.width

            self._warn_about_integer_haze_input(src)

            if sigma > SIGMA_CUTOFF:
                self.print_verbose(
                    f"Large Sigma detected ({sigma}). Using Pyramidal Haze (Scale {SCALE}x)."
                )

                new_h, new_w = height // SCALE, width // SCALE
                new_h, new_w = max(1, new_h), max(1, new_w)

                data_to_blur = src.read(
                    out_shape=(bands, new_h, new_w), resampling=Resampling.bilinear
                ).astype(np.float32)

                sigma_effective = sigma / SCALE
            else:
                self.print_verbose(
                    f"Blurring {bands} band(s) with sigma={sigma}..."
                )
                data_to_blur = src.read().astype(np.float32)
                sigma_effective = sigma

        for band_index in range(bands):
            data_to_blur[band_index] = gaussian_filter(
                data_to_blur[band_index], sigma=sigma_effective, mode="reflect", )

        if sigma > SIGMA_CUTOFF:
            z_h = height / data_to_blur.shape[1]
            z_w = width / data_to_blur.shape[2]

            blurred_data = zoom(
                data_to_blur, (1, z_h, z_w), order=1, )
        else:
            blurred_data = data_to_blur

        if self.args.normalize:
            for band_index in range(bands):
                band_min = blurred_data[band_index].min()
                band_max = blurred_data[band_index].max()

                if band_max - band_min > 1e-6:
                    blurred_data[band_index] = (blurred_data[band_index] - band_min) / (
                                band_max - band_min)

                    if profile["dtype"] == "uint8":
                        blurred_data[band_index] *= 255.0

        profile.update(
            {
                "driver": "GTiff", "tiled": True, "blockxsize": 256, "blockysize": 256,
                "compress": "deflate", "nodata": None,
            }
        )

        if self.args.co:
            for opt in self.args.co:
                if "=" in opt:
                    key, val = opt.split("=", 1)
                    profile[key.lower()] = int(val) if val.isdigit() else val

        Path(output_path).unlink(missing_ok=True)
        try:
            with rasterio.open(output_path, "w", **profile) as dst:
                dst.write(blurred_data.astype(profile["dtype"]))

            self.print_verbose(f"✅ Created blurred raster: {output_path}")

        except rasterio.errors.RasterioIOError as exc:
            Path(output_path).unlink(missing_ok=True)
            raise FileError(
                f"Could not write raster '{output_path}': {exc}"
            ) from exc

        except OSError as exc:
            Path(output_path).unlink(missing_ok=True)
            raise FileError(
                f"File error writing '{output_path}': {exc}"
            ) from exc

        except Exception:
            Path(output_path).unlink(missing_ok=True)
            raise




@register_command("vignette")
class Vignette(IOCommand):
    """
    Adds an Alpha gradient to the edge of a raster, creating a vignette fade.
    This is used so that an overlayed raster blends into the layer under it.

    Parameters:
      --border (float):
          Controls the width of the fade gradient.
          Calculated as a % of the image's smallest dimension (Height or Width).
          Example - 5.0 creates a fade that covers 5% of the image.
          **If 0, the input file is simply copied to the output.**

      --noise (float):
          Adds high-frequency "grain" (dithering) to the fade.
          Calculated as a % of the 'border' size.
          Purpose - Hides digital banding and makes the gradient look smoother.

      --warp (float):
          Adds low-frequency "wiggles" (fractal distortion) to the edge shape.
          Calculated as a % of the 'border' size.
          Purpose -  Breaks up straight lines, making the edge look organic.
          Note -  The visible image area shrinks slightly as warp increases to ensure edges remain
          soft.
    """

    @staticmethod
    def add_arguments(parser):
        super(Vignette, Vignette).add_arguments(parser)
        parser.add_argument(
            "--border", type=float, default=5.0, help="Fade width as a percentage. Default: 5.0%%"
        )
        parser.add_argument(
            "--noise", type=float, default=20.0, help="Noise amplitude. Default: 20%%"
        )
        parser.add_argument(
            "--warp", type=float, default=60.0, help="Warp distortion. Default: 60%%"
        )
        parser.add_argument(
            "--co", action="append",
            help="Creation options for the output driver (e.g., 'COMPRESS=DEFLATE')."
        )
        # ✅ alpha behavior
        parser.add_argument(
            "--replace-alpha", action="store_true",
            help="Replace existing alpha instead of multiplying it by the vignette alpha.", )
        #  random number reproducibility
        parser.add_argument(
            "--seed", type=int, default=None,
            help="Optional RNG seed for reproducible vignette edges.", )
        parser.add_argument(
            "--fade-data", action="store_true",
            help="Physically fade the data bands to black. Required for LandWeaver signals, "
                 "but should be OFF for final visual overlays."
        )

    def transform(self):
        import rasterio
        from scipy.ndimage import distance_transform_edt
        import shutil
        from pathlib import Path

        input_path = self.args.input
        output_path = self.args.output

        if self.args.border < 0:
            raise ConfigurationError("--border cannot be negative.")
        if self.args.noise < 0:
            raise ConfigurationError("--noise cannot be negative.")
        if self.args.warp < 0:
            raise ConfigurationError("--warp cannot be negative.")

        # === 0. Bypass Check ===
        if self.args.border <= 0:
            self.print_verbose(f"--- Border is 0%. Copying '{input_path}' to '{output_path}' ---")
            shutil.copy(input_path, output_path)
            return

        rng = np.random.default_rng(self.args.seed)

        # 1. Open Input
        with rasterio.open(input_path) as src:
            data = src.read()  # shape: (bands, h, w)
            profile = src.profile.copy()
            height, width = int(src.height), int(src.width)
            bands = int(src.count)

            # Extract existing alpha if it exists (the last band)
            existing_alpha = data[-1].copy() if bands in (2, 4) else None

        # 2. Calculate Absolute Pixel Values
        min_dim = min(height, width)
        fade_pixels = max(MIN_FADE_PIXELS, int(min_dim * (self.args.border / 100.0)))
        noise_amt = int(fade_pixels * (self.args.noise / 100.0))
        warp_amt = int(fade_pixels * (self.args.warp / 100.0))

        # 3. Mask Generation (Frame Vignette)
        mask = np.ones((height, width), dtype=np.uint8)
        mask[0, :], mask[-1, :], mask[:, 0], mask[:, -1] = 0, 0, 0, 0
        dist_grid = distance_transform_edt(mask).astype(np.float32)

        # 4. Warp & 5. Noise
        if warp_amt > 0:
            base_scale = max(BASE_SCALE_MIN_PIXELS, float(fade_pixels) * 1.5)
            fractal = generate_fractal_noise(height, width, base_scale, rng=rng)
            dist_grid += (fractal * float(warp_amt)) - float(warp_amt)

        if noise_amt > 0:
            dist_grid -= rng.uniform(0.0, float(noise_amt), (height, width))

        # 6. Normalize and Smooth
        dist_grid = np.clip(dist_grid, 0.0, float(fade_pixels))
        vignette_alpha = (dist_grid / float(fade_pixels)) * ALPHA_MAX
        vignette_alpha = np.round(vignette_alpha).clip(0, 255).astype(np.uint8)

        t = (vignette_alpha.astype(np.float32) / 255.0)
        vignette_alpha = np.round(smoothstep01(t) * 255.0).astype(np.uint8)

        #  Prepare Output data
        out = data.copy()
        v_fader = vignette_alpha.astype(np.float32) / 255.0

        # Handle Data Bands
        if self.args.fade_data:
            # SIGNAL / PREMULTIPLIED MODE: Use this when the raster serves as a
            # mathematical input for modeling, thresholding, or weighted blending.
            # Physically dropping the pixel intensity to zero ensures that
            # downstream calculations correctly interpret the vignette as a
            # loss of signal influence rather than just visual transparency.
            self.print_verbose("--- Fading data bands to black (Signal Mode) ---")
            data_bands_count = bands - 1 if bands in (2, 4) else bands
            for b in range(data_bands_count):
                out[b] = np.round(out[b].astype(np.float32) * v_fader).astype(data.dtype)
        else:
            # VISUAL / STRAIGHT MODE: Use this for final map overlays and
            # UI elements. RGB colors are preserved at full intensity, allowing
            # the Alpha channel to handle transparency. This prevents dark
            # "premultiplication" halos during standard browser or GPU rendering.
            self.print_verbose("--- Preserving RGB colors (Visual Mode) ---")

        # Handle Alpha Channel
        if bands in (2, 4):
            if self.args.replace_alpha:
                out[-1] = vignette_alpha
            else:
                ea = existing_alpha.astype(np.uint16)
                va = vignette_alpha.astype(np.uint16)
                out[-1] = np.round((ea * va) / 255.0).astype(np.uint8)
        else:
            alpha_band = vignette_alpha[np.newaxis, :, :]
            out = np.concatenate([out, alpha_band], axis=0)
            profile.update({"count": bands + 1})

        # Final Profile Update
        out_count = out.shape[0]
        profile.update(
            {
                "driver": "GTiff", "compress": "deflate", "tiled": True, "nodata": 0
                # Fallback hint for older software
            }
        )

        if out_count in (2, 4):
            # ✅ EXPLICIT ALPHA FLAG: Tells GDAL/QGIS that the last band is Alpha
            profile["alpha"] = "yes"

        if profile.get("tiled"):
            profile["blockxsize"], profile["blockysize"] = 256, 256

        # Photometric consistency
        profile["photometric"] = "RGB" if out_count >= 3 else "MINISBLACK"

        # Write and Set Band Metadata
        Path(output_path).unlink(missing_ok=True)
        try:
            with rasterio.open(output_path, "w", **profile) as dst:
                dst.write(out)

                # ✅ SET EXPLICIT COLOR INTERPRETATION
                # This ensures QGIS automatically enables transparency on load
                from rasterio.enums import ColorInterp
                if out_count == 4:
                    dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue,
                        ColorInterp.alpha]
                elif out_count == 2:
                    dst.colorinterp = [ColorInterp.gray, ColorInterp.alpha]
                elif out_count == 3:
                    dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue]
                elif out_count == 1:
                    dst.colorinterp = [ColorInterp.gray]

            self.print_verbose(f"✅ Created {output_path}")
        except rasterio.errors.RasterioIOError as exc:
            Path(output_path).unlink(missing_ok=True)
            raise FileError(
                f"Could not write raster '{output_path}': {exc}"
            ) from exc

        except OSError as exc:
            Path(output_path).unlink(missing_ok=True)
            raise FileError(
                f"File error writing '{output_path}': {exc}"
            ) from exc

        except Exception:
            Path(output_path).unlink(missing_ok=True)
            raise

@register_command("create_output_alpha")
class CreateOutputAlpha(IOCommand):
    """
    Create a single-band uint8 alpha raster.

    The input raster is used only as a template for grid / CRS / transform.
    The output contains:

    - 255 everywhere when --border is 0
    - a vignette alpha mask when --border > 0
    """

    @staticmethod
    def add_arguments(parser):
        super(CreateOutputAlpha, CreateOutputAlpha).add_arguments(parser)
        parser.add_argument(
            "--border",
            type=float,
            default=5.0,
            help="Fade width as a percentage of the smaller image dimension. Default: 5.0%%",
        )
        parser.add_argument(
            "--noise",
            type=float,
            default=20.0,
            help="Noise amplitude as a percentage of fade width. Default: 20%%",
        )
        parser.add_argument(
            "--warp",
            type=float,
            default=60.0,
            help="Warp distortion as a percentage of fade width. Default: 60%%",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="Optional RNG seed for reproducible vignette edges.",
        )
        parser.add_argument(
            "--co",
            action="append",
            help="Creation options for the output driver (e.g., 'COMPRESS=DEFLATE').",
        )

    def transform(self):
        from pathlib import Path

        import rasterio
        from rasterio.enums import ColorInterp
        from scipy.ndimage import distance_transform_edt

        input_path = self.args.input
        output_path = self.args.output

        if self.args.border < 0:
            raise ConfigurationError("--border cannot be negative.")
        if self.args.noise < 0:
            raise ConfigurationError("--noise cannot be negative.")
        if self.args.warp < 0:
            raise ConfigurationError("--warp cannot be negative.")

        rng = np.random.default_rng(self.args.seed)

        # Open input only to inherit grid / CRS / transform / size
        with rasterio.open(input_path) as src:
            profile = src.profile.copy()
            height, width = int(src.height), int(src.width)

        # Output is always a single-band uint8 alpha raster
        profile.update(
            {
                "driver": "GTiff",
                "dtype": "uint8",
                "count": 1,
                "compress": "deflate",
                "tiled": True,
                "nodata": None,
                "photometric": "MINISBLACK",
            }
        )

        if profile.get("tiled"):
            profile["blockxsize"] = 256
            profile["blockysize"] = 256

        if self.args.co:
            for opt in self.args.co:
                if "=" in opt:
                    key, value = opt.split("=", 1)
                    profile[key.lower()] = int(value) if value.isdigit() else value

        # ------------------------------------------------------------------
        # Create alpha mask
        # ------------------------------------------------------------------
        if self.args.border == 0:
            # Fully opaque alpha everywhere
            alpha_mask = np.full((height, width), ALPHA_MAX, dtype=np.uint8)
            self.print_verbose("--- Border is 0%. Creating full opaque alpha mask. ---")
        else:
            min_dim = min(height, width)
            fade_pixels = max(MIN_FADE_PIXELS, int(min_dim * (self.args.border / 100.0)))
            noise_amt = int(fade_pixels * (self.args.noise / 100.0))
            warp_amt = int(fade_pixels * (self.args.warp / 100.0))

            # Base frame distance
            mask = np.ones((height, width), dtype=np.uint8)
            mask[0, :], mask[-1, :], mask[:, 0], mask[:, -1] = 0, 0, 0, 0
            dist_grid = distance_transform_edt(mask).astype(np.float32)

            # Low-frequency warp
            if warp_amt > 0:
                base_scale = max(BASE_SCALE_MIN_PIXELS, float(fade_pixels) * 1.5)
                fractal = generate_fractal_noise(
                    height,
                    width,
                    base_scale,
                    rng=rng,
                )
                dist_grid += (fractal * float(warp_amt)) - float(warp_amt)

            # High-frequency grain
            if noise_amt > 0:
                dist_grid -= rng.uniform(0.0, float(noise_amt), (height, width))

            # Normalize and smooth
            dist_grid = np.clip(dist_grid, 0.0, float(fade_pixels))
            alpha_mask = (dist_grid / float(fade_pixels)) * ALPHA_MAX
            alpha_mask = np.round(alpha_mask).clip(0, 255).astype(np.uint8)

            t = alpha_mask.astype(np.float32) / 255.0
            alpha_mask = np.round(smoothstep01(t) * 255.0).astype(np.uint8)

        # ------------------------------------------------------------------
        # Write output
        # ------------------------------------------------------------------

        out = alpha_mask[np.newaxis, :, :]

        Path(output_path).unlink(missing_ok=True)
        try:
            with rasterio.open(output_path, "w", **profile) as dst:
                dst.write(out)
                dst.colorinterp = [ColorInterp.gray]

            self.print_verbose(f"✅ Created alpha mask: {output_path}")

        except rasterio.errors.RasterioIOError as exc:
            Path(output_path).unlink(missing_ok=True)
            raise FileError(
                f"Could not write raster '{output_path}': {exc}"
            ) from exc

        except OSError as exc:
            Path(output_path).unlink(missing_ok=True)
            raise FileError(
                f"File error writing '{output_path}': {exc}"
            ) from exc

        except Exception:
            Path(output_path).unlink(missing_ok=True)
            raise

@register_command("create_mbtiles")
class CreateMBTiles(IOCommand):
    @staticmethod
    def add_arguments(parser):
        super(CreateMBTiles, CreateMBTiles).add_arguments(parser)
        parser.add_argument("--co", action="append", help="GDAL Creation Options")
        parser.add_argument("--mo", action="append", help="GDAL Metadata Options")
        parser.add_argument("--minzoom", type=int, help="Minimum zoom level")
        parser.add_argument("--maxzoom", type=int, help="Maximum zoom level")
        parser.add_argument("-r", "--resampling", default="CUBIC", help="Resampling algo.")

    def transform(self):
        import time

        levels = self.calculate_levels()
        start = time.perf_counter()
        phase_start = time.perf_counter()
        self.run_mbtiles()
        mbtiles_time = time.perf_counter() - phase_start
        self.print_verbose(f"⏱️ Base MBTiles: {mbtiles_time:.2f}s")

        if levels:
            phase_start = time.perf_counter()
            self.run_gdaladdo(levels)
            overview_time = time.perf_counter() - phase_start
            self.print_verbose(f"⏱️ Overviews: {overview_time:.2f}s")

        phase_start = time.perf_counter()
        self.patch_metadata()
        metadata_time = time.perf_counter() - phase_start
        self.print_verbose(f"⏱️ Metadata: {metadata_time:.2f}s")

        total_time = time.perf_counter() - start
        self.print_verbose(f"⏱️ Total MBTiles: {total_time:.2f}s")

    def calculate_levels(self) -> List[str]:
        """Calculates power-of-two levels based on zoom range."""

        # Auto-calculate if both zooms are provided
        if False: #self.args.minzoom is not None and self.args.maxzoom is not None:
            diff = self.args.maxzoom - self.args.minzoom
            if diff <= 0:
                return []

            # Generate [2, 4, 8, 16, ...] up to the zoom difference
            return [str(2 ** i) for i in range(1, diff + 1)]

        # Fallback to a safe default if no zoom info is provided
        return ["2", "4", "8", "16"]

    def run_gdaladdo(self, levels: List[str]):
        self.print_verbose(
            f"--- Adding Overviews ({self.args.resampling}) | Levels: {' '.join(levels)} ---"
            )
        cmd = ["gdaladdo", "-r", self.args.resampling, self.args.output] + levels
        self._run_command(cmd)

    def patch_metadata(self):
        """
        Make minzoom/maxzoom metadata exactly match the tile pyramid.

        GDAL may already have written these keys, so delete all existing rows
        before inserting the authoritative values.
        """
        import sqlite3

        minzoom = self.args.minzoom
        maxzoom = self.args.maxzoom

        conn = sqlite3.connect(self.args.output)
        try:
            cursor = conn.cursor()

            cursor.execute(
                "DELETE FROM metadata WHERE name IN ('minzoom', 'maxzoom')"
            )

            cursor.execute(
                "INSERT INTO metadata (name, value) VALUES ('minzoom', ?)",
                (str(minzoom),),
            )
            cursor.execute(
                "INSERT INTO metadata (name, value) VALUES ('maxzoom', ?)",
                (str(maxzoom),),
            )

            conn.commit()
            print(f"Set MBTiles metadata zoom range: {minzoom}–{maxzoom}")
        finally:
            conn.close()



    def run_mbtiles(self):
        layer_name = Path(self.args.output).stem
        self.print_verbose(f"--- Generating MBTiles ({self.args.output}) ---")

        cmd = ["gdal_translate", "-of", "MBTiles", "-mo", f"name={layer_name}", "-mo",
               "type=overlay"]

        if self.args.co:
            for opt in self.args.co:
                cmd.extend(["-co", opt])

        if self.args.mo:
            for opt in self.args.mo:
                cmd.extend(["-mo", opt])

        cmd.extend([self.args.input, self.args.output])
        self._run_command(cmd)


@register_command("create_pmtiles")
class CreatePMTiles(IOCommand):
    """
    Converts an MBTiles archive to a PMTiles archive using the 'pmtiles' CLI tool.
    """

    @staticmethod
    def add_arguments(parser):
        super(CreatePMTiles, CreatePMTiles).add_arguments(
            parser
        )  # Add any specific pmtiles args here if needed in the future

    def transform(self):
        # Ensure input is actually an mbtiles file to avoid confused tool output
        if not self.args.input.endswith(".mbtiles"):
            self.print_verbose("⚠️  Warning: Input file does not have .mbtiles extension.")

        # pmtiles convert input.mbtiles output.pmtiles
        cmd = ["pmtiles", "convert", self.args.input, self.args.output]

        self._run_command(cmd)
        self.print_verbose(f"✅ Created PMTiles: {self.args.output}")


@register_command("run")
class Run(Command):
    """
    Passthrough command that executes arbitrary GDAL commands with validation.
    Usage: gdal-helper run gdal_translate -of GTiff ...
    """

    @staticmethod
    def add_arguments(parser):
        # nargs=argparse.REMAINDER collects all remaining args into a list
        parser.add_argument(
            "gdal_cmd", nargs=argparse.REMAINDER,
            help="The full GDAL command to run (e.g. gdal_translate ...)"
        )

    def execute(self):
        if not self.args.gdal_cmd:
            raise ConfigurationError("No command provided to run.")

        self._run_command(self.args.gdal_cmd)


@register_command("validate_raster")
class ValidateRaster(Command):
    """
    Checks if a raster meets minimum size requirements.
    Raises an exception if the file is too small or looks empty.
    """

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("input", help="The raster file to check.")
        parser.add_argument(
            "--min-bytes", type=int, default=1000,
            help="Minimum file size in bytes (Default: 1000)."
        )


    def execute(self):

        input_file = self.args.input
        min_bytes = self.args.min_bytes

        if not os.path.exists(input_file):
            raise FileError(f"Validation failed: file not found: '{input_file}'")

        # 1. Check File Size (Bytes)
        file_size = os.path.getsize(input_file)
        if file_size < min_bytes:
            raise ConfigurationError(
                f"Validation failed: file size is too small.\n"
                f"   File: {input_file}\n"
                f"   Size: {file_size} bytes\n"
                f"   Minimum: {min_bytes} bytes"
            )


@register_command("proximity")
class ProximityTool(IOCommand):
    """
    Custom proximity calculator (osgeo-free).
    Calculates distance from water to nearest land.
    """

    @staticmethod
    def add_arguments(parser):
        super(ProximityTool, ProximityTool).add_arguments(parser)
        parser.add_argument(
            "--targets", type=str, default="0,1,2,3,5,6",
            help="Comma-separated list of target IDs (Land)"
        )
        parser.add_argument(
            "--maxdist", type=float, default=None,
            help="Maximum distance to calculate (pixels). Caps values and optimizes compression."
        )

    def transform(self):
        import rasterio
        from scipy.ndimage import distance_transform_edt
        try:
            target_ids = [int(i.strip()) for i in self.args.targets.split(",")]
        except ValueError as exc:
            raise ConfigurationError(
                "--targets must contain comma-separated integer IDs."
            ) from exc

        if self.args.maxdist is not None and self.args.maxdist < 0:
            raise ConfigurationError("--maxdist cannot be negative.")

        with rasterio.open(self.args.input) as src:
            self.print_verbose(f"📏 Calculating proximity: {src.width}x{src.height}")
            data = src.read(1)

            # 1. Mask: Land=0, Water=1
            mask = np.isin(data, target_ids, invert=True).astype(np.uint8)

            # 2. Compute full Euclidean distance
            dist_map = distance_transform_edt(mask).astype(np.float32)

            # 3. Apply maxdist cap if provided
            if self.args.maxdist is not None:
                self.print_verbose(f"✂️ Capping distance at {self.args.maxdist} pixels")
                dist_map = np.clip(dist_map, 0, self.args.maxdist)

            # 4. Profile with Tiling and Predictor
            profile = src.profile.copy()
            profile.update(
                {
                    'dtype': 'float32', 'count': 1, 'compress': 'deflate', 'tiled': True,
                    'blockxsize': 256, 'blockysize': 256, 'predictor': 3
                    # High compression for float32 gradients
                }
            )

            with rasterio.open(self.args.output, 'w', **profile) as dst:
                dst.write(dist_map, 1)

        self.print_verbose(f"✅ Created Proximity Driver: {self.args.output}")
