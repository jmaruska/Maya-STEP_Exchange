"""
maya_cad_import.py - build NURBS surfaces in Maya from cad_extract.py output (STEP / IGES via OpenCascade).

MILESTONE 2: untrimmed AND trimmed faces. A trimmed face is built as: surface + one closed 2D boundary curve per trim
loop -> Maya's trimWithBoundaries node -> history deleted (baked). Every trimmed face is verified by asking Maya which
probe points are inside the result and comparing with OpenCascade's answer; if a face cannot be verified it is reported
and (trim_fallback=True) kept as its full untrimmed surface under the name <name>_UNTRIMMED_FALLBACK.

Pure Maya Python (maya.cmds + maya.api.OpenMaya, Python 3). OpenCascade never runs inside Maya: it runs in a separate
Python (the environment made by setup_macos.sh) that import_cad() launches for you.

    import sys; sys.path.append('/path/to/this/folder')
    import maya_cad_import as mci
    mci.selftest()                                    # run once: checks the API conventions on this Maya
    mci.import_cad('/path/to/part.step', python='/path/to/<environment folder>/bin/python')
    mci.import_json('/path/to/part.cad2maya.json')    # if you ran cad_extract.py yourself

SELF-VERIFICATION: Maya's docs do not say how a CV array is ordered, or whether rational weights are premultiplied.
So every surface is measured after it is built (MFnNurbsSurface.getPointAtParam) against points evaluated by
OpenCascade. If they disagree, the other conventions are tried automatically; the one that matches is remembered for
the session and reported. A surface that matches under no convention is deleted and listed in the report.
"""
import collections
import json
import os
import re
import subprocess
import sys
import tempfile

import maya.cmds as cmds
import maya.api.OpenMaya as om

import cad_names

FORMATS = ('cad2maya/1', 'cad2maya/2', 'cad2maya/3')

# convention candidates, in the order they are tried: (cv_order, weight_mode)
#   cv_order:    'u_major' -> index = iu * ncv_v + iv     'v_major' -> index = iv * ncv_u + iu
#   weight_mode: 'plain'   -> MPoint(x, y, z, w) with Cartesian x,y,z    'premult' -> MPoint(x*w, y*w, z*w, w)
_ORDERS = ('u_major', 'v_major')
_WEIGHTS = ('plain', 'premult')
_learned = {'order': None, 'weight': None, 'probe': None, 'trim_variant': None, 'form': None}


# ------------------------------------------------------------------ small helpers
def _clean(name):
    """A Maya-safe node name (FBX Rx rules, see cad_names.py): never empty, never just a number, no ':' or other special characters."""
    return cad_names.safe_name(name)


def _diag(face, scale):
    xs = [c[0] for row in face['cvs'] for c in row]
    ys = [c[1] for row in face['cvs'] for c in row]
    zs = [c[2] for row in face['cvs'] for c in row]
    return scale * ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2) ** 0.5


class ImportCancelled(Exception):
    """The user cancelled while the CAD file was being read."""


PROGRESS_FILE = os.path.join(tempfile.gettempdir(), 'cad2maya_progress.txt')


def _crumb(text):
    """Overwrite the progress file with what is about to happen. If Maya ever crashes inside a native call, this file names the
    face and the stage (the OS keeps the write even when the process dies)."""
    try:
        with open(PROGRESS_FILE, 'w') as f:
            f.write(text + '\n')
    except Exception:
        pass


def _stage(label, f, *a, **k):
    """Run one Maya call; if it fails, say WHICH call failed (the raw Maya messages are not specific)."""
    try:
        return f(*a, **k)
    except Exception as e:
        raise RuntimeError('[%s] %s: %s' % (label, type(e).__name__, e)) from None


def _closure(face, tol=1e-9):
    """(closed_u, closed_v): do the first and last CV rows/columns coincide?"""
    cv = face['cvs']
    cu = all(max(abs(a - b) for a, b in zip(cv[0][j], cv[-1][j])) < tol for j in range(len(cv[0])))
    cvv = all(max(abs(a - b) for a, b in zip(cv[i][0], cv[i][-1])) < tol for i in range(len(cv)))
    return cu, cvv


def _form_candidates(face):
    """Surface forms to try. Maya's docs allow both 'open' and 'closed' for a surface whose first and last CVs coincide;
    the closed form is only tried for geometrically closed surfaces, after the open form."""
    cu, cvv = _closure(face)
    forms = [('open', 'open')]
    if cu or cvv:
        forms.append(('closed' if cu else 'open', 'closed' if cvv else 'open'))
        if _learned['form'] == 'closed':
            forms.reverse()
    return forms


def _candidates(face):
    """Conventions to try for this face, learned ones first."""
    weights = _WEIGHTS if face['rational'] else ('plain',)
    out = []
    if _learned['order'] and (_learned['weight'] in weights or not face['rational']):
        out.append((_learned['order'], _learned['weight'] if face['rational'] else 'plain'))
    for o in _ORDERS:
        for w in weights:
            c = (o, w)
            if c not in out:
                out.append(c)
    return out


# ------------------------------------------------------------------ building one surface
def _build_surface(face, parent, scale, order, weight_mode, forms=('open', 'open')):
    """Create the surface under `parent` using one convention. Returns the shape MObject."""
    nu, nv = face['ncv_u'], face['ncv_v']
    rational = face['rational']
    idx = ([(iu, iv) for iu in range(nu) for iv in range(nv)] if order == 'u_major'
           else [(iu, iv) for iv in range(nv) for iu in range(nu)])
    pts = om.MPointArray()
    for iu, iv in idx:
        x, y, z = face['cvs'][iu][iv]
        x, y, z = x * scale, y * scale, z * scale
        if rational:
            w = face['weights'][iu][iv]
            if weight_mode == 'premult':
                pts.append(om.MPoint(x * w, y * w, z * w, w))
            else:
                pts.append(om.MPoint(x, y, z, w))
        else:
            pts.append(om.MPoint(x, y, z, 1.0))
    fn = om.MFnNurbsSurface()
    fu = om.MFnNurbsSurface.kClosed if forms[0] == 'closed' else om.MFnNurbsSurface.kOpen
    fv = om.MFnNurbsSurface.kClosed if forms[1] == 'closed' else om.MFnNurbsSurface.kOpen
    obj = _stage('MFnNurbsSurface.create', fn.create, pts, om.MDoubleArray(face['knots_u']), om.MDoubleArray(face['knots_v']),
                 face['degree_u'], face['degree_v'], fu, fv, bool(rational), parent)
    if not obj.hasFn(om.MFn.kNurbsSurface):          # some builds return the transform; find the shape under it
        obj = _stage('find shape under returned transform', om.MFnDagNode(obj).child, 0)
    return obj


def _measure(shape_obj, face, scale):
    """Largest distance between Maya's surface and OpenCascade's check points (in Maya internal units)."""
    fn = _stage('MFnNurbsSurface(shape)', om.MFnNurbsSurface, shape_obj)
    u0, u1, v0, v1 = face['domain']
    mu0, mu1, mv0, mv1 = _domain_of(fn, face)
    worst = 0.0
    for u, v, x, y, z in face['check']:
        fu = (u - u0) / (u1 - u0)                    # compare at the same FRACTION of the domain, so any affine
        fv = (v - v0) / (v1 - v0)                    # re-parameterization by Maya cannot cause a false alarm
        p = _stage('getPointAtParam', fn.getPointAtParam, mu0 + fu * (mu1 - mu0), mv0 + fv * (mv1 - mv0), om.MSpace.kObject)
        d = ((p.x - x * scale) ** 2 + (p.y - y * scale) ** 2 + (p.z - z * scale) ** 2) ** 0.5
        worst = max(worst, d)
    return worst


def _maxabs(face, scale):
    return scale * max(abs(c) for row in face['cvs'] for p in row for c in p)


def _finish_node(tr, shape, parent_group):
    tname = om.MFnDagNode(tr).fullPathName()
    sname = om.MFnDagNode(shape).fullPathName()
    if parent_group:
        tname = cmds.parent(tname, parent_group)[0]
        sname = cmds.listRelatives(tname, shapes=True, fullPath=True)[0]
    return tname, sname


