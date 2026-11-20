# ROLAVI 3D Model — plugin de QGIS (antes ROLAVI OBJ to SLPK)

Convierte un modelo 3D en un SLPK en WGS84 con los mismos escritores de GEOStudio:

- **Mallas** (OBJ texturizado, o PLY): IntegratedMesh I3S 1.7, pirámide de niveles de detalle.
- **Nubes de puntos** (PLY): PointCloud I3S 2.0 con compresión LEPCC y color RGB.
- **3D Gaussian Splatting** (PLY 3DGS): los centros de las gaussianas con su color, como PointCloud.

Además trae **ortomosaicos y DEM (GeoTIFF)** a la vista 2D y calcula **volúmenes de corte y
relleno** (dos DEM, o un DEM y una cota base) dentro de polígonos (SHP, DXF, KML, KMZ o dibujados).

Los archivos de **ROLAVIScan** (`*_mesh.ply`, `*_mesh_ENU.*`, `*_mesh_UTM.ply`, `*_3DGS.ply`,
`PointCloud/*_live_cloud.ply`) se ubican solos con su georreferencia. Es el formato que leen
ROLAVI Viewer y ArcGIS.

La carpeta `rolavi_3d_model/` **es el plugin de QGIS** (`metadata.txt`, `classFactory`, `LICENSE`);
la misma ventana también se abre sola desde VS Code con `run_dev.py`.

## Instalar en QGIS (prueba en tu PC)

1. QGIS → *Complementos → Administrar e instalar complementos → Instalar a partir de ZIP*.
2. Elige `rolavi_3d_model-1.0.0.zip` (dentro tiene la carpeta `rolavi_3d_model/`) → *Instalar*.
3. Se abre con el botón de la barra de herramientas o *Complementos → ROLAVI 3D Model*.

En QGIS usa pyproj si el Python de QGIS lo tiene (rápido con millones de puntos); si no, el motor de
coordenadas de QGIS (`qgis_crs.py`). La vista 3D necesita pyvista y pyvistaqt en el Python de QGIS
(en Windows: cerrar QGIS, abrir *OSGeo4W Shell* y `python -m pip install pyvista pyvistaqt`); sin
ellos funciona todo lo demás con la vista 2D.

## Publicar en el repositorio oficial (plugins.qgis.org)

Así aparece en *Complementos* para todo el mundo:

1. Entra a plugins.qgis.org con *Sign in with GitHub* (cuenta ROLAVIGNSS); también sirve un OSGeo ID.
2. Repositorio público en GitHub: https://github.com/ROLAVIGNSS/rolavi-3d-model (sube esta carpeta).
   El repositorio oficial exige enlaces válidos al código, al tracker (issues) y a la página, y que
   la licencia sea compatible con GPL v2 o posterior: el código queda público bajo GPL (`LICENSE`).
3. En `rolavi_3d_model/metadata.txt` pon tu correo en `email` (`repository`, `tracker` y
   `homepage` ya apuntan al repositorio de GitHub; `homepage` también puede ser una página de
   rolavisurveyor.com que describa el plugin).
4. Vuelve a armar el zip (carpeta `rolavi_3d_model/` adentro, sin `__pycache__`, menos de 20 MB) y
   súbelo en plugins.qgis.org con *Upload a plugin*. Un revisor lo aprueba (días hábiles) y desde
   ahí cualquiera lo instala desde *Complementos*.
5. Cada versión nueva: sube el `version` de `metadata.txt` y de `__init__.py`, anota el `changelog`
   y sube el zip nuevo.

## Ejecutar en VS Code

1. Abre la carpeta `ROLAVI_3D_Model` en VS Code.
2. Elige el intérprete de Python de GEOStudio, que ya trae PyQt5, numpy, pyproj y
   Pillow. En otro entorno: `pip install -r requirements-dev.txt`.
3. Pulsa F5 y elige **ROLAVI 3D Model**. También funciona desde la terminal:
   `python run_dev.py` o `python run_dev.py C:\ruta\modelo.obj` (o `.ply`, o un GeoTIFF `.tif`).

