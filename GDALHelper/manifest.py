import datetime
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Dict, Optional

import rasterio

HASH_CHUNK_BYTES = 64 * 1024
VALID_EXTS = {".tif", ".tiff", ".vrt", ".asc", ".img"}


def generate_manifest(input_dir, sources):
    if not input_dir.exists() or not input_dir.is_dir():
        raise RuntimeError(f"❌ --dir is not a directory: {input_dir}")

    sources_map = _load_sources(sources)

    files_to_process = sorted(
        [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in VALID_EXTS],
        key=lambda p: p.name.lower(), )

    manifest: Dict[str, Any] = {
        "generated_at_utc": _utc_now_iso(), "directory": str(input_dir.resolve()),
        "hash_algo": "sha256", "hash_chunk_bytes": HASH_CHUNK_BYTES,
        "tool_versions": _collect_tool_versions(), "files": {},
    }

    print(f"--- Generating Manifest for {len(files_to_process)} files ---")

    for f_path in files_to_process:
        print(f" Processing: {f_path.name}...")

        stats = f_path.stat()
        file_info: Dict[str, Any] = {
            "size_bytes": stats.st_size, "modified_utc": datetime.datetime.fromtimestamp(
                stats.st_mtime, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"), "sha256": _sha256_file(f_path),
            "source": _normalize_source_entry(sources_map.get(f_path.name)),
        }

        # Capture internal raster identity (detect reproject/resize renames)
        try:
            with rasterio.open(f_path) as src:
                file_info["driver"] = src.driver
                file_info["dtype"] = str(src.dtypes[0]) if src.count >= 1 else "unknown"
                file_info["count"] = src.count
                file_info["crs"] = str(src.crs)
                file_info["transform"] = tuple(src.transform)  # stable + explicit
                file_info["res"] = src.res
                file_info["width"] = src.width
                file_info["height"] = src.height
                file_info["nodata"] = src.nodata
        except Exception as e:
            file_info["raster_error"] = str(e)

        manifest["files"][f_path.name] = file_info


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    sha256_hash = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_BYTES), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def _run_cmd(cmd: list[str]) -> Optional[str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return p.stdout.strip()
    except Exception:
        return None


def _collect_tool_versions() -> Dict[str, Any]:
    """Collect versions that matter for reproducibility without osgeo dependency."""
    import platform
    import sys

    versions: Dict[str, Any] = {
        "python": sys.version.split()[0], "platform": platform.platform(),
    }

    def _try_mod(modname: str) -> None:
        try:
            mod = __import__(modname)
            versions[modname] = getattr(mod, "__version__", "unknown")
        except Exception:
            return

    _try_mod("numpy")
    _try_mod("rasterio")
    _try_mod("scipy")

    # Capture GDAL CLI versions (these are often more stable/available than osgeo)
    # gdalinfo --version prints e.g. "GDAL 3.8.4, released 2024/02/14"
    versions["gdalinfo_version"] = _run_cmd(["gdalinfo", "--version"])
    versions["gdal_edit_version"] = _run_cmd(["gdal_edit.py", "--version"])

    return versions


def _load_sources(path: Optional[str]) -> Dict[str, Any]:
    """Load filename -> source metadata mapping.

    Accept either:
      - string URL values
      - dict values with richer provenance:
          {"url": "...", "dataset": "...", "release": "...", "retrieved_utc": "..."}
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _normalize_source_entry(entry: Any) -> Dict[str, Any]:
    if entry is None:
        return {"url": "Unknown"}
    if isinstance(entry, str):
        return {"url": entry}
    if isinstance(entry, dict):
        # Keep anything provided; ensure url exists
        return {
            "url": entry.get("url", "Unknown"), **{k: v for k, v in entry.items() if k != "url"}
        }
    return {"url": "Unknown"}
