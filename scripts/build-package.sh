#!/usr/bin/env bash
set -euo pipefail

PKG="${1:?usage: $0 PACKAGE [REV]}"
REV="${2:-1}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/work/builds/$PKG"

JOBS="${BUILD_JOBS:-4}"
USE_RDISTRO="${USE_RDISTRO:-0}"

mkdir -p "$OUT"
rm -f "$OUT"/*

docker run --rm \
  --platform linux/arm64 \
  -e PKG="$PKG" \
  -e REV="$REV" \
  -e JOBS="$JOBS" \
  -e USE_RDISTRO="$USE_RDISTRO" \
  -v "$OUT:/output" \
  -v "$ROOT/repo/keys/rdistro-archive.asc:/etc/apt/keyrings/rdistro-archive.asc:ro" \
  rdistro-buildroot:2026-08-13 \
  bash -lc '
    set -eux

    export DEBIAN_FRONTEND=noninteractive

    # Use R-Distro binaries as build dependencies when available.
    cat >/etc/apt/sources.list.d/rdistro.sources <<EOF2
Types: deb
URIs: http://host.docker.internal:8080/
Suites: development
Components: main
Architectures: arm64
Signed-By: /etc/apt/keyrings/rdistro-archive.asc
EOF2

    apt-get update

    mkdir /build/work
    cd /build/work

    # Source still comes specifically from pinned Debian Snapshot.
    apt-get source "$PKG"

    SRC=$(find . -mindepth 1 -maxdepth 1 -type d -print -quit)
    cd "$SRC"

    OLD=$(dpkg-parsechangelog -SVersion)
    NEW="${OLD}+rdistro${REV}"

    export DEBFULLNAME="R-Distro Build System"
    export DEBEMAIL="build@rdistro.local"

    dch \
      --newversion "$NEW" \
      --distribution development \
      --force-distribution \
      "Rebuilt by R-Distro from pinned Debian source."

    echo "Building $PKG: $OLD -> $NEW"

    # ----------------------------------------------------
    # Build/export Debian SOURCE package
    # ----------------------------------------------------

    dpkg-source -b .

    DSC=$(find .. \
      -maxdepth 1 \
      -type f \
      -name "${PKG}_*.dsc" \
      -printf "%T@ %p\n" \
      | sort -nr \
      | head -1 \
      | cut -d" " -f2-)

    test -f "$DSC"

    cp "$DSC" /output/

    DSCDIR=$(dirname "$DSC")

    while read -r FILE; do
      test -f "$DSCDIR/$FILE"
      cp "$DSCDIR/$FILE" /output/
    done < <(
      awk "
        /^Files:/ { inside=1; next }
        inside && /^[^ ]/ { exit }
        inside { print \$3 }
      " "$DSC"
    )

    SRC=$(find /build/work \
      -mindepth 1 -maxdepth 1 \
      -type d \
      -print -quit)

    test -n "$SRC"

    # ----------------------------------------------------
    # Install Build-Depends
    # ----------------------------------------------------

    mk-build-deps \
      --install \
      --remove \
      --tool "apt-get -y --no-install-recommends" \
      debian/control

    # Debian package compilation must not run as root.
    chown -R builder:builder /build/work

    # ----------------------------------------------------
    # Build/export Debian BINARY packages
    # ----------------------------------------------------

    runuser -u builder -- \
      bash -c '
          cd "$1"
          dpkg-buildpackage \
              -us \
              -uc \
              -b \
              -j"$2"
      ' _ "$SRC" "$JOBS"

    cp ../*.deb /output/
    cp ../*.buildinfo /output/ 2>/dev/null || true
    cp ../*.changes /output/ 2>/dev/null || true

    # Preserve source artifacts.
    cp ../*.dsc /output/ 2>/dev/null || true
    cp ../*.orig.tar.* /output/ 2>/dev/null || true
    cp ../*.debian.tar.* /output/ 2>/dev/null || true
    cp ../*.diff.gz /output/ 2>/dev/null || true
  '

echo
echo "Artifacts:"
ls -lh "$OUT"
