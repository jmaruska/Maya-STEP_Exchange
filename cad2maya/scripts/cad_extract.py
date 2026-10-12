#!/usr/bin/env python3
"""
cad_extract.py - read STEP / IGES with OpenCascade and write Maya-ready NURBS data (JSON).

Runs OUTSIDE Maya (any Python 3.11-3.14 with `pip install cadquery-ocp==8.0.1.1.0 numpy`), e.g. the venv made by
setup_macos.sh. The Maya-side importer (maya_cad_import.py) reads the JSON, so Maya itself never loads OpenCascade.

Every face is converted to a clamped, non-periodic B-spline surface in MAYA's conventions:
  * knot vectors have (ncv + degree - 1) entries (the standard knot vector minus its first and last entry)
  * cvs[iu][iv] = [x, y, z]  (and weights[iu][iv] when the surface is rational)
  * surface parameter domain = [knots_u[0], knots_u[-1]] x [knots_v[0], knots_v[-1]]
Units are millimetres (OpenCascade's default for STEP and IGES); the Maya side scales them.

Faces whose boundary is exactly the surface's parameter rectangle are flagged trimmed=False; everything else is
trimmed=True (trim loops are exported for those in a later milestone).

Usage:  python cad_extract.py in.step -o out.json
"""
import os
import sys
import json
import argparse
import contextlib
import numpy as np

import cad_names

from OCP.collections import Sequence_TDF_Label, Array1_gp_Pnt2d, Array1_double, Array1_int
from OCP.Geom2d import Geom2d_BSplineCurve
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf, XCAFDoc_ColorGen
from OCP.TDataStd import TDataStd_Name
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.IGESCAFControl import IGESCAFControl_Reader
from OCP.IFSelect import IFSelect_RetDone
from OCP.TopExp import TopExp_Explorer, TopExp
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_REVERSED, TopAbs_VERTEX, TopAbs_SOLID, TopAbs_SHELL
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.Geom2d import Geom2d_TrimmedCurve
from OCP.Geom2dConvert import Geom2dConvert
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Pnt2d, gp_Vec2d
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_Line, GeomAbs_BSplineCurve, GeomAbs_C2
from OCP.ShapeAnalysis import ShapeAnalysis_Surface
from OCP.Geom2dAPI import Geom2dAPI_PointsToBSpline
from OCP.TopAbs import TopAbs_WIRE, TopAbs_IN
from matplotlib.path import Path as _MPath
from OCP.BRepAdaptor import BRepAdaptor_Curve2d
from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert

FORMAT = 'cad2maya/3'


@contextlib.contextmanager
def quiet():
    """Silence OpenCascade's very chatty C++ console output."""
    sys.stdout.flush()
    fd = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(fd, 1)
        os.close(fd)
        os.close(null)


# ------------------------------------------------------------------ reading the CAD file
def read_cad(path):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise FileNotFoundError('file not found: %s' % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.step', '.stp'):
        reader = STEPCAFControl_Reader()
    elif ext in ('.iges', '.igs'):
        reader = IGESCAFControl_Reader()
    else:
        raise ValueError('unsupported extension %r (use .step/.stp/.iges/.igs)' % ext)
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString('MDTV-XCAF'))
    app.InitDocument(doc)
    with quiet():
        status = reader.ReadFile(path)
        if status != IFSelect_RetDone:
            raise RuntimeError('could not read %s (status %s)' % (path, status))
        if not reader.Transfer(doc):
            raise RuntimeError('could not transfer %s into the document' % path)
    return doc


def _name(label):
    """The label's name, or None. IsAttribute() first: in this OpenCascade build FindAttribute() SEGFAULTS when the attribute is absent."""
    gid = TDataStd_Name.GetID_s()
    if label.IsNull() or not label.IsAttribute(gid):
        return None
    n = TDataStd_Name()
    if label.FindAttribute(gid, n):
        s = cad_names.repair_mojibake(n.Get().ToExtString())
        return s if s else None
    return None


def _color(ct, shape):
    for t in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
        c = Quantity_Color()
        try:
            if ct.GetColor(shape, t, c):
                return [round(c.Red(), 5), round(c.Green(), 5), round(c.Blue(), 5)]
        except Exception:
            pass
    return None


def _shape_key(sh):
    return sh.HashCode(2147483647) if hasattr(sh, 'HashCode') else hash(sh)


def _inherited_colors(ct, shape):
    """{face key: colour}. Exporters style different levels: some colour faces, this one (Autodesk Translation Framework) colours
    SOLIDS and SHELLS and never faces. So a face takes its own colour, else its shell's, else its solid's."""
    cmap = {}

    def faces_in(sh, avoid=None):
        ex = TopExp_Explorer(sh, TopAbs_FACE, avoid) if avoid is not None else TopExp_Explorer(sh, TopAbs_FACE)
        out = []
        while ex.More():
            out.append(ex.Current())
            ex.Next()
        return out

    def shells_in(sh, avoid=None):
        ex = TopExp_Explorer(sh, TopAbs_SHELL, avoid) if avoid is not None else TopExp_Explorer(sh, TopAbs_SHELL)
        out = []
        while ex.More():
            out.append(ex.Current())
            ex.Next()
        return out

    sx = TopExp_Explorer(shape, TopAbs_SOLID)
    while sx.More():
        solid = sx.Current()
        sc = _color(ct, solid)
        for shell in shells_in(solid):
            shc = _color(ct, shell) or sc
            for f in faces_in(shell):
                cmap[_shape_key(f)] = _color(ct, f) or shc
        for f in faces_in(solid):                                    # faces not reached through a shell
            cmap.setdefault(_shape_key(f), _color(ct, f) or sc)
        sx.Next()
    for shell in shells_in(shape, TopAbs_SOLID):                     # shells that belong to no solid
        shc = _color(ct, shell)
        for f in faces_in(shell):
            cmap.setdefault(_shape_key(f), _color(ct, f) or shc)
    for f in faces_in(shape, TopAbs_SHELL):                          # loose faces
        c = _color(ct, f)
        if c:
            cmap.setdefault(_shape_key(f), c)
    return cmap


class _Tree:
    """The product structure, one node per OCCURRENCE (so two instances of a sub-assembly stay two groups). Every node carries
    the original name and a Maya-safe, file-wide unique name (cad_names rules), so the JSON is clean before it reaches Maya."""

    def __init__(self):
        self.groups = []
        self.names = cad_names.NameRegistry()

    def add(self, kind, name, parent, product=None):
        gid = len(self.groups)
        fallback = 'unnamed_' + kind
        if kind == 'body' and parent is not None:
            fallback = self.groups[parent]['maya'] + '_body'          # an unnamed body is named after its part: Neck_1_body
        self.groups.append(dict(id=gid, parent=parent, kind=kind, name=name or '', product=product or '',
                                maya=self.names.make(name or product, fallback)))
        return gid


def _walk(st, label, loc, parent, out, tree, occ_name=None, path=()):
    """Flatten the XCAF tree into parts (leaf shapes, geometry already placed by the accumulated locations) while recording the
    assembly/part structure in `tree`. A component's OCCURRENCE name ('CoreWood:1') is preferred to its product name."""
    product = _name(label)
    name = cad_names.pick_name(occ_name, product)
    if st.IsAssembly_s(label):
        gid = tree.add('assembly', name, parent, product)
        comps = Sequence_TDF_Label()
        st.GetComponents_s(label, comps)
        for i in range(1, comps.Length() + 1):
            comp = comps.Value(i)
            ref = TDF_Label()
            if st.GetReferredShape_s(comp, ref):
                _walk(st, ref, loc.Multiplied(st.GetLocation_s(comp)), gid, out, tree, _name(comp), path + (name or 'assembly',))
    else:
        shape = st.GetShape_s(label)
        moved = shape.Moved(loc) if not loc.IsIdentity() else shape
        subs = Sequence_TDF_Label()                                 # named sub-shapes (bodies) inside the part, if the file has them
        st.GetSubShapes_s(label, subs)
        sub_names = []
        for i in range(1, subs.Length() + 1):
            nm = _name(subs.Value(i))
            if nm:
                sub_names.append((st.GetShape_s(subs.Value(i)), nm))
        gid = tree.add('part', name, parent, product)
        # colors are registered against the product's own (unmoved) shape, geometry must use the located one
        out.append(dict(name=name or 'part', product=product or '', path=list(path), shape=moved, color_shape=shape, group=gid, sub_names=sub_names))


def collect_parts(doc, tree=None):
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    free = Sequence_TDF_Label()
    st.GetFreeShapes(free)
    tree = tree if tree is not None else _Tree()
    parts = []
    for i in range(1, free.Length() + 1):
        _walk(st, free.Value(i), _identity_loc(), None, parts, tree)
    return parts, ct


def _identity_loc():
    from OCP.TopLoc import TopLoc_Location
    return TopLoc_Location()



# ------------------------------------------------------------------ trim loops
def _pcurve_segment(edge, face, reverse):
    """The edge's 2D curve (u, v) on the face as a clamped B-spline covering exactly the edge, in wire direction."""
    ad = BRepAdaptor_Curve2d(edge, face)
    t0, t1 = ad.FirstParameter(), ad.LastParameter()
    seg = Geom2dConvert.CurveToBSplineCurve_s(Geom2d_TrimmedCurve(ad.Curve(), t0, t1))
    if reverse:
        seg.Reverse()
    return seg


def _file_tolerance(face, floor=1e-4, factor=1.5):
    """The 3D tolerance the exporter itself declared for this face (largest edge / vertex tolerance), with some margin.
    Trim curves are only accurate to about this much, so UV gaps whose ends lie within it of each other in 3D are closed."""
    t = 0.0
    for kind, getter in ((TopAbs_EDGE, TopoDS.Edge), (TopAbs_VERTEX, TopoDS.Vertex)):
        ex = TopExp_Explorer(face, kind)
        while ex.More():
            t = max(t, BRep_Tool.Tolerance_s(getter(ex.Current())))
            ex.Next()
    return max(floor, factor * t)


