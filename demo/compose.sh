#!/bin/bash
# Compose demo videos — combine VNC recording + terminal recording side by side.
#
# Usage:
#   ./demo/compose.sh vnc.mp4 terminal.cast output.mp4
#   ./demo/compose.sh vnc.mp4 terminal.cast output.mp4 --layout vertical
#
# The terminal .cast file is first converted to a video via agg (asciinema gif generator)
# or svg-term, then composed alongside the VNC recording.

set -e

VNC_VIDEO="${1:?Usage: compose.sh <vnc.mp4> <terminal.cast> <output.mp4> [--layout horizontal|vertical]}"
TERMINAL_CAST="${2:?Missing terminal.cast}"
OUTPUT="${3:?Missing output.mp4}"
LAYOUT="${5:-horizontal}"  # horizontal (side-by-side) or vertical (stacked)

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "▶ Converting terminal recording to video..."

# Method 1: Use agg (asciinema gif generator) if available
if command -v agg &>/dev/null; then
    agg "$TERMINAL_CAST" "$TMPDIR/terminal.gif" \
        --font-size 14 --cols 80 --rows 24
    ffmpeg -y -i "$TMPDIR/terminal.gif" \
        -c:v libx264 -preset fast -crf 20 \
        -vf "scale=-1:720" \
        "$TMPDIR/terminal.mp4" </dev/null 2>/dev/null
# Method 2: Use asciinema-player in headless browser
elif command -v npx &>/dev/null; then
    # Generate SVG frames with svg-term-cli
    npx --yes svg-term-cli --in "$TERMINAL_CAST" \
        --out "$TMPDIR/terminal.svg" \
        --width 80 --height 24 2>/dev/null || {
        echo "  ⚠ svg-term-cli failed, using placeholder"
        # Create a simple black video as placeholder
        ffmpeg -y -f lavfi -i color=c=black:s=640x720:d=60 \
            -c:v libx264 "$TMPDIR/terminal.mp4" </dev/null 2>/dev/null
    }
else
    echo "  ⚠ Neither agg nor npx available — install with:"
    echo "    cargo install agg   # or"
    echo "    npm install -g svg-term-cli"
    echo "  Using terminal cast as-is (no video conversion)"
    cp "$VNC_VIDEO" "$OUTPUT"
    echo "▶ Output (VNC only): $OUTPUT"
    exit 0
fi

TERM_VIDEO="$TMPDIR/terminal.mp4"

if [ ! -f "$TERM_VIDEO" ]; then
    echo "  ⚠ Terminal video conversion failed, outputting VNC only"
    cp "$VNC_VIDEO" "$OUTPUT"
    exit 0
fi

echo "▶ Composing ${LAYOUT} layout..."

if [ "$LAYOUT" = "vertical" ]; then
    # Stack vertically: VNC on top, terminal on bottom
    ffmpeg -y \
        -i "$VNC_VIDEO" -i "$TERM_VIDEO" \
        -filter_complex "[0:v]scale=1280:540[top];[1:v]scale=1280:180[bot];[top][bot]vstack" \
        -c:v libx264 -preset fast -crf 22 \
        "$OUTPUT" </dev/null 2>/dev/null
else
    # Side by side: VNC left, terminal right
    ffmpeg -y \
        -i "$VNC_VIDEO" -i "$TERM_VIDEO" \
        -filter_complex "[0:v]scale=854:720[left];[1:v]scale=426:720[right];[left][right]hstack" \
        -c:v libx264 -preset fast -crf 22 \
        "$OUTPUT" </dev/null 2>/dev/null
fi

echo "▶ Output: $OUTPUT"
ls -lh "$OUTPUT"
