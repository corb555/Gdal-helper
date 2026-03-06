from dataclasses import dataclass
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from tqdm import tqdm

TILE_SIZE_DEFAULT = 256
COMPRESS_DEFAULT = "deflate"
OUTPUT_DTYPE = "uint8"
ALPHA_ON_DEFAULT = 255
ALPHA_OFF_DEFAULT = 0
NODATA_INDEX_DEFAULT = 0
SRC_BAND_INDEX = 1
DST_BAND_INDEX = 1
MAX_CLASSES = 255

# EVT / categorical bounds (offset LUT range)
MIN_CODE = -9999
MAX_CODE = 9999
CODE_OFFSET = -MIN_CODE  # 9999
LUT_SIZE = MAX_CODE - MIN_CODE + 1  # 19999


@dataclass(frozen=True, slots=True)
class _ClassSpec:
    """Single derived class specification.

    Attributes:
        name: Human-readable name (comment/debug).
        rgb_hex: Optional RGB hex string (e.g., "4d4339") used to write a palette.
        ids: EVT category ids to map to this class.
    """
    name: str
    rgb_hex: Optional[str]
    ids: Tuple[int, ...]


def _parse_rgb_hex(rgb_hex: str) -> Tuple[int, int, int]:
    """Parse a 6-digit RGB hex string to an (R, G, B) tuple.

    Args:
        rgb_hex: RGB hex string with or without leading '#'.

    Returns:
        Tuple of (r, g, b) integers in [0, 255].

    Raises:
        ValueError: If the string is not a valid RGB hex.
    """
    s = rgb_hex.strip().lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ValueError(f"Invalid rgb value '{rgb_hex}'. Expected 6 hex digits like '4d4339'.")
    r = int(s[0:2], 16)
    g = int(s[2:4], 16)
    b = int(s[4:6], 16)
    return r, g, b


