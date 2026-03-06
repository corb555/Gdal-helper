from __future__ import annotations

from pathlib import Path
from typing import Iterable

from GDALHelper.qml_palette import DEFAULT_LUT_SIZE, load_qml_palette
import numpy as np



EXPECTED = {
    0: ("#000000", 0),
    1: ("#574d42", 255),
    2: ("#625e5a", 255),
    3: ("#e3dbca", 255),
    4: ("#3a404a", 255),
    5: ("#edeff0", 255),
    6: ("#e3dbca", 255),
}


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def main() -> None:
    qml_path = Path("Rainier_EVT_themed.qml")

    pal = load_qml_palette(qml_path)

    # Build LUTs (this is what Step 3 will use)
    lut_rgb = pal.build_lut_rgb(size=DEFAULT_LUT_SIZE)
    lut_rgba = pal.build_lut_rgba(size=DEFAULT_LUT_SIZE)

    # Summary
    keys = sorted(pal.colors.keys())
    print("")
    print("✅ Parsed QML palette")
    print(f"   File: {qml_path}")
    print(f"   Entries: {len(keys)}")
    print(f"   Min/Max value: {keys[0]} .. {keys[-1]}")
    print(f"   LUT RGB shape/dtype: {lut_rgb.shape} {lut_rgb.dtype}")
    print(f"   LUT RGBA shape/dtype: {lut_rgba.shape} {lut_rgba.dtype}")

    # Quick spot-checks
    print("")
    print("🔎 Spot-check (value -> color, alpha):")
    failures: list[str] = []

    for v, (hex_expected, alpha_expected) in EXPECTED.items():
        rgb = tuple(int(x) for x in lut_rgb[v])
        rgba = tuple(int(x) for x in lut_rgba[v])
        hx = _rgb_to_hex(rgb)
        a = rgba[3]

        ok = (hx.lower() == hex_expected.lower()) and (a == alpha_expected)
        mark = "✅" if ok else "❌"
        print(f"   {mark} {v:3d}: {hx}  alpha={a}")

        if not ok:
            failures.append(
                f"value={v}: expected {hex_expected}/a={alpha_expected}, got {hx}/a={a}"
            )

    # Confirm most entries are black (as in your saved style)
    black_count = int(np.sum(np.all(lut_rgb == np.array([0, 0, 0], dtype=np.uint8), axis=1)))
    print("")
    print(f"ℹ️  LUT black entries: {black_count}/{DEFAULT_LUT_SIZE}")

    if failures:
        print("")
        print("❌ Failures:")
        for f in failures:
            print(f"   - {f}")
        raise SystemExit(2)

    print("")
    print("✅ QML palette read test passed.")


if __name__ == "__main__":
    main()
