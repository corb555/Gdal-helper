import os
import subprocess
import pytest
import rasterio

# Import your functions
from GDALHelper.git_utils import get_git_hash, set_tiff_version, get_tiff_version

@pytest.fixture
def temp_git_repo(tmp_path):
    """
    Creates a temporary folder, initializes it as a git repo,
    and sets the Current Working Directory (CWD) to it.
    """
    # 1. Initialize Git
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)

    # 2. Configure dummy user (required for CI environments)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=tmp_path, check=True)

    # 3. Create a commit so HEAD exists
    (tmp_path / "README.md").touch()
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True)

    # 4. Switch the process CWD to this folder so get_git_hash() sees it
    original_cwd = os.getcwd()
    os.chdir(tmp_path)

    yield tmp_path

    # 5. Cleanup: Switch back after test
    os.chdir(original_cwd)

import numpy as np  # <--- Import numpy here

@pytest.fixture
def sample_tiff(temp_git_repo):
    """Creates a tiny valid GeoTIFF inside the temp git repo."""
    filename = temp_git_repo / "test.tif"

    # Create a 10x10 dummy image
    profile = {
        'driver': 'GTiff',
        'height': 10, 'width': 10, 'count': 1, 'dtype': 'uint8'
    }
    with rasterio.open(filename, 'w', **profile) as dst:
        # Use np.zeros directly
        dst.write(np.zeros((1, 10, 10), dtype='uint8'))

    return str(filename)
# ==========================================
# THE TESTS
# ==========================================

def test_get_git_hash_clean(temp_git_repo):
    """Test retrieving hash from a clean repo."""
    git_hash = get_git_hash()
    assert len(git_hash) == 40  # Standard SHA-1 length
    assert "dirty" not in git_hash

def test_get_git_hash_dirty(temp_git_repo):
    """Test retrieving hash from a dirty repo."""
    # Modify a file to make repo dirty
    (temp_git_repo / "README.md").write_text("Change")

    git_hash = get_git_hash()
    assert git_hash.endswith("-dirty")

def test_set_and_get_tiff_version(temp_git_repo, sample_tiff):
    """Test the full cycle: Get Hash -> Stamp Tiff -> Read Tiff."""

    # 1. Get the hash
    current_hash = get_git_hash()

    # 2. Stamp the TIFF
    set_tiff_version(sample_tiff, current_hash)

    # 3. Read it back
    embedded_version = get_tiff_version(sample_tiff)

    assert embedded_version == current_hash
    print(f"\nSuccess! Stamped and retrieved: {embedded_version}")