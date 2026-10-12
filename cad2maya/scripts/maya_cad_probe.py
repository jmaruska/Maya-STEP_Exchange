"""
maya_cad_probe.py - READ-ONLY probe for the Maya -> CAD exporter. It changes nothing in the scene.

Select a few NURBS surfaces (include a trimmed one, ideally one with holes, and a surface you imported with the CAD importer), then:

    import sys; sys.path.append('/folder/with/the/scripts')
    import maya_cad_probe as probe
    probe.run()                 # prints a report; paste it back

It records what Maya's API actually returns for the things an exporter needs: the surface data, which parameter space the trim
boundaries are in, and how a trimmed surface exposes its boundaries. Every call is wrapped, so a missing method is reported, not fatal.
"""
import maya.api.OpenMaya as om
import maya.cmds as cmds


def _try(label, fn):
    try:
        return fn()
    except Exception as e:                                    # a missing method or a failing call is DATA for us
        print('      %-34s FAILED: %s: %s' % (label, type(e).__name__, str(e).splitlines()[0][:110] if str(e) else ''))
        return None


def _fmt(v, n=4):
    if isinstance(v, (om.MPoint, om.MVector)):
        return '(%s)' % ', '.join('%.*g' % (n, c) for c in v)
    if isinstance(v, (list, tuple)):
        return '[%s]' % ', '.join(_fmt(x, n) for x in v)
    if isinstance(v, float):
        return '%.*g' % (n, v)
    return str(v)


def _shapes():
    sel = cmds.ls(selection=True, long=True) or []
    out = []
    for s in sel:
        shapes = cmds.listRelatives(s, shapes=True, fullPath=True, type='nurbsSurface') if cmds.nodeType(s) != 'nurbsSurface' else [s]
        out += shapes or []
    return out


def _curve_report(obj, tag):
    """What a boundary edge looks like (it comes back as curve data)."""
    c = _try('MFnNurbsCurve(edge object)', lambda: om.MFnNurbsCurve(obj))
    if c is None:
        return
    deg = _try('degree', lambda: c.degree)
    ncv = _try('numCVs', lambda: c.numCVs)
    form = _try('form', lambda: c.form)
    cvs = _try('cvPositions()', lambda: c.cvPositions())
    knots = _try('knots()', lambda: list(c.knots()))
    print('      %s: degree %s, %s CVs, form %s' % (tag, deg, ncv, form))
    if cvs:
        print('         first CV %s   last CV %s   (z of first CV: %s)' % (_fmt(cvs[0]), _fmt(cvs[-1]), _fmt(cvs[0][2])))
        xs, ys = [p[0] for p in cvs], [p[1] for p in cvs]
        print('         extent x[%s, %s]  y[%s, %s]' % (_fmt(min(xs)), _fmt(max(xs)), _fmt(min(ys)), _fmt(max(ys))))
    if knots:
        print('         knots %s ... %s  (%d)' % (_fmt(knots[:3]), _fmt(knots[-2:]), len(knots)))


def probe_surface(shape):
    sel = om.MSelectionList()
    sel.add(shape)
    dag = sel.getDagPath(0)
    fn = om.MFnNurbsSurface(dag)
    print('=== %s' % shape)
    print('   degree (%s, %s)   CVs (%s, %s)   forms (%s, %s)' % (fn.degreeInU, fn.degreeInV, fn.numCVsInU, fn.numCVsInV, fn.formInU, fn.formInV))
    ku, kv = list(fn.knotsInU()), list(fn.knotsInV())
    print('   knots U %s...%s (%d)   V %s...%s (%d)' % (_fmt(ku[:2]), _fmt(ku[-2:]), len(ku), _fmt(kv[:2]), _fmt(kv[-2:]), len(kv)))
    print('   knot domain U %s  V %s' % (_fmt(fn.knotDomainInU), _fmt(fn.knotDomainInV)))
    cvs = _try('cvPositions(kObject)', lambda: fn.cvPositions(om.MSpace.kObject))
    cvw = _try('cvPositions(kWorld)', lambda: fn.cvPositions(om.MSpace.kWorld))
    if cvs and cvw:
        print('   first CV  object space %s   world space %s' % (_fmt(cvs[0]), _fmt(cvw[0])))
    print('   transform: %s' % _fmt(list(cmds.xform(cmds.listRelatives(shape, parent=True, fullPath=True)[0], query=True, matrix=True, worldSpace=True)), 3))
    print('   history connections into the shape: %s' % (cmds.listConnections(shape, source=True, destination=False) or 'none'))
    trimmed = _try('isTrimmedSurface', lambda: fn.isTrimmedSurface())
    print('   trimmed: %s' % trimmed)
    if not trimmed:
        return
    nreg = _try('numRegions()', lambda: fn.numRegions())
    print('   regions: %s' % nreg)
    for r in range(nreg or 0):
        nb = _try('numBoundaries(%d)' % r, lambda: fn.numBoundaries(r))
        print('   region %d: %s boundaries' % (r, nb))
        for b in range(nb or 0):
            btype = _try('boundaryType(%d,%d)' % (r, b), lambda: fn.boundaryType(r, b))
            ne = _try('numEdges(%d,%d)' % (r, b), lambda: fn.numEdges(r, b))
            print('    boundary %d: type %s, %s edge(s)' % (b, btype, ne))
            for e in range(min(ne or 0, 3)):                      # the first three edges are enough to see the conventions
                for param in (True, False):
                    obj = _try('edge(%d,%d,%d, paramEdge=%s)' % (r, b, e, param), lambda: fn.edge(r, b, e, param))
                    if obj is not None:
                        _curve_report(obj, 'edge %d, paramEdge=%s' % (e, param))
            if (ne or 0) > 3:
                print('      (%d more edges not shown)' % (ne - 3))


def run():
    try:
        shapes = _shapes()
    except Exception as e:
        print('Could not read the selection: %s: %s' % (type(e).__name__, e))
        return
    if not shapes:
        print('Select one or more NURBS surfaces first.')
        return
    print('cad2maya probe: %d surface(s). This changes nothing in the scene.' % len(shapes))
    for s in shapes[:6]:
        try:
            probe_surface(s)
        except Exception as e:
            print('   probe of %s stopped: %s: %s' % (s, type(e).__name__, e))
    if len(shapes) > 6:
        print('(%d more selected surfaces not probed)' % (len(shapes) - 6))
