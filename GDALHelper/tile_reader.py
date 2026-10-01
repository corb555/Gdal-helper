def run_tiled_kernel(input_path, output_path, kernel_fn, pad=0, tile_size=512, **kwargs):
    """
    Generic windowed I/O loop with halo/ghost-padding support.
    """
    import rasterio
    from rasterio.windows import Window
    from tqdm import tqdm
    from pathlib import Path

    with rasterio.open(input_path) as src:
        profile = src.profile.copy()
        # We explicitly set count to 1 for categorical smoothing,
        # though this orchestrator should eventually respect src.count
        profile.update(
            {
                "tiled": True, "blockxsize": 256, "blockysize": 256, "compress": "deflate",
                "count": 1, "dtype": "uint8"
            }
        )

        Path(output_path).unlink(missing_ok=True)
        with rasterio.open(output_path, "w", **profile) as dst:
            windows = [w for _, w in dst.block_windows(1)]

            for window in tqdm(windows, desc="   Processing", leave=False, mininterval=120):
                # Calculate the read window including the halo
                read_window = Window(
                    col_off=window.col_off - pad, row_off=window.row_off - pad,
                    width=window.width + 2 * pad, height=window.height + 2 * pad
                )

                # Read with boundless=True to handle image edges (pads with 0)
                data = src.read(window=read_window, boundless=True)

                # --- UPSTREAM CONTRACT ---
                # data is (Bands, H, W). For smoothing, it is (1, H, W)
                result = kernel_fn(data, **kwargs)

                # Crop result back to the original tile size
                h_out, w_out = int(window.height), int(window.width)

                if result.ndim == 3:
                    # Result is (Bands, H, W)
                    final_tile = result[:, pad: pad + h_out, pad: pad + w_out]
                    dst.write(final_tile, window=window)
                else:
                    # Result is (H, W) - THIS IS THE CASE FOR SMOOTHING
                    final_tile = result[pad: pad + h_out, pad: pad + w_out]
                    # THE FIX: Explicitly specify indexes=1 for 2D arrays
                    dst.write(final_tile, window=window, indexes=1)
