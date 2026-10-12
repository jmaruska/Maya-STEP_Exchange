"""
cad2maya_ui.py - a small window for importing STEP / IGES as NURBS surfaces in Maya.

    import cad2maya_ui; cad2maya_ui.show()          # open the window
    cad2maya_ui.install_shelf_button()              # add a "CAD" button to the current shelf

Two layers: `Controller` holds all the logic (settings, finding the OpenCascade Python, validating, running the import, cancel
handling) and has no widgets; `_Window` only creates widgets and forwards clicks to it. Everything uses plain maya.cmds.
"""
import contextlib
import io
import os
import subprocess
import sys
import traceback

import maya.cmds as cmds

import maya_cad_import as mci

WINDOW = 'cad2mayaWindow'
EXPORT_WINDOW = 'cad2mayaExportWindow'
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS) if os.path.basename(SCRIPTS) == 'scripts' else SCRIPTS      # the package folder
ICONS = os.path.join(ROOT, 'icons')
SCOPES = ['Selected objects', 'Everything in the scene']
PREFIX = 'cad2maya_'
VERSION = '1.0.2'

# What one unit in the file means -> centimetres. STEP/IGES declare their units and OpenCascade converts them to millimetres, so
# 'Millimetres' is right for those; the other entries are for files WITHOUT units (for example converted from RenderMan).
UNITS = [('Millimetres', 0.1), ('Centimetres', 1.0), ('Metres', 100.0), ('Inches', 2.54), ('Feet', 30.48), ('Custom', None)]
GROUPING = [('Auto', 'auto'), ('Always', True), ('Never', False)]
CAD_FILTER = 'CAD files (*.step *.stp *.iges *.igs);;STEP (*.step *.stp);;IGES (*.iges *.igs);;All files (*.*)'

DEFAULTS = dict(out_file='', scope='Selected objects', cad_names=True, last_out_dir='',
                file='', unit='Millimetres', scale=0.1, trims=True, verify=True, frame=True, grouping='Auto',
                python='', root_name='', warn_unsaved=True, trim_fallback=True, last_dir='')


# ------------------------------------------------------------------------------------------------ settings (optionVars)
def load_settings():
    s = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        name = PREFIX + key
        try:
            if cmds.optionVar(exists=name):
                value = cmds.optionVar(query=name)
                s[key] = bool(value) if isinstance(default, bool) else (float(value) if isinstance(default, float) else value)
        except Exception:
            pass
    return s


def save_settings(s):
    for key, value in s.items():
        name = PREFIX + key
        try:
            if isinstance(value, bool):
                cmds.optionVar(intValue=(name, int(value)))
            elif isinstance(value, float):
                cmds.optionVar(floatValue=(name, value))
            else:
                cmds.optionVar(stringValue=(name, str(value)))
        except Exception:
            pass


def unit_for_scale(scale):
    for name, factor in UNITS:
        if factor is not None and abs(factor - scale) < 1e-9:
            return name
    return 'Custom'


