from __future__ import annotations

import argparse
import json
import math
from typing import Optional
import os
import shlex
import subprocess
from typing import List

from GDALHelper.co_options import CoOptions
import numpy as np

# utils.py

BYTE_MAX = 255.0
TILE_SIZE_DEFAULT = 256
COMPRESS_DEFAULT = "deflate"

GAUSSIAN_TRUNCATE_DEFAULT = 3.0  # 3*sigma is usually plenty for cartographic masks

# ===================================================================
# Exceptions
# ===================================================================

class ApplicationError(Exception):
    """Expected error that should be displayed without a traceback."""


class ConfigurationError(ApplicationError):
    """Invalid command or configuration supplied by the user."""


class CommandError(ApplicationError):
    """Failure while executing an external command."""


class FileError(ApplicationError):
    """Expected input/output file error."""


# ===================================================================
# Command Base Class
# ===================================================================

class Command:
    """Base class for all helper commands."""

    def __init__(self, args: argparse.Namespace):
        self.args = args

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        """Add command-specific arguments to the subparser."""
        raise NotImplementedError

    def execute(self):
        """Execute the command."""
        raise NotImplementedError

    @staticmethod
    def print_verbose(message: str):
        """Print a command status message."""
        print(message, flush=True)

    @staticmethod
    def _truncate(text: str, limit: int = 400) -> str:
        """Keep the beginning and end of a long string."""
        if len(text) <= limit:
            return text

        keep = int(limit * 0.4)
        omitted_count = len(text) - (keep * 2)

        return (f"{text[:keep]}"
                f" [ ... {omitted_count} chars truncated ... ] "
                f"{text[-keep:]}")

    def _run_command(self, command_list: List[str]):
        """
        Run an external command.

        Expected GDAL configuration and subprocess failures are translated
        into ApplicationError subclasses so they can be shown without a
        Python traceback.
        """
        command_list = [str(item) for item in command_list]

        printable_cmd = " ".join(
            shlex.quote(arg) for arg in command_list
        )

        self.print_verbose(
            f"         {self._truncate(printable_cmd)}"
        )

        # Validate GDAL creation options before launching the command.
        if command_list and command_list[0].startswith("gdal"):
            try:
                CoOptions().validate(command_list)

            except ValueError as e:
                raise ConfigurationError(
                    f"GDAL configuration error.\n"
                    f"Command: {command_list[0]}\n"
                    f"Issue: {e}"
                ) from e

        try:
            subprocess.run(
                command_list, capture_output=True, text=True, check=True, )

        except FileNotFoundError as e:
            raise CommandError(
                f"Command not found: '{command_list[0]}'"
            ) from e

        except subprocess.CalledProcessError as e:
            details = [f"External command failed with exit code {e.returncode}.",
                f"Command: {printable_cmd}", ]

            if e.stdout and e.stdout.strip():
                details.extend(
                    ["", "STDOUT:", e.stdout.strip(), ]
                )

            if e.stderr and e.stderr.strip():
                details.extend(
                    ["", "STDERR:", e.stderr.strip(), ]
                )

            raise CommandError("\n".join(details)) from e


# ===================================================================
# Input / Output Command Base Class
# ===================================================================

class IOCommand(Command):
    """Base class for commands that transform Input -> Output."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument(
            "input", help="Source file path", )
        parser.add_argument(
            "output", help="Destination file path", )
        parser.add_argument(
            "--overwrite", action="store_true", help="Overwrite existing output", )

    def execute(self):
        if not os.path.exists(self.args.input):
            raise FileError(
                f"Input file does not exist: '{self.args.input}'"
            )

        self.transform()

    def transform(self):
        raise NotImplementedError



# ===================================================================
# Utility Functions
# ===================================================================
# Tunables / safety constants
DEFAULT_OCTAVES = 3
MIN_FADE_PIXELS = 1
BASE_SCALE_MIN_PIXELS = 50.0
ALPHA_MAX = 255.0


def smoothstep01(t: np.ndarray) -> np.ndarray:
    """Classic smoothstep on [0..1]."""
    return t * t * (3.0 - 2.0 * t)



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
    """Return grid and georeferencing information for a raster."""
    if not os.path.exists(filepath):
        raise FileError(
            f"Cannot get raster info: file not found: '{filepath}'"
        )

    try:
        result = subprocess.run(
            ["gdalinfo", "-json", filepath],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            "Cannot get raster info: 'gdalinfo' was not found in PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip() or exc.stdout.strip()
        message = f"gdalinfo failed for '{filepath}'."
        if details:
            message += f"\n{details}"
        raise CommandError(message) from exc

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(
            f"gdalinfo returned invalid JSON for '{filepath}'."
        ) from exc

    try:
        width, height = info["size"]
        geo_transform = tuple(info["geoTransform"])
        srs_wkt = info["coordinateSystem"]["wkt"]

        resolution = (
            geo_transform[1],
            geo_transform[5],
        )

        corners = info["cornerCoordinates"]
        xmin = min(corner[0] for corner in corners.values())
        xmax = max(corner[0] for corner in corners.values())
        ymin = min(corner[1] for corner in corners.values())
        ymax = max(corner[1] for corner in corners.values())

    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise FileError(
            f"Raster does not contain the expected grid/georeferencing "
            f"information: '{filepath}'"
        ) from exc

    return {
        "width": width,
        "height": height,
        "geo_transform": geo_transform,
        "resolution": resolution,
        "extent": (xmin, ymin, xmax, ymax),
        "srs_wkt": srs_wkt,
    }

def _compute_pad(sigma: float, truncate: float = GAUSSIAN_TRUNCATE_DEFAULT) -> int:
    """Compute halo padding for Gaussian blur."""
    if sigma <= 0:
        raise ValueError("sigma must be > 0.")
    if truncate <= 0:
        raise ValueError("truncate must be > 0.")
    return int(math.ceil(sigma * truncate))


def generate_fractal_noise(
    h: int,
    w: int,
    base_scale: float,
    rng: np.random.Generator,
    octaves: int = DEFAULT_OCTAVES,
) -> np.ndarray:
    """Generate low-frequency fractal noise in [-1..1] with deterministic RNG."""
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
