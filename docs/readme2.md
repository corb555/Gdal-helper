# GeoRasterUtil

> ------------------------------
> **Note:**  
GDALHelper has been renamed and moved to GeoRasterUtil.    
This repository is no longer maintained.    
> ------------------------------

**GeoRasterUtil** provides a command-line toolkit for preparing and publishing geospatial raster data.
GDAL and related geospatial tools provide enormously powerful building blocks, but production raster workflows often require more than invoking a single command. They depend on exact grid alignment, carefully chosen options, intermediate processing steps, validation, and consistent handling of outputs. Those details are easy to get wrong and often lead to repetitive scripts that are difficult to maintain.

GeoRasterUtil turns those recurring patterns into focused, reusable commands. It reduces the complexity and failure points of common raster workflows while also providing higher-level operations that are not conveniently available as a single standard GDAL command.

The commands are designed for shell scripts and automated pipelines: inputs are validated, failures are explicit, and each operation has a predictable interface. 

Most commands build on mature, high-performance geospatial and scientific libraries such as GDAL, Rasterio, NumPy, and SciPy, using them for raster I/O, reprojection, numerical processing, and image operations rather than reimplementing those capabilities. GeoRasterUtil adds orchestration, validation, consistent interfaces, and higher-level processing logic around those proven components.

Capabilities include exact raster alignment, categorical reclassification and overrides, ordered raster/vector overlays, terrain blending, mask feathering, broad-scale hillshading, output validation, provenance tracking, and packaging for web delivery.

GeoRasterUtil also favors using an existing raster as the spatial template for downstream operations. A canonical raster can define the CRS, extent, resolution, dimensions, transform, and pixel alignment once, allowing later commands to inherit that grid directly instead of requiring the same spatial parameters to be repeated. This makes multi-step pipelines simpler, more consistent, and less prone to alignment errors.

## Audience

It is intended for GIS analysts, cartographers, and developers who work with GDAL-, Rasterio-, and other open geospatial workflows and want common raster operations packaged as reliable, reusable commands instead of fragile one-off scripts or command chains.


## Features

### 1. Raster Preparation and Composition

* **`align_raster`**: Reprojects and resamples a source raster so its CRS, bounds, dimensions, resolution, and pixel grid
  exactly match a reference raster. Use it when rasters from different sources must be safely combined or compared
  pixel-for-pixel.

* **`create_subset`**: Extracts a repeatable fixed-size area from a larger raster using a relative anchor point. Use it to
  create compact test datasets, previews, or focused working areas without manually calculating pixel windows.

* **`reclassify`**: Reduces a large categorical raster to the smaller set of classes needed by a project. Use it when
  source datasets such as land cover, vegetation, geology, or fuels contain hundreds or thousands of categories but the
  downstream workflow only needs a few meaningful classes.

* **`overlay_layers`**: Applies one or more ordered raster or vector overlays to a base raster. The base defines the output
  grid, later overlays take precedence, and selected category values can be applied selectively. Use it for persistent
  manual corrections, categorical overrides, or priority-based merging of local data over broader source datasets.

### 2. Cartographic Effects and Blending

* **`apply_vignette`**: Creates an irregular alpha fade around a raster's edges so its rectangular footprint disappears
  when placed over another layer. Use it for localized raster overlays that should blend naturally into a larger map.

* **`haze`**: Applies a broad Gaussian blur to soften sharp lines, small-scale detail, and abrupt transitions. Use it for
  atmospheric effects or for smoothing environmental and geological rasters that otherwise look unnaturally crisp.

* **`feather`**: Creates a gradual transparency falloff around the boundary of a mask while preserving a fully opaque
  interior. Use it when a categorical region should retain a solid core but blend smoothly into surrounding layers.

* **`hillshade_blend`**: Blends detailed hillshade into a color raster while protecting extreme shadows and highlights.
  Use it when standard multiply blending makes terrain too dark, washes out highlights, or reduces color saturation.

* **`broad_hillshade`**: Generates medium- and broad-scale terrain shading as a companion to detailed
  `gdaldem hillshade -igor` output. Use it to emphasize large geographic form and terrain massing without adding more
  fine-scale texture.

* **`masked_blend`**: Combines two rasters using a third single-band raster as a spatial blend mask. Use it when different
  source layers should contribute by location or when a gradual transition is needed between two rendered surfaces.

* **`adjust_color_file`**: Creates coordinated variations of a `gdaldem color-relief` definition by adjusting hue,
  saturation, brightness, or elevation values. Use it when several related color ramps should be derived from one
  maintainable master rather than edited independently.

### 3. Validation, Provenance, and Delivery

* **`run`**: Executes a native GDAL command while validating creation options against the selected output driver. Use it
  when you want normal GDAL behavior but do not want misspelled or unsupported creation options to be silently ignored.

* **`validate_raster`**: Verifies that a raster exists and meets minimum size and image-dimension requirements. Use it as
  a pipeline guard so empty, truncated, or obviously invalid outputs do not propagate into later build steps.

* **`add_version` / `get_version`**: Stores and retrieves the current Git commit identifier in GeoTIFF metadata. Use it
  when generated rasters need to be traceable back to the code or configuration revision that produced them.

* **`create_mbtiles`**: Converts a GeoTIFF into MBTiles and builds overview levels for efficient access at multiple zoom
  scales. Use it when a raster needs to be packaged for map applications or offline distribution.

* **`create_pmtiles`**: Converts MBTiles into a single PMTiles archive. Use it when raster tiles should be distributed from
  ordinary static or object storage without running a dedicated tile server.

* **`publish`**: Copies a completed raster artifact to a local or remote destination and can optionally attach version
  metadata first. Use it as a consistent final delivery step in automated raster build pipelines.

## Usage
See [USAGE.md](USAGE.md) for command syntax, parameters, environment variables, and detailed examples.

## Examples

A few representative examples:

> Note: geo-raster is the CLI command name for GeoRasterUtil

```bash
# Make a  layer exactly match the target grid
geo-raster align_raster vegetation.tif terrain_dem.tif vegetation_aligned.tif \
  --resampling-method nearest

# Apply  categorical corrections to a layer
geo-raster overlay-layers base.tif corrections.gpkg \
  -o corrected.tif --value 1 --value 3

# Run a GDAL command but fail immediately if a creation option is invalid (GDAL will silently ignore them)
geo-raster run -- gdal_translate \
  -co COMPRESS=DEFLATE \
  -co TILED=YES \
  source.tif output.tif

# Hide the rectangular footprint of a localized raster overlay
geo-raster apply_vignette overlay.tif overlay_vignette.tif \
  --border 10 --warp 60 --noise 20
```

For command syntax, parameters, environment variables, configuration formats, and detailed execution examples, see
[USAGE.md](USAGE.md).

## License

This project is licensed under the **MIT License**—see the `LICENSE` file for details.
