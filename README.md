# ROLAVI 3D Model — QGIS plugin

Georeference 3D models and export them as Scene Layer Packages (SLPK) in WGS84:

- **Meshes** (textured OBJ, or PLY): IntegratedMesh I3S 1.7, with a level-of-detail pyramid.
- **Point clouds** (PLY): PointCloud I3S 2.0 with LEPCC compression and RGB colour.
- **3D Gaussian Splatting** (3DGS PLY): the centres of the Gaussians with their colour, as a PointCloud.

It also brings **GeoTIFF orthomosaics and DEMs** into the 2D view and computes **cut and fill
volumes** (two DEMs, or a DEM and a base elevation) inside polygons (SHP, DXF, KML, KMZ, or drawn
on the map).

**ROLAVIScan** files (`*_mesh.ply`, `*_mesh_ENU.*`, `*_mesh_UTM.ply`, `*_3DGS.ply`,
`PointCloud/*_live_cloud.ply`) are placed automatically with their own georeference. SLPK is the
format read by ROLAVI Viewer.

The `rolavi_3d_model/` folder **is the QGIS plugin** (`metadata.txt`, `classFactory`, `LICENSE`);
the same window can also run on its own, outside QGIS, with `run_dev.py`.

## Install

- **From QGIS:** *Plugins → Manage and Install Plugins…*, search for **ROLAVI 3D Model** and
  click *Install Plugin*.
- **From a ZIP:** *Plugins → Manage and Install Plugins… → Install from ZIP*, choose
  `rolavi_3d_model-1.0.0.zip` (it contains the `rolavi_3d_model/` folder) and click *Install Plugin*.

Open it with its toolbar button or *Plugins → ROLAVI 3D Model*.

### Requirements

- QGIS 3.28 or later (QGIS 4 included).
- numpy and Pillow: included with QGIS for Windows.
- pyproj is used when the Python of QGIS has it (fast with millions of points); otherwise the plugin
  uses the QGIS coordinate engine (`qgis_crs.py`).
- The **3D view** needs `pyvista` and `pyvistaqt` in the Python of QGIS. Windows: close QGIS, open the
  *OSGeo4W Shell* and run `python -m pip install pyvista pyvistaqt`. macOS / Linux: install them in the
  Python that QGIS uses. Without them, everything else works with the 2D view.

## Workflow

The window has the ROLAVI look: a top bar with the logo, the project and your ROLAVI Viewer account;
on the left the 6 steps (with a green check when done) and the **layers**; in the centre one step at
a time with *Back* / *Next*; on the right the view and the log; at the bottom the progress, *Cancel*
and the coordinates under the cursor.

**Layers** (LAYERS, on the left): every model you import (OBJ, PLY mesh, point cloud, 3DGS) is a new
layer; the previous ones stay open.

- The checkbox shows or hides it in the 2D and 3D views.
- The selected layer is the **active** one: the steps (coordinates, GCPs, export, sending to the
  Viewer) work on it.
- *Remove layer* removes it from the project (the file is not touched) and *+ Add* imports another
  one.
- Each layer keeps its own settings (coordinates, its GCP picks and its adjustment, output). The GCP
  list (names and E / N / H), the CRS, the heights and the geoid belong to the project, shared by all.
- Layers in the project CRS are shown together. A local (not georeferenced) layer is shown alone.

