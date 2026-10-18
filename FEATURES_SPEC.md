# pc-app feature spec: images/PDF, drawing tools, bend tool, break/combine, anchor editing, nudge

Shared design for all implementation phases. Read the section for your phase **and** "Data model".
App: PySide6, `pc-app/`. Run from `pc-app/` with `python main.py`. Scene units are **mm**, Y down.
No pytest: tests are plain scripts `pc-app/tests/test_*.py` runnable with `python pc-app/tests/test_x.py`
(insert `pc-app` on `sys.path`, and use `QT_QPA_PLATFORM=offscreen` for any Qt test).

## Data model (core/doc.py): Phase A1

### Bezier anchors
`ControlPoint` gains two optional handles, **svg-local coords** (same frame as x/y):
```python
hin:  tuple | None = None   # control point of the segment ENDING at this anchor
hout: tuple | None = None   # control point of the segment STARTING at this anchor
```
Segment `i` goes `a=points[i] -> b=points[(i+1)%n]`. It is a cubic if and only if `a.hout or b.hin` is set.
A missing control defaults to its own anchor (c1 = a.hout or (a.x,a.y), c2 = b.hin or (b.x,b.y)).
Straight segments behave exactly as before. Handles are robust to inserting/removing anchors.

Serialization: `pts` entries stay `[x, y, t]` for plain points. When a handle exists, write
`[x, y, t, hin_or_null, hout_or_null]` (each handle `[hx, hy]` or `null`). `from_dict` accepts both lengths.

### Corner radius
`ControlPoint.radius` (mm on the mat, default 0, not scaled with the object, like thickness)
rounds the corner at that anchor with an arc when the path is flattened (`Document._seg_poly_u`,
so canvas, selection, bbox and cutting all follow). The stored anchor never moves and stays one
point. The arc ends lie at most a whole adjacent segment from the corner, or half of it when the
segment's other end is rounded too. Smooth joins and open-path ends are never rounded. Segment `i`
holds the second half of the arc at its start anchor and the first half of the arc at its end.
Set by the polygon / rect Corner radius option and by Point Properties. Serialized as a sixth
`pts` column (`[x, y, t, hin_or_null, hout_or_null, radius]`), written only when non-zero.

### Tabs
`VectorPath.tabs` holds `Tab(id, seg, pos, gap_mm)` entries. A tab is attached to anchor segment
`seg` at `pos`, a fraction (0..1) of that segment's flattened arc length. Its gap (mm on the mat,
not scaled with the object) is centered on the tab and measured along the line. The gap can reach
into neighbouring segments, wraps around a closed path and stops at an open path's ends
(`Document.tab_gaps`). `plan_ops_flat` lifts the tool over every gap, and the canvas draws the
gap like a dash gap (translucent line, solid pieces on top). A tab is drawn as a 1 px line across
the path with a selectable node (scene item kind `"tab"`, `setData(1, tab_id)`). It is selected
through `Selection.tabs` (`(path_id, tab_id)`), which is the fifth entry of `selection_state()`.
Path edits that renumber segments (`break_apart`, `delete_selection`) move each tab to its
segment's new index, and drop tabs whose segment was joined or removed. Serialized as a `tabs`
list on the path (`{"id", "seg", "pos", "gap"}`), written only when the path has tabs. Added
with the Tab tool and edited in Tab Properties (gap length).

### Geometry access (all callers must use the right one)
- `path_abs_points(path)`: **unchanged**. Anchors only, abs mm, `(x, y, thickness)`. Used for anchor
  overlays and point box-select.
- `segment_abs_poly(path, i, tol=0.05) -> [(x,y,t), ...]`: flattened segment incl. both endpoints,
  abs mm, thickness linearly interpolated along the curve parameter. Flatten in ABS space so `tol`
  is real mm regardless of `svg.scale`. A straight segment returns its two endpoints.
- `path_abs_flat(path, tol=0.05) -> [(x,y,t), ...]`: whole path flattened. For closed paths, do not
  repeat the start point (consumers already close back to it), but DO include the interior points of
  the closing segment.
- `segment_is_curve(path, i)`, `cubic_point(p0,c1,c2,p3,t)`, `nearest_t_on_segment(path, i, x, y) -> (t, dist_mm)`.
- `core/gcode.py plan_ops_flat` switches to `path_abs_flat`. `svg_bbox` / `selection_bbox` use flattened
  geometry (curves bulge beyond anchors). For path-less image objects, `svg_bbox` returns the image rect.

