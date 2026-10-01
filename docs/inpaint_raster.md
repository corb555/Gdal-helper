
# inpaint_raster - High Level Spec

## Purpose

Reconstruct raster values inside a masked region using surrounding valid raster values. Typical
uses include filling missing data, repairing raster artifacts, reconstructing masked features,
and restoring categorical or continuous surfaces.

The utility is generic and operates on:

- an input raster,
- a mask defining pixels to reconstruct,
- an output raster,
- and a selected reconstruction method.

Reconstruction methods fall into two categories:

- **Value propagation** methods, such as `nearest`, copy existing values from nearby valid pixels.
  These are well suited to categorical rasters because they preserve exact category values.

- **Interpolation/inpainting** methods, such as `idw`, `telea`, `ns`, or `biharmonic`, estimate
  new numeric values from the surrounding surface. These are intended for continuous rasters such
  as elevation or other measured surfaces.

The mask identifies which pixels should be reconstructed; pixels outside the mask remain unchanged.

---

## Example Usage

Produce naturalistic hillshade rasters without visible human artifacts.

At approximately 10 m resolution, especially when using strong hillshade, highways and railroads
can become visually prominent because road cuts, fills, embankments, and grading are represented
in the DEM.

Categorical products such as Landfire EVT can also contain incorrect or missing classifications
along highway and railroad corridors.

This generic raster inpainting utility can be used to reconstruct these masked regions.

For this use case, masks can be derived from OpenStreetMap highway and railroad vectors.

---

# inpaint_raster

Basic operation:

```text
input raster
+
aligned mask
↓
inpaint_raster
↓
reconstructed raster
```

Example uses:

```text
EVT + transportation mask
↓
inpaint_raster --method nearest
↓
categorical processing
```

```text
DEM + transportation mask
↓
inpaint_raster --method telea
↓
dual-gain hillshade
```

Command form:

```text
inpaint_raster <input> <mask> <output>
    --method nearest|idw|telea|ns|biharmonic
    --mask-value ...
    --radius ...
    --max-search-distance ...
    --co ...
```

```bash
gdal-helper inpaint_raster \
    build/Jemez/Jemez_EVT.tif \
    build/Jemez/Jemez_transport_mask.tif \
    build/Jemez/Jemez_EVT_inpaint.tif \
    --method nearest \
    --max-search-distance 100

```


Method-specific parameters may apply only to selected methods. For example, `radius` is meaningful
for local inpainting methods such as Telea and Navier-Stokes, while `max-search-distance` is more
appropriate for methods such as IDW.

---

## Mask Semantics

The mask determines which pixels in the input raster are reconstructed.

Default behavior:

```text
mask == 0       → preserve input pixel
mask != 0       → reconstruct input pixel
```

An optional `--mask-value` parameter may restrict reconstruction to pixels containing a specific
mask value.

Pixels outside the selected mask remain unchanged.

Input NoData pixels must never be used as donor/source pixels during reconstruction.

Existing NoData pixels outside the selected reconstruction mask remain unchanged.

Conceptually:

```text
reconstruction pixels = pixels selected by the mask

valid donor pixels =
    pixels not selected by the reconstruction mask
    AND
    valid input pixels
```

The mask must match the input raster grid:

- CRS,
- dimensions,
- extent,
- resolution,
- pixel alignment.

Mask creation and alignment are responsibilities of upstream tools, not `inpaint_raster`.

---

## Raster Semantics

There are two primary raster semantics.

### Categorical rasters

Categorical raster values represent classes rather than numeric magnitudes.

Reconstruction must preserve valid class values and must not numerically interpolate between
category IDs.

Primary method:

```text
nearest
```

### Continuous rasters

Continuous raster values represent numeric quantities for which interpolation is meaningful.

Candidate methods include:

```text
idw
telea
ns
biharmonic
```

Additional surface-reconstruction methods may be evaluated later.

---

## Methods

### nearest

Fills each masked pixel using the value of the nearest unmasked valid source pixel.

Characteristics:

- preserves exact source values,
- preferred for categorical rasters,
- cannot invent intermediate category values,
- can also be used for continuous rasters when value propagation rather than interpolation is desired.

A likely implementation is nearest-valid-pixel propagation using a distance transform.

Example:

```text
forest forest forest | masked | shrub shrub shrub
                       ↓

forest forest forest forest shrub shrub shrub
```

The transition occurs naturally based on which valid source pixel is nearest.

---

### idw

Uses inverse-distance weighting to estimate values inside the masked region from surrounding valid
pixels.

Nearby donor pixels contribute more strongly than distant donor pixels.

Characteristics:

- continuous rasters,
- well suited to elevation and other continuously varying geospatial surfaces,
- fast compared with more complex surface-reconstruction methods,
- supports a configurable maximum search distance,
- optional smoothing may be applied after interpolation,
- may produce overly smooth or locally rounded surfaces across wide or complex gaps.

This provides a useful geospatial interpolation baseline for comparison with image-oriented
inpainting methods.

No Feedback Loops: IDW evaluates all missing pixels strictly against the original, static donor perimeter. A filled pixel never acts as a donor for its neighbor, completely eliminating feedback oscillations.
Pure Value Blending (No Gradient Extrapolation): IDW does not attempt to extrapolate tangent vectors across space; it creates a harmonic-like blend between opposite banks.
Low-Pass Filtering: GDAL's FillNodata IDW incorporates post-interpolation smoothing passes (3×3 box filter averaging), which explicitly dampens the exact high-frequency ripple noise that hillshading exposes.
---

