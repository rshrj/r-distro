# R-Distro

R-Distro is an attempt to rebuild the Debian ARM64 base system from source
and find out how much of it can be built without Debian binaries at all.

The interesting question is not "can these packages be recompiled" — they
can — but where the circle closes. A compiler needs a compiler. `dpkg`
needs `dpkg`. This repository contains the tooling that pins the inputs,
computes that boundary precisely, rebuilds it, and publishes each pass as
an immutable signed archive that the next pass can build against.

Target: `arm64`, Debian **forky** (testing), snapshot **20260813T165000Z**.

## Frozen inputs

Everything here derives from three pinned things, and nothing resolves
"latest":

| Input | Value |
|---|---|
| Debian snapshot | `20260813T165000Z` |
| Bootstrap image | `debian@sha256:8fe7e20e…350d276` |
| Base source set | the 49 packages in `manifests/base-sources.txt` |

All three images disable `Check-Valid-Until`, so the snapshot keeps
resolving indefinitely rather than expiring out from under a long
campaign.

## Generations

A generation is a complete rebuild of a package set. Generation *N*
stamps every package it produces with a `+rdistroN` version suffix — high
enough to win against Debian's version, low enough to preserve upstream
ordering — and is promoted as an immutable release before the next
generation starts.

```
Gen1   rebuilt from Debian snapshot binaries          +rdistro1
Gen2   rebuilt with R-Distro binaries preferred       +rdistro2
Gen3   controller-driven rebuild of the boundary      +rdistro3
Gen4   the proof: Gen3 rebuilt using only Gen3        +rdistro4
```

Self-hosting is claimed only at the point where a generation can be
rebuilt entirely from the previous generation's own binaries. Everything
before Gen4 is scaffolding for that measurement.

## The self-hosting boundary

`scripts/analyze-selfhost-boundary.py` computes which packages must be
buildable for the ARM64 base to be self-hosting. From the 49 base
sources it expands `required`/`important`/`essential`/`build-essential`
binaries plus `Build-Depends`, `Build-Depends-Arch`, `Pre-Depends` and
`Depends`, under the `nocheck nodoc` profiles, and recurses until a full
round discovers nothing new.

It is a fixed point, not a truncated walk — the same snapshot always
yields the same answer. `Build-Depends-Indep`, `Recommends`, `Suggests`,
other architectures and Debian's cross-toolchain packages are excluded
deliberately: none of them are needed to rebuild the ARM64 base.

For the pinned snapshot the boundary closes after 14 rounds at **4725
source packages** and **7570 binary packages**, with zero unresolved
relations. See `manifests/selfhost-boundary-arm64.md`.

## Status

- **`gen3-canary`** — promoted. The 41-package canary in
  `manifests/selfhost-canary-arm64.txt` rebuilt clean: 1061 signed
  artifacts, including the full toolchain, the kernel, and the packaging
  tools that build everything else.
- **`gen3-bootstrap`** — in progress across the full 4725-source
  boundary.

## Layout

```
builder/        package-rebuild image, pinned to the snapshot
buildroot/      unprivileged build image; BASE_IMAGE is parameterised so
                later generations build inside an R-Distro base
controller/     orchestration image (sbuild, autopkgtest, docker-cli)

config/
  package-policy/   per-package build exceptions, as data

manifests/
  2026-08-13/       the pinned snapshot and bootstrap image digests
  base-sources.txt  the 49-source base set (curated)
  bootstrap-frontier-common.txt
                    52 packages that keep reappearing at the edge of the
                    closure (curated)
  selfhost-canary-arm64.txt
                    41-package fail-fast slice (curated)
  selfhost-boundary-arm64.md
                    the boundary result

scripts/
  build-package.sh              rebuild one source package
  build-base.sh                 early parallel driver over the base set
  publish.sh / publish-all.sh   early in-place APT publishing
  analyze-deps.py               what was actually installed per build
  analyze-bootstrap-closure.py  closure from observed build deps
  analyze-bootstrap-recursive.py
                                closure from declared metadata
  botch-*-safe.py               deb822 normalisation for botch
  filter-botch-universe.py      narrow the universe to R-Distro
  analyze-selfhost-boundary.py  the boundary fixed point
  rdistroctl.py                 campaign controller
  rdistro_repo.py               validate / stage / promote releases
  retry-fetch-failures.py       requeue transient snapshot fetch failures
```