### Object kinds
`SvgInstance` gains (defaults keep old projects loading unchanged):
```python
kind: str = "svg"            # "svg" | "drawing" | "image" | "pdf"
source_path: str = ""        # absolute path for image/pdf
dpi: float = 300.0           # image: physical size, pdf: render resolution
px_w: int = 0; px_h: int = 0 # raster size at `dpi` (pdf: rendered size)
pdf_page: int = 0            # 0-based
outline_enabled: bool = False
outline_margin_mm: float = 0.0     # + grows outward, - shrinks
color_key_mode: str = "none"       # "none" | "auto" (corner pixel) | "custom"
color_key: str = "#FFFFFF"
color_key_tol: int = 32            # 0..255, max abs channel difference
outline_holes: bool = False
outline_color: str = "#000000"
```
Image local frame: the raster occupies `(0,0)-(w_mm,h_mm)` with `w_mm = px_w/dpi*25.4`, drawn at
offset with `scale` like any SVG. `image_size_mm(svg)` helper. `VectorPath` gains `generated: bool = False`
(outline paths, and regenerating replaces only generated paths of that object). Serialize all new fields
(short keys are acceptable) and keep loading of old files working.

### Doc operations (pure model, unit-tested)
- `selection_anchor_refs() -> set[(pid, idx)]`: points + both endpoints of selected segments + all anchors
  of selected paths (excluding paths whose svg is in `selection.svg_ids`).
- `move_anchors(refs, dx_mm, dy_mm)`: abs delta -> local (`/svg.scale`). Moves the anchor AND its hin/hout.
- `move_selection(dx_mm, dy_mm)`: svgs in `selection.svg_ids` move by offset, plus `move_anchors(selection_anchor_refs())`.
- `set_handle(pid, idx, which, abs_x, abs_y, mirror=True)`: `which` in `"in"|"out"`. If `mirror` and the
  opposite handle exists, rotate it to stay collinear (keep its length).
- `bend_segment(pid, i, t, dx_mm, dy_mm)`: make the segment cubic (if straight, c1/c2 start at the anchors)
  so the point at parameter t moves by (dx,dy). Clamp t to [0.05, 0.95]. With
  `k = 1 / (3*t*(1-t)*((1-t)**2 + t**2))`: `c1 += d*k*(1-t)`, `c2 += d*k*t` (d converted to local).
  This is exact for B(t) and is applied incrementally per drag tick.
- `straighten_segments(seg_refs)`: clear a.hout / b.hin for those segments.
- `break_apart() -> new_svg_id | None`: take selected geometry (whole selected paths, selected segments,
  and segments adjacent to selected points are NOT included, since points alone do not break anything) into a
  new `kind="drawing"` SvgInstance (offset 0,0 scale 1, points converted to abs coords, thickness kept,
  dash/color/closed kept, `generated=False`). For partially selected paths: split into runs of
  consecutive selected segments (new open paths) and runs of unselected segments (stay in the original
  svg as open paths, and a closed path with a selected run becomes one open path starting after the run).
  Delete svgs left empty. Select the new svg. Label "Part N".
- `combine() -> svg_id | None`: all selected paths (and all paths of selected svgs) move into ONE object:
  the first selected svg (by `doc.svgs` order) if any whole svg is selected and its kind is svg/drawing,
  else a new drawing. Convert points/handles into the target's local frame. Delete emptied svg/drawing
  objects (never delete image/pdf objects, because they keep their raster, but clear their generated flag on
  moved paths). Select the target svg.
- `delete_selection()`: delete selected svgs, remove selected paths, remove selected segments (splitting
  paths like break_apart) and remove selected points (joining neighbours with a straight segment, and paths with
  <2 anchors are removed). Delete svgs of kind svg/drawing that end up empty.