def _make_line(a, b, t0=0.0, t1=1.0):
    poles = Array1_gp_Pnt2d(1, 2)
    poles.SetValue(1, gp_Pnt2d(float(a[0]), float(a[1])))
    poles.SetValue(2, gp_Pnt2d(float(b[0]), float(b[1])))
    knots = Array1_double(1, 2)
    knots.SetValue(1, t0)
    knots.SetValue(2, t1)
    mults = Array1_int(1, 2)
    mults.SetValue(1, 2)
    mults.SetValue(2, 2)
    return Geom2d_BSplineCurve(poles, knots, mults, 1)


def _straighten(seg, tol):
    """Some exporters encode a straight edge as a high-degree curve whose polygon oscillates (and may retrace itself or
    overshoot). If the curve is geometrically a straight segment, replace it with a clean 2-point line."""
    if seg.Degree() < 2:
        return seg
    a, b = _end_points(seg)
    chord = b - a
    length = float(np.linalg.norm(chord))
    if length < 1e-12:
        return seg
    t0, t1 = seg.FirstParameter(), seg.LastParameter()
    nrm = np.array([-chord[1], chord[0]]) / length
    for t in np.linspace(t0, t1, 120):
        q = seg.Value(t)
        if abs(float(np.dot(np.array([q.X(), q.Y()]) - a, nrm))) > tol:
            return seg
    return _make_line(a, b, t0, t1)


def _seg_data(seg, degree):
    if seg.Degree() < degree:
        seg.IncreaseDegree(degree)
    n = seg.NbPoles()
    pts = np.array([[seg.Pole(i + 1).X(), seg.Pole(i + 1).Y()] for i in range(n)])
    w = np.array([seg.Weight(i + 1) for i in range(n)]) if seg.IsRational() else np.ones(n)
    kn = np.array(_arr1(seg.KnotSequence()))
    return pts, w, kn


def merge_segments(segs):
    """Join consecutive 2D B-spline segments into ONE curve (C0 at the joins). Returns degree, flat knots, poles, weights."""
    degree = max(sg.Degree() for sg in segs)
    data = [_seg_data(sg, degree) for sg in segs]
    pts_all, w_all, kn_all = [], [], []
    start = 0.0
    for k, (pts, w, kn) in enumerate(data):
        length = float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1))) or 1.0
        kn = start + (kn - kn[0]) / (kn[-1] - kn[0]) * length              # consecutive parameter ranges
        start += length
        pts = pts.copy()
        w = w.copy()
        if k == 0:
            pts_all.append(pts)
            w_all.append(w)
            kn_all.append(kn)
        else:
            pts[0] = pts_all[-1][-1]                                      # shared junction point
            w = w * (w_all[-1][-1] / w[0])                                # keep homogeneous continuity
            pts_all.append(pts[1:])
            w_all.append(w[1:])
            junction = kn_all[-1][-1]
            kn_all[-1] = np.concatenate([kn_all[-1][:-(degree + 1)], [junction] * degree])
            kn_all.append(kn[degree + 1:])
    return degree, np.concatenate(kn_all), np.vstack(pts_all), np.concatenate(w_all)


def eval_loop(loop, t):
    """Evaluate a loop dict (Maya-convention knots) at parameter t -> (u, v). Independent Cox-de Boor."""
    p = loop['degree']
    kn = [loop['knots'][0]] + list(loop['knots']) + [loop['knots'][-1]]
    n = len(loop['cvs'])
    t = min(max(t, kn[p]), kn[n] - 1e-12)
    i = p
    while i < n - 1 and t >= kn[i + 1]:
        i += 1
    N = [1.0] + [0.0] * p
    left = [0.0] * (p + 1)
    right = [0.0] * (p + 1)
    for j in range(1, p + 1):
        left[j] = t - kn[i + 1 - j]
        right[j] = kn[i + j] - t
        saved = 0.0
        for r in range(j):
            tmp = N[r] / (right[r + 1] + left[j - r])
            N[r] = saved + right[r + 1] * tmp
            saved = left[j - r] * tmp
        N[j] = saved
    num = np.zeros(2)
    den = 0.0
    for a in range(p + 1):
        idx = i - p + a
        w = loop['weights'][idx] if loop.get('rational') else 1.0
        num += N[a] * w * np.array(loop['cvs'][idx])
        den += N[a] * w
    return num / den


def eval_loop_many(loop, ts):
    """eval_loop for many parameters at once (numpy, same Cox-de Boor recurrence, same knot conventions). Returns an (m, 2) array."""
    p = loop['degree']
    kn = np.array([loop['knots'][0]] + list(loop['knots']) + [loop['knots'][-1]], dtype=float)
    n = len(loop['cvs'])
    t = np.minimum(np.maximum(np.asarray(ts, dtype=float), kn[p]), kn[n] - 1e-12)
    i = np.clip(np.searchsorted(kn, t, side='right') - 1, p, n - 1)
    m = len(t)
    N = np.zeros((m, p + 1))
    N[:, 0] = 1.0
    left = np.zeros((m, p + 1))
    right = np.zeros((m, p + 1))
    for j in range(1, p + 1):
        left[:, j] = t - kn[i + 1 - j]
        right[:, j] = kn[i + j] - t
        saved = np.zeros(m)
        for r in range(j):
            tmp = N[:, r] / (right[:, r + 1] + left[:, j - r])
            N[:, r] = saved + right[:, r + 1] * tmp
            saved = left[:, j - r] * tmp
        N[:, j] = saved
    cvs = np.asarray(loop['cvs'], dtype=float)
    wts = np.asarray(loop['weights'], dtype=float) if loop.get('rational') else None
    num = np.zeros((m, 2))
    den = np.zeros(m)
    for a in range(p + 1):
        idx = i - p + a
        w = wts[idx] if wts is not None else 1.0
        num += (N[:, a] * w)[:, None] * cvs[idx]
        den += N[:, a] * w
    return num / den[:, None]


def sample_loop(loop, per_span=10):
    k = loop['knots']
    ts = []
    distinct = sorted(set(k))
    for a, b in zip(distinct[:-1], distinct[1:]):
        ts += list(np.linspace(a, b, per_span, endpoint=False))
    ts.append(distinct[-1])
    return eval_loop_many(loop, ts)


def signed_area(poly):
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _reverse_loop(L):
    k = L['knots']
    L['knots'] = [k[0] + k[-1] - v for v in reversed(k)]
    L['cvs'] = L['cvs'][::-1]
    if L.get('rational'):
        L['weights'] = L['weights'][::-1]


def _end_points(seg):
    a, b = seg.Pole(1), seg.Pole(seg.NbPoles())
    return np.array([a.X(), a.Y()]), np.array([b.X(), b.Y()])


def _chain_segments(segs, tol, eval3d=None, tol3d=1e-4):
    """Order AND orient the pcurve segments head-to-tail in (u, v) by matching end points. OpenCascade's orientation
    flags are not reliable for closed edges on periodic surfaces, so geometry decides.
    Returns (order, bridges): order = [(index, reversed)], bridges = {position: (p, q)} for UV gaps that had to be closed.
    A UV gap is only bridged when both its end points map to the same 3D point (it lies on a collapsed edge), so a bridge
    can never change the shape."""
    n = len(segs)
    ends = [_end_points(sg) for sg in segs]

    def head(j, rev):
        return ends[j][1] if rev else ends[j][0]

    def tail(j, rev):
        return ends[j][0] if rev else ends[j][1]

    for t in (tol, 1e3 * tol):                                            # 1) exact chaining
        for first_rev in (False, True):
            used = [False] * n
            used[0] = True
            order = [(0, first_rev)]
            cur = tail(0, first_rev)
            start = head(0, first_rev)
            while len(order) < n:
                best = None
                for j in range(n):
                    if used[j]:
                        continue
                    for rev in (False, True):
                        d = float(np.linalg.norm(head(j, rev) - cur))
                        if d <= t and (best is None or d < best[0]):
                            best = (d, j, rev)
                if best is None:
                    break
                _, j, rev = best
                used[j] = True
                order.append((j, rev))
                cur = tail(j, rev)
            if len(order) == n and float(np.linalg.norm(cur - start)) <= t:
                return order, {}
    if eval3d is None:
        return None
    best_plan = None                                                      # 2) greedy nearest chaining with bridged gaps
    for first_rev in (False, True):
        used = [False] * n
        used[0] = True
        order = [(0, first_rev)]
        bridges = {}
        total = 0.0
        cur = tail(0, first_rev)
        start = head(0, first_rev)
        while len(order) < n:
            best = None
            for j in range(n):
                if used[j]:
                    continue
                for rev in (False, True):
                    d = float(np.linalg.norm(head(j, rev) - cur))
                    if best is None or d < best[0]:
                        best = (d, j, rev)
            d, j, rev = best
            if d > tol:
                bridges[len(order) - 1] = (cur.copy(), head(j, rev).copy())
                total += d
            used[j] = True
            order.append((j, rev))
            cur = tail(j, rev)
        d = float(np.linalg.norm(cur - start))
        if d > tol:
            bridges[n - 1] = (cur.copy(), start.copy())
            total += d
        if all(float(np.linalg.norm(np.array(eval3d(*p)) - np.array(eval3d(*q)))) <= tol3d for p, q in bridges.values()):
            if best_plan is None or total < best_plan[0]:
                best_plan = (total, order, bridges)
    return (best_plan[1], best_plan[2]) if best_plan else None


def _sample_seg(seg, n=300):
    ts = np.linspace(seg.FirstParameter(), seg.LastParameter(), n)
    return ts, np.array([[seg.Value(t).X(), seg.Value(t).Y()] for t in ts])


def _first_crossing(PA, PB, skip_end=1e-9):
    """Proper crossings between two polylines; returns (ia, fa, ib, fb) of the one closest to A's end and B's start."""
    A0, A1, B0, B1 = PA[:-1][:, None, :], PA[1:][:, None, :], PB[:-1][None, :, :], PB[1:][None, :, :]
    r, q = A1 - A0, B1 - B0
    den = r[..., 0] * q[..., 1] - r[..., 1] * q[..., 0]
    with np.errstate(divide='ignore', invalid='ignore'):
        t = ((B0[..., 0] - A0[..., 0]) * q[..., 1] - (B0[..., 1] - A0[..., 1]) * q[..., 0]) / den
        u = ((B0[..., 0] - A0[..., 0]) * r[..., 1] - (B0[..., 1] - A0[..., 1]) * r[..., 0]) / den
    hit = (t > skip_end) & (t < 1 - skip_end) & (u > skip_end) & (u < 1 - skip_end) & (np.abs(den) > 1e-30)
    ii, jj = np.nonzero(hit)
    if len(ii) == 0:
        return None
    # the crossing that removes the least: closest to the end of A and to the start of B
    k = int(np.argmin([(len(PA) - 1 - i - t[i, j]) + (j + u[i, j]) for i, j in zip(ii, jj)]))
    i, j = ii[k], jj[k]
    return int(i), float(t[i, j]), int(j), float(u[i, j])