# ------------------------------------------------------------------------------------------------ logic (no widgets)
class Controller:
    def __init__(self, folder=None):
        self.folder = folder or os.path.dirname(os.path.abspath(__file__))
        self.settings = load_settings()

    # -- the Python that has OpenCascade (the environment made by setup_macos.sh)
    def detect_python(self):
        """Look in the package folder and next to these scripts. The usual folder names (.venv, venv, cad2maya_env) are accepted when they
        hold a bin/python; a folder with ANY other name, hidden or not, is accepted if it is recognisably an environment (pyvenv.cfg)."""
        found = [os.environ.get('CAD2MAYA_PYTHON', '')]
        places = [self.folder]
        if os.path.basename(self.folder) == 'scripts':
            places.insert(0, os.path.dirname(self.folder))
        for place in places:
            try:
                names = os.listdir(place)
            except OSError:
                names = []
            usual = [n for n in ('.venv', 'venv', 'cad2maya_env') if n in names]
            for name in usual + sorted(n for n in names if n not in usual):
                env = os.path.join(place, name)
                if name in usual or os.path.isfile(os.path.join(env, 'pyvenv.cfg')):
                    found += [os.path.join(env, 'bin', 'python'), os.path.join(env, 'Scripts', 'python.exe')]
        for candidate in found:
            if candidate and os.path.isfile(candidate):
                return candidate
        return ''

    def python_path(self):
        saved = (self.settings.get('python') or '').strip()
        return saved if saved and os.path.isfile(os.path.expanduser(saved)) else self.detect_python()

    def test_python(self, py=None):
        """(ok, text): can that Python import OpenCascade, numpy and the extractor?"""
        py = py or self.python_path()
        if not py or not os.path.isfile(os.path.expanduser(py)):
            return False, 'No OpenCascade Python found.\nRun setup_macos.sh (it creates an environment folder next to these scripts) or browse to its bin/python.'
        code = 'import sys; sys.path.insert(0, %r); import OCP, numpy, cad_names, cad_extract; print("OpenCascade OK")' % self.folder
        try:
            r = subprocess.run([os.path.expanduser(py), '-c', code], capture_output=True, text=True, timeout=180)
        except Exception as e:
            return False, 'Could not run %s: %s' % (py, e)
        if r.returncode == 0 and 'OpenCascade OK' in r.stdout:
            return True, 'OK: %s can read STEP/IGES.' % py
        return False, 'That Python cannot import what the extractor needs:\n%s' % (r.stderr or r.stdout)[-1200:]

    # -- checks before an import
    def check_ready(self, path):
        """(ok, message). Message is empty when ok."""
        path = os.path.expanduser((path or '').strip())
        if not path:
            return False, 'Choose a CAD file first.'
        if not os.path.isfile(path):
            return False, 'File not found:\n%s' % path
        if os.path.splitext(path)[1].lower() not in ('.step', '.stp', '.iges', '.igs'):
            return False, 'Not a STEP or IGES file (.step .stp .iges .igs):\n%s' % os.path.basename(path)
        if not self.python_path():
            return False, ('The OpenCascade Python was not found.\nRun setup_macos.sh (it creates an environment folder next to these scripts), or '
                           'open Advanced and browse to its bin/python.')
        if not (self.settings['scale'] > 0):
            return False, 'The scale factor must be greater than zero.'
        return True, ''

    # -- the import
    def check_export(self, path, selection_only):
        """(ok, message, path): the path gets .step if it has no CAD extension."""
        path = os.path.expanduser((path or '').strip())
        if not path:
            return False, 'Choose where to save the file.', path
        if os.path.splitext(path)[1].lower() not in ('.step', '.stp', '.iges', '.igs'):
            path += '.step'
        if not os.path.isdir(os.path.dirname(path) or '.'):
            return False, 'The folder does not exist:\n%s' % os.path.dirname(path), path
        if not self.python_path():
            return False, ('The OpenCascade Python was not found.\nRun setup_macos.sh (it creates an environment folder next to the scripts), or '
                           'open Advanced in the import window and browse to its bin/python.'), path
        if selection_only and not (cmds.ls(selection=True) or []):
            return False, 'Nothing is selected. Select surfaces or groups, or choose "Everything in the scene".', path
        return True, '', path

    def run_export(self, path, selection_only=True, use_cad_names=True, on_wait=None):
        """Run the export. Returns dict(ok, cancelled, report, error). Never raises."""
        import maya_cad_export as mce
        buf = io.StringIO()
        result = dict(ok=False, cancelled=False, report='', error=None)
        try:
            with contextlib.redirect_stdout(buf):
                rep = mce.export_cad(path, python=self.python_path(), selection_only=selection_only, use_cad_names=use_cad_names, on_wait=on_wait)
            result.update(ok=True, summary=rep)
        except mci.ImportCancelled:
            result['cancelled'] = True
            buf.write('CANCELLED while writing the CAD file; nothing was written.\n')
        except Exception as e:
            result['error'] = str(e)
            buf.write('EXPORT FAILED:\n%s\n' % e)
            if not isinstance(e, RuntimeError):
                buf.write(traceback.format_exc()[-1500:])
        result['report'] = buf.getvalue()
        return result

    def run_import(self, path, progress=None, on_wait=None):
        """Run the import with the current settings. Returns dict(ok, cancelled, report, root, error). Never raises."""
        s = self.settings
        path = os.path.expanduser(path.strip())
        buf = io.StringIO()
        result = dict(ok=False, cancelled=False, report='', root=None, error=None)
        kw = dict(scale=float(s['scale']), trims=bool(s['trims']), verify=bool(s['verify']), trim_fallback=bool(s['trim_fallback']),
                  group_parts=dict(GROUPING)[s['grouping']] if s['grouping'] in dict(GROUPING) else 'auto')
        if (s.get('root_name') or '').strip():
            kw['root_name'] = s['root_name'].strip()
        try:
            with contextlib.redirect_stdout(buf):
                rep = mci.import_cad(path, python=self.python_path(), on_wait=on_wait, progress=progress, **kw)
            result.update(ok=True, root=rep.get('root'), cancelled=bool(rep.get('cancelled')), summary=rep)
        except mci.ImportCancelled:
            result.update(cancelled=True)
            buf.write('CANCELLED while reading the CAD file; nothing was imported.\n')
        except Exception as e:
            result['error'] = str(e)
            buf.write('IMPORT FAILED:\n%s\n' % e)
            if not isinstance(e, RuntimeError):                      # RuntimeErrors carry their own explanation; anything else may be a bug
                buf.write(traceback.format_exc()[-1500:])
        result['report'] = buf.getvalue()
        return result


