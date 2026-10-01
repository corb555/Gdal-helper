
## Purpose

`create_vignette_alpha` creates a reusable single-band alpha raster for blending one raster into another without exposing
the rectangular footprint of the overlaid raster.

A common use case is placing a higher-resolution raster over a lower- or medium-resolution raster. Without edge 
treatment, the higher-resolution layer can appear as an obvious rectangular “stamp.” A conventional straight-edged 
vignette reduces that effect, but the human eye can still detect the regular boundary.

The command therefore generates a vignette whose fade boundary is intentionally irregular. A broad, low-frequency 
 warp breaks up the rectangular edge, while smaller-scale noise prevents the warped boundary itself from 
appearing smooth or repetitive. The resulting transition is much harder to distinguish from natural spatial 
variation in the underlying map.

##  Objectives

The command should:

- keep the raster interior fully opaque and fade only near the boundary;
- avoid straight, geometric, symmetric, or obviously repeated edge patterns;
- use multi-scale irregularity so the vignette boundary is difficult to perceive;
- produce deterministic output when given the same parameters and seed;
- create a reusable alpha raster that can be applied independently as an alpha or blend mask;
- support very large rasters without requiring full-raster arrays in memory;
- use windowed or tiled processing so memory use is bounded by processing tile size rather than raster size;
- avoid unnecessary computation for interior regions that are guaranteed to remain fully opaque;
- provide high throughput suitable for multi-gigabyte production rasters;
- preserve the input raster's grid, CRS, extent, resolution, dimensions, and pixel alignment in the output alpha raster.

The source raster acts as the spatial template for the output. Using an existing raster as the template avoids 
requiring callers to separately specify CRS, extent, resolution, dimensions, transform, and pixel alignment, 
and guarantees that the generated alpha raster matches the source grid exactly.

## High-Level Design

### Command

`create_vignette_alpha` generates a single-band `uint8` GeoTIFF whose values represent opacity:

- `255` = fully opaque
- `0` = fully transparent

The input raster is used only as a spatial template. The output inherits its CRS, extent, resolution, dimensions, 
transform, and pixel alignment.

The command does not read or modify raster pixel data from the source. The source is used only as a 
spatial template for the output grid and metadata.

### Processing Model

The vignette is defined from the raster perimeter inward.

For each output pixel, the implementation computes its distance from each of the four raster edges. Each edge 
distance is then displaced by an independent, deterministic 1D multi-scale noise function evaluated along that edge.

Each edge function combines two scales of irregularity:

- **Warp** introduces broad, low-frequency displacement so the vignette does not follow a visibly rectangular boundary.
- **Noise** adds smaller-scale variation so the warped edge does not appear overly smooth or mechanically generated.

The four displaced edge-distance fields are then combined using a smooth minimum function so that influence transitions 
continuously between adjacent edges, especially near corners.

The resulting effective distance is normalized across the configured fade width, clamped to `0..1`, and passed through 
a smooth transition function to produce the final `0..255` alpha value.

### Edge Displacement

Each raster edge has its own 1D displacement function:

```text
top(x)
bottom(x)
left(y)
right(y)
```

The displacement for each edge is the sum of broad warp and finer detail components:

```text
edge_displacement = broad_warp + fine_noise
```

These functions should:

- contain multiple spatial scales;
- avoid short repeating periods;
- avoid symmetry or reuse between opposite edges;
- remain deterministic for a given seed;
- be defined from stable global edge coordinates;
- be inexpensive to evaluate for arbitrary portions of an edge.

The implementation should not require a full-raster 2D fractal noise field.

### Displaced Edge Distances

For each pixel, the implementation forms four displaced distance fields conceptually equivalent to:

```text
p_top    = distance_to_top    - top_displacement(x)
p_bottom = distance_to_bottom - bottom_displacement(x)
p_left   = distance_to_left   - left_displacement(y)
p_right  = distance_to_right  - right_displacement(y)
```

These fields remain independently defined across the raster rather than assigning each pixel to a single nearest edge.

This avoids discontinuities caused by switching between separate edge-coordinate systems near corners.

### Edge Combination

The four displaced edge-distance fields should be combined using a polynomial smooth minimum rather than a hard `min()`.

A suitable pairwise formulation is:

```text
smin(a, b, k) =
    min(a, b)
    - max(k - abs(a - b), 0)^2 / (4k)
```

When:

```text
abs(a - b) >= k
```

the result is identical to a normal minimum.

When the two distances are within `k` pixels of one another, their influence is blended smoothly. This prevents the abrupt derivative change that a hard minimum can produce along corner bisectors.

The smoothing width `k` should be derived from the fade width and should remain a configurable implementation constant or tunable fraction rather than being fixed to a single universal value. A fraction of approximately `0.2–0.5` of the fade width is a reasonable starting range for visual testing.