def _make_node(face, name, parent_group, scale, verify):
    """Build + verify one face. Returns (transform_name, shape_name, deviation, convention) or raises RuntimeError."""
    # Maya evaluates in about single precision relative to the COORDINATE MAGNITUDE (not the face size), so a tiny face far
    # from the origin legitimately shows ~1e-7 differences.
    tol = 1e-5 * max(_diag(face, scale), 1e-6) + 3e-6 * _maxabs(face, scale) + 1e-9
    best = None
    errors = []
    for forms in _form_candidates(face):
        for order, wmode in _candidates(face):
            tr = om.MFnTransform().create()
            om.MFnDependencyNode(tr).setName(name)
            try:
                shape = _build_surface(face, tr, scale, order, wmode, forms)
                dev = _measure(shape, face, scale) if verify else 0.0
            except Exception as e:                   # Maya rejected this construction: try the next one
                cmds.delete(om.MFnDagNode(tr).fullPathName())
                errors.append('%s/%s/%s: %s' % (order, wmode, '+'.join(forms), e))
                continue
            if dev <= tol:
                _learned['order'] = order
                if face['rational']:
                    _learned['weight'] = wmode
                if forms != ('open', 'open'):
                    _learned['form'] = 'closed'
                tname, sname = _finish_node(tr, shape, parent_group)
                return tname, sname, dev, (order, wmode)
            cmds.delete(om.MFnDagNode(tr).fullPathName())
            errors.append('%s/%s/%s: built but differs from OpenCascade by %.3g cm (tolerance %.3g)' % (order, wmode, '+'.join(forms), dev, tol))
            if best is None or dev < best:
                best = dev
    # last resort: the MEL surface command (a different construction route inside Maya)
    try:
        return _make_node_mel(face, name, parent_group, scale, verify, tol)
    except Exception as e:
        errors.append('cmds.surface route: %s' % e)
    first = errors[0] if errors else '?'
    if best is None:
        raise RuntimeError('Maya could not build this surface by any route. First failure: %s  (%d attempts; full list: %s)' % (first, len(errors), ' || '.join(errors)[:900]))
    raise RuntimeError('no CV-order/weight convention reproduced the OpenCascade surface (best deviation %.3g cm, tolerance %.3g)%s'
                       % (best, tol, ('; first error: ' + first) if first else ''))


_UNIT_FROM_CM = {'mm': 10.0, 'cm': 1.0, 'm': 0.01, 'in': 1 / 2.54, 'ft': 1 / 30.48, 'yd': 1 / 91.44}


def _make_node_mel(face, name, parent_group, scale, verify, tol):
    """Build the surface with the cmds.surface command instead of the API (it is more forgiving about some inputs)."""
    unit = cmds.currentUnit(query=True, linear=True)
    k = _UNIT_FROM_CM.get(unit, 1.0)                              # cmds.surface takes values in the current UI unit
    nu, nv = face['ncv_u'], face['ncv_v']
    cu, cvv = _closure(face)
    for order in (_learned['order'] or 'u_major', 'v_major' if (_learned['order'] or 'u_major') == 'u_major' else 'u_major'):
        idx = ([(i, j) for i in range(nu) for j in range(nv)] if order == 'u_major' else [(i, j) for j in range(nv) for i in range(nu)])
        kw = dict(degreeU=face['degree_u'], degreeV=face['degree_v'], formU='open', formV='open',
                  knotU=list(face['knots_u']), knotV=list(face['knots_v']))
        if face['rational']:
            kw['pointWeight'] = [tuple(c * scale * k for c in face['cvs'][i][j]) + (face['weights'][i][j],) for i, j in idx]
        else:
            kw['point'] = [tuple(c * scale * k for c in face['cvs'][i][j]) for i, j in idx]
        before = set(cmds.ls(type='nurbsSurface', long=True) or [])
        res = cmds.surface(**kw)
        new = [p for p in (cmds.ls(type='nurbsSurface', long=True) or []) if p not in before]
        if not new:
            raise RuntimeError('cmds.surface ran (returned %r) but created no nurbsSurface' % (res,))
        shape_path = new[0]
        tr_path = cmds.listRelatives(shape_path, parent=True, fullPath=True)[0]
        dev = _measure(_fn_for(shape_path).object(), face, scale) if verify else 0.0
        if dev <= tol:
            tr_path = cmds.rename(tr_path, name)
            tr_path = cmds.ls(tr_path, long=True)[0]
            if parent_group:
                tr_path = cmds.parent(tr_path, parent_group)[0]
            shape_path = cmds.listRelatives(tr_path, shapes=True, fullPath=True)[0]
            return tr_path, shape_path, dev, (order, 'plain')
        cmds.delete(tr_path)
    raise RuntimeError('cmds.surface built it but it differs from OpenCascade')


# ------------------------------------------------------------------ trimmed faces
def _fn_for(path):
    sel = om.MSelectionList()
    sel.add(path)
    return om.MFnNurbsSurface(sel.getDagPath(0))


def _prop(obj, name):
    """Python API 2.0 exposes many queries as properties and others as methods; accept either."""
    v = getattr(obj, name)
    return v() if callable(v) else v


def _maya_domain(fn):
    """The surface's REAL parameter range from Maya, or None. (MFnNurbsSurface.getKnotDomain does not exist in the Python
    API 2.0, so try the alternatives that do.)"""
    try:
        du, dv = _prop(fn, 'knotDomainInU'), _prop(fn, 'knotDomainInV')
        return du[0], du[1], dv[0], dv[1]
    except Exception:
        pass
    try:
        ku, kv = fn.knotsInU(), fn.knotsInV()
        return ku[0], ku[len(ku) - 1], kv[0], kv[len(kv) - 1]
    except Exception:
        pass
    try:
        m = fn.getKnotDomain()
        return m[0], m[1], m[2], m[3]
    except Exception:
        return None


def _domain_of(fn, face):
    d = _maya_domain(fn)
    if d is None:
        u0, u1, v0, v1 = face['domain']
        return u0, u1, v0, v1
    return d


def _make_curve(loop, face, dom, parent_tr, weight_mode, flip, form='closed'):
    """One closed boundary curve in (u, v) parameter space (z = 0), mapped into Maya's parameter domain."""
    u0, u1, v0, v1 = face['domain']
    m0, m1, n0, n1 = dom
    cvs = [list(c) for c in loop['cvs']]
    wts = list(loop['weights']) if loop.get('rational') else [1.0] * len(cvs)
    knots = list(loop['knots'])
    if flip:                                                      # reverse direction
        cvs.reverse()
        wts.reverse()
        knots = [knots[0] + knots[-1] - k for k in reversed(knots)]
    pts = om.MPointArray()
    for (u, v), w in zip(cvs, wts):
        x = m0 + (u - u0) / (u1 - u0) * (m1 - m0)
        y = n0 + (v - v0) / (v1 - v0) * (n1 - n0)
        if loop.get('rational') and weight_mode == 'premult':
            pts.append(om.MPoint(x * w, y * w, 0.0, w))
        else:
            pts.append(om.MPoint(x, y, 0.0, w))
    fn = om.MFnNurbsCurve()
    fn.create(pts, om.MDoubleArray(knots), loop['degree'], om.MFnNurbsCurve.kOpen if form == 'open' else om.MFnNurbsCurve.kClosed, False, bool(loop.get('rational')), parent_tr)


def _touches_domain_edge(loop, face):
    u0, u1, v0, v1 = face['domain']
    tol = 1e-9 * max(u1 - u0, v1 - v0)
    return any(min(abs(c[0] - u0), abs(c[0] - u1), abs(c[1] - v0), abs(c[1] - v1)) <= tol for c in loop['cvs'])


def _inset_loop(loop, face, rel=1e-4):
    """Move control points that sit exactly ON the surface edge slightly inward. On a CLOSED surface the outer boundary
    runs along both seam edges (the same 3D curve), which Maya's node may treat as a boundary touching itself."""
    u0, u1, v0, v1 = face['domain']
    eu, ev = rel * (u1 - u0), rel * (v1 - v0)
    tu, tv = 1e-9 * (u1 - u0), 1e-9 * (v1 - v0)
    out = dict(loop)
    cvs = []
    for u, v in loop['cvs']:
        if abs(u - u0) <= tu: u = u0 + eu
        elif abs(u - u1) <= tu: u = u1 - eu
        if abs(v - v0) <= tv: v = v0 + ev
        elif abs(v - v1) <= tv: v = v1 - ev
        cvs.append([u, v])
    out['cvs'] = cvs
    return out