# ------------------------------------------------------------------------------------------------ window
class _Window:
    def __init__(self, controller):
        self.c = controller
        self.w = {}

    # -- helpers to read / write widgets
    def _read(self):
        s, w = self.c.settings, self.w
        s['file'] = cmds.textFieldButtonGrp(w['file'], query=True, text=True)
        s['unit'] = cmds.optionMenuGrp(w['unit'], query=True, value=True)
        s['scale'] = float(cmds.floatFieldGrp(w['scale'], query=True, value1=True))
        s['trims'] = bool(cmds.checkBox(w['trims'], query=True, value=True))
        s['verify'] = bool(cmds.checkBox(w['verify'], query=True, value=True))
        s['frame'] = bool(cmds.checkBox(w['frame'], query=True, value=True))
        s['grouping'] = cmds.optionMenuGrp(w['grouping'], query=True, value=True)
        s['python'] = cmds.textFieldButtonGrp(w['python'], query=True, text=True)
        s['root_name'] = cmds.textFieldGrp(w['root'], query=True, text=True)
        s['warn_unsaved'] = bool(cmds.checkBox(w['warn'], query=True, value=True))
        s['trim_fallback'] = bool(cmds.checkBox(w['fallback'], query=True, value=True))
        save_settings(s)

    def _say(self, text):
        cmds.scrollField(self.w['report'], edit=True, text=text)

    # -- callbacks (all accept *args: Maya passes a varying number of them)
    def browse_file(self, *args):
        start = self.c.settings.get('last_dir') or os.path.expanduser('~')
        picked = cmds.fileDialog2(fileMode=1, caption='Choose a STEP or IGES file', fileFilter=CAD_FILTER, startingDirectory=start, dialogStyle=1)
        if picked:
            cmds.textFieldButtonGrp(self.w['file'], edit=True, text=picked[0])
            self.c.settings['last_dir'] = os.path.dirname(picked[0])
            self._read()

    def browse_python(self, *args):
        picked = cmds.fileDialog2(fileMode=1, caption='Choose the python that has OpenCascade (<environment folder>/bin/python)', fileFilter='All files (*.*)',
                                  startingDirectory=self.c.folder, dialogStyle=1)
        if picked:
            cmds.textFieldButtonGrp(self.w['python'], edit=True, text=picked[0])
            self._read()

    def test_python(self, *args):
        self._read()
        ok, text = self.c.test_python(self.c.settings.get('python') or None)
        self._say(('OpenCascade check\n%s\n' % text))

    def unit_changed(self, *args):
        name = cmds.optionMenuGrp(self.w['unit'], query=True, value=True)
        factor = dict(UNITS).get(name)
        if factor is not None:
            cmds.floatFieldGrp(self.w['scale'], edit=True, value1=factor)
        self._read()

    def scale_edited(self, *args):
        value = float(cmds.floatFieldGrp(self.w['scale'], query=True, value1=True))
        cmds.optionMenuGrp(self.w['unit'], edit=True, value=unit_for_scale(value))
        self._read()

    def settings_changed(self, *args):
        self._read()

    def copy_report(self, *args):
        text = cmds.scrollField(self.w['report'], query=True, text=True)
        for mod in ('PySide6', 'PySide2'):
            try:
                QtWidgets = __import__(mod + '.QtWidgets', fromlist=['QApplication']).QApplication
                QtWidgets.clipboard().setText(text)
                cmds.inViewMessage(amg='Report copied to the clipboard', pos='midCenter', fade=True)
                return
            except Exception:
                continue
        cmds.warning('Could not reach the clipboard: click in the report, select all, and copy.')

    def close(self, *args):
        if cmds.window(WINDOW, exists=True):
            cmds.deleteUI(WINDOW)

    def do_import(self, *args):
        try:
            self._do_import()
        except Exception:
            self._say('Unexpected error in the CAD to Maya window:\n%s' % traceback.format_exc()[-1800:])
            try:
                cmds.progressWindow(endProgress=True)
            except Exception:
                pass
            try:
                if cmds.window(WINDOW, exists=True):
                    cmds.showWindow(WINDOW)
            except Exception:
                pass

    def _do_import(self):
        self._read()
        s = self.c.settings
        ok, msg = self.c.check_ready(s['file'])
        if not ok:
            self._say(msg)
            cmds.confirmDialog(title='CAD to Maya', message=msg, button=['OK'])
            if 'OpenCascade Python' in msg:
                cmds.frameLayout(self.w['advanced'], edit=True, collapse=False)
            return
        if s['warn_unsaved'] and cmds.file(query=True, modified=True):
            answer = cmds.confirmDialog(title='Unsaved changes', button=['Save scene first', 'Import anyway', 'Cancel'], defaultButton='Save scene first',
                                        cancelButton='Cancel', dismissString='Cancel',
                                        message='The scene has unsaved changes. Importing a large CAD file is heavy work, so saving first is safer.')
            if answer == 'Cancel':
                return
            if answer == 'Save scene first':
                from maya import mel
                mel.eval('SaveScene')

        state = dict(last=0.0)
        import time

        def on_wait(elapsed):
            cmds.progressWindow(edit=True, status='Reading the CAD file with OpenCascade...  %d s (large files can take a minute)' % int(elapsed))
            return cmds.progressWindow(query=True, isCancelled=True)

        def progress(done, total, name):
            now = time.time()
            if done == total or now - state['last'] > 0.1:
                state['last'] = now
                cmds.progressWindow(edit=True, minValue=0, maxValue=max(total, 1), progress=done, status='Building surface %d of %d:  %s' % (done, total, name))
            return cmds.progressWindow(query=True, isCancelled=True)

        self._say('Importing %s ...\n' % os.path.basename(s['file']))
        # The progress window is a separate window and can open BEHIND this one (seen on macOS), so this window steps aside while
        # the import runs and always comes back afterwards, whether it finished, was cancelled, or failed.
        try:
            cmds.window(WINDOW, edit=True, visible=False)
        except Exception:
            pass
        try:
            cmds.progressWindow(title='CAD to Maya', progress=0, status='Starting...', isInterruptable=True, minValue=0, maxValue=100)
            result = self.c.run_import(s['file'], progress=progress, on_wait=on_wait)
        finally:
            try:
                cmds.progressWindow(endProgress=True)
            finally:
                if cmds.window(WINDOW, exists=True):
                    cmds.showWindow(WINDOW)
        self._say(result['report'])
        print(result['report'])
        if result['ok'] and result.get('root') and s['frame']:
            try:
                cmds.select(result['root'], replace=True)
                cmds.viewFit(fitFactor=0.95)
            except Exception:
                pass

    # -- layout
    def build(self):
        s = self.c.settings
        if cmds.window(WINDOW, exists=True):
            cmds.deleteUI(WINDOW)
        if cmds.windowPref(WINDOW, exists=True):
            cmds.windowPref(WINDOW, remove=True)
        cmds.window(WINDOW, title='CAD to Maya  v%s' % VERSION, widthHeight=(580, 640), sizeable=True)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=6, columnAttach=('both', 10))
        cmds.separator(height=4, style='none')
        cmds.text(label='Import STEP / IGES as NURBS surfaces (OpenCascade)', align='left', font='boldLabelFont')
        self.w['file'] = cmds.textFieldButtonGrp(label='CAD file', text=s['file'], buttonLabel='Browse...', buttonCommand=self.browse_file,
                                                 changeCommand=self.settings_changed, columnWidth3=(70, 360, 80), adjustableColumn=2)
        cmds.separator(height=6, style='in')
        self.w['unit'] = cmds.optionMenuGrp(label='File units', changeCommand=self.unit_changed, columnWidth2=(70, 200))
        for name, _ in UNITS:
            cmds.menuItem(label=name)
        cmds.optionMenuGrp(self.w['unit'], edit=True, value=unit_for_scale(s['scale']))
        self.w['scale'] = cmds.floatFieldGrp(label='Scale factor', numberOfFields=1, value1=s['scale'], precision=4, columnWidth2=(70, 100),
                                             changeCommand=self.scale_edited)
        cmds.text(label='STEP/IGES files declare their units and are converted to millimetres, so leave this on Millimetres (0.1 = mm to cm). '
                        'Change it only for files WITHOUT units, such as ones converted from RenderMan.',
                  align='left', wordWrap=True, font='smallObliqueLabelFont')
        cmds.separator(height=6, style='in')
        cmds.rowColumnLayout(numberOfColumns=2, columnWidth=[(1, 275), (2, 275)])
        self.w['trims'] = cmds.checkBox(label='Trim faces (recommended)', value=s['trims'], changeCommand=self.settings_changed)
        self.w['verify'] = cmds.checkBox(label='Verify against OpenCascade', value=s['verify'], changeCommand=self.settings_changed)
        self.w['frame'] = cmds.checkBox(label='Select and frame the result', value=s['frame'], changeCommand=self.settings_changed)
        cmds.text(label='')
        cmds.setParent('..')
        self.w['grouping'] = cmds.optionMenuGrp(label='Group parts', changeCommand=self.settings_changed, columnWidth2=(70, 200))
        for name, _ in GROUPING:
            cmds.menuItem(label=name)
        cmds.optionMenuGrp(self.w['grouping'], edit=True, value=s['grouping'] if s['grouping'] in dict(GROUPING) else 'Auto')
        self.w['advanced'] = cmds.frameLayout(label='Advanced', collapsable=True, collapse=bool(self.c.python_path()),                     # open only when the OpenCascade Python was not found
                                              
                                              marginWidth=6, marginHeight=4)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
        self.w['python'] = cmds.textFieldButtonGrp(label='OpenCascade Python', text=s['python'] or self.c.detect_python(), buttonLabel='Browse...',
                                                   buttonCommand=self.browse_python, changeCommand=self.settings_changed, columnWidth3=(120, 280, 80),
                                                   adjustableColumn=2)
        cmds.button(label='Test OpenCascade', height=24, command=self.test_python)
        self.w['root'] = cmds.textFieldGrp(label='Root group name', text=s['root_name'], changeCommand=self.settings_changed, columnWidth2=(120, 300))
        self.w['warn'] = cmds.checkBox(label='Warn if the scene has unsaved changes', value=s['warn_unsaved'], changeCommand=self.settings_changed)
        self.w['fallback'] = cmds.checkBox(label='Keep the full surface when a trim cannot be built (named ..._UNTRIMMED_FALLBACK)', value=s['trim_fallback'],
                                           changeCommand=self.settings_changed)
        cmds.setParent('..')
        cmds.setParent('..')
        cmds.rowLayout(numberOfColumns=3, adjustableColumn=1, columnAttach=[(1, 'both', 0), (2, 'both', 4), (3, 'both', 0)])
        cmds.button(label='Import', height=34, command=self.do_import, backgroundColor=(0.35, 0.55, 0.35))
        cmds.button(label='Copy report', height=34, width=100, command=self.copy_report)
        cmds.button(label='Close', height=34, width=80, command=self.close)
        cmds.setParent('..')
        cmds.text(label='Report', align='left', font='boldLabelFont')
        self.w['report'] = cmds.scrollField(editable=False, wordWrap=False, height=250, text='')
        cmds.showWindow(WINDOW)


