cad2maya 1.3.3 - STEP / IGES <-> Maya NURBS

LAYOUT  (keep the folder together)
  install_cad2maya.py   drag into a Maya viewport: adds the CAD in / CAD out shelf buttons
  setup_macos.sh        run once in Terminal: creates the Python environment with OpenCascade (cad2maya_env) next to it
  icons/                optional: cad2maya_import.svg and cad2maya_export.svg become the button icons (text labels without them)
  scripts/              the modules (below); you normally never touch them
  testdata/             small test files
  cad2maya_env/         made by setup_macos.sh

QUICK START
  1. In Terminal:  cd /path/to/cad2maya  then  ./setup_macos.sh   (about a minute; needs Python 3.11-3.14, e.g. brew install python@3.12)
  2. Drag install_cad2maya.py into a Maya viewport. It adds two buttons to the current shelf: CAD in and CAD out.
     (No drag-and-drop? In the Script Editor, Python tab:
         exec(open('/path/to/cad2maya/install_cad2maya.py').read()); onMayaDroppedPythonFile() )
  3. CAD in: browse to a .step / .stp / .iges / .igs file and click Import.  CAD out: see EXPORTING below.
  If you move the folder, drag install_cad2maya.py in again (the buttons remember where the scripts are).

EXPORTING  (CAD out)
  Export        Selected objects (with everything under them), or Everything in the scene.
  Save as       a .step/.stp or .iges/.igs file (a name without extension gets .step). Use STEP to keep the hierarchy: IGES has
                no assemblies, so it flattens.
  Use the original CAD names   on: groups made by the importer get their original names back (CoreWood:1), from the cadName
                attribute. Off: Maya's names.
  What is written: every NURBS surface, in WORLD space, as it is in Maya - nothing is rebuilt or approximated. Trimmed surfaces are
  written with their trims (one face per trimmed region). A surface whose trims cannot be read is LEFT OUT and listed in the report;
  it is never exported untrimmed (that would add geometry that is not in your model).
  Hierarchy: groups made by the importer keep their roles (assembly / part / body). Other groups are read by shape: a group of
  surfaces is a part, a group of groups an assembly, a part inside a part a body. The group an import wrapped everything in (named
  after the file) is left out unless it stands for the file's own top assembly. A sub-assembly exported on its own is named by its
  PRODUCT name (Body), because an occurrence name (Body:1) needs a parent.
  Sizes: written in millimetres. Surfaces imported with CAD in get their original numbers back (the import scale is stored on the
  imported group, cadImportScale); anything else is converted from Maya's centimetres.
  Checked against Maya: every surface and trim curve read out of Maya is evaluated and compared with Maya's own evaluation of it
  (getPointAtParam). Weights are taken from whichever source reproduces Maya: the points as cvPositions gives them, the per-point
  query (cvPosition), or the shape's weights attribute (some Maya versions give weight 0 through cvPositions for rational geometry).
  A trim curve whose weights no source gives is taken from Maya's own evaluation of it: a polyline, 24 points per span, every point
  exactly on Maya's curve. The report says which source each needed and how many trims were taken that way. A surface that no
  source reproduces is LEFT OUT and the report prints the raw weights Maya gave; it is never written wrong.
  Colours: from each surface's shader (color, or baseColor). Maya's default grey and the importer's grey for uncoloured faces are
  not written: they were never part of the model.
  The faces are written loose, grouped as their parts. Stitch them into bodies in Fusion with its own Stitch tool.

MOVING THE TOOLS
  Copy the scripts, NOT the environment folder. A Python environment records absolute paths and holds compiled code for one Mac's
  Python, so a copy on another machine usually will not work. On the new Mac run  ./setup_macos.sh  (about a minute) and it
  builds a fresh one. (Moving the whole folder on the SAME Mac is fine for the importer, although pip inside it will stop working.)
  setup_macos.sh with no argument reuses an environment already next to it, so it never builds a second copy; pass a name to
  choose a different folder:  ./setup_macos.sh my_env

