from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
from typing import Union, Optional

from GDALHelper.blur_raster import _compute_pad
from GDALHelper.color_ramp_hsv import new_color_ramp
from GDALHelper.gdal_helper import Command, IOCommand, COMMAND_REGISTRY, register_command
from GDALHelper.git_utils import get_git_hash, set_tiff_version, get_tiff_version
from GDALHelper.manifest import generate_manifest
from GDALHelper.reclassify import _parse_reclass_config, _validate_no_duplicate_ids, \
    _derive_alpha_path, _build_palette, _reclassify_and_output, _load_yaml
import numpy as np
from tqdm import tqdm


# Note: rasterio, and scipy are imported lazily inside specific commands
# to avoid forcing users to install them if they only use the CLI wrappers.

# ===================================================================
# Utility Functions
# ===================================================================

def _block_window_total(ds, band_index: int = 1) -> Optional[int]:
    """Compute total number of block windows without materializing them."""
    try:
        bh, bw = ds.block_shapes[band_index - 1]  # (block_height, block_width)
        return math.ceil(ds.height / bh) * math.ceil(ds.width / bw)
    except Exception:
        return None


def _get_image_dimensions(filepath: str) -> tuple[int, int]:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Cannot get dimensions: File not found at '{filepath}'")
    try:
        result = subprocess.run(
            ["gdalinfo", "-json", filepath], capture_output=True, text=True, check=True
        )
        info = json.loads(result.stdout)
        return info['size']
    except Exception as e:
        raise RuntimeError(
            f"Failed to get dimensions for {filepath}. Is gdalinfo in your PATH? Error: {e}"
        )


def _get_raster_info(filepath: str) -> dict:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Cannot get raster info: File not found at '{filepath}'")
    try:
        result = subprocess.run(
            ["gdalinfo", "-json", filepath], capture_output=True, text=True, check=True
        )
        info = json.loads(result.stdout)

        resolution = (info['geoTransform'][1], info['geoTransform'][5])
        srs_wkt = info['coordinateSystem']['wkt']
        corners = info['cornerCoordinates']
        xmin = min(c[0] for c in corners.values())
        xmax = max(c[0] for c in corners.values())
        ymin = min(c[1] for c in corners.values())
        ymax = max(c[1] for c in corners.values())

        return {
            "resolution": resolution, "extent": (xmin, ymin, xmax, ymax), "srs_wkt": srs_wkt
        }
    except Exception as e:
        raise RuntimeError(
            f"Failed to get raster info for {filepath}. Is gdalinfo in your PATH? Error: {e}"
        )


# ================================
# GDAL-Helper Commands
#    IOCommand(Command): transform Input -> Output
# ================================

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
        self.print_verbose(
            f"--- Creating subset from '{self.args.input}' to '{self.args.output}' ---"
        )
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
    """Reclassify a categorical raster into a compact uint8 class raster (optional palette + alpha).

    This command is designed to reduce large categorical rasters (e.g., LandFire EVT/FBFM/etc.)
    into a small number of derived classes, stored as uint8 values.

    - The main OUTPUT is a 1-band uint8 raster containing class values.
    - If enabled (default), a sidecar alpha raster is written with 255 where any rule matched,
      and 0 where no rule matched.
    - If any `rgb` entries exist in the config, an embedded GeoTIFF palette is written
      so tools like QGIS can render it as a paletted/classified raster.

    See `_parse_reclass_config` docstring for the YAML schema.
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

        _validate_no_duplicate_ids(rules)

        out_path = Path(self.args.output)
        if options.write_alpha:
            alpha_path = _derive_alpha_path(out_path, options.alpha_output)
        else:
            alpha_path = None

        palette = _build_palette(rules)

        try:
            _reclassify_and_output(
                src_path=str(self.args.input), out_path=out_path, alpha_path=alpha_path,
                rules=rules, options=options, palette=palette, )

            if hasattr(self, "print_verbose"):
                self.print_verbose(f"✅ Wrote class raster: {out_path}")
                if alpha_path is not None:
                    self.print_verbose(f"✅ Wrote alpha raster: {alpha_path}")

        except Exception:
            out_path.unlink(missing_ok=True)
            if alpha_path is not None:
                alpha_path.unlink(missing_ok=True)
            raise


@register_command("publish")
class Publish(Command):
    """Publish a file locally or via SCP, optionally stamping Git version metadata first.

    Marker file is created only if the publish action completes successfully.
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
            raise RuntimeError(f"❌ Source file does not exist: {src_path}")

        dest_name = self.args.rename if self.args.rename else src_path.name
        if not dest_name:
            raise RuntimeError("❌ Destination filename resolved to empty string.")

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
            raise RuntimeError("❌ Cannot stamp version: not in a git repo or git not available.")
        # Optional: if you ever want to forbid dirty in Publish, enforce here.
        # if git_hash.endswith("-dirty"):
        #     raise RuntimeError("❌ Cannot stamp version: repo has uncommitted changes.")
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
            raise RuntimeError(f"❌ Destination directory does not exist: {dest_dir_path}")
        if not dest_dir_path.is_dir():
            raise RuntimeError(f"❌ Destination is not a directory: {dest_dir_path}")

        dest_path = dest_dir_path / dest_name
        if dest_path.exists() and not self.args.overwrite:
            raise RuntimeError(f"❌ Destination exists (use --overwrite to allow): {dest_path}")

        command = ["cp", str(src_path), str(dest_path)]
        self._run_command(command)

    def _create_marker_or_fail(self, marker_path: Path) -> None:
        self.print_verbose(f"--- Creating marker file at '{marker_path}' ---")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.touch()
        self.print_verbose("--- Marker file created. ---")