_INSTANCE = None


def show():
    """Open the CAD to Maya window."""
    global _INSTANCE
    _INSTANCE = _Window(Controller())
    _INSTANCE.build()
    return _INSTANCE


# ------------------------------------------------------------------------------------------------ installation
class _ExportWindow:
    def __init__(self, controller):
        self.c = controller
        self.w = {}

    def _read(self):
        s, w = self.c.settings, self.w
        s['scope'] = cmds.optionMenuGrp(w['scope'], query=True, value=True)
        s['out_file'] = cmds.textFieldButtonGrp(w['file'], query=True, text=True)
        s['cad_names'] = bool(cmds.checkBox(w['names'], query=True, value=True))
        save_settings(s)

    def _say(self, text):
        cmds.scrollField(self.w['report'], edit=True, text=text)

    def settings_changed(self, *args):
        self._read()

    def browse(self, *args):
        start = self.c.settings.get('last_out_dir') or os.path.expanduser('~')
        picked = cmds.fileDialog2(fileMode=0, caption='Save as STEP or IGES', fileFilter='STEP (*.step *.stp);;IGES (*.iges *.igs)',
                                  startingDirectory=start, dialogStyle=1)
        if picked:
            cmds.textFieldButtonGrp(self.w['file'], edit=True, text=picked[0])
            self.c.settings['last_out_dir'] = os.path.dirname(picked[0])
            self._read()

    def copy_report(self, *args):
        _Window.copy_report(self)

    def close(self, *args):
        if cmds.window(EXPORT_WINDOW, exists=True):
            cmds.deleteUI(EXPORT_WINDOW)

    def do_export(self, *args):
        try:
            self._do_export()
        except Exception:
            self._say('Unexpected error in the export window:\n%s' % traceback.format_exc()[-1800:])
            try:
                cmds.progressWindow(endProgress=True)
            except Exception:
                pass

    def _do_export(self):
        self._read()
        s = self.c.settings
        selection_only = s['scope'] == SCOPES[0]
        ok, msg, path = self.c.check_export(s['out_file'], selection_only)
        if not ok:
            self._say(msg)
            cmds.confirmDialog(title='CAD from Maya', message=msg, button=['OK'])
            return
        if path != s['out_file']:
            cmds.textFieldButtonGrp(self.w['file'], edit=True, text=path)
            self._read()
        if os.path.exists(path):
            answer = cmds.confirmDialog(title='Replace file?', message='%s already exists. Replace it?' % os.path.basename(path),
                                        button=['Replace', 'Cancel'], defaultButton='Replace', cancelButton='Cancel', dismissString='Cancel')
            if answer != 'Replace':
                return

        def on_wait(elapsed):
            cmds.progressWindow(edit=True, status='Writing the CAD file with OpenCascade...  %d s' % int(elapsed))
            return cmds.progressWindow(query=True, isCancelled=True)

        self._say('Exporting to %s ...\n' % os.path.basename(path))
        try:
            cmds.window(EXPORT_WINDOW, edit=True, visible=False)
        except Exception:
            pass
        try:
            cmds.progressWindow(title='CAD from Maya', progress=0, status='Reading surfaces...', isInterruptable=True, minValue=0, maxValue=100)
            result = self.c.run_export(path, selection_only=selection_only, use_cad_names=s['cad_names'], on_wait=on_wait)
        finally:
            try:
                cmds.progressWindow(endProgress=True)
            finally:
                if cmds.window(EXPORT_WINDOW, exists=True):
                    cmds.showWindow(EXPORT_WINDOW)
        self._say(result['report'])
        print(result['report'])

    def build(self):
        s = self.c.settings
        if cmds.window(EXPORT_WINDOW, exists=True):
            cmds.deleteUI(EXPORT_WINDOW)
        if cmds.windowPref(EXPORT_WINDOW, exists=True):
            cmds.windowPref(EXPORT_WINDOW, remove=True)
        cmds.window(EXPORT_WINDOW, title='CAD from Maya  v%s' % VERSION, widthHeight=(580, 470), sizeable=True)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=6, columnAttach=('both', 10))
        cmds.separator(height=4, style='none')
        cmds.text(label='Export NURBS surfaces as STEP / IGES (OpenCascade)', align='left', font='boldLabelFont')
        self.w['scope'] = cmds.optionMenuGrp(label='Export', changeCommand=self.settings_changed, columnWidth2=(70, 220))
        for name in SCOPES:
            cmds.menuItem(label=name)
        cmds.optionMenuGrp(self.w['scope'], edit=True, value=s['scope'] if s['scope'] in SCOPES else SCOPES[0])
        self.w['file'] = cmds.textFieldButtonGrp(label='Save as', text=s['out_file'], buttonLabel='Browse...', buttonCommand=self.browse,
                                                 changeCommand=self.settings_changed, columnWidth3=(70, 360, 80), adjustableColumn=2)
        self.w['names'] = cmds.checkBox(label='Use the original CAD names (restores names such as CoreWood:1)', value=s['cad_names'],
                                        changeCommand=self.settings_changed)
        cmds.text(label='Written in millimetres. Surfaces imported with CAD to Maya get their original numbers back; anything else is '
                        'converted from centimetres. Use STEP to keep the hierarchy (IGES flattens it). A surface whose trims cannot be '
                        'read is left out and reported, never exported untrimmed.', align='left', wordWrap=True, font='smallObliqueLabelFont')
        cmds.rowLayout(numberOfColumns=3, adjustableColumn=1, columnAttach=[(1, 'both', 0), (2, 'both', 4), (3, 'both', 0)])
        cmds.button(label='Export', height=34, command=self.do_export, backgroundColor=(0.35, 0.45, 0.6))
        cmds.button(label='Copy report', height=34, width=100, command=self.copy_report)
        cmds.button(label='Close', height=34, width=80, command=self.close)
        cmds.setParent('..')
        cmds.text(label='Report', align='left', font='boldLabelFont')
        self.w['report'] = cmds.scrollField(editable=False, wordWrap=False, height=200, text='')
        cmds.showWindow(EXPORT_WINDOW)


