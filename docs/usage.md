# GDALHelper Usage Guide

`gdal-helper` provides raster-processing, cartographic blending, output, validation, and publishing commands built
around GDAL.

## Command Syntax

```bash
gdal-helper [-v|--verbose] <command> [inputs] [options]
```

Use `-v` or `--verbose` before the command name to enable verbose output.

For general or command-specific help:

```bash
gdal-helper --help
gdal-helper hillshade_blend --help
```

---

## 1. Raster Manipulation

### `align_raster`

Resamples a source raster to match the grid, extent, projection, and resolution of a template raster. Use it before
blending or combining layers that must be pixel-aligned.

```bash
gdal-helper align_raster <source> <template> <output> \
  --resampling-method bilinear \
  --co COMPRESS=DEFLATE
```

Use `near` for categorical rasters and `bilinear`, `cubic`, or `lanczos` for continuous data.

### `create_subset`

Extracts a pixel-based subset from a raster for previews, testing, or working with a smaller sample of a large dataset.

By default, the command creates a `4000 × 4000` pixel crop centered on the source raster. Use the horizontal and
vertical anchors to position the crop elsewhere.

Anchor values are relative to the source image:

* `0.0, 0.0`: top-left
* `0.5, 0.5`: center; the default
* `1.0, 1.0`: bottom-right

If `--size` is larger than either source dimension, the entire source raster is written instead.

Options:

* `--size` (default: `4000`): width and height of the crop in pixels
* `--x-anchor` (default: `0.5`): horizontal crop position from `0.0` for left to `1.0` for right
* `--y-anchor` (default: `0.5`): vertical crop position from `0.0` for top to `1.0` for bottom

```bash
gdal-helper create_subset input.tif output.tif \
  --size 4000 \
  --x-anchor 0.5 \
  --y-anchor 0.5
```

The command preserves the source raster’s pixel values, CRS, and resolution. The output extent changes to match
the selected crop.

### `vignette`

Adds a noise-modulated alpha fade around the perimeter of a raster so it blends smoothly into the layer beneath it.

Options:

- `--border` (default: `5.0`): fade width as a percentage of the smaller image dimension
- `--noise` (default: `20.0`): high-frequency noise as a percentage of the border width
- `--warp` (default: `60.0`): low-frequency edge distortion as a percentage of the border width
- `--replace-alpha`: replace an existing alpha band instead of multiplying it by the vignette
- `--seed`: optional random seed for reproducible edge shapes
- `--fade-data`: fade the data bands as well as the alpha channel
- `--co NAME=VALUE`: output creation option; may be repeated
- `--overwrite`: allow an existing output to be replaced

```bash
gdal-helper vignette input.tif output.tif \
  --border 10 \
  --warp 60 \
  --noise 20
```

### `haze`

Applies a Gaussian low-pass filter across all raster bands. It is useful for atmospheric effects, geological
transitions, and softened biome boundaries.

Options:

- `--sigma` (default: `2.0`): Gaussian standard deviation in pixels
- `--normalize`: stretches each output band back to the source data range
- `--co NAME=VALUE`: output creation option; may be repeated
- `--overwrite`: allow an existing output to be replaced

For values above `30.0`, the command uses a pyramidal downsample-blur-upsample strategy to improve performance on large
rasters.

```bash
gdal-helper haze input.tif output.tif \
  --sigma 60.0 \
  --normalize \
  --co COMPRESS=DEFLATE
```

### `feather`

Softens the perimeter of a single-band mask or alpha raster using a Euclidean Distance Transform. The Gaussian falloff
affects feature boundaries while preserving full intensity in the interior.

Options:

- `--sigma` (default: `1.0`): width and softness of the edge falloff
- `--truncate` (default: `2.5`): controls the processing halo around each tile
- `--tile-size` (default: `256`): output tile width and height in pixels
- `--band` (default: `1`): source band to use as the mask
- `--co NAME=VALUE`: output creation option; may be repeated
- `--overwrite`: allow an existing output to be replaced

```bash
gdal-helper feather input_mask.tif output.tif \
  --sigma 5.0 \
  --co COMPRESS=DEFLATE
```

