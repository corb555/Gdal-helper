from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Sequence

from GDALHelper.utils import ConfigurationError, FileError


@dataclass(frozen=True)
class ValidationFinding:
    level: str
    message: str


@dataclass(frozen=True)
class RasterSourceInfo:
    path: Path
    bounds: tuple[float, float, float, float]
    crs: str
    intersects_target: bool | None = None


@dataclass(frozen=True)
class CoverageInfo:
    total_pixels: int
    valid_pixels: int
    invalid_pixels: int
    valid_percent: float
    invalid_percent: float
    missing_bounds: tuple[float, float, float, float] | None
    touches_left: bool
    touches_right: bool
    touches_top: bool
    touches_bottom: bool


@dataclass(frozen=True)
class ValidationResult:
    findings: tuple[ValidationFinding, ...]
    sources: tuple[RasterSourceInfo, ...] = ()
    coverage: CoverageInfo | None = None

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.level == "warning")

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.level == "error")

    @property
    def ok(self) -> bool:
        return not self.errors and not self.warnings


def _require_rasterio():
    try:
        import rasterio
        from rasterio.warp import transform_bounds
    except ImportError as exc:
        raise ConfigurationError(
            "Raster validation requires rasterio."
        ) from exc

    return rasterio, transform_bounds


def _validate_extent(
    extent: Sequence[float],
) -> tuple[float, float, float, float]:
    if len(extent) != 4:
        raise ConfigurationError("Target extent must contain exactly four values.")

    left, bottom, right, top = (float(v) for v in extent)

    if left >= right or bottom >= top:
        raise ConfigurationError(
            f"Invalid target extent: {(left, bottom, right, top)}"
        )

    return left, bottom, right, top


