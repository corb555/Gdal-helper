
# GDALHelper

`gdal-helper` is a Python command-line tool that simplifies complex geospatial raster workflows. It wraps **GDAL**,
**Rasterio**, and **NumPy**
with high-level commands, simplifying tasks like texture shading, alignment, and vignette generation
that usually require brittle shell scripts.

Designed for GIS professionals and data scientists who need a reliable, scriptable way to manage raster pipelines.

> NOTE: This is beta software
> Breaking changes to parameters may be introduced in subsequent releases

## Key Benefits

*   **Simplify:** Replaces complex `gdalwarp`/`gdal_calc.py` chains with readable actions like `align_raster` or `hillshade_blend`.
*   **Safety & Validation:** The `run` command wraps standard GDAL tools but validates your creation options (`-co`) against the driver specifications, preventing silent failures where GDAL ignores invalid flags.
*   **Pixel-Level Control:** Leverages NumPy/SciPy for advanced operations difficult to do in standard GDAL, such as distance-based vignetting and
    texture-shaded blending.
*   **Extensible:** Built on a strict **Command Pattern**. Adding a new tool is as simple as defining a Python class with a decorator.

##  Commands

1.  **Raster Manipulation**
    *   [`align_raster`](#align_raster) - Modify the grids and resolution of a Tiff to match another Tiff
    *   [`create_subset`](#create_subset) - Extract a crop of an image.
    *   [`apply_vignette`](#apply_vignette) - Fade edges with organic noise.
2.  **Blending & Visualization**
    *   [`create_biome_layer`](#create_biome_layer) - Procedural terrain shader blending Arid, Humid, and Steep layers.
    *   [`hillshade_blend`](#hillshade_blend) - Intelligently blend a hillshade with a color relief
    *   [`masked_blend`](#masked_blend) - Composite layers using a mask.
    *   [`adjust_color_file`](#adjust_color_file) - Programmatically shift HSV values in a gdaldem color config file.
    *   [`reclassify`](#reclassify) - Reclassifies a categorical raster into a compact 8-bit class raster.
3.  **Validation and Publishing**
    *   [`run`](#run) - Execute standard GDAL commands with strict option validation.
    *   [`create_mbtiles`](#create_mbtiles) - Generate MBTiles with pyramids from a Tiff
    *   [`create_pmtiles`](#create_pmtiles) - Convert an MBTile to PMTile.
    *   [`validate_raster`](#validate_raster) - Validate that a raster meets a minimum size
    *   [`add_version` / `get_version`](#add_version--get_version) - Stamp a Git hash on a Tiff.  Retrieve hash from Tiff.
    *   [`publish`](#publish) - Deploy artifacts.

##  Usage

```bash
gdal-helper <command> [inputs] [options]

# Example
gdal-helper align_raster input.tif template.tif output.tif
```

---

## Command Reference

### 1. Raster Manipulation

#### `align_raster`
Resamples a source raster to  match the grid, extent, and resolution of a
template raster. Essential for ensuring pixel alignment before blending layers.

```bash
gdal-helper align_raster <source> <template> <output> \
  --resampling-method bilinear \
  --co COMPRESS=DEFLATE
```

#### `create_subset`
Extracts a cropped subset from a raster. Position of crop is based on relative anchor points.

**Anchors:**
Anchors define the center point of the crop relative to the image (0.0 to 1.0).
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
Adds an alpha gradient at the edge of a raster so overlays blend cleanly into the layer below.

**Parameters:**
*   `--border` (float): Width of the fade as a % of the smaller image dimension.
    Example: 5.0 = fade covers ~5% of the image.
*   `--noise` (float): Dither area as a % of border (reduces banding).
*   `--warp` (float): Fractal distortion size as a % of border (breaks up straight edges).

```bash
gdal-helper apply_vignette input.tif output.tif --border 10 --warp 60 --noise 20
```

### 2. Blending & Visualization

#### `create_biome_layer`
Similar to gdaldem color-relief, this creates a color-relief raster from a DEM, but produces a more 
natural-looking terrain raster that responds to
climate and geomorphology—e.g., deserts vs. forests vs. alpine rock.

The color relief is created by blending three color palettes — Arid, Humid, and Steep — using precipitation data,
slope, and topographic position (ridge vs gully).  The arid palette can also be a blend of arid and
arid_red using a surface lithology GeoTiff.

The blended palette is calculated per-pixel in a single pass,
rather than generating multiple intermediate layers and blending them serially. This is faster and preserves
clean, saturated colors.

**What it does**

1. **Base layer**: Computes the palette from the DEM using the Humid ramp and elevation (similar to gdaldem color-relief).

2. **Precip blend** (optional): Blends the Arid Pallete and Humid Palette per pixel using a precipitation 
GeoTIFF. `--no-precip` disables this step.

3. **Micro-climate** (optional): Uses TPI (Topographic Position Index; ridge vs. gully) to bias ridges drier 
and gullies wetter in the precipitation input to Precip Blend. `--no-tpi` disables this step.

4. **Cliffs** (optional): Blends in the Steep (rock) palette per pixel based on slope. `--no-slope` disables this step.

**Inputs:**
DEM file -  elevation GeoTIFF
Precipitation GeoTIFF - (optional) gridded annual precip in mm
Slope GeoTIFF - (optional) e.g. output from gdaldem slope
Color ramps (same format as gdaldem color-relief):
   arid -  palette for dry areas
   humid - palette for wet areas
   steep - palette for cliffs/rock

**Parameters:**
**Precip Blend**
--precip-min - values ≤ this are 100% Arid
--precip-max - values ≥ this are 100% Humid
Values between are blended smoothly.  

**Cliffs**
--slope-min 25 - values ≤ this are 0% Steep
--slope-max 45 - values ≥ this are 100% Steep
Values between are blended smoothly.

```bash
gdal-helper create_biome_layer build/Yosemite/Yosemite output.tif \
  --precip-min 150 --precip-max 750 \
  --slope-min 25 --slope-max 45
```


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

**Mask:**
*   **White (255):** Shows 100% Layer A.
*   **Gray Values:** Produces a weighted blend (e.g., 128 results in 50% A / 50% B).
*   **Black (0):** Shows 100% Layer B.

*Note: All three inputs must have identical dimensions and projections. Use `align_raster`  if
they do not match.*

```bash
gdal-helper masked_blend layerA.tif layerB.tif mask.tif output.tif 
```

#### `reclassify`

Reclassifies a categorical raster into a compact 8-bit class raster, with an optional sidecar alpha mask for downstream
blending/feathering.  Takes a list of categories, filters the raster to those and outputs them as 8-bit values. 

This is ideal for large classification layers (e.g., LandFire EVT/FBFM) when you only need a small subset of categories
and want faster rendering using a much smaller and simpler output.

> The output can only support 255 categories

**Inputs:**

* **Source Raster:** A **Single-Band Categorical** raster with integer classes, e.g. EVT.
* **Config (`--config`):** YAML file defining categories and output values and optional palette/alpha behavior.

**Outputs:**

* **Class Raster (`output.tif`):** A **Single-Band `byte`** raster where each pixel is replaced with a derived class value.

    * **Default Value:** Pixels that match no rule are set to `options default_value` (usually `0`).
    * **Priority:** Rules are applied **in config order**; the first matching rule wins.
    * **Palette (Optional):** If any class defines `rgb`, an embedded GeoTIFF palette is written so utilities
      like QGIS can render it as a color palette layer.
* **Alpha Raster (Optional):** A **Single-Band `byte`** raster written as a *sidecar file*.

    * **255:** Pixel matched any rule (presence).
    * **0:** Pixel matched no rule (absence).
    * Use `gdal-helper blur` to soften edges for cartographic blending.

**Config (`--config`) File:**

* **`classes[]`**: Each rule defines `name`, `ids`, optional `value`, and optional `rgb`.
* **`options.default_value`**: Output value for unmatched pixels.
* **`options.alpha.enabled`**: Enable/disable alpha output and set its path/values.
* **`options.report_unmapped`**: Optionally report source IDs encountered that were not mapped.

Sample:
```yaml
config_type: "Reclassify"
classes:
  - name: dark_rock   # Only used as a comment
    rgb: "4d4339"     # Optional - Used to fill in color palette for this item
    ids: [9033, 9153, 7734]   # The ID or ID's that will map to this value
    # value: omitted -> value defaults to 1, (config index)
  - name: playa
    rgb: "e3dbca"
    ids: [9008]
    # value: omitted -> value defaults to 2, (config index)
  - name: water
    rgb: "7d95ab"
    ids: [7735]
    value: 10   # Optional - explicit value for this category

options:
  default_value: 0   # Value for unmatched items. Default=0
  nodata_value: 0    # nodata value. Default=0
  tile_size: 256     # tile size. Default=255
  compress: deflate  # compression. Default=deflate

  alpha:
    enabled: true  # Optional - create alpha sidecar.  Default=true
    #  Optional - Default: name derived from main output as "<stem>_alpha.tif"
    output: "{BUILD_DIR}/{profile_name}_EVT_subset_alpha.tif"
    on: 255  # Value for any category match.  Default=255
    off: 0   # Value for no category.  Default=0

  report_unmapped:
    enabled: true
    max_ids: 50


```

*Note: Reclassify does not resample or align rasters. If your downstream layers must match dimensions/projection, run
`align_raster` (or your equivalent step) first.*

```bash
gdal-helper reclassify --config reclassify.yml LF2024_EVT.tif EVT_subset.tif
```


#### `adjust_color_file`
Programmatically updates the color definitions in a gdaldem color-relief text file changing
saturation,  hues, or  brightness. This allows you to have one master gdaldem
color-relief file and create variants that match the overall elevation and coloration of
the original. It updates the gdaldem definition file, not the raster's colors.

[See detailed explanation at end.](#adjust_color_file-details)

```bash
gdal-helper adjust_color_file input_colors.txt output_colors.txt \
  --saturation 1.2 --mid-adjust 0.1
```

### 3. Output Formats

#### `create_mbtiles`
Converts a GeoTIFF to an MBTiles file and generates internal overview pyramids (`gdaladdo`).

```bash
gdal-helper create_mbtiles input.tif output.mbtiles \
  --format JPEG --quality 80 --levels 2 4 8 16 32
```

#### `create_pmtiles`
Converts an MBTiles file into a  PMTiles file. Requires the `pmtiles` executable
in the system PATH.

```bash
gdal-helper create_pmtiles input.mbtiles output.pmtiles
```

### 4. Validation and Publishing

#### `run`
A passthrough command that executes standard GDAL tools (like `gdal_translate` or `gdalwarp`) but adds **Creation Option Validation**.

Standard GDAL tools will often silently ignore invalid `-co` flags (e.g., typos like `COMPRESS=BAD_ALGO` or unsupported options like `WEBP_QUALITY` on a GTiff driver). This wrapper queries the driver capabilities first and throws a hard error if you try to use an invalid option.

```bash
# This will fail immediately with a clear error because 'FAKE' is not a valid compression
gdal-helper run gdal_translate -of GTiff -co COMPRESS=FAKE input.tif output.tif
```

#### `validate_raster`
Asserts that a raster exists and meets minimum size requirements. Useful for stopping a build
pipeline if an upstream process produced a 1x1 pixel empty file (a common GDAL failure mode).

```bash
gdal-helper validate_raster input.tif --min-bytes 5000 --min-pixels 500
```

#### `add_version` / `get_version`
Embeds the current **Git Commit Hash** of the working directory into GeoTIFF metadata tags, or retrieves it. This ensures
data provenance, allowing you to trace exactly which version of the configuration generated a specific  TIFF.

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

GDALHelper is built on a **Command Pattern**. To add a new tool, define a class that inherits from `Command` or `IOCommand` and register it with
the decorator in `commands.py`.

### Choosing a Base Class
*   **`IOCommand` (Recommended):** Use this if your tool takes one input file, processes it, and produces one output file. It automatically handles
    the boilerplate for `input`, `output`, and validation.
*   **`Command`:** Use this for complex tools with multiple inputs (like blending) or no outputs (like inspecting metadata).

### How to Implement
Your class needs to implement two key methods:

1.  **`add_arguments(parser)`**:
    *   This is where you define your custom CLI flags (e.g., `--size`, `--opacity`).
    *   **Crucial:** If using `IOCommand`, call `super().add_arguments(parser)` first. This automatically adds the standard positional `input` and `output`
        arguments so you don't have to.

2.  **`run_transformation()`**:
    *   This is where your logic lives.
    *   You have access to all command line arguments via `self.args` (e.g., `self.args.input`, `self.args.size`).
    *   Use `self._run_command(["cmd", "arg"])` to execute shell commands safely with logging.
    *   Use `self.print_verbose()` for logging info that should only appear in verbose mode.

**Example Implementation:**

```python
@register_command("create_subset")
class CreateSubset(IOCommand):
    """Extracts a smaller section from a large raster file."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        # 1. Register the standard 'input' and 'output' args from the parent
        super(CreateSubset, CreateSubset).add_arguments(parser)

        # 2. Add your custom flags
        parser.add_argument(
            "--size", type=int, default=4000, help="The width/height of the crop."
        )
        parser.add_argument(
            "--x-anchor", type=float, default=0.5,
            help="Horizontal center (0.0=left, 0.5=center, 1.0=right)."
        )

    def run_transformation(self):
        # The parent IOCommand has already verified that self.args.input exists.
        
        width, height = _get_image_dimensions(self.args.input)

        # Calculate crop geometry
        x_offset = int((width - self.args.size) * self.args.x_anchor)
        y_offset = int((height - self.args.size) * self.args.y_anchor)
        
        # Build the gdal_translate command
        command = [
            "gdal_translate", 
            "-srcwin", str(x_offset), str(y_offset), str(self.args.size), str(self.args.size),
            self.args.input, 
            self.args.output
        ]
        
        # Execute
        self._run_command(command)
        self.print_verbose(f"✅ Subset created: {self.args.output}")
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
> `gdaldem color-relief`. It does not modify raster images directly.
> It only works with numerical RGB(A) color definitions, not named colors.

#### How the Adjustments Work

The command works by converting the RGB colors from the input file into the HSV (Hue, Saturation,
Value) color model, which is a more intuitive way to manipulate color properties.

**Adjust Saturation (Color Intensity)**
Saturation is the intensity of color.
*   The `--saturation` argument acts as a multiplier.
*   A value of `1.5` makes all colors 50% more vibrant.
*   A value of `0.25` makes all colors 25% as vibrant (e.g. more gray).

**Adjust Value (Brightness)**
Value is the brightness, ranging from darkest (0) to brightest (1.0). Instead of a
single brightness control, you have independent control over 3 different tonal regions. These all
work by **adding** the specified value.

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

**Adjust Elevation**
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
| `--saturation`       | float  | `1.0`    | _Multiplies_ the saturation. `1.1` is a 10% increase.        |
| `--shadow-adjust`    | float  | `0.0`    | _Additively_ adjusts the brightness of dark colors.          |
| `--mid-adjust`       | float  | `0.0`    | _Additively_ adjusts the brightness of mid-range colors.     |
| `--highlight-adjust` | float  | `0.0`    | _Additively_ adjusts the brightness of light colors.         |
| `--min-hue`          | float  | `0.0`    | Lower bound of the hue range to adjust (0-360).              |
| `--max-hue`          | float  | `0.0`    | Upper bound of the hue range to adjust (0-360).              |
| `--target-hue`       | float  | `0.0`    | Target hue that colors in the range will be shifted towards. |
| `--elev-adjust`      | float  | `1.0`    | _Multiplies_ all elevation values. `1.1` is a 10% increase.  |

## Installation
`pip install GDALHelper`

##  Requirements

*   Python 3.10+
*   GDAL (System binaries and Python bindings)
*   `rasterio`
*   `numpy`
*   `scipy`
*   `pmtiles` (CLI executable for PMTiles)