## Flujo de la ventana

La ventana tiene la presentación de ROLAVI: barra superior con el logo, el proyecto y tu cuenta
de ROLAVI Viewer; a la izquierda los 6 pasos (con check verde al completarse) y las **capas**; al
centro un paso a la vez con *Back* / *Next*; a la derecha la vista y el log; abajo el avance,
*Cancel* y las coordenadas bajo el cursor.

**Capas (LAYERS, a la izquierda):** cada modelo que importas (OBJ, PLY malla, nube, 3DGS) es una
capa nueva; los anteriores no se cierran.
- La casilla la **muestra u oculta** en las vistas 2D y 3D.
- La capa **seleccionada es la activa**: los pasos (coordenadas, GCP, exportar, enviar al Viewer)
  trabajan sobre ella.
- *Remove layer* la quita del proyecto (el archivo no se toca) y *+ Add* importa otra.
- Cada capa guarda sus propios ajustes (coordenadas, sus picks de GCP y su ajuste, salida). La
  lista de GCP (nombres y E / N / H), el CRS, las alturas y el geoide son del proyecto, comunes a todas.
- Las capas en el CRS del proyecto se ven juntas. Una capa local (sin georreferenciar) se ve sola.

1. **Model:** *Import model…* acepta:
   - **OBJ** texturizado (el MTL y las texturas van al lado).
   - **PLY malla**: se lee a través del `*_mesh_ENU.obj` que ROLAVIScan deja al lado (es la misma
     malla). Si no está, el PLY se convierte una sola vez a OBJ en la carpeta temporal.
   - **PLY nube de puntos**: XYZ (float o double) con color RGB. Binario o ASCII.
   - **PLY 3DGS**: el color es el color base de cada gaussiana (SH grado 0). Las gaussianas con
     opacidad menor de 0,1 (bruma) no se muestran ni se exportan.

   **ROLAVIScan:** si el archivo trae su georreferencia (`<captura>_georef.json` al lado, o los
   comentarios `ROLAVI_*` de la nube), aparece **Place it with its ROLAVIScan georeference**
   (marcado). El modelo se ubica exacto en el CRS que elijas: ARKit / ENU / UTM → ENU → lat, lon,
   h → CRS. El CRS se pone solo en la UTM de la captura (EPSG:32718 en Lima) y puedes cambiarlo.
   La fórmula se comprobó contra los archivos ENU y UTM de ROLAVIScan (0,000001 m). Los archivos
   UTM de ROLAVIScan no giran por la convergencia de cuadrícula (0,44° en Lima, unos 7,7 cm cada
   10 m desde el origen). Por eso las coordenadas del plugin difieren de esos archivos en ese
   giro, y las del plugin son las correctas.
   Si desmarcas la casilla, el modelo queda con los ajustes de abajo: un archivo ARKit queda local
   con Y arriba, para ubicarlo con GCP.

   Debajo eliges (si no lo ubica ROLAVIScan):
   - *Coordinates:* georreferenciado en el CRS o modelo local (este último se ubica con GCP).
   - *Up axis:* Z arriba o Y arriba.
   - *Offset (shift):* el desplazamiento usado al exportar el OBJ (0 si no tiene).
   - *Coordinate reference system:* el sistema del proyecto. Elige uno de la lista, escribe su código EPSG
     o pulsa **🔍 Search…** (o escribe un nombre y Enter) para buscar en toda la base EPSG por nombre,
     código o zona: *UTM 18S*, *Peru96*, *WGS 84*, *4326*, *Lima*. Filtra proyectados (metros) o
     geográficos (grados). Recuerda los últimos usados.
     - **Proyectado (metros):** se trabaja directo en él, como siempre.
     - **Geográfico (latitud / longitud):** el ajuste GCP y las vistas necesitan metros, así que el
       plugin trabaja en la zona UTM WGS 84 del modelo (la elige solo, del modelo o de los GCP). Tú ves y
       escribes latitud / longitud: el cursor, la extensión y la tabla de GCP pasan a *Lon, Lat, h*
       (grados con 9 decimales; un CSV con Lat antes que Lon se lee bien igual). Un archivo con sus
       coordenadas en lon / lat se marca *Georeferenced in the CRS* y se convierte punto por punto.
       El LAS de vista previa se guarda en la zona UTM.
   - *Heights:* elipsoidal u ortométrica. Al elegir **Orthometric (geoid)** aparece el **geoide GTX**: con la casilla marcada, al exportar cada vértice pasa a H = h − N (N del GTX, interpolación bilineal) y el SLPK se declara ortométrico. Debajo muestra N en el centro del modelo. Si el Z ya es ortométrico, desmarca la casilla y no se convierte nada.

   Si al lado del OBJ hay un `<modelo>.obj.georef.json` de GEOStudio o un `.prj`, toma
   de ahí el CRS, el offset y las alturas.
