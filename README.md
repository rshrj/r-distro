<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
    <img alt="R-Distro — rebuilding the Debian ARM64 base system from source, and measuring where self-hosting closes" src="docs/assets/banner-light.svg" width="100%">
  </picture>
</p>

<p align="center">
  <a href="https://rshrj.github.io/r-distro/"><img alt="Documentation" src="https://img.shields.io/badge/docs-artifact%20provenance-A0472A?style=flat-square"></a>
  <img alt="Debian forky at snapshot 20260813T165000Z" src="https://img.shields.io/badge/debian-forky%20%40%2020260813T165000Z-A81D33?style=flat-square&logo=debian&logoColor=white">
  <img alt="Architecture arm64" src="https://img.shields.io/badge/arch-arm64-5C6673?style=flat-square">
  <img alt="Self-hosting boundary: 4725 sources" src="https://img.shields.io/badge/self--hosting%20boundary-4725%20sources-2C6A4C?style=flat-square">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-3B6EA5?style=flat-square"></a>
  <img alt="Last commit" src="https://img.shields.io/github/last-commit/rshrj/r-distro?style=flat-square&color=5C6673">
</p>

<p align="center">
  <b><a href="https://rshrj.github.io/r-distro/">Artifact Provenance Reference&nbsp;→</a></b>
</p>

---

R-Distro rebuilds the Debian **forky** ARM64 base system from source and finds out how
much of it can be built without Debian binaries at all.

The interesting question is not *can these packages be recompiled* — they can — but
**where the circle closes**. A compiler needs a compiler. `dpkg` needs `dpkg`. This
repository contains the tooling that pins the inputs, computes that boundary precisely,
rebuilds it, and publishes each pass as an immutable signed archive that the next pass
builds against.

## Frozen inputs

Everything derives from three pinned things. Nothing resolves "latest".

| Input | Value |
|---|---|
| Debian snapshot | `20260813T165000Z` |
| Bootstrap image | `debian@sha256:8fe7e20e…350d276` |
| Base source set | the 49 packages in [`manifests/base-sources.txt`](manifests/base-sources.txt) |

All three container images disable `Check-Valid-Until`, so the pinned snapshot keeps
resolving indefinitely rather than expiring out from under a multi-day campaign.

## Generations

A generation is a complete rebuild of a package set. Generation *N* stamps every package
it produces with a `+rdistroN` version suffix — high enough to win against Debian's
version, low enough to preserve upstream ordering — and is promoted as an immutable
signed release before the next generation starts.

| | Built against | Version suffix |
|---|---|---|
| **Gen 1** | Debian snapshot binaries | `+rdistro1` |
| **Gen 2** | R-Distro binaries preferred | `+rdistro2` |
| **Gen 3** | controller-driven rebuild of the boundary | `+rdistro3` |
| **Gen 4** | *the proof* — Gen 3 rebuilt using only Gen 3 | `+rdistro4` |

Self-hosting is claimed only where a generation can be rebuilt entirely from the previous
generation's own binaries. Everything before Gen 4 is scaffolding for that measurement.

## The self-hosting boundary

[`scripts/analyze-selfhost-boundary.py`](scripts/analyze-selfhost-boundary.py) computes
which packages must be buildable for the ARM64 base to be self-hosting. From the 49 base
sources it expands `required` / `important` / `essential` / `build-essential` binaries
plus `Build-Depends`, `Build-Depends-Arch`, `Pre-Depends` and `Depends`, under the
`nocheck nodoc` profiles, and recurses until a full round discovers nothing new.

It is a **fixed point**, not a truncated walk — the same snapshot always yields the same
answer. `Build-Depends-Indep`, `Recommends`, `Suggests`, other architectures and Debian's
cross-toolchain packages are excluded deliberately: none are needed to rebuild the ARM64
base.

| Sources | Binaries | Rounds | Unresolved | Status |
|--:|--:|--:|--:|:--|
| 4725 | 7570 | 14 | 0 | **VALID** |

Full result: [`manifests/selfhost-boundary-arm64.md`](manifests/selfhost-boundary-arm64.md)

## Status

- **`gen3-canary`** — promoted. The 41-package canary in
  [`manifests/selfhost-canary-arm64.txt`](manifests/selfhost-canary-arm64.txt) rebuilt
  clean: 1061 signed artifacts, including the full toolchain, the kernel, and the
  packaging tools that build everything else.
- **`gen3-bootstrap`** — in progress across the full 4725-source boundary.

## Quick start

```sh
# Build the images (pinned to the snapshot)
docker build -t rdistro-buildroot:2026-08-13 buildroot/

# Generate an archive signing key
mkdir -p keys/gnupg repo/keys && chmod 700 keys/gnupg
GNUPGHOME=keys/gnupg gpg --gen-key
GNUPGHOME=keys/gnupg gpg --armor --export archive@rdistro.local \
    > repo/keys/rdistro-archive.asc

# Rebuild one package
scripts/build-package.sh hello 1
```

