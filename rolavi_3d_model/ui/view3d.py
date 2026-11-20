"""3D view of the layers (PyVista / VTK, the same engine as GEOStudio's viewer).

Optional: without pyvista + pyvistaqt the window keeps its 2D view and the 3D button says
how to install them (GEOStudio's Python already has them).

* Several layers (meshes, point clouds, splat centres), each shown / hidden / removed by key.
  They are drawn around one origin (coordinates moved near 0, 0, 0 for the graphics card).
* A textured mesh is drawn with its real texture (uv of every corner, nearest texel: the
  2 x 2 px patches of ROLAVIScan do not bleed into each other); simplified ones with a
  colour per vertex. The GCPs are markers with their names.
* Controls as in ROLAVI Viewer: left button moves (pan), right button rotates (Z always up)
  around the centre of the view, the wheel zooms towards the cursor. A double click on the
  model makes that point the centre of rotation.
* Trackball: a sphere with three circles (red around X, green around Y, blue around Z) at the
  centre of rotation, sized to the view; it can be turned off.
* Under the cursor: ``hovered(x, y, z, on_model)`` gives the project coordinates of the surface
  (read from the depth of the drawn frame: instant, whatever the size of the model).
* GCP picking: with *Pick on model* on, a left click without dragging picks the active layer
  (``picked(face, r, s)``: triangle of its display mesh and the parametric coordinates, or
  ``picked(point, 0, 0)`` for a point cloud).
* The white-view fix of GEOStudio's viewer: when Qt gives the widget another native window,
  VTK is told to draw into the new one.
"""

from __future__ import annotations

import math
import re
import time
import traceback

import numpy as np

from .qt import IN_QGIS, QtCore, Signal

try:
    import pyvista as pv
    from pyvistaqt import QtInteractor
    from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTerrain
    from vtkmodules.vtkRenderingCore import vtkCellPicker, vtkPointPicker

    AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the Python used
    pv = None
    QtInteractor = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc) or exc.__class__.__name__

BACKGROUND = "#07131F"
MODEL_MARK = "#FF9900"
SELECTED_MARK = "#FFE14D"
CLICK_SLOP = 4            # pixels: a press and release closer than this is a click, not a drag
ZOOM_STEP = 1.2
VTK_CURSOR_DEFAULT = 0
VTK_CURSOR_CROSSHAIR = 10
KNOWN_MARK = "#00C8FF"
HOVER_EVERY = 0.04         # seconds between two readings of the point under the cursor
TRACKBALL_SIZE = 0.28      # radius of the trackball: share of half the height of the view
TRACKBALL_COLOURS = (("x", "#E8463A"), ("y", "#3DBB5A"), ("z", "#3D7BEA"))
if IN_QGIS:
    INSTALL_HINT = ("The 3D view needs pyvista and pyvistaqt in the Python of QGIS (the 2D view, the export "
                    "and everything else work without them).\n"
                    "Windows: close QGIS, open the OSGeo4W Shell and run:  python -m pip install pyvista pyvistaqt\n"
                    "macOS / Linux: in a terminal, with the Python QGIS uses:  python3 -m pip install pyvista pyvistaqt\n"
                    "Then start QGIS again.")
else:
    INSTALL_HINT = ("The 3D view needs pyvista and pyvistaqt (GEOStudio's Python already has them).\n"
                    "In the VS Code terminal of this Python:  pip install pyvista pyvistaqt")


# ---------------------------------------------------------------------------
# Native window of the VTK view (from GEOStudio's viewer: white view after a re-parent)
# ---------------------------------------------------------------------------

def _vtk_window_id(window) -> int | None:
    try:
        text = str(window.GetGenericWindowId())
    except Exception:
        return None
    match = re.search(r"_([0-9a-fA-F]+)_p_void", text)  # VTK writes a pointer as "_<hex>_p_void"
    if match is not None:
        return int(match.group(1), 16)
    return int(text) if text.strip().lstrip("-").isdigit() else None


def _window_info(window_id: int) -> str:
    low = int(window_id) & 0xFFFFFFFF
    if low >= 0x80000000:
        low -= 0x100000000
    return str(low)


