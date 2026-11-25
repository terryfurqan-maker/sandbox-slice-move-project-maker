---
name: sandbox-slice-move-project-maker
description: >-
  Automatically builds a Midland Valley Move 2016 (.move + .movd) 3D geological
  project from an unreferenced sandbox map image with drawn transects and
  corresponding vertical slice/section images. Use when the user asks to create
  a Move 2016 project from sandbox slice photos, digitize transect lines into
  section traces, and mount vertical section images onto each section.
---

# Sandbox Slice Move Project Maker

Builds a complete, ready-to-open **Midland Valley Move 2016** project (`.move` + `.movd`) directly from:
1. A top-down sandbox **map image** (e.g., `image1.jpeg` with annotated transect lines `T1`–`TN`), and
2. A folder of **vertical section/slice images** (e.g., `1.JPG`–`N.JPG`), ordered to match the transects.

## Workflow

1. **Inspect Inputs:**
   * Verify the map image path and the folder containing the vertical slice images (`1.JPG`..`N.JPG`, or naturally sorted image files).
   * Confirm how section image numbers map to transect lines on the map (default: `1` -> `T1`/`L1`, `2` -> `T2`/`L2`, etc.) and confirm vertical orientation (default: image left-to-right corresponds to top-to-bottom of the transect on the map, `z` from `0.0` downward preserving each image's aspect ratio).

2. **Generate the Move Project (and Preview Transect Detection):**
   Run [make_move_project.py](./scripts/make_move_project.py):
   ```powershell
   python "C:\Users\User\.gemini\config\skills\sandbox-slice-move-project-maker\scripts\make_move_project.py" `
     --map "<path-to-map-image>" `
     --sections-dir "<path-to-sections-folder>" `
     --name "<project-name>" `
     --preview "<path-to-verify_transects.png>"
   ```
   * Optional flags:
     * `--out-dir <dir>`: Output directory (defaults to the parent directory of `--map`).
     * `--map-width <meters>`: Target horizontal extent in meters (defaults to pixel width of `--map`, preserving aspect ratio).
     * `--post-depth <meters>`: Bottom Z of section posts (default `-900.0`).
     * `--transects <json>`: Supply explicit transect endpoints instead of auto-detection.
     * `--verify`: Launches `Move.exe` (if installed) to load the generated `.move` file and verify `Import Summary` (`OK`).

3. **Visually Verify Detection & Output:**
   * Always inspect the `--preview` overlay image (`verify_transects.png`) to confirm every detected transect line (`T1`–`TN`) sits accurately on its annotated line before finalizing.
   * If `--verify` is used on Windows with Move 2016 installed, confirm `Import Summary` reports `OK` and `Move.exe` loads all sections and vertical images cleanly.
   * Ensure `Move.exe` is closed after verification so no `db/LOCK` file remains active.

## References

* [Move `.movd` Storage Format](./references/movd_format.md) — LevelDB, Protobuf, QDataStream, and `geos/` layout details.