def ZZ_load_config(path: Path) -> Dict:
    """Load YAML config dict.

    Expected structure (basic):
        config_type: "Reclassify"
        classes: [{name, ids, rgb?}, ...]
        alpha_output: "path/to/alpha.tif" (optional)
        strict: true/false (optional)
        tile_size: 256 (optional)
        compress: "deflate" (optional)
        nodata_index: 0 (optional)
        alpha_on: 255 (optional)
        alpha_off: 0 (optional)

    Args:
        path: YAML file path.

    Returns:
        Parsed config dict.

    Raises:
        FileNotFoundError: If YAML path doesn't exist.
        ValueError: If YAML is malformed.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        import yaml  # Late import: only required for this command.
    except Exception as e:  # pragma: no cover
        raise RuntimeError("PyYAML is required for --config parsing (pip install pyyaml).") from e

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError("Config YAML must parse to a mapping/object at the top level.")
    return cfg


def _normalize_classes(cfg: Dict) -> List[_ClassSpec]:
    """Normalize 'classes' list from config.

    Args:
        cfg: Loaded config mapping.

    Returns:
        List of normalized class specs in config order.

    Raises:
        ValueError: If classes are missing/invalid.
    """
    raw = cfg.get("classes")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Config must include a non-empty 'classes' list.")

    if len(raw) > MAX_CLASSES:
        raise ValueError(f"Too many classes ({len(raw)}). Max supported is {MAX_CLASSES}.")

    classes: List[_ClassSpec] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"classes[{i}] must be a mapping with keys: name, ids, (optional) rgb."
            )
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"classes[{i}].name is required and must be non-empty.")
        rgb_hex = item.get("rgb")
        rgb_hex = str(rgb_hex).strip() if rgb_hex is not None else None

        ids_raw = item.get("ids")
        if not isinstance(ids_raw, list) or not ids_raw:
            raise ValueError(f"classes[{i}].ids must be a non-empty list of integers.")

        try:
            ids = tuple(int(v) for v in ids_raw)
        except Exception as e:
            raise ValueError(f"classes[{i}].ids must contain only integers.") from e

        if any(v < 0 for v in ids):
            raise ValueError(f"classes[{i}].ids must be >= 0.")

        classes.append(_ClassSpec(name=name, rgb_hex=rgb_hex, ids=ids))

    return classes


def _validate_no_duplicate_ids(classes: Sequence[_ClassSpec]) -> None:
    """Fail if any EVT id appears in more than one class."""
    seen: Dict[int, str] = {}
    for cls in classes:
        for cat_id in cls.ids:
            prev = seen.get(cat_id)
            if prev is not None:
                raise ValueError(
                    f"Duplicate category id {cat_id} appears in multiple classes: '{prev}' and '"
                    f"{cls.name}'."
                )
            seen[cat_id] = cls.name


@dataclass(frozen=True, slots=True)
class ReclassRule:
    """A reclassification rule.

    Attributes:
        name: Human-friendly name (comment/debug).
        value: Output value to assign when any of `ids` match.
        ids: Source category ids that map to this rule.
        rgb_hex: Optional RGB hex string used for an embedded palette on the output.
    """
    name: str
    value: int
    ids: Tuple[int, ...]
    rgb_hex: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ReclassOptions:
    strict: bool = True
    tile_size: int = TILE_SIZE_DEFAULT
    compress: str = COMPRESS_DEFAULT
    nodata_value: int = 0
    default_value: int = 0
    input_nodata: Tuple[int, ...] = (MIN_CODE,)
    write_alpha: bool = False
    alpha_output: Optional[Path] = None
    alpha_on: int = 255
    alpha_off: int = 0

    # make alpha GeoTIFF tile-sparse when possible (huge win for blur)
    alpha_sparse_ok: bool = True

    report_unmapped: bool = False
    max_unmapped_ids: int = 50


from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True, slots=True)
class _LutMapper:
    """Offset-LUT mapper for fast per-block reclassification."""
    lut: np.ndarray
    default_value: np.uint8
    alpha_on: np.uint8
    alpha_off: np.uint8
    input_nodata: Tuple[int, ...]  # e.g. (-9999, 32767)

    def _nodata_mask(self, data: np.ndarray) -> Optional[np.ndarray]:
        """Return mask of pixels that are input-nodata (or None if none configured)."""
        codes = self.input_nodata
        if not codes:
            return None

        # Fast paths for small sets (common case)
        if len(codes) == 1:
            return data == codes[0]
        if len(codes) == 2:
            a, b = codes
            return (data == a) | (data == b)
        if len(codes) == 3:
            a, b, c = codes
            return (data == a) | (data == b) | (data == c)
        if len(codes) == 4:
            a, b, c, d = codes
            return (data == a) | (data == b) | (data == c) | (data == d)

        # Rare path: many nodata codes
        return np.isin(data, np.asarray(codes, dtype=np.int32))

    def map_block_into(
            self, data: np.ndarray, *, idx_out: np.ndarray, alpha_out: Optional[np.ndarray],
            shifted_tmp: np.ndarray, ) -> None:
        """Map a 2D block into preallocated outputs.

        Notes:
            - shifted_tmp is int32 and same shape as data.
            - idx_out/alpha_out are uint8 and same shape as data.
        """
        # Ensure predictable arithmetic type (rasterio often gives int16, but be explicit).
        # This is a view/cast only if needed; no copy if already int32-compatible.
        d = data.astype(np.int32, copy=False)

        nodata_mask = self._nodata_mask(d)

        # shifted_tmp = d + CODE_OFFSET (no alloc)
        np.add(d, CODE_OFFSET, out=shifted_tmp, casting="unsafe")

        # Force nodata sentinels to map to LUT index 0 (which corresponds to MIN_CODE=-9999).
        if nodata_mask is not None:
            any_nd = bool(nodata_mask.any())
            if any_nd:
                shifted_tmp[nodata_mask] = 0

        try:
            # idx_out = lut[shifted_tmp] (no alloc)
            np.take(self.lut, shifted_tmp, out=idx_out, mode="raise")
        except IndexError as e:
            dmin = int(d.min())
            dmax = int(d.max())
            raise RuntimeError(
                f"❌ Input contains category values outside supported range "
                f"[{MIN_CODE}, {MAX_CODE}], excluding configured input_nodata={self.input_nodata}. "
                f"Found min={dmin}, max={dmax}."
            ) from e

        if alpha_out is not None:
            alpha_out.fill(self.alpha_off)
            alpha_out[idx_out != self.default_value] = self.alpha_on


def _build_lut_mapper(
        rules: Sequence["ReclassRule"], *, default_value: int, alpha_on: int, alpha_off: int,
        input_nodata: Tuple[int, ...],  # NEW
) -> _LutMapper:
    """Build the offset LUT once per run (fast)."""
    _validate_uint8("options.default_value", default_value)
    _validate_uint8("options.alpha.on", alpha_on)
    _validate_uint8("options.alpha.off", alpha_off)

    # Fail fast: input nodata values must be integers
    try:
        nodata_codes = tuple(int(v) for v in input_nodata)
    except Exception as e:
        raise ValueError("input_nodata must contain only integers.") from e

    # Optional: enforce MIN_CODE in nodata codes (nice invariant)
    if MIN_CODE not in nodata_codes:
        nodata_codes = (MIN_CODE, *nodata_codes)

    lut = np.full(LUT_SIZE, np.uint8(default_value), dtype=np.uint8)

    for r in rules:
        ids = np.asarray(r.ids, dtype=np.int32)
        if ids.size == 0:
            continue

        # ids are config-validated >=0; also ensure within MAX_CODE
        max_id = int(ids.max())
        if max_id > MAX_CODE:
            raise ValueError(f"Rule '{r.name}' contains ids > {MAX_CODE}. max={max_id}")

        lut[ids + CODE_OFFSET] = np.uint8(r.value)

    return _LutMapper(
        lut=lut, default_value=np.uint8(default_value), alpha_on=np.uint8(alpha_on),
        alpha_off=np.uint8(alpha_off), input_nodata=nodata_codes, )


def _load_yaml(path: Path) -> Dict:
    """Load a YAML file into a dict.

    Args:
        path: YAML file path.

    Returns:
        Parsed YAML mapping.

    Raises:
        FileNotFoundError: If YAML path doesn't exist.
        ValueError: If YAML isn't a mapping.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    try:
        import yaml  # Late import: only required for this command.
    except Exception as e:  # pragma: no cover
        raise RuntimeError("PyYAML is required for --config parsing (pip install pyyaml).") from e

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError("Config YAML must parse to a mapping/object at the top level.")
    return cfg


