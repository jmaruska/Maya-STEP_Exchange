#!/usr/bin/env bash
#
# setup_macos.sh - create a Python virtual environment for rib2nurbs on macOS.
#
# Usage:   ./setup_macos.sh [env_dir]         (any folder name; it does not have to be hidden)
#
# With no argument it reuses an environment that already sits next to this script (.venv, venv or cad2maya_env), and
# otherwise creates a visible folder called cad2maya_env. Environments are NOT portable between machines (they record
# absolute paths and hold compiled code for this Mac's Python): to move the tools, copy the scripts and run this again.
#
# Installs: numpy, matplotlib, cadquery-ocp==8.0.1.1.0 (OpenCascade bindings).
# cadquery-ocp 8.0.1.1.0 needs Python 3.11 - 3.14. Apple's built-in /usr/bin/python3 is
# 3.9 and is too old; install a newer one with:  brew install python@3.12
# (or use the installer from python.org).

set -euo pipefail

MIN_MINOR=11
MAX_MINOR=14
PACKAGES=(numpy matplotlib "cadquery-ocp==8.0.1.1.0")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${1:-}" ]; then
    VENV_DIR="$1"
else
    VENV_DIR="$SCRIPT_DIR/cad2maya_env"                       # a fresh machine gets a visible folder
    for name in .venv venv cad2maya_env; do                   # ...but an environment that already exists is reused, not duplicated
        if [ -x "$SCRIPT_DIR/$name/bin/python" ]; then VENV_DIR="$SCRIPT_DIR/$name"; break; fi
    done
fi
case "$VENV_DIR" in /*) ;; *) VENV_DIR="$PWD/$VENV_DIR" ;; esac
printf 'Environment folder: %s\n' "$VENV_DIR"

say()  { printf '%s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

if [ "$(uname -s)" != "Darwin" ]; then
    say "Note: this script is written for macOS (detected $(uname -s)); continuing anyway."
fi

# True if the given python executable is within the supported version range.
python_ok() {
    "$1" -c "import sys; sys.exit(0 if (3, $MIN_MINOR) <= sys.version_info[:2] <= (3, $MAX_MINOR) else 1)" >/dev/null 2>&1
}

# ---------------------------------------------------------------- find a suitable Python
# Prefer 3.12/3.13 (best-tested wheels), then the other supported versions, then plain python3.
CANDIDATES=(
    python3.13 python3.12 python3.11 python3.14
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.14
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3.14
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
    python3
)

PY=""
for c in "${CANDIDATES[@]}"; do
    if command -v "$c" >/dev/null 2>&1; then
        resolved="$(command -v "$c")"
        if python_ok "$resolved"; then PY="$resolved"; break; fi
    fi
done

if [ -z "$PY" ]; then
    found="$(command -v python3 >/dev/null 2>&1 && python3 --version 2>&1 || echo 'no python3 found')"
    fail "No Python 3.$MIN_MINOR - 3.$MAX_MINOR found (python3 on PATH: $found).
Install one, then re-run this script:
    brew install python@3.12
or download an installer from https://www.python.org/downloads/macos/"
fi

say "Using Python: $PY ($("$PY" --version 2>&1))"

# Warn about Rosetta / architecture mismatch (works, but slower).
HOST_ARCH="$(uname -m)"
PY_ARCH="$("$PY" -c 'import platform; print(platform.machine())')"
if [ "$HOST_ARCH" != "$PY_ARCH" ]; then
    say "Note: this Python is $PY_ARCH but the machine is $HOST_ARCH (running under Rosetta). It will work, but a native Python is faster."
fi

# ---------------------------------------------------------------- create / reuse the venv
if [ -x "$VENV_DIR/bin/python" ]; then
    if python_ok "$VENV_DIR/bin/python"; then
        say "Reusing existing virtual environment: $VENV_DIR"
    else
        fail "Existing venv at $VENV_DIR uses an unsupported Python ($("$VENV_DIR/bin/python" --version 2>&1)).
Delete it (rm -rf \"$VENV_DIR\") and re-run, or pass a different directory."
    fi
else
    say "Creating virtual environment: $VENV_DIR"
    "$PY" -m venv "$VENV_DIR" || fail "Could not create the virtual environment."
fi

VENV_PY="$VENV_DIR/bin/python"

# ---------------------------------------------------------------- install packages
say "Upgrading pip ..."
"$VENV_PY" -m pip install --quiet --upgrade pip

say "Installing: ${PACKAGES[*]}"
say "(cadquery-ocp pulls in VTK - roughly 150 MB - so this can take a minute.)"
# --only-binary: never try to compile from source; fail fast if no wheel exists for this Mac.
if ! "$VENV_PY" -m pip install --only-binary=:all: "${PACKAGES[@]}"; then
    fail "pip could not install the packages. cadquery-ocp wheels exist for macOS 11+ (Apple Silicon and Intel).
If you are on an older macOS, or pip reported 'no matching distribution', that is the likely cause."
fi

# ---------------------------------------------------------------- smoke test
say "Verifying install ..."
"$VENV_PY" - <<'EOF' || fail "Installed, but the import check failed (see error above)."
import numpy, matplotlib
from OCP.gp import gp_Pnt
from OCP.Geom import Geom_BSplineSurface
from OCP.collections import Array2_gp_Pnt   # the OCP 8.x layout the scripts rely on
print("  numpy %s, matplotlib %s, OpenCascade bindings OK" % (numpy.__version__, matplotlib.__version__))
EOF

# the cad2maya scripts themselves must load in this environment (they live in scripts/ next to this file)
if [ -f "$SCRIPT_DIR/scripts/cad_extract.py" ]; then
    "$VENV_PY" -c "import sys; sys.path.insert(0, '$SCRIPT_DIR/scripts'); import cad_names, cad_extract, cad_write" >/dev/null 2>&1 \
        && say "  cad2maya scripts load OK" \
        || fail "the cad2maya scripts failed to load; run: \"$VENV_PY\" -c \"import sys; sys.path.insert(0, '$SCRIPT_DIR/scripts'); import cad_extract\" to see why."
fi

cat <<EOF

Setup complete. The Python environment is in:
    $VENV_DIR

Next, in Maya: drag install_cad2maya.py (in $SCRIPT_DIR) into a viewport.
It adds the CAD import and export buttons to your current shelf.
EOF
