from __future__ import annotations

from pathlib import Path

import numpy as np
from tqdm import tqdm

from GDALHelper.utils import (
    BYTE_MAX,
    TILE_SIZE_DEFAULT,
    ConfigurationError,
    FileError,
)


_HILL_FLOAT_ASSUME_MAX = 1.0
_HILL_FLOAT_MAX_CUTOFF = 1.5
_HILL_SAMPLE_WINDOWS = 6
_NODATA_INPAINT_SEARCH_DIST = 100.0

class Hillshade:
    def __init__(self, args):
        self.args = args
        
    def blend(self) -> None:

        import rasterio
        from rasterio.enums import ColorInterp
        from rasterio.errors import RasterioIOError

        self._validate_args()

        print(
            f"--- Blending '{self.args.hillshade}' + "
            f"'{self.args.color}' (Windowed) ---"
        )

        out_path = Path(self.args.output)

        try:
            with (
                rasterio.open(self.args.hillshade) as src_h,
                rasterio.open(self.args.color) as src_c,
            ):
                self._validate_sources(src_h, src_c)

                profile = self._setup_profile(src_c)

                try:
                    out_path.unlink(missing_ok=True)
                except OSError as exc:
                    raise FileError(
                        f"Could not replace output raster '{out_path}': {exc}"
                    ) from exc

                try:
                    with rasterio.open(out_path, "w", **profile) as dst:
                        if src_c.count == 4:
                            dst.colorinterp = (
                                ColorInterp.red,
                                ColorInterp.green,
                                ColorInterp.blue,
                                ColorInterp.alpha,
                            )
                        else:
                            dst.colorinterp = (
                                ColorInterp.red,
                                ColorInterp.green,
                                ColorInterp.blue,
                            )

                        self._blend_all_windows(src_h, src_c, dst)

                except RasterioIOError as exc:
                    out_path.unlink(missing_ok=True)
                    raise FileError(
                        f"Could not create raster '{out_path}': {exc}"
                    ) from exc

                except OSError as exc:
                    out_path.unlink(missing_ok=True)
                    raise FileError(
                        f"File error while creating '{out_path}': {exc}"
                    ) from exc

                except Exception:
                    # Clean up incomplete output, but do not hide programming errors.
                    out_path.unlink(missing_ok=True)
                    raise

        except RasterioIOError as exc:
            raise FileError(
                f"Could not open hillshade or color raster: {exc}"
            ) from exc

        print(f"✅ Created {out_path}")

    def _validate_sources(self, src_h, src_c) -> None:
        """Validate that the source rasters can be blended safely."""

        if src_h.count < 1:
            raise ConfigurationError(
                "Hillshade raster does not contain any bands."
            )

        if src_c.count not in (3, 4):
            raise ConfigurationError(
                f"Color raster must have 3 or 4 bands; "
                f"found {src_c.count}."
            )

        if (
            src_h.width != src_c.width
            or src_h.height != src_c.height
        ):
            raise ConfigurationError(
                "Hillshade and color raster dimensions do not match."
            )

        if src_h.crs != src_c.crs:
            raise ConfigurationError(
                "Hillshade and color raster CRS do not match."
            )

        if src_h.transform != src_c.transform:
            raise ConfigurationError(
                "Hillshade and color raster grids are not aligned."
            )

    def _setup_profile(self, src_c):
        """Prepare output raster profile based on the color source."""

        profile = src_c.profile.copy()

        has_alpha = src_c.count == 4
        out_count = 4 if has_alpha else 3

        profile.update(
            {
                "driver": "GTiff",
                "count": out_count,
                "dtype": "uint8",
                "compress": "deflate",
                "tiled": True,
                "blockxsize": TILE_SIZE_DEFAULT,
                "blockysize": TILE_SIZE_DEFAULT,
                "photometric": "RGB",
            }
        )

        if self.args.co:
            for opt in self.args.co:
                if "=" not in opt:
                    raise ConfigurationError(
                        f"Invalid creation option '{opt}'. "
                        "Expected KEY=VALUE."
                    )

                key, value = opt.split("=", 1)
                key = key.strip().lower()
                value = value.strip()

                if not key or not value:
                    raise ConfigurationError(
                        f"Invalid creation option '{opt}'. "
                        "Expected KEY=VALUE."
                    )

                profile[key] = (
                    int(value)
                    if value.isdigit()
                    else value
                )

        compress = str(profile.get("compress", "")).lower()

        # YCbCr is appropriate only for three-band JPEG.
        if compress == "jpeg" and out_count == 3:
            if not self._creation_option_supplied("PHOTOMETRIC"):
                profile["photometric"] = "YCBCR"
        else:
            profile["photometric"] = "RGB"

        return profile

    def _creation_option_supplied(self, name: str) -> bool:
        if not self.args.co:
            return False

        name = name.upper()

        for opt in self.args.co:
            if "=" not in opt:
                continue

            key, _ = opt.split("=", 1)

            if key.strip().upper() == name:
                return True

        return False

    def _blend_all_windows(self, src_h, src_c, dst) -> None:
        """Iterate over raster windows and write blended output."""

        self.nodata_val = src_h.nodata
        self.has_nodata = self.nodata_val is not None

        self.hill_den = self._infer_hillshade_denominator(
            src_h,
            src_c,
        )

        s0, s1 = self.args.shadow_range
        h0, h1 = self.args.highlight_range

        self.shadow_start = s0 / BYTE_MAX
        self.shadow_end = s1 / BYTE_MAX
        self.highlight_start = h0 / BYTE_MAX
        self.highlight_end = h1 / BYTE_MAX

        self.protect_shadows = float(
            self.args.protect_shadows
        )
        self.protect_highlights = float(
            self.args.protect_highlights
        )

        self.hill_floor = float(self.args.hill_floor)
        self.hill_gamma = float(self.args.hill_gamma)
        self.hill_ceil = float(self.args.hill_ceil)

        windows = list(src_c.block_windows(1))

        for _, window in tqdm(
            windows,
            total=len(windows),
            unit="block",
            desc="   Blending",
            leave=False,
            mininterval=10.0,
        ):
            chunk = self._process_single_chunk(
                src_h,
                src_c,
                window,
            )

            dst.write(chunk, window=window)

    def _validate_args(self) -> None:
        """Validate command parameters."""

        if not 0.0 <= self.args.protect_shadows <= 1.0:
            raise ConfigurationError(
                "--protect-shadows must be in [0..1]."
            )

        if not 0.0 <= self.args.protect_highlights <= 1.0:
            raise ConfigurationError(
                "--protect-highlights must be in [0..1]."
            )

        s0, s1 = self.args.shadow_range
        h0, h1 = self.args.highlight_range

        if not (
            0 <= s0 <= BYTE_MAX
            and 0 <= s1 <= BYTE_MAX
            and s0 < s1
        ):
            raise ConfigurationError(
                "--shadow-range must be two integers in "
                "[0..255] with START < END."
            )

        if not (
            0 <= h0 <= BYTE_MAX
            and 0 <= h1 <= BYTE_MAX
            and h0 < h1
        ):
            raise ConfigurationError(
                "--highlight-range must be two integers in "
                "[0..255] with START < END."
            )

        if not 0.0 <= self.args.hill_floor <= 1.0:
            raise ConfigurationError(
                "--hill-floor must be in [0..1]."
            )

        if self.args.hill_gamma <= 0.0:
            raise ConfigurationError(
                "--hill-gamma must be greater than 0."
            )

        if not 0.0 < self.args.hill_ceil <= 1.0:
            raise ConfigurationError(
                "--hill-ceil must be in (0..1]."
            )

        if self.args.hill_floor >= self.args.hill_ceil:
            raise ConfigurationError(
                "--hill-floor must be less than --hill-ceil."
            )

        if not 0.0 <= self.args.shade_strength <= 1.0:
            raise ConfigurationError(
                "--shade-strength must be in [0..1]."
            )

    def _infer_hillshade_denominator(
        self,
        src_h,
        src_c,
    ) -> float:
        """Infer normalization denominator for hillshade values."""

        dtype = np.dtype(src_h.dtypes[0])

        if np.issubdtype(dtype, np.integer):
            return float(np.iinfo(dtype).max)

        windows = list(
            src_c.block_windows(1)
        )[:_HILL_SAMPLE_WINDOWS]

        if not windows:
            return _HILL_FLOAT_ASSUME_MAX

        max_vals = []

        for _, window in windows:
            arr = src_h.read(
                1,
                window=window,
            ).astype(
                "float32",
                copy=False,
            )

            if np.isfinite(arr).any():
                max_vals.append(
                    float(np.nanmax(arr))
                )

        if not max_vals:
            return _HILL_FLOAT_ASSUME_MAX

        sample_max = max(max_vals)

        if sample_max <= _HILL_FLOAT_MAX_CUTOFF:
            return _HILL_FLOAT_ASSUME_MAX

        return BYTE_MAX

    def _process_single_chunk(
        self,
        src_h,
        src_c,
        window,
    ):
        """Read, blend, and return one RGB(A) raster window."""

        from rasterio.fill import fillnodata

        rgb = src_c.read(
            [1, 2, 3],
            window=window,
        )

        hill = src_h.read(
            1,
            window=window,
        )

        # Grid alignment has already been validated, so shape mismatch
        # here indicates a programming/runtime problem rather than
        # something that should silently skip a block.
        if hill.shape != rgb.shape[1:]:
            raise RuntimeError(
                "Unexpected raster window shape mismatch."
            )

        if self.has_nodata:
            if np.isnan(self.nodata_val):
                valid = ~np.isnan(hill)
            else:
                valid = hill != self.nodata_val

            hill = fillnodata(
                hill,
                mask=valid.astype("uint8"),
                max_search_distance=(
                    _NODATA_INPAINT_SEARCH_DIST
                ),
            )

        rgb_f = rgb.astype(
            "float32",
            copy=False,
        )

        hill_f = (
            hill.astype("float32", copy=False)
            / float(self.hill_den)
        )

        hill_f = np.clip(
            hill_f,
            0.0,
            1.0,
        )

        if (
            self.hill_floor > 0.0
            or self.hill_gamma != 1.0
            or self.hill_ceil < 1.0
        ):
            # Gamma > 1 lifts midtones/shadows, matching CLI semantics.
            hill_f = hill_f ** (
                1.0 / self.hill_gamma
            )

            if self.hill_floor > 0.0:
                hill_f = (
                    self.hill_floor
                    + (1.0 - self.hill_floor)
                    * hill_f
                )

            if self.hill_ceil < 1.0:
                hill_f = np.minimum(
                    hill_f,
                    self.hill_ceil,
                )

            hill_f = np.clip(
                hill_f,
                0.0,
                1.0,
            )

        w_shadow = (
            self._shadow_weight(hill_f)
            * self.protect_shadows
        )

        w_high = (
            self._highlight_weight(hill_f)
            * self.protect_highlights
        )

        w = np.maximum(
            w_shadow,
            w_high,
        )

        m = (
            hill_f
            + w * (1.0 - hill_f)
        )

        m = np.clip(
            m,
            0.0,
            1.0,
        )

        strength = float(
            self.args.shade_strength
        )

        m = (
            (1.0 - strength)
            + strength * m
        )

        m = np.clip(
            m,
            0.0,
            1.0,
        )

        blended = (
            m[None, :, :]
            * rgb_f
        )

        blended_u8 = (
            np.round(blended)
            .clip(0, BYTE_MAX)
            .astype("uint8")
        )

        if src_c.count == 4:
            alpha = src_c.read(
                4,
                window=window,
            )

            return np.concatenate(
                [
                    blended_u8,
                    alpha[None, :, :],
                ],
                axis=0,
            )

        return blended_u8

    @staticmethod
    def _smoothstep(
        edge0: float,
        edge1: float,
        x,
    ):
        """Smoothstep interpolation from 0 to 1."""

        if edge1 == edge0:
            return np.zeros_like(
                x,
                dtype="float32",
            )

        t = (
            (x - edge0)
            / (edge1 - edge0)
        )

        t = np.clip(
            t,
            0.0,
            1.0,
        )

        return (
            t * t
            * (3.0 - 2.0 * t)
        )

    def _shadow_weight(self, hill_f):
        """Return full protection in deep shadows, fading to zero."""

        return 1.0 - self._smoothstep(
            self.shadow_start,
            self.shadow_end,
            hill_f,
        )

    def _highlight_weight(self, hill_f):
        """Return zero protection below highlights, ramping to full."""

        return self._smoothstep(
            self.highlight_start,
            self.highlight_end,
            hill_f,
        )