def _self_crossings(P):
    """Proper self-crossings of a polyline: list of (i, fi, j, fj) with i < j, non-adjacent segments."""
    A0, A1 = P[:-1][:, None, :], P[1:][:, None, :]
    B0, B1 = P[:-1][None, :, :], P[1:][None, :, :]
    r, q = A1 - A0, B1 - B0
    den = r[..., 0] * q[..., 1] - r[..., 1] * q[..., 0]
    with np.errstate(divide='ignore', invalid='ignore'):
        t = ((B0[..., 0] - A0[..., 0]) * q[..., 1] - (B0[..., 1] - A0[..., 1]) * q[..., 0]) / den
        u = ((B0[..., 0] - A0[..., 0]) * r[..., 1] - (B0[..., 1] - A0[..., 1]) * r[..., 0]) / den
    n = len(P) - 1
    idx = np.arange(n)
    hit = (t > 1e-9) & (t < 1 - 1e-9) & (u > 1e-9) & (u < 1 - 1e-9) & (np.abs(den) > 1e-30) & ((idx[None, :] - idx[:, None]) > 1)
    ii, jj = np.nonzero(hit)
    return [(int(i), float(t[i, j]), int(j), float(u[i, j])) for i, j in zip(ii, jj)]


def _trim_curls(seg, window=0.2, n=400):
    """A curve that curls back across its own path near its END (or START), returning to the junction point, carries a tiny
    loop. Cut the curve where it first reaches that point so the loop disappears. Returns True if it changed the curve."""
    ts, P = _sample_seg(seg, n)
    cr = _self_crossings(P)
    if not cr:
        return False
    changed = False
    # a loop containing the END: later crossing partner within the last `window` of the curve
    tail = [c for c in cr if c[2] >= (1 - window) * (n - 1)]
    if tail:
        i, fi, j, fj = min(tail, key=lambda c: c[0])                  # earliest start of such a loop -> removes the whole curl
        ta = ts[i] + fi * (ts[i + 1] - ts[i])
        try:
            seg.Segment(seg.FirstParameter(), ta)
            changed = True
        except Exception:
            pass
    ts, P = _sample_seg(seg, n)
    cr = _self_crossings(P)
    head = [c for c in cr if c[0] <= window * (n - 1)]
    if head:
        i, fi, j, fj = max(head, key=lambda c: c[2])                  # latest end of such a loop
        tb = ts[j] + fj * (ts[j + 1] - ts[j])
        try:
            seg.Segment(tb, seg.LastParameter())
            changed = True
        except Exception:
            pass
    return changed


def _nearest_on_polyline(ts, P, pt):
    """Closest point of a sampled curve to `pt`: (curve parameter, distance)."""
    A, B = P[:-1], P[1:]
    AB = B - A
    L2 = np.maximum((AB ** 2).sum(1), 1e-300)
    f = np.clip(((pt - A) * AB).sum(1) / L2, 0.0, 1.0)
    Q = A + f[:, None] * AB
    d = np.linalg.norm(Q - pt, axis=1)
    i = int(np.argmin(d))
    return float(ts[i] + f[i] * (ts[i + 1] - ts[i])), float(d[i])


def _trim_tjunctions(ordered, tol):
    """At a junction that still has a gap, one curve may simply run PAST the point where the next one begins (a T-junction: the
    next curve's start lies ON this curve's path, a little before its end). Cut the curve that overshoots at that point."""
    cut = set()
    n = len(ordered)
    for k in range(n):
        A, B = ordered[k], ordered[(k + 1) % n]
        if A is B:
            continue
        a_end = _end_points(A)[1]
        b_start = _end_points(B)[0]
        gap = float(np.linalg.norm(a_end - b_start))
        if gap <= 1e3 * tol:
            continue
        tsA, PA = _sample_seg(A)
        ta, dA = _nearest_on_polyline(tsA, PA, b_start)                # does B's start lie on A's path?
        tsB, PB = _sample_seg(B)
        tb, dB = _nearest_on_polyline(tsB, PB, a_end)                  # does A's end lie on B's path?
        try:
            if dA <= 0.1 * gap and ta < A.LastParameter() - 1e-9 * max(1.0, abs(A.LastParameter())) and dA <= dB:
                A.Segment(A.FirstParameter(), ta)
                cut.add(k)
            elif dB <= 0.1 * gap and tb > B.FirstParameter() + 1e-9 * max(1.0, abs(B.FirstParameter())):
                B.Segment(tb, B.LastParameter())
                cut.add(k)
        except Exception:
            pass
    return cut


def _trim_overshoots(ordered, tol=1e-7):
    """`ordered` = the wire's real curves, head-to-tail in loop order (no bridges). Where consecutive curves cross each other
    BEFORE their end points (the exporter let them run past the corner, or stopped just beyond it), cut both at the crossing.
    Returns the set of junction indices k (between ordered[k] and ordered[k+1]) that were cut."""
    if len(ordered) == 1:
        return set()          # one closed curve is a whole loop (a circle, or a folded polygon): nothing to trim at a junction, and curl-trimming would cut it
    for seg in ordered:
        _trim_curls(seg)
    cut = set()
    n = len(ordered)
    for k in range(n):
        A, B = ordered[k], ordered[(k + 1) % n]
        if A is B:
            continue
        tsA, PA = _sample_seg(A)
        tsB, PB = _sample_seg(B)
        hit = _first_crossing(PA, PB)
        if hit is None:
            continue
        ia, fa, ib, fb = hit
        ta = tsA[ia] + fa * (tsA[ia + 1] - tsA[ia])
        tb = tsB[ib] + fb * (tsB[ib + 1] - tsB[ib])
        try:
            A.Segment(A.FirstParameter(), ta)
            B.Segment(tb, B.LastParameter())
            cut.add(k)
        except Exception:
            pass
    cut |= _trim_tjunctions(ordered, tol)
    return cut


def _wire_edges(wire, face):
    """(edges, complete). BRepTools_WireExplorer walks edges by shared vertices and silently STOPS where they are not shared (seen:
    136 of 385 edges of one wire), which leaves a loop that can never close. If it comes up short, return every edge via
    TopExp_Explorer instead (unordered: geometry orders them later) and say so."""
    walked = []
    we = BRepTools_WireExplorer(wire, face)
    while we.More():
        walked.append(we.Current())
        we.Next()
    every = []
    ex = TopExp_Explorer(wire, TopAbs_EDGE)
    while ex.More():
        every.append(TopoDS.Edge(ex.Current()))
        ex.Next()
    if len(every) > len(walked):
        return every, False
    return walked, True


def _edge_ends3d(e):
    if BRep_Tool.Degenerated_s(e):
        p = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(e))
        return np.array(p.Coord()), np.array(p.Coord())
    ad = BRepAdaptor_Curve(e)
    return np.array(ad.Value(ad.FirstParameter()).Coord()), np.array(ad.Value(ad.LastParameter()).Coord())


def _order_edges_directed(edges):
    """[(edge index, reversed)] following each edge's OWN direction in the wire (OpenCascade stores that correctly even when the
    connectivity between edges is broken). Where several edges start at the current point (the two copies of a seam), the one that
    would immediately retrace the edge just walked is not taken. Jumps (no edge starts here) take the nearest start and are counted."""
    n = len(edges)
    ends = [_edge_ends3d(e) for e in edges]
    rev = [e.Orientation() == TopAbs_REVERSED for e in edges]
    start = [ends[i][1] if rev[i] else ends[i][0] for i in range(n)]
    stop = [ends[i][0] if rev[i] else ends[i][1] for i in range(n)]
    allp = np.array(start + stop)
    tol = max(1e-6, 1e-4 * float(np.linalg.norm(allp.max(0) - allp.min(0))))
    used = [False] * n
    used[0] = True
    order = [(0, rev[0])]
    cur, prev, jumps, gap = stop[0], 0, 0, 0.0
    while len(order) < n:
        cand = [(float(np.linalg.norm(start[j] - cur)), j) for j in range(n) if not used[j]]
        near = [c for c in cand if c[0] <= tol]
        if near:
            keep = [c for c in near if not edges[c[1]].IsSame(edges[prev])]   # do not retrace the edge just walked
            pick = min(keep or near)
        else:
            pick = min(cand)
            jumps += 1
            gap = max(gap, pick[0])
        j = pick[1]
        used[j] = True
        order.append((j, rev[j]))
        cur, prev = stop[j], j
    closing = float(np.linalg.norm(cur - start[order[0][0]]))
    return order, jumps, gap, closing


def _order_edges_3d(edges):
    """[(edge index, reversed)] chained head to tail by 3D end points (both start directions tried, the tighter chain wins)."""
    n = len(edges)
    ends = [_edge_ends3d(e) for e in edges]
    best = None
    for first_rev in (False, True):
        used = [False] * n
        used[0] = True
        order = [(0, first_rev)]
        cur = ends[0][0] if first_rev else ends[0][1]
        total = 0.0
        while len(order) < n:
            cand = None
            for j in range(n):
                if used[j]:
                    continue
                for rev in (False, True):
                    h = ends[j][1] if rev else ends[j][0]
                    d = float(np.linalg.norm(h - cur))
                    if cand is None or d < cand[0]:
                        cand = (d, j, rev)
            total += cand[0]
            used[cand[1]] = True
            order.append((cand[1], cand[2]))
            cur = ends[cand[1]][0] if cand[2] else ends[cand[1]][1]
        if best is None or total < best[0]:
            best = (total, order)
    return best[1]


