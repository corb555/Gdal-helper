#!/usr/bin/env python
import argparse
import json
import os
from pathlib import Path
import subprocess

# Note: rasterio, numpy, and scipy are imported lazily inside specific commands
# to avoid forcing users to install them if they only use the CLI wrappers.

from GDALHelper.color_ramp_hsv import adjust_color_ramp
from GDALHelper.gdal_helper import Command, IOCommand, COMMAND_REGISTRY, register_command
from GDALHelper.git_utils import get_git_hash, set_tiff_version, get_tiff_version


# ===================================================================
# Utility Functions
# ===================================================================

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
            "resolution": resolution,
            "extent": (xmin, ymin, xmax, ymax),
            "srs_wkt": srs_wkt
        }
    except Exception as e:
        raise RuntimeError(
            f"Failed to get raster info for {filepath}. Is gdalinfo in your PATH? Error: {e}"
        )

# ================================
# GDAL-Helper Commands
# ================================

@register_command("adjust_color_file")
class AdjustColorFile(IOCommand):
    """Updates the HSV values in a GDALDEM color-relief color config file."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
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
            "--elev_adjust", type=float, default=1.0, help="Elevation multiplier."
        )

    def run_transformation(self):
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


@register_command("create_subset")
class CreateSubset(IOCommand):
    """Extracts a smaller section from a large raster file."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
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

    def run_transformation(self):
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


@register_command("publish")
class Publish(Command):
    """
    Publishes a file, optionally stamping it with a git version first.
    """

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
        parser.add_argument(
            "--stamp-version", action="store_true",
            help="If present, embed the current git commit hash into the file before publishing."
        )

    def execute(self):
        if self.args.stamp_version:
            self.print_verbose(f"--- Stamping version on '{self.args.source_file}' ---")
            git_hash = get_git_hash()
            # set_tiff_version handles the extension check internally.
            # If it fails (e.g. gdal_edit not found), it raises RuntimeError, stopping the publish.
            set_tiff_version(self.args.source_file, git_hash)

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


