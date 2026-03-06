import pytest
import argparse
import os
from pathlib import Path

# Import your command class
from GDALHelper.commands import MaskedBlend

# --- Test Data ---
# Format: (Test Name, List of "--co" flags, Should it Succeed?)
MASKED_BLEND_SCENARIOS = [
    # 1. Happy Path: No options (Defaults to Deflate in your code)
    ("Default", [], True),

    # 2. Valid: WebP High Quality
    ("WebP_Valid", ["COMPRESS=WEBP", "WEBP_QUALITY=90"], True),

    # 3. Valid: JPEG (Explicit)
    ("JPEG_Valid", ["COMPRESS=JPEG", "JPEG_QUALITY=75"], True),

    # 4. Valid: ZSTD (Lossless)
    ("ZSTD_Valid", ["COMPRESS=ZSTD"], True),

    # 5. REGRESSION TEST: WebP + YCBCR Conflict
    # This should FAIL because WebP doesn't support YCBCR photometric interpretation
    ("Conflict_WebP_YCBCR", ["COMPRESS=WEBP", "PHOTOMETRIC=YCBCR"], False),

    # 6. Valid Failure: Predictor Mismatch (Hard Error)
    # Predictor 3 requires Float data, but input is Byte (uint8). This causes a crash.
    ("Invalid_Predictor", ["COMPRESS=LZW", "PREDICTOR=3"], False),
]

@pytest.mark.parametrize("name, co_flags, should_succeed", MASKED_BLEND_SCENARIOS)
def test_masked_blend_options(tiny_rasters, tmp_path, name, co_flags, should_succeed):
    # 1. Setup Input/Output Paths
    output_path = tmp_path / f"output_{name}.tif"

    # 2. Construct CLI
    arg_list = [
        tiny_rasters["A"],
        tiny_rasters["B"],
        tiny_rasters["Mask"],
        str(output_path)
    ]
    for flag in co_flags:
        arg_list.extend(["--co", flag])

    # 3. Initialize
    parser = argparse.ArgumentParser()
    MaskedBlend.add_arguments(parser)
    args = parser.parse_args(arg_list)
    command = MaskedBlend(args)

    # 4. Execute and Verify
    try:
        command.execute()

        # --- SUCCESS BLOCK ---
        if should_succeed:
            assert output_path.exists(), "Output file was not created"
        else:
            pytest.fail(f"Test '{name}' succeeded but was expected to FAIL.")

    except Exception as e:
        # --- FAILURE BLOCK ---
        if should_succeed:
            pytest.fail(f"Test '{name}' failed unexpectedly: {e}")
        else:
            # We successfully caught the expected error.
            # We do NOT check output_path.exists() because GDAL often leaves
            # a 0-byte zombie file behind when driver options fail.
            pass