## Rebuilding one package

```sh
docker build -t rdistro-buildroot:2026-08-13 buildroot/
scripts/build-package.sh hello 1
```

Artifacts land in `work/builds/hello`. Set `USE_RDISTRO=1` to build
against a running R-Distro archive instead of Debian binaries;
`RDISTRO_OUTPUT_DIR` overrides the output path.

The container always mounts `repo/keys/rdistro-archive.asc`, so export
your archive public key there first even for a Debian-only build.

Packages that need an exception declare it in
`config/package-policy/$PKG.env` rather than being special-cased in the
build script:

```sh
# config/package-policy/bash.env  — bash needs its documentation targets
DEB_BUILD_OPTIONS="nocheck"
DEB_BUILD_PROFILES="nocheck"

# config/package-policy/linux.env — the kernel generates its own control
PRE_BUILD_COMMAND='make -f debian/rules debian/control-real'
```

## Running a campaign

```sh
scripts/rdistroctl.py doctor
scripts/rdistroctl.py plan --campaign gen3-bootstrap --generation 3 \
                           --manifest manifests/selfhost-boundary-arm64.txt
scripts/rdistroctl.py run  --campaign gen3-bootstrap --parallel 4
scripts/rdistroctl.py status    --campaign gen3-bootstrap
scripts/rdistroctl.py dashboard --campaign gen3-bootstrap
```

State lives in SQLite. A campaign survives `pause`, `resume`, and the
controller process dying. Every build is an immutable attempt with its
own artifact directory, so a retry never destroys the evidence from the
attempt before it, and failures are classified from their logs into
`disk`, `oom`, `dependency`, `source-fetch`, `tests`, `configure` and
`docker` — which is what turns several hundred red jobs into a handful
of real problems.

## Publishing a release

```sh
scripts/rdistro_repo.py validate --campaign gen3-bootstrap \
                                 --manifest manifests/selfhost-canary-arm64.txt
scripts/rdistro_repo.py stage    --campaign gen3-bootstrap --release gen3-canary
scripts/rdistro_repo.py promote  --release gen3-canary
```

`validate` writes nothing; it checks that every source in the manifest
has a successful attempt carrying exactly one `.dsc` at the version the
generation implies. `stage` hardlinks artifacts, generates APT indices
and signs `Release`. `promote` renames staging into `repo/releases/` on
the same filesystem — an atomic rename, so a release is either absent or
complete, never half-published. Promoting over an existing release is
refused.

## What is not in this repository

Git holds source, configuration, policy, documentation and frozen input
definitions. It does not hold anything the tracked scripts can produce.
[`docs/GENERATED-ARTIFACTS.md`](docs/GENERATED-ARTIFACTS.md) traces every
generated artifact back to the command that produces it, and names the twenty
that no current script can produce any more.

| Untracked | Regenerate with |
|---|---|
| `work/` — build roots, campaign DB, fetched sources | `rdistroctl.py plan` + `run` |
| `repo/`, `repo-pass*/` — APT pools, indices, releases | `rdistro_repo.py stage` + `promote` |
| `analysis/` — dependency graphs, CSVs, DOT/SVG | the `analyze-*` scripts |
| `manifests/selfhost-boundary-arm64.txt` | `analyze-selfhost-boundary.py` |
| `logs/`, `out/` | any build |
| `keys/gnupg/` — archive signing key | `gpg --gen-key`, then re-sign |

The signing key is deliberately absent. Anyone reproducing this generates
their own and signs their own archive; a release is only as trustworthy
as the key that signed it, and that key should never be this one.