- `add_drawing(label=None) -> svg_id` (kind "drawing", label "Drawing N") and
  `add_drawn_path(svg_id, abs_anchors, closed, color) -> path_id` where `abs_anchors` is a list of
  `(x, y, hin|None, hout|None)` in abs mm (converted to the svg's local frame). Calls `ensure_color`.
- `add_image(kind, source_path, px_w, px_h, dpi, page=0, offset=(10,10)) -> svg_id` (label = basename,
  `p<page+1>` suffix for pdf), `set_outline_paths(svg_id, loops_local_mm)` (replace generated paths,
  color = `outline_color`, closed, `generated=True`).

`core/shapes.py` (new): `ellipse_anchors(cx,cy,rx,ry)` (4 cubic anchors, k=0.5522847498),
`regular_polygon(cx,cy,r,n,rot_rad)`, `rdp(points, tol)`, `flatten_cubic_abs` wrapper over
`core/flatten.flatten_cubic`, `constrain_angle(x0,y0,x,y,step_deg=15)`.

## Raster pipeline (core/raster.py): Phase A2 (independent of doc.py)
- `load_rgba(path) -> (np.uint8 HxWx4, dpi_or_None)` via Pillow (JPEG/PNG/BMP, EXIF transpose, DPI from
  `info["dpi"]` if >= 30 else None).
- `pdf_available() -> bool`, `pdf_page_count(path)`, `render_pdf(path, page, dpi) -> HxWx4` via optional
  `pypdfium2` (import lazily, raise `RuntimeError("PDF support needs: pip install pypdfium2")`). Render
  with a white background (fill white), so auto color key picks it up.
- `get_rgba(kind, path, page=0, dpi=None)`: small LRU cache keyed on (kind, path, mtime, page, dpi for pdf).
- `outline_loops(rgba, dpi, margin_mm=0, key_mode="none", key_color="#FFFFFF", key_tol=32,
  include_holes=False, simplify_mm=0.05, max_grid=1200) -> list[list[(x_mm, y_mm)]]` closed loops in
  image-local mm (origin top-left of image, `mm = px*25.4/dpi`).
  Foreground = alpha >= 128 AND not (key match: max |rgb - key| <= tol). key "auto" = most common of the
  4 corner colours. No alpha and key "none" -> whole image -> rectangle (± margin).
  Downsample the mask by an integer factor so the long side <= max_grid (a cell is foreground if any
  source pixel is). Pad by margin+2 cells. Dilate (margin>0) / erode (margin<0) by
  `round(|margin| / cell_mm)` iterations of alternating 4- and 8-neighbour numpy shift steps (octagon).
  Marching squares (numpy-vectorized case table, then stitch edges into loops in Python) at the 0.5
  iso-level. Orientation: outer loops vs holes by signed area. Drop holes unless include_holes, and drop loops
  with area < (2 cells)^2. Simplify with RDP at `simplify_mm`. Must handle 4000x3000 in < ~1 s.
- `rgba_to_qimage` must NOT be placed here (no Qt in core).

## Phase B1: canvas editing (features 3, 4, 5, 6)
- Draw curved segments correctly: `_draw_path` iterates segments using `segment_abs_poly` (one
  QPainterPath per segment or per thickness run, and keep `setData(0,pid) setData(1,seg_idx) setData(2,"seg")`).
  Dash overlay and toolpath preview must follow curves too.
- Bezier handles: for each selected anchor (and both ends of selected curved segments) draw its existing
  handles as small circles (kind `"bhandle"`, data(1)=(idx, "in"|"out")) with thin lines to the anchor.
  Dragging a bhandle calls `set_handle` (Alt = no mirror).
- Anchor/line dragging (mouse tool and transform tool): pressing on a point item, or on a seg item whose
  segment/path is selected, starts `_drag_kind="move_geom"`. Pressing an unselected point first selects it
  (Shift adds). Each tick: `doc.move_anchors(refs, dx, dy)`. If released with no movement, fall back to the
  existing click-select (keeps click cycling). Priority: resize handle > bhandle > point > selected seg >
  existing whole-SVG move > existing logic.
- Bend tool `"bend"` (new tool, icon e.g. iconpark `bezier-curve`/`curve-adjustment`): press on any seg
  item -> `nearest_t_on_segment`, then each drag tick `bend_segment(pid, i, t, dx, dy)` with the
  incremental delta. Works on straight and already-curved segments. Hover cursor: cross.
- Nudge: in the **mouse** tool, arrow keys call `doc.move_selection` by `canvas.nudge_mm`
  (Shift = x10), then emit `svg_moved`. Mouse tool options page gets a "Nudge" QDoubleSpinBox
  (mm, 0.01..100, default 1.0, 2 decimals) bound to `canvas.nudge_mm`. Canvas must keep focus for keys
  (setFocus on mouse press).
- Context menu (right-click in mouse/box/transform/bend tools): if right-clicking an unselected item,
  select it first. Actions: "Break apart into new SVG" (`break_apart`), "Combine into one SVG"
  (`combine`), "Straighten curve(s)" (only if selected segments include curves), separator, "Delete"
  (`delete_selection`). Disable actions that would have no effect. After any: emit `selection_changed`
  (main window refresh_all). Delete/Backspace key also calls `delete_selection` (any non-drawing tool).

## Phase B2: drawing tools (feature 2)
New tools `"line"`, `"freehand"`, `"curve"`, `"polygon"`, `"ellipse"` (+ icons, buttons, options pages).
- Draw session: `canvas._draw_svg_id`. The first shape after selecting a drawing tool while not in a
  session creates `doc.add_drawing()`. Later shapes go into the same object. Switching between drawing
  tools keeps the session, and switching to any non-drawing tool (or deleting that object) ends it.
- Shared draw option: stroke colour (colour swatch button -> QColorDialog, default #000000), shown on
  every drawing tool's options page and stored on the canvas.
- line: click to add vertices (straight polyline). Press-drag-release on the first click makes a single
  line. Double-click / Enter / right-click finishes, and clicking within 2 mm (screen-scaled: 8 px) of the
  first vertex closes. Shift constrains to 15°. Esc cancels. Live rubber-band preview.
- freehand: press-drag-release records points, simplified with `rdp` at the "Smoothing" option
  (mm, default 0.3). Closes if the end is within 8 px of the start.
- curve (pen): click = corner anchor, press-drag = smooth anchor (hout = drag point, hin mirrored).
  Finish / close / cancel like line. Live preview of the pending segment.
- polygon: drag from centre, with a "Sides" option (3..32, default 6). Shift snaps rotation to 15°.
- ellipse: drag a bounding box. Shift = circle, Ctrl = from centre. Uses `ellipse_anchors`.
- After each finished shape: emit `selection_changed` so panels/cut order refresh, and select the new path.
- Preview items use kind `"preview_draw"` and are not pickable.

## Phase B3: images and PDFs (feature 1)
- File menu "Import..." (replace "Open SVG..."): filter
  `"All supported (*.svg *.png *.jpg *.jpeg *.bmp *.pdf);;SVG (*.svg);;Images (*.png *.jpg *.jpeg *.bmp);;PDF (*.pdf)"`.
  SVG -> existing path. Image -> `raster.get_rgba`, dpi = file dpi or 300 -> `doc.add_image`.
  PDF -> page 0 at 150 dpi (multi-page: ask for page with QInputDialog if count > 1).
- Canvas draws image/pdf objects as a pixmap item (Z -1, above mat grid, below paths) with
  `setData(0, svg_id) setData(2, "image")`. Transform = offset + scale*25.4/dpi per pixel. Cache QImages
  (downsample to <= ~8 px/mm like the scan background). `_pick` returns image items at lowest priority.
  Clicking an image selects its object (`selection.svg_ids`) so the existing whole-object move/resize works.
  Hidden objects are not drawn, and ghost objects are drawn at 30% opacity.
- Properties card: the existing "SVG Properties" card retitles by selection: all svg/drawing ->
  "SVG Properties", all image -> "Image Properties", all pdf -> "PDF Properties", mixed -> "Object Properties".
  Keep X/Y/Scale + Apply. Add an image section (visible for image or pdf-only selections): DPI (image)
  or Render DPI + Page (pdf), size readout "W x H mm", "Cut outline" checkbox, Margin mm (-50..50, signed),
  Colour key combo (None / Auto (corner) / Custom) + colour swatch, Tolerance (0..255), "Include holes",
  Outline colour swatch. Changes apply immediately (on editingFinished/toggled) to all selected objects,
  then regenerate outlines via `raster.outline_loops` + `doc.set_outline_paths` (clear generated paths
  when outline is disabled), then emit `changed`. Changing DPI changes the physical size (local frame).
  Regenerate the outline afterwards.
- Save/load projects keeps image objects (source path), and outline paths regenerate on load if the file
  exists (else keep stored paths and show a status warning).
- Missing pypdfium2: import of a PDF shows a message box with the install hint. The app must still run.
- Add `pypdfium2>=4` to requirements.txt with a comment "(optional, PDF import)".
