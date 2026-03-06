````markdown
# USGS LANDFIRE EVT/NVC with Natural Classification Colors

This document describes a practical workflow for turning the **USGS LANDFIRE EVT/NVC categorical GeoTIFF**
into a **geology-friendly natural basemap** with:

- believable “natural” colors (greens ↔ tans) across the US West
- a **precipitation-driven blend** between humid and arid looks
- de-emphasized **developed/urban** areas 
- **roads removed** (inpainted) so they don’t telegraph as linework
- optional **subtle elevation cue** without plastic hypsometric gradients
- “special geology-relevant classes” (scree, lava, playa, scabland, ice) emphasized via overrides

The approach is designed to be **iterated in QGIS first**, then automated in Python once stable.

---

## Goals

### What we want
1. **Natural-looking land surface identity** driven by categorical vegetation/landcover with natural colors
2. **Arid ↔ humid blending** controlled by precipitation (already working in the pipeline).
3. **Urban/ag** rendered unobtrusively (neutral fills), not a competing “map layer.”
4. **Roads disappear** into surrounding landcover, especially after hillshade multiply.
5. Preserve a few key “geology signals”:
   - scree/talus / alpine rock
   - volcanic rock / lava fields
   - scabland
   - playas
   - glaciers/ice

### What we want to avoid
- “plastic” continuous hypsometric ramps that ignore material boundaries
- visible road networks due to coherent thin linework

---

## High-level Strategy

1. Use **LANDFIRE EVT MACROGROUP** as the primary semantic label.
2. Map many Macrogroups to a smaller set of **ColorGroups** (≈ 40–60 groups).
3. Assign each ColorGroup a single RGB color in two palettes:
   - **lush** palette (humid-leaning)
   - **arid** palette (dry-leaning) - this could be automated as desaturated and shifted tan
4. Blend lush↔arid using the existing **precipitation mask**.
5. Treat **roads** as a special case:
   - inpaint road pixels using neighboring classes using a new gdal-helper routine
6. Apply **VALUE overrides** for special geology-relevant codes.

---

## Inputs

- EVT / NVC categorical raster (GeoTIFF)
  - must include (or be joinable to) an attribute table with at least:
    - `VALUE` (integer raster code)
    - `MACROGROUP` (string)
    - other fields are helpful but not required

- Precipitation raster (or precipitation-derived mask)
- Optional drivers already in the pipeline:
  - slope, hillshade,  lithology, etc.

---

## Outputs

- A stable mapping from landcover semantics → colors:
  - `macrogroup_to_colorgroup.csv`
  - `colorgroup_palette.csv`
  - `value_overrides.csv`

- Two QGIS styles (or equivalent):
  - `evtnvc_lush.qml`
  - `evtnvc_arid.qml`

- Optional preprocessed categorical raster:
  - `evt_inpainted_roads.tif` (same class codes, but road pixels replaced)

---

## Data Model

### 1) Macrogroup → ColorGroup

Use a compact “visual material family” key (`ColorGroup`) rather than trying to color every macrogroup uniquely.

Examples:
- Developed → `CG_URBAN`
- Agriculture → `CG_AG_CROPS`, `CG_AG_ORCHARD_VINE`, `CG_AG_PASTURE_HAY`
- Wetlands/Water → `CG_OPEN_WATER`, `CG_SALT_MARSH`, `CG_FRESHWATER_MARSH`, `CG_RIPARIAN_WET_FOREST`
- Vegetation structure → `CG_MONTANE_CONIFER`, `CG_CHAPARRAL`, `CG_SAGEBRUSH`, `CG_DESERT_SCRUB`, etc.
- Bare/rock/ice → `CG_CLIFF_SCREE_ROCK`, `CG_VOLCANIC_ROCK`, `CG_SCABLAND`, `CG_PLAYA`, `CG_GLACIER_ICE`

Create:

