"""Tests for the tray QR popup positioning helpers.

Pins down compute_popup_position (a pure per-axis placement function) and the
two Windows adapters get_cursor_pos / get_work_area. None of these exist in
tray.py yet, so this suite is RED by design until they are implemented. The
assertions target observable behavior (containment, the adjacency band, the
exact fallback corner, per-axis degradation) so a different but correct formula
still passes.

Run with: uv run pytest tests/test_qr_position.py
"""
from __future__ import annotations

import sys

import pytest

import tray


MARGIN = 12


def contained(
    pos: tuple[int, int],
    win_w: int,
    win_h: int,
    work_area: tuple[int, int, int, int],
) -> bool:
    """True when the win_w by win_h window at pos lies fully inside work_area."""
    x, y = pos
    left, top, right, bottom = work_area
    return left <= x and top <= y and x + win_w <= right and y + win_h <= bottom


def test_roomy_anchor_sits_up_left_within_gap_band() -> None:
    """Case 1: roomy area, interior lower-right anchor. Containment plus the
    adjacency band (window bottom-right sits a small gap up-left of the anchor)."""
    win_w, win_h = 300, 200
    work_area = (0, 0, 1920, 1080)
    anchor = (1600, 900)
    x, y = tray.compute_popup_position(win_w, win_h, work_area, anchor, MARGIN)

    assert contained((x, y), win_w, win_h, work_area), (
        "containment: roomy anchored window must lie fully inside the work area"
    )
    assert x + win_w < anchor[0], (
        "adjacency: window right edge must be left of the anchor"
    )
    assert y + win_h < anchor[1], (
        "adjacency: window bottom edge must be above the anchor"
    )
    gap_x = anchor[0] - (x + win_w)
    gap_y = anchor[1] - (y + win_h)
    assert 1 <= gap_x <= 2 * MARGIN, (
        f"adjacency band: x gap {gap_x} outside [1, {2 * MARGIN}]"
    )
    assert 1 <= gap_y <= 2 * MARGIN, (
        f"adjacency band: y gap {gap_y} outside [1, {2 * MARGIN}]"
    )


def test_top_left_anchor_hugs_clamped_edge() -> None:
    """Case 2: anchor near the top-left (models top or left taskbars). The
    margin tier engages, so the window hugs left+margin and top+margin."""
    win_w, win_h = 300, 200
    work_area = (0, 0, 1920, 1080)
    left, top, right, bottom = work_area
    anchor = (5, 5)
    x, y = tray.compute_popup_position(win_w, win_h, work_area, anchor, MARGIN)

    assert contained((x, y), win_w, win_h, work_area), (
        "containment: window must stay inside even when the anchor is off-corner"
    )
    assert x == left + MARGIN, "clamp: window must hug the left work-area margin"
    assert y == top + MARGIN, "clamp: window must hug the top work-area margin"


def test_anchor_none_places_exact_bottom_right_corner() -> None:
    """Case 3: anchor None gives the exact margin-inset bottom-right corner."""
    win_w, win_h = 300, 200
    work_area = (0, 0, 1920, 1080)
    left, top, right, bottom = work_area
    pos = tray.compute_popup_position(win_w, win_h, work_area, None, MARGIN)
    assert pos == (right - MARGIN - win_w, bottom - MARGIN - win_h), (
        "corner: unanchored placement must be the exact margin-inset bottom-right corner"
    )