1. **Model:** *Import model…* accepts:
   - Textured **OBJ** (the MTL and the textures next to it).
   - **PLY mesh:** read through the `*_mesh_ENU.obj` that ROLAVIScan writes next to it (it is the
     same mesh). If it is not there, the PLY is converted once to OBJ in the temporary folder.
   - **PLY point cloud:** XYZ (float or double) with RGB colour. Binary or ASCII.
   - **3DGS PLY:** the colour is the base colour of each Gaussian (SH degree 0). Gaussians with an
     opacity below 0.1 (haze) are neither shown nor exported.

   **ROLAVIScan:** when the file brings its georeference (`<capture>_georef.json` next to it, or the
   `ROLAVI_*` comments of the cloud), *Place it with its ROLAVIScan georeference* appears (ticked). The
   model is placed exactly in the CRS you choose: ARKit / ENU / UTM → ENU → lat, lon, h → CRS. The CRS
   is set to the UTM zone of the capture (EPSG:32718 in Lima) and you can change it. The formula was
   checked against the ENU and UTM files of ROLAVIScan (0.000001 m). The UTM files of ROLAVIScan are
   not rotated by the grid convergence (0.44° in Lima, about 7.7 cm every 10 m from the origin), so the
   plugin's coordinates differ from those files by that rotation, and the plugin's are the correct
   ones. If you untick it, the model keeps the settings below: an ARKit file stays local with Y up, to
   be placed with GCPs.

   Below you choose (when ROLAVIScan does not place it):
   - *Coordinates:* georeferenced in the CRS, or a local model (placed with GCPs).
   - *Up axis:* Z up or Y up.
   - *Offset (shift):* the offset used when the OBJ was exported (0 if none).
   - *Coordinate reference system:* the CRS of the project. Pick one from the list, type its EPSG
     code, or click 🔍 *Search…* (or type a name and press Enter) to search the whole EPSG database by
     name, code or area: UTM 18S, Peru96, WGS 84, 4326, Lima. Filter projected (metres) or geographic
     (degrees). The last ones used are remembered.
     - **Projected** (metres): the plugin works directly in it.
     - **Geographic** (latitude / longitude): the GCP adjustment and the views need metres, so the
       plugin works in the WGS 84 UTM zone of the model (chosen automatically from the model or the
       GCPs). You see and type latitude / longitude: the cursor, the extent and the GCP table switch to
       Lon, Lat, h (degrees with 9 decimals; a CSV with Lat before Lon is read correctly too). A file
       with its coordinates in lon / lat is marked *Georeferenced in the CRS* and converted point by
       point. The preview LAS is saved in the UTM zone.
   - *Heights:* ellipsoidal or orthometric. Choosing **Orthometric (geoid)** shows the **GTX geoid**:
     with the box ticked, on export every vertex becomes H = h − N (N from the GTX, bilinear
     interpolation) and the SLPK is declared orthometric. Below it shows N at the centre of the model.
     If Z is already orthometric, untick the box and nothing is converted.

   If there is a `<model>.obj.georef.json` (ROLAVI GEOStudio) or a `.prj` next to the OBJ, the CRS,
   the offset and the heights are taken from it.