@register_command("add_version")
class AddVersion(Command):
    """Embeds the current git commit hash into a TIFF's metadata."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("target_file", help="The TIFF file to stamp with a version.")

    def execute(self):
        git_hash = get_git_hash()
        self.print_verbose(f"--- Stamping version on '{self.args.target_file}' Version: {git_hash} ---")
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
    def add_arguments(parser: argparse.ArgumentParser):
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
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("source", help="The raster file to be aligned (e.g., the mask).")
        parser.add_argument("template", help="The raster file with the desired grid (e.g., the base DEM).")
        parser.add_argument("output", help="The path for the new, aligned output file.")
        parser.add_argument(
            "-r", "--resampling-method", default="bilinear",
            help="Resampling method to use (e.g., near, bilinear, cubic). Default: bilinear."
        )
        parser.add_argument(
            "--co",
            action="append",
            metavar="NAME=VALUE",
            help="Creation option for the output driver (e.g., 'COMPRESS=JPEG'). Can be specified multiple times."
        )

    def execute(self):
        template_info = _get_raster_info(self.args.template)
        x_res, y_res = template_info["resolution"]
        xmin, ymin, xmax, ymax = template_info["extent"]
        srs_wkt = template_info["srs_wkt"]

        self.print_verbose(f"--- Aligning '{self.args.source}' to match '{self.args.template}' ---")

        command = [
            "gdalwarp",
            "-t_srs", srs_wkt,
            "-te", str(xmin), str(ymin), str(xmax), str(ymax),
            "-tr", str(x_res), str(y_res),
            "-r", self.args.resampling_method,
        ]

        if self.args.co:
            for option in self.args.co:
                command.extend(["-co", option])

        command.extend([
            "-overwrite",
            self.args.source,
            self.args.output
        ])

        self._run_command(command)
        self.print_verbose("--- Raster aligned successfully. ---")

@register_command("masked_blend")
class MaskedBlend(Command):
    """
    Blends two layers using a third layer as a grayscale mask.

    Logic:
      The blend is performed using a linear interpolation formula:
      Output = (Foreground * Mask) + (Background * (1 - Mask))

      - Where the Mask is White (255): The output is 100% Foreground (Layer A).
      - Where the Mask is Black (0): The output is 100% Background (Layer B).
      - Intermediate mask values produce a weighted mix of both layers.
    """
    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("layerA", help="Input layer A (Foreground)")
        parser.add_argument("layerB", help="Input layer B (Background)")
        parser.add_argument("mask", help="The mask file (Layer C).")
        parser.add_argument("output", help="The path for the blended output file.")

    def execute(self):
        # Lazy imports
        import rasterio
        import numpy as np

        self.print_verbose(
            f"--- Blending '{self.args.layerA}' and '{self.args.layerB}' (Native Python) ---"
        )
        try:
            with rasterio.open(self.args.layerA) as src_a, \
                    rasterio.open(self.args.layerB) as src_b, \
                    rasterio.open(self.args.mask) as src_mask:

                arr_a = src_a.read()
                arr_b = src_b.read()
                arr_mask = src_mask.read(1)

                if arr_a.shape != arr_b.shape:
                    raise ValueError(f"Shape mismatch: A {arr_a.shape} vs B {arr_b.shape}")

                # Normalize mask to 0.0 - 1.0 range
                mask_float = arr_mask.astype('float32') / 255.0
                mask_expanded = mask_float[None, :, :]

                # Perform Linear Interpolation
                blended = (arr_a * mask_expanded) + (arr_b * (1.0 - mask_expanded))

                blended_byte = np.round(blended).clip(0, 255).astype('uint8')

                output_path = Path(self.args.output)
                output_path.unlink(missing_ok=True)

                kwargs = {
                    'driver': 'GTiff',
                    'height': src_a.height,
                    'width': src_a.width,
                    'count': 3,
                    'dtype': 'uint8',
                    'crs': src_a.crs,
                    'transform': src_a.transform,
                    'compress': 'deflate',
                    'tiled': True,
                    'blockxsize': 256,
                    'blockysize': 256,
                    'photometric': 'RGB'
                }

                with rasterio.open(self.args.output, 'w', **kwargs) as dst:
                    dst.write(blended_byte)

            self.print_verbose(f"✅ Created {self.args.output}")

        except Exception as e:
            print(f"❌ Blend Failed: {e}")
            raise

@register_command("hillshade_blend")
class HillshadeBlend(Command):
    """
    Blends a Hillshade with a Color image using conditional logic to preserve
    shadows and highlights

    Logic:
      1. Standard Multiplication: Generally calculates `Color * Hillshade`.
      2. Highlight/Shadow Protection: In areas where the hillshade is extreme
         (defined by --shadow-thresh and --highlight-thresh), the hillshade
         intensity is softened using the formula:
         `(soften_factor * Hillshade + (1.0 - soften_factor))`.
    """
    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("hillshade", help="Input Hillshade")
        parser.add_argument("color", help="Input Color Image")
        parser.add_argument("output", help="Output path")
        parser.add_argument("--co", action="append", help="Creation options (e.g. COMPRESS=JPEG)")
        parser.add_argument(
            "--shadow-thresh", type=int, default=10,
            help="Pixel value below which shadow protection applies (0-255). Default: 10"
        )
        parser.add_argument(
            "--highlight-thresh", type=int, default=245,
            help="Pixel value above which highlight protection applies (0-255). Default: 245"
        )
        parser.add_argument(
            "--soften-factor", type=float, default=0.8,
            help="Multiplier for hillshade intensity in protected areas. Default: 0.8"
        )

    def execute(self):
        # Lazy imports
        import rasterio
        import numpy as np

        self.print_verbose(
            f"--- Blending Hillshade '{self.args.hillshade}' onto '{self.args.color}' ---"
        )

        shadow_thresh = self.args.shadow_thresh
        highlight_thresh = self.args.highlight_thresh
        factor = self.args.soften_factor

        try:
            with rasterio.open(self.args.hillshade) as src_hill, \
                    rasterio.open(self.args.color) as src_color:

                arr_hill = src_hill.read()
                arr_color = src_color.read()

                if arr_hill.shape[1:] != arr_color.shape[1:]:
                    raise ValueError(f"Dimensions mismatch: Hillshade {arr_hill.shape} vs Color {arr_color.shape}")

                hill_float = arr_hill.astype('float32') / 255.0
                color_float = arr_color.astype('float32')

                # Define extreme shadows and highlights based on args
                luminosity_mask = (arr_hill < shadow_thresh) | (arr_hill > highlight_thresh)

                # Extreme area Formula: reduce contrast in extreme areas
                softened_value = (factor * hill_float + (1.0 - factor)) * color_float

                # Standard area formula: multiplication for mid-tones
                standard_value = hill_float * color_float

                # Apply  per pixel
                blended = np.where(luminosity_mask, softened_value, standard_value)
                blended_byte = np.round(blended).clip(0, 255).astype('uint8')

                Path(self.args.output).unlink(missing_ok=True)

                kwargs = {
                    'driver': 'GTiff',
                    'height': src_color.height,
                    'width': src_color.width,
                    'count': 3,
                    'dtype': 'uint8',
                    'crs': src_color.crs,
                    'transform': src_color.transform,
                    'tiled': True,
                    'blockxsize': 256,
                    'blockysize': 256,
                    'compress': 'jpeg',
                    'jpeg_quality': 85,
                    'photometric': 'YCBCR'
                }

                if self.args.co:
                    for opt in self.args.co:
                        if '=' in opt:
                            key, val = opt.split('=', 1)
                            key = key.lower()
                            if val.isdigit():
                                val = int(val)
                            kwargs[key] = val

                with rasterio.open(self.args.output, 'w', **kwargs) as dst:
                    dst.write(blended_byte)

            self.print_verbose(f"✅ Created {self.args.output}")

        except Exception as e:
            print(f"❌ Hillshade Blend Failed: {e}")
            Path(self.args.output).unlink(missing_ok=True)
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
          Note: The visible image area shrinks slightly as warp increases to ensure edges remain soft.
    """
    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        super(Vignette, Vignette).add_arguments(parser)
        parser.add_argument(
            "--border", type=float, default=5.0,
            help="Fade width as a percentage of the image's smallest dimension. Default: 5.0%%"
        )
        parser.add_argument(
            "--noise", type=float, default=20.0,
            help="Noise amplitude as a percentage of the calculated border width. Default: 20%%"
        )
        parser.add_argument(
            "--warp", type=float, default=60.0,
            help="Warp distortion as a percentage of the calculated border width. Default: 60%%"
        )

    def _generate_fractal_noise(self, h, w, base_scale, octaves=3):
        import numpy as np
        from scipy.ndimage import zoom

        total_noise = np.zeros((h, w))
        amplitude = 1.0
        max_possible_value = 0.0

        current_scale = base_scale

        for _ in range(octaves):
            small_h = max(1, int(h / current_scale))
            small_w = max(1, int(w / current_scale))

            layer = np.random.uniform(-1.0, 1.0, (small_h, small_w))

            zoom_h = h / small_h
            zoom_w = w / small_w

            upscaled = zoom(layer, (zoom_h, zoom_w), order=3)
            upscaled = upscaled[:h, :w]

            total_noise += upscaled * amplitude

            max_possible_value += amplitude
            amplitude *= 0.5
            current_scale /= 2.0

        return total_noise / max_possible_value

    def run_transformation(self):
        import rasterio
        import numpy as np
        from scipy.ndimage import distance_transform_edt
        import shutil

        input_path = self.args.input
        output_path = self.args.output

        # === 0. Bypass Check ===
        if self.args.border <= 0:
            self.print_verbose(f"--- Border is 0%. Copying '{input_path}' to '{output_path}' ---")
            shutil.copy(input_path, output_path)
            return

        # 1. Open Input to get Dimensions
        with rasterio.open(input_path) as src:
            data = src.read()
            profile = src.profile.copy()
            height = src.height
            width = src.width
            bands = src.count

        # 2. Calculate Absolute Pixel Values from Percentages
        min_dim = min(height, width)

        # Border: % of image size
        fade_pixels = int(min_dim * (self.args.border / 100.0))
        fade_pixels = max(1, fade_pixels) # Prevent divide by zero errors

        # Noise/Warp: % of the border size (not image size)
        noise_amt = int(fade_pixels * (self.args.noise / 100.0))
        warp_amt = int(fade_pixels * (self.args.warp / 100.0))

        self.print_verbose(
            f"--- Vignette: {self.args.border}% ({fade_pixels}px) | "
            f"Warp: {self.args.warp}% ({warp_amt}px) | "
            f"Noise: {self.args.noise}% ({noise_amt}px) ---"
        )

        # 3. Smart Mask Generation
        if bands == 4 or bands == 2:
            existing_alpha = data[-1]
            mask = (existing_alpha > 0).astype(np.float32)
        else:
            mask = np.ones((height, width), dtype=np.float32)

        mask[0, :] = 0; mask[-1, :] = 0; mask[:, 0] = 0; mask[:, -1] = 0

        # Calculate distance
        dist_grid = distance_transform_edt(mask)

        # 4. Fractal Warp Injection
        if warp_amt > 0:
            base_scale = max(50, fade_pixels * 1.5)
            fractal = self._generate_fractal_noise(height, width, base_scale, octaves=3)
            # Apply Safety Shift
            dist_grid += (fractal * warp_amt) - warp_amt

        # 5. High-Freq Grain
        if noise_amt > 0:
            grain = np.random.uniform(0, noise_amt, (height, width))
            dist_grid -= grain

        # 6. Normalize
        alpha = np.clip(dist_grid, 0, fade_pixels)
        alpha = (alpha / fade_pixels) * 255
        alpha = alpha.astype(np.uint8)

        # 7. Save
        if bands == 4 or bands == 2:
            output_data = data
            output_data[-1] = alpha
        else:
            alpha_band = alpha[np.newaxis, :, :]
            output_data = np.concatenate([data, alpha_band], axis=0)
            profile.update({'count': profile['count'] + 1})

        profile.update({
            'driver': 'GTiff',
            'compress': 'deflate',
            'tiled': True,
            'photometric': None
        })

        Path(output_path).unlink(missing_ok=True)
        with rasterio.open(output_path, 'w', **profile) as dst:
            dst.write(output_data)

        self.print_verbose(f"✅ Created {output_path}")


