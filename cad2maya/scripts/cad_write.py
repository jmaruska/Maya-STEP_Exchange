#!/usr/bin/env python3
"""
cad_write.py - the other direction: write the cad2maya JSON as STEP or IGES with OpenCascade.

Input is exactly what cad_extract.py produces (and what maya_cad_export.py reads out of Maya): one JSON with a product tree
(`groups`), and `parts` holding NURBS faces (degree, knots, control points, optional trim loops in parameter space).
Output: STEP (assemblies, part names, colors, trimmed faces) or IGES (flat, named).

Runs OUTSIDE Maya, in the same Python as cad_extract.py (OpenCascade + numpy + matplotlib).

    python cad_write.py model.cad2maya.json -o model.step
    python cad_write.py model.cad2maya.json -o model.step --sew 0.01      # also sew faces into shells/solids (tolerance in mm)

A trim loop is either one closed curve (as cad_extract writes it) or a list of separate `edges` (as they come out of Maya);
separate edges are joined, overshoots cut and small gaps closed with the same code that reads CAD files.
"""
import argparse
import json
import os
import sys

import numpy as np

import cad_extract as ce
import cad_names
from OCP.collections import Array1_gp_Pnt2d, Array1_double, Array1_int, Array2_gp_Pnt, Array2_double
from OCP.Geom import Geom_BSplineSurface
from OCP.Geom2d import Geom2d_BSplineCurve
from OCP.gp import gp_Pnt, gp_Pnt2d
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire,
                                BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid)
from OCP.ShapeFix import ShapeFix_Face, ShapeFix_Shape
from OCP.BRepLib import BRepLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRep import BRep_Tool, BRep_Builder
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
from OCP.TopLoc import TopLoc_Location
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf, XCAFDoc_ColorGen
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.IGESCAFControl import IGESCAFControl_Writer
from OCP.Interface import Interface_Static

ACCEPTED = ('cad2maya/2', 'cad2maya/3')
UNIT_NAMES = {'mm': 'MM', 'cm': 'CM', 'm': 'M', 'in': 'INCH', 'inch': 'INCH'}


# ------------------------------------------------------------------------------------------------ surfaces and curves
def _full(k):
    """Maya-convention knots (first and last omitted) -> the full knot vector OpenCascade wants."""
    k = [float(x) for x in k]
    return [k[0]] + k + [k[-1]]


def _knots_mults(kv, tol=1e-12):
    """Group equal knots into (value, multiplicity). The tolerance is relative and tiny: knots that differ at all are DIFFERENT
    knots (merging near-equal ones moves the surface by microns on a metre-sized model)."""
    ks, ms = [], []
    for k in kv:
        if ks and abs(k - ks[-1]) <= tol * max(1.0, abs(k)):
            ms[-1] += 1
        else:
            ks.append(float(k))
            ms.append(1)
    K = Array1_double(1, len(ks))
    M = Array1_int(1, len(ks))
    for i, (k, m) in enumerate(zip(ks, ms)):
        K.SetValue(i + 1, k)
        M.SetValue(i + 1, m)
    return K, M


def make_surface(face):
    nu, nv = face['ncv_u'], face['ncv_v']
    poles = Array2_gp_Pnt(1, nu, 1, nv)
    for i in range(nu):
        for j in range(nv):
            x, y, z = face['cvs'][i][j]
            poles.SetValue(i + 1, j + 1, gp_Pnt(x, y, z))
    UK, UM = _knots_mults(_full(face['knots_u']))
    VK, VM = _knots_mults(_full(face['knots_v']))
    if face.get('rational'):
        w = Array2_double(1, nu, 1, nv)
        for i in range(nu):
            for j in range(nv):
                w.SetValue(i + 1, j + 1, float(face['weights'][i][j]))
        return Geom_BSplineSurface(poles, w, UK, VK, UM, VM, face['degree_u'], face['degree_v'], False, False)
    return Geom_BSplineSurface(poles, UK, VK, UM, VM, face['degree_u'], face['degree_v'], False, False)