### `reclassify`

Reduces a large categorical raster to only the classes needed for a map or analysis. It groups selected source category IDs into compact 8-bit class values, assigns valid but unmatched source pixels a configured default value, preserves source nodata as a separate output nodata value, and can generate a matching alpha mask.

This is useful for datasets such as LANDFIRE EVT or FBFM when the source contains many categories but the final map needs only a small set, such as glacier, tree-canopy, or volcanic terrain. Valid source categories that are not explicitly selected can be collapsed to a general fallback category such as `land`, while true source nodata remains distinguishable. The resulting raster is smaller, simpler, and faster to render, style, blend, and publish.

The command processes band 1 of an integer categorical raster and writes a single-band 8-bit GeoTIFF. It preserves the source CRS, extent, resolution, dimensions, and pixel grid; it does not reproject, resample, or align the data.

An optional `source_check` section can check that the input appears to use the expected category system before any output is written. This is useful when a YAML configuration is intended for a specific dataset family, such as LANDFIRE EVT or World Ecological Land Units.

#### Inputs

* **Source raster:** An integer categorical raster. Only band 1 is processed.
* **Configuration:** A YAML file supplied with `--config`.

Source nodata is determined from both `options.input_nodata` and the source raster's own nodata metadata. These values are combined before reclassification. 

#### Outputs

* **Class raster:** A single-band Byte GeoTIFF containing the configured output class values, the configured default value for valid but unmatched source categories, and a separate nodata value for source nodata.
* **Optional alpha raster:** A single-band Byte GeoTIFF in which valid source pixels use `alpha.on`, normally `255`, and source nodata pixels use `alpha.off`, normally `0`.

Each configured source category ID may appear in only one class. Multiple source IDs may be grouped into the same output class.

The output distinguishes three cases:

```text
configured source category  -> configured class value
valid but unmatched source  -> options.default_value
source nodata                -> options.nodata_value
```

`options.nodata_value` is also stored as the GeoTIFF output nodata metadata value. `options.default_value` is 
therefore a valid output category, not nodata.

If `nodata_value` is omitted, it defaults to `default_value`.

Output class values must be unique integers from `0` through `255` and may not equal either `options.default_value` or `options.nodata_value`. The configured default and nodata values therefore reserve output values that cannot also be used by an explicit class.

#### Source category validation

`source_check` is optional. When present, `reclassify` scans band 1 before creating the output and verifies 
that the source contains a small fingerprint of expected category IDs.

```yaml
source_check:
  category: LANDFIRE_EVT
  required_ids: [7735]
  expected_any_ids: [7734, 9016, 9033, 9153, 9160]
  minimum_expected_any: 1
```

`category` is a human-readable name used in error messages. It does not inspect or require source metadata.

Every ID listed in `required_ids` must occur at least once in the source raster.

`expected_any_ids` provides an additional category fingerprint. At least `minimum_expected_any` distinct IDs from that list must occur at least once.

For example, the configuration above requires category `7735` and at least one of `7734`, `9016`, `9033`, `9153`, or `9160`.

A validation failure stops processing before the output raster is created. For example:

```text
Category validation error. Category 7735: Matches:0 Threshold:1.
Verify this uses 'LANDFIRE_EVT' categories.
```

The checks are intended to help catch an incorrect source raster or a raster using a different category 
system. They are not intended to prove the exact dataset version.

#### Example configuration

```yaml
config_type: "Reclassify"

source_check:
  category: LANDFIRE_EVT
  required_ids: [7735]
  expected_any_ids: [7734, 9016, 9033, 9153, 9160]
  minimum_expected_any: 1

classes:
  - name: volcanic
    ids: [9033, 9153, 9160]
    value: 1

  - name: rock
    ids: [7734, 9016, 7733]
    value: 2

  - name: playa
    ids: [9008, 9004, 9151]
    value: 3

options:
  # Valid source categories not matched above become general land.
  default_value: 4

  #  source nodata remains nodata in the output.
  nodata_value: 0

  compress: deflate
  input_nodata: [-9999, 32767]

  report_unmapped:
    enabled: false
    max_ids: 50

  alpha:
    enabled: false
    on: 255
    off: 0
    sparse_ok: true
```

