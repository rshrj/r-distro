#!/usr/bin/env bash
set -euo pipefail

PKG="${1:?usage: $0 PACKAGE}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/work/builds/$PKG"
REPO="$ROOT/repo"

test -d "$OUT"

mkdir -p "$REPO/pool/main/r/rdistro"
mkdir -p "$REPO/dists/development/main/binary-arm64"
mkdir -p "$REPO/dists/development/main/source"

cp "$OUT"/*.deb "$REPO/pool/main/r/rdistro/" 2>/dev/null || true
cp "$OUT"/*.dsc "$REPO/pool/main/r/rdistro/" 2>/dev/null || true
cp "$OUT"/*.orig.tar.* "$REPO/pool/main/r/rdistro/" 2>/dev/null || true
cp "$OUT"/*.debian.tar.* "$REPO/pool/main/r/rdistro/" 2>/dev/null || true
cp "$OUT"/*.diff.gz "$REPO/pool/main/r/rdistro/" 2>/dev/null || true

docker run --rm \
  -v "$REPO:/repo" \
  r-distro-builder:2026-08-13 \
  bash -lc '
    set -eux
    cd /repo

    apt-ftparchive packages pool/main \
      > dists/development/main/binary-arm64/Packages

    gzip -9c dists/development/main/binary-arm64/Packages \
      > dists/development/main/binary-arm64/Packages.gz

    apt-ftparchive sources pool/main \
      > dists/development/main/source/Sources

    gzip -9c dists/development/main/source/Sources \
      > dists/development/main/source/Sources.gz

    rm -f \
      dists/development/InRelease \
      dists/development/Release \
      dists/development/Release.gpg

    apt-ftparchive \
      -o APT::FTPArchive::Release::Origin="R-Distro" \
      -o APT::FTPArchive::Release::Label="R-Distro" \
      -o APT::FTPArchive::Release::Suite="development" \
      -o APT::FTPArchive::Release::Codename="development" \
      -o APT::FTPArchive::Release::Architectures="arm64" \
      -o APT::FTPArchive::Release::Components="main" \
      release dists/development \
      > dists/development/Release
  '

docker run --rm \
  -v "$ROOT/keys/gnupg:/keysrc:ro" \
  -v "$REPO:/repo" \
  r-distro-builder:2026-08-13 \
  bash -lc '
    set -eux

    export GNUPGHOME=/tmp/gnupg
    mkdir -m 700 "$GNUPGHOME"

    cp -a /keysrc/pubring.kbx "$GNUPGHOME/"
    cp -a /keysrc/private-keys-v1.d "$GNUPGHOME/"
    [ ! -f /keysrc/trustdb.gpg ] || cp /keysrc/trustdb.gpg "$GNUPGHOME/"

    chmod -R go-rwx "$GNUPGHOME"

    gpg \
      --batch --yes \
      --local-user archive@rdistro.local \
      --clearsign \
      -o /repo/dists/development/InRelease \
      /repo/dists/development/Release

    gpg \
      --batch --yes \
      --local-user archive@rdistro.local \
      --armor --detach-sign \
      -o /repo/dists/development/Release.gpg \
      /repo/dists/development/Release
  '

echo "Published $PKG"