def pcurve(c):
    """A trim curve dict (degree, knots, cvs, rational, weights) as a 2D B-spline in (u, v)."""
    n = len(c['cvs'])
    poles = Array1_gp_Pnt2d(1, n)
    for i, (u, v) in enumerate(c['cvs']):
        poles.SetValue(i + 1, gp_Pnt2d(float(u), float(v)))
    K, M = _knots_mults(_full(c['knots']))
    if c.get('rational'):
        w = Array1_double(1, n)
        for i in range(n):
            w.SetValue(i + 1, float(c['weights'][i]))
        return Geom2d_BSplineCurve(poles, w, K, M, c['degree'])
    return Geom2d_BSplineCurve(poles, K, M, c['degree'])


def _curve_dict(curve):
    """Back from a Geom2d curve to a loop dict (only needed to sample / orient)."""
    n = curve.NbPoles()
    cvs = [[curve.Pole(i + 1).X(), curve.Pole(i + 1).Y()] for i in range(n)]
    flat = []
    for i in range(1, curve.NbKnots() + 1):
        flat += [curve.Knot(i)] * curve.Multiplicity(i)
    d = dict(degree=curve.Degree(), knots=flat[1:-1], cvs=cvs, rational=bool(curve.IsRational()))
    if d['rational']:
        d['weights'] = [curve.Weight(i + 1) for i in range(n)]
    return d


def split_at_corners(c):
    """One closed trim curve with C0 corners -> one curve per smooth piece (so the STEP has edges and vertices at the corners)."""
    p = c.Degree()
    breaks = [c.Knot(i) for i in range(2, c.NbKnots()) if c.Multiplicity(i) >= p]
    if not breaks:
        return [c]
    params = [c.FirstParameter()] + breaks + [c.LastParameter()]
    out = []
    for a, b in zip(params[:-1], params[1:]):
        if b - a < 1e-12:
            continue
        s = c.Copy()
        s.Segment(a, b)
        out.append(s)
    return out


def join_edges(edge_dicts, surf, tol3d):
    """Trim edges that arrive as separate curves (from Maya) -> ONE closed loop curve, using the extractor's machinery:
    order and orient head-to-tail, cut overshoots at their crossing, bridge only gaps that are the same 3D point."""
    segs = [pcurve(e) for e in edge_dicts]
    u1, u2, v1, v2 = surf.Bounds()
    tol = 1e-7 * max(u2 - u1, v2 - v1, 1.0)

    def eval3d(u, v):
        q = surf.Value(float(u), float(v))
        return np.array([q.X(), q.Y(), q.Z()])

    plan = ce._chain_segments(segs, tol, eval3d, tol3d)
    if plan is None:
        raise RuntimeError('trim edges do not form a closed loop')
    order, _bridges = plan
    ordered = []
    for j, rev in order:
        if rev:
            segs[j].Reverse()
        ordered.append(segs[j])
    ce._trim_overshoots(ordered, tol)
    chained = []
    for pos, seg in enumerate(ordered):
        chained.append(seg)
        a_end = ce._end_points(seg)[1]
        b_start = ce._end_points(ordered[(pos + 1) % len(ordered)])[0]
        if float(np.linalg.norm(a_end - b_start)) > 1e3 * tol:
            chained.append(ce._make_line(a_end, b_start))
    degree, flat, pts, w = ce.merge_segments(chained)
    rational = bool(np.any(np.abs(np.asarray(w) - 1.0) > 1e-12))
    d = dict(degree=degree, knots=[float(v) for v in flat[1:-1]], cvs=[[float(a), float(b)] for a, b in pts], rational=rational)
    if rational:
        d['weights'] = [float(v) for v in w]
    return pcurve(d)


def _poly(curve, per=12):
    d = _curve_dict(curve)
    return ce.sample_loop(d, per)


def _wire(curves, surf):
    mw = BRepBuilderAPI_MakeWire()
    for cv in curves:
        e = BRepBuilderAPI_MakeEdge(cv, surf, cv.FirstParameter(), cv.LastParameter()).Edge()
        mw.Add(e)
    if not mw.IsDone():
        raise RuntimeError('trim edges would not form a wire')
    return mw.Wire()


# ------------------------------------------------------------------------------------------------ faces
def _area(f):
    p = GProp_GProps()
    BRepGProp.SurfaceProperties_s(f, p)
    return p.Mass()