```bash
gdal-helper reclassify \
  --config reclassify.yml \
  LF2024_EVT.tif \
  EVT_subset.tif
```

In this example:

```text
volcanic IDs -> 1
rock IDs     -> 2
playa IDs    -> 3
other valid EVT categories -> 4
source nodata -> 0
```

That allows a downstream overlay operation, for example, to distinguish ordinary land from areas where the source has no coverage.

#### Alpha behavior

The optional alpha raster represents **valid source coverage**, not merely explicit class matches.

A valid source category that does not match any configured class still receives `options.default_value`, so its alpha pixel is `alpha.on`. Only source nodata receives `alpha.off`.

This is different from the older behavior, where unmatched categories and nodata were both treated as alpha-off. The current mapper explicitly preserves source nodata separately from valid unmatched pixels. 

#### Restrictions and behavior

* Only band 1 is processed.
* The source band must use an integer data type.
* The output is always a single-band 8-bit GeoTIFF.
* Output class values must be unique.
* Output class values may not equal `options.default_value`.
* Output class values may not equal `options.nodata_value`.
* A source category ID may not appear in more than one class.
* Configured source category IDs may not also be configured as input nodata.
* Source category IDs not listed in a configured class are assigned `options.default_value`.
* Source nodata pixels are assigned `options.nodata_value`.
* The output GeoTIFF nodata metadata is set to `options.nodata_value`.
* If `options.nodata_value` is omitted, it defaults to `options.default_value` for backward compatibility.
* Source raster nodata metadata is automatically combined with values listed in `options.input_nodata`.
* Unused source IDs outside the internal lookup range are treated as unmatched rather than causing the command to fail.
* `report_unmapped`, when enabled, reports unmatched source category IDs but excludes source nodata values.
* `source_check` is optional.
* Every `source_check.required_ids` value must occur at least once.
* At least `source_check.minimum_expected_any` distinct values from `expected_any_ids` must occur.
* IDs may not be duplicated within a source-check list or appear in both `required_ids` and `expected_any_ids`.
* Source category validation is performed before output creation.
* The source CRS, extent, resolution, dimensions, and pixel grid are preserved.
* No color palette is generated.
* No reprojection, resampling, or alignment is performed.
* The optional alpha raster distinguishes valid source coverage from source nodata; valid unmatched/default pixels remain alpha-on.


### `lookup_raster`

Creates a new raster by replacing each integer source pixel value with a numeric attribute read from an external
lookup table. The source raster value acts as a key into the table, and the selected table field becomes the output
pixel value.

This is useful for categorical raster products whose pixel values are identifiers rather than the final data of
interest. A common example is an ArcGIS Raster Attribute Table (`.vat.dbf`) in which the raster stores a combined
`Value` and the table contains component attributes such as lithology, landform, soil class, or ecological category.

For example, the USGS World Ecological Land Units raster stores a combined ecological-facet value. Its accompanying
VAT contains fields such as:

```text
Value
Bio_Val
LF_Val
Lit_Val
GLC_Val
```

Using `Value` as the key field and `Lit_Val` as the value field creates a lithology raster from the combined source.

The command processes band 1 of an integer raster and writes a single-band GeoTIFF. It preserves the source CRS,
extent, resolution, dimensions, and pixel grid; it does not reproject, resample, or align the data.

#### Inputs

* **Source raster:** An integer raster whose band 1 pixel values are lookup keys.
* **Lookup table:** A `.dbf` or `.csv` table supplied with `--table`.
* **Key field:** The table column corresponding to source raster values, supplied with `--key-field`.
* **Value field:** The numeric table column to write to the output raster, supplied with `--value-field`.

Each lookup-table key must be a unique, non-negative integer.

Rows whose selected value field is null are treated as unmapped.

#### Output

* **Lookup raster:** A single-band GeoTIFF containing the selected table attribute.

The output data type is inferred from the selected value field unless `--dtype` is specified. Integer and
floating-point output fields are supported.

Source values that do not have a usable table entry receive the output default/nodata value. When
`--default-value` is omitted, the command selects a non-conflicting value automatically.