2. **GCP adjustment (opcional, obligatorio para un modelo local):**
   - *Import CSV…:* Nombre, E, N, H, con o sin encabezado (también el orden Nombre, N, E, H).
   - *Pick on model:* seleccionas la fila y haces clic en el punto en la vista.
   - *Check fit:* residuales del modelo tal como está, sin ajustar.
   - *Solve:* tres modos, *Keep vertical* (giro + escala + traslación), *Full 3D* o
     *Shift only*, con la opción *Lock scale* para no cambiar la escala.

   Si cambias los GCP después de *Solve*, la exportación se bloquea hasta que resuelvas de nuevo.
**Vista 2D / 3D** (botones arriba de la vista):
- *2D*: vista en planta rápida con una muestra de puntos de colores (los centros de las caras).
  - **🗺 Basemap:** mapa base debajo del modelo: Google Satellite, Hybrid, Maps, Terrain, Roads,
    Traffic y OpenStreetMap, o tu propio servidor XYZ con *Add XYZ tiles…* (se recuerda). Cada
    tile se ubica por sus esquinas en el CRS de la vista, así que calza con el modelo en cualquier
    proyección. Se descarga en segundo plano y queda solo en memoria (nada se guarda en disco), con
    el crédito del proveedor abajo a la derecha. Solo aparece si el modelo está en el CRS del
    proyecto (georreferenciado o ajustado con GCP).
  - Ojo: los términos de Google solo permiten sus mapas a través de sus API (Map Tiles API, con
    clave) y prohíben guardarlos. Estas direcciones XYZ son las que la gente agrega a mano en QGIS;
    para un plugin que se vende conviene usar un proveedor con licencia.
- *3D*: las capas de verdad, con los GCP.
  - Una malla texturizada de hasta 4 millones de triángulos se ve **entera y con su textura real**
    (el UV de cada esquina, sin mezclar texeles: los parches de 2 × 2 px de ROLAVIScan no se
    manchan entre sí).
  - Una malla más grande, o sin textura, se simplifica a ~1,5 millones de triángulos con color por
    vértice. Ese color es el promedio de sus triángulos muestreados a resolución completa; antes se
    tomaba de una miniatura de 1024 px y salían cuadros.
  - Abajo a la derecha: **E, N y Z** del punto del modelo bajo el cursor (Lat, Lon, h si el CRS es
    geográfico). Se lee de la profundidad de la imagen ya dibujada, así que es instantáneo aunque el
    modelo sea grande.
  - Controles como en ROLAVI Viewer: **botón izquierdo mueve, botón derecho gira** (Z siempre arriba)
    alrededor del centro de la vista, **la rueda acerca** hacia el cursor. **Doble clic** sobre el
    modelo: ese punto pasa a ser el centro de giro.
  - **Trackball:** la esfera con los círculos rojo (giro en X), verde (Y) y azul (Z) marca el centro
    de giro y se ajusta al zoom. Se prende y apaga con el botón *Trackball* (se recuerda).
  - *Top* lo mira desde arriba y *Fit view* encuadra las capas visibles. La primera vez lee cada
    malla completa (un modelo grande tarda un rato). Usa pyvista + pyvistaqt, como GEOStudio.