2. **GCP adjustment** (optional; required for a local model):
   - *Import CSV…:* Name, E, N, H, with or without a header (also in the order Name, N, E, H).
   - *Pick on model:* select the row and click the point in the view.
   - *Check fit:* residuals of the model as it is, without adjusting.
   - *Solve:* three modes, *Keep vertical* (rotation + scale + translation), *Full 3D* or *Shift
     only*, with *Lock scale* to keep the scale.

   If you change the GCPs after *Solve*, the export is blocked until you solve again.

   **2D / 3D view** (buttons above the view):
   - **2D:** a fast plan view with a sample of coloured points (the centres of the faces).
     - 🗺 **Basemap:** a base map under the model: Google Satellite, Hybrid, Maps, Terrain, Roads,
       Traffic and OpenStreetMap, or your own XYZ server with *Add XYZ tiles…* (remembered). Each tile
       is placed by its corners in the CRS of the view, so it fits the model in any projection. Tiles
       are downloaded in the background and kept only in memory (nothing is saved to disk), with the
       provider's credit at the bottom right. It shows only when the model is in the project CRS
       (georeferenced or adjusted with GCPs).
     - Note: Google's terms only allow its maps through its APIs (Map Tiles API, with a key) and forbid
       storing them. These XYZ addresses are the ones people add by hand in QGIS; for commercial work,
       prefer a licensed provider.
   - **3D:** the real layers, with the GCPs.
     - A textured mesh of up to 4 million triangles is shown whole with its real texture (the UV of
       each corner, without blending texels: the 2 × 2 px patches of ROLAVIScan do not bleed into
       each other).
     - A larger mesh, or one without texture, is simplified to ~1.5 million triangles with a colour
       per vertex: the average of its triangles sampled at full resolution.
     - Bottom right: E, N and Z of the point of the model under the cursor (Lat, Lon, h if the CRS is
       geographic). It is read from the depth of the image already drawn, so it is instant even for a
       large model.
     - Controls as in ROLAVI Viewer: left button pans, right button rotates (Z always up) around the
       centre of the view, the wheel zooms towards the cursor. Double click on the model: that point
       becomes the centre of rotation.
     - **Trackball:** the sphere with the red (rotation about X), green (Y) and blue (Z) circles marks
       the centre of rotation and follows the zoom. Turn it on and off with the *Trackball* button
       (remembered).
     - *Top* looks from above and *Fit view* frames the visible layers. The first time it reads each
       whole mesh (a large model takes a while). It uses pyvista + pyvistaqt.
   - In 3D, a point cloud or a 3DGS is drawn as coloured points: up to 2.5 million, and for a 3DGS its
     visible centres.
   - *Pick on model* works in 3D: choose the GCP row and click (without dragging) on its point on the
     model. On a mesh the point snaps to the nearest real vertex of the full OBJ (not of the simplified
     model). On a cloud it takes the nearest real point to the click. Then it moves to the next empty
     row. The selected GCP is shown in yellow.

3. **Review in CRS:** a plan view in the project CRS (E / N), with the coordinates under the cursor
   and the centre in WGS84. *Save preview LAS for QGIS…* saves the points of the view in the CRS. In
   QGIS go to *Layer ▸ Add Layer ▸ Add Point Cloud Layer* and check that they fall in place.

4. **Export SLPK (WGS84):** with a progress bar, time left and *Cancel*. A mesh is exported as an
   IntegratedMesh. A cloud or a 3DGS as a PointCloud (I3S 2.0, LEPCC, 5 mm precision, with its RGB).
   When it ends, the package is read back to confirm it is in WGS84 and of the right type. If you are
   signed in to ROLAVI Viewer (step 6), it asks whether to send it to your space.

5. **Rasters & volumes** (optional): GeoTIFF orthomosaics and DEMs, and volumes.
   - *Import GeoTIFF…* (or *+ Add ▸ GeoTIFF* in the layers): it recognises whether it is a **DEM** (one
     band of numbers) or an **orthomosaic** (an RGB photo). The CRS comes from the EPSG keys of the
     GeoTIFF, from its `.aux.xml` or from the name of the CRS; if it has none, the plugin asks. It is
     drawn in 2D in the project CRS: the DEM as a shaded relief (without green or red, which mean cut
     and fill). The first raster of an empty project gives the project its CRS. At the bottom, the
     cursor shows the DEM elevation (*DEM Z*) and the Δz of the cut / fill.
   - *Send to ROLAVI Viewer:* an **orthomosaic** is first converted to what the Viewer stores: a `.zip`
     with `name.jpg` + `name.jpg.aux.xml` (GDAL SRS + GeoTransform), much lighter than the GeoTIFF
     (e.g. 234 MB → 22 MB for a 1.94 cm orthomosaic). Areas without data (alpha or nodata) go black,
     which the Viewer makes transparent. A **large** orthomosaic (more than 150 million pixels) goes
     **in pieces** of 8,192 px (each a `.jpg` + `.jpg.aux.xml`, plus `mosaic.json`) at its **original
     resolution**: the server makes the tiles piece by piece. A **DEM** goes as GeoTIFF (the Viewer
     needs the elevations). *Save JPG + .aux.xml…* saves the same `.zip` on your PC. The Viewer accepts
     WGS 84 / SIRGAS in UTM or lon / lat: if the orthomosaic is in PSAD56 or another projection, the
     plugin asks you to reproject it in QGIS first.
   - **Volume:**
     - *Two DEMs:* before (reference) and after. dz = after − before on the grid of the "before" DEM
       (the other one is sampled bilinearly, even in another CRS). *⇄ Swap* exchanges them.
     - *DEM and elevation:* a DEM and a **base elevation**: dz = elevation − DEM; cut above the
       elevation, fill below it. *Mean of the DEM in the area* sets the mean elevation of the DEM
       inside the area. The elevation must be in the same height system as the DEM.
     - *Area:* the whole common area or a **polygon**: *Import polygon…* (Shapefile `.shp` or `.zip`,
       DXF, KML, KMZ) or *Draw on the plan view* (click each vertex; double click, right click or Enter
       closes it; Backspace removes the last one; Esc cancels). A polygon inside another one is a hole.
     - A **DXF always asks** for the datum (which sets the ellipsoid: WGS 84, PSAD56 / International
       1924, SIRGAS 2000 or Peru96 / GRS 80) and the UTM zone, and shows the resulting EPSG; *Other
       CRS…* opens the search for any other one. A shapefile without `.prj` asks the same. From a DXF,
       closed polylines (with arcs) and circles are used; open ones are reported.
     - *Ignore changes smaller than:* a threshold to leave noise out.
   - **Result:** cut and fill (m³) with their areas, net, maximums, compared area and cells. In 2D the
     cut is **red** and the fill **green** over the grey relief (stronger the deeper / higher). *Save
     report (CSV)…* saves the summary.