_EXPORT_INSTANCE = None


def show_export():
    """Open the export window."""
    global _EXPORT_INSTANCE
    _EXPORT_INSTANCE = _ExportWindow(Controller())
    _EXPORT_INSTANCE.build()
    return _EXPORT_INSTANCE


SHELF_BUTTONS = [
    ('CAD to Maya: import STEP / IGES as NURBS', 'CAD in', 'cad2maya_import.svg', 'cad2maya_ui.show()'),
    ('CAD to Maya: export NURBS as STEP / IGES', 'CAD out', 'cad2maya_export.svg', 'cad2maya_ui.show_export()'),
]


def install_shelf_buttons():
    """Add the CAD in / CAD out buttons to the current shelf (replacing older ones). They remember where the scripts are, so no
    userSetup.py is needed. Icons come from the icons/ folder if present (cad2maya_import.svg, cad2maya_export.svg)."""
    from maya import mel
    # each click loads the modules FRESH from this folder (same steps as install_cad2maya.load): first on the path, Python's cached
    # view of the folder cleared, modules already in memory dropped rather than reloaded - so an older copy can never answer
    common = ("import sys, importlib\n_p = r'%s'\nif _p in sys.path:\n    sys.path.remove(_p)\nsys.path.insert(0, _p)\n"
              "sys.path_importer_cache.pop(_p, None)\nimportlib.invalidate_caches()\n"
              "for _n in ('cad2maya_ui', 'maya_cad_export', 'maya_cad_import', 'cad_names'):\n    sys.modules.pop(_n, None)\n"
              "import cad2maya_ui\n") % SCRIPTS
    top = mel.eval('$tmp = $gShelfTopLevel')
    shelf = cmds.tabLayout(top, query=True, selectTab=True)
    for child in cmds.shelfLayout(shelf, query=True, childArray=True) or []:
        try:
            if cmds.shelfButton(child, query=True, annotation=True).startswith('CAD to Maya'):
                cmds.deleteUI(child)
        except Exception:
            pass
    for annotation, label, icon, call in SHELF_BUTTONS:
        path = os.path.join(ICONS, icon)
        kw = dict(parent=shelf, label=label, annotation=annotation, sourceType='python', command=common + call + '\n')
        if os.path.isfile(path):
            kw['image1'] = path
        else:
            kw.update(image1='commandButton.png', imageOverlayLabel=label)
        cmds.shelfButton(**kw)
    return shelf


install_shelf_button = install_shelf_buttons            # the old name keeps working
