
# GDALHelper

`gdal-helper` is a command-line tool that simplifies geospatial raster processing workflows.
It acts as a high-level wrapper around **GDAL**, **Rasterio**, and **NumPy**, abstracting away the
complexity of raw GDAL CLI arguments and enabling complex pixel manipulation (such as
texture shading and vignette generation) within a script or build pipeline.

This tool is designed for GIS professionals and data scientists who use raster data  and need a reliable, 
scriptable way to simplify complex tasks.

##  Key Benefits

*   **Pipeline / Script Ready:** Designed to be used with scripts or build systems (e.g.,
    Makefiles, LiteBuild).
*   **Abstraction:** Replaces complex `gdalwarp`/`gdal_calc.py` chains with semantic commands like
    `align_raster` or `hillshade_blend`.
*   **Pixel-Level Control:** Uses NumPy/SciPy for operations that are difficult in standard GDAL,
    such as distance-based vignetting and texture-shaded blending.
*   **Safety:** Includes a validation step (`validate_raster`) to fail builds fast if artifacts
    are empty or corrupt.
*   **Extensible:** Uses a strict **Command Pattern** with auto-registration. Adding a new tool is
    as simple as adding a class and a decorator.

##  Requirements

*   Python 3.10+
*   GDAL (System binaries and Python bindings)
*   `rasterio`
*   `numpy`
*   `scipy`
*   `pmtiles` (CLI executable for PMTiles conversion)

##  Command Table of Contents

