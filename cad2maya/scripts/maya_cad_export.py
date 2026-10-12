"""
maya_cad_export.py - Maya NURBS surfaces -> STEP / IGES (the other half of cad2maya).

    import maya_cad_export as mce
    mce.export_cad('/path/out.step', selection_only=True)

Runs inside Maya. It reads each NURBS surface (world space), its trim boundaries, the group hierarchy, names and shader colours,
writes them as cad2maya JSON and hands that to cad_write.py, which runs in the Python that has OpenCascade.

Principles (truth over repair):
  * geometry is written as it is in Maya; nothing is rebuilt or approximated here;
  * a surface whose trims cannot be read is LEFT OUT and reported, never exported untrimmed (that would add geometry);
  * groups made by the importer keep their original CAD names (cadName) and roles (cadKind); other groups are read by shape:
    a group of surfaces is a part, a group of groups an assembly, a part inside a part a body;
  * sizes: surfaces under a group imported by CAD to Maya are scaled back by that import's factor (cadImportScale), so the file
    gets its original numbers; anything else is converted from Maya's centimetres to millimetres.
"""
import json
import os
import re
import tempfile

import maya.cmds as cmds
import maya.api.OpenMaya as om

import cad_names
from maya_cad_import import ImportCancelled, _extractor_python, _run_extractor

FORMAT = 'cad2maya/3'
CAMERAS = {'persp', 'top', 'front', 'side', 'bottom', 'left', 'back'}


# ------------------------------------------------------------------------------------------------ small helpers
def _val(obj, name, *args):
    """Maya API 2.0 exposes some of these as properties and some as methods: accept either."""
    v = getattr(obj, name)
    return v(*args) if callable(v) else v


def _attr(node, attr):
    try:
        if cmds.attributeQuery(attr, node=node, exists=True):
            return cmds.getAttr(node + '.' + attr)
    except Exception:
        pass
    return None


def _short(node):
    return node.split('|')[-1]


def _clean_label(name):
    """A Maya name back to a CAD-ish label: drop the importer's suffixes."""
    for suf in ('_UNTRIMMED_FALLBACK', '_PARTIAL_TRIM', '_grp'):
        if name.endswith(suf):
            name = name[:-len(suf)]
    return re.sub(r'_f\d+$', '', name)


def _is_type(node, t):
    try:
        return cmds.nodeType(node) == t
    except Exception:
        return False


def _surface_shapes(node):
    out = []
    for s in cmds.listRelatives(node, shapes=True, fullPath=True) or []:
        if _is_type(s, 'nurbsSurface') and not cmds.getAttr(s + '.intermediateObject'):
            out.append(s)
    return out


def _child_transforms(node):
    return [c for c in (cmds.listRelatives(node, children=True, fullPath=True) or []) if _is_type(c, 'transform')]


def _color(shape):
    try:
        for sg in cmds.listConnections(shape, type='shadingEngine') or []:
            if sg == 'initialShadingGroup':
                continue                                              # Maya's default grey is not a designed colour
            for mat in cmds.listConnections(sg + '.surfaceShader', source=True, destination=False) or []:
                if mat == 'lambert1' or mat.startswith('cad_mtl_default'):
                    continue                                          # nor is the grey the importer gives uncoloured faces
                for attr in ('color', 'baseColor'):
                    c = _attr(mat, attr)
                    if c:
                        c = c[0] if isinstance(c, (list, tuple)) and isinstance(c[0], (list, tuple)) else c
                        return [round(float(x), 5) for x in c[:3]]
    except Exception:
        pass
    return None


def _file_factor(node):
    """Maya units -> file millimetres for a surface: undo the import scale of the nearest imported root, else cm -> mm."""
    n = node
    while n:
        s = _attr(n, 'cadImportScale')
        if s:
            return 1.0 / float(s)
        parents = cmds.listRelatives(n, parent=True, fullPath=True)
        n = parents[0] if parents else None
    return 10.0