POLY_PER_SPAN = 16        # vertices per knot span of the exact boundary when a polyline stands in for it (about 1e-5 of the surface size)


def _trim_variants(face):
    """(outer_mode, flip, inset, representation) combinations to try. All EXACT-curve variants come first; the one that
    worked before goes first. Polyline stand-ins (outer boundary only, then all boundaries) are the last resort."""
    v = [('explicit', False, False, 'exact'), ('explicit', True, False, 'exact')]
    touches = _touches_domain_edge(face['loops'][0], face) and not face.get('outer_is_domain')
    if touches:
        v += [('explicit', False, True, 'exact'), ('explicit', True, True, 'exact')]      # hairline gap at the edge
    for rep in ('outer_poly', 'all_poly'):
        v += [('explicit', False, False, rep), ('explicit', True, False, rep)]
        if touches:
            v += [('explicit', False, True, rep), ('explicit', True, True, rep)]
    learned = _learned['trim_variant']
    if learned is not None:
        match = [x for x in v if x[:3] == tuple(learned[:3]) and x[3] == 'exact']
        if match:
            v.remove(match[0])
            v.insert(0, match[0])
    return v


def _loops_for_variant(face, inset, rep):
    loops = list(face['loops'])
    if inset:
        loops[0] = _inset_loop(loops[0], face)
    if rep == 'outer_poly':
        loops[0] = _polyline_loop(loops[0], POLY_PER_SPAN)
    elif rep == 'all_poly':
        loops = [_polyline_loop(l, POLY_PER_SPAN) for l in loops]
    return loops


def _poly_deviation(face, rep):
    """Largest distance (as a fraction of the surface's parameter size) between the exact boundary and its polyline."""
    u0, u1, v0, v1 = face['domain']
    size = max(u1 - u0, v1 - v0)
    worst = 0.0
    for loop in (face['loops'][:1] if rep == 'outer_poly' else face['loops']):
        distinct = sorted(set(round(x, 12) for x in loop['knots']))
        for a, b in zip(distinct[:-1], distinct[1:]):
            for q in range(POLY_PER_SPAN):
                ta, tb = a + (b - a) * q / float(POLY_PER_SPAN), a + (b - a) * (q + 1) / float(POLY_PER_SPAN)
                pa, pb, pm = _eval_loop_py(loop, ta), _eval_loop_py(loop, tb), _eval_loop_py(loop, 0.5 * (ta + tb))
                dx, dy = pb[0] - pa[0], pb[1] - pa[1]
                l2 = dx * dx + dy * dy
                t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((pm[0] - pa[0]) * dx + (pm[1] - pa[1]) * dy) / l2))
                worst = max(worst, ((pm[0] - pa[0] - t * dx) ** 2 + (pm[1] - pa[1] - t * dy) ** 2) ** 0.5)
    return worst / size


def _probe_mismatches(out_shape, face, fn_name):
    fn = _fn_for(out_shape)
    u0, u1, v0, v1 = face['domain']
    m0, m1, n0, n1 = _domain_of(fn, face)
    bad = 0
    for u, v, inside in face['probes']:
        pu = m0 + (u - u0) / (u1 - u0) * (m1 - m0)
        pv = n0 + (v - v0) / (v1 - v0) * (n1 - n0)
        got = bool(getattr(fn, fn_name)(pu, pv))
        bad += int(got != bool(inside))
    return bad


def _verify_trim(out_shape, face):
    """Returns (mismatches, n_probes, function_used). Tries Maya's two region queries and keeps the one that works."""
    _crumb('probing the trimmed region of %s with %d probe points' % (out_shape, len(face['probes'])))
    n = len(face['probes'])
    if n == 0:
        return 0, 0, None
    order = ['isPointInTrimmedRegion', 'isParamOnSurface']
    if _learned['probe'] in order:
        order.remove(_learned['probe'])
        order.insert(0, _learned['probe'])
    best = None
    last_error = None
    for name in order:
        try:
            bad = _probe_mismatches(out_shape, face, name)
        except Exception as e:
            last_error = '%s: %s: %s' % (name, type(e).__name__, e)
            continue
        if best is None or bad < best[0]:
            best = (bad, name)
        if bad == 0:
            break
    if best is None:
        raise RuntimeError('neither region query worked on this Maya (%s)' % last_error)
    return best[0], n, best[1]


def _wire_trim(src_shape, curve_shapes, outer_mode, name):
    """trimWithBoundaries(inputSurface, inputBoundaries[]) -> new surface shape; returns (node, out_transform, out_shape)."""
    tw = cmds.createNode('trimWithBoundaries')
    cmds.connectAttr(src_shape + '.local', tw + '.inputSurface', force=True)
    first = 1 if outer_mode == 'native' else 0                   # 'native': leave index 0 empty so Maya uses the surface edge
    for k, cs in enumerate(curve_shapes):
        cmds.connectAttr(cs + '.local', '%s.inputBoundaries[%d]' % (tw, k + first), force=True)
    out_tr = cmds.createNode('transform', name=name)
    out_shape = cmds.createNode('nurbsSurface', parent=out_tr)
    cmds.connectAttr(tw + '.outputSurface', out_shape + '.create', force=True)
    return tw, out_tr, out_shape


def _make_trimmed(face, name, parent_group, scale, verify, report):
    """Build + trim + verify one trimmed face. Returns (transform, shape, info) or raises RuntimeError."""
    # 1) the untrimmed surface (this also checks the surface geometry itself)
    src_tr, src_shape, dev, conv = _make_node(face, name + '_src', None, scale, verify)
    fn_src = _fn_for(src_shape)
    dom = _domain_of(fn_src, face)
    weight_mode = _learned['weight'] or 'plain'
    loops = face['loops']
    errors = []
    best = None
    for outer_mode, flip, inset, rep in _trim_variants(face):
        group = cmds.group(empty=True, name=name + '_tmpCurves')
        use_loops = _loops_for_variant(face, inset, rep)
        tw = out_tr = None
        try:
            curve_trs = []                                            # creation order matters: the outer boundary must be first
            for k, loop in enumerate(use_loops):
                if outer_mode == 'native' and k == 0:
                    continue
                # flip=True reverses every loop: outer clockwise, holes counter-clockwise
                tr = om.MFnTransform().create()
                _make_curve(loop, face, dom, tr, weight_mode, flip)
                curve_trs.append(cmds.parent(om.MFnDagNode(tr).fullPathName(), group)[0])
            curve_shapes = [cmds.listRelatives(t, shapes=True, fullPath=True)[0] for t in curve_trs]
            tw, out_tr, out_shape = _wire_trim(src_shape, curve_shapes, outer_mode, name)
            _crumb('baking the trim (delete history) of %s, variant %s%s%s %s' % (name, outer_mode, '+flip' if flip else '', '+inset' if inset else '', rep))
            cmds.delete(out_shape, constructionHistory=True)       # evaluates and bakes the trim
            out_tr_full = cmds.ls(out_tr, long=True)[0]
            out_shape = cmds.listRelatives(out_tr_full, shapes=True, fullPath=True)[0]
            bad, n, fname = _verify_trim(out_shape, face) if verify else (0, 0, None)
            allowed = int(0.005 * n)
            if bad <= allowed:
                best = (out_tr_full, out_shape, bad, n, fname, (outer_mode, flip, inset, rep))
                if rep == 'exact':
                    _learned['trim_variant'] = (outer_mode, flip, inset)
                if fname:
                    _learned['probe'] = fname
                cmds.delete(group)
                break
            errors.append('%s%s%s%s: %d/%d probes disagree (via %s)' % (outer_mode, '+flip' if flip else '', '+inset' if inset else '', '' if rep == 'exact' else '+' + rep, bad, n, fname))
            cmds.delete(out_tr_full)
        except Exception as e:
            errors.append('%s%s%s%s: %s: %s' % (outer_mode, '+flip' if flip else '', '+inset' if inset else '', '' if rep == 'exact' else '+' + rep, type(e).__name__, e))
            for node in (out_tr,):
                try:
                    if node and cmds.objExists(node):
                        cmds.delete(node)
                except Exception:
                    pass
        finally:
            for node in (tw, group):
                try:
                    if node and cmds.objExists(node):
                        cmds.delete(node)
                except Exception:
                    pass
    try:
        cmds.delete(src_tr)
    except Exception:
        pass
    if best is None:
        raise RuntimeError('trim could not be verified. Tried: ' + ' | '.join(errors))
    tname, sname = best[0], best[1]
    if parent_group:
        tname = cmds.parent(tname, parent_group)[0]
        sname = cmds.listRelatives(tname, shapes=True, fullPath=True)[0]
    pdev = _poly_deviation(face, best[5][3]) if best[5][3] != 'exact' else 0.0
    return tname, sname, dict(mismatch=best[2], probes=best[3], fn=best[4], variant=best[5], surface_dev=dev, poly_dev=pdev)


