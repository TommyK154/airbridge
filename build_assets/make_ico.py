"""Generate build/airbridge.ico from the tray icon drawing.

The icon is drawn in code (tray.make_icon), so the repo carries no binary
image assets. Run before PyInstaller: uv run python build_assets/make_ico.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tray import make_icon  # noqa: E402 (needs ROOT on sys.path first)

SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    out = ROOT / "build" / "airbridge.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    image = make_icon(running=True, size=256)
    image.save(out, format="ICO", sizes=[(n, n) for n in SIZES])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
