#!/bin/sh
# Downloads the Core ML models this project needs. Nothing here is in the repository.
#
#   Depth Anything V2 Small F16   47 MB
#   huggingface.co/apple/coreml-depth-anything-v2-small
#
# WHY IT IS NOT COMMITTED. 47 MB of weights in a repository that ships to everybody on
# GitHub would be in every clone, every fork and every diff, and it is a file Apple
# already hosts. `models/` is gitignored; this script is the supported way to fill it.
#
# curl rather than huggingface_hub: no account, no token, no extra dependency. The
# files are public.
#
# An .mlpackage IS A DIRECTORY, so the layout below is not decoration - Core ML will
# not load one whose Manifest.json and Data/ are not exactly where it expects them.
# That is why this fetches three files into a tree rather than one archive.
#
# Re-runnable: anything already present and big enough is skipped, so this is also the
# repair path for an interrupted download.
#
# Ref: docs/DEPTH.md for what to do after this, and for the pins.
set -eu
cd "$(dirname "$0")/.."
mkdir -p models
HF=https://huggingface.co

big_enough() { [ -f "$1" ] && [ "$(wc -c < "$1")" -gt "$2" ]; }

fetch_package() {   # repo, package name, expected weight bytes
    pkg="models/$2"
    if big_enough "$pkg/Data/com.apple.CoreML/weights/weight.bin" "$3"; then
        echo "have $2"
        return 0
    fi
    echo "fetching $2 (about 47 MB)"
    base="$HF/$1/resolve/main/$2"
    mkdir -p "$pkg/Data/com.apple.CoreML/weights"
    curl -fsSL -o "$pkg/Manifest.json" "$base/Manifest.json"
    curl -fsSL -o "$pkg/Data/com.apple.CoreML/model.mlmodel" \
        "$base/Data/com.apple.CoreML/model.mlmodel"
    # No -s on the big one: a 47 MB download with no progress bar looks like a hang.
    curl -fSL -o "$pkg/Data/com.apple.CoreML/weights/weight.bin" \
        "$base/Data/com.apple.CoreML/weights/weight.bin"
}

fetch_package apple/coreml-depth-anything-v2-small \
    DepthAnythingV2SmallF16.mlpackage 40000000

echo
echo "Models are in ./models and are gitignored."
echo "They compile to .mlmodelc on first use - a few seconds, once, and the sidecar"
echo "prints that it is happening. Delete the .mlmodelc to force a rebuild."
echo
echo "Check it works, with no camera and no TouchDesigner:"
echo "    ~/.venvs/visionhands/bin/python tools/depth_probe.py"