def test_negative_origin_contains_anchored_and_unanchored() -> None:
    """Case 4: negative-origin area (secondary monitor). Containment holds for
    both the anchored and the unanchored placement."""
    win_w, win_h = 300, 200
    work_area = (-1920, 0, 0, 1080)
    left, top, right, bottom = work_area

    anchored = tray.compute_popup_position(win_w, win_h, work_area, (-300, 900), MARGIN)
    assert contained(anchored, win_w, win_h, work_area), (
        "negative-origin: anchored window must stay inside a negative-origin area"
    )
    assert anchored[0] + win_w < -300 and anchored[1] + win_h < 900, (
        "negative-origin: anchored window must still sit up-left of the anchor"
    )

    unanchored = tray.compute_popup_position(win_w, win_h, work_area, None, MARGIN)
    assert contained(unanchored, win_w, win_h, work_area), (
        "negative-origin: unanchored window must stay inside a negative-origin area"
    )
    assert unanchored == (right - MARGIN - win_w, bottom - MARGIN - win_h), (
        "negative-origin: unanchored placement is still the margin-inset corner"
    )


def test_oversized_window_pins_to_origin_per_axis() -> None:
    """Case 5: window larger than the area. Overflowing axes pin to the
    work-area origin (left / top) with no exception, independently per axis."""
    win_w, win_h = 300, 200

    tiny = (50, 60, 150, 160)  # avail 100 x 100, non-zero origin
    left, top, right, bottom = tiny
    pos = tray.compute_popup_position(win_w, win_h, tiny, (120, 140), MARGIN)
    assert pos == (left, top), (
        "oversized: a window larger than the area must pin to (left, top)"
    )

    mixed = (50, 60, 150, 460)  # avail 100 (x, overflows) x 400 (y, fits)
    left, top, right, bottom = mixed
    px, py = tray.compute_popup_position(win_w, win_h, mixed, None, MARGIN)
    assert px == left, "oversized: overflowing x axis must pin to left"
    assert top <= py and py + win_h <= bottom, (
        "per-axis: the fitting y axis must remain contained, independent of x"
    )


def test_margin_collapse_band_stays_inside_without_margin() -> None:
    """Case 6: area fits the window but not window plus two margins. The window
    stays fully inside, margins collapsing rather than the window overflowing."""
    win_w, win_h = 100, 80
    # win_w < avail < win_w + 2*margin  ->  100 < 110 < 124 (and 80 < 90 < 104).
    work_area = (200, 300, 310, 390)  # avail 110 x 90, non-zero origin

    unanchored = tray.compute_popup_position(win_w, win_h, work_area, None, MARGIN)
    assert contained(unanchored, win_w, win_h, work_area), (
        "margin-collapse: window must stay fully inside when margins cannot fit"
    )

    anchored = tray.compute_popup_position(win_w, win_h, work_area, (305, 385), MARGIN)
    assert contained(anchored, win_w, win_h, work_area), (
        "margin-collapse: anchored window must also stay inside the collapse band"
    )


def test_determinism_identical_inputs() -> None:
    """Case 7: the function is pure. Identical inputs give identical output."""
    args = (300, 200, (-1920, 0, 0, 1080), (-400, 950), MARGIN)
    first = tray.compute_popup_position(*args)
    second = tray.compute_popup_position(*args)
    assert first == second, "determinism: identical inputs must give identical output"


@pytest.mark.skipif(
    sys.platform != "win32", reason="Win32 work-area and cursor adapters"
)
def test_windows_work_area_and_cursor_adapters() -> None:
    """Case 8: the real Windows adapters return sane rects and a cursor pair.
    No Tk is instantiated anywhere in this file."""
    rect = tray.get_work_area()
    assert isinstance(rect, tuple) and len(rect) == 4, "work area must be a 4-tuple"
    left, top, right, bottom = rect
    assert right > left and bottom > top, "work area must be a non-empty rect"

    cursor = tray.get_cursor_pos()
    assert isinstance(cursor, tuple) and len(cursor) == 2, "cursor must be an (x, y) pair"
    assert all(isinstance(v, int) for v in cursor), "cursor coordinates must be ints"

    at_cursor = tray.get_work_area(cursor)
    assert isinstance(at_cursor, tuple) and len(at_cursor) == 4, (
        "monitor rect must be a 4-tuple"
    )
    cl, ct, cr, cb = at_cursor
    assert cr > cl and cb > ct, "monitor work area must be a non-empty rect"