COLOURS
  Colours are read from the face, else its shell, else its solid (Autodesk's STEP exporter styles solids and shells, never faces),
  and become one Lambert shader per distinct colour. cad_write.py writes them back, including faces inside body groups.

HOW TRIMS ARE REBUILT (files from Autodesk's exporter, and others)
  A STEP file holds no 2D trim curves: they are derived by projecting each 3D edge onto its surface, and on closed or periodic surfaces
  OpenCascade's projection can be wrong. When a face fails, or when its loops look wrong (they cross themselves, or disagree with the
  points OpenCascade classified), the extractor rebuilds the 2D curves itself from the 3D edges:
    - it uses EVERY edge of a wire when OpenCascade's wire walker stops early (seen: 136 of 385 edges);
    - edges are chained by their own direction in the wire, never retracing the edge just walked (seam copies);
    - a pole (collapsed edge) absorbs a one-period wrap; a boundary that reaches past the seam is folded back across it
      (OpenCascade 2D booleans), which gives polyline loops for those faces;
    - if every edge of a face lies on its surface's own boundary curves (in 3D), the face is the whole surface (this needs no
      projection, so it also works for surfaces that collapse to a point at their ends);
    - on a flat, non-periodic surface an edge walked once each way is a zero-width slit: it is dropped and the rest split into loops;
    - loops that still cross themselves are kept as extracted and listed in the report as suspect.
  A face that cannot be rebuilt at all is kept as its full surface, named <name>_UNTRIMMED_FALLBACK. If only the OUTER boundary fails
  but the holes work, it is named <name>_PARTIAL_TRIM. Faces enclosing no area are skipped and counted. Extraction of a file with
  hundreds of such faces can take a couple of minutes.