- En el 3D, una nube de puntos o un 3DGS se dibuja como puntos de color: hasta 2,5 millones, y del
  3DGS sus centros visibles.
- *Pick on model* trabaja en el 3D: elige la fila del GCP y haz clic (sin arrastrar) sobre su punto en el
  modelo. En una malla el punto se ajusta al vértice real más cercano del OBJ completo (no al modelo
  simplificado). En una nube toma el punto real más cercano al clic. Luego pasa a la siguiente fila
  vacía. El GCP elegido se ve amarillo.

3. **View in the CRS:** vista en planta en el CRS del proyecto (E/N), con coordenadas bajo
   el cursor y el centro en WGS84. *Save preview LAS for QGIS…* guarda los puntos de la
   vista en el CRS. En QGIS ve a *Layer ▸ Add Layer ▸ Add Point Cloud Layer* y revisa que
   caigan en su sitio.
4. **Export SLPK (WGS84):** con barra de avance, tiempo restante y *Cancel*. Una malla sale como
   IntegratedMesh. Una nube o un 3DGS salen como **PointCloud** (I3S 2.0, LEPCC, 5 mm de precisión,
   con su RGB). Al terminar se relee el paquete para confirmar que está en WGS84 y que es del tipo correcto.
   Si ya entraste a ROLAVI Viewer (paso 6), pregunta si lo envías a tu espacio.
5. **Rasters & volumes** (opcional): ortomosaicos y DEM en GeoTIFF, y volúmenes.
   - *Import GeoTIFF…* (o *+ Add ▸ GeoTIFF* en las capas): reconoce solo si es **DEM** (una banda de
     números) u **ortomosaico** (foto RGB). El CRS sale de las claves EPSG del GeoTIFF, de su
     `.aux.xml` o del nombre del CRS; si no trae ninguno, lo pregunta. Se dibuja en 2D en el CRS del
     proyecto: el DEM como relieve sombreado (sin verde ni rojo, que son de corte y relleno). El
     primer ráster de un proyecto vacío le pone su CRS al proyecto. Abajo, el cursor muestra la cota
     del DEM (*DEM Z*) y el Δz del corte / relleno.
   - *Send to ROLAVI Viewer:* un **ortomosaico** se convierte antes a lo que guarda el visor: un `.zip`
     con `nombre.jpg` + `nombre.jpg.aux.xml` (SRS + GeoTransform de GDAL, igual a tu
     `orthomosaic.zip`), mucho más liviano que el GeoTIFF (234 MB → 22 MB en la prueba con tu
     ortomosaico de 1.94 cm). Las zonas sin datos (alfa o *nodata*) van en negro, que el visor vuelve
     transparente. Un ortomosaico **grande** (más de 150 millones de píxeles) va **en piezas** de
     8,192 px (cada una `.jpg` + `.jpg.aux.xml`, más `mosaic.json`) a su **resolución original**: el
     servidor hace los tiles pieza por pieza y nunca necesita más de unos 400 MB de memoria. Para eso
     el servidor debe tener ROLAVI Viewer *Step 28*; con uno anterior, el plugin avisa en vez de
     enviarlo (antes se colgaba el servicio). Un **DEM** va como GeoTIFF (el visor necesita las cotas). *Save JPG + .aux.xml…*
     guarda ese mismo `.zip` en tu PC. El visor acepta WGS 84 / SIRGAS en UTM o lon/lat: si el
     ortomosaico está en PSAD56 u otra proyección, avisa que lo reproyectes en QGIS.
   - **Volume:**
     - *Two DEMs:* antes (referencia) y después. dz = después − antes en la grilla del DEM de antes
       (el otro se muestrea bilineal, aunque esté en otro CRS). *⇄ Swap* los cambia.
     - *DEM and elevation:* un DEM y una **cota base**: dz = cota − DEM; corte por encima de la cota,
       relleno por debajo. *Mean of the DEM in the area* pone la cota media del DEM dentro del área.
       La cota debe estar en el mismo sistema de alturas que el DEM.
     - *Area:* todo el área común o un **polígono**: *Import polygon…* (Shapefile `.shp` o `.zip`,
       DXF, KML, KMZ) o *Draw on the plan view* (clic en cada vértice; doble clic, clic derecho o
       Enter cierra; Retroceso borra el último; Esc cancela). Un polígono dentro de otro es un hueco.
     - Un **DXF siempre pregunta** el datum (que fija el elipsoide: WGS 84, PSAD56 / Internacional
       1924, SIRGAS 2000 o Peru96 / GRS 80) y la zona UTM, y muestra el EPSG que resulta; *Other
       CRS…* abre el buscador para cualquier otro. Un shapefile sin `.prj` pregunta lo mismo.
       Del DXF se usan las polilíneas cerradas (con arcos) y los círculos; las abiertas se avisan.
     - *Ignore changes smaller than:* umbral para no contar ruido.
   - **Result:** corte y relleno (m³) con sus áreas, neto, máximos, área comparada y celdas. En 2D el
     corte va en **rojo** y el relleno en **verde** sobre el relieve gris (más intenso cuanto más
     profundo / alto). *Save report (CSV)…* guarda el resumen.
