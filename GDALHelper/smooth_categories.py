import numpy as np
from scipy.ndimage import median_filter, binary_fill_holes, gaussian_filter


def smooth_categories_kernel(data, **kwargs):
    """
    Input data is (Bands, H, W).
    Returns a 2D (H, W) array of the same padded size.
    """
    smooth_map = kwargs['smooth_map']
    background_id = kwargs['background_id']
    claim_threshold = kwargs['claim_threshold']
    median_threshold = kwargs['median_threshold']

    # 1. Force to 2D
    theme_ids_2d = data[0]

    # 2. Check for empty space
    present_ids = [int(v) for v in np.unique(theme_ids_2d) if v != background_id]
    if not present_ids:
        return theme_ids_2d  # Return the 2D padded zeros

    out = np.full_like(theme_ids_2d, background_id)
    support_fields = []
    support_ids = []

    for tid in present_ids:
        sigma = smooth_map.get(tid, 0.0)
        mask = (theme_ids_2d == tid).astype(np.float32)

        if sigma > median_threshold:
            mask = median_filter(mask, size=3)

        mask = binary_fill_holes(mask.astype(bool)).astype(np.float32)

        if sigma > 0.0:
            mask = gaussian_filter(mask, sigma=sigma, mode='reflect')

        support_fields.append(mask)
        support_ids.append(tid)

    if not support_fields:
        return theme_ids_2d

    # 3. Stack and Resolve "winner" layer for each pixel
    stacked = np.stack(support_fields, axis=0)
    winner_index = np.argmax(stacked, axis=0)
    winner_support = np.max(stacked, axis=0)

    winner_ids = np.asarray(support_ids, dtype=theme_ids_2d.dtype)
    claim_mask = winner_support >= claim_threshold
    out[claim_mask] = winner_ids[winner_index[claim_mask]]

    # Returns the PADDED 2D array
    return out