def _validate_uint8(name: str, value: int) -> None:
    """Validate a value fits in uint8.

    Args:
        name: Field name.
        value: Value to validate.

    Raises:
        ValueError: If out of range.
    """
    if not (0 <= value <= 255):
        raise ValueError(f"{name} must be within [0, 255]. Got {value}.")


def _parse_reclass_config(cfg: Dict) -> Tuple[List[ReclassRule], ReclassOptions]:
    """Parse and validate the reclassify configuration.

    Supported YAML schema (top-level):

    classes:
      - name: water
        ids: [7735]
        rgb: "7d95ab"     # optional
        value: 10         # optional (defaults to config order: 1..N)

    options:
      strict: true
      default_value: 0
      nodata_value: 0
      tile_size: 256
      compress: deflate
      alpha:
        enabled: true
        output: "/path/to/alpha.tif"   # optional
        on: 255
        off: 0
      report_unmapped:
        enabled: false
        max_ids: 50

    Args:
        cfg: Parsed YAML dict.

    Returns:
        (rules, options)

    Raises:
        ValueError: If invalid config.
    """
    raw_classes = cfg.get("classes")
    if not isinstance(raw_classes, list) or not raw_classes:
        raise ValueError("Config must include a non-empty 'classes' list.")

    if len(raw_classes) > MAX_CLASSES:
        raise ValueError(f"Too many classes ({len(raw_classes)}). Max supported is {MAX_CLASSES}.")

    opt_raw = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
    alpha_raw = opt_raw.get("alpha") if isinstance(opt_raw.get("alpha"), dict) else {}
    report_raw = opt_raw.get("report_unmapped") if isinstance(
        opt_raw.get("report_unmapped"), dict
    ) else {}

    options = ReclassOptions(
        strict=True,  # your policy: cannot be disabled
        tile_size=int(opt_raw.get("tile_size", TILE_SIZE_DEFAULT)),
        compress=str(opt_raw.get("compress", COMPRESS_DEFAULT)),
        nodata_value=int(opt_raw.get("nodata_value", 0)),
        default_value=int(opt_raw.get("default_value", 0)),
        input_nodata=(opt_raw.get("input_nodata", 0)),
        write_alpha=bool(alpha_raw.get("enabled", True)),
        alpha_output=Path(str(alpha_raw["output"])) if alpha_raw.get("output") else None,
        alpha_on=int(alpha_raw.get("on", 255)), alpha_off=int(alpha_raw.get("off", 0)),
        alpha_sparse_ok=bool(alpha_raw.get("sparse_ok", True)),
        report_unmapped=bool(report_raw.get("enabled", False)),
        max_unmapped_ids=int(report_raw.get("max_ids", 50)), )

    if options.tile_size <= 0:
        raise ValueError("options.tile_size must be > 0.")
    _validate_uint8("options.nodata_value", options.nodata_value)
    _validate_uint8("options.default_value", options.default_value)
    _validate_uint8("options.alpha.on", options.alpha_on)
    _validate_uint8("options.alpha.off", options.alpha_off)
    if options.max_unmapped_ids <= 0:
        raise ValueError("options.report_unmapped.max_ids must be > 0.")

    rules: List[ReclassRule] = []
    used_values: set[int] = set()
    for i, item in enumerate(raw_classes):
        if not isinstance(item, dict):
            raise ValueError(
                f"classes[{i}] must be a mapping with keys: name, ids, (optional) rgb, (optional) "
                f"value."
            )

        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"classes[{i}].name is required and must be non-empty.")

        ids_raw = item.get("ids")
        if not isinstance(ids_raw, list) or not ids_raw:
            raise ValueError(f"classes[{i}].ids must be a non-empty list of integers.")

        try:
            ids = tuple(int(v) for v in ids_raw)
        except Exception as e:
            raise ValueError(f"classes[{i}].ids must contain only integers.") from e

        if any(v < 0 for v in ids):
            raise ValueError(f"classes[{i}].ids must be >= 0.")

        value = int(item.get("value", i + 1))
        _validate_uint8(f"classes[{i}].value", value)
        if value == options.default_value:
            raise ValueError(
                f"classes[{i}].value ({value}) conflicts with options.default_value ("
                f"{options.default_value})."
            )
        if value in used_values:
            raise ValueError(
                f"Duplicate output class value {value} in classes (values must be unique)."
            )
        used_values.add(value)

        rgb_hex = item.get("rgb")
        rgb_hex = str(rgb_hex).strip() if rgb_hex is not None else None
        if rgb_hex:
            _ = _parse_rgb_hex(rgb_hex)  # validate

        rules.append(ReclassRule(name=name, value=value, ids=ids, rgb_hex=rgb_hex))

    return rules, options


