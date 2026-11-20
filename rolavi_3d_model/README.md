# ROLAVI 3D Model (QGIS plugin)

Georeference 3D models and export them as Scene Layer Packages (SLPK / I3S), with DEM volumes
and publishing to ROLAVI Viewer.

*Georreferencia modelos 3D y expórtalos como SLPK (I3S); volúmenes con DEM y envío a ROLAVI Viewer.*

## What it does

- Import textured **OBJ**, **PLY** meshes, PLY **point clouds** and **3D Gaussian splats**
  (3DGS), including ROLAVIScan files placed with their own georeference.
- Place the model in any CRS (EPSG search, projected or geographic) and adjust it with
  **ground control points** (similarity transform, residuals per point). Optional GTX geoid.
- Export **SLPK** in WGS84: IntegratedMesh I3S 1.7 (meshes), PointCloud I3S 2.0 (point clouds).
- **GeoTIFF** orthomosaics and DEMs in the 2D view; **cut / fill volumes** between two DEMs or a
  DEM and a base elevation, inside polygons (Shapefile, DXF, KML, KMZ, or drawn).
- Optional: send models, DEMs, orthomosaics and vector layers to a **ROLAVI Viewer** account
  (https://viewer.rolavi.net). Everything else works without an account.

## Use

*Plugins > ROLAVI 3D Model*, or its toolbar button. The window has six steps: Model, GCP
adjustment, Review in CRS, Export SLPK, Rasters & volumes, ROLAVI Viewer.

## Requirements

- QGIS 3.28 or later (QGIS 4 included).
- numpy and Pillow: included with QGIS for Windows. pyproj is used when present, otherwise the
  QGIS coordinate engine.
- Optional 3D view: `pyvista` and `pyvistaqt`. Windows: close QGIS, open the **OSGeo4W Shell** and
  run `python -m pip install pyvista pyvistaqt`. macOS / Linux: install them in the Python that
  QGIS uses. Without them the plugin works with the 2D view.

## License

GNU General Public License, version 2 or (at your option) any later version. See `LICENSE`.
Copyright (C) 2026 Marco Antonio Herrera Diaz - ROLAVI.
