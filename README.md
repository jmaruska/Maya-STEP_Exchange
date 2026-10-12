# Maya STEP Exchange

Bring CAD surfaces into Maya as real NURBS, edit them, and send them back to CAD — with the trims, the part hierarchy, the names and the colours intact.

## Why this exists

On an Apple Silicon Mac, Maya has no dependable way to read CAD data: its IGES plug-in crashes, and the usual route through FBX loses the assembly structure, so a model arrives as a flat pile of surfaces that has to be reorganised by hand every time.

This tool fills that gap with an open-source geometry kernel ([OpenCascade](https://dev.opencascade.org/)) and a pair of shelf buttons.

## What it does

**CAD in** — imports STEP and IGES files as Maya NURBS surfaces:
- trimmed surfaces stay trimmed, and every trim is checked against the original file
- assemblies and parts become nested groups, with Maya-safe versions of the original names (the originals are kept on each group)
- colours become shaders
- units are read from the file

**CAD out** — exports Maya NURBS surfaces as STEP (or IGES):
- the group hierarchy becomes assemblies and parts, with the original CAD names restored
- surfaces imported with CAD in go back at their original size
- nothing is approximated or "repaired" behind your back: a surface that can't be written exactly is left out and listed in the report

Faces are exported loose, grouped by part — stitch them into bodies in Fusion (or other CAD tool) with its own Stitch tool.

## How it works

OpenCascade never runs inside Maya. A small Python environment, set up once, does the CAD reading and writing; Maya only builds and reads surfaces. The two sides talk through a plain JSON file. That keeps Maya stable and lets each side check the other's work.

## Getting started

You need macOS, Maya, and Python 3.11–3.14 (for example `brew install python@3.12`).

1. In Terminal, run `cad2maya/setup_macos.sh` (about a minute).
2. Drag `cad2maya/install_cad2maya.py` into a Maya viewport. It adds the **CAD in** and **CAD out** buttons to your shelf.

The full manual, including options, troubleshooting and what to send when something looks wrong, is in [`cad2maya/README.txt`](cad2maya/README.txt).

## Status

Developed and used on an Apple Silicon Mac with Maya 2027, with STEP and IGES files from Fusion and Alias. Import is in regular use. Export is new: untrimmed surfaces round-trip cleanly, and trimmed surfaces are still being proven in real projects.