Artifacts land in `work/builds/hello`. Set `USE_RDISTRO=1` to build against a running
R-Distro archive instead of Debian binaries; `RDISTRO_OUTPUT_DIR` overrides the output
path.

Packages needing an exception declare it in `config/package-policy/$PKG.env` rather than
being special-cased in the build script:

```sh
# config/package-policy/bash.env  — bash needs its documentation targets
DEB_BUILD_OPTIONS="nocheck"
DEB_BUILD_PROFILES="nocheck"

# config/package-policy/linux.env — the kernel generates its own control file
PRE_BUILD_COMMAND='make -f debian/rules debian/control-real'
```

### Running a campaign

```sh
scripts/rdistroctl.py doctor --manifest manifests/selfhost-boundary-arm64.txt
scripts/rdistroctl.py plan   --campaign gen3-bootstrap --generation 3 \
                             --manifest manifests/selfhost-boundary-arm64.txt
scripts/rdistroctl.py run    --campaign gen3-bootstrap --parallel 4
scripts/rdistroctl.py status --campaign gen3-bootstrap
```

State lives in SQLite. A campaign survives `pause`, `resume`, and the controller process
dying. Every build is an immutable attempt with its own artifact directory, so a retry
never destroys the evidence from the attempt before it, and failures are classified from
their logs — which is what turns several hundred red jobs into a handful of real problems.

### Publishing a release

```sh
scripts/rdistro_repo.py validate --campaign gen3-bootstrap \
                                 --manifest manifests/selfhost-canary-arm64.txt
scripts/rdistro_repo.py stage    --campaign gen3-bootstrap \
                                 --manifest manifests/selfhost-canary-arm64.txt \
                                 --release gen3-canary
scripts/rdistro_repo.py promote  --release gen3-canary
```

`validate` writes nothing; it checks that every source in the manifest has a successful
attempt carrying exactly one `.dsc` at the version the generation implies. `stage`
hardlinks artifacts, generates APT indices and signs `Release`. `promote` renames staging
into `repo/releases/` on the same filesystem — an atomic rename, so a release is either
absent or complete, never half-published.

## Documentation

**[Artifact Provenance Reference →](https://rshrj.github.io/r-distro/)**

Every generated artifact traced back to the exact command that produces it, stage by
stage, plus a from-scratch runbook and the twenty artifacts on disk that **no current
script can produce any more**. Also available as
[Markdown](docs/GENERATED-ARTIFACTS.md).

## Layout

```
builder/        package-rebuild image, pinned to the snapshot
buildroot/      unprivileged build image; BASE_IMAGE is parameterised so
                later generations build inside an R-Distro base
controller/     orchestration image (sbuild, autopkgtest, docker-cli)

config/package-policy/    per-package build exceptions, as data

manifests/
  2026-08-13/             pinned snapshot and bootstrap image digests
  base-sources.txt        the 49-source base set (curated)
  bootstrap-frontier-common.txt
                          52 packages at the edge of the closure (curated)
  selfhost-canary-arm64.txt
                          41-package fail-fast slice (curated)
  selfhost-boundary-arm64.md
                          the boundary result

scripts/
  build-package.sh              rebuild one source package
  build-base.sh                 early parallel driver over the base set
  publish.sh / publish-all.sh   signed development archive
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

## What is not in this repository

Git holds source, configuration, policy, documentation and frozen input definitions —
29 files, about 380 KB. It does not hold anything the tracked scripts can produce, which
is roughly **31 GB** of build roots, APT pools, analysis dumps and logs.

| Untracked | Regenerate with |
|---|---|
| `work/` — build roots, campaign DB, fetched sources | `rdistroctl.py plan` + `run` |
| `repo/`, `repo-pass*/` — APT pools, indices, releases | `rdistro_repo.py stage` + `promote` |
| `analysis/` — dependency graphs, CSVs, DOT/SVG | the `analyze-*` scripts |
| `manifests/selfhost-boundary-arm64.txt` | `analyze-selfhost-boundary.py` |
| `logs/`, `out/` | any build |
| `keys/gnupg/` — archive signing key | `gpg --gen-key`, then re-sign |

The signing key is deliberately absent. Anyone reproducing this generates their own and
signs their own archive; a release is only as trustworthy as the key that signed it, and
that key should never be this one.

> [!NOTE]
> Not every file on disk is reproducible. Twenty categories of artifact exist that no
> current script produces — earlier script versions wrote them, or a human ran the command
> by hand. They are catalogued in the
> [provenance reference](https://rshrj.github.io/r-distro/#stranded) rather than quietly
> implied reproducible.

## License

[MIT](LICENSE) © Rishi Raj