def _heal(f, rel_tol=5e-3):
    """Repair an invalid face (for example a self-crossing trim loop). Accept only if every piece is valid and the area holds."""
    try:
        sf = ShapeFix_Shape(f)
        sf.Perform()
        out = []
        ex = TopExp_Explorer(sf.Shape(), TopAbs_FACE)
        while ex.More():
            out.append(TopoDS.Face(ex.Current()))
            ex.Next()
        if not out or not all(BRepCheck_Analyzer(x).IsValid() for x in out):
            return None
        a0, a1 = _area(f), sum(_area(x) for x in out)
        return out if abs(a1 - a0) <= rel_tol * max(a0, 1e-12) else None
    except Exception:
        return None


def _degen_tol(surf, U1, U2, V1, V2):
    """Tolerance for treating a side of the parameter rectangle as a POINT (a degenerate edge). A side shorter than 1/2000 of the face's
    longest side is a collapsed tip (seen: grille bars whose ends are 0.000226 long): written as a real microscopic edge, Fusion dropped the
    whole face; the original file marks it degenerate, so this does the same."""
    def side(pts):
        P = np.array([q.Coord() for q in pts])
        return float(np.sum(np.linalg.norm(np.diff(P, axis=0), axis=1)))
    us, vs = np.linspace(U1, U2, 40), np.linspace(V1, V2, 40)
    longest = max(side([surf.Value(u, V1) for u in us]), side([surf.Value(u, V2) for u in us]),
                  side([surf.Value(U1, v) for v in vs]), side([surf.Value(U2, v) for v in vs]))
    return max(1e-7, 5e-4 * longest)


def build_face(face, tol3d=0.01):
    """-> (list of TopoDS_Face, info dict). Several faces only if healing had to split one."""
    surf = make_surface(face)
    U1, U2, V1, V2 = surf.Bounds()
    info = dict(kind='untrimmed', healed=False, valid=True)
    if not face.get('trimmed') or not face.get('loops'):
        faces = [BRepBuilderAPI_MakeFace(surf, U1, U2, V1, V2, _degen_tol(surf, U1, U2, V1, V2)).Face()]
    else:
        loops = []
        for L in face['loops']:
            c = join_edges(L['edges'], surf, tol3d) if L.get('edges') else pcurve(L)
            poly = _poly(c)
            area = ce.signed_area(poly)
            kind = L.get('kind')
            if (kind == 'outer' and area < 0) or (kind == 'inner' and area > 0):
                c.Reverse()                                       # the loop's own declaration wins over its winding
                poly = poly[::-1]
                area = -area
            loops.append(dict(curves=split_at_corners(c), poly=poly, area=area, kind=kind))
        outers = [l for l in loops if (l['kind'] == 'outer') or (l['kind'] is None and l['area'] > 0)]
        holes = [l for l in loops if l not in outers]
        if not outers:                                            # only holes: the outer boundary is the whole parameter rectangle
            base = BRepBuilderAPI_MakeFace(surf, U1, U2, V1, V2, _degen_tol(surf, U1, U2, V1, V2)).Face()
            mk = BRepBuilderAPI_MakeFace(base)
            for h in holes:
                mk.Add(_wire(h['curves'], surf))
            raw = [mk.Face()]
            info['kind'] = 'rect-with-%d-holes' % len(holes)
        else:
            raw = []
            for o in outers:
                mk = BRepBuilderAPI_MakeFace(surf, _wire(o['curves'], surf), True)
                path = ce._MPath(o['poly'])
                for h in holes:
                    if path.contains_point(h['poly'][0]):
                        mk.Add(_wire(h['curves'], surf))
                raw.append(mk.Face())
            info['kind'] = '%d-outer/%d-holes' % (len(outers), len(holes))
        faces = []
        for f in raw:
            sf = ShapeFix_Face(f)
            sf.FixOrientation()
            sf.Perform()
            ex = TopExp_Explorer(sf.Result(), TopAbs_FACE)
            n = 0
            while ex.More():
                fc = TopoDS.Face(ex.Current())
                BRepLib.BuildCurves3d_s(fc, 1e-5)
                faces.append(fc)
                n += 1
                ex.Next()
            if n == 0:
                raise RuntimeError('ShapeFix returned no faces')
    if not all(BRepCheck_Analyzer(f).IsValid() for f in faces):
        healed = _heal(faces[0]) if len(faces) == 1 else None
        if healed:
            faces, info['healed'] = healed, True
        else:
            info['valid'] = False
    if face.get('reversed'):
        faces = [TopoDS.Face(f.Reversed()) for f in faces]
    return faces, info