6. **ROLAVI Viewer:** your Viewer account inside the plugin (optional; everything else works without
   an account).
   - *Server:* `https://viewer.rolavi.net` (or `localhost:8080` to test with a local Viewer).
   - *Sign in* with your user and password. *Keep me signed in* keeps the session for 30 days on this
     PC (the password is never saved). It shows your space, the space used, the limit and the end of
     the plan.
   - *Send exported SLPK* (the one from step 4), *SLPK…*, *DEM / ortho (GeoTIFF)…* and *Vector (SHP,
     KML, DXF)…*. Everything goes to the account's space.
     - Before sending it checks the session and the free space of the plan; if the model already
       exists, it asks whether to replace it.
     - Files larger than 32 MB go in 32 MB parts that are retried automatically if the server fails.
     - A `.shp` is zipped automatically with its `.shx`, `.dbf`, `.prj` and `.cpg`.
     - The server processes GeoTIFFs: the progress bar follows the server until the DEM or the
       orthomosaic is ready. *DEM / ortho…* converts an orthomosaic to JPG + `.aux.xml` before sending
       it, as in step 5.
     - *If the file has no CRS:* the WGS 84 UTM zone used only when the GeoTIFF has no EPSG or the
       shapefile has no `.prj`.

## Run outside QGIS (development)

1. Open the `ROLAVI_3D_Model` folder in VS Code.
2. Choose a Python 3.9+ interpreter with PyQt5, numpy, pyproj and Pillow (for example the Python of
   ROLAVI GEOStudio). Otherwise: `pip install -r requirements-dev.txt`.
3. Press F5 and choose **ROLAVI 3D Model**. From a terminal: `python run_dev.py` or
   `python run_dev.py C:\path\model.obj` (or a `.ply`, or a GeoTIFF `.tif`).

## Project structure

