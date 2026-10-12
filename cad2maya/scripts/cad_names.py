"""
cad_names.py - Maya-safe node names. Pure Python (no Maya, no OpenCascade), shared by cad_extract.py and maya_cad_import.py.

The rules are those of FBX Rx, made ASCII-strict because Maya has the strictest node naming of the programs in use:
  * every character outside [A-Za-z0-9_] becomes an underscore ("CoreWood:1" -> "CoreWood_1", "a b-c.d" -> "a_b_c_d");
    accented letters are transliterated first ("Ecole" not "_cole"); anything that cannot be (CJK, emoji) becomes "_"
  * runs of underscores collapse to one; leading and trailing underscores are removed
  * a name that is EMPTY after that gets a generated fallback (Maya cannot have an empty name)
  * a name that starts with a digit gets the prefix "obj_" (Maya cannot have a name that is just a number: "123" -> "obj_123")
  * a name Maya (or this importer) already uses gets "_obj" appended ("transform" -> "transform_obj")
  * collisions are numbered _001, _002, ...  (NameRegistry)
"""
import re
import unicodedata

# Maya node types / default scene nodes that clash with ordinary words (the first block is FBX Rx's list)
RESERVED = {
    'transform', 'shape', 'camera', 'light', 'mesh', 'curve', 'surface', 'locator', 'group', 'null', 'joint', 'constraint',
    'deformer', 'shader', 'material', 'texture', 'file', 'place2dtexture', 'place3dtexture',
    # default cameras and nodes that exist in every scene
    'persp', 'top', 'front', 'side', 'perspshape', 'topshape', 'frontshape', 'sideshape', 'time1', 'lambert1',
    'initialshadinggroup', 'initialparticlese', 'defaultlightset', 'defaultobjectset', 'layermanager', 'renderglobals',
}

MAX_LEN = 180            # Maya allows more, but suffixes such as _grp, _f1234 and _001 are added later


def repair_mojibake(s):
    """STEP/IGES readers sometimes hand back UTF-8 bytes decoded as Latin-1 ('\u00c3\u009cn' for '\u00dcn'). If the text round-trips
    cleanly as UTF-8, recover the real characters; otherwise leave it alone."""
    if not any(ord(c) > 127 for c in s):
        return s
    try:
        fixed = s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed if fixed != s else s


def sanitize_name(name):
    """Return a Maya-safe version of `name`, or None if nothing usable is left (the caller then supplies a fallback)."""
    if name is None:
        return None
    s = repair_mojibake(str(name))
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))             # e-acute -> e
    s = re.sub(r'[^A-Za-z0-9_]', '_', s)                                  # also turns ':' '|' ' ' '-' '.' ... into '_'
    s = re.sub(r'_+', '_', s).strip('_')
    if not s:
        return None
    if s[0].isdigit():
        s = 'obj_' + s
    if s.lower() in RESERVED:
        s = s + '_obj'
    return s[:MAX_LEN]


def safe_name(name, fallback='unnamed'):
    """sanitize_name, but never empty."""
    return sanitize_name(name) or fallback


class NameRegistry:
    """Hands out names that are valid AND unique within one import: the first use keeps the name, later ones get _001, _002..."""

    def __init__(self):
        self.used = set()

    def make(self, name, fallback='unnamed'):
        base = sanitize_name(name) or sanitize_name(fallback) or 'unnamed'
        cand, n = base, 0
        while cand in self.used:
            n += 1
            cand = '%s_%03d' % (base, n)
        self.used.add(cand)
        return cand


def pick_name(occurrence, product):
    """Which name to show for a component: its OCCURRENCE name ('CoreWood:1', what the CAD browser shows) if it contains a letter,
    else the product name. Exporters give unnamed occurrences generated IDs like '0:1:1:2' or '9', which must not beat a real name."""
    has_letter = lambda t: bool(t) and any(c.isalpha() for c in t)
    if has_letter(occurrence):
        return occurrence
    if has_letter(product):
        return product
    return occurrence or product or None
