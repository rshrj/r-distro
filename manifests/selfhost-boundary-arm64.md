# R-Distro ARM64 Self-Hosting Boundary

Status: **VALID**

## Definition

R-Distro's ARM64 base system is considered self-hosting when every binary
inside this boundary can be produced from a source package inside the same
boundary and every source package can be architecture-built using only
binaries inside the same boundary.

### Included

- The 49 source packages in `manifests/base-sources.txt`.
- Binary packages with Priority `required` or `important`.
- Binary packages marked `Essential: yes`.
- Binary packages marked `Build-Essential: yes`.
- `Build-Depends`.
- `Build-Depends-Arch`.
- `Pre-Depends` and `Depends` of every required binary.
- ARM64 and Architecture: all packages.
- Build profiles: `nocheck nodoc`.

### Excluded

- `Build-Depends-Indep`.
- `Recommends`.
- `Suggests`.
- `Enhances`.
- Other architectures.
- Dependencies removed by the `nocheck` or `nodoc` profiles.
- Debian cross-compilers and foreign-architecture sysroot packages
  (for example `gcc-*-cross`, `cross-toolchain-base*`, and `*-cross`
  binary packages).

### Fixed-point stopping rule

Recursion stops only when processing all currently required sources and
binaries discovers no new required source or binary package.

## Result

- Boundary sources: **4725**
- Boundary binaries: **7570**
- Build dependency edges: **29669**
- Runtime dependency edges: **32278**
- Source SCCs: **15**
- Cyclic SCCs: **1**
- Largest SCC: **4711**
- Closure rounds: **14**
- Unresolved relations: **0**
- Ambiguous virtual choices: **187**
- Source-version mismatches: **0**

## Interpretation

`boundary-source-names.txt` is the canonical R-Distro self-hosting source
boundary for this snapshot and architecture.

This is not the full Debian archive and it is not the closure required to
build every architecture-independent/documentation package. It is the
explicit boundary chosen for rebuilding the ARM64 R-Distro base system.