THE WINDOW
  CAD file          The STEP or IGES file. Remembered between sessions, together with every setting below.
  File units        What one unit in the file means. STEP/IGES declare their units and are converted to millimetres, so
                    leave it on Millimetres (scale 0.1: mm -> Maya's cm). Change it only for files WITHOUT units, such as
                    ones converted from RenderMan. The menu and the Scale factor field stay linked; a value that matches no
                    unit shows as Custom.
  Trim faces        Build trimmed faces (holes, cut-outs) with Maya's trim node. Verified against OpenCascade.
  Verify            Compare every surface and trim with OpenCascade as it is built (recommended).
  Select and frame  Select the new root group and frame it in the active view.
  Group parts       Auto / Always / Never: one group per part. Auto keeps files made of single-face parts flat.
  Advanced          Opens by itself if the OpenCascade Python is not found. 'Test OpenCascade' checks that it can read CAD.
                    Also: root group name, the unsaved-scene warning, and keeping full surfaces when a trim cannot be built.
  Import            A progress window shows the file being read, then each surface. Cancel works at both stages.
  Report            Everything the importer found: counts, verification, warnings. 'Copy report' puts it on the clipboard.
  If the scene has unsaved changes you are asked whether to save first. Importing a large file is heavy work.

SCRIPTS  (in scripts/)
  cad2maya_ui.py       the two windows (maya.cmds only), settings, shelf buttons.
  cad_names.py         Maya-safe node names (the FBX Rx rules).
  cad_extract.py       runs in the OpenCascade Python: STEP/IGES -> JSON in Maya's NURBS conventions (surfaces, trims, names, colours, tree).
  cad_write.py         runs in the OpenCascade Python: JSON -> STEP/IGES (assemblies, names, colours, trimmed faces).
  maya_cad_import.py   runs inside Maya: builds and VERIFIES surfaces and trims, groups, names, shaders.
  maya_cad_export.py   runs inside Maya: reads surfaces, trims, groups, names and colours back out and calls cad_write.py.
  maya_cad_probe.py    read-only diagnostic: reports how your Maya exposes surfaces and trim boundaries.
  OpenCascade never runs inside Maya.

SCRIPT USE  (Maya > Script Editor > Python tab)
    import sys; sys.path.append('/path/to/cad2maya/scripts')
    import maya_cad_import as mci, maya_cad_export as mce
    mci.import_cad('/path/file.step', python='/path/to/cad2maya/cad2maya_env/bin/python')
    mce.export_cad('/path/out.step', python='/path/to/cad2maya/cad2maya_env/bin/python', selection_only=True)

SCALE  (read this first if the report prints a WARNING about the model size)
  STEP/IGES files normally carry their units and the importer converts them to Maya's centimetres. Files with NO units
  (for example the ones converted from RenderMan RIB files) are assumed to be millimetres, which can be 1000x too small.
  If the report says the model is only a few cm across, import again with scale=100 (metres -> cm). Maya handles tiny
  geometry badly.

DIAGNOSING A FACE THAT FAILS
    mci.diagnose('/path/to/file.step', 'cylinder__72', python='/path/to/cad2maya/cad2maya_env/bin/python')
  Builds the faces whose name contains that text, step by step, and prints what Maya does at every stage, including the
  knots and parameter range Maya actually stored and, for trimmed faces, whether Maya's result is flagged as trimmed and
  which probe points disagree (nothing is kept). Send me that output.

BISECTING A TRIM THAT FAILS
    mci.bisect_trim('/path/to/file.step', 'name-of-the-face', python='/path/to/cad2maya/cad2maya_env/bin/python', scale=100)
  Re-trims that one surface with subsets of its boundaries (outer only, each hole alone, a plain rectangle instead of the
  real outer boundary, holes only, ...) and prints a table of which combinations Maya accepts. That shows which boundary,
  or which combination of boundaries, the trim node cannot handle. Nothing is kept. Send me the table.

    mci.bisect_outer('/path/to/file.step', 'name-of-the-face', python='/path/to/cad2maya/cad2maya_env/bin/python', scale=100)
  Gives Maya the OUTER boundary of one face in several representations (the exact curve, an open-form copy, polylines of
  increasing detail, a plain rectangle as a control) and prints which of them it trims with.

OPTIONS (import_cad / import_json / import_data)
  scale=0.1              file units are millimetres; 0.1 gives Maya's centimetres
  trims=True             False = skip trimmed faces (or, with include_trimmed=True, import their FULL surfaces as a preview)
  trim_fallback=True     a trimmed face that cannot be trimmed AND verified is kept as its full untrimmed surface, named
                         <name>_UNTRIMMED_FALLBACK, and listed in the report (False = drop it and list it as failed)
  verify=True            measure every surface / probe every trim against OpenCascade (recommended)
  root_name=...          name of the top group

HOW TRIMMED FACES ARE BUILT
  surface + one closed boundary curve per trim loop (parameter space, outer counter-clockwise, holes clockwise)
  -> trimWithBoundaries -> history deleted (the result is a normal trimmed nurbsSurface).
  Verification: OpenCascade classified ~100 (u,v) probe points per face as inside/outside the face. The importer asks Maya
  the same question of the finished surface (MFnNurbsSurface.isPointInTrimmedRegion, or isParamOnSurface if that one does
  not behave) and compares. Several choices are not documented, so the importer tries them and keeps what verifies:
  an explicit outer boundary or Maya's native boundary, curve direction, which region query. The first working choice is
  remembered and printed in the report.

FALLBACKS FOR SURFACES MAYA REJECTS
  Maya's error messages are vague, so each surface is tried several ways until one verifies against OpenCascade:
  the 'open' form, the 'closed' form (for surfaces whose first and last CVs coincide), then the cmds.surface command.
  A failure names the Maya call that failed.
  For a trimmed face on a CLOSED surface whose outer boundary runs along both seam edges, Maya's trim node may silently do
  nothing; the last resort moves that boundary inward by 0.01% of the surface (a hairline gap at the seam). The report says
  how many faces needed that.

POLYLINE BOUNDARIES (a workaround for a Maya limitation)
  On some faces Maya's trim node silently ignores the exact NURBS boundary curve but trims correctly with the SAME boundary
  approximated as a polyline (found with mci.bisect_outer; the cause is not known). The importer tries the exact curve
  first. If that fails verification, it retries with the outer boundary as a polyline (16 vertices per curve span), then
  with all boundaries. Such faces are listed in the report with their measured deviation (typically ~1e-5 of the surface
  size, e.g. 0.002 cm on a 141 cm surface).

TRIM CURVES FROM CAD EXPORTERS
  CAD systems export trim curves only to about the tolerance recorded in the file, so the pieces of a trim loop rarely meet
  exactly. The extractor (1) bridges gaps whose two ends lie within 1.5x the file's own edge/vertex tolerance of each other in
  3D, (2) where two curves overshoot the corner and cross, cuts both at the crossing, (3) cuts a curve that curls back across
  its own end point, (4) caps a range that exceeds a periodic surface's period by noise, (5) clamps loops that stick out of
  the surface domain by a small amount. Faces that still cannot be converted are listed with the reason.

IF MAYA CRASHES
  Before each risky step the importer writes the face and stage it is about to work on to a small text file. After a crash,
  restart Maya and run:
      import maya_cad_import as mci; print(open(mci.PROGRESS_FILE).read())
  The line names the face and the stage (building / baking the trim / probing the region). Send it to me. (Maya's own crash
  file also names the Python line, if you can send that too.) Save your scene before importing a big file.

HIERARCHY
  The Maya groups mirror the CAD product tree, one group per OCCURRENCE: assemblies in assemblies, parts in assemblies, and bodies
  in a part that has several. Every group is named <name>_grp. Two instances of one sub-assembly stay two groups, each holding its
  own parts. A component is named after its occurrence (what the CAD browser shows, e.g. 'CoreWood:1'); an exporter-generated
  ID such as '0:1:1:2' or '9' never beats a real name. A top assembly that repeats the root's name is not nested again.
  An unnamed body is named after its part (Neck_1_body). With group_parts='auto', files in which every part is a single face
  (RenderMan conversions) stay flat. IGES has no standard assembly structure: what you get depends on the exporting program
  (Fusion keeps its top-level bodies as named parts; the STEP keeps the whole tree).
  Each group carries the ORIGINAL CAD names: attributes cadName (the occurrence name, e.g. 'CoreWood:1'), cadProduct, cadKind.

NODE NAMES (screened before they reach Maya)
  Rules from FBX Rx, ASCII-strict. Every character outside A-Z a-z 0-9 _ becomes an underscore ('CoreWood:1' -> CoreWood_1);
  accented letters are transliterated; repeated underscores collapse; a name that starts with a digit gets 'obj_' ('123' ->
  obj_123); an empty name gets a generated one (unnamed_part ...); Maya words (transform, top, persp ...) get '_obj'; duplicates are
  numbered _001, _002 ... so the _grp suffix stays last. The extractor does this, so the JSON is already clean, and the importer
  does it again for JSON from any source.

READING THE REPORT
  built N surface(s) (R rational) ... max deviation        every surface was measured against OpenCascade
  trimmed faces built and verified: T (probe points that disagreed ...: 0 of P)    the number that matters: should be 0
  trim method that worked / region query                    what this Maya turned out to need
  K trimmed face(s) too small/thin to place probe points    built but not region-verified (usually tiny slivers)
  N trimmed face(s) could NOT be trimmed ... _UNTRIMMED_FALLBACK   listed with the reason; the surface is there, untrimmed
  extractor: "N face(s) had trim loops ... clamped"         source-data quirk: trim curves poked out of their surface

WHAT TO SEND BACK  (when something looks wrong)
  The report from the window (Copy report), and a screenshot. For export problems with trimmed surfaces: select a few of them and run
      import maya_cad_probe as p; p.run()
  in the Script Editor (Python tab), then send its output.

NOT HANDLED YET
  Face orientation flags (normals follow the surface, not the CAD face direction); solids/shells come in as their individual faces
  and go out loose (stitch in Fusion); polygon meshes and subdivision surfaces are not exported (NURBS only).

TEST DATA  testdata/assembly.step (two colored cylinders), torus.step, box.igs,
           trim_plate.step (plate with two holes), trim_seam_cut.step (cylinder with a notch across its seam)