def _derive_alpha_path(output_path: Path, alpha_output: Optional[Path]) -> Path:
    """Derive alpha output path from main output if not explicitly provided."""
    return alpha_output if alpha_output is not None else output_path.with_name(
        f"{output_path.stem}_alpha{output_path.suffix}"
    )


def _prepare_gtiff_profile(
        src_profile: Dict, *, nodata: int, tile_size: int, compress: str,
        creation_options: Optional[Dict[str, str]] = None, ) -> Dict:
    """Prepare a GeoTIFF profile for a single-band uint8 raster."""
    base = {
        **src_profile, "driver": "GTiff", "dtype": OUTPUT_DTYPE, "count": 1, "nodata": nodata,
        "compress": compress, "tiled": True, "blockxsize": tile_size, "blockysize": tile_size,
    }
    if creation_options:
        base.update(creation_options)  # e.g., {"SPARSE_OK": "YES"}
    return base


def _compute_reclass_block(
        data: np.ndarray, rules: Sequence[ReclassRule], *, default_value: int, alpha_on: int,
        alpha_off: int, ) -> Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
    """Reclassify a single window block.

    Args:
        data: Source categorical data (2D array).
        rules: Reclass rules in priority order.
        default_value: Value for unmatched pixels.
        alpha_on: Alpha value for matched pixels.
        alpha_off: Alpha value for unmatched pixels.

    Returns:
        (index_block, alpha_block, unmapped_ids_block)
        unmapped_ids_block is an array of unique unmapped ids (or None if not computed).
    """
    index = np.full(data.shape, default_value, dtype=np.uint8)
    alpha = np.full(data.shape, alpha_off, dtype=np.uint8)

    remaining = index == default_value
    for rule in rules:
        if not remaining.any():
            break
        hit = np.isin(data, rule.ids)
        assign = hit & remaining
        if assign.any():
            index[assign] = rule.value
            alpha[assign] = alpha_on
            remaining = index == default_value

    unmapped_ids: Optional[np.ndarray] = None
    if remaining.any():
        # Unique ids for pixels that did not match any rule.
        # Note: can be expensive; only enabled when requested by config.
        unmapped_ids = np.unique(data[remaining])

    return index, alpha, unmapped_ids


