"""
Drag this file into a Maya viewport. It adds the "CAD in" and "CAD out" buttons to the current shelf and opens the import window.

Layout (keep it together):
    install_cad2maya.py   this file
    setup_macos.sh        creates the Python environment with OpenCascade (run it once, before this)
    scripts/              the cad2maya modules
    icons/                cad2maya_import.svg, cad2maya_export.svg (optional: the buttons get text labels without them)

No drag-and-drop? In the Script Editor (Python tab):
    exec(open('/path/to/cad2maya/install_cad2maya.py').read()); onMayaDroppedPythonFile()
"""
import importlib
import os
import sys

MODULES = ('cad2maya_ui', 'maya_cad_export', 'maya_cad_import', 'cad_names')


def load(scripts):
    """Import the cad2maya modules FRESH from `scripts`, whatever this Maya session loaded before: the folder goes first on the path,
    Python's cached view of it is cleared (a folder looked up before its files existed stays 'empty' otherwise), and modules
    already in memory (an older copy, or one from a folder that has moved) are dropped instead of reloaded."""
    if scripts in sys.path:
        sys.path.remove(scripts)
    sys.path.insert(0, scripts)
    sys.path_importer_cache.pop(scripts, None)
    importlib.invalidate_caches()
    for name in MODULES:
        sys.modules.pop(name, None)
    for name in reversed(MODULES):                       # all four, explicitly: the UI imports the exporter only when you export
        importlib.import_module(name)
    import cad2maya_ui
    wrong = [n for n in MODULES if not os.path.abspath(getattr(sys.modules.get(n), '__file__', '') or '').startswith(scripts + os.sep)]
    if wrong:
        raise RuntimeError('cad2maya loaded %s from the wrong place: %s\nExpected everything from %s'
                           % (', '.join(wrong), ', '.join(str(getattr(sys.modules.get(n), '__file__', None)) for n in wrong), scripts))
    return cad2maya_ui


def onMayaDroppedPythonFile(*args):
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        import inspect
        here = os.path.dirname(os.path.abspath(inspect.getfile(onMayaDroppedPythonFile)))
    scripts = os.path.join(here, 'scripts')
    if not os.path.isfile(os.path.join(scripts, 'cad2maya_ui.py')):
        raise RuntimeError('scripts/cad2maya_ui.py is missing next to install_cad2maya.py (looked in %s). Keep the cad2maya folder together.' % scripts)
    ui = load(scripts)
    ui.install_shelf_buttons()
    ui.show()
    print('CAD to Maya: "CAD in" and "CAD out" buttons added to the shelf (scripts in %s).' % scripts)