The default/nodata value may not also occur as a legitimate mapped value because GIS software would otherwise be
unable to distinguish valid data from nodata.

#### Example

Extract the lithology component from the USGS World Ecological Land Units Raster Attribute Table:

```bash
gdal-helper lookup_raster \
  --table "World_Ecological_2015.tif.vat.dbf" \
  --key-field Value \
  --value-field Lit_Val \
  World_Ecological_2015.tif \
  world_lithology.tif
```

Conceptually:

```text
source raster Value
        ↓
lookup matching DBF row
        ↓
Lit_Val
        ↓
output raster
```

The resulting raster can then be passed to `reclassify` when only selected classes are needed. For example:

```text
World Ecological raster
→ lookup_raster: Value → Lit_Val
→ 16-class lithology raster
→ reclassify: Lit_Val 2
→ siliciclastic mask
```

#### Optional arguments

`--default-value` specifies the value used for source IDs that are absent from the lookup table or whose selected
table attribute is null. If omitted, a suitable non-conflicting nodata value is selected automatically.

`--dtype` explicitly sets the output data type, for example:

```text
uint8
uint16
int32
float32
```

If omitted, the command selects an appropriate type from the lookup values.

`--input-nodata` specifies additional source category IDs that should be treated as nodata. It may be repeated or
contain comma-separated values.

`--report-unmapped` reports a bounded sample of source raster values that could not be resolved through the lookup
table.

`--max-unmapped-ids` controls the maximum number of unmapped IDs reported.

`--max-lookup-key` limits the largest permitted table key. `lookup_raster` uses a dense in-memory lookup array for
fast block processing, so this limit prevents an unexpectedly large or malformed key from allocating excessive
memory.

#### Restrictions and behavior

* Only band 1 is processed.
* The source band must use an integer data type.
* Lookup keys must be non-negative integers.
* Lookup-table keys must be unique.
* The lookup value field must contain numeric values.
* Null lookup values are treated as unmapped.
* `.dbf` and `.csv` lookup tables are supported.
* Source raster values absent from the lookup table receive the default/nodata value.
* Source metadata nodata and values supplied with `--input-nodata` are treated as nodata.
* The output data type is inferred unless explicitly specified with `--dtype`.
* The default/nodata value may not conflict with a valid mapped value.
* The source CRS, extent, resolution, dimensions, and pixel grid are preserved.
* No color palette is generated.
* No reprojection, resampling, or alignment is performed.
* Processing is performed by raster blocks rather than loading the full raster into memory.
* The lookup table is converted to an in-memory lookup array before raster processing begins.

`lookup_raster` does not align or resample its input. Run `align_raster` or another appropriate preprocessing step
first when downstream layers must share the same dimensions, projection, extent, and pixel grid.

For categorical source rasters, any preprocessing reprojection or resampling should normally use nearest-neighbor
resampling so lookup IDs are not altered.

### `smooth_categories`

```bash
gdal-helper smooth_categories input.tif output.tif \
  --config classes.yml \
  --claim-threshold 0.2 \
  --median-threshold 1.5
```

Options:

- `--config`: required YAML configuration
- `--claim-threshold` (default: `0.2`)
- `--median-threshold` (default: `1.5`)
- `--overwrite`

### `proximity`

```bash
gdal-helper proximity input.tif output.tif \
  --targets 0,1,2,3,5,6 \
  --maxdist 500
```

Options:

- `--targets` (default: `0,1,2,3,5,6`)
- `--maxdist`
- `--overwrite`

---

## 2. Blending and Visualization

### `hillshade_blend`

Blends a grayscale hillshade with a color-relief raster using texture-shading logic.

A luminosity mask softens extreme shadows and highlights while retaining mid-tone terrain detail. This helps preserve
color saturation in dark areas and avoids washed-out highlights.

Options:

- `--protect-shadows` (default: `0.2`)
- `--protect-highlights` (default: `0.10`)
- `--shadow-range START END` (default: `0 60`)
- `--highlight-range START END` (default: `220 255`)
- `--hill-floor` (default: `0.0`)
- `--hill-gamma` (default: `1.0`)
- `--hill-ceil` (default: `1.0`)
- `--shade-strength` (default: `0.8`)
- `--co NAME=VALUE`: output creation option; may be repeated

