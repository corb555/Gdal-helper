#!/usr/bin/env python
import argparse
import json
import os
from pathlib import Path
import subprocess

from GDALHelper.color_ramp_hsv import adjust_color_ramp
from GDALHelper.gdal_helper import Command
from GDALHelper.git_utils import get_git_hash, set_geotiff_version, get_geotiff_version


# ===================================================================
# Utility Functions
# ===================================================================

def _get_image_dimensions(filepath: str) -> tuple[int, int]:
    # ... (This function is unchanged) ...
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
    """
    Runs gdalinfo and parses its JSON output to get resolution, extent, and SRS.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Cannot get raster info: File not found at '{filepath}'")
    try:
        result = subprocess.run(
            ["gdalinfo", "-json", filepath], capture_output=True, text=True, check=True
        )
        info = json.loads(result.stdout)

        # Extract the necessary components
        resolution = (info['geoTransform'][1], info['geoTransform'][5])
        srs_wkt = info['coordinateSystem']['wkt']
        # Get corner coordinates to calculate the min/max extent
        corners = info['cornerCoordinates']
        xmin = min(c[0] for c in corners.values())
        xmax = max(c[0] for c in corners.values())
        ymin = min(c[1] for c in corners.values())
        ymax = max(c[1] for c in corners.values())

        return {
            "resolution": resolution,
            "extent": (xmin, ymin, xmax, ymax),
            "srs_wkt": srs_wkt
        }
    except Exception as e:
        raise RuntimeError(
            f"Failed to get raster info for {filepath}. Is gdalinfo in your PATH? Error: {e}"
        )


class AdjustColorFile(Command):
    """Updates the HSV values in a GDALDEM color-relief color config file."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("input", help="The source GDAL color file.")
        parser.add_argument("output", help="The path for the new color file.")
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
            "--elev_adjust", type=float, default=1.0, help="Elevation multiplier."
        )

    def execute(self):
        self.print_verbose(
            f"--- Adjusting color file '{self.args.input}' to '{self.args.output}' ---"
        )
        adjust_color_ramp(
            self.args.input, self.args.output, saturation_multiplier=self.args.saturation,
            shadow_adjust=self.args.shadow_adjust, mid_adjust=self.args.mid_adjust,
            highlight_adjust=self.args.highlight_adjust, min_hue=self.args.min_hue,
            max_hue=self.args.max_hue, target_hue=self.args.target_hue, elev_adjust=self.args.elev_adjust
        )
        self.print_verbose("--- Color file adjusted. ---")


class CreateSubset(Command):
    """Extracts a smaller section from a large raster file."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("input", help="The source raster file.")
        parser.add_argument("output", help="The path for the new preview file.")
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

    def execute(self):
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


class Publish(Command):
    """Publishes a file, optionally stamping it with a git version first."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("source_file", help="The local file to publish.")
        parser.add_argument("directory", help="The destination directory (local or remote).")
        parser.add_argument("--host", help="Optional: destination host.")
        parser.add_argument(
            "--marker-file", help="Optional path for a marker file to create on success."
        )
        parser.add_argument(
            "--disable", action="store_true", help="If present, the publish action is skipped."
        )
        # --- NEW ARGUMENT ---
        parser.add_argument(
            "--stamp-version", action="store_true",
            help="If present, embed the current git commit hash into the file before publishing."
        )

    def execute(self):
        # --- NEW: Version Stamping Logic ---
        if self.args.stamp_version:
            self.print_verbose(f"--- Stamping version on '{self.args.source_file}' ---")
            git_hash = get_git_hash()
            set_geotiff_version(self.args.source_file, git_hash)

        if not self.args.disable:
            if self.args.host and self.args.host != "None":
                self.print_verbose(
                    f"--- Publishing '{self.args.source_file}' to remote host {self.args.host} ---"
                )
                command = ["scp", self.args.source_file, f"{self.args.host}:{self.args.directory}"]
            else:
                self.print_verbose(
                    f"--- Publishing '{self.args.source_file}' to local directory ---"
                )
                command = ["cp", self.args.source_file, self.args.directory]
            self._run_command(command)
            self.print_verbose("--- Publish complete. ---")
        else:
            self.print_verbose(f"--- Publish is disabled for '{self.args.source_file}'. ---")

        if self.args.marker_file:
            self.print_verbose(f"--- Creating marker file at '{self.args.marker_file}' ---")
            marker = Path(self.args.marker_file)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            self.print_verbose("--- Marker file created. ---")


class AddVersion(Command):
    """Embeds the current git commit hash into a GeoTIFF's metadata."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("target_file", help="The raster file to stamp with a version.")

    def execute(self):
        self.print_verbose(f"--- Stamping version on '{self.args.target_file}' ---")
        git_hash = get_git_hash()
        set_geotiff_version(self.args.target_file, git_hash)
        self.print_verbose("--- Version stamping complete. ---")


class GetVersion(Command):
    """Reads the embedded LiteBuild version hash from a GeoTIFF's metadata."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("target_file", help="The raster file to inspect.")

    def execute(self):
        print(f"--- Reading version info from '{self.args.target_file}' ---")
        version_hash = get_geotiff_version(self.args.target_file)
        if version_hash:
            print(f"✅ Found Version: {version_hash}")
            if version_hash.endswith("-dirty"):
                print("   ⚠️  This file was built from a repository with uncommitted changes.")
        else:
            print("❌ No LiteBuild version information found in file.")