# ------------------------------------------------------------------------------------------------ reading weights truthfully
WEIGHT_READINGS = ('as given', 'non-rational', 'premultiplied')
WEIGHT_STATS = {}          # reading -> count, for the report


def _basis(knots, p, n, t):
    """(span, basis values) for Maya-convention knots (n + p - 1 values)."""
    kn = [knots[0]] + list(knots) + [knots[-1]]
    t = min(max(t, kn[p]), kn[n] - 1e-12)
    i = p
    while i < n - 1 and t >= kn[i + 1]:
        i += 1
    N = [1.0] + [0.0] * p
    left, right = [0.0] * (p + 1), [0.0] * (p + 1)
    for j in range(1, p + 1):
        left[j] = t - kn[i + 1 - j]
        right[j] = kn[i + j] - t
        saved = 0.0
        for r in range(j):
            den = right[r + 1] + left[j - r]
            tmp = N[r] / den if den else 0.0
            N[r] = saved + right[r + 1] * tmp
            saved = left[j - r] * tmp
        N[j] = saved
    return i, N


def _candidates(xyz, sources):
    """The ways the points and weights could be meant, from each weight SOURCE ({name: weights or None}):
    [(name, points, weights or None)]. Sources that give no positive weights cannot describe rational geometry."""
    out = [('non-rational', xyz, None)]
    for src, w in sources.items():
        if w and len(w) == len(xyz) and all(x > 0 for x in w):
            tag = '' if src == 'cvPositions' else ' (%s)' % src
            out.append(('as given' + tag, xyz, w))
            out.append(('premultiplied' + tag, [tuple(c / wi for c in p) for p, wi in zip(xyz, w)], w))
    return out


def _sample(w, n=4):
    return '[%s%s]' % (', '.join('%.4g' % x for x in (w or [])[:n]), ', ...' if w and len(w) > n else '')


def _eval_curve(knots, p, pts, w, t):
    n = len(pts)
    i, N = _basis(knots, p, n, t)
    num, den = [0.0] * len(pts[0]), 0.0
    for a in range(p + 1):
        k = i - p + a
        c = N[a] * (w[k] if w else 1.0)
        num = [x + c * y for x, y in zip(num, pts[k])]
        den += c
    return [x / den for x in num]


def _eval_surface(ku, kv, du, dv, nu, nv, pts, w, u, v):
    iu, Nu = _basis(ku, du, nu, u)
    iv, Nv = _basis(kv, dv, nv, v)
    num, den = [0.0, 0.0, 0.0], 0.0
    for a in range(du + 1):
        for b in range(dv + 1):
            k = (iu - du + a) * nv + (iv - dv + b)
            c = Nu[a] * Nv[b] * (w[k] if w else 1.0)
            num = [x + c * y for x, y in zip(num, pts[k])]
            den += c
    return [x / den for x in num]


def _pick_reading(evaluate, truth, samples, size, what):
    """Try each reading of the weights and keep the one that reproduces Maya's own evaluation BEST (not the first acceptable one:
    with weights near 1 a wrong reading can also land inside the tolerance). It must still be within the tolerance."""
    scored, errors = [], []
    for name, pts, w in evaluate:
        try:
            worst = max(max(abs(a - b) for a, b in zip(evaluate.fn(pts, w, s), truth[k])) for k, s in enumerate(samples))
        except Exception as e:
            errors.append('%s: %s' % (name, e))
            continue
        scored.append((worst, name, pts, w))
        errors.append('%s off by %.3g' % (name, worst))
    if scored:
        worst, name, pts, w = min(scored, key=lambda x: x[0])
        if worst <= 1e-6 * max(size, 1e-9) + 1e-9:
            WEIGHT_STATS[what + ' ' + name] = WEIGHT_STATS.get(what + ' ' + name, 0) + 1
            return name, pts, w
    raise RuntimeError('no reading of its weights reproduces Maya\'s own %s (%s)' % (what, '; '.join(errors)))


class _Readings(list):
    fn = None


# ------------------------------------------------------------------------------------------------ one surface
POLY_PER_SPAN = 24


