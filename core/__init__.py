"""Core of ROLAVI 3D Model: no Qt and no QGIS imports.

* ``obj_io``      - streaming reader of large OBJ models.
* ``slpk_io``     - SLPK writer, IntegratedMesh I3S 1.7 / PointCloud I3S 2.0 in WGS84.
* ``scan_io``     - ROLAVIScan PLY files (mesh, point cloud, 3D Gaussian splats) and their georeference.
* ``crs``         - coordinate reference systems through a backend (pyproj, or QGIS in the plugin).
* ``georef``      - model frame (up axis + offset) and GCP adjustment (similarity).
* ``gcp_io``      - GCP CSV files.
* ``geoid``       - GTX geoid grids (orthometric heights).
* ``raster_io``   - GeoTIFF orthomosaics and DEMs; orthomosaics for ROLAVI Viewer (JPG + .aux.xml).
* ``vector_io``   - polygons from Shapefile, KML, KMZ and DXF.
* ``volumes``     - cut / fill volumes from DEMs.
* ``tiles``       - XYZ base map tiles.
* ``viewer_client`` - ROLAVI Viewer account and uploads.
* ``net``         - http(s)-only requests (no file:// or ftp://).
* ``safe_xml``    - safe XML reader for KML and .aux.xml (no entities).
* ``pipeline``    - load a model, build its preview, export the SLPK.
"""