# ------------------------------------------------------------------ hierarchy + shaders
def _synthesize_groups(data):
    """JSON from older versions has only parts with a `path` of assembly names: turn it into the tree format (assemblies are merged
    by name there, as before; one part group per part)."""
    reg = cad_names.NameRegistry()
    groups, by_path = [], {}
    for part in data['parts']:
        parent = None
        key = ()
        for p in part.get('path', []):
            key += (p,)
            if key not in by_path:
                by_path[key] = len(groups)
                groups.append(dict(id=len(groups), parent=parent, kind='assembly', name=p, product=p, maya=reg.make(p, 'unnamed_assembly')))
            parent = by_path[key]
        part['group'] = len(groups)
        groups.append(dict(id=len(groups), parent=parent, kind='part', name=part['name'], product=part['name'], maya=reg.make(part['name'], 'unnamed_part')))
        for f in part['faces']:
            f['group'] = part['group']
    return groups


def _tag_original(node, g):
    """Keep the original CAD names on the group (Maya's name had to be changed, e.g. 'CoreWood:1' -> 'CoreWood_1'). The exporter
    reads them back, so a round trip restores the names and the roles (assembly / part / body)."""
    for attr, value in (('cadName', g.get('name', '')), ('cadProduct', g.get('product', '')), ('cadKind', g.get('kind', ''))):
        try:
            exists = False
            try:
                exists = bool(cmds.attributeQuery(attr, node=node, exists=True))
            except Exception:
                pass
            if not exists:
                cmds.addAttr(node, longName=attr, dataType='string')
            cmds.setAttr(node + '.' + attr, value or '', type='string')
        except Exception:
            pass


def _build_groups(groups, root, root_clean, grouping_parts, rep):
    """One Maya group per OCCURRENCE in the product tree (assembly in assembly, parts in assemblies, bodies in parts), named
    <name>_grp. Returns {tree id: parent node for that occurrence's surfaces}."""
    gmap = {}
    for g in groups:                                              # parents always come before their children
        parent = gmap[g['parent']] if g['parent'] is not None else root
        tag = _clean(g['maya'])
        if g['kind'] == 'assembly' and g['parent'] is None and tag == root_clean:
            gmap[g['id']] = root                                  # the top assembly just repeats the root's name: do not nest it
            _tag_original(root, g)                                # ...but the root carries its original name
            continue
        if g['kind'] == 'part' and not grouping_parts:
            gmap[g['id']] = parent                                # single-face parts (RIB conversions): no group per face
            continue
        node = _group(tag + '_grp', parent)
        _tag_original(node, g)
        gmap[g['id']] = node
        rep['part_groups'] += int(g['kind'] == 'part')
        rep['groups_made'] += 1
    return gmap


def _group(name, parent=None):
    g = cmds.group(empty=True, name=_clean(name))
    if parent:
        g = cmds.parent(g, parent)[0]
    return g


def _material_for(color, cache):
    key = tuple(round(c, 4) for c in (color or (0.6, 0.6, 0.6))) if color else 'default'
    if key not in cache:
        base = 'cad_mtl_default' if key == 'default' else 'cad_mtl_%d' % len(cache)      # 'default': the file gave no colour
        mtl = cmds.shadingNode('lambert', asShader=True, name=base)
        rgb = (0.6, 0.6, 0.6) if key == 'default' else key
        cmds.setAttr(mtl + '.color', rgb[0], rgb[1], rgb[2], type='double3')
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=base + 'SG')
        cmds.connectAttr(mtl + '.outColor', sg + '.surfaceShader', force=True)
        cache[key] = (sg, [], mtl)
    return cache[key]


# ------------------------------------------------------------------ public API
def import_data(data, scale=0.1, trims=True, include_trimmed=False, trim_fallback=True, verify=True, root_name=None, group_parts='auto', progress=None):
    """Build the scene from a cad2maya data dict. progress(done, total, text) is called per face; return True from it to cancel. `scale` converts the file's millimetres to Maya's centimetres."""
    if data.get('format') not in FORMATS:
        raise ValueError('not a cad2maya file (format=%r)' % data.get('format'))
    root_label = root_name or os.path.splitext(data.get('source', 'cad'))[0]
    root = _group(root_label)
    root_clean = _clean(root_label)
    # 'root' = only the group the import wraps everything in; it becomes an assembly (with the original name) if the file's own top
    # assembly was folded into it below. The exporter leaves a plain 'root' out, so a round trip adds no level.
    _tag_original(root, dict(name=os.path.splitext(data.get('source', 'cad'))[0], product=os.path.splitext(data.get('source', 'cad'))[0], kind='root'))
    try:                                                          # the exporter divides by this to give the file back its own numbers
        cmds.addAttr(root, longName='cadImportScale', attributeType='double')
        cmds.setAttr(root + '.cadImportScale', float(scale))
    except Exception:
        pass
    mats = {}
    rep = dict(built=0, skipped_trimmed=0, preview_trimmed=0, failed=[], max_deviation=0.0, conventions={}, rational=0,
               trimmed_built=0, trim_variants={}, trim_probe_fn={}, trim_mismatch=0, trim_probes=0, trim_fallback=[],
               trim_unverified=[], trim_poly=[], part_groups=0, groups_made=0)
    # Part groups: 'auto' groups every part unless each part is a single face (RenderMan conversions: a group per face is clutter).
    grouping = (group_parts is True) or (group_parts == 'auto' and any(len(p['faces']) > 1 for p in data['parts']))
    tree = data.get('groups') or _synthesize_groups(data)
    gmap = _build_groups(tree, root, root_clean, grouping, rep)
    total = sum(len(p['faces']) for p in data['parts'])
    done = 0
    rep['cancelled'] = False
    for part in data['parts']:
        if rep['cancelled']:
            break
        tag = _clean(tree[part['group']]['maya'])
        for face in part['faces']:
            nm = '%s_f%d' % (tag, face.get('index', 0))
            parent = gmap[face.get('group', part['group'])]
            done += 1
            if progress is not None:
                try:
                    if progress(done, total, nm):
                        rep['cancelled'] = True
                        break
                except Exception:
                    pass                                           # a broken progress display must never stop an import
            if face.get('trim_partial'):                               # only the HOLES could be rebuilt; the surface edge stands in for the outer boundary
                rep.setdefault('partial', []).append(nm)
                nm = nm + '_PARTIAL_TRIM'
            if face.get('trim_failed'):                                # the FILE's trim data was damaged: only the surface could be recovered
                if not trim_fallback:
                    rep['failed'].append('%s: damaged trim data in the file (%s)' % (nm, face['trim_failed']))
                    continue
                rep['trim_fallback'].append('%s: damaged trim data in the file (%s)' % (nm, face['trim_failed']))
                nm = nm + '_UNTRIMMED_FALLBACK'
            _crumb('building %s  (%s, %s)' % (nm, 'trimmed' if face['trimmed'] else 'untrimmed', 'degree (%d,%d), CVs (%d,%d)' % (face['degree_u'], face['degree_v'], face['ncv_u'], face['ncv_v'])))
            color = face.get('color') or part.get('color')
            if face['trimmed'] and trims and face.get('loops'):
                try:
                    tname, sname, info = _make_trimmed(face, nm, parent, scale, verify, rep)
                except Exception as e:
                    if not trim_fallback:
                        rep['failed'].append('%s: %s' % (nm, e))
                        continue
                    rep['trim_fallback'].append('%s: %s' % (nm, e))
                    try:
                        tname, sname, dev, conv = _make_node(face, nm + '_UNTRIMMED_FALLBACK', parent, scale, verify)
                    except Exception as e2:
                        rep['failed'].append('%s: %s' % (nm, e2))
                        continue
                    rep['built'] += 1
                    if not grouping:                                       # flat file: the part name lives on the surface itself
                        _tag_original(tname, tree[part['group']])
                    _material_for(color, mats)[1].append(sname)
                    continue
                rep['built'] += 1
                rep['trimmed_built'] += 1
                rep['rational'] += int(face['rational'])
                rep['max_deviation'] = max(rep['max_deviation'], info['surface_dev'])
                rep['trim_variants'][info['variant']] = rep['trim_variants'].get(info['variant'], 0) + 1
                if info['fn']:
                    rep['trim_probe_fn'][info['fn']] = rep['trim_probe_fn'].get(info['fn'], 0) + 1
                rep['trim_mismatch'] += info['mismatch']
                rep['trim_probes'] += info['probes']
                if info['probes'] == 0:
                    rep['trim_unverified'].append(nm)
                if info['variant'][3] != 'exact':
                    rep['trim_poly'].append((nm, info['poly_dev'], info['variant'][3]))
                if not grouping:                                       # flat file: the part name lives on the surface itself
                    _tag_original(tname, tree[part['group']])
                _material_for(color, mats)[1].append(sname)
                continue
            if face['trimmed']:
                if not include_trimmed:
                    rep['skipped_trimmed'] += 1
                    continue
                rep['preview_trimmed'] += 1
            try:
                tname, sname, dev, conv = _make_node(face, nm, parent, scale, verify)
            except Exception as e:
                rep['failed'].append('%s: %s' % (nm, e))
                continue
            rep['built'] += 1
            rep['rational'] += int(face['rational'])
            rep['max_deviation'] = max(rep['max_deviation'], dev)
            rep['conventions'][conv] = rep['conventions'].get(conv, 0) + 1
            if not grouping:                                       # flat file: the part name lives on the surface itself
                _tag_original(tname, tree[part['group']])
            _material_for(color, mats)[1].append(sname)
    for sg, shapes, _mtl in mats.values():
        if shapes:
            cmds.sets(shapes, forceElement=sg)
    rep['shading'] = [n for sg, _s, mtl in mats.values() for n in (mtl, sg)]
    rep['extent_cm'] = _extent_cm(data, scale)
    rep['root'] = root
    _print_report(rep, data, scale)
    return rep


