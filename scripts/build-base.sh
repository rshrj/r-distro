#!/usr/bin/env bash
set -euo pipefail

REV="${1:-1}"
PARALLEL="${PARALLEL:-2}"
BUILD_JOBS="${BUILD_JOBS:-4}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$ROOT/logs/pass$REV"
: > "$ROOT/logs/pass$REV/succeeded"
: > "$ROOT/logs/pass$REV/failed"

build_one()
{
    pkg="$1"

    echo "[BUILD] $pkg"

    if BUILD_JOBS="$BUILD_JOBS" \
       "$ROOT/scripts/build-package.sh" "$pkg" "$REV" \
       >"$ROOT/logs/pass$REV/$pkg.log" 2>&1
    then
        echo "$pkg" >>"$ROOT/logs/pass$REV/succeeded"
        echo "[ OK  ] $pkg"
    else
        echo "$pkg" >>"$ROOT/logs/pass$REV/failed"
        echo "[FAIL ] $pkg"
    fi
}

export ROOT REV BUILD_JOBS
export -f build_one

xargs -P "$PARALLEL" -n1 \
    bash -c 'build_one "$1"' _ \
    < "$ROOT/manifests/base-sources.txt"

echo
echo "Succeeded: $(wc -l < "$ROOT/logs/pass$REV/succeeded")"
echo "Failed:    $(wc -l < "$ROOT/logs/pass$REV/failed")"
