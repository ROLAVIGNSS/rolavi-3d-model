"""ROLAVI 3D Model - QGIS plugin (and standalone window for VS Code).

Georeference a textured OBJ, a PLY mesh, a point cloud or 3D Gaussian splats (ROLAVIScan files
placed with their georeference) in its CRS, with an optional GCP adjustment, and export it as a
WGS84 Scene Layer Package (IntegratedMesh I3S 1.7, or PointCloud I3S 2.0). GeoTIFF orthomosaics
and DEMs, cut / fill volumes, and sending everything to ROLAVI Viewer.

* In QGIS: ``classFactory`` below (QGIS calls it when it loads the plugin).
* In VS Code: ``python run_dev.py`` opens the same window without QGIS.

Copyright (C) 2026 Marco Antonio Herrera Diaz - ROLAVI. Licensed under the GNU General Public
License, version 2 or (at your option) any later version: see LICENSE.
"""

__version__ = "1.0.1"


def classFactory(iface):  # noqa: N802 - name required by QGIS
    """QGIS loads the plugin through this function."""
    from .plugin import Rolavi3DModelPlugin

    return Rolavi3DModelPlugin(iface)
