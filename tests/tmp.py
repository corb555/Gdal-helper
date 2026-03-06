from __future__ import annotations

from typing import Dict, Optional, List

from GDALHelper.biome_config import SurfaceKey, _BlendSpec
import numpy as np


# -------------------------------------------------------------------------
# Blending
# -------------------------------------------------------------------------

"""
_blend_pixels()
Composite per-pixel palette colors using a sequential blend pipeline.

This method treats the biome renderer like a small terrain shader: it starts from
one or more base palettes  and applies a series of
compositing stages driven by per-pixel factor masks computed in `_calc_factors()`.

Each pipeline stage is defined by a `_BlendSpec` with:
  - `kind`: the compositing operation
  - `factor_nm`: factor key in `factors` used as the per-pixel control signal
  - `source_a` / `source_b` / `target` / `output_key`: palette sources and outputs
    (depending on `kind`)

Conventions:
  - Palette arrays are float32 (H, W, bands) in [0..255].
  - Weight factors are float32 (H, W, 1) in [0..1] (0 = choose A / no effect,
    1 = choose B / full effect).
  - Multiplier factors are float32 (H, W, 1) typically in [0..1] or [0..>1]
    depending on your lighting model (1 = neutral).

Blend stage kinds:

**mix_palettes**
    Precompute a *derived palette* by blending two palettes with a factor mask.
    This does not affect the final image directly; it stores the result into
    `active_palettes[output_key]` so later stages can reference it.

    Use when you want to create “variant” palettes (e.g., soil→soil_red, rock→rock_red)
    once, then treat that output like any other palette.

    Operation:
        derived = lerp(source_a, source_b, factor)
        active_palettes[output_key] = derived

    Expected factor:
        Weight mask in [0..1] (H, W, 1).

**init**
    Initialize the compositing chain. This is the first stage that produces
    `current_img`.

    Use when you need to start from a meaningful base surface (e.g., soil→groundcover
    using vegetation-height, or arid→humid base using precipitation).

    Operation:
        current_img = lerp(source_a, target, factor)

    Expected factor:
        Weight mask in [0..1] (H, W, 1).

**lerp**
    Layer/overlay stage: blend the current image toward a target palette.
    This is how you stack surfaces: rock over ground, snow over rock, canopy
    over groundcover, etc.

    If `target` is missing, the stage becomes a no-op (blending with itself).

    Operation:
        current_img = lerp(current_img, target, factor)

    Expected factor:
        Weight mask in [0..1] (H, W, 1).

**multiply**
    Post-process modulation stage: multiply the RGB channels by a per-pixel
    multiplier (lighting/shading/texture term). Alpha, if present, is preserved.

    Use for hillshade, cliff shadowing, subtle texturing masks, etc.
    Multipliers should be designed so neutral = 1.0.

    Operation:
        current_rgb = current_rgb * factor
        (alpha preserved if bands == 4)

    Expected factor:
        Multiplier term (H, W, 1), neutral at 1.0.

Notes:
    - The pipeline order matters. Typical ordering:
        1) mix_palettes (precompute variants)
        2) first_layer (establish base)
        3) lerp layers (rock/snow/canopy overlays)
        4) multiply (hillshade/textures last)
    - Ensure `factors[spec.factor_nm]` exists for each stage and has the correct
      semantic type (weight vs multiplier) to avoid surprising results.
"""
def _blend_pixels(
        self,
        c: Dict[SurfaceKey, np.ndarray],
        factors: FactorResult,
        pipeline: Optional[List[_BlendSpec]] = None,
) -> np.ndarray:
    """Composite per-pixel colors using a validated sequential blend pipeline.

    Args:
        c: Palette images keyed by :class:`PaletteKey`, each shaped (H, W, bands).
        factors: Per-pixel factor fields keyed by factor name, shaped (H, W, 1).
        pipeline: Ordered list of blend steps.

    Returns:
        Output image block shaped (bands, H, W) as uint8.

    Raises:
        ValueError: If the pipeline is empty, a required palette/factor is missing,
            a factor/palette has an invalid shape, or a step is invalid.
    """
    if not pipeline:
        raise ValueError("Blend Pipeline: pipeline is None/empty.")

    active_palettes = dict(c)
    current_img: Optional[np.ndarray] = None

    def _step_ctx(i: int, spec: _BlendSpec) -> str:
        return (
            f"pipeline item {i}\n kind={spec.comp_op!r} factor={getattr(spec, 'factor_nm', None)!r} "
            f"source_a={getattr(spec, 'source_a', None)!r} source_b={getattr(spec, 'source_b', None)!r} "
            f"target={getattr(spec, 'target', None)!r} output_key={getattr(spec, 'output_key', None)!r}"
        )

    def _require(name: str, val: object, *, i: int, spec: _BlendSpec) -> None:
        if val is None:
            raise ValueError(f"Blend Pipeline: missing {name} at: \n {_step_ctx(i, spec)}.")

    def _get_palette(key: object, fallback: np.ndarray, *, i: int, spec: _BlendSpec) -> np.ndarray:
        """Resolve a palette from active_palettes by key, or raise with context."""
        pal = active_palettes.get(key, None)
        if pal is False: #None:
            # If the caller provided a string key (like "soil_mix"), show known palette keys.
            keys = ", ".join(str(k) for k in sorted(active_palettes.keys(), key=lambda x: str(x)))
            raise ValueError(
                f"Blend Pipeline: palette {key!r} not found for {_step_ctx(i, spec)}. \n"
                f"Available palettes: {keys}\n"
                f"Validate the palette spelling or is in the biome palettes YML file"
            )
        return pal

    def _validate_img(img: np.ndarray, *, name: str, i: int, spec: _BlendSpec) -> None:
        if img is None:
            return False
            #raise ValueError(f"_blend_pixels(): {name} is None for {_step_ctx(i, spec)}.")
        if not isinstance(img, np.ndarray) or img.ndim != 3 or img.shape[2] not in (3, 4):
            return False
            """raise ValueError(
                f"_blend_pixels(): {name} must be ndarray (H,W,3/4), got {type(img).__name__} "
                f"shape={getattr(img, 'shape', None)} for {_step_ctx(i, spec)}."
            )"""
        return True

    def _validate_compat(a: np.ndarray, b: np.ndarray, *, i: int, spec: _BlendSpec) -> None:
        if a.shape != b.shape:
            return False
        else:
            return True
            """raise ValueError(
                f"_blend_pixels(): palette shape mismatch for {_step_ctx(i, spec)}. "
                f"A shape={a.shape}, B shape={b.shape}."
            )"""

    # Require VEGETATION as a universal fallback.
    veg = active_palettes.get(SurfaceKey.HUMID_VEGETATION)
    if veg is None:
        keys = ", ".join(str(k) for k in sorted(active_palettes.keys(), key=lambda x: str(x)))
        raise ValueError(
            "Blend Pipeline: VEGETATION palette is required but missing from `c`. "
            f"Provided palette keys: [{keys}]"
        )
    _validate_img(veg, name="VEGETATION palette", i=-1, spec=pipeline[0])

    for i, spec in enumerate(pipeline):
        # Factor rules: multiply defaults to 1s in caller logic; others default to 0 is NOT safe.
        #factor = _require_factor(spec.factor_nm, i=i, spec=spec)
        #if not self.cfg.effect_on(spec.name):
        #    continue
        if spec.factor_nm not in factors: continue # SKIP
        factor = factors.get(spec.factor_nm)

        if spec.comp_op == "mix_palettes":
            _require("spec.source_a", spec.palette_a, i=i, spec=spec)
            pA = _get_palette(spec.palette_a, veg, i=i, spec=spec)

            if factor is not None:
                # Normal mixing logic
                _require("spec.source_b", spec.palette_b, i=i, spec=spec)
                pB = _get_palette(spec.palette_b, pA, i=i, spec=spec)
                mixed = self._lerp_static(pA, pB, factor)
            else:
                # Fallback: If factor missing (effect disabled), pass through Source A
                mixed = pA

            active_palettes[spec.output_key] = mixed
            continue # Done with this step

        # --- SKIP OTHER STEPS IF FACTOR MISSING ---
        if factor is None:
            continue

        elif spec.comp_op == "init":
            if current_img is not None:
                raise ValueError(
                    f"_blend_pixels(): 'init' encountered but current_img already set at {_step_ctx(i, spec)}."
                )
            _require("spec.source_a", spec.palette_a, i=i, spec=spec)

            p_source = _get_palette(spec.palette_a, veg, i=i, spec=spec)
            p_target = _get_palette(spec.target, p_source, i=i, spec=spec)
            if not _validate_img(p_source, name="p_start", i=i, spec=spec):
                raise ValueError(f"❌ Error: Blend Action={spec.comp_op}: invalid source_a: '{spec.palette_a}' Val={p_source}")
            if not _validate_img(p_target, name="p_target", i=i, spec=spec):
                raise ValueError(f"❌ Error: Blend Action={spec.comp_op}: invalid target: '{spec.target}' Val={p_target}")

            if not _validate_compat(p_source, p_target, i=i, spec=spec):
                raise ValueError(f"❌ Error: Blend Action={spec.comp_op}: source and target are different sizes")

            current_img = self._lerp_static(p_source, p_target, factor)

        elif spec.comp_op == "lerp":

            _require("current_img", current_img, i=i, spec=spec)
            _validate_img(current_img, name="current_img", i=i, spec=spec)

            # If spec.target is None, lerp current_img toward itself (no-op). That's probably a bug.
            _require("spec.target", spec.target, i=i, spec=spec)
            p_target = _get_palette(spec.target, current_img, i=i, spec=spec)
            res1 = _validate_img(p_target, name="p_target", i=i, spec=spec)
            res2 = _validate_compat(current_img, p_target, i=i, spec=spec)
            if res1 and res2:
                current_img = self._lerp_static(current_img, p_target, factor)

        elif spec.comp_op == "multiply":
            _require("current_img", current_img, i=i, spec=spec)
            _validate_img(current_img, name="current_img", i=i, spec=spec)

            if not np.issubdtype(factor.dtype, np.floating):
                factor = factor.astype("float32", copy=False)

            if current_img.shape[:2] != factor.shape[:2]:
                raise ValueError(
                    f"_blend_pixels(): multiply factor shape mismatch for {_step_ctx(i, spec)}. "
                    f"current_img (H,W)=({current_img.shape[0]},{current_img.shape[1]}), "
                    f"factor (H,W)=({factor.shape[0]},{factor.shape[1]})."
                )

            if current_img.shape[2] == 4:
                rgb = current_img[..., :3] * factor
                current_img = np.concatenate([rgb, current_img[..., 3:4]], axis=2)
            else:
                current_img = current_img * factor

        else:
            raise ValueError(
                f"_blend_pixels(): unknown step kind {spec.comp_op!r} for {_step_ctx(i, spec)}.\n"
                f"Available kinds: mix_palettes, init, lerp, multiply"
            )

    _require("current_img (final)", current_img, i=len(pipeline) - 1, spec=pipeline[-1])
    _validate_img(current_img, name="current_img (final)", i=len(pipeline) - 1, spec=pipeline[-1])

    return np.round(current_img.transpose(2, 0, 1)).clip(0, 255).astype("uint8")


