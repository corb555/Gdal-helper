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
        The git commit hash string, or "unknown" if not in a git repository.
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
            print("⚠️ Warning: Git repository has uncommitted changes.")
            git_hash += "-dirty"

        return git_hash

    except (subprocess.CalledProcessError, FileNotFoundError):
        # Handle cases where git is not installed or this is not a git repo
        print("⚠️ Warning: Could not get git hash. Not a git repository or git is not installed.")
        return "unknown"


def set_geotiff_version(filepath: str, version_hash: str):
    """

    Embeds a version hash into a GeoTIFF's metadata using gdal_edit.py.

    This modifies the file in-place.

    Args:
        filepath: The path to the GeoTIFF file to be updated.
        version_hash: The version string (e.g., a git hash) to embed.
    """
    print(f"  Stamping version '{version_hash[:12]}...' into '{filepath}'")
    try:
        metadata_tag = f"VERSION={version_hash}"
        command = ["gdal_edit.py", "-mo", metadata_tag, filepath]
        # Use a generic run command here as it's part of the utility
        subprocess.run(command, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        stderr = e.stderr.strip() if hasattr(e, 'stderr') else 'Is gdal_edit.py in your PATH?'
        raise RuntimeError(
            f"❌ Failed to set version metadata on {filepath}.\n   --- STDERR ---\n{stderr}"
            )


def get_geotiff_version(filepath: str) -> Optional[str]:
    """
    Reads the embedded version hash from a GeoTIFF's metadata.

    Args:
        filepath: The path to the GeoTIFF file to inspect.

    Returns:
        The version string if found, otherwise None.
    """
    try:
        result = subprocess.run(
            ["gdalinfo", "-json", filepath], capture_output=True, text=True, check=True
        )
        info = json.loads(result.stdout)
        # The metadata is nested under a blank key in the 'metadata' dict
        return info.get("metadata", {}).get("", {}).get("LITEBUILD_VERSION")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