def _rect_intersection(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    left = max(a[0], b[0])
    bottom = max(a[1], b[1])
    right = min(a[2], b[2])
    top = min(a[3], b[3])

    if left >= right or bottom >= top:
        return None

    return left, bottom, right, top


def _rect_union_area(
    rects: Sequence[tuple[float, float, float, float]],
) -> float:
    if not rects:
        return 0.0

    xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
    area = 0.0

    for x0, x1 in zip(xs, xs[1:]):
        if x0 >= x1:
            continue

        intervals: list[tuple[float, float]] = []
        for left, bottom, right, top in rects:
            if left < x1 and right > x0:
                intervals.append((bottom, top))

        if not intervals:
            continue

        intervals.sort()
        merged_start, merged_end = intervals[0]
        y_coverage = 0.0

        for start, end in intervals[1:]:
            if start <= merged_end:
                merged_end = max(merged_end, end)
            else:
                y_coverage += merged_end - merged_start
                merged_start, merged_end = start, end

        y_coverage += merged_end - merged_start
        area += (x1 - x0) * y_coverage

    return area


def _uncovered_rects(
    target: tuple[float, float, float, float],
    covered_rects: Sequence[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Return meaningful rectangular gaps inside target not covered by any source."""
    left, bottom, right, top = target
    linear_tolerance = max(right - left, top - bottom) * 1e-9
    area_tolerance = (right - left) * (top - bottom) * 1e-10

    xs = sorted(
        {left, right}
        | {max(left, min(right, r[0])) for r in covered_rects}
        | {max(left, min(right, r[2])) for r in covered_rects}
    )
    ys = sorted(
        {bottom, top}
        | {max(bottom, min(top, r[1])) for r in covered_rects}
        | {max(bottom, min(top, r[3])) for r in covered_rects}
    )

    row_runs: list[tuple[float, float, float, float]] = []
    for y0, y1 in zip(ys, ys[1:]):
        if y1 - y0 <= linear_tolerance:
            continue

        run_start = None
        for x0, x1 in zip(xs, xs[1:]):
            if x1 - x0 <= linear_tolerance:
                continue

            mid_x = (x0 + x1) / 2.0
            mid_y = (y0 + y1) / 2.0
            covered = any(
                r[0] <= mid_x <= r[2] and r[1] <= mid_y <= r[3]
                for r in covered_rects
            )

            if not covered:
                if run_start is None:
                    run_start = x0
            elif run_start is not None:
                row_runs.append((run_start, y0, x0, y1))
                run_start = None

        if run_start is not None:
            row_runs.append((run_start, y0, right, y1))

    merged: list[list[float]] = []
    for run in sorted(row_runs, key=lambda r: (r[0], r[2], r[1], r[3])):
        x0, y0, x1, y1 = run
        if (
            merged
            and abs(merged[-1][0] - x0) <= linear_tolerance
            and abs(merged[-1][2] - x1) <= linear_tolerance
            and abs(merged[-1][3] - y0) <= linear_tolerance
        ):
            merged[-1][3] = y1
        else:
            merged.append([x0, y0, x1, y1])

    gaps = []
    for x0, y0, x1, y1 in merged:
        width = x1 - x0
        height = y1 - y0
        area = width * height
        if (
            width <= linear_tolerance
            or height <= linear_tolerance
            or area <= area_tolerance
        ):
            continue
        gaps.append((x0, y0, x1, y1))

    return gaps


def _missing_region_name(
    rect: tuple[float, float, float, float],
    target: tuple[float, float, float, float],
    tolerance: float,
) -> str:
    """Describe a missing rectangle using compass-oriented terminology."""
    touches_west = abs(rect[0] - target[0]) <= tolerance
    touches_south = abs(rect[1] - target[1]) <= tolerance
    touches_east = abs(rect[2] - target[2]) <= tolerance
    touches_north = abs(rect[3] - target[3]) <= tolerance

    vertical = ""
    horizontal = ""

    if touches_north and not touches_south:
        vertical = "North"
    elif touches_south and not touches_north:
        vertical = "South"

    if touches_east and not touches_west:
        horizontal = "East"
    elif touches_west and not touches_east:
        horizontal = "West"

    if vertical and horizontal:
        return f"{vertical}{horizontal.lower()}"
    if vertical:
        return vertical
    if horizontal:
        return horizontal

    target_mid_x = (target[0] + target[2]) / 2.0
    target_mid_y = (target[1] + target[3]) / 2.0
    rect_mid_x = (rect[0] + rect[2]) / 2.0
    rect_mid_y = (rect[1] + rect[3]) / 2.0

    vertical = "North" if rect_mid_y >= target_mid_y else "South"
    horizontal = "East" if rect_mid_x >= target_mid_x else "West"
    return f"Interior {vertical}{horizontal.lower()}"


def _adjacent_sources(
    rect: tuple[float, float, float, float],
    sources: Sequence[RasterSourceInfo],
    tolerance: float,
) -> list[tuple[str, RasterSourceInfo]]:
    """Find contributing rasters immediately adjacent to a missing rectangle."""
    left, bottom, right, top = rect
    candidates: list[tuple[str, float, RasterSourceInfo]] = []

    for source in sources:
        if not source.intersects_target:
            continue

        s_left, s_bottom, s_right, s_top = source.bounds
        overlaps_x = min(right, s_right) > max(left, s_left)
        overlaps_y = min(top, s_top) > max(bottom, s_bottom)

        if overlaps_x and s_bottom >= top - tolerance:
            candidates.append(("North", max(0.0, s_bottom - top), source))
        if overlaps_x and s_top <= bottom + tolerance:
            candidates.append(("South", max(0.0, bottom - s_top), source))
        if overlaps_y and s_left >= right - tolerance:
            candidates.append(("East", max(0.0, s_left - right), source))
        if overlaps_y and s_right <= left + tolerance:
            candidates.append(("West", max(0.0, left - s_right), source))

    adjacent: list[tuple[str, RasterSourceInfo]] = []
    for direction in ("North", "South", "East", "West"):
        matches = [item for item in candidates if item[0] == direction]
        if matches:
            _, _, source = min(matches, key=lambda item: item[1])
            adjacent.append((direction, source))

    return adjacent


_GMTED2010_RE = re.compile(
    r"^(?P<lat>\d{2})(?P<ns>[ns])(?P<lon>\d{3})(?P<ew>[ew])"
    r"_(?P<date>\d{8})_gmted_(?P<product>[a-z]+)(?P<resolution>\d{3})\.tif$",
    re.IGNORECASE,
)

_USGS_13_RE = re.compile(
    r"^USGS_13_(?P<ns>[ns])(?P<lat>\d{2})(?P<ew>[ew])(?P<lon>\d{3})"
    r"_(?P<date>\d{8})\.tif$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DemTileName:
    family: str
    path: Path
    latitude: int
    longitude: int
    ns: str
    ew: str
    suffix: str


def _parse_dem_tile_name(path: Path) -> DemTileName | None:
    """Recognize supported USGS DEM file families."""
    name = path.name

    match = _GMTED2010_RE.match(name)
    if match:
        return DemTileName(
            family="USGS GMTED2010",
            path=path,
            latitude=int(match.group("lat")),
            longitude=int(match.group("lon")),
            ns=match.group("ns").lower(),
            ew=match.group("ew").lower(),
            suffix=(
                f"_{match.group('date')}_gmted_"
                f"{match.group('product')}{match.group('resolution')}.tif"
            ),
        )

    match = _USGS_13_RE.match(name)
    if match:
        return DemTileName(
            family="USGS_13",
            path=path,
            latitude=int(match.group("lat")),
            longitude=int(match.group("lon")),
            ns=match.group("ns").lower(),
            ew=match.group("ew").lower(),
            suffix=f"_{match.group('date')}.tif",
        )

    return None


def _detect_dem_family(paths: Sequence[Path]) -> str:
    """Return GMTED2010, USGS_13, or unrecognized for the supplied DEM set."""
    parsed = [_parse_dem_tile_name(path) for path in paths]
    families = {item.family for item in parsed if item is not None}

    if len(parsed) == 0 or any(item is None for item in parsed):
        return "unrecognized"
    if len(families) != 1:
        return "unrecognized"

    return next(iter(families))


def _signed_coord(value: int, hemisphere: str) -> int:
    return -value if hemisphere.lower() in ("s", "w") else value


def _coord_parts(value: int, positive: str, negative: str, width: int) -> tuple[int, str]:
    hemisphere = positive if value >= 0 else negative
    return abs(value), hemisphere


def _format_dem_tile_name(
    family: str,
    latitude: int,
    longitude: int,
    suffix: str,
) -> str | None:
    lat, ns = _coord_parts(latitude, "n", "s", 2)
    lon, ew = _coord_parts(longitude, "e", "w", 3)

    if family == "GMTED2010":
        return f"{lat:02d}{ns}{lon:03d}{ew}{suffix}"
    if family == "USGS_13":
        return f"USGS_13_{ns}{lat:02d}{ew}{lon:03d}{suffix}"
    return None


def _infer_missing_tile_name(
    family: str,
    adjacent: Sequence[tuple[str, RasterSourceInfo]],
) -> str | None:
    """Infer a likely missing filename from recognized adjacent DEM tiles."""
    if family == "unrecognized" or not adjacent:
        return None

    parsed_by_direction = {}
    for direction, source in adjacent:
        parsed = _parse_dem_tile_name(source.path)
        if parsed is None or parsed.family != family:
            return None
        parsed_by_direction[direction] = parsed

    suffixes = {item.suffix for item in parsed_by_direction.values()}
    if len(suffixes) != 1:
        return None
    suffix = next(iter(suffixes))

    latitude = None
    longitude = None

    # East/West neighbors share the missing tile's latitude band.
    for direction in ("East", "West"):
        item = parsed_by_direction.get(direction)
        if item is not None:
            latitude = _signed_coord(item.latitude, item.ns)
            break

    # North/South neighbors share the missing tile's longitude band.
    for direction in ("North", "South"):
        item = parsed_by_direction.get(direction)
        if item is not None:
            longitude = _signed_coord(item.longitude, item.ew)
            break

    # If only one axis is directly available, derive the other from the
    # recognized family's regular tile spacing.
    lat_step = 20 if family == "GMTED2010" else 1
    lon_step = 30 if family == "GMTED2010" else 1

    if latitude is None:
        north = parsed_by_direction.get("North")
        south = parsed_by_direction.get("South")
        if north is not None:
            latitude = _signed_coord(north.latitude, north.ns) - lat_step
        elif south is not None:
            latitude = _signed_coord(south.latitude, south.ns) + lat_step

    if longitude is None:
        east = parsed_by_direction.get("East")
        west = parsed_by_direction.get("West")
        if east is not None:
            longitude = _signed_coord(east.longitude, east.ew) - lon_step
        elif west is not None:
            longitude = _signed_coord(west.longitude, west.ew) + lon_step

    if latitude is None or longitude is None:
        return None

    return _format_dem_tile_name(family, latitude, longitude, suffix)


def validate_source_coverage(
    inputs: Iterable[Path | str],
    *,
    target_extent: Sequence[float] | None = None,
    target_srs: str | None = None,
) -> ValidationResult:
    """
    Inspect source rasters before a warp.

    If target_extent is supplied, source bounds are transformed into target_srs
    (or the first source CRS when target_srs is omitted), then checked for
    intersection and aggregate extent coverage.
    """
    rasterio, transform_bounds = _require_rasterio()

    paths = tuple(Path(p) for p in inputs)
    if not paths:
        raise ConfigurationError("At least one source raster is required.")

    dem_family = _detect_dem_family(paths)
    target = _validate_extent(target_extent) if target_extent is not None else None

    raw_sources: list[tuple[Path, object, tuple[float, float, float, float]]] = []
    reference_crs = None

    for path in paths:
        if not path.exists():
            raise FileError(f"Source raster not found: '{path}'")

        try:
            with rasterio.open(path) as src:
                if src.width <= 0 or src.height <= 0:
                    raise ConfigurationError(
                        f"Source raster has invalid dimensions: '{path}'"
                    )
                if src.crs is None:
                    raise ConfigurationError(
                        f"Source raster has no CRS: '{path}'"
                    )

                bounds = (
                    float(src.bounds.left),
                    float(src.bounds.bottom),
                    float(src.bounds.right),
                    float(src.bounds.top),
                )
                crs = src.crs
        except (ConfigurationError, FileError):
            raise
        except Exception as exc:
            raise FileError(f"Could not open source raster '{path}': {exc}") from exc

        if reference_crs is None:
            reference_crs = crs

        raw_sources.append((path, crs, bounds))

    comparison_crs = target_srs or reference_crs
    findings: list[ValidationFinding] = []
    source_infos: list[RasterSourceInfo] = []
    clipped_rects: list[tuple[float, float, float, float]] = []

    for path, source_crs, source_bounds in raw_sources:
        bounds = source_bounds

        if comparison_crs is not None and source_crs != comparison_crs:
            try:
                bounds = tuple(
                    float(v)
                    for v in transform_bounds(
                        source_crs,
                        comparison_crs,
                        *source_bounds,
                        densify_pts=21,
                    )
                )
            except Exception as exc:
                raise ConfigurationError(
                    f"Could not transform bounds for '{path}' into "
                    f"'{comparison_crs}': {exc}"
                ) from exc

        intersects = None
        if target is not None:
            clipped = _rect_intersection(bounds, target)
            intersects = clipped is not None

            if clipped is None:
                findings.append(
                    ValidationFinding(
                        "warning",
                        f"DEM source is completely outside target extent and contributes no coverage: '{path}'",
                    )
                )
            else:
                clipped_rects.append(clipped)

        source_infos.append(
            RasterSourceInfo(
                path=path,
                bounds=bounds,
                crs=str(comparison_crs),
                intersects_target=intersects,
            )
        )

    if target is not None:
        target_area = (target[2] - target[0]) * (target[3] - target[1])
        covered_area = _rect_union_area(clipped_rects)
        area_tolerance = target_area * 1e-9
        linear_tolerance = max(target[2] - target[0], target[3] - target[1]) * 1e-9

        if covered_area + area_tolerance < target_area:
            missing_percent = 100.0 * (target_area - covered_area) / target_area

            if clipped_rects:
                coverage_bounds = (
                    min(r[0] for r in clipped_rects),
                    min(r[1] for r in clipped_rects),
                    max(r[2] for r in clipped_rects),
                    max(r[3] for r in clipped_rects),
                )
            else:
                coverage_bounds = None

            uncovered_directions = []
            if coverage_bounds is None:
                uncovered_directions = ["West", "South", "East", "North"]
            else:
                if coverage_bounds[0] > target[0] + linear_tolerance:
                    uncovered_directions.append("West")
                if coverage_bounds[1] > target[1] + linear_tolerance:
                    uncovered_directions.append("South")
                if coverage_bounds[2] < target[2] - linear_tolerance:
                    uncovered_directions.append("East")
                if coverage_bounds[3] < target[3] - linear_tolerance:
                    uncovered_directions.append("North")

            detail_lines = [
                f"Source DEM rasters only cover {100 - missing_percent:.1f}% of target extent.",
                f"Target extent ({comparison_crs}, GDAL -te order = W S E N): \n"
                f"   W={target[0]:.3f}  S={target[1]:.3f}  "
                f"E={target[2]:.3f}  N={target[3]:.3f}",
            ]

            if coverage_bounds is not None:
                detail_lines.append(
                    "Combined source coverage:\n"
                    f"   W={coverage_bounds[0]:.3f}  S={coverage_bounds[1]:.3f}  "
                    f"E={coverage_bounds[2]:.3f}  N={coverage_bounds[3]:.3f}"
                )

            if uncovered_directions:
                detail_lines.append(
                    "Missing coverage reaches target edge(s): "
                    + ", ".join(uncovered_directions)
                )

                gap_lines = []
                if "West" in uncovered_directions and coverage_bounds is not None:
                    gap_lines.append(
                        f"  West: target starts at W={target[0]:.3f}, "
                        f"but source coverage starts at W={coverage_bounds[0]:.3f} "
                        f"(gap {coverage_bounds[0] - target[0]:.1f} map units)."
                    )
                if "South" in uncovered_directions and coverage_bounds is not None:
                    gap_lines.append(
                        f"  South: target starts at S={target[1]:.3f}, "
                        f"but source coverage starts at S={coverage_bounds[1]:.3f} "
                        f"(gap {coverage_bounds[1] - target[1]:.1f} map units)."
                    )
                if "East" in uncovered_directions and coverage_bounds is not None:
                    gap_lines.append(
                        f"  East: target ends at E={target[2]:.3f}, "
                        f"but source coverage ends at E={coverage_bounds[2]:.3f} "
                        f"(gap {target[2] - coverage_bounds[2]:.1f} map units)."
                    )
                if "North" in uncovered_directions and coverage_bounds is not None:
                    gap_lines.append(
                        f"  North: target ends at N={target[3]:.3f}, "
                        f"but source coverage ends at N={coverage_bounds[3]:.3f} "
                        f"(gap {target[3] - coverage_bounds[3]:.1f} map units)."
                    )

                if gap_lines:
                    detail_lines.append("Coverage shortfall:")
                    detail_lines.extend(gap_lines)

                intersecting_sources = [
                    source for source in source_infos if source.intersects_target
                ]
                if intersecting_sources:
                    detail_lines.append("Likely place to look:")
                    if "West" in uncovered_directions:
                        source = min(intersecting_sources, key=lambda s: s.bounds[0])
                        detail_lines.append(
                            f"  West of '{source.path.name}' "
                            f"(westernmost contributing source)."
                        )
                    if "South" in uncovered_directions:
                        source = min(intersecting_sources, key=lambda s: s.bounds[1])
                        detail_lines.append(
                            f"  South of '{source.path.name}' "
                            f"(southernmost contributing source)."
                        )
                    if "East" in uncovered_directions:
                        source = max(intersecting_sources, key=lambda s: s.bounds[2])
                        detail_lines.append(
                            f"  East of '{source.path.name}' "
                            f"(easternmost contributing source)."
                        )
                    if "North" in uncovered_directions:
                        source = max(intersecting_sources, key=lambda s: s.bounds[3])
                        detail_lines.append(
                            f"  North of '{source.path.name}' "
                            f"(northernmost contributing source)."
                        )
                    detail_lines.append(
                        "  Add the adjacent DEM tile(s) in the indicated direction, "
                        "or reduce the target extent."
                    )
            else:
                missing_rects = _uncovered_rects(target, clipped_rects)

                detail_lines.append(
                    "The overall source bounding box reaches West, South, East, and North, "
                    "but that does not mean every point inside the box is covered. "
                    "The source mosaic contains a real gap."
                )

                if missing_rects:
                    detail_lines.append(
                        f"Detected {len(missing_rects)} uncovered region"
                        f"{'s' if len(missing_rects) != 1 else ''}:"
                    )

                    intersecting_sources = [
                        source for source in source_infos if source.intersects_target
                    ]

                    for index, missing_rect in enumerate(missing_rects, start=1):
                        region_name = _missing_region_name(
                            missing_rect, target, linear_tolerance
                        )
                        missing_area = (
                            (missing_rect[2] - missing_rect[0])
                            * (missing_rect[3] - missing_rect[1])
                        )
                        region_percent = 100.0 * missing_area / target_area

                        detail_lines.append(
                            f"  {index}. {region_name} gap "
                            f"({region_percent:.2f}% of target):"
                        )
                        detail_lines.append(
                            f"     W={missing_rect[0]:.3f}  "
                            f"S={missing_rect[1]:.3f}  "
                            f"E={missing_rect[2]:.3f}  "
                            f"N={missing_rect[3]:.3f}"
                        )

                        adjacent = _adjacent_sources(
                            missing_rect, intersecting_sources, linear_tolerance
                        )
                        if adjacent:
                            detail_lines.append("     Adjacent contributing sources:")
                            for direction, source in adjacent:
                                detail_lines.append(
                                    f"       {direction}: {source.path.name}"
                                )

                        likely_tile = _infer_missing_tile_name(dem_family, adjacent)
                        if likely_tile:
                            detail_lines.append(
                                f"     Likely missing {dem_family} tile: {likely_tile}"
                            )

                        detail_lines.append(
                            "     Fix: add the DEM tile covering this region "
                            "or reduce the target extent to exclude it."
                        )
                else:
                    detail_lines.append(
                        "The uncovered region could not be isolated from source bounds. "
                        "Inspect the source mosaic for internal gaps or irregular coverage."
                    )

            detail_lines.append("Contributing source rasters:")
            for source in source_infos:
                if source.intersects_target:
                    left, bottom, right, top = source.bounds
                    detail_lines.append(
                        f"  - {source.path}: "
                        f"W={left:.3f}  S={bottom:.3f}  "
                        f"E={right:.3f}  N={top:.3f}"
                    )

            findings.append(
                ValidationFinding(
                    "warning",
                    "\n".join(detail_lines),
                )
            )

    return ValidationResult(
        findings=tuple(findings),
        sources=tuple(source_infos),
    )


def _coverage_from_mask(dataset) -> CoverageInfo:
    import numpy as np
    from rasterio.windows import bounds as window_bounds

    total_pixels = int(dataset.width) * int(dataset.height)
    valid_pixels = 0

    min_col = dataset.width
    min_row = dataset.height
    max_col = -1
    max_row = -1

    touches_left = False
    touches_right = False
    touches_top = False
    touches_bottom = False

    for _, window in dataset.block_windows(1):
        mask = dataset.dataset_mask(window=window)
        invalid = mask == 0
        invalid_count = int(np.count_nonzero(invalid))
        valid_pixels += int(mask.size) - invalid_count

        if invalid_count == 0:
            continue

        rows, cols = np.nonzero(invalid)

        global_min_col = int(window.col_off) + int(cols.min())
        global_max_col = int(window.col_off) + int(cols.max())
        global_min_row = int(window.row_off) + int(rows.min())
        global_max_row = int(window.row_off) + int(rows.max())

        min_col = min(min_col, global_min_col)
        max_col = max(max_col, global_max_col)
        min_row = min(min_row, global_min_row)
        max_row = max(max_row, global_max_row)

        touches_left = touches_left or global_min_col == 0
        touches_right = touches_right or global_max_col == dataset.width - 1
        touches_top = touches_top or global_min_row == 0
        touches_bottom = touches_bottom or global_max_row == dataset.height - 1

    invalid_pixels = total_pixels - valid_pixels

    if total_pixels:
        valid_percent = 100.0 * valid_pixels / total_pixels
    else:
        valid_percent = 0.0

    invalid_percent = 100.0 - valid_percent

    missing_bounds = None
    if invalid_pixels:
        # Build a pixel window around every observed invalid pixel, then convert
        # that window to geospatial bounds.
        from rasterio.windows import Window

        missing_window = Window(
            col_off=min_col,
            row_off=min_row,
            width=max_col - min_col + 1,
            height=max_row - min_row + 1,
        )
        left, bottom, right, top = window_bounds(
            missing_window,
            dataset.transform,
        )
        missing_bounds = (
            float(left),
            float(bottom),
            float(right),
            float(top),
        )

    return CoverageInfo(
        total_pixels=total_pixels,
        valid_pixels=valid_pixels,
        invalid_pixels=invalid_pixels,
        valid_percent=valid_percent,
        invalid_percent=invalid_percent,
        missing_bounds=missing_bounds,
        touches_left=touches_left,
        touches_right=touches_right,
        touches_top=touches_top,
        touches_bottom=touches_bottom,
    )


def validate_raster(
    path: Path | str,
    *,
    min_bytes: int | None = None,
    min_pixels: int | None = None,
    min_coverage: float | None = None,
) -> ValidationResult:
    """
    Validate one raster structurally and, when requested, by valid-data coverage.

    Threshold failures are returned as warnings. Problems that prevent the raster
    from being inspected are raised as errors.
    """
    rasterio, _ = _require_rasterio()

    raster_path = Path(path)
    if not raster_path.exists():
        raise FileError(f"Validation failed: file not found: '{raster_path}'")

    if min_bytes is not None and min_bytes < 0:
        raise ConfigurationError("--min-bytes cannot be negative.")
    if min_pixels is not None and min_pixels < 0:
        raise ConfigurationError("--min-pixels cannot be negative.")
    if min_coverage is not None and not 0.0 <= min_coverage <= 100.0:
        raise ConfigurationError("--min-coverage must be between 0 and 100.")

    findings: list[ValidationFinding] = []

    if min_bytes is not None:
        size = raster_path.stat().st_size
        if size < min_bytes:
            findings.append(
                ValidationFinding(
                    "warning",
                    f"Raster file is smaller than required: {size} bytes "
                    f"(minimum {min_bytes}).",
                )
            )

    try:
        with rasterio.open(raster_path) as src:
            width = int(src.width)
            height = int(src.height)

            if width <= 0 or height <= 0:
                raise ConfigurationError(
                    f"Raster has invalid dimensions: {width}x{height}"
                )

            pixel_count = width * height
            if min_pixels is not None and pixel_count < min_pixels:
                findings.append(
                    ValidationFinding(
                        "warning",
                        f"Raster contains {pixel_count} pixels "
                        f"(minimum {min_pixels}).",
                    )
                )

            coverage = None
            if min_coverage is not None:
                coverage = _coverage_from_mask(src)

                if coverage.valid_percent < min_coverage:
                    boundary_names = []
                    if coverage.touches_left:
                        boundary_names.append("left")
                    if coverage.touches_right:
                        boundary_names.append("right")
                    if coverage.touches_top:
                        boundary_names.append("top")
                    if coverage.touches_bottom:
                        boundary_names.append("bottom")

                    detail = ""
                    if boundary_names:
                        detail = " Missing data reaches: " + ", ".join(boundary_names) + "."

                    findings.append(
                        ValidationFinding(
                            "warning",
                            f"DEM coverage is below the required {min_coverage:.2f}%.{detail}",
                        )
                    )
    except (ConfigurationError, FileError):
        raise
    except Exception as exc:
        raise FileError(
            f"Could not inspect raster '{raster_path}': {exc}"
        ) from exc

    return ValidationResult(
        findings=tuple(findings),
        coverage=coverage,
    )


def enforce_validation(
    result: ValidationResult,
    *,
    ignore_warnings: bool = False,
) -> None:
    """
    Apply command/pipeline policy to a ValidationResult.

    The validation functions report quality findings. This function turns those
    findings into failure when warnings are not explicitly ignored.
    """
    if result.errors:
        raise ConfigurationError("Validation failed.")

    if result.warnings and not ignore_warnings:
        raise ConfigurationError("Validation failed.")


def format_validation_result(
    result: ValidationResult,
) -> list[str]:
    """Return concise human-readable diagnostics for CLI/orchestrator output."""
    lines: list[str] = []

    for finding in result.findings:
        prefix = "⚠️" if finding.level == "warning" else "❌"
        lines.append(f"{prefix} {finding.message}")

    if result.coverage is not None:
        coverage = result.coverage
        lines.extend(
            [
                f"Valid coverage: {coverage.valid_percent:.2f}%",
                f"Missing coverage: {coverage.invalid_percent:.2f}%",
            ]
        )

        if coverage.missing_bounds is not None:
            left, bottom, right, top = coverage.missing_bounds
            lines.append(
                "Missing-data bounds: "
                f"{left:.3f} {bottom:.3f} {right:.3f} {top:.3f}"
            )

    return lines