# ------------------------------------------------------------------------------------------------ sewing
def sew_faces(faces, tol):
    """Sew faces into shells and turn closed shells into solids. Returns (shape, n_shells, n_closed_solids)."""
    sw = BRepBuilderAPI_Sewing(tol)
    for f in faces:
        sw.Add(f)
    sw.Perform()
    sewn = sw.SewedShape()
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    shells = solids = 0
    ex = TopExp_Explorer(sewn, TopAbs_SHELL)
    seen_shell = False
    while ex.More():
        seen_shell = True
        sh = TopoDS.Shell(ex.Current())
        shells += 1
        if BRep_Tool.IsClosed_s(sh):
            sol = BRepBuilderAPI_MakeSolid(sh).Solid()
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(sol, props)
            if props.Mass() < 0:
                sol = TopoDS.Solid(sol.Reversed())
            bld.Add(comp, sol)
            solids += 1
        else:
            bld.Add(comp, sh)
        ex.Next()
    if not seen_shell:                                            # nothing sewed together: keep the faces as they are
        return sewn, 0, 0
    return comp, shells, solids


# ------------------------------------------------------------------------------------------------ the document
def _set_name(label, text):
    if text:
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(str(text)))


def _rgb(c):
    return Quantity_Color(float(c[0]), float(c[1]), float(c[2]), Quantity_TOC_RGB)


def _synthesize_groups(data):
    groups = []
    for part in data['parts']:
        part['group'] = len(groups)
        groups.append(dict(id=len(groups), parent=None, kind='part', name=part['name'], product=part.get('product') or part['name'],
                           maya=cad_names.safe_name(part['name'])))
        for f in part['faces']:
            f['group'] = part['group']
    return groups