### telea

Uses Telea-style image inpainting.

The algorithm begins at the boundary of the masked region and propagates inward. For each missing
pixel, it estimates a replacement value from nearby known pixels using a weighted neighborhood
calculation that also considers the geometry of the fill front and local gradients.

This tends to preserve local gradients and works especially well for narrow masked regions.

Characteristics:

- continuous rasters only,
- fast,
- generally well suited to narrow linear masks,
- propagates surrounding gradients and local structure inward,
- likely useful for narrow road, railroad, seam, or artifact masks.

> Note: Computer-vision inpainting methods (telea, ns) are designed for photographs and rely on gradient 
> propagation via the Fast Marching Method. When applied to DEMs, front collisions and gradient feedback 
> loops produce severe transverse striations ("tank track" artifacts) that are heavily amplified by hillshade 
> shaders. idw (inverse distance weighting) is 
> recommended for continuous terrain surfaces

---

### ns

Uses Navier-Stokes-style image inpainting.

Rather than primarily estimating each missing pixel from nearby values, the method uses local
gradients to continue surrounding structures across the masked region.

Compared with Telea, NS places more emphasis on preserving the direction of surrounding structures
and gradients, while Telea is generally a faster local weighted-fill method.

Characteristics:

- continuous rasters only,
- emphasizes continuation of surrounding structure,
- may better preserve directional features across narrow gaps,
- typically slower than Telea,
- still image-oriented rather than specifically designed for terrain surfaces.

---

### biharmonic

Uses biharmonic interpolation across the masked region.

The algorithm reconstructs missing values by solving for a smooth surface that minimizes curvature
while matching the known values around the mask boundary.

Characteristics:

- continuous rasters only,
- produces a smooth reconstructed surface,
- may perform well for wider masked regions,
- may be particularly useful where maintaining a smooth underlying surface is more important than
  preserving fine local texture,
- potentially  computationally expensive and memory intensive .

Biharmonic produces mathematically optimal 
slope continuity for hillshading, but is computationally prohibitive on large masks due to sparse matrix 
memory requirements, and is prone to sagging/bulging where line cuts introduce steep boundary slopes. 


---

## Data Type Handling

The output raster should preserve the input raster datatype unless explicitly configured otherwise.

Some reconstruction libraries operate internally on a limited set of datatypes. Implementations may
therefore convert source values to an appropriate working datatype during reconstruction.

For continuous rasters:

- integer rasters may be converted to floating point for interpolation,
- reconstructed values must be rounded and clamped as appropriate before conversion back to the
  source datatype,
- floating-point inputs should retain their numeric precision as far as supported by the selected
  implementation.

For categorical rasters:

- category values must remain exact,
- categorical reconstruction must not pass category IDs through numeric interpolation,
- large integer category IDs must not be altered by unnecessary floating-point conversion.

---

## NoData Handling

Input NoData pixels are never valid reconstruction donors.

Existing NoData outside the reconstruction mask remains unchanged.

If a masked pixel cannot be reconstructed because no valid donor data is available within the
method's permitted search or context area, the behavior must be explicit rather than silently
producing a partial result.

---

## Unfillable Pixels

A reconstruction method may be unable to reconstruct every selected pixel. This can occur when,
for example:

- a mask reaches the edge of the input raster,
- a masked region borders source NoData,
- no valid donor pixels exist within a configured search or context distance,
- or a masked region is too large for the selected method and parameters.

The command must handle these cases explicitly.

The behavior is controlled by:

```text
--unfillable preserve|nodata|fail

preserve — leave the original input value unchanged. (default)
nodata — replace the unfillable pixel with the input raster's NoData value.
fail — abort the operation if any selected pixel cannot be reconstructed.  

---

## Large Raster Processing

Raster reconstruction may depend on neighboring pixels beyond the immediate output region.

Naively processing independent raster tiles can therefore create visible seams where masked regions
cross processing-window boundaries.

Windowed processing must provide sufficient surrounding context for the selected reconstruction
method.

Conceptually:

```text
expanded read window
┌────────────────────────────┐
│         context            │
│    ┌──────────────────┐    │
│    │   write window   │    │
│    └──────────────────┘    │
│         context            │
└────────────────────────────┘
```

Only the interior write window is written to the output.

The required amount of surrounding context is method-specific and must not be assumed to equal a
single universal `radius` value.

For sparse masks, such as transportation networks, implementations may optimize processing by:

1. identifying connected masked regions,
2. computing buffered bounding boxes around those regions,
3. reconstructing only those local regions,
4. writing the reconstructed masked pixels back to the output raster.

This is an implementation optimization and does not change the command's external behavior.

---

## Band Support

The initial implementation supports single-band rasters.

This covers the initial target uses:

- categorical rasters such as EVT,
- continuous rasters such as DEMs.

Multi-band support may be added later. When supported, the same spatial mask would normally be
applied independently to each band unless a reconstruction method defines different behavior.

---

## Future Methods

Potential future methods include:

- spline interpolation,
- radial basis functions,
- additional PDE-based surface reconstruction,
- terrain-specific interpolation.

These may be useful for wider masks or complex raster surfaces but may require substantially more
processing time or memory.

Method selection and parameters should be evaluated using the final downstream product where
appropriate. For example, DEM reconstruction should be judged not only from the reconstructed DEM
but also from derived products such as hillshade, where subtle interpolation artifacts may become
much more visible.