def _curve_dict(obj, factor_uv=1.0):
    """One trim edge as a 2D curve in the surface's (u, v) space. Its points and weights are checked against Maya's own evaluation of the
    curve; if no source of weights reproduces it, the curve is taken from that evaluation itself (a dense polyline: no weights needed)."""
    c = om.MFnNurbsCurve(obj)
    raw = list(c.cvPositions())
    p = int(_val(c, 'degree'))
    knots = [float(k) for k in c.knots()]
    xy = [(float(q.x), float(q.y)) for q in raw]
    n = len(xy)
    sources = {'cvPositions': [float(q.w) for q in raw]}
    try:
        sources['cvPosition'] = [float(c.cvPosition(i).w) for i in range(n)]
    except Exception:
        pass
    a, b = knots[p - 1], knots[n - 1]
    samples = [a + (b - a) * f for f in (0.0, 0.13, 0.37, 0.5, 0.71, 0.94, 1.0)]
    size = max(max(x for x, _ in xy) - min(x for x, _ in xy), max(y for _, y in xy) - min(y for _, y in xy))
    try:
        truth = []
        for t in samples:
            q = c.getPointAtParam(t, om.MSpace.kObject)
            truth.append((float(q.x), float(q.y)))
    except Exception:
        truth = None
    if truth is None:                                                 # cannot check: only the unambiguous case
        w = sources['cvPositions']
        if len(set(round(x, 12) for x in w)) <= 1:
            return dict(degree=p, knots=knots, cvs=[[x, y] for x, y in xy], rational=False, weights=None)
        raise RuntimeError('trim curve weights could not be checked against Maya (cvPositions w %s)' % _sample(w))
    readings = _Readings(_candidates(xy, sources))
    readings.fn = lambda pts, ww, t: _eval_curve(knots, p, pts, ww, t)
    try:
        name, pts, ww = _pick_reading(readings, truth, samples, size, 'trim curve')
        return dict(degree=p, knots=knots, cvs=[[x, y] for x, y in pts], rational=ww is not None, weights=list(ww) if ww else None)
    except RuntimeError:
        pass
    # no source of weights reproduces the curve: take it from Maya's OWN evaluation, which needs no weights
    distinct = sorted(set(knots[p - 1:n]))
    ts = []
    for s0, s1 in zip(distinct[:-1], distinct[1:]):
        ts += [s0 + (s1 - s0) * k / float(POLY_PER_SPAN) for k in range(POLY_PER_SPAN)]
    ts.append(distinct[-1])
    pts = []
    for t in ts:
        q = c.getPointAtParam(t, om.MSpace.kObject)
        pts.append([float(q.x), float(q.y)])
    WEIGHT_STATS['trim curve from Maya\'s evaluation (polyline, %d per span)' % POLY_PER_SPAN] = WEIGHT_STATS.get('trim curve from Maya\'s evaluation (polyline, %d per span)' % POLY_PER_SPAN, 0) + 1
    return dict(degree=1, knots=[float(i) for i in range(len(pts))], cvs=pts, rational=False, weights=None)


