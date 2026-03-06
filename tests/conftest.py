import pytest
import rasterio
import numpy as np
from rasterio.transform import from_origin

@pytest.fixture
def tiny_rasters(tmp_path):
    """
    Creates a set of valid, tiny (10x10) GeoTIFFs for testing.
    Returns a dictionary of pathlib.Path objects.
    """

    # Common settings
    width, height = 10, 10
    transform = from_origin(0, 0, 1, 1) # Simple coordinate system
    crs = "EPSG:3857"

    # 1. Create Foreground (Layer A) - RGB
    path_a = tmp_path / "layer_a.tif"
    with rasterio.open(
            path_a, 'w', driver='GTiff', height=height, width=width,
            count=3, dtype='uint8', crs=crs, transform=transform
    ) as dst:
        data = np.random.randint(0, 255, (3, height, width), dtype='uint8')
        dst.write(data)

    # 2. Create Background (Layer B) - RGB
    path_b = tmp_path / "layer_b.tif"
    with rasterio.open(
            path_b, 'w', driver='GTiff', height=height, width=width,
            count=3, dtype='uint8', crs=crs, transform=transform
    ) as dst:
        data = np.zeros((3, height, width), dtype='uint8') # All black
        dst.write(data)

    # 3. Create Mask - Grayscale (1 Band)
    path_mask = tmp_path / "mask.tif"
    with rasterio.open(
            path_mask, 'w', driver='GTiff', height=height, width=width,
            count=1, dtype='uint8', crs=crs, transform=transform
    ) as dst:
        # Create a gradient mask
        data = np.arange(100).reshape(1, 10, 10).astype('uint8')
        dst.write(data)

    return {
        "A": str(path_a),
        "B": str(path_b),
        "Mask": str(path_mask)
    }