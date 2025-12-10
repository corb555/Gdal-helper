import pytest

from GDALHelper.color_ramp_hsv import adjust_hsv


# Helper to convert from degrees (0-360) to the function's normalized format (0-1)
# This makes the test cases much more intuitive to read and write.
def h(degrees: float) -> float:
    return degrees / 360.0


# Define the test cases for the adjust_hsv function
# Each tuple is a complete test case:
# (test_id, h, s, v, sat_mult, sh_adj, mid_adj, hi_adj, min_hue, max_hue, target_hue, expected_hsv)
ADJUST_HSV_TEST_CASES = [("no_change", h(120), 0.8, 0.7,  # A bright green color
                          1.0, 0.0, 0.0, 0.0, 0, 0, 0,  # Neutral adjustments
                          (h(120), 0.8, 0.7)  # Expected: No change
                          ), ("boost_midtones", h(240), 0.9, 0.5,  # A mid-bright blue
                              1.0, 0.0, 0.2, 0.0, 0, 0, 0,  # Boost midtones by 0.2
                              (h(240), 0.9, 0.7)  # Expected: Only brightness (v) increases
                              ), ("crush_shadows", h(20), 0.7, 0.1,  # A dark orange
                                  1.0, -0.1, 0.0, 0.0, 0, 0, 0,  # Crush shadows by 0.1
                                  # Expected: brightness is reduced. Math: v=0.1 ->
                                  # shadow_weight=0.8. Change=-0.1*0.8=-0.08.
                                  # New v=0.02
                                  (h(20), 0.7, 0.02)),
                         ("increase_saturation", h(300), 0.5, 0.5,  # A mid-saturated magenta
                          1.5, 0.0, 0.0, 0.0, 0, 0, 0,  # Increase saturation by 50%
                          # Expected: saturation increases. Math: fade=1. s_change=0.5*(
                          # 1.5-1)=0.25. New s=0.75
                          (h(300), 0.75, 0.5)),
                         ("no_saturation_on_grey", h(0), 0.0, 0.5,  # A pure grey
                          2.0, 0.0, 0.0, 0.0, 0, 0, 0,  # Try to double saturation
                          (h(0), 0.0, 0.5)  # Expected: No change, saturation must remain 0
                          ), ("simple_hue_shift_in_range", h(100), 0.8, 0.5,  # A saturated green
                              1.0, 0.0, 0.0, 0.0, 90, 150, 120,
                              # Range: green->cyan, Target: a bluer green
                              # Expected: hue shifts towards target. Math: fade=1. h=0.277, th=0.333. diff=0.056. new_h=0.333
                              (h(120), 0.8, 0.5)),
                         ("no_hue_shift_outside_range", h(30), 0.8, 0.5,  # An orange color
                          1.0, 0.0, 0.0, 0.0, 90, 150, 120,  # Range: green->cyan
                          (h(30), 0.8, 0.5)  # Expected: No hue change
                          ), ("no_color_shift_on_white", h(120), 0.8, 1.0,
                              # A pure white, but hue is in range
                              2.0, 0.0, 0.0, 0.0, 90, 150, 120,  # Try to shift hue and saturation
                              (h(120), 0.8, 1.0)  # Expected: No change due to fade_factor being 0
                              ), ("clamping_value_high", h(50), 0.5, 0.9,  # A bright yellow
                                  1.0, 0.0, 0.0, 0.3, 0, 0, 0,  # A large highlight boost
                                  (h(50), 0.5, 1.0)  # Expected: final_v is clamped to 1.0
                                  ),

                         ]


@pytest.mark.parametrize(
    "test_id, h_in, s_in, v_in, sat_mult, sh_adj, mid_adj, hi_adj, min_hue, max_hue, target_hue, expected_hsv",
    ADJUST_HSV_TEST_CASES, ids=[case[0] for case in ADJUST_HSV_TEST_CASES]
    # Use test_id for readable report
)
def test_adjust_hsv(
        test_id, h_in, s_in, v_in, sat_mult, sh_adj, mid_adj, hi_adj, min_hue, max_hue, target_hue,
        expected_hsv
):
    """
    Tests the adjust_hsv function with a variety of inputs to check its
    brightness, saturation, and hue shifting logic, including edge cases.
    """
    # Unpack the expected results
    expected_h, expected_s, expected_v = expected_hsv

    # Call the function with the test case inputs
    actual_h, actual_s, actual_v = adjust_hsv(
        h_in, s_in, v_in, saturation_multiplier=sat_mult, shadow_adjust=sh_adj, mid_adjust=mid_adj,
        highlight_adjust=hi_adj, min_hue=min_hue, max_hue=max_hue, target_hue=target_hue
    )

    # Assert that the results are close to the expected values
    assert actual_h == pytest.approx(expected_h, abs=1e-3)
    assert actual_s == pytest.approx(expected_s, abs=1e-3)
    assert actual_v == pytest.approx(expected_v, abs=1e-3)