6. **ROLAVI Viewer:** tu cuenta del visor dentro del plugin.
   - *Server:* `https://viewer.rolavi.net` (o `localhost:8080` para probar con el visor en tu PC).
   - *Sign in* con tu usuario y contraseña. *Keep me signed in* guarda la sesión 30 días en
     esta PC (la contraseña nunca se guarda). Muestra tu espacio, lo usado, el tope y el fin del plan.
   - *Send exported SLPK* (el del paso 4), *SLPK…*, *DEM / ortho (GeoTIFF)…* y
     *Vector (SHP, KML, DXF)…*. Todo va al espacio de la cuenta (el administrador: su espacio privado).
     - Antes de enviar revisa la sesión y el espacio libre del plan; si el modelo ya existe, pregunta
       si lo reemplazas.
     - Archivos de más de 32 MB van en partes de 32 MB que se reintentan solas si el servidor falla
       (Cloudflare deja 100 MB por petición).
     - Un `.shp` se comprime solo con su `.shx`, `.dbf`, `.prj` y `.cpg`.
     - El GeoTIFF lo procesa el servidor: la barra sigue el avance del servidor hasta que el DEM o el
       ortomosaico queda listo. *DEM / ortho…* convierte el ortomosaico a JPG + `.aux.xml` antes de
       enviarlo, como en el paso 5.
     - *If the file has no CRS:* zona UTM WGS 84 que se usa solo si el GeoTIFF no trae EPSG o el
       shapefile no tiene `.prj`.

## Estructura

