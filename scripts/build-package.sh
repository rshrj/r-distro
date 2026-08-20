#!/usr/bin/env bash
set -euo pipefail

PKG="${1:?usage: $0 PACKAGE [REV]}"
REV="${2:-1}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${RDISTRO_OUTPUT_DIR:-$ROOT/work/builds/$PKG}"

RDISTRO_JOBS="${BUILD_JOBS:-4}"
USE_RDISTRO="${USE_RDISTRO:-0}"

DEB_BUILD_OPTIONS="${DEB_BUILD_OPTIONS:-nocheck nodoc}"
DEB_BUILD_PROFILES="${DEB_BUILD_PROFILES:-nocheck nodoc}"
PRE_BUILD_COMMAND="${PRE_BUILD_COMMAND:-}"

POLICY_FILE="$ROOT/config/package-policy/$PKG.env"

if [ -f "$POLICY_FILE" ]; then
    # shellcheck disable=SC1090
    source "$POLICY_FILE"
fi

mkdir -p "$OUT"
rm -rf "$OUT"/*

docker run --rm \
  --platform linux/arm64 \
  -e PKG="$PKG" \
  -e REV="$REV" \
  -e RDISTRO_JOBS="$RDISTRO_JOBS" \
  -e USE_RDISTRO="$USE_RDISTRO" \
  -e DEB_BUILD_OPTIONS="$DEB_BUILD_OPTIONS" \
  -e DEB_BUILD_PROFILES="$DEB_BUILD_PROFILES" \
  -e PRE_BUILD_COMMAND="$PRE_BUILD_COMMAND" \
  -v "$OUT:/output" \
  -v "$ROOT/repo/keys/rdistro-archive.asc:/etc/apt/keyrings/rdistro-archive.asc:ro" \
  rdistro-buildroot:2026-08-13 \
  bash -lc '
    set -euo pipefail
    set -x

    export DEBIAN_FRONTEND=noninteractive

    # ----------------------------------------------------
    # Optional R-Distro build-dependency repository
    # ----------------------------------------------------

    if [ "$USE_RDISTRO" = "1" ]; then
      cat >/etc/apt/sources.list.d/rdistro.sources <<EOF
Types: deb
URIs: http://host.docker.internal:8080/
Suites: development
Components: main
Architectures: arm64
Signed-By: /etc/apt/keyrings/rdistro-archive.asc
EOF
    fi

    apt-get \
      -o Acquire::Retries=5 \
      update \
      --error-on=any

    # For Pass 2+, replace already-installed Debian packages with
    # R-Distro versions whenever an R-Distro upgrade is available.
    if [ "$USE_RDISTRO" = "1" ]; then
        mapfile -t RDISTRO_UPGRADES < <(
            apt-get -s upgrade |
            awk "/^Inst / && /[+]rdistro[0-9]+/ { print \$2 }"
        )

        if [ "${#RDISTRO_UPGRADES[@]}" -gt 0 ]; then
            echo "Upgrading installed packages to R-Distro:"
            printf "  %s\n" "${RDISTRO_UPGRADES[@]}"

            apt-get install \
                -y \
                --no-install-recommends \
                --only-upgrade \
                "${RDISTRO_UPGRADES[@]}"
        else
            echo "No installed packages have R-Distro upgrades available."
        fi
    fi

    rm -rf /build/work
    mkdir -p /build/work
    cd /build/work

    # Source comes from pinned Debian Snapshot because
    # R-Distro currently has no deb-src entry configured here.
    apt-get source "$PKG"

    SRC=$(find /build/work \
      -mindepth 1 \
      -maxdepth 1 \
      -type d \
      -print -quit)

    test -n "$SRC"
    cd "$SRC"

    SOURCE=$(dpkg-parsechangelog -SSource)
    OLD=$(dpkg-parsechangelog -SVersion)
    NEW="${OLD}+rdistro${REV}"

    export DEBFULLNAME="R-Distro Build System"
    export DEBEMAIL="build@rdistro.local"

    dch \
      --newversion "$NEW" \
      --distribution development \
      --force-distribution \
      "Rebuilt by R-Distro from pinned Debian source."

    # dch can rename native-package source directories.
    SRC=$(find /build/work \
      -mindepth 1 \
      -maxdepth 1 \
      -type d \
      -print -quit)

    test -n "$SRC"
    cd "$SRC"

    echo "Building source package $SOURCE: $OLD -> $NEW"



    # ----------------------------------------------------
    # Install Build-Depends as root
    # ----------------------------------------------------

    cd "$SRC"

    export DEB_BUILD_OPTIONS
    export DEB_BUILD_PROFILES

    mk-build-deps \
      --install \
      --remove \
      --tool "apt-get -y --no-install-recommends" \
      debian/control

    if [ -n "${PRE_BUILD_COMMAND:-}" ]; then
        echo "+ applying package pre-build policy:"
        echo "  $PRE_BUILD_COMMAND"

        (
            cd "$SRC"

            # Package-preparation hooks must not contaminate the
            # Debian source tree with Python bytecode caches.
            export PYTHONDONTWRITEBYTECODE=1

            bash -euxo pipefail -c "$PRE_BUILD_COMMAND"
        )
    fi

    # ----------------------------------------------------
    # Sanitize source tree after preparation hooks
    # ----------------------------------------------------

    find "$SRC" \
        -type d \
        -name __pycache__ \
        -prune \
        -exec rm -rf {} +

    find "$SRC" \
        -type f \
        \( -name '*.pyc' -o -name '*.pyo' \) \
        -delete

    # ----------------------------------------------------
    # Build/export Debian SOURCE package
    # ----------------------------------------------------

    dpkg-source -b .

    DSC=$(find /build/work \
      -maxdepth 1 \
      -type f \
      -name "${SOURCE}_*rdistro${REV}.dsc" \
      -print -quit)

    test -n "$DSC"
    test -f "$DSC"

    cp "$DSC" /output/

    DSCDIR=$(dirname "$DSC")

    # Copy exactly the source files referenced by the .dsc.
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

    # Actual package compilation should be unprivileged.
    chown -R builder:builder /build/work

    # ----------------------------------------------------
    # Build Debian BINARY packages as builder
    # ----------------------------------------------------

    runuser -u builder -- \
      bash -c "
        set -euo pipefail

        cd \"\$1\"

        export DEB_BUILD_OPTIONS=\"\$3\"
        export DEB_BUILD_PROFILES=\"\$4\"

        dpkg-buildpackage \
          -us \
          -uc \
          -b \
          -j\"\$2\"
      " _ "$SRC" "$RDISTRO_JOBS" "$DEB_BUILD_OPTIONS" "$DEB_BUILD_PROFILES"

    # ----------------------------------------------------
    # Export binary build artifacts
    # ----------------------------------------------------

    cp /build/work/*.deb /output/
    cp /build/work/*.buildinfo /output/ 2>/dev/null || true
    cp /build/work/*.changes /output/ 2>/dev/null || true
  '

echo
echo "Artifacts:"
ls -lh "$OUT"