def _wire_loop(wire, face, tol, eval3d=None, tol3d=1e-4, segs=None):
    """One wire -> one closed B-spline curve in (u, v), or None. `segs` = the wire's 2D curves if the caller already has them."""
    if segs is None:
        segs = []
        for e in _wire_edges(wire, face)[0]:
            segs.append(_straighten(_pcurve_segment(e, face, False), 1e-9 * max(tol * 1e7, 1.0)))   # natural direction; geometry decides below
    if not segs:
        return None
    plan = _chain_segments(segs, tol, eval3d, tol3d)
    if plan is None:
        raise RuntimeError('trim wire does not close in (u, v)')
    order, bridges = plan
    ordered = []
    for j, rev in order:
        if rev:
            segs[j].Reverse()
        ordered.append(segs[j])
    _trim_overshoots(ordered, tol)                              # cut overshooting curves at their true intersection first
    chained = []
    n_o = len(ordered)
    for pos, seg in enumerate(ordered):
        chained.append(seg)
        a_end = _end_points(seg)[1]
        b_start = _end_points(ordered[(pos + 1) % n_o])[0]
        if float(np.linalg.norm(a_end - b_start)) > 1e3 * tol:   # a gap that is STILL there after trimming: bridge it (3D-verified in the plan)
            chained.append(_make_line(a_end, b_start))
    degree, flat, pts, w = merge_segments(chained)
    gap = float(np.linalg.norm(pts[-1] - pts[0]))
    pts[-1] = pts[0]                                                      # close exactly
    w[-1] = w[0]
    rational = bool(np.max(np.abs(w - 1.0)) > 1e-12)
    L = dict(degree=degree, knots=[float(v) for v in flat[1:-1]], cvs=[[float(a), float(b)] for a, b in pts], rational=rational, closure_gap=gap)
    if rational:
        L['weights'] = [float(v) for v in w]
    return L


def trim_loops(face, du, dv, eval3d=None, tol3d=1e-4, wire_segs=None):
    """Loops for a face: outer first (counter-clockwise in (u, v)), then holes (clockwise).
    wire_segs = [(is_outer, [2D curves])] when the 2D curves were rebuilt from the 3D edges instead of read from the face."""
    outer_wire = BRepTools.OuterWire_s(face)
    loops, outer = [], None
    if wire_segs is not None:
        for is_outer, segs in wire_segs:
            L = _wire_loop(None, face, 1e-7 * max(du, dv, 1.0), eval3d, tol3d, segs)
            if L is not None:
                if is_outer:
                    outer = L
                else:
                    loops.append(L)
    we = TopExp_Explorer(face, TopAbs_WIRE) if wire_segs is None else None
    while we is not None and we.More():
        w = TopoDS.Wire(we.Current())
        L = _wire_loop(w, face, 1e-7 * max(du, dv, 1.0), eval3d, tol3d)
        if L is not None:
            if w.IsSame(outer_wire):
                outer = L
            else:
                loops.append(L)
        we.Next()
    if outer is None:
        raise RuntimeError('face has no outer wire')
    for L, want_ccw in [(outer, True)] + [(h, False) for h in loops]:
        a = signed_area(sample_loop(L))
        if (a > 0) != want_ccw:
            _reverse_loop(L)
    return [outer] + loops


def _loop_is_rect(loop, u0, u1, v0, v1):
    tol = 1e-6 * max(u1 - u0, v1 - v0, 1.0)
    P = sample_loop(loop, 24)
    return bool(np.all(np.minimum.reduce([abs(P[:, 0] - u0), abs(P[:, 0] - u1), abs(P[:, 1] - v0), abs(P[:, 1] - v1)]) <= tol))


def clamp_loops(loops, u0, u1, v0, v1, max_rel=0.15):
    """Maya needs boundaries INSIDE the surface domain. A curve lies inside the hull of its control points, so clamping the
    control points into the domain guarantees it. Only done for small overshoots (reported); returns the largest one."""
    size = max(u1 - u0, v1 - v0)
    worst = 0.0
    for L in loops:
        for c in L['cvs']:
            over = max(u0 - c[0], c[0] - u1, v0 - c[1], c[1] - v1, 0.0)
            worst = max(worst, over)
    if worst > 1e-9 * size:
        # only the part of the polygon that actually leaves the domain matters: measure the curve itself first
        curve_over = 0.0
        for L in loops:
            P = sample_loop(L, 24)
            curve_over = max(curve_over, u0 - P[:, 0].min(), P[:, 0].max() - u1, v0 - P[:, 1].min(), P[:, 1].max() - v1, 0.0)
        if curve_over > 1e-9 * size:
            if curve_over > max_rel * size:
                raise RuntimeError('trim loop leaves the surface domain by %.3g (%.0f%% of its size)' % (curve_over, 100 * curve_over / size))
            for L in loops:
                L['cvs'] = [[min(max(c[0], u0), u1), min(max(c[1], v0), v1)] for c in L['cvs']]
                L['cvs'][-1] = list(L['cvs'][0])
            return curve_over
    return 0.0


# ------------------------------------------------------------------ trim curves rebuilt from the 3D edges
def _edge_points(edge):
    """Points along an edge's 3D curve, in the curve's own direction (denser for curves with many spans)."""
    ad = BRepAdaptor_Curve(edge)
    t0, t1 = ad.FirstParameter(), ad.LastParameter()
    kind = ad.GetType()
    if kind == GeomAbs_Line:
        n = 3
    elif kind == GeomAbs_BSplineCurve:
        n = int(min(240, max(24, 8 * (ad.NbKnots() - 1))))
    else:
        n = 64
    return [ad.Value(t) for t in np.linspace(t0, t1, n)]


def _make_polyline2d(pts):
    n = len(pts)
    poles = Array1_gp_Pnt2d(1, n)
    for i, (u, v) in enumerate(pts):
        poles.SetValue(i + 1, gp_Pnt2d(float(u), float(v)))
    knots = Array1_double(1, n)
    mults = Array1_int(1, n)
    for i in range(n):
        knots.SetValue(i + 1, float(i))
        mults.SetValue(i + 1, 2 if i in (0, n - 1) else 1)
    return Geom2d_BSplineCurve(poles, knots, mults, 1)


def _fit2d(pts, size):
    """A smooth 2D curve through projected points (ends exact). Falls back to a polyline if fitting fails."""
    P = [pts[0]]
    for p in pts[1:]:
        if np.hypot(p[0] - P[-1][0], p[1] - P[-1][1]) > 1e-12 * max(size, 1.0):
            P.append(p)
    if len(P) < 2:
        return None
    if len(P) == 2:
        return _make_line(np.array(P[0]), np.array(P[1]))
    try:
        arr = Array1_gp_Pnt2d(1, len(P))
        for i, (u, v) in enumerate(P):
            arr.SetValue(i + 1, gp_Pnt2d(float(u), float(v)))
        fit = Geom2dAPI_PointsToBSpline(arr, 3, 8, GeomAbs_C2, 1e-6 * max(size, 1.0)).Curve()
        a, b = _end_points(fit)
        if np.hypot(a[0] - P[0][0], a[1] - P[0][1]) < 1e-6 * max(size, 1.0) and np.hypot(b[0] - P[-1][0], b[1] - P[-1][1]) < 1e-6 * max(size, 1.0):
            return fit
    except Exception:
        pass
    return _make_polyline2d(P)


def _reverse_pieces(pieces):
    return [list(reversed(p)) for p in reversed(pieces)]


def _split_at_window_edge(pieces, D, edge):
    """Walk `pieces` (point lists, increasing in coordinate D) and cut at the first point reaching coordinate `edge`.
    Returns (before, after): `before` ends exactly ON the edge, `after` starts exactly on it. `after` is empty if the walk ends there."""
    before = []
    for k, piece in enumerate(pieces):
        for i, p in enumerate(piece):
            if p[D] >= edge - 1e-9 * max(1.0, abs(edge)):
                if i == 0 and k == 0:
                    return [], pieces
                if i == 0:
                    return pieces[:k], pieces[k:]
                a, c = piece[i - 1], piece[i]
                t = (edge - a[D]) / (c[D] - a[D]) if c[D] != a[D] else 1.0
                x = [a[0] + t * (c[0] - a[0]), a[1] + t * (c[1] - a[1])]
                x[D] = edge
                head = piece[:i] + [x]
                tail = [list(x)] + piece[i:]
                before = pieces[:k] + [head]
                after = [tail] + pieces[k + 1:]
                if len(tail) < 2 or np.hypot(tail[0][0] - tail[1][0], tail[0][1] - tail[1][1]) < 1e-12:
                    after = pieces[k + 1:]
                return before, after
    return pieces, []


def _merge_wrapping(A, B, pu, pv):
    """Two loops that each run ONCE around a periodic surface (a band between two circles, with no seam edge in the file).
    In the unrolled (u, v) plane neither closes. The face is the strip between them: cut B where it crosses the window edge, then
    close the loop with a seam segment on each side. Returns the pieces of the one resulting loop."""
    D = 0 if A['wrap'][0] else 1
    P = pu if D == 0 else pv
    if B['wrap'][D] == 0 or abs(A['wrap'][D]) != 1 or abs(B['wrap'][D]) != 1 or A['wrap'][1 - D] or B['wrap'][1 - D]:
        raise RuntimeError('two wrapping loops that do not wrap the same way')
    for W in (A, B):
        if W['wrap'][D] < 0:                                         # make both run in the increasing direction
            W['pieces'] = _reverse_pieces(W['pieces'])
            W['wrap'] = (-W['wrap'][0], -W['wrap'][1])
    L = A['pieces'][0][0][D]                                         # the window [L, L+P] starts where A starts, so A spans it exactly
    k = int(np.floor((B['pieces'][0][0][D] - L) / P + 1e-9))
    bp = [[[q[0] - (k * P if D == 0 else 0.0), q[1] - (k * P if D == 1 else 0.0)] for q in piece] for piece in B['pieces']]
    right = L + P
    before, after = _split_at_window_edge(bp, D, right)
    shifted = [[[q[0] - (P if D == 0 else 0.0), q[1] - (P if D == 1 else 0.0)] for q in piece] for piece in after]
    a_start, a_end = A['pieces'][0][0], A['pieces'][-1][-1]
    other = 1 - D
    # B traversed backwards, starting from the right-hand window edge
    back = _reverse_pieces(before) + _reverse_pieces(shifted)
    first_pt = back[0][0] if back else None
    last_pt = back[-1][-1] if back else None
    if first_pt is None:
        raise RuntimeError('second loop is empty')
    seam_r = [list(a_end), list(first_pt)]
    seam_l = [list(last_pt), list(a_start)]
    seam_r[1][D] = right
    seam_r[0][D] = right
    seam_l[0][D] = L
    seam_l[1][D] = L
    pieces = [list(p) for p in A['pieces']]
    pieces[-1][-1] = list(seam_r[0])
    back[0][0] = list(seam_r[1])
    back[-1][-1] = list(seam_l[0])
    pieces[0][0] = list(seam_l[1])
    return pieces + [seam_r] + back + [seam_l]