def _same_window(first: int | None, second: int | None) -> bool:
    return first is not None and second is not None and (first & 0xFFFFFFFF) == (second & 0xFFFFFFFF)


def _pv_faces(faces: np.ndarray) -> np.ndarray:
    faces = np.asarray(faces, dtype=np.int64)
    return np.hstack([np.full((len(faces), 1), 3, dtype=np.int64), faces]).ravel()


if AVAILABLE:
    def _address(actor) -> str:
        try:
            return actor.GetAddressAsString("vtkObject")
        except Exception:
            return str(id(actor))

    class View3D(QtInteractor):
        picked = Signal(int, float, float)        # active layer: triangle (or point) and parametric r, s
        hovered = Signal(float, float, float, bool)   # project coordinates under the cursor, on the model?

        def __init__(self, parent=None):
            super().__init__(parent)
            self._origin: np.ndarray | None = None
            self._vtk_win_stale = True
            self._vtk_win_id = None
            self._layers: dict = {}               # key -> {"actors": [(actor, face_start)], "points", "visible"}
            self._active = None
            self._textures: dict = {}             # id(image) -> (image, pv.Texture)
            self._pick_mode = False
            self._press = None
            self._picker = vtkCellPicker()
            self._picker.SetTolerance(0.0005)
            self._point_picker = vtkPointPicker()
            self._point_picker.SetTolerance(0.004)     # fraction of the window diagonal: a few pixels
            self._centre_picker = vtkCellPicker()
            self._centre_picker.SetTolerance(0.004)
            self._trackball_on = True
            self._trackball: list = []
            self._hover_time = 0.0
            self.set_background(BACKGROUND)
            self._install_controls()
            try:
                self.add_axes(line_width=1, labels_off=False)
            except Exception:
                pass
            try:
                self.renderer.AddObserver("StartEvent", self._update_trackball)
            except Exception:
                traceback.print_exc()

        # ---------------------------------------------------------------- controls (as ROLAVI Viewer)
        def _install_controls(self) -> None:
            """Left = pan, right = rotate with Z up, wheel = zoom towards the cursor."""
            style = vtkInteractorStyleTerrain()
            style.SetDefaultRenderer(self.renderer)
            # an observer on the style replaces its own handler of that event
            style.AddObserver("LeftButtonPressEvent", self._pan_start)
            style.AddObserver("LeftButtonReleaseEvent", self._pan_end)
            style.AddObserver("MiddleButtonPressEvent", self._pan_start)
            style.AddObserver("MiddleButtonReleaseEvent", self._pan_end)
            style.AddObserver("RightButtonPressEvent", self._rotate_start)
            style.AddObserver("RightButtonReleaseEvent", self._rotate_end)
            style.AddObserver("MouseWheelForwardEvent", lambda _obj, _event: self._zoom(ZOOM_STEP))
            style.AddObserver("MouseWheelBackwardEvent", lambda _obj, _event: self._zoom(1.0 / ZOOM_STEP))
            self._style = style
            self.iren.interactor.SetInteractorStyle(style)
            try:   # an observer of the interactor (not of the style): pan / rotate keep working
                self.iren.interactor.AddObserver("MouseMoveEvent", self._hover)
            except Exception:
                traceback.print_exc()

        def _hover(self, _obj=None, _event=None) -> None:
            """Project coordinates of the surface under the cursor, from the depth of the last frame."""
            now = time.monotonic()
            if now - self._hover_time < HOVER_EVERY or self._origin is None:
                return
            try:
                if self._style.GetState() != 0:     # panning / rotating: no reading
                    return
                self._hover_time = now
                x, y = self._event_position()
                depth = float(self.renderer.GetZ(x, y))
                if not 0.0 < depth < 0.999999:       # background
                    self.hovered.emit(0.0, 0.0, 0.0, False)
                    return
                self.renderer.SetDisplayPoint(x, y, depth)
                self.renderer.DisplayToWorld()
                world = self.renderer.GetWorldPoint()
                if not world[3]:
                    return
                point = np.array(world[:3], dtype=np.float64) / world[3] + self._origin
                self.hovered.emit(float(point[0]), float(point[1]), float(point[2]), True)
            except Exception:
                pass

        def _event_position(self) -> tuple[int, int]:
            x, y = self.iren.interactor.GetEventPosition()
            return int(x), int(y)

        def _pan_start(self, _obj=None, _event=None) -> None:
            x, y = self._event_position()
            try:
                double = self.iren.interactor.GetRepeatCount() > 0
            except Exception:
                double = False
            if double and not self._pick_mode:
                self._press = None
                self.centre_at(x, y)
                return
            self._press = (x, y)
            self._style.FindPokedRenderer(x, y)
            if self._style.GetCurrentRenderer() is not None:
                self._style.StartPan()

        def _pan_end(self, _obj=None, _event=None) -> None:
            self._style.EndPan()
            x, y = self._event_position()
            if (self._pick_mode and self._press is not None
                    and abs(x - self._press[0]) + abs(y - self._press[1]) <= CLICK_SLOP):
                self._pick_at(x, y)
            self._press = None

        def _rotate_start(self, _obj=None, _event=None) -> None:
            x, y = self._event_position()
            self._style.FindPokedRenderer(x, y)
            if self._style.GetCurrentRenderer() is not None:
                self._style.StartRotate()

        def _rotate_end(self, _obj=None, _event=None) -> None:
            self._style.EndRotate()

        def _zoom(self, factor: float) -> None:
            """Zoom towards the point under the cursor (parallel view: towards the centre)."""
            renderer = self.renderer
            camera = renderer.GetActiveCamera()
            if camera.GetParallelProjection():
                camera.SetParallelScale(camera.GetParallelScale() / factor)
            else:
                x, y = self._event_position()
                focal = np.array(camera.GetFocalPoint(), dtype=np.float64)
                position = np.array(camera.GetPosition(), dtype=np.float64)
                renderer.SetWorldPoint(focal[0], focal[1], focal[2], 1.0)
                renderer.WorldToDisplay()
                depth = renderer.GetDisplayPoint()[2]
                renderer.SetDisplayPoint(x, y, depth)
                renderer.DisplayToWorld()
                world = np.array(renderer.GetWorldPoint(), dtype=np.float64)
                target = world[:3] / world[3] if world[3] else focal
                step = 1.0 - 1.0 / factor
                camera.SetPosition(*(position + (target - position) * step))
                camera.SetFocalPoint(*(focal + (target - focal) * step))
            renderer.ResetCameraClippingRange()
            self.render()

        def centre_at(self, x: int, y: int) -> None:
            """The point of the model under (x, y) becomes the centre of the view and of rotation."""
            picker = self._centre_picker
            picker.InitializePickList()
            count = 0
            for layer in self._layers.values():
                if layer["visible"]:
                    for actor, _start in layer["actors"]:
                        picker.AddPickList(actor)
                        count += 1
            if not count:
                return
            picker.PickFromListOn()
            if not picker.Pick(x, y, 0, self.renderer) or picker.GetCellId() < 0:
                return
            point = np.array(picker.GetPickPosition(), dtype=np.float64)
            camera = self.renderer.GetActiveCamera()
            focal = np.array(camera.GetFocalPoint(), dtype=np.float64)
            position = np.array(camera.GetPosition(), dtype=np.float64)
            camera.SetFocalPoint(*point)
            camera.SetPosition(*(position + point - focal))
            self.renderer.ResetCameraClippingRange()
            self.render()

        # ---------------------------------------------------------------- trackball
        def set_trackball(self, on: bool) -> None:
            self._trackball_on = bool(on)
            if on and not self._trackball:
                self._make_trackball()
            for actor in self._trackball:
                actor.SetVisibility(bool(on) and bool(self._layers))
            self.render()

        def _make_trackball(self) -> None:
            try:
                sphere = pv.Sphere(radius=1.0, theta_resolution=48, phi_resolution=32)
                actor = self.add_mesh(sphere, color="#D8DEE6", opacity=0.13, name="trackball_sphere",
                                      reset_camera=False, pickable=False, show_scalar_bar=False, specular=0.0)
                self._trackball.append(actor)
                angles = np.linspace(0.0, 2.0 * math.pi, 145)
                cos, sin, zero = np.cos(angles), np.sin(angles), np.zeros_like(angles)
                rings = {"x": np.column_stack([zero, cos, sin]), "y": np.column_stack([cos, zero, sin]),
                         "z": np.column_stack([cos, sin, zero])}
                for axis, colour in TRACKBALL_COLOURS:
                    ring = pv.PolyData(rings[axis].astype(np.float32))
                    ring.lines = np.concatenate([[len(angles)], np.arange(len(angles))]).astype(np.int64)
                    actor = self.add_mesh(ring, color=colour, line_width=2.5, lighting=False,
                                          name=f"trackball_{axis}", reset_camera=False, pickable=False,
                                          show_scalar_bar=False)
                    self._trackball.append(actor)
                for actor in self._trackball:
                    try:
                        actor.SetUseBounds(False)   # not counted when the view is fitted
                    except Exception:
                        pass
                    actor.SetPickable(False)
            except Exception:
                traceback.print_exc()
                self._trackball = []

        def _update_trackball(self, _obj=None, _event=None) -> None:
            """Before each frame: the trackball at the centre of rotation, sized to the view."""
            if not self._trackball:
                return
            visible = self._trackball_on and bool(self._layers)
            for actor in self._trackball:
                actor.SetVisibility(visible)
            if not visible:
                return
            camera = self.renderer.GetActiveCamera()
            if camera.GetParallelProjection():
                half = camera.GetParallelScale()
            else:
                half = camera.GetDistance() * math.tan(math.radians(camera.GetViewAngle()) / 2.0)
            radius = max(1e-6, TRACKBALL_SIZE * half)
            focal = camera.GetFocalPoint()
            for actor in self._trackball:
                actor.SetPosition(*focal)
                actor.SetScale(radius, radius, radius)

        # ---------------------------------------------------------------- picking
        def set_pick_mode(self, on: bool) -> None:
            self._pick_mode = bool(on)
            try:
                self.ren_win.SetCurrentCursor(VTK_CURSOR_CROSSHAIR if on else VTK_CURSOR_DEFAULT)
            except Exception:
                pass

        def _pick_at(self, x: int, y: int) -> None:
            layer = self._layers.get(self._active)
            if layer is None or not layer["actors"]:
                return
            if layer["points"]:
                picker = self._point_picker
                picker.InitializePickList()
                picker.AddPickList(layer["actors"][0][0])
                picker.PickFromListOn()
                if not picker.Pick(x, y, 0, self.renderer):
                    return
                point = int(picker.GetPointId())
                if point >= 0:
                    self.picked.emit(point, 0.0, 0.0)
                return
            picker = self._picker
            picker.InitializePickList()
            starts = {}
            for actor, start in layer["actors"]:
                picker.AddPickList(actor)
                starts[_address(actor)] = start
            picker.PickFromListOn()
            if not picker.Pick(x, y, 0, self.renderer):
                return
            cell = int(picker.GetCellId())
            if cell < 0:
                return
            start = starts.get(_address(picker.GetActor()), 0)
            r, s, _t = picker.GetPCoords()
            self.picked.emit(start + cell, float(r), float(s))

        # ---------------------------------------------------------------- layers
        @property
        def origin(self) -> np.ndarray | None:
            return self._origin

        def set_origin(self, origin) -> None:
            """Project coordinates drawn at 0, 0, 0 (the layers must be set again after a change)."""
            self._origin = np.round(np.asarray(origin, dtype=np.float64), 3)

        def has_layer(self, key) -> bool:
            return key in self._layers

        def layer_keys(self) -> list:
            return list(self._layers)

        def _texture(self, image: np.ndarray):
            entry = self._textures.get(id(image))
            if entry is None or entry[0] is not image:
                texture = pv.numpy_to_texture(np.ascontiguousarray(image))
                entry = (image, texture)
                self._textures[id(image)] = entry
            return entry[1]

        def set_layer_mesh(self, key, world: np.ndarray, parts: list, colors: np.ndarray, visible: bool = True) -> None:
            """A mesh layer: ``world`` = its vertices in project coordinates, ``parts`` = DisplayPart list."""
            self.remove_layer(key, render=False)
            world = np.asarray(world, dtype=np.float64)
            if self._origin is None:
                self.set_origin((world.min(axis=0) + world.max(axis=0)) / 2.0)
            local = (world - self._origin).astype(np.float32)
            colors = np.asarray(colors, dtype=np.uint8)
            actors = []
            for index, part in enumerate(parts):
                if not len(part.faces):
                    continue
                mesh = pv.PolyData(np.ascontiguousarray(local[part.point_vertex]), _pv_faces(part.faces))
                name = f"layer{key}_{index}"
                options = dict(name=name, show_scalar_bar=False, ambient=0.35, diffuse=0.75, specular=0.0,
                               reset_camera=False)
                if part.image is not None and part.uv is not None:
                    try:
                        mesh.active_texture_coordinates = np.ascontiguousarray(part.uv, dtype=np.float32)
                    except AttributeError:          # older pyvista
                        mesh.t_coords = np.ascontiguousarray(part.uv, dtype=np.float32)
                    actor = self.add_mesh(mesh, texture=self._texture(part.image), **options)
                else:
                    mesh.point_data["RGB"] = np.ascontiguousarray(colors[part.point_vertex][:, :3])
                    actor = self.add_mesh(mesh, scalars="RGB", rgb=True, **options)
                actor.SetVisibility(bool(visible))
                actors.append((actor, int(part.face_start)))
            self._layers[key] = {"actors": actors, "points": False, "visible": bool(visible)}
            self.render()

        def set_layer_points(self, key, world: np.ndarray, colors: np.ndarray, point_size: float = 2.0,
                             visible: bool = True) -> None:
            """A point cloud layer (or the centres of Gaussian splats) in project coordinates."""
            self.remove_layer(key, render=False)
            world = np.asarray(world, dtype=np.float64)
            if not len(world):
                return
            if self._origin is None:
                self.set_origin((world.min(axis=0) + world.max(axis=0)) / 2.0)
            points = np.ascontiguousarray((world - self._origin).astype(np.float32))
            cloud = pv.PolyData(points)
            if cloud.n_cells != len(points):   # one vertex cell per point: point id = picked id
                cloud.verts = np.column_stack([np.ones(len(points), dtype=np.int64),
                                               np.arange(len(points), dtype=np.int64)]).ravel()
            cloud.point_data["RGB"] = np.ascontiguousarray(np.asarray(colors, dtype=np.uint8)[:, :3])
            actor = self.add_mesh(cloud, scalars="RGB", rgb=True, name=f"layer{key}_0", show_scalar_bar=False,
                                  style="points", point_size=float(point_size),
                                  render_points_as_spheres=False, lighting=False, reset_camera=False)
            actor.SetVisibility(bool(visible))
            self._layers[key] = {"actors": [(actor, 0)], "points": True, "visible": bool(visible)}
            self.render()

        def set_layer_visible(self, key, visible: bool) -> None:
            layer = self._layers.get(key)
            if layer is None:
                return
            layer["visible"] = bool(visible)
            for actor, _start in layer["actors"]:
                actor.SetVisibility(bool(visible))
            self.render()

        def remove_layer(self, key, render: bool = True) -> None:
            layer = self._layers.pop(key, None)
            if layer is None:
                return
            for actor, _start in layer["actors"]:
                try:
                    self.remove_actor(actor, render=False)
                except Exception:
                    pass
            if not self._layers:
                self._origin = None
                self._textures.clear()
            if render:
                self.render()

        def set_active_layer(self, key) -> None:
            self._active = key

        def clear_model(self) -> None:
            for key in list(self._layers):
                self.remove_layer(key, render=False)
            for name in ("gcp_model", "gcp_selected", "gcp_known", "gcp_labels"):
                try:
                    self.remove_actor(name, render=False)
                except Exception:
                    pass
            self.render()

        def set_markers(self, markers: list[dict]) -> None:
            """``[{"name", "model": (x, y, z) | None, "known": (x, y, z) | None, "selected": bool}]``."""
            for name in ("gcp_model", "gcp_selected", "gcp_known", "gcp_labels"):
                try:
                    self.remove_actor(name, render=False)
                except Exception:
                    pass
            if self._origin is None:
                self.render()
                return
            model = [m for m in markers if m.get("model") is not None]
            known = [m for m in markers if m.get("known") is not None]
            for group, color, size, name in (([m for m in model if not m.get("selected")], MODEL_MARK, 14, "gcp_model"),
                                             ([m for m in model if m.get("selected")], SELECTED_MARK, 20, "gcp_selected")):
                if group:
                    pts = np.array([m["model"] for m in group], dtype=np.float64) - self._origin
                    self.add_points(pts.astype(np.float32), color=color, point_size=size,
                                    render_points_as_spheres=True, name=name, reset_camera=False, pickable=False)
            if known:
                pts = np.array([m["known"] for m in known], dtype=np.float64) - self._origin
                self.add_points(pts.astype(np.float32), color=KNOWN_MARK, point_size=10,
                                render_points_as_spheres=True, name="gcp_known", reset_camera=False, pickable=False)
            anchors = model or known
            if anchors:
                key = "model" if model else "known"
                pts = np.array([m[key] for m in anchors], dtype=np.float64) - self._origin
                names = [str(m.get("name", "")) for m in anchors]
                try:
                    self.add_point_labels(pts.astype(np.float32), names, font_size=12, text_color="white",
                                          shape_opacity=0.35, show_points=False, always_visible=True,
                                          name="gcp_labels", reset_camera=False)
                except TypeError:
                    self.add_point_labels(pts.astype(np.float32), names, font_size=12, text_color="white",
                                          shape_opacity=0.35, show_points=False, name="gcp_labels")
            self.render()

        # ---------------------------------------------------------------- camera
        def _has_visible(self) -> bool:
            return any(layer["visible"] and layer["actors"] for layer in self._layers.values())

        def reset_view(self) -> None:
            """Oblique view of the visible layers with Z up (as GEOStudio's 3D view)."""
            if not self._has_visible():
                return
            try:
                self.disable_parallel_projection()
                self.view_isometric(render=False)
                self.set_viewup([0.0, 0.0, 1.0], render=False)
                self.camera.Azimuth(-35.0)
                self.camera.OrthogonalizeViewUp()
                self.reset_camera(render=False)
                self.reset_camera_clipping_range()
            except Exception:
                traceback.print_exc()
            self.render()

        def top_view(self) -> None:
            if not self._has_visible():
                return
            try:
                self.enable_parallel_projection()
                self.view_xy(render=False)
                self.reset_camera(render=False)
            except Exception:
                traceback.print_exc()
            self.render()

        # ---------------------------------------------------------------- VTK window (GEOStudio fix)
        def ensure_vtk_window(self, force: bool = False) -> None:
            if not force and not self._vtk_win_stale:
                return
            try:
                current = int(self.winId())
            except Exception:
                return
            window = getattr(self, "ren_win", None)
            if not current or window is None:
                return
            self._vtk_win_stale = False
            try:
                opened = window.GetGenericContext() is not None
            except Exception:
                opened = True
            bound = _vtk_window_id(window)
            if opened and (_same_window(bound, current) or (bound is None and not force)):
                self._vtk_win_id = current
                return
            try:
                for _ in range(1000):
                    if not window.GetMapped():
                        break
                    window.Finalize()
                window.SetNextWindowInfo(_window_info(current))
                window.WindowRemap()
            except Exception:
                traceback.print_exc()
                return
            self._vtk_win_id = current
            try:
                self.render()
            except Exception:
                pass

        def event(self, event):
            try:
                if event.type() == QtCore.QEvent.Type.WinIdChange:
                    self._vtk_win_stale = True   # Qt gave the widget another native window
            except Exception:
                pass
            return super().event(event)

        def paintEvent(self, event):
            self.ensure_vtk_window()
            try:
                super().paintEvent(event)
            except Exception:
                traceback.print_exc()

        def showEvent(self, event):
            try:
                super().showEvent(event)
            finally:
                self.ensure_vtk_window()
                try:
                    self.update()
                except Exception:
                    pass
else:  # pragma: no cover
    View3D = None