```
run_dev.py                     arranque para VS Code
rolavi_3d_model/               EL PLUGIN (esta carpeta va dentro del zip)
  __init__.py                  versión y classFactory (QGIS carga el plugin por aquí)
  plugin.py                    botón y menú de QGIS; abre la ventana
  qgis_crs.py                  motor de coordenadas de QGIS (cuando no hay pyproj)
  metadata.txt                 nombre, versión, descripción, enlaces (lo lee QGIS y plugins.qgis.org)
  LICENSE, README.md, icon.png
  core/                        sin Qt ni QGIS
    obj_io.py, slpk_io.py,     de GEOStudio, con tres mejoras que conviene pasar a GEOStudio:
    lepcc.py                   obj_io encuentra la textura si una copia le cambió la extensión
                               (texture.jpg.jpeg); slpk_io da a los niveles simplificados el color
                               promedio de sus triángulos (antes: cuadros) y, cuando los parches de un
                               tile están dispersos en el atlas, los copia a un atlas compacto en vez
                               de achicar el atlas entero; lepcc: compresión de nubes de puntos
    scan_io.py                 PLY (malla / nube / 3DGS), georreferencia de ROLAVIScan y ScanFrame
                               (ARKit / ENU / UTM -> ECEF -> lat, lon, h -> CRS, exacto)
    crs.py                     CRS a través de un "backend" (pyproj, o QGIS en el plugin), búsqueda EPSG
                               y datum + zona UTM -> EPSG (WGS 84, PSAD56, SIRGAS 2000, Peru96)
    raster_io.py               GeoTIFF (Pillow; GDAL si Pillow no puede): DEM, ortomosaico, vista previa y
                               ortomosaico -> .zip JPG + .aux.xml para ROLAVI Viewer
    vector_io.py               polígonos de Shapefile, KML, KMZ y DXF (o dibujados)
    volumes.py                 corte y relleno: dos DEM o DEM y cota, dentro de polígonos, colores rojo / verde
    tiles.py                   mapas base XYZ: lista, matemática de tiles y descarga en segundo plano
    georef.py                  marco del modelo (eje arriba + offset) y ajuste por GCP
    gcp_io.py                  CSV de GCP
    las_preview.py             LAS 1.2 con color y CRS (sin laspy)
    pipeline.py                cargar el modelo, vista previa y exportar SLPK (malla o nube)
    geoid.py                   geoide GTX (NOAA / PROJ): N en lon/lat, H = h - N
    viewer_client.py           cuenta de ROLAVI Viewer y envío de archivos (solo urllib)
  ui/                          ventana (PyQt5 en VS Code, el PyQt propio de QGIS en el plugin: Qt5 o Qt6)
    volumes_panel.py           paso 5, rásters y volúmenes
    viewer_panel.py            paso 6, ROLAVI Viewer
    layers.py                  lista de capas: modelos y capas de mapa (mostrar / ocultar, activa, quitar)
    crs_dialog.py              buscador de sistemas de coordenadas
    datum_dialog.py            datum + zona UTM de un archivo sin CRS (DXF, SHP sin .prj, GeoTIFF sin CRS)
    basemap.py                 mapa base de la vista 2D (tiles XYZ en el CRS de la vista)
    view3d.py                  vista 3D por capas: textura real, trackball, doble clic = centro de giro
    widgets.py, theme.py       botones segmentados, pasos, tarjetas y estilo ROLAVI
    app_icon.py                icono en la barra de título, los diálogos y la barra de tareas de Windows
    assets/                    logo ROLAVI
    assets/icons/              icono del programa: PNG de 16 a 256 px (16-32 px con un dibujo más grueso),
                               .ico para accesos directos de Windows y los SVG de origen
```

## ROLAVI Viewer y las nubes de puntos

Para que el visor muestre un SLPK de nube de puntos (o de 3DGS), el `server.py` del visor debe
tener la extensión de las nubes en `STRIP_EXTENSIONS` (`.pccxyz`, `.pccrgb`...). Sin eso el visor
no encuentra la geometría de los nodos. Ya está en el `server.py` de ROLAVI Viewer Step 28, que va en
la carpeta principal del visor (junto a `raster_import.py`), no dentro de `public/`.

## Siguiente paso

1. Integración con el proyecto de QGIS: cargar el SLPK y los rásters como capas del proyecto y
   **enviar a ROLAVI Viewer una capa abierta en QGIS** (ráster → GeoTIFF, vectorial → shapefile).
2. Volúmenes: plataformas con talud o superficie de diseño (no solo una cota plana) y reporte por
   polígono cuando el archivo trae varios.
3. Volúmenes sobre el modelo 3D (el mismo mallado que va al SLPK).
4. 3DGS de verdad: ajuste GCP que gire y escale cada gaussiana, PLY 3DGS ajustado, y capa de
   splats en ROLAVI Viewer (ArcGIS 5.1 `GaussianSplatLayer`, que lee 3D Tiles, no SLPK).
5. LAS / LAZ (laspy) y exportar la nube ajustada en el CRS de destino.
