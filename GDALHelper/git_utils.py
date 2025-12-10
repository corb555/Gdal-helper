# git_utils.py

import json
import subprocess
from typing import Optional


def get_git_hash() -> str:
    """
    Gets the current git commit hash for the repository.

    Checks if the repository has uncommitted changes ("dirty"). If so, it
    appends a '-dirty' suffix to the hash.

    Returns:
        The git commit hash string, or None if not in a git repository.
    """
    try:
        # Get the full commit hash
        hash_process = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        git_hash = hash_process.stdout.strip()

        # Check for uncommitted changes
        status_process = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        if status_process.stdout:
            git_hash += "-dirty"

        return git_hash

    except (subprocess.CalledProcessError, FileNotFoundError):
        # Handle cases where git is not installed or this is not a git repo
        return None


def set_tiff_version(filepath: str, version_hash: str):
    """
    Embeds a version hash into a TIFF's metadata using gdal_edit.py.

    This modifies the file in-place. Note: This only supports TIFFs (.tif).
    Other formats will be skipped with a warning.

    Args:
        filepath: The path to the TIFF file to be updated.
        version_hash: The version string (e.g., a git hash) to embed.
    """
    # Simple check for TIF extension
    if not filepath.lower().endswith(('.tif', '.tiff')):
        raise RuntimeError(
            f"❌ Failed to set version metadata on {filepath}. Not a TIFF"
        )

    try:
        metadata_tag = f"VERSION={version_hash}"
        command = ["gdal_edit.py", "-mo", metadata_tag, filepath]
        # Use a generic run command here as it's part of the utility
        subprocess.run(command, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        stderr = e.stderr.strip() if hasattr(e, 'stderr') else 'Is gdal_edit.py in your PATH?'
        raise RuntimeError(
            f"❌ Failed to set version metadata on {filepath}.\n{stderr}"
        )


def get_tiff_version(filepath: str) -> Optional[str]:
    """
    Reads the embedded version hash from a TIFF's metadata.

    Args:
        filepath: The path to the TIFF file to inspect.

    Returns:
        The version string if found, otherwise None.
    """
    # Simple check for TIF extension
    if not filepath.lower().endswith(('.tif', '.tiff')):
        return None

    try:
        result = subprocess.run(
            ["gdalinfo", "-json", filepath], capture_output=True, text=True, check=True
        )
        info = json.loads(result.stdout)
        # The metadata is nested under a blank key in the 'metadata' dict
        # Note: Changed key to 'VERSION' to match the setter function
        return info.get("metadata", {}).get("", {}).get("VERSION")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None