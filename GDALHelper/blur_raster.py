import math
from typing import Tuple

import numpy as np
from scipy.ndimage import gaussian_filter

PAD_TRUNCATE_DEFAULT = 3.0  # 3*sigma is usually plenty for cartographic masks


def _compute_pad(sigma: float, truncate: float = PAD_TRUNCATE_DEFAULT) -> int:
    """Compute halo padding for Gaussian blur."""
    if sigma <= 0:
        raise ValueError("sigma must be > 0.")
    if truncate <= 0:
        raise ValueError("truncate must be > 0.")
    return int(math.ceil(sigma * truncate))


def _alloc_buffers(
        *, count: int, read_h: int, read_w: int, out_h: int, out_w: int, dtype_out: np.dtype, ) -> \
        Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pre-allocate scratch buffers for speed."""
    # float32 input for scipy
    scratch = np.empty((count, read_h, read_w), dtype=np.float32)
    blurred = np.empty_like(scratch)
    out = np.empty((count, out_h, out_w), dtype=dtype_out)
    return scratch, blurred, out


def _read_into_scratch(src, read_window, fill_value: int, scratch: np.ndarray) -> None:
    """Read raster data into a preallocated float32 scratch buffer."""
    # rasterio returns shape (count, H, W) when indexes is None
    data = src.read(window=read_window, boundless=True, fill_value=fill_value)
    # Convert into scratch (float32) without allocating a new array.
    np.copyto(scratch, data, casting="unsafe")


def _is_all_zero_padded(scratch: np.ndarray) -> bool:
    """Safe skip check: if padded halo has no signal, output is guaranteed zero."""
    # For float32 scratch (converted from uint8/uint16), this is still cheap.
    return not np.any(scratch)


def _blur_in_place(
        scratch: np.ndarray, blurred: np.ndarray, *, sigma: float, count: int, ) -> None:
    """Apply Gaussian blur into the provided output buffer."""
    if count == 1:
        # Single-band fast path
        gaussian_filter(
            scratch[0], sigma=sigma, output=blurred[0], mode="constant", cval=0.0,
            truncate=PAD_TRUNCATE_DEFAULT, )
        return

    # Multi-band: do not blur across bands (sigma=0 on band axis)
    gaussian_filter(
        scratch, sigma=(0.0, sigma, sigma), output=blurred, mode="constant", cval=0.0,
        truncate=PAD_TRUNCATE_DEFAULT, )


def _crop_to_tile(
        blurred: np.ndarray, out: np.ndarray, *, pad: int, out_h: int, out_w: int,
        dtype_out: np.dtype, ) -> np.ndarray:
    """Crop blurred halo back to tile size and cast to output dtype."""
    cropped = blurred[:, pad: pad + out_h, pad: pad + out_w]
    # Cast directly into `out` to avoid allocating.
    np.copyto(out, cropped, casting="unsafe")
    return out