def read_surface(shape, factor):
    """The face dicts for one NURBS surface (one per trimmed region). Raises with a reason if its trims cannot be read."""
    sel = om.MSelectionList()
    sel.add(shape)
    fn = om.MFnNurbsSurface(sel.getDagPath(0))
    du, dv = int(_val(fn, 'degreeInU')), int(_val(fn, 'degreeInV'))
    nu, nv = int(_val(fn, 'numCVsInU')), int(_val(fn, 'numCVsInV'))
    ku, kv = [float(k) for k in fn.knotsInU()], [float(k) for k in fn.knotsInV()]
    raw = list(fn.cvPositions(om.MSpace.kWorld))
    if len(raw) != nu * nv:
        raise RuntimeError('expected %d x %d control points, got %d' % (nu, nv, len(raw)))
    xyz = [(float(q.x), float(q.y), float(q.z)) for q in raw]
    wraw = [float(q.w) for q in raw]
    sources = {'cvPositions': wraw}
    try:
        sources['cvPosition'] = [float(fn.cvPosition(i, j, om.MSpace.kWorld).w) for i in range(nu) for j in range(nv)]
    except Exception:
        pass
    try:
        wa = cmds.getAttr('%s.weights[0:%d]' % (shape, nu * nv - 1))
        if wa is not None:
            sources['weights attribute'] = [float(x) for x in (wa if isinstance(wa, (list, tuple)) else [wa])]
    except Exception:
        pass
    # Maya knot convention: n + degree - 1 values; the valid range is [k[degree-1], k[n-1]]
    domain = [ku[du - 1], ku[nu - 1], kv[dv - 1], kv[nv - 1]]
    # which reading of Maya's weights reproduces Maya's OWN evaluation of this surface?
    samples = [(domain[0] + (domain[1] - domain[0]) * fu, domain[2] + (domain[3] - domain[2]) * fv) for fu in (0.0, 0.31, 0.5, 0.77, 1.0) for fv in (0.0, 0.43, 0.66, 1.0)]
    size = max(max(c[i] for c in xyz) - min(c[i] for c in xyz) for i in range(3))
    try:
        truth = []
        for u, v in samples:
            q = fn.getPointAtParam(u, v, om.MSpace.kWorld)
            truth.append((float(q.x), float(q.y), float(q.z)))
    except Exception:
        truth = None
    if truth is None:
        if len(set(round(x, 12) for x in wraw)) <= 1:
            reading, pts, ww = 'non-rational', xyz, None
        elif all(x > 0 for x in wraw):
            reading, pts, ww = 'as given', xyz, wraw
        else:
            raise RuntimeError('surface weights read from Maya are not positive (%.3g .. %.3g) and could not be checked' % (min(wraw), max(wraw)))
    else:
        readings = _Readings(_candidates(xyz, sources))
        readings.fn = lambda pts, ww, s: _eval_surface(ku, kv, du, dv, nu, nv, pts, ww, s[0], s[1])
        try:
            reading, pts, ww = _pick_reading(readings, truth, samples, size, 'surface')
        except RuntimeError as e:
            raise RuntimeError('%s. Weights Maya gave: %s' % (e, '; '.join('%s %s' % (k, _sample(v)) for k, v in sources.items())))
    cvs = [[[pts[i * nv + j][0] * factor, pts[i * nv + j][1] * factor, pts[i * nv + j][2] * factor] for j in range(nv)] for i in range(nu)]
    rational = ww is not None
    weights = [[ww[i * nv + j] for j in range(nv)] for i in range(nu)] if rational else None
    base = dict(degree_u=du, degree_v=dv, ncv_u=nu, ncv_v=nv, knots_u=ku, knots_v=kv, cvs=cvs, rational=rational,
                weights=weights, domain=domain, reversed=False, trimmed=False)
    trimmed = False
    try:
        trimmed = bool(_val(fn, 'isTrimmedSurface'))
    except Exception:
        trimmed = False
    if not trimmed:
        return [base]
    S = om.MFnNurbsSurface
    k_outer, k_inner = getattr(S, 'kOuter', 1), getattr(S, 'kInner', 2)
    faces = []
    nreg = int(_val(fn, 'numRegions'))
    for r in range(nreg):
        loops = []
        for b in range(int(fn.numBoundaries(r))):
            bt = fn.boundaryType(r, b)
            kind = 'outer' if bt == k_outer else ('inner' if bt == k_inner else None)
            edges = []
            for e in range(int(fn.numEdges(r, b))):
                objs = fn.edge(r, b, e, True)                       # True: the edge as a curve in the surface's (u, v) space
                objs = list(objs) if isinstance(objs, (list, tuple)) or hasattr(objs, '__len__') else [objs]
                edges += [_curve_dict(o) for o in objs]
            if edges:
                loops.append(dict(kind=kind, edges=edges))
        if loops:
            f = dict(base)
            f.update(trimmed=True, loops=loops)
            faces.append(f)
    if not faces:
        raise RuntimeError('trimmed surface, but no trim boundary could be read')
    return faces


