"""ROLAVI 3D Model - standalone run for VS Code (the same window as the QGIS plugin).

    python run_dev.py                 # empty window
    python run_dev.py C:/models/model.obj   # opens the OBJ at start

Needs PyQt5, numpy, pyproj and Pillow (GEOStudio's environment has them all).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rolavi_3d_model.ui.main_window import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