def write_cad(data, path, fmt=None, sew=None, tol3d=0.01, log=print):
    """Write `data` (cad2maya JSON as a dict) to `path`. fmt: 'step' or 'iges' (default: from the extension).
    sew: tolerance in file units (mm) to sew each part's faces into shells/solids, or None. Returns a report dict."""
    if data.get('format') not in ACCEPTED:
        raise RuntimeError('unknown JSON format %r (expected one of %s)' % (data.get('format'), ACCEPTED))
    fmt = (fmt or os.path.splitext(path)[1].lstrip('.')).lower()
    fmt = {'stp': 'step', 'igs': 'iges'}.get(fmt, fmt)
    if fmt not in ('step', 'iges'):
        raise RuntimeError('cannot tell the format from %r: use .step/.stp or .iges/.igs' % path)
    groups = data.get('groups') or _synthesize_groups(data)
    rep = dict(faces=0, trimmed=0, invalid=[], healed=0, failed=[], parts=0, assemblies=0, bodies=0, shells=0, solids=0, format=fmt)

    # 1. every face -> OpenCascade faces, collected per tree node
    faces_of = {}
    for part in data['parts']:
        for face in part['faces']:
            tag = '%s face %s' % (part['name'], face.get('index', '?'))
            try:
                fs, info = build_face(face, tol3d)
            except Exception as e:
                rep['failed'].append('%s: %s' % (tag, str(e)[:140]))
                continue
            rep['faces'] += 1
            rep['trimmed'] += int(bool(face.get('trimmed')))
            rep['healed'] += int(info['healed'])
            if not info['valid']:
                rep['invalid'].append(tag)
            faces_of.setdefault(face.get('group', part['group']), []).extend((f, face.get('color')) for f in fs)

    # 2. the XCAF document: assemblies, parts, names, colors
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString('MDTV-XCAF'))
    app.InitDocument(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    kids = {}
    for g in groups:
        kids.setdefault(g['parent'], []).append(g['id'])

    def shape_of(entries):
        """Faces (with colors) -> one shape: a compound of faces, or sewn shells/solids."""
        faces = [f for f, _ in entries]
        if sew and len(faces) > 1:
            shape, ns, nsol = sew_faces(faces, sew)
            rep['shells'] += ns
            rep['solids'] += nsol
            return shape, True
        comp = TopoDS_Compound()
        bld = BRep_Builder()
        bld.MakeCompound(comp)
        for f in faces:
            bld.Add(comp, f)
        return comp, False

    def make(gid):
        g = groups[gid]
        product = g.get('product') or g.get('name') or g.get('maya')
        if g['kind'] == 'assembly':
            lab = st.NewShape()
            _set_name(lab, product)
            rep['assemblies'] += 1
            for kid in kids.get(gid, []):
                if groups[kid]['kind'] == 'body':
                    continue
                comp = st.AddComponent(lab, make(kid), TopLoc_Location())
                _set_name(comp, groups[kid].get('name') or groups[kid].get('product'))
            return lab
        # a part: its own faces, plus one compound per body
        bodies = [k for k in kids.get(gid, []) if groups[k]['kind'] == 'body']
        own = faces_of.get(gid, [])
        sewn_any = False
        if bodies:
            top = TopoDS_Compound()
            bld = BRep_Builder()
            bld.MakeCompound(top)
            body_shapes = []
            for k in bodies:
                sh, sewn = shape_of(faces_of.get(k, []))
                sewn_any |= sewn
                bld.Add(top, sh)
                body_shapes.append((k, sh))
            if own:
                sh, sewn = shape_of(own)
                sewn_any |= sewn
                bld.Add(top, sh)
            shape = top
        else:
            shape, sewn_any = shape_of(own)
            body_shapes = []
        lab = st.AddShape(shape, False)
        _set_name(lab, product)
        rep['parts'] += 1
        for k, sh in body_shapes:
            sub = TDF_Label()
            if st.AddSubShape(lab, sh, sub):
                _set_name(sub, groups[k].get('name'))
                rep['bodies'] += 1
            if not sewn_any:                                       # faces inside a BODY need their colours too (they were being skipped)
                for f, c in faces_of.get(k, []):
                    if c:
                        fsub = TDF_Label()
                        if st.AddSubShape(lab, f, fsub):
                            ct.SetColor(fsub, _rgb(c), XCAFDoc_ColorSurf)
        colors = [c for _, c in own if c]
        if colors and not sewn_any:
            if len(colors) == len(own) and all(tuple(c) == tuple(colors[0]) for c in colors):
                ct.SetColor(lab, _rgb(colors[0]), XCAFDoc_ColorGen)
            else:
                for f, c in own:
                    if c:
                        sub = TDF_Label()
                        if st.AddSubShape(lab, f, sub):
                            ct.SetColor(sub, _rgb(c), XCAFDoc_ColorSurf)
        elif colors and sewn_any:
            ct.SetColor(lab, _rgb(colors[0]), XCAFDoc_ColorGen)    # sewing replaces the faces, so per-face colors cannot survive
        return lab

    for gid in kids.get(None, []):
        make(gid)
    st.UpdateAssemblies()                                         # builds each assembly's own shape from its components; without it nothing is written for assemblies

    # 3. write
    unit = UNIT_NAMES.get(str(data.get('units', 'mm')).lower(), 'MM')
    with ce.quiet():
        if fmt == 'step':
            Interface_Static.SetCVal_s('write.step.unit', unit)
            w = STEPCAFControl_Writer()
            w.SetColorMode(True)
            w.SetNameMode(True)
            w.Transfer(doc, STEPControl_AsIs)
            ok = w.Write(path)
        else:
            Interface_Static.SetCVal_s('write.iges.unit', unit)
            w = IGESCAFControl_Writer()
            w.SetColorMode(True)
            w.SetNameMode(True)
            w.Transfer(doc)
            w.ComputeModel()
            ok = w.Write(path)
    ok = bool(ok) and os.path.isfile(path)
    if not ok:
        raise RuntimeError('OpenCascade could not write %s' % path)
    rep['path'] = path
    rep['bytes'] = os.path.getsize(path)
    return rep


def main():
    ap = argparse.ArgumentParser(description='Write cad2maya JSON as STEP or IGES.')
    ap.add_argument('json')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--sew', type=float, default=None, help='sew each part\'s faces together within this tolerance (mm) and make solids of closed shells')
    ap.add_argument('--join-tol', type=float, default=0.01, help='3D tolerance (mm) for closing gaps between trim edges that came from Maya')
    a = ap.parse_args()
    with open(a.json) as f:
        data = json.load(f)
    rep = write_cad(data, a.out, sew=a.sew, tol3d=a.join_tol)
    print(json.dumps(rep))


if __name__ == '__main__':
    main()
