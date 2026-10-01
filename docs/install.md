# GDALHelper Usage Guide

This document provides a comprehensive operational guide for the `gdal-helper` CLI, detailing input schemas, options,
and real-world cartographic execution recipes.

## Global Command Syntax

```bash
gdal-helper <command> [inputs] [options]
```

To see global assistance or get help for an individual command:

```bash
gdal-helper --help
gdal-helper hillshade_blend --help
```

---

## Detailed Command Reference

### `align_raster`

Surgically alters a source raster to conform exactly to the geometry, coordinate system, and resolution of a template
raster. This is a critical prerequisite for passing layers to the `LandWeaver` rendering pipeline.

* **Arguments:** `<input_path> <template_path> <output_path>`
* **Options:**
    * `--resoring-method`: Choose from `nearest`, `bilinear`, `cubic`, `lanczos` (Default: `bilinear`).
* **Example:**
  ```bash
  gdal-helper align_raster landfire_raw.tif srtm_dem.tif landfire_normalized.tif --resoring-method nearest
  ```

### `hillshade_blend`

Blends an analytical hillshade with an elevation color relief image. It prevents overexposure by dynamically preserving
luminance details across high-gradient mountain topography.

* **Arguments:** `<hillshade_path> <color_relief_path> <output_path>`
* **Options:**
    * `--blend-mode`: System compositing method (`multiply`, `soft_light`, `overlay`). Default: `multiply`.
    * `--opacity`: Opacity of the hillshade overlay layer from `0.0` to `1.0`. Default: `1.0`.
* **Example:**
  ```bash
  gdal-helper hillshade_blend dual_gain_shade.tif terrain_colors.tif final_relief.tif --blend-mode multiply --opacity 0.85
  ```

### `reclassify`

Compresses and remaps high-range or sparse categorical raster values into an efficient, contiguous 8-bit classification
layout. Essential for generating lightweight masks from datasets like USGS LANDFIRE.

* **Arguments:** `<input_path> <mapping_file> <output_path>`
* **Example:**
  ```bash
  gdal-helper reclassify raw_vegetation.tif landfire_lookup.txt classified_mask.tif
  ```
  *The `mapping_file` is a space-separated text file matching raw codes to output integers (e.g., `3012 1`, `3015 2`).*

### `apply_vignette`

Applies an organic, noise-modulated edge-fading operation directly into the alpha channel of a raster, allowing
cartographers to design soft, blended map margins.

* **Arguments:** `<input_path> <output_path>`
* **Options:**
    * `--radius`: Inward distance in pixels from the raster perimeter where the fade initiates.
    * `--noise-frequency`: Controls the ruggedness of the edge boundary blending.
* **Example:**
  ```bash
  gdal-helper apply_vignette map_background.tif map_vignetted.tif --radius 150 --noise-frequency 0.05
  ```

### `run` (With Creation Option Validation)

Executes a raw GDAL subcommand shell process, but actively intercepts your configuration payload to validate creation
options (`-co`) against the specific target file driver specs.

* **Arguments:** `-- <gdal_command_string>`
* **Example:**
  ```bash
  gdal-helper run -- gdalwarp -co COMPRESS=DEFLATE -co TILED=YES source.tif output.tif
  ```
  *If you pass an unsupported flag—such as `-co COMPRESS=INVALID_METHOD`—`gdal-helper` will instantly halt execution
  with an explicit error trace rather than letting GDAL silently drop the flag.*

---

## Real-World Cartographic Workflow Recipe

The following execution script demonstrates a complete end-to-end pipeline: preparing a raw USGS LANDFIRE categorical
layer, aligning it to a regional Digital Elevation Model, generating an analytical hillshade, and generating an
optimized, web-ready cloud tile package.

```bash
#!/bin/bash
set -e

echo "Step 1: Reclassifying raw LANDFIRE vegetation codes to an 8-bit mask..."
gdal-helper reclassify landfire_raw.tif values_map.txt landfire_8bit.tif

echo "Step 2: Aligning the 8-bit mask to match the precise grid of the master DEM..."
gdal-helper align_raster landfire_8bit.tif colorado_dem.tif landfire_aligned.tif --resoring-method nearest

echo "Step 3: Executing a verified compression run using the safe GDAL wrapper..."
gdal-helper run -- gdal_translate -of GTiff -co COMPRESS=WEBP -co WEBP_LEVEL=85 landfire_aligned.tif landfire_final.tif

echo "Step 4: Compiling the final spatial imagery into an MBTiles pyramid..."
gdal-helper create_mbtiles landfire_final.tif regional_layer.mbtiles

echo "Step 5: Converting the MBTiles archive to a cloud-optimized PMTiles container..."
gdal-helper create_pmtiles regional_layer.mbtiles regional_layer.pmtiles

echo "Pipeline complete. Artifact 'regional_layer.pmtiles' is ready for static web deployment."
```