**`macrogroup_to_colorgroup.csv`**
```csv
MACROGROUP,ColorGroup
Developed-Low Intensity,CG_URBAN
Developed-Medium Intensity,CG_URBAN
Developed-High Intensity,CG_URBAN
Developed-Open Space,CG_URBAN
Close Grain Crop,CG_AG_CROPS
Corn Crop,CG_AG_CROPS
Fruit Orchard,CG_AG_ORCHARD_VINE
Grape Vineyard,CG_AG_ORCHARD_VINE
Permanent Pasture & Grass Hay Field,CG_AG_PASTURE_HAY
Open Water,CG_OPEN_WATER
Barren,CG_BARREN
Great Plains Cliff Scree & Rock Vegetation,CG_CLIFF_SCREE_ROCK
Intermountain Basins Cliff Scree & Badland Sparse Vegetation,CG_BADLANDS_SPARSE
Great Basin & Intermountain Tall Sagebrush Shrubland & Steppe,CG_SAGEBRUSH
Mojave-Sonoran Semi-Desert Scrub,CG_DESERT_SCRUB
Vancouverian Coastal Rainforest Macrogroup,CG_COASTAL_RAINFOREST
...
````

### 2) ColorGroup → RGB (lush & arid)

Each ColorGroup gets a defined RGB in both “lush” and “arid” palettes.

**`colorgroup_palette.csv`**

```csv
ColorGroup,R_lush,G_lush,B_lush,R_arid,G_arid,B_arid
CG_URBAN,70,95,70,160,140,95
CG_AG_CROPS,95,115,80,175,155,105
CG_OPEN_WATER,80,110,140,80,110,140
CG_CLIFF_SCREE_ROCK,150,140,120,150,140,120
CG_VOLCANIC_ROCK,120,110,105,120,110,105
CG_SCABLAND,165,155,135,165,155,135
CG_PLAYA,190,180,150,195,185,150
CG_GLACIER_ICE,210,220,225,210,220,225
...
```

Notes:

* You may keep many groups identical between palettes.
* Only a subset (esp. Developed/Agriculture) may need strong lush↔arid variation.

### 3) VALUE overrides for special classes

For classes that must always “read right” for geology (lava, scree, playa, ice, etc.), override by `VALUE`.
Overrides take precedence over macrogroup mappings.

**`value_overrides.csv`**

```csv
VALUE,ColorGroup,Note
7735,CG_GLACIER_ICE,glacier and icefield
9033,CG_VOLCANIC_ROCK,volcanic rock
7065,CG_SCABLAND,scabland
9008,CG_PLAYA,intermountain playa
7734,CG_CLIFF_SCREE_ROCK,alpine bedrock and scree
9016,CG_CLIFF_SCREE_ROCK,alpine bedrock and scree
...
```

---

## Special Handling: Developed Classes & Roads

LANDFIRE/NVC Developed classes (example):

* 7296 Developed-Low Intensity
* 7297 Developed-Medium Intensity
* 7298 Developed-High Intensity
* 7299 Developed-Roads
* 7300 Developed-Open Space

### Policy

* 7296 / 7297 / 7298 / 7300:

    * map to `CG_URBAN`
    * render as neutral “natural fill”

        * neutral green in lush palette
        * neutral tan in arid palette
    * then precip-blend does the contextual transition

* 7299 (Developed-Roads):

    * **inpaint** in categorical space (replace pixels with neighbor classes)
    * do NOT just recolor to neutral, because the line network remains visible under hillshade

---

## QGIS Iteration Workflow

### Step 1: Load data

* EVT/NVC categorical raster
* precipitation raster (or existing precip mask)
* optional hillshade raster (or create it)
* mapping CSVs:

    * `macrogroup_to_colorgroup.csv`
    * `colorgroup_palette.csv`
    * `value_overrides.csv`

### Step 2: Build a render table (recommended)

Maintain a “render table” keyed by `VALUE` that resolves to final ColorGroup and final RGB.

Recommended resolution order:

1. If `VALUE` exists in `value_overrides.csv` → use override ColorGroup
2. Else map `MACROGROUP` via `macrogroup_to_colorgroup.csv`
3. Else fallback (assign to a “needs mapping” ColorGroup for review)

Then join to `colorgroup_palette.csv` to get RGB (lush/arid).

### Step 3: Create two styles

* **Lush style**

    * ColorGroup RGB uses `R_lush/G_lush/B_lush`
* **Arid style**

    * ColorGroup RGB uses `R_arid/G_arid/B_arid`

Save both as QML (or your preferred style format).

### Step 4: Blend lush & arid

Use your existing precipitation blending mechanism:

* `base_rgb = w * lush_rgb + (1 - w) * arid_rgb`

    * where `w` is the precip weight (0..1)

### Step 5: Add hillshade (and other drivers)

Common stack:

* vegetation base (blended)
* hillshade above with **Multiply** (≈ 30–45%)
* optional rock exposure mask (Soft light / Overlay)
* geology overlay layers above

---

## Optional: Subtle Elevation Cue Without “Plastic Gradients”

EVT already implies elevation zones (montane/subalpine/alpine/barren). A DEM-based cue should be very subtle:

### Recommended approach

* Use elevation only to adjust **value** and optionally **saturation**, not hue.
* Apply as a faint modifier late in the pipeline (after landcover colors are established).

If using QGIS for prototyping:

* add a grayscale elevation layer on top with **Soft light** at ~5–10% opacity.

Optionally “gate” elevation cue mostly to high-elevation ColorGroups
(e.g., subalpine, alpine, barren, scree/rock) to avoid broad lowland banding.

---

## Automation (Python) – When Ready

Once colors stabilize:

1. Implement a preprocessing step:

    * inpaint `VALUE=7299` (roads) using categorical neighbor fill (mode/nearest)
2. Generate the two RGB bases (lush/arid) from ColorGroup palette.
3. Apply precipitation blend.
4. Apply additional effects (rock exposure, snow, hillshade protection) per your existing pipeline.

This yields a stable exported RGB GeoTIFF that matches your QGIS look.

---

## Notes on Multi-resolution Overlays (CONUS + 90m + 10m AOIs)

When mixing multiple resolutions:

* Slope/roughness normalization may differ by resolution (10m appears “rougher”).

    * Consider per-resolution config ranges or percentile normalization.
* If you use procedural noise, anchor it to projected X/Y (not row/col) if you want cross-layer continuity.

---

## Checklist

### Minimum viable version

* [ ] macrogroup→colorgroup mapping for: Developed, Agriculture, Water/Wetlands, Barren/Rock, major vegetation families
* [ ] value overrides for key geology signals (scree/lava/scabland/playa/ice)
* [ ] two palettes (lush/arid) with neutral developed fills
* [ ] precip blend works and looks natural in QGIS
* [ ] hillshade multiply doesn’t reveal roads excessively

### Road removal quality

* [ ] categorical inpaint of 7299 makes road networks vanish
* [ ] no “seams” introduced by tiling (if tiled)

---

## Appendix: Special VALUE codes (example list)

(Use `value_overrides.csv` for these.)

Low altitude :

* 7668 tidal salt marsh
* 7156 lowland riparian forest
* 7037 maritime dry mesic douglas fir
* 9826 lowland ruderal grassland
* 7063 broadleaf landslide forest
* 7967 western cool pasture and hayland

Medium :

* 7018 mixed conifer
* 7047 montane mixed conifer

High altitude :

* 7055 dry mesic spruce fir

Alpine :

* 7171 NP Alpine dry grassland
* 7734 alpine bedrock and scree

Great Basin :

* 7080 sagebrush
* 7019 pinyon juniper
* 9503 riparian shrubland

Special :

* 7735 glacier and icefield
* 7065 scabland
* 9016 alpine bedrock and scree
* 9033 volcanic rock
* 9008 intermountain playa