```
run_dev.py                     standalone start (VS Code)
rolavi_3d_model/               THE PLUGIN (this folder goes inside the zip)
  __init__.py                  version and classFactory (QGIS loads the plugin through it)
  plugin.py                    QGIS toolbar button and menu; opens the window
  qgis_crs.py                  QGIS coordinate engine (when pyproj is missing)
  metadata.txt                 name, version, description, links (read by QGIS and plugins.qgis.org)
  LICENSE, README.md, icon.png
  core/                        no Qt and no QGIS
    obj_io.py, slpk_io.py,     streaming OBJ reader, SLPK writer and LEPCC point cloud compression
    lepcc.py                   (shared with ROLAVI GEOStudio)
    scan_io.py                 PLY (mesh / cloud / 3DGS), ROLAVIScan georeference and ScanFrame
                               (ARKit / ENU / UTM -> ECEF -> lat, lon, h -> CRS, exact)
    crs.py                     CRS through a backend (pyproj, or QGIS in the plugin), EPSG search
                               and datum + UTM zone -> EPSG (WGS 84, PSAD56, SIRGAS 2000, Peru96)
    raster_io.py               GeoTIFF (Pillow; GDAL when Pillow cannot): DEM, orthomosaic, preview and
                               orthomosaic -> JPG + .aux.xml .zip for ROLAVI Viewer
    vector_io.py               polygons from Shapefile, KML, KMZ and DXF (or drawn)
    volumes.py                 cut and fill: two DEMs or a DEM and an elevation, inside polygons
    tiles.py                   XYZ base maps: list, tile maths and background download
    georef.py                  model frame (up axis + offset) and GCP adjustment
    gcp_io.py                  GCP CSV files
    las_preview.py             LAS 1.2 with colour and CRS (without laspy)
    pipeline.py                load the model, preview and export the SLPK (mesh or cloud)
    geoid.py                   GTX geoid (NOAA / PROJ): N at lon / lat, H = h - N
    viewer_client.py           ROLAVI Viewer account and uploads (urllib only)
  ui/                          the window (PyQt5 outside QGIS, the PyQt of QGIS in the plugin: Qt5 or Qt6)
    main_window.py             the window and the six steps
    volumes_panel.py           step 5, rasters and volumes
    viewer_panel.py            step 6, ROLAVI Viewer
    layers.py                  layer list: models and map layers (show / hide, active, remove)
    crs_dialog.py              coordinate reference system search
    datum_dialog.py            datum + UTM zone of a file without CRS (DXF, SHP without .prj, GeoTIFF)
    basemap.py                 base map of the 2D view (XYZ tiles in the CRS of the view)
    plan_view.py               2D view: points, rasters, polygons, drawing
    view3d.py                  3D view by layers: real texture, trackball, double click = rotation centre
    widgets.py, theme.py       segmented buttons, steps, cards and ROLAVI style
    app_icon.py                icon in the title bar, the dialogs and the Windows taskbar; settings
    assets/                    ROLAVI logo
    assets/icons/              program icon: PNG from 16 to 256 px (16-32 px with a bolder drawing),
                               .ico for Windows shortcuts and the SVG sources
```

## Releasing a new version

1. Raise `version` in `rolavi_3d_model/metadata.txt` and in `rolavi_3d_model/__init__.py`, and add
   the changes to `changelog` in `metadata.txt`.
2. Zip the `rolavi_3d_model/` folder (the folder inside the zip, without `__pycache__`, under 20 MB).
3. On plugins.qgis.org, open the plugin page and add the new version with the zip. Update this
   repository with the same code.

## Roadmap

- QGIS project integration: load the SLPK and the rasters as project layers, and send a layer open
  in QGIS to ROLAVI Viewer (raster → GeoTIFF, vector → shapefile).
- Volumes: platforms with slopes or a design surface (not only a flat elevation), and a report per
  polygon when the file has several.
- Volumes on the 3D model (the same mesh that goes into the SLPK).
- Real 3DGS: a GCP adjustment that rotates and scales each Gaussian, adjusted 3DGS PLY, and a splat
  layer in ROLAVI Viewer.
- LAS / LAZ (laspy) and exporting the adjusted cloud in the target CRS.

## License

GNU General Public License, version 2 or (at your option) any later version. See `LICENSE`.

Copyright (C) 2026 Marco Antonio Herrera Diaz — ROLAVI ·
contact.rolavi@rolavisurveyor.com · https://www.rolavisurveyor.com