@register_command("add_version")
class AddVersion(Command):
    """Embeds the current git commit hash into a TIFF's metadata."""

    @staticmethod
    def add_arguments(parser):
        parser.add_argument("target_file", help="The TIFF file to stamp with a version.")

    def execute(self):
        git_hash = get_git_hash()
        self.print_verbose(
            f"--- Stamping version on '{self.args.target_file}' Version: {git_hash} ---"
        )
        try:
            set_tiff_version(self.args.target_file, git_hash)
            self.print_verbose("--- Version stamping complete. ---")
        except RuntimeError as e:
            # Catch the error from set_tiff_version to print a clean message
            print(str(e))
            # Re-raise if you want the build pipeline to actually fail
            raise


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

        self.print_verbose(f"--- Blending (Windowed) to {self.args.output} ---")

        try:
            with rasterio.open(self.args.layerA) as src_a, rasterio.open(
                    self.args.layerB
            ) as src_b, rasterio.open(self.args.mask) as src_m:

                # Validation
                if src_a.width != src_b.width:
                    raise ValueError("Dimensions mismatch.")

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

        except Exception as e:
            print(f"\n❌ Blend Failed: {e}")
            Path(self.args.output).unlink(missing_ok=True)
            raise


TILE_SIZE_DEFAULT = 256
COMPRESS_DEFAULT = "deflate"
SIGMA_DEFAULT = 1.0
PAD_TRUNCATE_DEFAULT = 2.5  # 2.5*sigma is usually plenty for cartographic masks