def _simplify_polyline(P, tol):
    """Ramer-Douglas-Peucker for a closed or open polyline (iterative): keeps the shape within `tol`, drops the dense samples."""
    P = np.asarray(P, dtype=float)
    n = len(P)
    if n < 4:
        return P
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg = P[b] - P[a]
        L = np.hypot(seg[0], seg[1])
        d = P[a + 1:b] - P[a]
        dist = np.abs(seg[0] * d[:, 1] - seg[1] * d[:, 0]) / L if L > 0 else np.hypot(d[:, 0], d[:, 1])
        k = int(np.argmax(dist))
        if dist[k] > tol:
            m = a + 1 + k
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return P[keep]


def _fold_into_window(recs, pu, pv, b):
    """A face on a periodic surface can have a boundary that reaches past the seam: the unrolled loop is wider than one period. The part
    beyond the window is the same strip as the opposite side, so cut the region at the window edge, move the overflow by one period and
    merge (OpenCascade 2D booleans on planar faces). Returns the loops as polylines: [(is_outer, [points])], or raises if the result is
    not one region."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace as MF, BRepBuilderAPI_Transform
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Fuse
    from OCP.gp import gp_Trsf, gp_Vec, gp_Pnt
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.ShapeFix import ShapeFix_Face
    wu0, wu1 = (b[0], b[0] + pu) if pu else (b[0], b[1])
    wv0, wv1 = (b[2], b[2] + pv) if pv else (b[2], b[3])

    def poly_wire(P):
        mp = BRepBuilderAPI_MakePolygon()
        for u, v in P[:-1] if np.allclose(P[0], P[-1]) else P:
            mp.Add(gp_Pnt(float(u), float(v), 0.0))
        mp.Close()
        return mp.Wire()

    loops = []
    scale = max(wu1 - wu0, wv1 - wv0)
    for r in recs:
        pts = [q for p in r['pieces'] for q in p]
        loops.append((r['is_outer'], _simplify_polyline(pts, 2e-6 * scale)))      # the boolean does not need the dense samples
    outer = next(P for o, P in loops if o)
    mk = MF(poly_wire(outer), True)
    for o, P in loops:
        if not o:
            mk.Add(poly_wire(P))
    f = ShapeFix_Face(mk.Face())
    f.FixOrientation()
    f.Perform()
    region = f.Result()
    window = MF(poly_wire(np.array([[wu0, wv0], [wu1, wv0], [wu1, wv1], [wu0, wv1], [wu0, wv0]])), True).Face()
    pieces = []
    for ku in ((-1, 0, 1) if pu else (0,)):
        for kv in ((-1, 0, 1) if pv else (0,)):
            tr = gp_Trsf()
            tr.SetTranslation(gp_Vec(ku * (pu or 0.0), kv * (pv or 0.0), 0.0))
            moved = BRepBuilderAPI_Transform(region, tr, True).Shape()
            com = BRepAlgoAPI_Common(moved, window)
            if com.IsDone() and _faces_of_shape(com.Shape()):
                pieces.append(com.Shape())
    if not pieces:
        raise RuntimeError('folding left no region inside the window')
    acc = pieces[0]
    for p in pieces[1:]:
        fu = BRepAlgoAPI_Fuse(acc, p)
        if not fu.IsDone():
            raise RuntimeError('folding could not merge the pieces')
        acc = fu.Shape()
    un = ShapeUpgrade_UnifySameDomain(acc, True, True, True)
    un.Build()
    acc = un.Shape()
    faces = _faces_of_shape(acc)
    if len(faces) != 1:
        raise RuntimeError('folding gave %d separate regions' % len(faces))
    out = []
    outer_w = BRepTools.OuterWire_s(faces[0])
    we = TopExp_Explorer(faces[0], TopAbs_WIRE)
    while we.More():
        w = TopoDS.Wire(we.Current())
        we.Next()
        pts = []
        wx = BRepTools_WireExplorer(w, faces[0])
        while wx.More():
            e = wx.Current()
            wx.Next()
            a, c = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(e, True)), BRep_Tool.Pnt_s(TopExp.LastVertex_s(e, True))
            if not pts:
                pts.append([a.X(), a.Y()])
            pts.append([c.X(), c.Y()])
        out.append((w.IsSame(outer_w), pts))
    return out, (wu0, wu1, wv0, wv1)


def _faces_of_shape(shape):
    out = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        out.append(TopoDS.Face(ex.Current()))
        ex.Next()
    return out


def _wire_sequences(w, f2, planar):
    """The wire as one or more closed sequences of (edge, reversed). On a flat, non-periodic surface an edge walked once in each direction
    is a SLIT (zero width): it contributes nothing to the region, so both walks are dropped and what remains is split into ordinary
    closed loops (two circles joined by a slit become two circles). Periodic surfaces keep doubled edges: those are seams."""
    edges, complete = _wire_edges(w, f2)
    if complete:
        seq = [(e, e.Orientation() == TopAbs_REVERSED) for e in edges]
    else:
        seq = [(edges[j], rev) for j, rev in _order_edges_directed(edges)[0]]
    if not planar:
        return [seq]
    drop = set()
    for i in range(len(seq)):
        if i in drop:
            continue
        for j in range(i + 1, len(seq)):
            if j not in drop and seq[i][1] != seq[j][1] and seq[i][0].IsSame(seq[j][0]):
                drop.update((i, j))
                break
    if not drop:
        return [seq]
    seq = [x for k, x in enumerate(seq) if k not in drop]
    if not seq:
        return []

    def ends(x):
        a, c = _edge_ends3d(x[0])
        return (c, a) if x[1] else (a, c)

    pts = np.array([q for x in seq for q in ends(x)])
    tol = max(1e-6, 1e-4 * float(np.linalg.norm(pts.max(0) - pts.min(0))))
    groups, cur = [], []
    for x in seq:
        if cur and float(np.linalg.norm(ends(x)[0] - ends(cur[-1])[1])) > tol:
            groups.append(cur)
            cur = []
        cur.append(x)
    groups.append(cur)
    return groups


def _project_wires(f2, tol3d=1e-4):
    """Rebuild the face's trim loops from its 3D EDGES, ignoring OpenCascade's own 2D curves. A STEP file holds no 2D curves at all
    (OpenCascade invents them by projecting), and on closed/periodic surfaces that projection can pick the wrong period or reuse one
    seam curve for both sides. Here every edge is sampled, each point is projected while tracking the previous one (so a point a
    whole period away is unwrapped to the right period), and the loop is joined by the wire's own connectivity.
    Two loops that each run once around a periodic surface (a band; the file has no seam edge) are joined into one loop through
    seam segments. Returns ([(is_outer, [2D curves in wire order])], (u0, u1, v0, v1))."""
    surf = BRep_Tool.Surface_s(f2)
    sas = ShapeAnalysis_Surface(surf)
    b = surf.Bounds()
    # A direction that is merely CLOSED (its first and last iso-curves coincide) but not flagged periodic, like a tube, still has a seam:
    # wrap it at its own domain width.
    pu = surf.UPeriod() if surf.IsUPeriodic() else ((b[1] - b[0]) if surf.IsUClosed() else None)
    pv = surf.VPeriod() if surf.IsVPeriodic() else ((b[3] - b[2]) if surf.IsVClosed() else None)
    size = max(b[1] - b[0], b[3] - b[2])
    outer = BRepTools.OuterWire_s(f2)
    recs = []
    we = TopExp_Explorer(f2, TopAbs_WIRE)
    work = []
    while we.More():
        w = TopoDS.Wire(we.Current())
        we.Next()
        for sequence in _wire_sequences(w, f2, not (pu or pv)):
            work.append((w, sequence))
    for w, sequence in work:
        pieces, prev = [], None
        for e, rev in sequence:
            if BRep_Tool.Degenerated_s(e):
                pieces.append(None)                                          # a collapsed edge (pole): joined by a straight line below
                continue
            P3 = _edge_points(e)
            if rev:
                P3 = P3[::-1]
            uvs = []
            for P in P3:
                uv = sas.ValueOfUV(P, 1e-6) if prev is None else sas.NextValueOfUV(prev, P, 1e-6)
                u, v = uv.X(), uv.Y()
                if prev is not None:
                    if pu:
                        u += round((prev.X() - u) / pu) * pu                     # unwrap to the period nearest the previous point
                    if pv:
                        v += round((prev.Y() - v) / pv) * pv
                prev = gp_Pnt2d(u, v)
                uvs.append([u, v])
            if pieces and pieces[-1]:
                uvs[0] = list(pieces[-1][-1])                                    # one edge starts exactly where the last one ended
            pieces.append(uvs)
        real = [p for p in pieces if p]
        if not real:
            continue
        first, last = real[0][0], real[-1][-1]
        gu, gv = last[0] - first[0], last[1] - first[1]
        wrap = (0, 0)
        open_wire = False
        open_gap = None
        if np.hypot(gu, gv) > 1e-3 * max(size, 1.0):
            ku = int(round(gu / pu)) if pu else 0
            kv = int(round(gv / pv)) if pv else 0
            if (ku or kv) and abs(gu - ku * (pu or 0.0)) < 1e-3 * size and abs(gv - kv * (pv or 0.0)) < 1e-3 * size:
                wrap = (ku, kv)
            else:
                # Not closed in the parameter plane. Judge it in 3D, where it matters: edges that miss each other by a hair (the file's
                # own tolerance is often exceeded by small amounts) still bound the same face. The accepted gap scales with the face.
                open_wire = True
                def at(uv):
                    u, v = uv
                    return np.array(surf.Value(u if pu else min(max(u, b[0]), b[1]), v if pv else min(max(v, b[2]), b[3])).Coord())
                allp = np.array([at(q) for p in real for q in p[::max(1, len(p) // 6)]])
                diag = float(np.linalg.norm(allp.max(0) - allp.min(0)))
                gap3 = float(np.linalg.norm(at(last) - at((first[0] + ku * (pu or 0.0), first[1] + kv * (pv or 0.0)))))
                if gap3 <= max(20.0 * tol3d, 1e-4 * diag):
                    wrap = (ku, kv)
                    open_wire = False
                else:
                    open_gap = (np.hypot(gu, gv), gap3)
        pole_closes = False
        if not open_wire and wrap != (0, 0) and sum(1 for q in pieces if q is None) == 1:
            # A POLE (collapsed edge) has no 3D curve to project, but in (u, v) it is a straight line spanning the whole period, so the
            # one-period gap at the wire's closure belongs to it. Put the jump at the pole: everything after it moves back by the period.
            idx = next(i for i, q in enumerate(pieces) if q is None)
            if 0 < idx < len(pieces) - 1:
                sh = (-wrap[0] * (pu or 0.0), -wrap[1] * (pv or 0.0))
                for q in pieces[idx + 1:]:
                    if q:
                        for pt in q:
                            pt[0] += sh[0]
                            pt[1] += sh[1]
                first, last = real[0][0], real[-1][-1]
            else:
                pole_closes = True                                                # the pole sits at the wire's start/end: it closes the loop itself
            wrap = (0, 0)
        if not open_wire and not pole_closes:
            real[-1][-1] = [first[0] + wrap[0] * (pu or 0.0), first[1] + wrap[1] * (pv or 0.0)]
        for i, p in enumerate(pieces):                                         # collapsed edges become straight lines between their neighbours
            if p is None:
                a = next(q for q in reversed(pieces[:i]) if q)[-1] if any(pieces[:i]) else real[-1][-1]
                c = next(q for q in pieces[i + 1:] if q)[0] if any(pieces[i + 1:]) else real[0][0]
                pieces[i] = [list(a), list(c)]
        recs.append(dict(is_outer=w.IsSame(outer), pieces=pieces, wrap=wrap, open=open_wire, open_gap=open_gap if open_wire else None))
    if not recs:
        raise RuntimeError('rebuilt face has no loops')
    ext = lambda r: np.ptp(np.array([q for p in r['pieces'] for q in p])[:, 0]) * np.ptp(np.array([q for p in r['pieces'] for q in p])[:, 1])
    big = max(recs, key=ext)                                                   # OpenCascade's own choice rests on the 2D curves that failed
    for r in recs:
        r['is_outer'] = r is big or (r['wrap'] != (0, 0) and big['wrap'] != (0, 0) and r is not big and False)
    partial = False
    opened = [r for r in recs if r['open']]
    if opened:
        if len(opened) == 1 and opened[0] is big and len(recs) > 1:
            # The OUTER boundary could not be rebuilt but the inner ones (the holes) can: keep the holes, and let the surface's own
            # edge stand in for the outer boundary. Flagged to the caller as a partial trim.
            partial = True
            recs = [r for r in recs if not r['open']]
            for r in recs:
                r['is_outer'] = False
        else:
            raise RuntimeError('rebuilt trim loop does not close (off by %.3g in (u, v), %.3g in 3D)' % opened[0]['open_gap'])
    wrapping = [r for r in recs if r['wrap'] != (0, 0)]
    if wrapping:
        if len(wrapping) != 2:
            raise RuntimeError('%d loops run around the periodic surface (a band needs exactly 2)' % len(wrapping))
        A = next(r for r in wrapping if r['is_outer'])
        B = next(r for r in wrapping if not r['is_outer'])
        merged = _merge_wrapping(A, B, pu, pv)
        recs = [dict(is_outer=True, pieces=merged, wrap=(0, 0))] + [r for r in recs if r['wrap'] == (0, 0) and not r['is_outer']]
    if partial:
        # the window is the surface's own parameter range (one period in a periodic direction); the holes are moved into it
        wu0, wu1 = (b[0], b[0] + pu) if pu else (b[0], b[1])
        wv0, wv1 = (b[2], b[2] + pv) if pv else (b[2], b[3])
        corners = [[wu0, wv0], [wu1, wv0], [wu1, wv1], [wu0, wv1]]
        rect = [[list(corners[i]), list(corners[(i + 1) % 4])] for i in range(4)]
        for r in recs:
            P = np.array([q for p in r['pieces'] for q in p])
            cu, cv = P[:, 0].mean(), P[:, 1].mean()
            ku = -int(np.floor((cu - wu0) / pu)) if pu else 0
            kv = -int(np.floor((cv - wv0) / pv)) if pv else 0
            if ku or kv:
                r['pieces'] = [[[q[0] + ku * (pu or 0.0), q[1] + kv * (pv or 0.0)] for q in p] for p in r['pieces']]
        recs = [dict(is_outer=True, pieces=rect, wrap=(0, 0), open=False, open_gap=None)] + recs
    # holes must sit in the same period as the outer loop
    outer_poly = np.array([q for p in next(r for r in recs if r['is_outer'])['pieces'] for q in p])
    path = _MPath(outer_poly)
    for r in recs:
        if r['is_outer']:
            continue
        if not (pu or pv):
            if not path.contains_point(tuple(r['pieces'][0][0])):
                raise RuntimeError('rebuilt loops are not nested (the face would be several separate regions)')
            continue
        p0 = r['pieces'][0][0]
        for ku in ((0, -1, 1) if pu else (0,)):
            for kv in ((0, -1, 1) if pv else (0,)):
                if path.contains_point((p0[0] + ku * (pu or 0.0), p0[1] + kv * (pv or 0.0))):
                    if ku or kv:
                        r['pieces'] = [[[q[0] + ku * (pu or 0.0), q[1] + kv * (pv or 0.0)] for q in p] for p in r['pieces']]
                    break
            else:
                continue
            break
    allp0 = np.array([q for r in recs for p in r['pieces'] for q in p])
    over_u = bool(pu) and np.ptp(allp0[:, 0]) > pu * (1 + 1e-3)
    over_v = bool(pv) and np.ptp(allp0[:, 1]) > pv * (1 + 1e-3)
    if (over_u or over_v) and not partial:
        folded, window = _fold_into_window(recs, pu, pv, b)
        out = [(o, [_make_polyline2d(P)]) for o, P in folded]
        return out, window, False
    out, allpts = [], []
    for r in recs:
        segs = [sg for sg in (_fit2d(p, size) for p in r['pieces']) if sg is not None]
        out.append((r['is_outer'], segs))
        for p in r['pieces']:
            allpts += p
    A = np.array(allpts)
    u0, u1, v0, v1 = A[:, 0].min(), A[:, 0].max(), A[:, 1].min(), A[:, 1].max()
    if not pu:
        u0, u1 = max(u0, b[0]), min(u1, b[1])
    if not pv:
        v0, v1 = max(v0, b[2]), min(v1, b[3])
    if partial:
        u0, u1, v0, v1 = wu0, wu1, wv0, wv1
    return out, (float(u0), float(u1), float(v0), float(v1)), partial


def _probes_from_loops(loops, u0, u1, v0, v1, n=12):
    """Probe points classified by the loops themselves (used when OpenCascade's own 2D curves for the face cannot be trusted)."""
    polys = [sample_loop(L, 24) for L in loops]
    paths = [_MPath(P) for P in polys]
    margin = 0.02 * max(u1 - u0, v1 - v0)
    out, info = [], []
    for fu in np.linspace(0.04, 0.96, n):
        for fv in np.linspace(0.04, 0.96, n):
            u, v = u0 + fu * (u1 - u0), v0 + fv * (v1 - v0)
            if min(float(np.min(np.linalg.norm(P - np.array([u, v]), axis=1))) for P in polys) < margin:
                continue
            if not paths[0].contains_point((u, v)):
                out.append([u, v, 0])
                info.append([0, 0])
            else:
                hole = next((k for k in range(1, len(paths)) if paths[k].contains_point((u, v))), 0)
                out.append([u, v, 0 if hole else 1])
                info.append([2, hole] if hole else [1, 0])
    return out, info


def _probes(face, loops, u0, u1, v0, v1, n=12):
    """(u, v, inside) samples classified by OpenCascade, away from the boundary, for verification inside Maya. Also returns
    info[i] = [kind, hole]: kind 0 = outside the outer loop, 1 = inside the region, 2 = inside hole number `hole` (1-based)."""
    polys = [sample_loop(L, 24) for L in loops]
    paths = [_MPath(P) for P in polys]
    margin = 0.02 * max(u1 - u0, v1 - v0)
    out, info = [], []
    for fu in np.linspace(0.04, 0.96, n):
        for fv in np.linspace(0.04, 0.96, n):
            u, v = u0 + fu * (u1 - u0), v0 + fv * (v1 - v0)
            near = min(float(np.min(np.linalg.norm(P - np.array([u, v]), axis=1))) for P in polys)
            if near < margin:
                continue                                                  # too close to a boundary to be a fair test
            st = BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State()
            out.append([u, v, 1 if st == TopAbs_IN else 0])
            if not paths[0].contains_point((u, v)):
                info.append([0, 0])
            else:
                hole = next((k for k in range(1, len(paths)) if paths[k].contains_point((u, v))), 0)
                info.append([2, hole] if hole else [1, 0])
    return out, info


def _face_area(face):
    """Area from a triangulation (OpenCascade's BRepGProp integrator returns nonsense for faces with collapsed poles)."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopLoc import TopLoc_Location
    BRepMesh_IncrementalMesh(face, 0.01, True, 0.3, False)
    tri = BRep_Tool.Triangulation_s(face, TopLoc_Location())
    if tri is None:
        return 0.0
    P = np.array([[tri.Node(i).X(), tri.Node(i).Y(), tri.Node(i).Z()] for i in range(1, tri.NbNodes() + 1)])
    total = 0.0
    for i in range(1, tri.NbTriangles() + 1):
        a, b, c = tri.Triangle(i).Get()
        total += 0.5 * float(np.linalg.norm(np.cross(P[b - 1] - P[a - 1], P[c - 1] - P[a - 1])))
    return total


# ------------------------------------------------------------------ one face -> Maya NURBS
def _arr1(a):
    return [a.Value(i) for i in range(a.Lower(), a.Upper() + 1)]


def _on_rect_boundary(face, u0, u1, v0, v1, samples=24):
    """True if every edge of the face lies on the boundary of the parameter rectangle (=> untrimmed)."""
    du, dv = u1 - u0, v1 - v0
    tol = 1e-6 * max(du, dv, 1.0)          # real exports carry ~1e-8 noise here; trimmed boundaries differ by orders of magnitude more
    total = 0.0
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    n_edges = 0
    while ex.More():
        e = TopoDS.Edge(ex.Current())
        n_edges += 1
        try:
            c = BRepAdaptor_Curve2d(e, face)
        except Exception:
            return False
        a, b = c.FirstParameter(), c.LastParameter()
        prev = None
        for t in np.linspace(a, b, samples):
            p = c.Value(t)
            u, v = p.X(), p.Y()
            if min(abs(u - u0), abs(u - u1), abs(v - v0), abs(v - v1)) > tol:
                return False
            if prev is not None:
                total += ((u - prev[0]) ** 2 + (v - prev[1]) ** 2) ** 0.5
            prev = (u, v)
        ex.Next()
    perimeter = 2.0 * (du + dv)
    return n_edges >= 3 and abs(total - perimeter) <= 1e-4 * perimeter


def _natural_boundary(f2, tol):
    """True if EVERY non-degenerate edge of the face lies on the boundary iso-curves of its surface (u = min/max, v = min/max) in 3D. Then the
    face is the whole surface. This needs no projection, so it is safe where projection is ambiguous (surfaces that collapse to a point at
    their ends, seams, closed shapes)."""
    surf = BRep_Tool.Surface_s(f2)
    b = surf.Bounds()
    n = 160
    us, vs = np.linspace(b[0], b[1], n), np.linspace(b[2], b[3], n)
    iso = np.array([surf.Value(u, b[2]).Coord() for u in us] + [surf.Value(u, b[3]).Coord() for u in us] +
                   [surf.Value(b[0], v).Coord() for v in vs] + [surf.Value(b[1], v).Coord() for v in vs])
    pts = []
    ex = TopExp_Explorer(f2, TopAbs_EDGE)
    while ex.More():
        e = TopoDS.Edge(ex.Current())
        ex.Next()
        if not BRep_Tool.Degenerated_s(e):
            pts += [p.Coord() for p in _edge_points(e)]
    if not pts:
        return False
    P = np.array(pts)
    size = float(np.linalg.norm(iso.max(0) - iso.min(0)))
    # distance to the boundary curves as POLYLINES (point-to-segment), not to their sample points: sampling must not count as a gap
    A = np.vstack([iso[k * n:(k + 1) * n - 1] for k in range(4)])
    B = np.vstack([iso[k * n + 1:(k + 1) * n] for k in range(4)])
    AB = B - A
    L2 = np.maximum((AB ** 2).sum(1), 1e-300)
    worst = 0.0
    for s0 in range(0, len(P), 500):
        Q = P[s0:s0 + 500]
        t = np.clip(((Q[:, None, :] - A[None]) * AB[None]).sum(2) / L2[None], 0.0, 1.0)
        d = np.linalg.norm(Q[:, None, :] - (A[None] + t[..., None] * AB[None]), axis=2).min(1)
        worst = max(worst, float(d.max()))
    return worst <= max(20.0 * tol, 2e-3 * size)


def face_to_nurbs(face, check_grid=(0.17, 0.5, 0.83), fallback=False, from3d=False):
    """Return a dict describing the face as a Maya-convention B-spline surface, or None if degenerate.
    fallback=True ignores the face's own boundary (used when its trim data is damaged) and returns the WHOLE surface, untrimmed
    (one period if the surface is periodic)."""
    nc = BRepBuilderAPI_NurbsConvert(face, True)
    f2 = TopoDS.Face(nc.Shape())
    bs = BRep_Tool.Surface_s(f2).Copy()          # work on a COPY: Segment() below must not alter the face's own surface
    if not hasattr(bs, 'UKnotSequence'):
        raise RuntimeError('face did not convert to a B-spline surface (%s)' % type(bs).__name__)
    wires3d = None
    partial_trim = False
    natural = False
    if from3d and not fallback and _natural_boundary(f2, _file_tolerance(face)):
        fallback = natural = True                   # every edge is on the surface's own boundary: the face IS the whole surface
    if from3d and not natural:                      # OpenCascade's 2D curves cannot be trusted for this face: rebuild from the 3D edges
        wires3d, (fu0, fu1, fv0, fv1), partial_trim = _project_wires(f2, _file_tolerance(face))
    else:
        fu0, fu1, fv0, fv1 = BRepTools.UVBounds_s(f2)
    bu0, bu1, bv0, bv1 = bs.Bounds()
    if fallback:
        fu0, fu1, fv0, fv1 = bu0, bu1, bv0, bv1
    if from3d and partial_trim:
        pass
    # A face on a periodic surface may straddle the seam, so segment WHILE the surface is still periodic (this also makes
    # it non-periodic). Directions that are not periodic are clipped to the surface's own range.
    u0, u1 = (fu0, fu1) if bs.IsUPeriodic() else (max(fu0, bu0), min(fu1, bu1))
    v0, v1 = (fv0, fv1) if bs.IsVPeriodic() else (max(fv0, bv0), min(fv1, bv1))
    # OpenCascade refuses to segment a periodic direction by more than one period. A face range that exceeds the period by
    # a hair (1e-9 .. 1e-5 seen in real exports) is noise: cap it. A genuinely longer range would be an error.
    if bs.IsUPeriodic():
        per = bs.UPeriod()
        if u1 - u0 > per:
            if u1 - u0 - per > 1e-3 * per: raise RuntimeError('face wraps more than one period in U')
            u1 = u0 + per
    if bs.IsVPeriodic():
        per = bs.VPeriod()
        if v1 - v0 > per:
            if v1 - v0 - per > 1e-3 * per: raise RuntimeError('face wraps more than one period in V')
            v1 = v0 + per
    if (u1 - u0) < 1e-12 or (v1 - v0) < 1e-12:
        return None
    bs.Segment(u0, u1, v0, v1)          # clamps the knots and shrinks the surface to what the face actually uses
    # On periodic surfaces Segment may return the surface shifted by whole periods relative to the face's own parameter
    # space (where its trim curves live). Shifting a knot vector does not change the geometry, so align the exported
    # knots to the face's parameter space.
    a0, _a1, b0, _b1 = bs.Bounds()
    shift_u, shift_v = u0 - a0, v0 - b0
    nu, nv = bs.NbUPoles(), bs.NbVPoles()
    du, dv = bs.UDegree(), bs.VDegree()
    uk, vk = _arr1(bs.UKnotSequence()), _arr1(bs.VKnotSequence())
    if len(uk) != nu + du + 1 or len(vk) != nv + dv + 1:
        raise RuntimeError('unexpected flat knot count')
    rational = bool(bs.IsURational() or bs.IsVRational())
    cvs = [[None] * nv for _ in range(nu)]
    wts = [[1.0] * nv for _ in range(nu)]
    for i in range(nu):
        for j in range(nv):
            p = bs.Pole(i + 1, j + 1)
            cvs[i][j] = [p.X(), p.Y(), p.Z()]
            if rational:
                wts[i][j] = bs.Weight(i + 1, j + 1)
    d = dict(
        degree_u=du, degree_v=dv, ncv_u=nu, ncv_v=nv,
        knots_u=[k + shift_u for k in uk[1:-1]], knots_v=[k + shift_v for k in vk[1:-1]],   # Maya convention: drop first/last
        cvs=cvs, rational=rational,
        domain=[uk[1] + shift_u, uk[-2] + shift_u, vk[1] + shift_v, vk[-2] + shift_v],
        trimmed=False if fallback else (True if from3d else not _on_rect_boundary(f2, u0, u1, v0, v1)),
        trim_partial=bool(partial_trim),
        reversed=(face.Orientation() == TopAbs_REVERSED),
    )
    if rational:
        d['weights'] = wts
    if d['trimmed']:
        orig = BRep_Tool.Surface_s(f2)                       # the face's own surface, in the face's parameter space (periodic if the file says so)
        ob = orig.Bounds()
        per_u, per_v = orig.IsUPeriodic(), orig.IsVPeriodic()

        def eval3d(u, v):
            # periodic directions WRAP (a point one period away is the same point); other directions clamp
            if not per_u:
                u = min(max(u, ob[0]), ob[1])
            if not per_v:
                v = min(max(v, ob[2]), ob[3])
            q = orig.Value(u, v)
            return (q.X(), q.Y(), q.Z())
        loops = trim_loops(f2, u1 - u0, v1 - v0, eval3d, _file_tolerance(face), wire_segs=wires3d)
        d['trim_clamped'] = float(clamp_loops(loops, u0, u1, v0, v1))
        # A face whose loops enclose NO area (a doubled line, a collapsed sliver: seen in files converted from FBX) has no interior.
        # It contributes nothing to the model and would only produce self-intersecting loops, so it is skipped (and counted).
        net = signed_area(sample_loop(loops[0], 24)) + sum(signed_area(sample_loop(L, 24)) for L in loops[1:])
        if abs(net) < 1e-9 * (u1 - u0) * (v1 - v0):
            return None
        d['loops'] = loops
        d['outer_is_domain'] = bool(_loop_is_rect(loops[0], u0, u1, v0, v1))
        if from3d:
            d['probes'], d['probe_info'] = _probes_from_loops(loops, u0, u1, v0, v1)
        else:
            d['probes'], d['probe_info'] = _probes(f2, loops, u0, u1, v0, v1)
        if from3d and len(loops) == 1 and d['outer_is_domain']:      # the rebuilt boundary IS the whole parameter rectangle: not trimmed after all
            d['trimmed'] = False
            for k in ('loops', 'outer_is_domain', 'probes', 'probe_info', 'trim_clamped'):
                d.pop(k, None)
    # Check points evaluated by OpenCascade. Sample in the surface's NATIVE parameter range (what bs.Value understands) and
    # export the parameters in the FACE's parameter space (what the exported knots and the trim loops use).
    a0n, a1n, b0n, b1n = bs.Bounds()
    pts = []
    for fu in check_grid:
        for fv in check_grid:
            un, vn = a0n + fu * (a1n - a0n), b0n + fv * (b1n - b0n)
            q = bs.Value(un, vn)
            pts.append([un + shift_u, vn + shift_v, q.X(), q.Y(), q.Z()])
    d['check'] = pts
    return d


# ------------------------------------------------------------------ whole file
def _faces_by_solid(shape):
    """[(face, solid_index or None)]: faces inside each solid in turn, then faces that belong to no solid (surface models)."""
    out = []
    sx = TopExp_Explorer(shape, TopAbs_SOLID)
    k = 0
    while sx.More():
        fx = TopExp_Explorer(sx.Current(), TopAbs_FACE)
        while fx.More():
            out.append((TopoDS.Face(fx.Current()), k))
            fx.Next()
        k += 1
        sx.Next()
    lx = TopExp_Explorer(shape, TopAbs_FACE, TopAbs_SOLID)         # faces NOT inside a solid
    while lx.More():
        out.append((TopoDS.Face(lx.Current()), None))
        lx.Next()
    return out


def _solid_names(color_shape, sub_names):
    """Names of the solids of an (unmoved) part shape, in iteration order; None where the file gives none."""
    names = []
    sx = TopExp_Explorer(color_shape, TopAbs_SOLID)
    while sx.More():
        sol = sx.Current()
        names.append(next((nm for sh, nm in sub_names if sh.IsSame(sol)), None))
        sx.Next()
    return names


def _loop_crossings(P, chunk=250):
    """Number of proper self-crossings of a closed polyline (chunked: loops can have thousands of samples)."""
    A, B = P[:-1], P[1:]
    n = len(A)
    total = 0
    for s0 in range(0, n, chunk):
        a, b = A[s0:s0 + chunk][:, None, :], B[s0:s0 + chunk][:, None, :]
        c, d = A[None, :, :], B[None, :, :]
        r, q = b - a, d - c
        den = r[..., 0] * q[..., 1] - r[..., 1] * q[..., 0]
        with np.errstate(divide='ignore', invalid='ignore'):
            t = ((c[..., 0] - a[..., 0]) * q[..., 1] - (c[..., 1] - a[..., 1]) * q[..., 0]) / den
            u = ((c[..., 0] - a[..., 0]) * r[..., 1] - (c[..., 1] - a[..., 1]) * r[..., 0]) / den
        hit = (t > 1e-9) & (t < 1 - 1e-9) & (u > 1e-9) & (u < 1 - 1e-9) & (np.abs(den) > 1e-30)
        ii, jj = np.nonzero(hit)
        for i, j in zip(ii, jj):
            gi = s0 + i
            if abs(gi - j) > 1 and not (gi == 0 and j == n - 1) and not (j == 0 and gi == n - 1):
                total += 1
    return total


def _suspect(d):
    """Why a finished trimmed face looks wrong (a short text), or None. Loops that cross themselves cannot bound a region, and loops that
    disagree with the points OpenCascade classified mean one of the two is wrong."""
    if not d.get('trimmed') or not d.get('loops'):
        return None
    polys = [sample_loop(L, 12) for L in d['loops']]
    n = sum(_loop_crossings(P) for P in polys)
    if n:
        return 'loops cross themselves (%d crossings)' % n
    probes = d.get('probes') or []
    if probes:
        paths = [_MPath(P) for P in polys]
        bad = 0
        for (u, v, inside) in probes:
            mine = paths[0].contains_point((u, v)) and not any(p.contains_point((u, v)) for p in paths[1:])
            bad += int(bool(mine) != bool(inside))
        if bad > 0.02 * len(probes):
            return '%d of %d classified points disagree with the loops' % (bad, len(probes))
    return None


def extract(path, verbose=False):
    doc = read_cad(path)
    tree = _Tree()
    parts, ct = collect_parts(doc, tree)
    out_parts = []
    stats = dict(parts=len(parts), faces=0, untrimmed=0, trimmed=0, rational=0, skipped_degenerate=0, failed=0, clamped=0, trim_failed=0, rebuilt_from_3d=0, partial_trim=0, rebuilt_suspect=0, suspect=0)
    failures = []
    degraded = []
    suspects = []
    for part in parts:
        pcolor = _color(ct, part['color_shape'])
        entries = _faces_by_solid(part['shape'])                   # located geometry
        centries = _faces_by_solid(part['color_shape'])            # same faces, unmoved: where the document registered colors
        cmap = _inherited_colors(ct, part['color_shape'])
        fcolors = [cmap.get(_shape_key(f)) or _color(ct, f) for f, _ in centries]
        nsol = 1 + max([k for _, k in entries if k is not None] or [-1])
        body_gid = {}
        if nsol > 1:                                               # a part with several bodies: one nested group per body
            for k, nm in enumerate(_solid_names(part['color_shape'], part['sub_names'])):
                body_gid[k] = tree.add('body', nm, part['group'])
        faces = []
        for idx, (f, k) in enumerate(entries, 1):
            rebuilt = False
            try:
                d = face_to_nurbs(f)
            except Exception as e:                                 # never lose the whole file for one bad face
                reason = str(e)[:120]
                d = False
                try:                                               # 1) OpenCascade's 2D curves failed: rebuild them from the 3D edges
                    d = face_to_nurbs(f, from3d=True)
                    rebuilt = True
                    stats['rebuilt_from_3d'] += 1
                except Exception as e3:
                    reason += ' | rebuilding from the 3D edges failed: ' + str(e3)[:90]
                if d is False:
                    try:                                           # 2) damaged beyond repair: keep the SURFACE, flagged, rather than lose the face
                        d = face_to_nurbs(f, fallback=True)
                    except Exception:
                        d = None
                    if d is None:
                        stats['failed'] += 1
                        failures.append('%s face %d: %s' % (part['name'], idx, reason))
                        continue
                    d['trim_failed'] = reason
                    stats['trim_failed'] += 1
                    degraded.append('%s face %d: %s' % (part['name'], idx, reason))
            if not rebuilt and d not in (None, False) and d.get('trimmed') and not d.get('trim_failed'):
                why = _suspect(d)
                if why:                                            # extracted without error but self-inconsistent: try the rebuild
                    try:
                        d3 = face_to_nurbs(f, from3d=True)
                    except Exception:
                        d3 = False
                    if d3 is None or (d3 is not False and not _suspect(d3)):
                        d = d3
                        rebuilt = True
                        stats['rebuilt_from_3d'] += 1
                        stats['rebuilt_suspect'] += 1
                    else:
                        d['trim_suspect'] = why
                        stats['suspect'] += 1
                        suspects.append('%s face %d: %s' % (part['name'], idx, why))
            if rebuilt and d is not None:
                d['trim_source'] = '3d'
                if d.get('trim_partial'):
                    stats['partial_trim'] += 1
                    degraded.append('%s face %d: PARTIAL TRIM - outer boundary replaced by the surface edge, %d hole(s) kept' % (part['name'], idx, len(d.get('loops', [])) - 1))
            if d is None:
                stats['skipped_degenerate'] += 1
                continue
            d['color'] = (fcolors[idx - 1] if idx - 1 < len(fcolors) else None) or pcolor
            d['index'] = idx
            d['group'] = body_gid.get(k, part['group'])
            faces.append(d)
            stats['faces'] += 1
            stats['trimmed' if d['trimmed'] else 'untrimmed'] += 1
            stats['rational'] += int(d['rational'])
            stats['clamped'] += int(bool(d.get('trim_clamped')))
        if faces:
            out_parts.append(dict(name=part['name'], product=part['product'], path=part['path'], group=part['group'], color=pcolor, faces=faces))
    stats['assemblies'] = sum(1 for g in tree.groups if g['kind'] == 'assembly')
    stats['bodies'] = sum(1 for g in tree.groups if g['kind'] == 'body')
    return dict(format=FORMAT, source=os.path.basename(path), units='mm', groups=tree.groups, parts=out_parts, stats=stats, failures=failures, degraded=degraded, suspects=suspects)


def main():
    ap = argparse.ArgumentParser(description='STEP/IGES -> Maya-ready NURBS JSON (OpenCascade).')
    ap.add_argument('cad', help='input .step/.stp/.iges/.igs')
    ap.add_argument('-o', '--out', help='output .json (default: next to the input)')
    a = ap.parse_args()
    data = extract(a.cad)
    out = a.out or os.path.splitext(a.cad)[0] + '.cad2maya.json'
    with open(out, 'w') as f:
        json.dump(data, f)
    s = data['stats']
    print('%s: %d part(s), %d faces (%d untrimmed, %d trimmed, %d rational)' % (os.path.basename(a.cad), s['parts'], s['faces'], s['untrimmed'], s['trimmed'], s['rational']))
    if s.get('clamped'):
        print('  %d face(s) had trim loops that stuck out of their surface domain and were clamped to it (source data quirk)' % s['clamped'])
    if s['skipped_degenerate']:
        print('  skipped %d zero-area face(s)' % s['skipped_degenerate'])
    if s.get('rebuilt_from_3d'):
        print('  %d face(s): OpenCascade could not derive their 2D trim curves (this file stores none), so they were rebuilt from the 3D edges' % s['rebuilt_from_3d'])
    partial = [m for m in data['degraded'] if 'PARTIAL TRIM' in m]
    failed_trim = [m for m in data['degraded'] if 'PARTIAL TRIM' not in m]
    if partial:
        print('  NOTE: %d face(s) could only be trimmed PARTIALLY (their outer boundary could not be rebuilt; the holes were kept):' % len(partial))
        for m in partial[:10]:
            print('    ', m)
    if s.get('suspect'):
        print('  WARNING: %d face(s) have trims that look WRONG and could not be repaired (kept as extracted):' % s['suspect'])
        for m in data['suspects'][:10]:
            print('    ', m)
    if s.get('trim_failed'):
        print('  WARNING: %d face(s) could not be trimmed even that way; their SURFACES were kept untrimmed:' % s['trim_failed'])
        for m in failed_trim[:10]:
            print('    ', m)
    if s['failed']:
        print('  WARNING: %d face(s) could not be converted at all:' % s['failed'])
        for m in data['failures'][:10]:
            print('    ', m)
    print('  wrote', out, '(%.1f MB)' % (os.path.getsize(out) / 1e6))


if __name__ == '__main__':
    main()