```bash
gdal-helper hillshade_blend hillshade.tif color_relief.tif output.tif \
  --co COMPRESS=JPEG \
  --co JPEG_QUALITY=85
```

### `masked_blend`

Composites two RGB or RGBA layers using a single-band grayscale mask.

Mask values determine the blend:

- `255`: 100% Layer A
- `128`: approximately 50% Layer A and 50% Layer B
- `0`: 100% Layer B

All three inputs must have identical dimensions and projections. Use `align_raster` first when necessary.

```bash
gdal-helper masked_blend layer_a.tif layer_b.tif mask.tif output.tif
```

### `adjust_color_file`

Creates a modified GDAL color-relief definition file by adjusting hue, saturation, brightness, or elevation values. It
modifies the definition file, not a raster.

```bash
gdal-helper adjust_color_file input_colors.txt output_colors.txt \
  --saturation 1.2 \
  --mid-adjust 0.1
```

See [Adjusting Color-Relief Files](#adjusting-color-relief-files) for the complete option reference.

---

## 3. Output Formats

### `create_mbtiles`

Converts a raster to MBTiles and builds internal overview levels with `gdaladdo`.

Options:

- `--co NAME=VALUE`: GDAL creation option; may be repeated
- `--mo NAME=VALUE`: GDAL metadata option; may be repeated
- `--minzoom`: minimum zoom level
- `--maxzoom`: maximum zoom level
- `-r`, `--resampling` (default: `CUBIC`): overview resampling algorithm
- `--overwrite`: allow an existing output to be replaced

```bash
gdal-helper create_mbtiles input.tif output.mbtiles \
  --co TILE_FORMAT=JPEG \
  --co QUALITY=80 \
  --minzoom 6 \
  --maxzoom 12
```

### `create_pmtiles`

Converts an MBTiles archive to PMTiles. The `pmtiles` executable must be available in the system `PATH`.

Options:

- `--overwrite`: allow an existing output to be replaced

```bash
gdal-helper create_pmtiles input.mbtiles output.pmtiles
```

---

## 4. Validation and Publishing

### `manifest`

```bash
gdal-helper manifest \
  --dir ./inputs \
  --output ./inputs/manifest.json \
  --sources ./inputs/sources.json
```

Options:

- `--dir`: required directory to scan
- `--output`: required JSON output path
- `--sources`: optional JSON provenance mapping

### `run`

Executes a standard GDAL command while validating each creation option (`-co`) against the selected output driver's
capabilities.

GDAL utilities may silently ignore invalid or unsupported creation options. This wrapper stops immediately and reports
the invalid option instead.

```bash
gdal-helper run \
  gdal_translate \
  -of GTiff \
  -co COMPRESS=DEFLATE \
  -co TILED=YES \
  input.tif \
  output.tif
```

For example, `-co COMPRESS=FAKE` fails before the GDAL operation begins.

### `validate_raster`

Verifies that a raster exists and meets minimum file-size and total-pixel-count requirements. It can stop a build
pipeline when an upstream operation produces an empty or invalid raster.

Options:

- `--min-bytes` (default: `1000`): minimum file size in bytes
- `--min-pixels` (default: `1000`): minimum total pixel count (`width × height`)

```bash
gdal-helper validate_raster input.tif \
  --min-bytes 5000 \
  --min-pixels 500
```

### `add_version` and `get_version`

`add_version` writes the current Git commit hash into a GeoTIFF's metadata. `get_version` reads it back.

If the repository contains uncommitted changes, `add_version` appends `-dirty` to the hash.

Requirements and limitations:

- Git must be installed.
- The command must run within a Git repository.
- Version metadata is supported only for TIFF files, not JPEG, PNG, MBTiles, or PMTiles.

```bash
gdal-helper add_version input.tif
gdal-helper get_version input.tif
```

### `publish`

Completes a build pipeline by copying or transferring the finished
artifact, and updating a completion marker. Publishing can be disabled without breaking downstream dependency tracking.
This can also embed the current Git commit identifier—the version of the project source used to build the
file—into TIFF metadata.

The command can optionally:

- embed the current Git commit hash in the source file before publishing;
- rename the file at the destination;
- permit replacement of an existing local destination file; and
- create a marker file after the command completes successfully.

#### Syntax

```bash
gdal-helper publish <source_file> <directory> [options]
````

#### Arguments

* `source_file`: Local file to copy or transfer.
* `directory`: Destination directory. For remote publishing, this is the directory on the remote host.

#### Options

* `--host HOST`: Publish with `scp` to `HOST`. When omitted, the file is copied locally.
* `--rename NAME`: Use a different filename at the destination. The source filename is used by default.
* `--overwrite`: Allow an existing local destination file to be replaced.
* `--stamp-version`: Embeds the current Git commit identifier—the version of the project source used to build the
  file—into TIFF metadata. This option is intended for
  version-controlled projects. Git must be installed, and the command must run inside a Git repository.
* `--marker-file PATH`: Creates or updates a completion marker after successful processing. The marker is also updated
  when --disable is used,
  allowing dependency-based pipelines to continue when publishing is intentionally disabled.
* `--disable`: Skips the copy or transfer but still performs any requested version stamping and updates the marker file.

#### Local publishing

The destination directory must already exist. The command fails if the destination file exists unless
`--overwrite` is supplied.

```bash
gdal-helper publish \
  build/map.tif \
  /var/www/maps/ \
  --stamp-version \
  --overwrite
```

To publish under a different filename:

```bash
gdal-helper publish \
  build/map.tif \
  /var/www/maps/ \
  --rename current-map.tif \
  --overwrite
```

#### Remote publishing

Provide `--host` to transfer the file using `scp`:

```bash
gdal-helper publish \
  build/map.tif \
  /var/www/maps/ \
  --host maps.example.com \
  --rename current-map.tif
```

The remote target is constructed as:

```text
HOST:DIRECTORY/FILENAME
```

Remote destination existence and overwrite behavior are handled by `scp`; `--overwrite` is not checked for
remote publishing.

#### Version stamping

When `--stamp-version` is supplied, the source file is modified before it is copied or transferred. Version
stamping requires Git and a supported TIFF source file. Publishing stops if the Git revision cannot be obtained
or the metadata cannot be written.

#### Marker files

When `--marker-file` is supplied, the command creates the marker file and any missing parent directories after
successful processing.

When `--disable` is also supplied, the publish operation is skipped but the marker file is still created.


---

## Example Cartographic Workflow

The following pipeline reclassifies a LANDFIRE layer, aligns it to a DEM, validates output options, and produces a
PMTiles archive.

```bash
#!/bin/bash
set -e

echo "Step 1: Reclassifying LANDFIRE vegetation..."
gdal-helper reclassify \
  --config reclassify.yml \
  landfire_raw.tif \
  landfire_8bit.tif

echo "Step 2: Aligning the class raster to the DEM..."
gdal-helper align_raster \
  landfire_8bit.tif \
  colorado_dem.tif \
  landfire_aligned.tif \
  --resampling-method near

echo "Step 3: Creating a validated, compressed GeoTIFF..."
gdal-helper run \
  gdal_translate \
  -of GTiff \
  -co COMPRESS=DEFLATE \
  -co TILED=YES \
  landfire_aligned.tif \
  landfire_final.tif

echo "Step 4: Creating MBTiles..."
gdal-helper create_mbtiles landfire_final.tif regional_layer.mbtiles

echo "Step 5: Creating PMTiles..."
gdal-helper create_pmtiles regional_layer.mbtiles regional_layer.pmtiles

echo "Pipeline complete: regional_layer.pmtiles"
```

---

## Extending GDALHelper

GDALHelper uses the Command Pattern. New commands inherit from `Command` or `IOCommand` and are registered with the
decorator in `commands.py`.

### Choosing a Base Class

- **`IOCommand`:** Recommended for commands that accept one input file and produce one output file. It provides standard
  input, output, and validation handling.
- **`Command`:** Use for commands with multiple inputs, no output file, or more complex argument structures.

### Required Methods

A command normally implements:

1. **`add_arguments(parser)`** to define command-line arguments. An `IOCommand` implementation must invoke the parent
   method first to register the standard input and output arguments.
2. **`transform()`** to perform the operation.

Arguments are available through `self.args`. Use `self._run_command()` to execute subprocesses safely and
`self.print_verbose()` for verbose-only output.

```python
@register_command("create_subset")
class CreateSubset(IOCommand):
    """Extract a square subset from a raster."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        """Register command-line arguments.

        Args:
            parser: Parser used to register the command arguments.
        """
        super(CreateSubset, CreateSubset).add_arguments(parser)
        parser.add_argument(
            "--size", type=int, default=4000, help="Width and height of the output subset.", )
        parser.add_argument(
            "--x-anchor", type=float, default=0.5, help="Horizontal anchor: 0.0=left, 0.5=center, 1.0=right.", )

    def transform(self) -> None:
        """Extract the configured subset."""
        width, height = _get_image_dimensions(self.args.input)
        x_offset = int((width - self.args.size) * self.args.x_anchor)
        y_offset = int((height - self.args.size) * self.args.y_anchor)

        command = ["gdal_translate", "-srcwin", str(x_offset), str(y_offset), str(self.args.size), str(self.args.size),
            self.args.input, self.args.output, ]

        self._run_command(command)
        self.print_verbose(f"Subset created: {self.args.output}")
```

---

## Adjusting Color-Relief Files

`adjust_color_file` reads numerical RGB or RGBA entries from a GDAL color-relief definition file, converts the colors to
HSV, applies the requested adjustments, and writes a new definition file.

This allows multiple coordinated color ramps to be generated from one maintained source file. Neutral colors are
protected from inappropriate hue shifts.

> The command does not modify raster imagery and does not support named color values.

### Saturation

`--saturation` multiplies color intensity:

- `1.5` increases saturation by 50%.
- `0.25` reduces saturation to 25% of its original value.

### Brightness

Brightness adjustments are additive and divided into overlapping tonal regions:

- `--shadow-adjust`: darkest colors
- `--mid-adjust`: mid-tones
- `--highlight-adjust`: brightest colors

The regions blend smoothly to avoid hard tonal boundaries.

### Hue

Use `--min-hue` and `--max-hue` to select a hue range and `--target-hue` to shift matching colors toward a target.

To select a range that crosses the `0°/360°` boundary, set `--min-hue` greater than `--max-hue`. For example,
`--min-hue 280 --max-hue 80` selects violets, reds, oranges, and yellows while leaving greens and blues unchanged.

### Elevation

`--elev-adjust` multiplies every elevation value in the file. For example, `1.1` increases all values by 10%.

### Usage

```bash
gdal-helper adjust_color_file <input> <output> [options]
```

```bash
gdal-helper adjust_color_file base_ramp.txt arid_ramp.txt \
  --target-hue 46 \
  --saturation 0.8
```

| Argument             | Type    | Default | Description                                   |
|:---------------------|:--------|:--------|:----------------------------------------------|
| `input`              | `str`   | —       | Source GDAL color definition file.            |
| `output`             | `str`   | —       | Output color definition file.                 |
| `--saturation`       | `float` | `1.0`   | Multiplies saturation.                        |
| `--shadow-adjust`    | `float` | `0.0`   | Additively adjusts dark colors.               |
| `--mid-adjust`       | `float` | `0.0`   | Additively adjusts mid-tones.                 |
| `--highlight-adjust` | `float` | `0.0`   | Additively adjusts light colors.              |
| `--min-hue`          | `float` | `0.0`   | Lower bound of the hue range, from 0 to 360.  |
| `--max-hue`          | `float` | `0.0`   | Upper bound of the hue range, from 0 to 360.  |
| `--target-hue`       | `float` | `0.0`   | Hue toward which matching colors are shifted. |
| `--elev-adjust`      | `float` | `1.0`   | Multiplies elevation values.                  |
| `--overwrite`        | flag    | off     | Allow an existing output to be replaced.      |
