# Building a native package end to end

This walks the full round trip for an R-Distro native package: build it in the
pinned container, inspect the result, publish it to a local development
archive, and install it from that archive to confirm the distribution identity
took effect.

It uses `rdistro-minimal` as the worked example. The same five steps apply to
any package under `packages/`.

This is the manual path. It is worth doing once, because it is the sequence
that `rdistroctl` automates, and having run it by hand makes the controller's
failure categories legible.

> **Prerequisites.** The `rdistro-buildroot:2026-08-13` image must exist (see
> [Stage 1](GENERATED-ARTIFACTS.md#stage-1--container-images)), and step 5
> requires the archive to be served on `:8080` (see
> [Serving the archive](GENERATED-ARTIFACTS.md#serving-the-archive)).

---

## Step 1 — Build the package

Native packages have no Debian counterpart to fetch, so there is no
`apt-get source` step. The source tree is copied into the container from
`packages/<pkg>` and built in place.

The build runs as an unprivileged `builder` user, and the output is chowned
back to the invoking user on the way out so the artifacts are not left owned
by root.

```bash
PKG=rdistro-minimal
OUT="$PWD/work/local-builds/$PKG/clean-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$OUT"

docker run --rm \
  --platform linux/arm64 \
  -e PKG="$PKG" \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -v "$PWD/packages/$PKG:/input:ro" \
  -v "$OUT:/output" \
  rdistro-buildroot:2026-08-13 \
  bash -lc '
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export DEB_BUILD_OPTIONS=nocheck
export DEB_BUILD_PROFILES=nocheck

apt-get \
    -o Acquire::Retries=5 \
    update \
    --error-on=any

apt-get install -y --no-install-recommends \
    build-essential \
    devscripts \
    equivs \
    fakeroot

rm -rf /build/work
mkdir -p /build/work/src

cp -a /input/. /build/work/src/

mkdir -p /tmp/rdistro-builddeps
cd /tmp/rdistro-builddeps

mk-build-deps \
    --install \
    --remove \
    --tool "apt-get -y --no-install-recommends" \
    /build/work/src/debian/control

chown -R builder:builder /build/work

cd /build/work/src

runuser -u builder -- \
    env \
      HOME=/home/builder \
      DEB_BUILD_OPTIONS="$DEB_BUILD_OPTIONS" \
      DEB_BUILD_PROFILES="$DEB_BUILD_PROFILES" \
      dpkg-buildpackage -us -uc

cp -a /build/work/rdistro-minimal_* /output/

chown -R "$HOST_UID:$HOST_GID" /output
'

echo "$OUT"
```

The source is mounted read-only and copied before building, so a failed build
cannot leave debhelper output in the tracked `packages/` tree. Anything that
does end up there is ignored — see the `packages/*/debian/` rules in
`.gitignore`.

## Step 2 — Inspect the result

Check the contents and the control metadata before publishing anything. For a
metapackage the interesting part is the dependency list; for
`rdistro-archive-keyring` it is that the key landed in `/usr/share/keyrings`.

```bash
docker run --rm \
  --platform linux/arm64 \
  -v "$OUT:/artifacts:ro" \
  rdistro-buildroot:2026-08-13 \
  bash -lc '
set -e

ls -lh /artifacts

dpkg-deb -c /artifacts/rdistro-minimal_0.1_arm64.deb
echo
dpkg-deb -I /artifacts/rdistro-minimal_0.1_arm64.deb
'
```

## Step 3 — Stage into the development archive

`repo/dev/` is a scratch archive, separate from the promoted releases under
`repo/`. It exists so a package can be installed and tested before it is
promoted anywhere.

```bash
DEST="$PWD/repo/dev/pool/main/r/rdistro/rdistro-minimal"

mkdir -p "$DEST"

cp "$OUT"/rdistro-minimal_* "$DEST/"
```

## Step 4 — Regenerate and sign the archive metadata

Copying a `.deb` into the pool does not make it installable; the indices have
to be regenerated and the `Release` file re-signed. This is the same
`rdistro_repo` code path the release pipeline uses, called directly against
the development stage rather than through `rdistroctl`.

```bash
python3 - <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, "scripts")
import rdistro_repo

rdistro_repo.generate_repository_metadata(
    stage_root=Path("repo/dev").resolve(),
    suite="development",
    buildroot_image="rdistro-buildroot:2026-08-13",
    private_gnupg=Path("keys/gnupg"),
    sign=True,
)
PY
```

Signing happens inside the container with the private keyring bind-mounted,
so the key material never has to be readable by anything on the host but
`gpg`.

## Step 5 — Install it and verify the identity

The real test. Configure APT against the development archive, trusting only
the R-Distro archive key, and install `rdistro-release`. If the vendor
definition, the origins symlink and the keyring all landed correctly, the
final `dpkg-vendor` calls answer.

```bash
docker run --rm \
  --platform linux/arm64 \
  -v "$PWD/repo/keys/rdistro-archive.asc:/etc/apt/keyrings/rdistro-bootstrap.asc:ro" \
  rdistro-buildroot:2026-08-13 \
  bash -lc '
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

cat >/etc/apt/sources.list.d/rdistro-dev.sources <<EOF
Types: deb
URIs: http://host.docker.internal:8080/dev/
Suites: development
Components: main
Signed-By: /etc/apt/keyrings/rdistro-bootstrap.asc
EOF

apt-get update

echo
echo "=== APT candidates ==="
apt-cache policy \
    rdistro-archive-keyring \
    rdistro-release

echo
echo "=== Install ==="
apt-get install -y rdistro-release

echo
echo "=== Installed release metadata ==="
cat /usr/lib/rdistro/release

echo
echo "=== dpkg origins ==="
cat /etc/dpkg/origins/rdistro
printf "default -> "
readlink /etc/dpkg/origins/default

echo
echo "=== APT template ==="
cat /usr/share/rdistro/apt/rdistro.sources.in

echo
echo "=== Archive key ==="
test -f /usr/share/keyrings/rdistro-archive-keyring.asc
gpg --show-keys \
    /usr/share/keyrings/rdistro-archive-keyring.asc

echo
echo "=== dpkg vendor ==="
dpkg-vendor --query Vendor
dpkg-vendor --derives-from Debian

echo
echo "ALL R-DISTRO RELEASE TESTS PASSED"
'
```

`apt-get update` succeeding is itself a check: it means the `Release` file
signature verified against the key mounted at
`/etc/apt/keyrings/rdistro-bootstrap.asc`. A signing failure in step 4 shows
up here rather than silently producing an unauthenticated archive.

The last two commands are the ones that matter. `dpkg-vendor --query Vendor`
returning `R-Distro` means the origins symlink was relinked by the postinst,
and `--derives-from Debian` succeeding means the parent relationship is
declared — which is what lets Debian's own packaging tooling behave correctly
on an R-Distro system.

---

## What this does not cover

This is the native package path. Rebuilt Debian sources go through
`scripts/build-package.sh` and the campaign controller instead — see
[Stage 3](GENERATED-ARTIFACTS.md#stage-3--building-packages) and
[Stage 7](GENERATED-ARTIFACTS.md#stage-7--campaigns).

Publishing here targets `repo/dev/`, which is deliberately outside the
promotion path. Nothing in this walkthrough produces a release; see
[Stage 8](GENERATED-ARTIFACTS.md#stage-8--releases) for that.