# ------------------------------------------------------------------------------------------------ the scene
class _Collector:
    def __init__(self, use_cad_names=True):
        self.use_cad_names = use_cad_names
        self.groups, self.faces, self.skipped, self.surfaces = [], [], [], 0
        self.memo = {}

    def name_of(self, node):
        if self.use_cad_names:
            n = _attr(node, 'cadName')
            if n:
                return n, (_attr(node, 'cadProduct') or n)
        n = _clean_label(_short(node))
        return n, n

    def add_group(self, kind, node, parent, name=None):
        nm, product = (name, name) if name else self.name_of(node)
        gid = len(self.groups)
        self.groups.append(dict(id=gid, parent=parent, kind=kind, name=nm, product=product, maya=cad_names.safe_name(nm)))
        return gid

    def has_surfaces(self, node):
        if node not in self.memo:
            self.memo[node] = bool(_surface_shapes(node)) or any(self.has_surfaces(c) for c in _child_transforms(node))
        return self.memo[node]

    def add_surface(self, node, group):
        for shape in _surface_shapes(node):
            self.surfaces += 1
            try:
                faces = read_surface(shape, _file_factor(node))
            except Exception as e:
                self.skipped.append('%s: %s' % (_short(node), str(e)[:160]))
                continue
            color = _color(shape)
            for f in faces:
                f.update(index=len(self.faces) + 1, group=group, color=color, maya_node=_short(node))
                self.faces.append(f)

    def visit(self, node, parent, parent_kind):
        if _short(node) in CAMERAS or not self.has_surfaces(node):
            return
        kids = [c for c in _child_transforms(node) if self.has_surfaces(c)]
        if _surface_shapes(node):
            # a surface: it belongs to the enclosing part/body, or becomes a part of its own (flat files, loose surfaces)
            group = parent if parent_kind in ('part', 'body') else self.add_group('part', node, parent)
            self.add_surface(node, group)
            for c in kids:
                self.visit(c, parent, parent_kind)
            return
        kind = _attr(node, 'cadKind')
        if kind == 'root':                                           # only the group an import wrapped everything in: not part of the CAD tree
            for c in kids:
                self.visit(c, parent, parent_kind)
            return
        if not self.use_cad_names:
            kind = None
        if kind not in ('assembly', 'part', 'body'):
            only_surfaces = all(_surface_shapes(c) and not _child_transforms(c) for c in kids)
            kind = 'part' if only_surfaces else 'assembly'
        if kind == 'part' and parent_kind in ('part', 'body'):
            kind = 'body'                                            # a part inside a part
        if kind == 'body' and parent_kind not in ('part', 'body'):
            kind = 'part'
        gid = self.add_group(kind, node, parent)
        for c in kids:
            self.visit(c, gid, kind)

    def data(self, source):
        parts = []
        part_of = {}
        for g in self.groups:
            if g['kind'] == 'part':
                part_of[g['id']] = g['id']
            elif g['kind'] == 'body':
                p = g['parent']
                while p is not None and self.groups[p]['kind'] != 'part':
                    p = self.groups[p]['parent']
                part_of[g['id']] = p
        for g in self.groups:
            if g['kind'] != 'part':
                continue
            faces = [f for f in self.faces if part_of.get(f['group']) == g['id']]
            if faces:
                parts.append(dict(name=g['name'], product=g['product'], path=[], group=g['id'], color=None, faces=faces))
        nf = sum(len(p['faces']) for p in parts)
        return dict(format=FORMAT, source=source, units='mm', groups=self.groups, parts=parts,
                    stats=dict(parts=len(parts), faces=nf, trimmed=sum(1 for p in parts for f in p['faces'] if f['trimmed']),
                               surfaces=self.surfaces, skipped=len(self.skipped)),
                    skipped=self.skipped)