The four fields can be combined by repeated pairwise smooth-min operations.

### Windowed Processing

The output is generated in independent raster windows or tiles.

For each window:

1. Determine the global pixel coordinates covered by the window.
2. Determine which raster edges can possibly influence that window.
3. Evaluate only the required 1D edge-displacement functions over the corresponding global edge coordinates.
4. Construct the displaced distance fields for the relevant edges.
5. Combine those fields using smooth minimum.
6. Normalize the resulting effective distance by the fade width.
7. Clamp the normalized value to `0..1`.
8. Apply smoothstep to produce the final opacity.
9. Convert to `uint8` and write the completed window directly to the output raster.

No full-size source, distance, noise, or alpha arrays should be allocated.

Memory consumption should therefore remain approximately proportional to the configured processing tile size rather than to the total raster dimensions.

### Opaque Interior Optimization

Only pixels within a bounded distance of the raster perimeter can be affected by the vignette.

The implementation should calculate a conservative maximum affected depth from:

- fade width;
- maximum broad warp displacement;
- maximum fine-noise displacement;
- corner smoothing allowance.

Any output window completely outside this perimeter band is guaranteed to contain only fully opaque pixels and can be filled directly with `255`.

This avoids vignette calculations across the large interior region of production rasters.

### Per-Edge Window Pruning

For windows that intersect the overall vignette band, individual edges should also be excluded when they cannot influence the window.

For example, a window near the left side of a large raster may require only:

```text
left
```

or, near a corner:

```text
left + top
```

There is no need to evaluate the right or bottom displacement functions when their maximum possible influence cannot reach the window.

This reduces noise generation and array operations further, particularly for very large rasters with relatively narrow vignette borders.

### Corner Handling

Corners are handled by combining independently displaced edge-distance fields rather than projecting pixels onto a single nearest perimeter coordinate.

This avoids sharp diagonal seams caused by switching abruptly between horizontal and vertical edge-coordinate systems.

Smooth minimum blends competing edge influences through the corner transition region, producing a continuous fade with a smoothly changing gradient.

The four edge functions do not need to share the same noise signal or be symmetric at a corner.

### Distance Clamping and Alpha Mapping

Independent edge displacement can produce negative effective distances, particularly near strongly eroded corners. This is valid and should not be treated as an error.

After edge combination:

```text
t = clip(effective_distance / fade_width, 0.0, 1.0)
```

The alpha transition is then generated using smoothstep:

```text
alpha = (3t² - 2t³) × 255
```

Values below the displaced boundary therefore clamp cleanly to transparent, while pixels beyond the fade width become fully opaque.

This also makes aggressive warp and noise parameters safe from numerical overflow into invalid alpha values.

### Determinism

When `--seed` is provided, output must be reproducible regardless of:

- processing tile size;
- tile traversal order;
- number of worker processes;
- machine architecture, where practical.

Noise generation must therefore depend on stable global edge coordinates and deterministic per-edge seed derivation rather than on the sequential state of a random-number generator consumed during tile processing.

Each edge should derive an independent noise stream from the user seed so that opposite edges do not share visible structure.

This also keeps future parallel processing possible.

### Parameters

The existing high-level controls remain appropriate:

- `--border` — width of the fade as a percentage of the raster's smaller dimension.
- `--warp` — magnitude of broad edge displacement relative to the fade width.
- `--noise` — magnitude of smaller-scale edge irregularity relative to the fade width.
- `--seed` — optional deterministic noise seed.
- `--co` — output GeoTIFF creation options.

The parameters describe visual intent rather than implementation details.

For example:

```yaml
create_vignette_alpha:
    border: 2
    noise: 20
    warp: 50
```

means a relatively narrow fade whose boundary has substantial broad displacement plus moderate fine-scale irregularity.

### Output

The output should:

- be a single-band `uint8` GeoTIFF;
- use `255` for fully opaque interior pixels;
- transition smoothly toward `0` at the effective displaced raster boundary;
- have no NoData requirement;
- preserve the input grid exactly;
- be tiled and compressed by default;
- support standard GeoTIFF creation-option overrides.

### Implementation Structure

The CLI class should remain thin and compliant with GeoRasterUtil's framework:

```python
@register_command("create_vignette_alpha")
class CreateVignetteAlpha(IOCommand):
    ...
```

Its responsibilities should be limited to argument definition, validation/orchestration, and reporting.

The raster algorithm should live in a separate module, for example:

```text
GeoRasterUtil/
    vignette_alpha.py
```

That module should own:

```text
configuration / validation
edge-coordinate calculation
multi-scale 1D edge displacement
smooth-min edge combination
window alpha calculation
opaque-window detection
per-edge pruning
output writing
```

This keeps the algorithm independent of the CLI and leaves the vignette-generation logic reusable by `apply_vignette` or other compositing operations.