def _guard_output_paths(
        *, src_path: str, out_path: Path, alpha_path: Optional[Path], ) -> Tuple[
    Path, Optional[Path]]:
    """Validate input/output paths to prevent destructive or ambiguous deletes.

    This is intentionally strict. It prevents:
      - deleting the input file
      - output == alpha output
      - writing to a directory path
      - deleting paths that do not resolve cleanly
      - deleting directories

    Args:
        src_path: Input raster path.
        out_path: Output index raster path.
        alpha_path: Optional alpha raster path.

    Returns:
        (resolved_out_path, resolved_alpha_path)

    Raises:
        RuntimeError: If any unsafe condition is detected.
    """
    src = Path(src_path).expanduser()
    if not src.exists():
        raise RuntimeError(f"❌ Input raster does not exist: {src}")
    if src.is_dir():
        raise RuntimeError(f"❌ Input raster path is a directory: {src}")

    out = out_path.expanduser()
    alpha = alpha_path.expanduser() if alpha_path is not None else None

    # If an output path exists and is a directory, fail early.
    if out.exists() and out.is_dir():
        raise RuntimeError(f"❌ Output path is a directory: {out}")
    if alpha is not None and alpha.exists() and alpha.is_dir():
        raise RuntimeError(f"❌ Alpha output path is a directory: {alpha}")

    # Resolve paths (without requiring output to exist).
    src_r = src.resolve(strict=True)
    out_r = out.resolve(strict=False)
    alpha_r = alpha.resolve(strict=False) if alpha is not None else None

    # Disallow collisions.
    if out_r == src_r:
        raise RuntimeError(f"❌ Output path equals input path: {out_r}")
    if alpha_r is not None and alpha_r == src_r:
        raise RuntimeError(f"❌ Alpha output path equals input path: {alpha_r}")
    if alpha_r is not None and alpha_r == out_r:
        raise RuntimeError(f"❌ Alpha output path equals output path: {alpha_r}")

    # require outputs to to have a suffix.
    if not out_r.suffix:
        raise RuntimeError(f"❌ Output path has no file extension (expected .tif/.tiff): {out_r}")
    if alpha_r is not None and not alpha_r.suffix:
        raise RuntimeError(f"❌ Alpha output path has no file extension: {alpha_r}")

    # Optional policy: same directory is fine, but prevent writing into INPUT directory
    # if  pipeline expects strict separation. (Commented out by default.)
    # if out_r.parent == src_r.parent:
    #     raise RuntimeError("❌ Refusing to write outputs into the input directory.")

    return out_r, alpha_r


def _block_window_total(ds, band_index: int = 1) -> Optional[int]:
    """Compute total number of block windows without materializing them."""
    try:
        bh, bw = ds.block_shapes[band_index - 1]  # (block_height, block_width)
        return math.ceil(ds.height / bh) * math.ceil(ds.width / bw)
    except Exception:
        return None


def _safe_unlink(path: Path) -> None:
    """Safely unlink a file; error if it's a directory."""
    if not path.exists():
        return
    if path.is_dir():
        raise RuntimeError(f"❌ Refusing to delete directory: {path}")
    path.unlink()


def _build_palette(rules: Sequence[ReclassRule]) -> Optional[Dict[int, Tuple[int, int, int]]]:
    """Build a GeoTIFF palette (colormap) dict if any rgb is provided.

    Args:
        rules: Reclass rules.

    Returns:
        Palette mapping: output_value -> (r,g,b), or None if no rgb entries.
    """
    if not any(r.rgb_hex for r in rules):
        return None

    palette: Dict[int, Tuple[int, int, int]] = {0: (0, 0, 0)}
    for r in rules:
        palette[r.value] = _parse_rgb_hex(r.rgb_hex) if r.rgb_hex else (0, 0, 0)
    return palette


