#!/bin/bash
# Render manim scenes for a chalk project.
#
# Usage:
#   ./render.sh                     — render all landscape scenes at -ql
#   ./render.sh -qh                 — render all landscape scenes at -qh
#   ./render.sh -qh S03_Square      — render one scene at -qh
#   ./render.sh --shorts            — render all shorts scenes at -qh
#   ./render.sh --shorts -ql        — render all shorts scenes at -ql
#   ./render.sh --shorts S03_Square — render one shorts scene

set -euo pipefail

# ── defaults ─────────────────────────────────────────────────
QUALITY="-ql"
SHORTS=false
SCENES=()

# ── parse args ───────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        -ql|-qm|-qh|-qk) QUALITY="$1"; shift ;;
        --shorts)         SHORTS=true; shift ;;
        -*)               echo "Unknown flag: $1"; exit 1 ;;
        *)                SCENES+=("$1"); shift ;;
    esac
done

# ── pick source file ─────────────────────────────────────────
if $SHORTS; then
    SRC="timed_scenes_shorts.py"
else
    SRC="timed_scenes.py"
fi

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found"
    exit 1
fi

# ── discover scenes if none specified ────────────────────────
if [ ${#SCENES[@]} -eq 0 ]; then
    while IFS= read -r line; do
        SCENES+=("$line")
    done < <(grep -oE 'class (S[0-9a-z_]+[A-Za-z_]+)\(' "$SRC" | sed 's/class //;s/($//')
fi

if [ ${#SCENES[@]} -eq 0 ]; then
    echo "ERROR: no scene classes found in $SRC"
    exit 1
fi

# ── activate venv ────────────────────────────────────────────
if [ -d .venv ] && [ -z "${VIRTUAL_ENV:-}" ]; then
    source .venv/bin/activate
fi

# ── render ───────────────────────────────────────────────────
if $SHORTS; then
    echo "rendering ${#SCENES[@]} shorts scenes ($QUALITY)..."
    for scene in "${SCENES[@]}"; do
        echo "  $scene"
        manim render -r 1080,1920 --fps 60 "$QUALITY" "$SRC" "$scene"
    done
else
    echo "rendering ${#SCENES[@]} scenes ($QUALITY)..."
    for scene in "${SCENES[@]}"; do
        echo "  $scene"
        manim render "$QUALITY" "$SRC" "$scene"
    done
fi

echo ""
echo "done. rendered ${#SCENES[@]} scenes from $SRC"
