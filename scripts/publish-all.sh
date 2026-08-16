#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

BUILDS="$ROOT/work/builds"
REPO="$ROOT/repo"
KEYSRC="$ROOT/keys/gnupg"

DIST="development"
COMPONENT="main"
ARCH="arm64"

POOL="$REPO/pool/main/r/rdistro"

SIGNING_KEY="${SIGNING_KEY:-archive@rdistro.local}"

# ----------------------------------------------------------------------
# Sanity checks
# ----------------------------------------------------------------------

test -d "$BUILDS" || {
    echo "ERROR: build directory not found: $BUILDS" >&2
    exit 1
}

test -d "$KEYSRC" || {
    echo "ERROR: signing-key directory not found: $KEYSRC" >&2
    exit 1
}

test -f "$KEYSRC/pubring.kbx" || {
    echo "ERROR: $KEYSRC/pubring.kbx not found" >&2
    exit 1
}

test -d "$KEYSRC/private-keys-v1.d" || {
    echo "ERROR: $KEYSRC/private-keys-v1.d not found" >&2
    exit 1
}

echo "Checking repository tooling..."

docker run --rm \
    rdistro-controller:2026-08-13 \
    bash -lc '
        set -e
        command -v apt-ftparchive >/dev/null
        command -v gpg >/dev/null
        command -v gzip >/dev/null
    ' || {
        echo >&2
        echo "ERROR: rdistro-controller:2026-08-13 is missing required tools." >&2
        echo "It needs at least apt-utils, gnupg and gzip." >&2
        exit 1
    }

# ----------------------------------------------------------------------
# Reconstruct pool from current R-Distro build artifacts
# ----------------------------------------------------------------------

echo
echo "Publishing R-Distro build artifacts..."

rm -rf "$POOL"
mkdir -p "$POOL"

published_sources=0
published_binaries=0