def roots(selection_only):
    if selection_only:
        out = []
        for s in cmds.ls(selection=True, long=True) or []:
            if not _is_type(s, 'transform'):
                p = cmds.listRelatives(s, parent=True, fullPath=True)
                s = p[0] if p else s
            if s not in out:
                out.append(s)
        # a node inside another selected node is already included through it
        return [s for s in out if not any(s != o and s.startswith(o + '|') for o in out)]
    return [n for n in (cmds.ls(assemblies=True, long=True) or []) if _short(n) not in CAMERAS]


def collect(selection_only=True, use_cad_names=True, source='maya'):
    """The cad2maya JSON for the selection (or the whole scene) - without writing anything."""
    c = _Collector(use_cad_names)
    for r in roots(selection_only):
        c.visit(r, None, None)
    return c.data(source)


# ------------------------------------------------------------------------------------------------ export
def export_cad(path, python=None, selection_only=True, use_cad_names=True, on_wait=None):
    """Write the selection (or the whole scene) as STEP or IGES (by extension). Returns a report dict; prints a report."""
    path = os.path.expanduser(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.step', '.stp', '.iges', '.igs'):
        raise RuntimeError('The file must end in .step, .stp, .iges or .igs: %s' % path)
    WEIGHT_STATS.clear()
    data = collect(selection_only, use_cad_names, source=os.path.basename(path))
    if not data['parts']:
        raise RuntimeError('Nothing to export: %s contains no NURBS surfaces%s.' % ('the selection' if selection_only else 'the scene',
                           ' that could be read' if data['skipped'] else ''))
    here = os.path.dirname(os.path.abspath(__file__))
    tmp = os.path.join(tempfile.mkdtemp(prefix='cad2maya_out_'), 'export.cad2maya.json')
    with open(tmp, 'w') as f:
        json.dump(data, f)
    code, so, se = _run_extractor([_extractor_python(python), os.path.join(here, 'cad_write.py'), tmp, '-o', path], on_wait)
    if code != 0:
        lines = [l.strip() for l in (se or so).strip().splitlines() if l.strip()]
        raise RuntimeError('The CAD file could not be written: %s\n\nDetails from cad_write.py:\n%s' % (lines[-1] if lines else 'no error text', (so[-1500:] + '\n' + se[-1500:]).strip()))
    wrep = {}
    for line in reversed(so.strip().splitlines()):
        try:
            wrep = json.loads(line)
            break
        except ValueError:
            continue
    rep = dict(path=path, parts=len(data['parts']), faces=data['stats']['faces'], trimmed=data['stats']['trimmed'], surfaces=data['stats']['surfaces'],
               skipped=data['skipped'], written=wrep.get('faces'), write_failed=wrep.get('failed', []), invalid=wrep.get('invalid', []),
               assemblies=sum(1 for g in data['groups'] if g['kind'] == 'assembly'), bytes=wrep.get('bytes'))
    print('--- cad2maya export report: %s' % os.path.basename(path))
    print('    %d surface(s) read -> %d face(s) (%d trimmed) in %d part(s), %d assembly level(s)' % (rep['surfaces'], rep['faces'], rep['trimmed'], rep['parts'], rep['assemblies']))
    print('    written: %s face(s), %.2f MB%s' % (rep['written'], (rep['bytes'] or 0) / 1e6, '' if ext in ('.step', '.stp') else '   (IGES: no assemblies, the hierarchy is flattened)'))
    if WEIGHT_STATS:
        print('    weights, read the way that reproduces Maya\'s own evaluation: %s' % ', '.join('%s: %d' % kv for kv in sorted(WEIGHT_STATS.items())))
    if rep['skipped']:
        print('    LEFT OUT: %d surface(s) that could not be read exactly (never written wrong or untrimmed):' % len(rep['skipped']))
        for line in rep['skipped'][:6]:
            print('       ' + line)
    if rep['write_failed']:
        print('    WARNING: %d face(s) could not be written: %s' % (len(rep['write_failed']), '; '.join(rep['write_failed'][:4])))
    if rep['invalid']:
        print('    NOTE: %d written face(s) failed OpenCascade\'s validity check: %s' % (len(rep['invalid']), ', '.join(rep['invalid'][:6])))
    return rep