def _extent_cm(data, scale):
    lo = [1e300] * 3
    hi = [-1e300] * 3
    for part in data['parts']:
        for face in part['faces']:
            for row in face['cvs']:
                for p in row:
                    for k in range(3):
                        lo[k] = min(lo[k], p[k])
                        hi[k] = max(hi[k], p[k])
    return [(h - l) * scale for l, h in zip(lo, hi)] if data['parts'] else [0, 0, 0]


def _print_report(rep, data, scale):
    print('--- cad2maya import report: %s' % data.get('source'))
    print('    (progress file, if Maya ever crashes: %s)' % PROGRESS_FILE)
    if rep.get('cancelled'):
        print('    CANCELLED: the import was stopped by the user; what was built so far is in the scene.')
    ext = max(rep.get('extent_cm') or [0])
    if ext and (ext < 5.0 or ext > 1e5):
        print('    WARNING: the imported model is %.3g cm across at scale=%g. Maya works badly with very small or very large geometry\n'
              '             (tolerances, trims). If the model should be about 1000x bigger/smaller, import again with scale=%g.'
              % (ext, scale, scale * (1000.0 if ext < 5.0 else 0.001)))
    print('    built %d surface(s) (%d rational)   max deviation from OpenCascade: %.3g cm' % (rep['built'], rep['rational'], rep['max_deviation']))
    if rep.get('groups_made'):
        print('    hierarchy: %d group(s) (<name>_grp) mirroring the CAD product tree: %d part(s)%s' % (rep['groups_made'], rep['part_groups'], ', plus assemblies/bodies' if rep['groups_made'] > rep['part_groups'] else ''))
    if rep['conventions']:
        print('    conventions that matched (cv order, weight mode): %s' % rep['conventions'])
    if rep['trimmed_built']:
        print('    trimmed faces built and verified: %d   (probe points that disagreed with OpenCascade: %d of %d)' % (rep['trimmed_built'], rep['trim_mismatch'], rep['trim_probes']))
        print('    trim method that worked (outer boundary, reversed?, moved inward?): %s   region query: %s' % (rep['trim_variants'], rep['trim_probe_fn']))
        if rep['trim_unverified']:
            print('    %d trimmed face(s) were too small/thin to place probe points: built, but not region-verified (e.g. %s)'
                  % (len(rep['trim_unverified']), ', '.join(rep['trim_unverified'][:3])))
    if rep['trim_poly']:
        worst = max(rep['trim_poly'], key=lambda x: x[1])
        print('    NOTE: %d trimmed face(s) were trimmed with a POLYLINE boundary (%d vertices per curve span) because Maya\'s trim node '
              'rejected the exact curve. Largest deviation from the exact boundary: %.1e of the surface size (%s). Faces: %s'
              % (len(rep['trim_poly']), POLY_PER_SPAN, worst[1], worst[0], ', '.join(x[0] for x in rep['trim_poly'][:4])))
    if any(k[2] for k in rep['trim_variants']):
        print('    NOTE: %d trimmed face(s) on closed surfaces needed their outer boundary moved inward by 0.01%% of the surface at the seam'
              ' (leaves a hairline gap there; not water-tight)' % sum(n for k, n in rep['trim_variants'].items() if k[2]))
    if rep.get('partial'):
        print('    NOTE: %d face(s) are only PARTIALLY trimmed (name ends _PARTIAL_TRIM): the outer boundary could not be rebuilt from the file, so the surface edge stands in for it and only the holes were cut: %s' % (len(rep['partial']), ', '.join(rep['partial'][:4])))
    if rep['trim_fallback']:
        print('    %d trimmed face(s) could NOT be trimmed and were kept as full untrimmed surfaces (name ends _UNTRIMMED_FALLBACK):' % len(rep['trim_fallback']))
        for m in rep['trim_fallback'][:8]:
            print('      ', m[:520])
    if rep['skipped_trimmed']:
        print('    skipped %d trimmed face(s) (trims=False; include_trimmed=True previews their full surfaces)' % rep['skipped_trimmed'])
    if rep['preview_trimmed']:
        print('    NOTE: %d trimmed face(s) were imported as their FULL UNTRIMMED surface (preview only)' % rep['preview_trimmed'])
    if rep['failed']:
        print('    FAILED %d face(s):' % len(rep['failed']))
        for m in rep['failed'][:15]:
            print('      ', m)
    for f in data.get('failures', [])[:10]:
        print('    extractor could not convert:', f)


def import_json(path, **kw):
    with open(path) as f:
        return import_data(json.load(f), **kw)


def _extractor_python(python=None):
    p = python or os.environ.get('CAD2MAYA_PYTHON')
    if not p:
        raise RuntimeError('Tell me which Python has OpenCascade: import_cad(path, python="/path/to/<environment folder>/bin/python") '
                           'or set the CAD2MAYA_PYTHON environment variable.')
    if not os.path.exists(p):
        raise RuntimeError('Python not found: %s' % p)
    return p


def _run_extractor(cmd, on_wait=None):
    """Run the extractor as a separate process. While it runs, on_wait(seconds_elapsed) is called about 10 times a second; if it
    returns True the process is killed and ImportCancelled is raised. Output goes to temp files (no pipe can fill up and stall it)."""
    import time
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err)
        t0 = time.time()
        while proc.poll() is None:
            time.sleep(0.1)
            if on_wait is not None:
                try:
                    cancel = bool(on_wait(time.time() - t0))
                except Exception:
                    cancel = False
                if cancel:
                    proc.kill()
                    proc.wait()
                    raise ImportCancelled('cancelled while reading the CAD file')
        out.seek(0)
        err.seek(0)
        return proc.returncode, out.read().decode('utf-8', 'replace'), err.read().decode('utf-8', 'replace')