while IFS= read -r -d '' dir; do
    # There should normally be exactly one current +rdistro*.dsc.
    # Pick the highest version if more than one happens to remain.
    shopt -s nullglob

    dsc_candidates=("$dir"/*+rdistro*.dsc)

    if [ "${#dsc_candidates[@]}" -eq 0 ]; then
        echo "WARNING: no R-Distro .dsc found in $dir" >&2
        continue
    fi

    if [ "${#dsc_candidates[@]}" -ne 1 ]; then
        echo "ERROR: expected exactly one R-Distro .dsc in $dir, found:" >&2
        printf '  %s\n' "${dsc_candidates[@]}" >&2
        exit 1
    fi

    dsc="${dsc_candidates[0]}"

    if [ -n "$dsc" ]; then
        cp -f "$dsc" "$POOL/"
        published_sources=$((published_sources + 1))

        # Copy exactly the source files referenced by this .dsc.
        while read -r file; do
            [ -n "$file" ] || continue

            src="$(dirname "$dsc")/$file"

            if [ ! -f "$src" ]; then
                echo "ERROR: source component referenced by .dsc is missing:" >&2
                echo "       $src" >&2
                exit 1
            fi

            cp -f "$src" "$POOL/"
        done < <(
            awk '
                /^Files:/ {
                    inside=1
                    next
                }

                inside && /^[^ ]/ {
                    exit
                }

                inside {
                    print $3
                }
            ' "$dsc"
        )
    fi

    # Publish only locally-versioned binaries.
    while IFS= read -r -d '' deb; do
        cp -f "$deb" "$POOL/"
        published_binaries=$((published_binaries + 1))
    done < <(
        find "$dir" \
            -maxdepth 1 \
            -type f \
            -name '*+rdistro*.deb' \
            -print0
    )

done < <(
    find "$BUILDS" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        -print0
)

echo "Source packages found: $published_sources"
echo "Binary packages found: $published_binaries"
echo "Pool files:            $(find "$POOL" -maxdepth 1 -type f | wc -l | tr -d ' ')"

if [ "$published_sources" -eq 0 ]; then
    echo "ERROR: no R-Distro source packages found." >&2
    exit 1
fi

if [ "$published_binaries" -eq 0 ]; then
    echo "ERROR: no R-Distro binary packages found." >&2
    exit 1
fi

# ----------------------------------------------------------------------
# Generate Packages, Sources, Release and signatures inside Debian
# ----------------------------------------------------------------------

echo
echo "Generating and signing repository metadata..."

docker run --rm \
    --platform linux/arm64 \
    -e DIST="$DIST" \
    -e COMPONENT="$COMPONENT" \
    -e ARCH="$ARCH" \
    -e SIGNING_KEY="$SIGNING_KEY" \
    -v "$REPO:/repo" \
    -v "$KEYSRC:/keysrc:ro" \
    rdistro-controller:2026-08-13 \
    bash -lc '
        set -euo pipefail

        command -v apt-ftparchive >/dev/null
        command -v gpg >/dev/null
        command -v gzip >/dev/null

        DISTDIR="/repo/dists/$DIST"
        BINDIR="$DISTDIR/$COMPONENT/binary-$ARCH"
        SRCDIR="$DISTDIR/$COMPONENT/source"

        mkdir -p "$BINDIR" "$SRCDIR"

        cd /repo

        # --------------------------------------------------------------
        # Binary index
        # --------------------------------------------------------------

        apt-ftparchive packages pool/main/r/rdistro \
            > "$BINDIR/Packages"

        gzip -9n -c "$BINDIR/Packages" \
            > "$BINDIR/Packages.gz"

        # --------------------------------------------------------------
        # Source index
        # --------------------------------------------------------------

        apt-ftparchive sources pool/main/r/rdistro \
            > "$SRCDIR/Sources"

        gzip -9n -c "$SRCDIR/Sources" \
            > "$SRCDIR/Sources.gz"

        # --------------------------------------------------------------
        # Release metadata
        # --------------------------------------------------------------

        cat >/tmp/rdistro-release.conf <<EOF
APT::FTPArchive::Release::Origin "R-Distro";
APT::FTPArchive::Release::Label "R-Distro";
APT::FTPArchive::Release::Suite "$DIST";
APT::FTPArchive::Release::Codename "$DIST";
APT::FTPArchive::Release::Architectures "$ARCH";
APT::FTPArchive::Release::Components "$COMPONENT";
APT::FTPArchive::Release::Description "R-Distro development repository";
EOF

        apt-ftparchive \
            -c /tmp/rdistro-release.conf \
            release "$DISTDIR" \
            > "$DISTDIR/Release"

        # --------------------------------------------------------------
        # Signing
        #
        # Do not use the macOS-mounted GNUPG directory directly because
        # gpg-agent needs Unix-domain sockets. Copy persistent key
        # material into a container-local GNUPGHOME.
        # --------------------------------------------------------------

        export GNUPGHOME=/tmp/gnupg

        rm -rf "$GNUPGHOME"
        mkdir -p "$GNUPGHOME"
        chmod 700 "$GNUPGHOME"

        cp /keysrc/pubring.kbx "$GNUPGHOME/"

        if [ -f /keysrc/trustdb.gpg ]; then
            cp /keysrc/trustdb.gpg "$GNUPGHOME/"
        fi

        cp -a /keysrc/private-keys-v1.d "$GNUPGHOME/"
        chmod 700 "$GNUPGHOME/private-keys-v1.d"

        rm -f \
            "$DISTDIR/InRelease" \
            "$DISTDIR/Release.gpg"

        gpg \
            --batch \
            --yes \
            --local-user "$SIGNING_KEY" \
            --clearsign \
            --output "$DISTDIR/InRelease" \
            "$DISTDIR/Release"

        gpg \
            --batch \
            --yes \
            --local-user "$SIGNING_KEY" \
            --armor \
            --detach-sign \
            --output "$DISTDIR/Release.gpg" \
            "$DISTDIR/Release"
    '

# ----------------------------------------------------------------------
# Final verification
# ----------------------------------------------------------------------

PACKAGES="$REPO/dists/$DIST/$COMPONENT/binary-$ARCH/Packages"
SOURCES="$REPO/dists/$DIST/$COMPONENT/source/Sources"

test -s "$PACKAGES"
test -s "$SOURCES"
test -s "$REPO/dists/$DIST/Release"
test -s "$REPO/dists/$DIST/InRelease"
test -s "$REPO/dists/$DIST/Release.gpg"

binary_count="$(grep -c '^Package:' "$PACKAGES" || true)"
source_count="$(grep -c '^Package:' "$SOURCES" || true)"

echo
echo "========================================"
echo " R-Distro repository published"
echo "========================================"
echo
echo "Binary entries: $binary_count"
echo "Source entries: $source_count"
echo
echo "Metadata:"
ls -lh \
    "$REPO/dists/$DIST/Release" \
    "$REPO/dists/$DIST/InRelease" \
    "$REPO/dists/$DIST/Release.gpg"