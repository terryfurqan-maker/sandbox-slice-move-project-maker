# Sandbox Slice Move Project Maker

An Antigravity skill and standalone Python tool for automatically creating **Midland Valley Move 2016** (`.move` + `.movd`) 3D geological projects from sandbox analogue model top-view maps and vertical slice photographs.

## What It Does

1. Reads an unreferenced top-view sandbox map image (e.g., `image1.jpeg`) and preserves its aspect ratio in Move metric coordinates (`[0..W, 0..H]`).
2. Automatically detects annotated colored transect lines (`T1`–`TN`) on the map (or reads explicit transect coordinates from JSON).
3. Creates `PolyLine` section traces (`L1`–`LN`) with vertical `Post2` endpoints (`z = -900` to `0 m`).
4. Mounts each vertical slice image (`1.JPG`–`N.JPG`) onto its corresponding section as a `SegyImage` scaled by the transect length and image aspect ratio.
5. Writes a valid Move 2016 `.move` + `.movd` LevelDB project directly, with optional verification in `Move.exe`.

## Requirements

* Python 3.10+
* `Pillow` (`pip install pillow`)
* Midland Valley Move 2016.1 (only required to open the project or run `--verify`)

## Usage

```bash
python scripts/make_move_project.py \
  --map "/path/to/image1.jpeg" \
  --sections-dir "/path/to/SLICE-P1-crop-flipped" \
  --name "v3_1_project" \
  --preview "verify_transects.png"
```

Pass `--verify` on Windows with Move 2016 installed to automatically open the generated `.move` file in `Move.exe`, confirm `Import Summary: OK`, and capture screenshots.

## Installing as an Antigravity Skill

Copy this folder to your global Antigravity skills directory:

* Windows: `%USERPROFILE%\.gemini\config\skills\sandbox-slice-move-project-maker\`
* Linux/macOS: `~/.gemini/config/skills/sandbox-slice-move-project-maker/`