def import_cad(path, python=None, on_wait=None, **kw):
    """Convert a STEP/IGES file with OpenCascade (in a separate Python process) and import the result.
    on_wait(seconds) is called while the file is being read; return True from it to cancel."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise RuntimeError('CAD file not found: %s\n(check the path; for the bundled test files the "testdata" folder must be '
                           'copied next to the scripts)' % path)
    py = _extractor_python(python)
    here = os.path.dirname(os.path.abspath(__file__))
    for needed in ('cad_extract.py', 'cad_names.py'):
        if not os.path.isfile(os.path.join(here, needed)):
            raise RuntimeError('%s must be in the same folder as maya_cad_import.py (%s)' % (needed, here))
    out = os.path.join(tempfile.mkdtemp(prefix='cad2maya_'), os.path.basename(path) + '.cad2maya.json')
    code, so, se = _run_extractor([py, os.path.join(here, 'cad_extract.py'), path, '-o', out], on_wait)
    if code != 0:
        lines = [l.strip() for l in (se or so).strip().splitlines() if l.strip()]
        headline = lines[-1] if lines else 'no error text was produced'
        raise RuntimeError('The CAD file could not be processed: %s\n\nDetails from cad_extract.py:\n%s' % (headline, (so[-1500:] + '\n' + se[-1500:]).strip()))
    print(so.strip())
    return import_json(out, **kw)


def _load_source(source, python=None):
    """A .json from cad_extract.py, or a STEP/IGES file converted with the OpenCascade python."""
    if source.lower().endswith('.json'):
        with open(source) as f:
            return json.load(f)
    py = _extractor_python(python)
    out = os.path.join(tempfile.mkdtemp(prefix='cad2maya_'), 'diag.json')
    r = subprocess.run([py, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cad_extract.py'), source, '-o', out], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:])
    with open(out) as f:
        return json.load(f)


def _eval_loop_py(loop, t):
    """Evaluate a loop (Maya-convention knots) at parameter t -> (u, v). Pure Python (Cox-de Boor)."""
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
    x = y = den = 0.0
    for a in range(p + 1):
        idx = i - p + a
        w = loop['weights'][idx] if loop.get('rational') else 1.0
        x += N[a] * w * loop['cvs'][idx][0]
        y += N[a] * w * loop['cvs'][idx][1]
        den += N[a] * w
    return x / den, y / den


def _polyline_loop(loop, per):
    """The same boundary as a degree-1 closed curve with `per` vertices in every knot span of the original curve."""
    k = loop['knots']
    distinct = sorted(set(round(x, 12) for x in k))
    spans = list(zip(distinct[:-1], distinct[1:]))
    pts = []
    for a, b in spans:
        for q in range(per):
            pts.append(_eval_loop_py(loop, a + (b - a) * q / float(per)))
    pts.append(pts[0])
    return dict(degree=1, knots=[float(i) for i in range(len(pts))], cvs=[list(p) for p in pts], rational=False)


def bisect_outer(source, match, scale=0.1, python=None):
    """Give Maya the OUTER boundary of one trimmed face in several representations (the exact curve, an open-form copy,
    polylines with 24..768 vertices, a plain rectangle) and report which ones it trims with. Nothing is kept."""
    data = _load_source(source, python)
    for part in data['parts']:
        if match not in part['name']:
            continue
        for face in part['faces']:
            if not (face['trimmed'] and face.get('loops') and face.get('probe_info')):
                continue
            loop = face['loops'][0]
            print('=== %s: outer loop has %d control points, degree %d; %d probe points (%d outside the outer loop)' %
                  (part['name'], len(loop['cvs']), loop['degree'], len(face['probes']), sum(1 for k in face['probe_info'] if k[0] == 0)))
            variants = [('exact curve', loop, 'closed'), ('exact curve, OPEN form', loop, 'open'),
                        ('plain rectangle (control: shows 6)', _rect_loop(face), 'closed')]
            for per in (1, 2, 4, 8, 16):
                pl = _polyline_loop(loop, per)
                variants.append(('polyline, %d vertices' % (len(pl['cvs']) - 1), pl, 'closed'))
            try:
                src_tr, src_shape, dev, conv = _make_node(face, '_bo_src', None, scale, True)
            except Exception as e:
                print('   the untrimmed source surface already fails: %s' % str(e)[:300])
                return
            dom = _domain_of(_fn_for(src_shape), face)
            print('   %-30s %-10s %-11s %s' % ('representation', 'trimmed?', 'boundaries', 'probes disagreeing (isParamOnSurface, of %d)' % len(face['probes'])))
            for label, lp, form in variants:
                group = tw = out_tr = None
                try:
                    group = cmds.group(empty=True, name='_bo_curves')
                    tr = om.MFnTransform().create()
                    _make_curve(lp, face, dom, tr, 'plain', False, form)
                    t = cmds.parent(om.MFnDagNode(tr).fullPathName(), group)[0]
                    shape = cmds.listRelatives(t, shapes=True, fullPath=True)[0]
                    tw, out_tr, out_shape = _wire_trim(src_shape, [shape], 'explicit', '_bo_out')
                    cmds.delete(out_shape, constructionHistory=True)
                    out_shape = cmds.listRelatives(cmds.ls(out_tr, long=True)[0], shapes=True, fullPath=True)[0]
                    fn = _fn_for(out_shape)
                    u0, u1, v0, v1 = face['domain']
                    m0, m1, n0, n1 = _domain_of(fn, face)
                    bad = 0
                    for (u, v, _), (kind, hole) in zip(face['probes'], face['probe_info']):
                        pu, pv = m0 + (u - u0) / (u1 - u0) * (m1 - m0), n0 + (v - v0) / (v1 - v0) * (n1 - n0)
                        bad += int(bool(fn.isParamOnSurface(pu, pv)) != (kind != 0))
                    try:
                        trimmed, nb = _prop(fn, 'isTrimmedSurface'), fn.numBoundaries(0)
                    except Exception:
                        trimmed, nb = '?', '?'
                    print('   %-30s %-10s %-11s %d' % (label, trimmed, nb, bad))
                except Exception as e:
                    print('   %-30s FAILED: %s: %s' % (label, type(e).__name__, str(e)[:140]))
                for node in (out_tr, tw, group):
                    try:
                        if node and cmds.objExists(node):
                            cmds.delete(node)
                    except Exception:
                        pass
            cmds.delete(src_tr)
            print('   (0 disagreements = Maya trimmed correctly. The rectangle row should show 6: it is only a control that trimming works at all.)')
            return
    print('no trimmed face name contains %r' % match)


def _rect_loop(face):
    u0, u1, v0, v1 = face['domain']
    return dict(degree=1, knots=[0, 1, 2, 3, 4], cvs=[[u0, v0], [u1, v0], [u1, v1], [u0, v1], [u0, v0]], rational=False)


def bisect_trim(source, match, scale=0.1, python=None, max_faces=1):
    """Re-trim ONE surface with subsets of its boundaries and report which combinations Maya accepts. If the real trim
    fails, this shows which boundary (or which combination) the trim node cannot handle. Nothing is kept."""
    data = _load_source(source, python)
    shown = 0
    for part in data['parts']:
        if match not in part['name']:
            continue
        for face in part['faces']:
            if not (face['trimmed'] and face.get('loops')) or shown >= max_faces:
                continue
            if not face.get('probe_info'):
                print('this data has no probe labels: update cad_extract.py and run again')
                return
            shown += 1
            holes = list(range(1, len(face['loops'])))
            print('=== %s: %d boundary loop(s) (outer + %d hole(s)); %d probe points' % (part['name'], len(face['loops']), len(holes), len(face['probes'])))
            variants = [('outer only', 'loop', []), ('outer + ALL holes   <- the real trim', 'loop', holes)]
            variants += [('outer + hole %d only' % h, 'loop', [h]) for h in holes]
            variants += [('plain rectangle only', 'rect', []), ('plain rectangle + ALL holes', 'rect', holes)]
            variants += [('plain rectangle + hole %d' % h, 'rect', [h]) for h in holes]
            variants += [('native boundary + ALL holes', 'native', holes)]
            variants += [('native boundary + hole %d' % h, 'native', [h]) for h in holes]
            try:
                src_tr, src_shape, dev, conv = _make_node(face, '_bis_src', None, scale, True)
            except Exception as e:
                print('   the untrimmed source surface already fails: %s' % str(e)[:300])
                return
            dom = _domain_of(_fn_for(src_shape), face)
            print('   %-42s %-10s %-11s %-34s %s' % ('variant', 'trimmed?', 'boundaries', 'probes disagreeing (of %d)' % len(face['probes']), ''))
            print('   %-42s %-10s %-11s %-17s %-17s' % ('', '', '', 'isPointInTrimmed', 'isParamOnSurface'))
            for label, outer_spec, hs in variants:
                if outer_spec == 'native' and not hs:
                    continue
                group = tw = out_tr = None
                try:
                    loops = ([face['loops'][0]] if outer_spec == 'loop' else [_rect_loop(face)] if outer_spec == 'rect' else []) + [face['loops'][h] for h in hs]
                    group = cmds.group(empty=True, name='_bis_curves')
                    trs = []
                    for loop in loops:
                        tr = om.MFnTransform().create()
                        _make_curve(loop, face, dom, tr, _learned['weight'] or 'plain', False)
                        trs.append(cmds.parent(om.MFnDagNode(tr).fullPathName(), group)[0])
                    shapes = [cmds.listRelatives(t, shapes=True, fullPath=True)[0] for t in trs]
                    tw, out_tr, out_shape = _wire_trim(src_shape, shapes, 'native' if outer_spec == 'native' else 'explicit', '_bis_out')
                    cmds.delete(out_shape, constructionHistory=True)
                    out_shape = cmds.listRelatives(cmds.ls(out_tr, long=True)[0], shapes=True, fullPath=True)[0]
                    fn = _fn_for(out_shape)
                    u0, u1, v0, v1 = face['domain']
                    m0, m1, n0, n1 = _domain_of(fn, face)
                    bad = {'isPointInTrimmedRegion': 0, 'isParamOnSurface': 0}
                    for (u, v, _), (kind, hole) in zip(face['probes'], face['probe_info']):
                        outer_in = (kind != 0) if outer_spec == 'loop' else True
                        expected = outer_in and not (kind == 2 and hole in hs)
                        pu, pv = m0 + (u - u0) / (u1 - u0) * (m1 - m0), n0 + (v - v0) / (v1 - v0) * (n1 - n0)
                        for q in bad:
                            try:
                                bad[q] += int(bool(getattr(fn, q)(pu, pv)) != expected)
                            except Exception:
                                bad[q] += 1
                    try:
                        trimmed, nb = _prop(fn, 'isTrimmedSurface'), fn.numBoundaries(0)
                    except Exception:
                        trimmed, nb = '?', '?'
                    print('   %-42s %-10s %-11s %-17s %-17s' % (label, trimmed, nb, bad['isPointInTrimmedRegion'], bad['isParamOnSurface']))
                except Exception as e:
                    print('   %-42s FAILED: %s: %s' % (label, type(e).__name__, str(e)[:160]))
                for node in (out_tr, tw, group):
                    try:
                        if node and cmds.objExists(node):
                            cmds.delete(node)
                    except Exception:
                        pass
            cmds.delete(src_tr)
            print('   (0 = Maya agrees with the expected region for that variant. Send me this table.)')
    if shown == 0:
        print('no trimmed face name contains %r' % match)


def _fmt(v, n=4):
    try:
        return '[' + ', '.join(('%.*g' % (n, x)) for x in v) + ']'
    except Exception:
        return str(v)


def _diagnose_trim(face, scale):
    """Build the trimmed result once (explicit outer boundary, normal direction) and report what Maya actually produced."""
    print('  -- TRIM diagnosis (explicit outer boundary, not flipped)')
    try:
        src_tr, src_shape, dev, conv = _make_node(face, '_diag_src', None, scale, True)
    except Exception as e:
        print('     the untrimmed source surface already fails: %s' % str(e)[:300])
        return
    group = tw = out_tr = None
    try:
        dom = _domain_of(_fn_for(src_shape), face)
        group = cmds.group(empty=True, name='_diag_curves')
        trs = []
        for loop in face['loops']:
            tr = om.MFnTransform().create()
            _make_curve(loop, face, dom, tr, _learned['weight'] or 'plain', False)
            trs.append(cmds.parent(om.MFnDagNode(tr).fullPathName(), group)[0])
        shapes = [cmds.listRelatives(t, shapes=True, fullPath=True)[0] for t in trs]
        print('     %d boundary curve(s) created (outer + %d hole(s)); control points per loop: %s' % (len(shapes), len(shapes) - 1, [len(l['cvs']) for l in face['loops']]))
        tw, out_tr, out_shape = _wire_trim(src_shape, shapes, 'explicit', '_diag_out')
        cmds.delete(out_shape, constructionHistory=True)
        out_shape = cmds.listRelatives(cmds.ls(out_tr, long=True)[0], shapes=True, fullPath=True)[0]
        fn = _fn_for(out_shape)
        for label in ('isTrimmedSurface', 'numRegions'):
            try:
                print('     %-18s %s' % (label, _prop(fn, label)))
            except Exception as e:
                print('     %-18s unavailable (%s: %s)' % (label, type(e).__name__, e))
        try:
            print('     numBoundaries(0)   %s   (expected %d: one outer + holes)' % (fn.numBoundaries(0), len(face['loops'])))
        except Exception as e:
            print('     numBoundaries(0)   unavailable (%s: %s)' % (type(e).__name__, e))
        u0, u1, v0, v1 = face['domain']
        m0, m1, n0, n1 = _domain_of(fn, face)
        rows = []
        for u, v, inside in face['probes']:
            pu, pv = m0 + (u - u0) / (u1 - u0) * (m1 - m0), n0 + (v - v0) / (v1 - v0) * (n1 - n0)
            ans = {}
            for q in ('isPointInTrimmedRegion', 'isParamOnSurface'):
                try:
                    ans[q] = bool(getattr(fn, q)(pu, pv))
                except Exception as e:
                    ans[q] = 'ERR'
            rows.append((u, v, inside, ans))
        for q in ('isPointInTrimmedRegion', 'isParamOnSurface'):
            bad = [r for r in rows if r[3][q] != bool(r[2])]
            print('     %-24s disagrees with OpenCascade on %d of %d probes' % (q, len(bad), len(rows)))
        shown = [r for r in rows if any(r[3][q] != bool(r[2]) for q in r[3])][:8]
        if shown:
            print('     first disagreeing probes  (u, v | OpenCascade says inside? | isPointInTrimmedRegion | isParamOnSurface):')
            for u, v, inside, ans in shown:
                print('       (%.4f, %.4f) | %s | %s | %s' % (u, v, bool(inside), ans['isPointInTrimmedRegion'], ans['isParamOnSurface']))
    except Exception as e:
        print('     trim step FAILED -> %s: %s' % (type(e).__name__, e))
    for node in (out_tr, tw, group, src_tr):
        try:
            if node and cmds.objExists(node):
                cmds.delete(node)
        except Exception:
            pass


def diagnose(source, match, scale=0.1, python=None, max_faces=3):
    """Build faces whose name contains `match` step by step and print what Maya does at every stage.
    `source` is a .json from cad_extract.py, or a STEP/IGES file (converted with the OpenCascade python). Nothing is kept."""
    data = _load_source(source, python)
    shown = 0
    for part in data['parts']:
        if match not in part['name']:
            continue
        for face in part['faces']:
            if shown >= max_faces:
                return
            shown += 1
            cu, cvv = _closure(face)
            ext = [(max(c[k] for row in face['cvs'] for c in row) - min(c[k] for row in face['cvs'] for c in row)) * scale for k in range(3)]
            print('=== %s  (face %s)' % (part['name'], face.get('index')))
            print('    degree (%d,%d)  CVs (%d,%d)  rational=%s  closed (U,V)=(%s,%s)  trimmed=%s' %
                  (face['degree_u'], face['degree_v'], face['ncv_u'], face['ncv_v'], face['rational'], cu, cvv, face['trimmed']))
            print('    OUR parameter domain: u[%.6g, %.6g]  v[%.6g, %.6g]   size in Maya cm: %.5g x %.5g x %.5g (scale=%g)' % (*face['domain'], *ext, scale))
            print('    OUR knots_u: %s\n    OUR knots_v: %s' % (_fmt(face['knots_u']), _fmt(face['knots_v'])))
            for forms in _form_candidates(face):
                tr = om.MFnTransform().create()
                print('  -- try forms=%s order=u_major weights=plain' % '+'.join(forms))
                try:
                    shape = _build_surface(face, tr, scale, 'u_major', 'plain', forms)
                    print('     create: ok (shape type %s)' % shape.apiTypeStr)
                except Exception as e:
                    print('     create FAILED -> %s' % e)
                    cmds.delete(om.MFnDagNode(tr).fullPathName())
                    continue
                fn = om.MFnNurbsSurface(shape)
                for label in ('numCVsInU', 'numCVsInV', 'degreeInU', 'degreeInV', 'formInU', 'formInV'):
                    try:
                        print('     %-12s %s' % (label, _prop(fn, label)))
                    except Exception as e:
                        print('     %-12s unavailable (%s: %s)' % (label, type(e).__name__, e))
                for label in ('knotsInU', 'knotsInV'):
                    try:
                        print('     MAYA %-9s %s' % (label, _fmt(getattr(fn, label)())))
                    except Exception as e:
                        print('     MAYA %-9s unavailable (%s: %s)' % (label, type(e).__name__, e))
                d = _maya_domain(fn)
                print('     MAYA parameter domain: %s' % ('unavailable (no accessor worked)' if d is None else 'u[%.6g, %.6g]  v[%.6g, %.6g]' % d))
                try:
                    dev = _measure(shape, face, scale)
                    print('     measured against OpenCascade: max deviation %.3g cm' % dev)
                except Exception as e:
                    print('     measuring FAILED -> %s' % e)
                cmds.delete(om.MFnDagNode(tr).fullPathName())
            try:
                tname, sname, dev, conv = _make_node_mel(face, '_diag_mel', None, scale, True, 1e-3)
                print('  -- cmds.surface route: ok (deviation %.3g cm)' % dev)
                cmds.delete(tname)
            except Exception as e:
                print('  -- cmds.surface route FAILED -> %s' % e)
            if face['trimmed'] and face.get('loops'):
                _diagnose_trim(face, scale)
    if shown == 0:
        print('no face name contains %r' % match)


def selftest(scale=1.0):
    """Build two tiny surfaces with known geometry in the current Maya and report which conventions it uses.
    Rational surface: quarter cylinder r=1, h=2 (a degree-2 circle arc needs weights). Non-rational: 3x4 CV patch.
    Everything created is deleted afterwards."""
    import math
    w = math.sqrt(0.5)
    quarter = dict(degree_u=2, degree_v=1, ncv_u=3, ncv_v=2, knots_u=[0, 0, 1, 1], knots_v=[0, 1], rational=True,
                   cvs=[[[1, 0, 0], [1, 0, 2]], [[1, 1, 0], [1, 1, 2]], [[0, 1, 0], [0, 1, 2]]],
                   weights=[[1, 1], [w, w], [1, 1]], domain=[0, 1, 0, 1], trimmed=False, index=1)
    # check points from the closed-form quarter cylinder: u maps to the angle through the rational parameterization
    chk = []
    for fu in (0.17, 0.5, 0.83):
        for fv in (0.17, 0.5, 0.83):
            b0, b1, b2 = (1 - fu) ** 2, 2 * fu * (1 - fu), fu ** 2
            den = b0 + b1 * w + b2
            chk.append([fu, fv, (b0 + b1 * w) / den, (b1 * w + b2) / den, 2 * fv])
    quarter['check'] = chk
    cvs = [[[i, j, (i * 7 + j * 3) % 5 * 0.3] for j in range(4)] for i in range(3)]        # asymmetric on purpose: 3 x 4
    patch = dict(degree_u=2, degree_v=3, ncv_u=3, ncv_v=4, knots_u=[0, 0, 1, 1], knots_v=[0, 0, 0, 1, 1, 1], rational=False,
                 cvs=cvs, domain=[0, 1, 0, 1], trimmed=False, index=2)
    patch['check'] = _bezier_checks(patch)
    plate, tri = _trim_test_faces()
    data = dict(format=FORMATS[-1], source='selftest', units='mm', stats={}, failures=[],
                parts=[dict(name='selftest', path=[], color=None, faces=[quarter, patch, plate, tri])])
    _learned.update(order=None, weight=None, probe=None, trim_variant=None)
    rep = import_data(data, scale=scale, root_name='cad2maya_selftest', trim_fallback=False)
    cmds.delete(rep['root'])
    if rep.get('shading'):
        cmds.delete(rep['shading'])
    ok = rep['built'] == 4 and not rep['failed'] and not rep['trim_fallback']
    if ok:
        print('SELFTEST PASSED - conventions on this Maya: cv order=%s, rational weights=%s, trim method=%s, region query=%s'
              % (_learned['order'], _learned['weight'], _learned['trim_variant'], _learned['probe']))
    else:
        print('SELFTEST FAILED - copy the report above and send it to me. A TypeError/AttributeError in it means this Maya\'s '
              'Python API differs from the documented signature; a deviation number means the surface was built but looks different.')
    return rep


def _trim_test_faces():
    """Two flat 10 x 10 test faces over the parameter domain [0,1]^2 whose trimmed regions are known in closed form:
    a plate with a rational circular hole, and a triangle."""
    import math
    cvs = [[[0, 0, 0], [0, 10, 0]], [[10, 0, 0], [10, 10, 0]]]                         # cvs[iu][iv]: u -> x, v -> y
    base = dict(degree_u=1, degree_v=1, ncv_u=2, ncv_v=2, knots_u=[0, 1], knots_v=[0, 1], rational=False, cvs=cvs,
                domain=[0, 1, 0, 1], trimmed=True)
    base['check'] = _bezier_checks(base)
    w = math.sqrt(0.5)
    c, r = 0.5, 0.2
    ccw = [[c + r, c], [c + r, c + r], [c, c + r], [c - r, c + r], [c - r, c], [c - r, c - r], [c, c - r], [c + r, c - r], [c + r, c]]
    hole = dict(degree=2, knots=[0, 0, 1, 1, 2, 2, 3, 3, 4, 4], cvs=ccw[::-1], rational=True,
                weights=[1, w, 1, w, 1, w, 1, w, 1])                                   # clockwise = a hole
    rect = dict(degree=1, knots=[0, 1, 2, 3, 4], cvs=[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], rational=False)
    probes = []
    for i in range(11):
        for j in range(11):
            u, v = 0.03 + 0.94 * i / 10, 0.03 + 0.94 * j / 10
            d = math.hypot(u - c, v - c)
            if abs(d - r) < 0.03:
                continue
            probes.append([u, v, 1 if d > r else 0])
    plate = dict(base, loops=[rect, hole], outer_is_domain=True, probes=probes, area=100.0 * (1.0 - math.pi * r * r), index=3)
    tri_pts = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]
    tri_loop = dict(degree=1, knots=[0, 1, 2, 3], cvs=[tri_pts[0], tri_pts[1], tri_pts[2], tri_pts[0]], rational=False)

    def seg_dist(p, a, b):
        ax, ay, bx, by = a[0], a[1], b[0], b[1]
        t = max(0.0, min(1.0, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2)))
        return math.hypot(p[0] - (ax + t * (bx - ax)), p[1] - (ay + t * (by - ay)))

    def inside_tri(p):
        a, b, cc = tri_pts
        s1 = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        s2 = (cc[0] - b[0]) * (p[1] - b[1]) - (cc[1] - b[1]) * (p[0] - b[0])
        s3 = (a[0] - cc[0]) * (p[1] - cc[1]) - (a[1] - cc[1]) * (p[0] - cc[0])
        return s1 > 0 and s2 > 0 and s3 > 0
    tprobes = []
    for i in range(11):
        for j in range(11):
            p = (0.03 + 0.94 * i / 10, 0.03 + 0.94 * j / 10)
            if min(seg_dist(p, tri_pts[k], tri_pts[(k + 1) % 3]) for k in range(3)) < 0.03:
                continue
            tprobes.append([p[0], p[1], 1 if inside_tri(p) else 0])
    tri = dict(base, loops=[tri_loop], outer_is_domain=False, probes=tprobes, area=100.0 * 0.32, index=4)
    return plate, tri


def _bezier_checks(face):
    """Check points for a single-span (Bezier) non-rational test patch, computed independently with Bernstein bases."""
    from math import comb
    p, q = face['degree_u'], face['degree_v']
    out = []
    for fu in (0.17, 0.5, 0.83):
        for fv in (0.17, 0.5, 0.83):
            pt = [0.0, 0.0, 0.0]
            for i in range(p + 1):
                bi = comb(p, i) * fu ** i * (1 - fu) ** (p - i)
                for j in range(q + 1):
                    bj = comb(q, j) * fv ** j * (1 - fv) ** (q - j)
                    for k in range(3):
                        pt[k] += bi * bj * face['cvs'][i][j][k]
            out.append([fu, fv] + pt)
    return out