@register_command("create_mbtiles")
class CreateMBTiles(IOCommand):
    """
    Converts a TIF to MBTiles and generates internal pyramids (gdaladdo).
    Supports PNG (default) or JPEG formats.
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        super(CreateMBTiles, CreateMBTiles).add_arguments(parser)
        parser.add_argument(
            "--format", default="PNG", choices=["PNG", "JPEG"],
            help="Tile format (PNG for transparency/elevation, JPEG for imagery). Default: PNG."
        )
        parser.add_argument(
            "--quality", type=int, default=85,
            help="JPEG Quality (1-100). Only used if format is JPEG. Default: 85."
        )
        parser.add_argument(
            "--levels", nargs="+", default=["2", "4", "8", "16", "32", "64"],
            help="Overview levels for gdaladdo. Default: 2 4 8 16 32 64"
        )

    def run_transformation(self):
        self.print_verbose(f"--- Converting '{self.args.input}' to MBTiles ({self.args.output}) ---")

        # 1. gdal_translate
        # -co TILE_FORMAT=PNG/JPEG
        # -co JPEG_QUALITY=85 (optional)
        cmd_translate = [
            "gdal_translate",
            "-of", "MBTiles",
            "-co", f"TILE_FORMAT={self.args.format}",
        ]

        if self.args.format == "JPEG":
            cmd_translate.extend(["-co", f"JPEG_QUALITY={self.args.quality}"])

        cmd_translate.extend([self.args.input, self.args.output])

        self._run_command(cmd_translate)

        # 2. gdaladdo (Build Pyramids inside the MBTiles file)
        # Note: gdaladdo modifies the file in place
        self.print_verbose(f"--- Building Pyramids (Levels: {self.args.levels}) ---")
        cmd_addo = ["gdaladdo", "-r", "average", self.args.output] + self.args.levels

        self._run_command(cmd_addo)

        self.print_verbose(f"✅ Created MBTiles: {self.args.output}")


@register_command("create_pmtiles")
class CreatePMTiles(IOCommand):
    """
    Converts an MBTiles archive to a PMTiles archive using the 'pmtiles' CLI tool.
    """

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        super(CreatePMTiles, CreatePMTiles).add_arguments(parser)
        # Add any specific pmtiles args here if needed in the future

    def run_transformation(self):
        self.print_verbose(f"--- Converting '{self.args.input}' to PMTiles ({self.args.output}) ---")

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

@register_command("validate_raster")
class ValidateRaster(Command):
    """
    Checks if a raster meets minimum size requirements.
    Fails the build if the file is too small or looks empty.
    """
    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument("input", help="The raster file to check.")
        parser.add_argument("--min-bytes", type=int, default=1000, help="Minimum file size in bytes (Default: 1000).")
        # UPDATED: This now represents TOTAL pixels, not edge length.
        parser.add_argument("--min-pixels", type=int, default=1000, help="Minimum TOTAL pixels (Width * Height). Default: 1000.")

    def execute(self):
        # Lazy import
        import rasterio

        input_file = self.args.input
        min_bytes = self.args.min_bytes
        min_pixels = self.args.min_pixels

        self.print_verbose(f"--- Validating '{input_file}' ---")

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

                self.print_verbose(f"✅ Validation Passed: {width}x{height} ({total_pixels} px), {file_size} bytes.")

        except rasterio.errors.RasterioIOError:
            raise ValueError(f"❌ Validation Failed: File exists but is not a valid raster: {input_file}")
# Point to the registry populated by the decorators
COMMANDS = COMMAND_REGISTRY