@register_command("blur")
class BlurRaster(IOCommand):
    """Applies Gaussian Blur to a raster using windowed processing.

    Optimized for sparse masks:
      - Skips work if the *padded* read window is all zeros (safe).
      - Uses a smaller halo (truncate=3*sigma) by default.
      - Preallocates scratch buffers based on band count and halo size.
      - Uses SciPy output= to avoid allocations.
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        super(BlurRaster, BlurRaster).add_arguments(parser)
        parser.add_argument("--sigma", type=float, default=SIGMA_DEFAULT)
        # Optional knob; keep default fast/sane.
        parser.add_argument("--truncate", type=float, default=PAD_TRUNCATE_DEFAULT)
        # Allow overriding compression/tile size if you ever need it.
        parser.add_argument("--compress", type=str, default=COMPRESS_DEFAULT)
        parser.add_argument("--tile-size", type=int, default=TILE_SIZE_DEFAULT)

    def transform(self) -> None:
        # Late imports keep dependencies local to this command.
        from pathlib import Path

        import numpy as np
        import rasterio
        from rasterio.enums import ColorInterp
        from rasterio.windows import Window
        from scipy.ndimage import distance_transform_edt
        from tqdm import tqdm

        sigma = float(self.args.sigma)
        truncate = float(self.args.truncate)
        pad = _compute_pad(sigma, truncate)

        tile_size = int(self.args.tile_size)
        if tile_size <= 0:
            raise ValueError("tile-size must be > 0.")

        out_path = Path(self.args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        self.print_verbose(
            f"--- Edge-feather '{self.args.input}' (sigma={sigma}, truncate={truncate}) ---"
        )

        # For mask feathering, treat "absence" as 0.
        ALPHA_OFF = 0
        ALPHA_ON = 255

        try:
            with rasterio.open(self.args.input) as src:
                if int(src.count) != 1:
                    raise RuntimeError(
                        "❌ edge-feather blur expects a single-band mask/alpha raster. "
                        f"Found {src.count} bands."
                    )

                # Output is a single-band uint8 alpha-like raster.
                profile = src.profile.copy()
                profile.update(
                    {
                        "driver": "GTiff", "dtype": "uint8", "count": 1, "nodata": ALPHA_OFF,
                        "compress": "deflate", "tiled": True, "blockxsize": tile_size,
                        "blockysize": tile_size, "SPARSE_OK": "YES",
                    }
                )

                # Remove stale output (fail-fast on directories is handled by your guard helpers
                # elsewhere).
                out_path.unlink(missing_ok=True)

                with rasterio.open(out_path, "w", **profile) as dst:
                    # Hint QGIS that this is alpha-like.
                    dst.colorinterp = (ColorInterp.alpha,)

                    total = _block_window_total(dst, band_index=1)

                    # Preallocate max buffers (views for edge tiles)
                    read_h_max = tile_size + 2 * pad
                    read_w_max = tile_size + 2 * pad

                    scratch_u8_max = np.empty((read_h_max, read_w_max), dtype=np.uint8)
                    out_full_u8_max = np.empty((read_h_max, read_w_max), dtype=np.uint8)
                    out_tile_u8_max = np.empty((tile_size, tile_size), dtype=np.uint8)

                    # distance_transform_edt output (float64 is expected/typical)
                    dist_max = np.empty((read_h_max, read_w_max), dtype=np.float64)
                    alpha_f_max = np.empty((read_h_max, read_w_max), dtype=np.float32)

                    inv_two_sigma2 = np.float32(1.0 / (2.0 * sigma * sigma))

                    win_iter = (w for _, w in dst.block_windows(1))
                    for window in tqdm(
                            win_iter, total=total, unit="block", desc="   Feathering", leave=False,
                            mininterval=10.0, ):
                        out_h = int(window.height)
                        out_w = int(window.width)
                        read_h = out_h + 2 * pad
                        read_w = out_w + 2 * pad

                        scratch_u8 = scratch_u8_max[:read_h, :read_w]
                        out_full_u8 = out_full_u8_max[:read_h, :read_w]
                        out_tile_u8 = out_tile_u8_max[:out_h, :out_w]
                        dist = dist_max[:read_h, :read_w]
                        alpha_f = alpha_f_max[:read_h, :read_w]

                        read_window = Window(
                            col_off=window.col_off - pad, row_off=window.row_off - pad,
                            width=read_w, height=read_h, )

                        # Read padded mask into scratch.
                        # boundless fill uses ALPHA_OFF to make edges deterministic.
                        data = src.read(
                            1, window=read_window, boundless=True, fill_value=ALPHA_OFF, )
                        np.copyto(scratch_u8, data, casting="unsafe")

                        # ✅ Safe skip: padded area has no signal => output tile is all-off.
                        # For sparse GTiff, don't write empty tiles.
                        if not np.any(scratch_u8):
                            continue

                        # feature = True where mask is on
                        feature = scratch_u8 != ALPHA_OFF

                        # If everything is on, output is all-on (still sparse-unfriendly, but rare).
                        if feature.all():
                            out_tile_u8.fill(ALPHA_ON)
                            dst.write(out_tile_u8, window=window, indexes=1)
                            continue

                        # Compute distance for background pixels to nearest feature pixel.
                        # distance_transform_edt computes distances for non-zero pixels to
                        # nearest zero.
                        # Use (~feature): background True, feature False (zeros).
                        distance_transform_edt(~feature, distances=dist)

                        # alpha_f = exp(-(d^2)/(2*sigma^2)) * 255
                        np.multiply(dist, dist, out=alpha_f, casting="unsafe")  # alpha_f = d^2
                        alpha_f *= -inv_two_sigma2  # alpha_f = -(d^2)/(2*s^2)
                        np.exp(alpha_f, out=alpha_f)  # alpha_f = exp(...)
                        alpha_f *= np.float32(255.0)

                        # Force feature pixels to fully on
                        alpha_f[feature] = np.float32(255.0)

                        # Cast to uint8 into padded output buffer
                        np.clip(alpha_f, 0.0, 255.0, out=alpha_f)
                        np.copyto(out_full_u8, alpha_f, casting="unsafe")

                        # Crop padded -> tile
                        cropped = out_full_u8[pad: pad + out_h, pad: pad + out_w]
                        np.copyto(out_tile_u8, cropped, casting="unsafe")

                        # Keep output sparse: skip writing all-off tiles
                        if not np.any(out_tile_u8):
                            continue

                        dst.write(out_tile_u8, window=window, indexes=1)

            self.print_verbose(f"✅ Created Edge Feather: {out_path}")

        except Exception:
            out_path.unlink(missing_ok=True)
            raise


@register_command("hillshade_blend")
class HillshadeBlend(Command):
    """Blend a grayscale hillshade onto a color relief using texture-shading logic.

    The blend is primarily a multiplicative shading (like Photoshop "Multiply"):

        out = rgb * hill

    To preserve color saturation and avoid harsh clipping in deep shadows and bright highlights,
    this command can reduce shading near extremes using smooth "protection" ramps.

    Protection model (intuitive):
        multiplier = hill + w * (1 - hill)

    where w is a protection weight (0..1). When w=0, you get standard shading. When w=1, the
    multiplier becomes 1 (no shading), so the original color is preserved.

    Optional hillshade tone mapping (floor/gamma/ceil) can be applied before blending to lift
    blacks or tame highlights without adding extra pipeline steps.

    Args:
        hillshade: Input hillshade raster (1-band preferred). Can be uint8/uint16/float.
        color: Input RGB/RGBA color raster.
        output: Output path.

    Raises:
        ValueError: If dimensions mismatch or parameters are invalid.
        RuntimeError: If processing fails and the output cannot be written.
    """

    # ---------- Named constants ----------
    _BYTE_MAX = 255.0
    _DEFAULT_BLOCK_SIZE = 256
    _HILL_FLOAT_ASSUME_MAX = 1.0
    _HILL_FLOAT_MAX_CUTOFF = 1.5  # if sample max <= this, treat float hillshade as 0..1
    _HILL_SAMPLE_WINDOWS = 6  # small sample for robust normalization
    _NODATA_INPAINT_SEARCH_DIST = 100.0

    @staticmethod
    def add_arguments(parser) -> None:
        parser.add_argument("hillshade", help="Input Hillshade")
        parser.add_argument("color", help="Input Color Image (RGB or RGBA)")
        parser.add_argument("output", help="Output path")
        parser.add_argument(
            "--co", action="append", help="Creation options (e.g., COMPRESS=DEFLATE)"
        )

        # v2 API: protection is strength + range (smooth ramps)
        parser.add_argument(
            "--protect-shadows", type=float, default=0.2,
            help="Shadow protection strength in [0..1]. 0 disables. "
                 "Typical: 0.2–0.6 to prevent inky blacks.", )
        parser.add_argument(
            "--protect-highlights", type=float, default=0.10,
            help="Highlight protection strength in [0..1]. 0 disables. "
                 "Typical: 0.05–0.25 to prevent washed highlights.", )
        parser.add_argument(
            "--shadow-range", type=int, nargs=2, metavar=("START", "END"), default=[0, 60],
            help="Shadow protection ramp in hillshade byte-space [0..255]. "
                 "Full protection near START, fades to none by END. Example: 0 60.", )
        parser.add_argument(
            "--highlight-range", type=int, nargs=2, metavar=("START", "END"), default=[220, 255],
            help="Highlight protection ramp in hillshade byte-space [0..255]. "
                 "No protection until START, ramps to full by END. Example: 225 255.", )

        # Optional hillshade tone mapping (no behavior change by default)
        parser.add_argument(
            "--hill-floor", type=float, default=0.0,
            help="Lift shadows by enforcing a minimum hillshade brightness in [0..1]. "
                 "0 leaves unchanged; ~0.06–0.12 is common for harsh hillshades.", )
        parser.add_argument(
            "--hill-gamma", type=float, default=1.0,
            help="Gamma curve applied to normalized hillshade in [0..1]. "
                 "1 leaves unchanged; >1 lifts shadows; <1 deepens shadows.", )
        parser.add_argument(
            "--hill-ceil", type=float, default=1.0,
            help="Optional maximum hillshade brightness in [0..1] after tone mapping. "
                 "1 leaves unchanged; <1 can reduce blown highlights.", )
        parser.add_argument(
            "--shade-strength", type=float, default=0.8,
            help="Global hillshade strength in [0..1]. "
                 "1 applies full shading; lower values lift shadows everywhere. "
                 "Typical: 0.55–0.85 (snow/white terrain often likes 0.55–0.70).", )

    def execute(self) -> None:
        import rasterio

        self.print_verbose(
            f"--- Blending '{self.args.hillshade}' + '{self.args.color}' (Windowed) ---"
        )

        out_path = Path(self.args.output)
        try:
            with rasterio.open(self.args.hillshade) as src_h, rasterio.open(
                    self.args.color
            ) as src_c:
                if src_h.width != src_c.width or src_h.height != src_c.height:
                    raise ValueError("Source dimensions do not match.")

                profile = self._setup_profile(src_c)

                out_path.unlink(missing_ok=True)

                with rasterio.open(out_path, "w", **profile) as dst:
                    self._blend_all_windows(src_h, src_c, dst)

            print(f"\n✅ Created {self.args.output}")
        except Exception as exc:
            print(f"\n❌ Hillshade Blend Failed: {exc}")
            out_path.unlink(missing_ok=True)
            raise

    def _setup_profile(self, src_c):
        """Prepare output raster profile/metadata based on the color source."""
        profile = src_c.profile.copy()
        has_alpha = (src_c.count == 4)
        out_count = 4 if has_alpha else 3

        profile.update(
            {
                "driver": "GTiff", "count": out_count, "dtype": "uint8", "compress": "deflate",
                "tiled": True, "blockxsize": self._DEFAULT_BLOCK_SIZE,
                "blockysize": self._DEFAULT_BLOCK_SIZE, "photometric": "RGB",
            }
        )

        # Apply creation options from CLI
        if self.args.co:
            for opt in self.args.co:
                if "=" not in opt:
                    continue
                k, v = opt.split("=", 1)
                key = k.strip().lower()
                val = v.strip()
                profile[key] = int(val) if val.isdigit() else val

        # Force YCBCR for JPEG 3-band only
        if profile.get("compress") == "jpeg" and out_count == 3:
            if "PHOTOMETRIC" not in str(self.args.co).upper():
                profile["photometric"] = "YCBCR"
        else:
            profile["photometric"] = "RGB"

        return profile

    def _blend_all_windows(self, src_h, src_c, dst) -> None:
        """Iterate over windows and write blended output."""
        self._validate_args()

        # Determine nodata handling: only inpaint if nodata is actually defined
        self.nodata_val = src_h.nodata
        self.has_nodata = (self.nodata_val is not None)

        # Robust hillshade normalization denominator
        self.hill_den = self._infer_hillshade_denominator(src_h, src_c)

        # Precompute protection ramps in normalized space (0..1)
        s0, s1 = self.args.shadow_range
        h0, h1 = self.args.highlight_range
        self.shadow_start = s0 / self._BYTE_MAX
        self.shadow_end = s1 / self._BYTE_MAX
        self.highlight_start = h0 / self._BYTE_MAX
        self.highlight_end = h1 / self._BYTE_MAX
        self.protect_shadows = float(self.args.protect_shadows)
        self.protect_highlights = float(self.args.protect_highlights)

        # Tone mapping params
        self.hill_floor = float(self.args.hill_floor)
        self.hill_gamma = float(self.args.hill_gamma)
        self.hill_ceil = float(self.args.hill_ceil)

        windows = list(src_c.block_windows(1))
        total = len(windows)

        for _, window in tqdm(
                windows, total=total, unit="block", desc="   Blending", leave=False,
                mininterval=10.0
        ):
            chunk = self._process_single_chunk(src_h, src_c, window)
            if chunk is not None:
                dst.write(chunk, window=window)

    def _validate_args(self) -> None:
        """Validate user parameters."""
        if not (0.0 <= self.args.protect_shadows <= 1.0):
            raise ValueError("--protect-shadows must be in [0..1].")
        if not (0.0 <= self.args.protect_highlights <= 1.0):
            raise ValueError("--protect-highlights must be in [0..1].")

        s0, s1 = self.args.shadow_range
        h0, h1 = self.args.highlight_range
        if not (0 <= s0 <= 255 and 0 <= s1 <= 255 and s0 < s1):
            raise ValueError("--shadow-range must be two ints in [0..255] with START < END.")
        if not (0 <= h0 <= 255 and 0 <= h1 <= 255 and h0 < h1):
            raise ValueError("--highlight-range must be two ints in [0..255] with START < END.")

        if not (0.0 <= self.args.hill_floor <= 1.0):
            raise ValueError("--hill-floor must be in [0..1].")
        if not (0.0 < self.args.hill_gamma):
            raise ValueError("--hill-gamma must be > 0.")
        if not (0.0 < self.args.hill_ceil <= 1.0):
            raise ValueError("--hill-ceil must be in (0..1].")
        if self.args.hill_floor >= self.args.hill_ceil:
            raise ValueError("--hill-floor must be < --hill-ceil.")
        if not (0.0 <= self.args.shade_strength <= 1.0):
            raise ValueError("--shade-strength must be in [0..1].")

    def _infer_hillshade_denominator(self, src_h, src_c) -> float:
        """Infer a reasonable normalization denominator for hillshade values.

        Integer hillshades: use dtype max (e.g., 255 for uint8, 65535 for uint16).
        Float hillshades: sample a few windows; if max <= ~1.5 assume 0..1, else assume 0..255.
        """
        dtype = np.dtype(src_h.dtypes[0])
        if np.issubdtype(dtype, np.integer):
            return float(np.iinfo(dtype).max)

        # Float: sample a few windows from the color raster's tiling for stable behavior
        windows = list(src_c.block_windows(1))[:self._HILL_SAMPLE_WINDOWS]
        if not windows:
            return self._HILL_FLOAT_ASSUME_MAX

        max_vals = []
        for _, window in windows:
            arr = src_h.read(1, window=window).astype("float32", copy=False)
            if np.isfinite(arr).any():
                max_vals.append(float(np.nanmax(arr)))

        if not max_vals:
            return self._HILL_FLOAT_ASSUME_MAX

        sample_max = max(max_vals)
        return self._HILL_FLOAT_ASSUME_MAX if sample_max <= self._HILL_FLOAT_MAX_CUTOFF else (
            self._BYTE_MAX)

    def _process_single_chunk(self, src_h, src_c, window):
        """Read, compute, and return blended RGB(A) window."""
        from rasterio.fill import fillnodata

        rgb = src_c.read([1, 2, 3], window=window)
        hill = src_h.read(1, window=window)

        if hill.shape != rgb.shape[1:]:
            return None

        # Inpaint only if hillshade has explicit nodata
        if self.has_nodata:
            mask = (hill != self.nodata_val).astype("uint8")
            hill = fillnodata(hill, mask=mask, max_search_distance=self._NODATA_INPAINT_SEARCH_DIST)

        rgb_f = rgb.astype("float32", copy=False)

        # Normalize hillshade to 0..1
        hill_f = hill.astype("float32", copy=False) / float(self.hill_den)
        hill_f = np.clip(hill_f, 0.0, 1.0)

        # Optional tone mapping
        if self.hill_floor > 0.0 or self.hill_gamma != 1.0 or self.hill_ceil < 1.0:
            hill_f = self.hill_floor + (1.0 - self.hill_floor) * (hill_f ** self.hill_gamma)
            if self.hill_ceil < 1.0:
                hill_f = np.minimum(hill_f, self.hill_ceil)
            hill_f = np.clip(hill_f, 0.0, 1.0)

        # Compute smooth protection weight
        w_shadow = self._shadow_weight(hill_f) * self.protect_shadows
        w_high = self._highlight_weight(hill_f) * self.protect_highlights
        w = np.maximum(w_shadow, w_high)  # combine

        # Apply protection to multiplier: m = hill + w*(1-hill)
        strength = float(self.args.shade_strength)
        strength = np.clip(strength, 0.0, 1.0)

        m = hill_f + w * (1.0 - hill_f)
        m = np.clip(m, 0.0, 1.0)

        m = (1.0 - strength) + strength * m
        m = np.clip(m, 0.0, 1.0)

        m_exp = m[None, :, :]

        blended = m_exp * rgb_f
        blended_u8 = np.round(blended).clip(0, 255).astype("uint8")

        if src_c.count == 4:
            alpha = src_c.read(4, window=window)
            return np.concatenate([blended_u8, alpha[None, :, :]], axis=0)

        return blended_u8

    @staticmethod
    def _smoothstep(edge0: float, edge1: float, x):
        """Smoothstep interpolation returning 0..1 for x in [edge0..edge1]."""

        # Avoid division by zero
        if edge1 == edge0:
            return np.zeros_like(x, dtype="float32")
        t = (x - edge0) / (edge1 - edge0)
        t = np.clip(t, 0.0, 1.0)
        return t * t * (3.0 - 2.0 * t)

    def _shadow_weight(self, hill_f):
        """Weight 1 in deep shadows, fades to 0 by shadow_end."""
        # Full protection near shadow_start, zero after shadow_end
        # w = 1 - smoothstep(start, end, hill)
        return 1.0 - self._smoothstep(self.shadow_start, self.shadow_end, hill_f)

    def _highlight_weight(self, hill_f):
        """Weight 0 until highlight_start, ramps to 1 by highlight_end."""
        return self._smoothstep(self.highlight_start, self.highlight_end, hill_f)


# Tunables / safety constants
DEFAULT_OCTAVES = 3
MIN_FADE_PIXELS = 1
BASE_SCALE_MIN_PIXELS = 50.0
ALPHA_MAX = 255.0


def _smoothstep01(t: np.ndarray) -> np.ndarray:
    """Classic smoothstep on [0..1]."""
    return t * t * (3.0 - 2.0 * t)


@register_command("vignette")
class Vignette(IOCommand):
    """
    Adds an Alpha gradient to the edge of a raster, creating a vignette fade.
    This is used so that an overlayed raster blends into the layer under it.

    Parameters:
      --border (float):
          Controls the width of the fade gradient.
          Calculated as a % of the image's smallest dimension (Height or Width).
          Example: 5.0 creates a fade that covers 5% of the image.
          **If 0, the input file is simply copied to the output.**

      --noise (float):
          Adds high-frequency "grain" (dithering) to the fade.
          Calculated as a % of the 'border' size.
          Purpose: Hides digital banding and makes the gradient look smoother.

      --warp (float):
          Adds low-frequency "wiggles" (fractal distortion) to the edge shape.
          Calculated as a % of the 'border' size.
          Purpose: Breaks up straight lines, making the edge look organic.
          Note: The visible image area shrinks slightly as warp increases to ensure edges remain
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

    def transform(self):
        import rasterio
        from scipy.ndimage import distance_transform_edt
        import shutil

        input_path = self.args.input
        output_path = self.args.output

        # === 0. Bypass Check ===
        if self.args.border <= 0:
            self.print_verbose(f"--- Border is 0%. Copying '{input_path}' to '{output_path}' ---")
            shutil.copy(input_path, output_path)
            return

        # ✅ deterministic RNG (optional)
        rng = np.random.default_rng(self.args.seed)

        # 1. Open Input to get Dimensions
        with rasterio.open(input_path) as src:
            data = src.read()  # shape: (bands, h, w)
            profile = src.profile.copy()
            height = int(src.height)
            width = int(src.width)
            bands = int(src.count)

        # 2. Calculate Absolute Pixel Values from Percentages
        min_dim = min(height, width)

        fade_pixels = int(min_dim * (self.args.border / 100.0))
        fade_pixels = max(MIN_FADE_PIXELS, fade_pixels)

        noise_amt = int(fade_pixels * (self.args.noise / 100.0))
        warp_amt = int(fade_pixels * (self.args.warp / 100.0))

        self.print_verbose(
            f"Border: {self.args.border}% ({fade_pixels}px) , "
            f"Warp: {self.args.warp}% ({warp_amt}px) , "
            f"Noise: {self.args.noise}% ({noise_amt}px)"
        )

        # 3. Smart Mask Generation (defines “inside” for distance transform)
        if bands in (2, 4):
            existing_alpha = data[-1].astype(np.uint8, copy=False)
            mask = (existing_alpha > 0).astype(np.uint8)
        else:
            existing_alpha = None
            mask = np.ones((height, width), dtype=np.uint8)

        # Force outermost border to be “outside”
        mask[0, :] = 0
        mask[-1, :] = 0
        mask[:, 0] = 0
        mask[:, -1] = 0

        # Distance to nearest outside pixel
        dist_grid = distance_transform_edt(mask).astype(np.float32, copy=False)

        # 4. Fractal Warp Injection
        if warp_amt > 0:
            base_scale = max(BASE_SCALE_MIN_PIXELS, float(fade_pixels) * 1.5)
            fractal = self._generate_fractal_noise(height, width, base_scale, rng=rng)
            # Safety shift so the warp mostly pulls inward near the fade band
            dist_grid += (fractal * float(warp_amt)) - float(warp_amt)

        # 5. High-Freq Grain
        if noise_amt > 0:
            grain = rng.uniform(0.0, float(noise_amt), (height, width)).astype(
                np.float32, copy=False
            )
            dist_grid -= grain

        # 6. Normalize to alpha byte
        dist_grid = np.clip(dist_grid, 0.0, float(fade_pixels))
        vignette_alpha = (dist_grid / float(fade_pixels)) * ALPHA_MAX
        vignette_alpha = np.round(vignette_alpha).clip(0, 255).astype(np.uint8)

        # ✅ smooth the vignette a bit (helps banding on some sources)
        t = (vignette_alpha.astype(np.float32) / 255.0)
        vignette_alpha = np.round(_smoothstep01(t) * 255.0).astype(np.uint8)

        # 7. Prepare Output data
        out = data.copy()  # safer than mutating `data` in-place
        if bands in (2, 4):
            if self.args.replace_alpha:
                out[-1] = vignette_alpha
            else:
                # ✅ Default: preserve existing alpha by multiplying with vignette alpha
                ea = existing_alpha.astype(np.uint16, copy=False)
                va = vignette_alpha.astype(np.uint16, copy=False)
                out[-1] = np.round((ea * va) / 255.0).astype(np.uint8)
        else:
            alpha_band = vignette_alpha[np.newaxis, :, :]
            out = np.concatenate([out, alpha_band], axis=0)
            profile.update({"count": bands + 1})

        # Default-ish output settings
        profile.update(
            {
                "driver": "GTiff", "compress": "deflate", "tiled": True,
            }
        )

        # Apply User Overrides (e.g. COMPRESS=ZSTD)
        if self.args.co:
            for opt in self.args.co:
                if "=" in opt:
                    key, val = opt.split("=", 1)
                    key = key.lower()
                    profile[key] = int(val) if val.isdigit() else val

        # --- Force valid tiling if tiled ---
        if profile.get("tiled", False):
            # Pick a standard tile size (must be multiples of 16)
            TILE = 256  # or 512 for fewer blocks
            profile["blockxsize"] = TILE
            profile["blockysize"] = TILE

        # Photometric must match band semantics
        out_count = profile.get("count", out.shape[0])
        if out_count in (1, 2):
            # 1-band or gray+alpha
            profile["photometric"] = "MINISBLACK"
        else:
            # RGB or RGBA
            profile["photometric"] = "RGB"

        # Clean up and Write
        Path(output_path).unlink(missing_ok=True)
        try:
            with rasterio.open(output_path, "w", **profile) as dst:
                dst.write(out)
            self.print_verbose(f"✅ Created {output_path}")
        except Exception:
            Path(output_path).unlink(missing_ok=True)
            raise

    def _generate_fractal_noise(
            self, h: int, w: int, base_scale: float, rng: np.random.Generator,
            octaves: int = DEFAULT_OCTAVES, ) -> np.ndarray:
        """Generate low-frequency fractal noise in [-1..1] with deterministic RNG.

        Args:
            h: Height in pixels.
            w: Width in pixels.
            base_scale: Starting scale (larger => smoother).
            rng: Numpy RNG generator.
            octaves: Number of octaves.

        Returns:
            Noise field shaped (h, w), float32, roughly in [-1..1].
        """
        from scipy.ndimage import zoom

        total_noise = np.zeros((h, w), dtype=np.float32)
        amplitude = 1.0
        max_possible_value = 0.0
        current_scale = float(base_scale)

        for _ in range(int(octaves)):
            small_h = max(1, int(h / current_scale))
            small_w = max(1, int(w / current_scale))

            layer = rng.uniform(-1.0, 1.0, (small_h, small_w)).astype(np.float32, copy=False)

            zoom_h = h / small_h
            zoom_w = w / small_w

            upscaled = zoom(layer, (zoom_h, zoom_w), order=3).astype(np.float32, copy=False)
            upscaled = upscaled[:h, :w]

            total_noise += upscaled * amplitude
            max_possible_value += amplitude

            amplitude *= 0.5
            current_scale /= 2.0

        denom = max_possible_value if max_possible_value > 0 else 1.0
        return (total_noise / denom).astype(np.float32, copy=False)