class AlignRaster(Command):
    """
    Resamples a source raster to perfectly match a template raster's
    SRS, extent, and resolution. This is essential for ensuring pixels
    are perfectly aligned before performing calculations.

    Example:
      gdal-helper align_raster mask.tif base_dem.tif mask_aligned.tif --co "COMPRESS=JPEG"
    """
    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("source", help="The raster file to be aligned (e.g., the mask).")
        parser.add_argument("template", help="The raster file with the desired grid (e.g., the base DEM).")
        parser.add_argument("output", help="The path for the new, aligned output file.")
        parser.add_argument(
            "-r", "--resampling-method", default="bilinear",
            help="Resampling method to use (e.g., near, bilinear, cubic). Default: bilinear."
        )
        # Allows for multiple --co flags, which argparse will collect into a list.
        parser.add_argument(
            "--co",
            action="append",
            metavar="NAME=VALUE",
            help="Creation option for the output driver (e.g., 'COMPRESS=JPEG'). Can be specified multiple times."
        )

    def execute(self):
        # Get all alignment info from the template file in one go
        template_info = _get_raster_info(self.args.template)
        x_res, y_res = template_info["resolution"]
        xmin, ymin, xmax, ymax = template_info["extent"]
        srs_wkt = template_info["srs_wkt"]

        self.print_verbose(f"--- Aligning '{self.args.source}' to match '{self.args.template}' ---")
        self.print_verbose(f"Target grid: x_res={x_res}, y_res={y_res}, extent=[{xmin}, {ymin}, {xmax}, {ymax}]")

        # --- UPDATED COMMAND CONSTRUCTION ---
        command = [
            "gdalwarp",
            "-t_srs", srs_wkt,
            "-te", str(xmin), str(ymin), str(xmax), str(ymax),
            "-tr", str(x_res), str(y_res),
            "-r", self.args.resampling_method,
        ]

        # Add creation options to the command if they were provided
        if self.args.co:
            self.print_verbose(f"Using creation options: {self.args.co}")
            for option in self.args.co:
                command.extend(["-co", option])

        command.extend([
            "-overwrite",
            self.args.source,
            self.args.output
        ])

        self._run_command(command)
        self.print_verbose("--- Raster aligned successfully. ---")

class MaskedBlend(Command):
    """
    Blends two layers using a third layer as a mask via gdal_calc.

    This command expects a formula where band A is the foreground, B is the
    background, and C is the mask. The formula should normalize the 8-bit
    mask (0-255) to a float (0.0-1.0) for a proper blend.

    Example:
      gdal-helper masked_blend layerA.tif layerB.tif mask.tif blended.tif --calc "numpy.clip(A*(C.astype(float)/255.0) + B*(1-C.astype(float)/255.0), 0, 255)" --extent union
    """
    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("layerA", help="Input layer  A")
        parser.add_argument("layerB", help="Input layer  B")
        parser.add_argument("mask", help="The mask file (Layer C).")
        parser.add_argument("output", help="The path for the blended output file.")
        parser.add_argument(
            "--calc", required=True,
            # Updated help text to show the correct formula pattern
            help="The gdal_calc numpy formula to apply. Must normalize the mask (C)."
        )

        parser.add_argument(
            "--temp-dir", default=".",
            help="Directory to store intermediate single-band files."
        )
        parser.add_argument(
            "--keep-temp", action="store_true",
            help="If this flag is present, temporary files will not be deleted."
        )

    def execute(self):
        """Orchestrates the entire extract -> blend -> merge workflow."""
        self.print_verbose(
            f"--- Blending '{self.args.layerA}' and '{self.args.layerB}' ---"
        )

        temp_dir = Path(self.args.temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)

        temp_files_to_delete = []
        final_blended_channels = []

        try:
            for channel, band_index in [('R', 1), ('G', 2), ('B', 3)]:
                self.print_verbose(f"--- Processing Channel: {channel} ---")

                layerA_band = temp_dir / f"{Path(self.args.layerA).stem}_{channel}.tif"
                layerB_band = temp_dir / f"{Path(self.args.layerB).stem}_{channel}.tif"
                blend_band = temp_dir / f"{Path(self.args.output).stem}_{channel}.tif"

                temp_files_to_delete.extend([layerA_band, layerB_band, blend_band])
                final_blended_channels.append(str(blend_band))

                cmd_extract1 = ["gdal_translate", "-b", str(band_index), self.args.layerA, str(layerA_band)]
                self._run_command(cmd_extract1)

                cmd_extract2 = ["gdal_translate", "-b", str(band_index), self.args.layerB, str(layerB_band)]
                self._run_command(cmd_extract2)

                # The --allBands flag is not needed here because the formula handles the types.
                cmd_blend = [
                    "gdal_calc.py",
                    "-A", str(layerA_band),
                    "-B", str(layerB_band),
                    "-C", self.args.mask,
                    "--calc", self.args.calc,
                    "--type=Byte",
                    "--overwrite",
                    "--outfile", str(blend_band)
                ]
                self._run_command(cmd_blend)

            self.print_verbose("--- Merging blended channels into final output ---")
            cmd_merge = [
                            "gdal_merge.py",
                            "-separate",
                            "-o", self.args.output,
                        ] + final_blended_channels
            self._run_command(cmd_merge)

            self.print_verbose("--- Blend complete. ---")

        finally:
            if not self.args.keep_temp:
                self.print_verbose("--- Cleaning up temporary files... ---")
                for f in temp_files_to_delete:
                    Path(f).unlink(missing_ok=True)
                self.print_verbose("Cleanup done.")


# ===================================================================
# Main execution block
# ===================================================================

COMMANDS = {
    "adjust_color_file": AdjustColorFile, "create_subset": CreateSubset, "publish": Publish,
     "masked_blend": MaskedBlend, "add_version": AddVersion,
    "get_version": GetVersion, "align_raster":AlignRaster,
}
