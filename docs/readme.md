
# GDAL Helper: High-Level Workflows for Geospatial Raster Processing

## Overview

`gdal-helper` is a command-line tool that simplifies complex GDAL workflows. It acts as a high-level wrapper around the 
powerful GDAL/OGR library, consolidating multi-step sequences of commands into single, intuitive, and reproducible actions.  It does not replace all GDAL tools, but simply
provides a wrapper for some of the more complex cases.

This tool is designed for GIS professionals and data scientists who use GDAL with raster data (like Digital Elevation Models or 
satellite imagery) and need a reliable, scriptable way to simplify some complex tasks.  

### Key Benefits

*   **Simplifies Complexity:** Replaces some long, error-prone GDAL command chains with a single command (e.g., `gdal-helper align_raster ...`).
*   **Ensures Correctness:** Implements best practices for common GIS tasks like raster alignment, reducing the risk of subtle errors.
*   **Automates Workflows:** Works well with automated build systems, providing features like version stamping and marker files.

## General Usage

The `gdal-helper` command follows a standard pattern:
```sh
gdal-helper <command> [arguments...] [options...] [--verbose]
```

---

## Commands Summary

| Command                                       | Description                                                                     |
|:----------------------------------------------|:--------------------------------------------------------------------------------|
| **[`align_raster`](#align_raster)**           | Aligns a raster to perfectly match a template's grid (SRS, extent, resolution). |
| **[`masked_blend`](#masked_blend)**           | Blends two RGB layers using a third layer as a mask via `gdal_calc`.            |
| **[`create_subset`](#create_subset)**         | Extracts a smaller rectangular section from a large raster file.                |
| **[`adjust_color_file`](#adjust_color_file)** | Adjusts color and elevation values in a `gdaldem color-relief` text file to     |
|                                               | easily create variants from one base definition.                                |
| **[`publish`](#publish)**                     | Publishes a file to a remote or local destination, with version stamping.       |
| **[`add_version`](#add_version)**             | Embeds the current git commit hash into a GeoTIFF's metadata.                   |
| **[`get_version`](#get_version)**             | Reads the embedded version hash from a GeoTIFF's metadata.                      |

---

## Command Reference

### `align_raster`

Resamples a source raster to  match a template raster's SRS, extent, and resolution. This is essential for ensuring pixels are 
 aligned before performing calculations.

**Usage:** `gdal-helper align_raster <source> <template> <output> [options...]`

**Example:**
```sh
gdal-helper align_raster mask.tif base_dem.tif mask_aligned.tif -r cubic --co "COMPRESS=DEFLATE"
```

**Arguments:**

| Argument                    | Type  | Description                                                                                |
|:----------------------------|:------|:-------------------------------------------------------------------------------------------|
| `source`                    | str   | The raster file to be aligned.                                                             |
| `template`                  | str   | The raster file with the desired grid.                                                     |
| `output`                    | str   | The path for the new, aligned output file.                                                 |
| `-r`, `--resampling-method` | str   | Resampling method (e.g., `near`, `bilinear`, `cubic`). **Default: `bilinear`**.            |
| `--co`                      | str   | Creation option for the output driver (e.g., `COMPRESS=JPEG`). Can be used multiple times. |

**Technical Details:**
*   Runs `gdalinfo -json` on the `<template>` file to extract its projection (SRS), extent (`-te`), and resolution (`-tr`).
*   Executes a `gdalwarp` command using these extracted values to resample the `<source>` file.

---

### `masked_blend`

Blends two RGB layers (A and B) using a single-band third layer (C) as a mask. The layers should be aligned first (see `align_raster`). 
This is useful for complex styling, such as blending an arid color relief with a temperate one using a precipitation mask.

**Usage:** `gdal-helper masked_blend <layerA> <layerB> <mask> <output> --calc <formula> [options...]`

**Example:**
```sh
gdal-helper masked_blend hillshade.tif color.tif mask.tif final.tif --calc "A*(C/255.0) + B*(1-C/255.0)"
```

**Arguments:**

| Argument      | Type  | Description                                                  |
|:--------------|:------|:-------------------------------------------------------------|
| `layerA`      | str   | The foreground layer (band `A`).                             |
| `layerB`      | str   | The background layer (band `B`).                             |
| `mask`        | str   | The mask file (band `C`).                                    |
| `output`      | str   | The path for the blended output file.                        |
| `--calc`      | str   | The `gdal_calc` numpy formula to apply.                      |
| `--temp-dir`  | str   | Directory for intermediate files. **Default: `.`**.          |
| `--keep-temp` | flag  | If present, temporary single-band files will not be deleted. |

**Technical Details:**
*   For each channel (R, G, B), it uses `gdal_translate -b <band>` to extract the single bands from `layerA` and `layerB`.
*   It then uses `gdal_calc.py` with the user-provided formula to blend the single bands.
*   Finally, it uses `gdal_merge.py -separate` to combine the three blended channel files into a single RGB output.

---

### `create_subset`

Extracts a rectangular section from a raster file, useful for creating fast-rendering previews or test samples.

**Usage:** `gdal-helper create_subset <input> <output> [options...]`

**Example:**
```sh
gdal-helper create_subset large_dem.tif preview.tif --size 1024
```

**Arguments:**

| Argument     | Type   | Description                                                                       |
|:-------------|:-------|:----------------------------------------------------------------------------------|
| `input`      | str    | The source raster file.                                                           |
| `output`     | str    | The path for the new subset file.                                                 |
| `--size`     | int    | The width and height of the square crop in pixels. **Default: `4000`**.           |
| `--x-anchor` | float  | Horizontal anchor for the crop (0=left, 0.5=center, 1=right). **Default: `0.5`**. |
| `--y-anchor` | float  | Vertical anchor for the crop (0=top, 0.5=center, 1=bottom). **Default: `0.5`**.   |

**Technical Details:**
*   Uses `gdalinfo` to get the dimensions of the `<input>` file.
*   Constructs and executes a `gdal_translate` command, using the `-srcwin` option to define the pixel offset and size of the desired window.

---

### `adjust_color_file`

This takes a gdaldem color-relief definition file and shifts Hue, Saturation, Value to produce a new variant 
of the definition file. It is useful for creating multiple, stylistically harmonious color schemes from a single base color ramp file.

Instead of manually editing multiple gdaldem color files to create variations (e.g., for a muted variation, different biomes, lighting,
or map styles), this 
command allows you to define one high-quality **base ramp** and then programmatically generate all other variations from it.

### Key Benefits

*   **Maintain a Single Source of Truth:** You only  need to edit your one base color ramp. If you decide to adjust an elevation tier 
or change a core color, you can simply re-run this command to regenerate all its stylistic variations instantly.
*   **Guarantee Stylistic Harmony:** Because all variations are derived from the same source, they are guaranteed to share the same core 
structure (elevation tiers, contrast profile), ensuring your maps have a professional and consistent aesthetic.
*   **Subtle and High-Quality Adjustments:** The color shifting algorithm is designed to produce subtle, natural-looking 
shifts while intelligently protecting neutral tones (greys, whites, blacks) from being artificially colorized.

> **Note:** This command operates on the color definition text file itself, *before* it is used by `gdaldem color-relief`. It 
> does not modify raster images directly. 
> It only works with numerical RGB(A) color definitions, not named colors.

---

#### How the Adjustments Work

The command works by converting the RGB colors from the input file into the HSV (Hue, Saturation, Value) color model, which is a more 
intuitive way to manipulate color properties.  You can define adjustments as follows.

**Adjust Saturation (Color Intensity)**
Saturation is the intensity of color.
_To shift the saturation:_
*   The `--saturation` argument acts as a multiplier.
*   A value of `1.5` makes all colors 50% more vibrant.
*   A value of `0.25` makes all colors 25% as vibrant (e.g. more gray).

**Adjust Value (Brightness & Contrast)**
Value is the brightness of a color, ranging from darkest (0) to brightest (1.0).
_To shift the brightness:_

Instead of a single brightness control, you have independent control over 3 different 
tonal regions.  These all work by adding the specified value.  For example an entry with a value of 0.2 (dark) and a --shadow-adjust of
0.1 would end up approximately:  0.2 + 0.1 = 0.3.  The exact value would be slightly different because the algorithm blends
the adjustments for the most natural effect.

*   `--shadow-adjust`: Adds to the brightness of the darkest colors . Use a positive value (e.g., `0.1`) to brighten 
shadows, or a negative value to further darken them.
*   `--mid-adjust`: Adds to the brightness of the mid-tones, affecting the main body of the color ramp.
*   `--highlight-adjust`: Adds to the brightness of the brightest colors. Use a positive value (e.g., `0.1`) to further brighten
    bright areas, or a negative value to darken them.

These adjustments are applied as a smooth, weighted blend. A color with a brightness of `0.7` will be affected by both the
`mid-adjust` and `highlight-adjust` parameters, ensuring there are no hard edges in the final ramp. However, for the most natural results, 
make gradual changes between the three values.

**Shift Hue (Color Tone)**
Hue is the pure color, represented as a circle from 0 to 360 degrees (e.g., 0° is red, 120° is green, 240° is blue).
_To shift the hue:_
*   `--min-hue`, `max-hue` - You can define a specific range of hues to shift (e.g., only affect the greens, from `min-hue: 80` to `max-hue: 140`).
*   `--target-hue`  - You can then shift the colors within that range toward a new `target-hue`.
*   The shift is designed to be subtle with a drop-off in shift towards the edge of the specified range, however very large hue
    shifts may still produce artificial results.
*   Neutral colors (greys, whites, blacks) are protected from being colorized.
*   The algorithm correctly handles the circular nature of hue. For example, shifting a
    color at 40° (orange) towards a `target-hue` of 340° (magenta) will correctly move it *backward* to 20° (red).
* Defining the Range (--min-hue, --max-hue): Due to the circular nature of hue, you can define a range in two ways:
  **Standard Range:** To select a standard range, set --min-hue to a smaller value than --max-hue.
  Example: --min-hue 80 --max-hue 280 selects the range of greens, cyans, and blues, leaving violets, magentas, reds, oranges, and yellows untouched.
  **Wrap-Around Range:** To select the range that crosses over the 0/360° mark (red), set --min-hue to a larger value than --max-hue.
  Example: --min-hue 280 --max-hue 80 selects violets, magentas, reds, oranges, and yellows, leaving greens, cyans, and blues untouched.
* https://www.selecolor.com/en/hsv-color-picker/ is one of many good sites for selecting HSV values

**Elevation**
*   The `--elev_adjust` argument acts as a simple multiplier on all elevation values in the file. A value of `1.1` 
would scale all elevations up by 10%.

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

**Technical Details:**
This command does not call any GDAL commands. It is a pure Python script that reads the input text file, adjusts color and elevation values, 
and writes a new color definition file.

---

### `publish`

Publishes a file to a remote server (`scp`) or a local directory (`cp`). It can optionally stamp the file with the current git commit hash 
and update a server marker file.

**Usage:** `gdal-helper publish <source_file> <directory> [options...]`

**Example:**
```sh
gdal-helper publish final_map.tif /var/www/maps --host user@server --stamp-version
```

**Arguments:**

| Argument          | Type  | Description                                                                        |
|:------------------|:------|:-----------------------------------------------------------------------------------|
| `source_file`     | str   | The local file to publish.                                                         |
| `directory`       | str   | The destination directory.                                                         |
| `--host`          | str   | Optional remote host (e.g., `user@server`). If omitted, a local copy is performed. |
| `--stamp-version` | flag  | If present, embed the git commit hash into the file before publishing.             |
| `--marker-file`   | str   | Optional path to create  a marker file on successful copy.                         |
| `--disable`       | flag  | If present, the publish action is skipped entirely.                                |


**Technical Details:**
*   Executes either a `cp` or `scp` command to transfer the file.
*   If `--stamp-version` is used, it first calls the `add_version` logic.

---

### `add_version`

Creates a permanent, traceable link between a binary GeoTIFF and the exact version of the source code that produced it by embedding the 
current git commit hash into the file's metadata.

**Usage:** `gdal-helper add_version <target_file>`

**Arguments:**

| Argument      | Type  | Description                              |
|:--------------|:------|:-----------------------------------------|
| `target_file` | str   | The raster file to stamp with a version. |

**Technical Details:**
*   Uses the `git` command-line tool to get the current commit hash.
*   Executes `gdal_edit.py` with the `-mo` flag to write a metadata item `VERSION=<hash>`.

---

### `get_version`

Reads the embedded `VERSION` hash from a GeoTIFF's metadata, allowing you to trace a binary artifact back to the exact code and configuration used to create it.

**Usage:** `gdal-helper get_version <target_file>`

**Arguments:**

| Argument      | Type   | Description                 |
|:--------------|:-------|:----------------------------|
| `target_file` | str    | The raster file to inspect. |

**Technical Details:**
*   Executes `gdalinfo` and parses its output to find the value of the `VERSION` metadata tag.

---


## Installation

This tool requires Python 3.9+ and a working GDAL installation on your system `PATH`.

Install using pip from the project root:
```sh
pip install .
# or for development:
pip install -e .
```
This will create the `gdal-helper` command in your environment.