@register_command("create_mbtiles")
class CreateMBTiles(IOCommand):
    """
    Converts a TIF to MBTiles.

    Optimization Strategy:
    1. Generates temporary external overviews (.ovr) on the SOURCE file first.
       This ensures downsampling happens from the lossless source, not a lossy intermediate.
    2. Runs gdal_translate to populate the MBTiles database.
       Because source overviews exist, gdal_translate uses them to create
       lower zoom levels without compounding compression artifacts.
    3. Patches the SQLite metadata table manually to ensure minzoom/maxzoom are correct.
    """

    @staticmethod
    def add_arguments(parser):
        super(CreateMBTiles, CreateMBTiles).add_arguments(parser)

        # 1. GDAL Pass-Throughs
        parser.add_argument("--co", action="append", help="GDAL Creation Options")
        parser.add_argument("--mo", action="append", help="GDAL Metadata Options")

        # 2. Logic Options
        parser.add_argument(
            "--min-zoom", type=int, help="Force 'minzoom' metadata in MBTiles."
        )
        parser.add_argument(
            "--max-zoom", type=int, help="Force 'maxzoom' metadata in MBTiles."
        )

        # 3. Pyramid Options
        parser.add_argument("-r", "--resampling", default="CUBIC", help="Resampling algo.")
        parser.add_argument("--levels", nargs="+", default=["2", "4", "8", "16", "32", "64", "128"])

    def transform(self):
        self.run_mbtiles()
        self.run_gdaladdo()

    def run_mbtiles(self):
        layer_name = Path(self.args.output).stem
        self.print_verbose(f"--- Generating MBTiles ({self.args.output}) ---")

        # Base Command
        cmd = ["gdal_translate", "-of", "MBTiles", "-mo", f"name={layer_name}", "-mo",
               "type=overlay", ]

        # Inject User Options (Only valid -co flags should be here now)
        if self.args.co:
            for opt in self.args.co:
                cmd.extend(["-co", opt])

        if self.args.mo:
            for opt in self.args.mo:
                cmd.extend(["-mo", opt])

        cmd.extend([self.args.input, self.args.output])

        try:
            self._run_command(cmd)
            self.print_verbose(f"✅ Created MBTiles: {self.args.output}")
        except Exception:
            Path(self.args.output).unlink(missing_ok=True)
            raise

    def run_gdaladdo(self):
        # Force BILINEAR. It is the safest for re-compressing noisy data.
        cmd = ["gdaladdo", "-r", "BILINEAR", self.args.output] + self.args.levels
        self._run_command(cmd)

    def patch_metadata(self):
        import sqlite3

        if self.args.min_zoom or self.args.max_zoom:
            self.print_verbose("--- Patching Metadata ---")
            conn = sqlite3.connect(self.args.output)
            cursor = conn.cursor()

            # DELETE duplicates first to avoid confusion
            if self.args.min_zoom:
                cursor.execute("DELETE FROM metadata WHERE name='minzoom'")
                cursor.execute(
                    "INSERT INTO metadata (name, value) VALUES ('minzoom', ?)",
                    (self.args.min_zoom,)
                )

            if self.args.max_zoom:
                cursor.execute("DELETE FROM metadata WHERE name='maxzoom'")
                cursor.execute(
                    "INSERT INTO metadata (name, value) VALUES ('maxzoom', ?)",
                    (self.args.max_zoom,)
                )

            conn.commit()
            conn.close()


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
        self.print_verbose(
            f"--- Converting '{self.args.input}' to PMTiles ({self.args.output}) ---"
        )

        # Ensure input is actually an mbtiles file to avoid confused tool output
        if not self.args.input.endswith(".mbtiles"):
            self.print_verbose("⚠️  Warning: Input file does not have .mbtiles extension.")

        # pmtiles convert input.mbtiles output.pmtiles
        cmd = ["pmtiles", "convert", self.args.input, self.args.output]

        try:
            self._run_command(cmd)
            self.print_verbose(f"✅ Created PMTiles: {self.args.output}")
        except FileNotFoundError:
            print("❌ Error: 'pmtiles' executable not found in PATH.")
            print("   Please install it from: https://github.com/protomaps/go-pmtiles")
            raise


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
            print("❌ Error: No command provided to run.")
            return

        # self._run_command will handle the logging and the CoOptions validation
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
        parser.add_argument(
            "--min-pixels", type=int, default=1000,
            help="Minimum TOTAL pixels (Width * Height). Default: 1000."
        )

    def execute(self):
        # Lazy import
        import rasterio

        input_file = self.args.input
        min_bytes = self.args.min_bytes
        min_pixels = self.args.min_pixels

        if not os.path.exists(input_file):
            raise FileNotFoundError(f"❌ Validation Failed: File not found: {input_file}")

        # 1. Check File Size (Bytes)
        file_size = os.path.getsize(input_file)
        if file_size < min_bytes:
            raise ValueError(
                f"❌ Validation Failed: File size is too small.\n"
                f"   File: {input_file}\n"
                f"   Size: {file_size} bytes\n"
                f"   Minimum: {min_bytes} bytes"
            )

        # 2. Check Total Pixels (Rasterio)
        try:
            with rasterio.open(input_file) as src:
                width = src.width
                height = src.height
                total_pixels = width * height

                if total_pixels < min_pixels:
                    raise ValueError(
                        f"❌ Validation Failed: Image area is too small.\n"
                        f"   File: {input_file}\n"
                        f"   Dimensions: {width}x{height} ({total_pixels} pixels)\n"
                        f"   Minimum Area: {min_pixels} pixels"
                    )

        except rasterio.errors.RasterioIOError:
            raise ValueError(
                f"❌ Validation Failed: File exists but is not a valid raster: {input_file}"
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
        target_ids = [int(i.strip()) for i in self.args.targets.split(",")]

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


# Point to the registry populated by the decorators
COMMANDS = COMMAND_REGISTRY