def _reclassify_and_output(
        src_path: str, out_path: Path, alpha_path: Optional[Path], rules: Sequence["ReclassRule"],
        options: "ReclassOptions", palette: Optional[Dict[int, Tuple[int, int, int]]], ) -> None:
    """Windowed reclassification with fast LUT mapping + sparse alpha writing."""
    import rasterio
    from rasterio.enums import ColorInterp

    out_r, alpha_r = _guard_output_paths(
        src_path=src_path, out_path=out_path, alpha_path=alpha_path
    )

    out_r.parent.mkdir(parents=True, exist_ok=True)
    _safe_unlink(out_r)
    if alpha_r is not None:
        alpha_r.parent.mkdir(parents=True, exist_ok=True)
        _safe_unlink(alpha_r)

    unmapped_seen: set[int] = set()

    with rasterio.open(src_path) as src:
        src_nodata = src.nodata
        nodata_codes = set(options.input_nodata)
        if src_nodata is not None:
            nodata_codes.add(int(src_nodata))

        # Always include MIN_CODE as a safety baseline
        nodata_codes.add(MIN_CODE)

        mapper = _build_lut_mapper(
            rules, default_value=options.default_value, alpha_on=options.alpha_on,
            alpha_off=options.alpha_off, input_nodata=tuple(sorted(nodata_codes)), )
        src_profile = src.profile.copy()

        idx_profile = _prepare_gtiff_profile(
            src_profile, nodata=options.nodata_value, tile_size=options.tile_size,
            compress=options.compress, )

        alpha_creation_opts: Dict[str, str] = {}
        if alpha_r is not None and options.alpha_sparse_ok:
            # ✅ Huge for sparse masks: avoids writing all-zero tiles.
            alpha_creation_opts["SPARSE_OK"] = "YES"

        alpha_profile = _prepare_gtiff_profile(
            src_profile, nodata=options.alpha_off, tile_size=options.tile_size,
            compress=options.compress,
            creation_options=alpha_creation_opts if alpha_creation_opts else None, )

        # Preallocate buffers at max tile size; use views for edge tiles.
        ts = int(options.tile_size)
        idx_buf = np.empty((ts, ts), dtype=np.uint8)
        alpha_buf = np.empty((ts, ts), dtype=np.uint8) if alpha_r is not None else None
        shifted_buf = np.empty((ts, ts), dtype=np.int32)

        with rasterio.open(out_r, "w", **idx_profile) as dst_idx:
            dst_idx.colorinterp = (ColorInterp.palette,) if palette is not None else (
                ColorInterp.gray,)
            if palette is not None:
                dst_idx.write_colormap(DST_BAND_INDEX, palette)

            dst_alpha = None
            if alpha_r is not None:
                dst_alpha = rasterio.open(alpha_r, "w", **alpha_profile)
                dst_alpha.colorinterp = (ColorInterp.alpha,)

            try:
                win_iter = (w for _, w in dst_idx.block_windows(DST_BAND_INDEX))
                total = _block_window_total(dst_idx, band_index=DST_BAND_INDEX)

                for window in tqdm(
                        win_iter, total=total, unit="block", desc="Reclassifying", mininterval=10.0
                ):
                    h = int(window.height)
                    w = int(window.width)

                    # Read source block (2D)
                    data = src.read(SRC_BAND_INDEX, window=window)

                    idx_view = idx_buf[:h, :w]
                    shifted_view = shifted_buf[:h, :w]
                    alpha_view = alpha_buf[:h, :w] if alpha_buf is not None else None

                    mapper.map_block_into(
                        data, idx_out=idx_view, alpha_out=alpha_view, shifted_tmp=shifted_view, )

                    dst_idx.write(idx_view, window=window, indexes=DST_BAND_INDEX)
                    if dst_alpha is not None and alpha_view is not None:
                        dst_alpha.write(alpha_view, window=window, indexes=DST_BAND_INDEX)

            finally:
                if dst_alpha is not None:
                    dst_alpha.close()

    if options.report_unmapped and unmapped_seen:
        vals = sorted(unmapped_seen)
        print(f"*️⃣ Unmapped ids (sample up to {options.max_unmapped_ids}): {vals}")