1.  **Raster Manipulation**
    *   [`align_raster`](#align_raster) - Modify the grids and resolution of a Tiff to match another Tiff
    *   [`create_subset`](#create_subset) - Extract a crop of an image.
    *   [`apply_vignette`](#apply_vignette) - Fade edges with organic noise.
2.  **Blending & Visualization**
    *   [`hillshade_blend`](#hillshade_blend) - Intelligently blend a hillshade with a color relief
    *   [`masked_blend`](#masked_blend) - Composite layers using a mask.
    *   [`adjust_color_file`](#adjust_color_file) - Programmatically shift HSV values in a gdaldem color config file.
3.  **Validation and Publishing**
    *   [`create_mbtiles`](#create_mbtiles) - Generate MBTiles with pyramids from a Tiff
    *   [`create_pmtiles`](#create_pmtiles) - Convert an MBTile to PMTile.
    *   [`validate_raster`](#validate_raster) - Validate that a raster meets a minimum size
    *   [`add_version` / `get_version`](#add_version--get_version) - Stamp a Git hash on a Tiff.  Retrieve hash from Tiff.
    *   [`publish`](#publish) - Deploy artifacts.

##  Usage

The utilities are accessed via the main entry point `gdal_helper.py`. All commands support a
`-v/--verbose` flag for detailed logging.

```bash
# General Syntax
python -m GDALHelper.gdal_helper <command> [inputs] [options]

# Example
python -m GDALHelper.gdal_helper align_raster input.tif template.tif output.tif
```

---

## Command Reference

### 1. Raster Manipulation

#### `align_raster`
Resamples and reprojects a source raster to perfectly match the grid, extent, and resolution of a
template raster. Essential for ensuring pixel alignment before blending layers.

```bash
gdal-helper align_raster <source> <template> <output> \
  --resampling-method bilinear \
  --co COMPRESS=DEFLATE
```

#### `create_subset`
Extracts a cropped preview or subset from a larger raster based on relative anchor points.

**Anchors:**
Anchors define the center point of the crop relative to the image size (0.0 to 1.0).
*   `0.0, 0.0`: Extract Top-Left corner.
*   `0.5, 0.5`: Extract Center of the image (Default).
*   `1.0, 1.0`: Extract Bottom-Right corner.


```bash
gdal-helper create_subset input.tif output.tif \
  --size 4000 \
  --x-anchor 0.5 \
  --y-anchor 0.5
```

#### `apply_vignette`
Adds an Alpha gradient to the edge of a raster, creating a vignette fade. This is used so that an
overlayed raster blends seamlessly into the layer under it.

**Parameters:**
*   `--border` (float): Controls the width of the fade gradient. Calculated as a % of the image's
    smallest dimension (Height or Width). Example: 5.0 creates an alpha fade that covers 5% of the image.
*   `--noise` (float): Adds high-frequency dithering to the fade. Calculated as a % of
    the 'border' size. This hides digital banding and makes the gradient look smoother.
*   `--warp` (float): Adds low-frequency fractal distortion to the edge shape.
    Calculated as a % of the 'border' size. This breaks up straight edge lines, making the edge look
    organic. *Note: The visible image area shrinks slightly as warp increases to ensure edges
    remain soft.*

```bash
gdal-helper apply_vignette input.tif output.tif --border 10 --warp 60 --noise 20
```

### 2. Blending & Visualization

#### `hillshade_blend`
Blends a grayscale hillshade onto a color relief map using **Texture Shading** logic.

Unlike a standard multiply blend, this uses a "Luminosity Mask" to
protect colors. Extreme shadows (<10) and extreme highlights (>245) in the hillshade are softened
mathematically. This preserves color saturation in dark areas and prevents light areas from
washing out to white, while still enhancing terrain detail in the mid-tones.

```bash
gdal-helper hillshade_blend hillshade.tif color.tif output.tif \
  --co COMPRESS=JPEG --co JPEG_QUALITY=85
```

#### `masked_blend`
Composites two layers (Layer A and Layer B) using a third layer as a blending mask.

**Inputs:**
*   **Layer A / Layer B:** RGB (3-band) or RGBA (4-band).
*   **Mask:** Must be a **Single-Band Grayscale** image.

**Mask Logic:**
*   **White (255):** Shows 100% Layer A.
*   **Gray Values:** Produces a weighted blend (e.g., 128 results in 50% A / 50% B).
*   **Black (0):** Shows 100% Layer B.

*Note: All three inputs must have identical dimensions and projections. Use `align_raster` first if
they do not match.*

```bash
gdal-helper masked_blend layerA.tif layerB.tif mask.tif output.tif 
```

#### `adjust_color_file`
Programmatically updates the color definitions in a gdaldem color-relief text file (e.g., changing
saturation, shifting hues, or adjusting brightness). This allows you to have a master gdaldem
color-relief file and create variants that will still match the overall elevation and coloration of
the original. It updates the text definition file, not the raster.

[See detailed explanation at end.](#adjust_color_file-details)

```bash
gdal-helper adjust_color_file input_colors.txt output_colors.txt \
  --saturation 1.2 --mid-adjust 0.1
```

### 3. Output Formats

#### `create_mbtiles`
Converts a GeoTIFF to an MBTiles archive and generates internal overview pyramids (`gdaladdo`).
Supports both PNG and JPEG formats.

```bash
gdal-helper create_mbtiles input.tif output.mbtiles \
  --format JPEG --quality 80 --levels 2 4 8 16 32
```

#### `create_pmtiles`
Converts an MBTiles archive into a cloud-native PMTiles archive. Requires the `pmtiles` executable
in the system PATH.

```bash
gdal-helper create_pmtiles input.mbtiles output.pmtiles
```

### 4. Validation and Publishing

#### `validate_raster`
Asserts that a raster exists and meets minimum size requirements. Useful for stopping a build
pipeline if an upstream process produced a 1x1 pixel empty file (a common GDAL failure mode).

```bash
gdal-helper validate_raster input.tif --min-bytes 5000 --min-pixels 500
```

#### `add_version` / `get_version`
Embeds the current **Git Commit Hash** of the working directory into  GeoTIFF metadata tags, or retrieves it. This ensures 
data provenance, allowing you to trace exactly which version of the configuration generated a specific map TIFF.

*   If the repository has uncommitted changes, a **`-dirty`** suffix is appended to the hash.
*   **Requirements:** Git must be installed, and the command must be run from within a versioned git repository.
*   This only works for TIFF files, not jpg, png, pmtile, or mbtile

```bash
gdal-helper add_version input.tif
gdal-helper get_version input.tif
```

#### `publish`
Copies or SCPs a file to a destination directory, optionally stamping version metadata before
transfer.

```bash
gdal-helper publish build/map.tif /var/www/maps/ --stamp-version
```

---

## Extending GDALHelper

GDALHelper is built on a **Command Pattern**. To add a new tool, define a class that inherits from
`Command` or `IOCommand` and register it with the decorator.

**Example:**

```python
from GDALHelper.gdal_helper import IOCommand, register_command

@register_command("my_new_tool")
class MyNewTool(IOCommand):
    """Description of what the tool does."""
    
    def run_transformation(self):
        # self.args.input and self.args.output are auto-populated
        print(f"Processing {self.args.input}...")
```

---

## `adjust_color_file` Details

This command takes a gdaldem color-relief definition file and shifts Hue, Saturation, and Value to
produce a new variant of the definition file. It is useful for creating multiple, stylistically
harmonious color schemes from a single base color ramp file.

Instead of manually editing multiple gdaldem color files to create variations (e.g., for a muted
variation, different biomes, lighting, or map styles), this command allows you to define one
high-quality **base ramp** and then programmatically generate all other variations from it.

### Key Benefits

*   **Maintain a Single Source of Truth:** You only need to edit your one base color ramp. If you
    decide to adjust an elevation tier or change a core color, you can simply re-run this command
    to regenerate all its stylistic variations instantly.
*   **Guarantee Stylistic Harmony:** Because all variations are derived from the same source, they
    are guaranteed to share the same core structure (elevation tiers, contrast profile), ensuring
    your maps have a professional and consistent aesthetic.
*   **Subtle and High-Quality Adjustments:** The color shifting algorithm is designed to produce
    subtle, natural-looking shifts while intelligently protecting neutral tones (greys, whites,
    blacks) from being artificially colorized.

> **Note:** This command operates on the color definition text file itself, *before* it is used by
> `gdaldem color-relief`. It does not modify raster images directly. It only works with numerical
> RGB(A) color definitions, not named colors.

#### How the Adjustments Work

The command works by converting the RGB colors from the input file into the HSV (Hue, Saturation,
Value) color model, which is a more intuitive way to manipulate color properties.

**Adjust Saturation (Color Intensity)**
Saturation is the intensity of color.
*   The `--saturation` argument acts as a multiplier.
*   A value of `1.5` makes all colors 50% more vibrant.
*   A value of `0.25` makes all colors 25% as vibrant (e.g. more gray).

**Adjust Value (Brightness & Contrast)**
Value is the brightness of a color, ranging from darkest (0) to brightest (1.0). Instead of a
single brightness control, you have independent control over 3 different tonal regions. These all
work by adding the specified value.

*   `--shadow-adjust`: Adds to the brightness of the darkest colors. Use a positive value (e.g.,
    `0.1`) to brighten shadows, or a negative value to further darken them.
*   `--mid-adjust`: Adds to the brightness of the mid-tones, affecting the main body of the color
    ramp.
*   `--highlight-adjust`: Adds to the brightness of the brightest colors. Use a positive value to
    further brighten bright areas, or a negative value to darken them.

These adjustments are applied as a smooth, weighted blend. A color with a brightness of `0.7` will
be affected by both the `mid-adjust` and `highlight-adjust` parameters, ensuring there are no hard
edges in the final ramp.

**Shift Hue (Color Tone)**
Hue is the pure color, represented as a circle from 0 to 360 degrees (e.g., 0° is red, 120° is
green, 240° is blue).

*   `--min-hue`, `--max-hue`: You can define a specific range of hues to shift (e.g., only affect
    the greens, from `min-hue: 80` to `max-hue: 140`).
*   `--target-hue`: You can then shift the colors within that range toward a new `target-hue`.
*   The shift is designed to be subtle with a drop-off in shift towards the edge of the specified
    range.
*   **Wrap-Around Range:** To select the range that crosses over the 0/360° mark (red), set
    `--min-hue` to a larger value than `--max-hue`.
    *   *Example:* `--min-hue 280 --max-hue 80` selects violets, magentas, reds, oranges, and
        yellows, leaving greens and blues untouched.

**Elevation**
*   The `--elev_adjust` argument acts as a simple multiplier on all elevation values in the file. A
    value of `1.1` would scale all elevations up by 10%.

---

**Usage:** `gdal-helper adjust_color_file <input> <output> [options...]`

**Example:**
```sh
gdal-helper adjust_color_file base_ramp.txt arid_ramp.txt --target-hue 46 --saturation 0.8
```

**Arguments:**

| Argument             | Type   | Default  | Description                                                  |
|:---------------------|:-------|:---------|:-------------------------------------------------------------|
| `input`              | str    | -        | The source GDAL color definition file.                       |
| `output`             | str    | -        | The path for the new, adjusted color file.                   |
| `--saturation`       | float  | `1.0`    | Multiplies the saturation. `1.1` is a 10% increase.          |
| `--shadow-adjust`    | float  | `0.0`    | Additively adjusts the brightness of dark colors.            |
| `--mid-adjust`       | float  | `0.0`    | Additively adjusts the brightness of mid-range colors.       |
| `--highlight-adjust` | float  | `0.0`    | Additively adjusts the brightness of light colors.           |
| `--min-hue`          | float  | `0.0`    | Lower bound of the hue range to adjust (0-360).              |
| `--max-hue`          | float  | `0.0`    | Upper bound of the hue range to adjust (0-360).              |
| `--target-hue`       | float  | `0.0`    | Target hue that colors in the range will be shifted towards. |
| `--elev_adjust`      | float  | `1.0`    | Multiplies all elevation values. `1.1` is a 10% increase.    |