#!/usr/bin/env python3

from pathlib import Path
from collections import defaultdict, Counter
import csv
import json
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
BUILDS = ROOT / "work" / "builds"
MANIFEST = ROOT / "manifests" / "base-sources.txt"
OUT = ROOT / "analysis" / "pass2"

OUT.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
# Debian RFC822-ish control parser
# ----------------------------------------------------------------------

def parse_control(path):
    fields = {}
    current = None

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")

            if not line:
                continue

            if line[0].isspace() and current:
                fields[current] += "\n" + line.strip()
                continue

            if ":" in line:
                key, value = line.split(":", 1)
                current = key
                fields[key] = value.strip()

    return fields


def dependency_atoms(text):
    """
    Extract binary package names from Debian dependency expressions.

    Example:
        foo (>= 1.0) | bar:any [arm64] <!nocheck>
    ->
        foo, bar
    """

    if not text:
        return []

    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\[[^]]*\]", "", text)
    text = re.sub(r"<[^>]*>", "", text)

    result = []

    for group in text.split(","):
        for alt in group.split("|"):
            alt = alt.strip()

            m = re.match(
                r"([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?",
                alt
            )

            if m:
                result.append(m.group(1))

    return result


def installed_dependencies(text):
    """
    Parse Installed-Build-Depends from .buildinfo.

    Returns:
        [(binary_package, version), ...]
    """

    if not text:
        return []

    result = []

    for item in text.split(","):
        item = item.strip()

        m = re.match(
            r"([a-z0-9][a-z0-9+.-]*)(?::[a-z0-9-]+)?"
            r"\s*\(=\s*([^)]+)\)",
            item,
        )

        if m:
            result.append((m.group(1), m.group(2).strip()))

    return result


# ----------------------------------------------------------------------
# Load base sources
# ----------------------------------------------------------------------

base_sources = [
    x.strip()
    for x in MANIFEST.read_text().splitlines()
    if x.strip()
]

base_set = set(base_sources)


# ----------------------------------------------------------------------
# Parse Gen-2 DSC / buildinfo
# ----------------------------------------------------------------------

packages = {}

for source in base_sources:
    d = BUILDS / source

    dsc_candidates = list(d.glob("*+rdistro2.dsc"))

    if len(dsc_candidates) != 1:
        print(
            f"WARNING: {source}: expected one +rdistro2.dsc, "
            f"found {len(dsc_candidates)}"
        )
        continue

    dsc = dsc_candidates[0]
    dsc_fields = parse_control(dsc)

    buildinfo_candidates = list(d.glob("*.buildinfo"))

    buildinfo = None
    bi_fields = {}

    for candidate in buildinfo_candidates:
        x = parse_control(candidate)

        if "+rdistro2" in x.get("Version", ""):
            buildinfo = candidate
            bi_fields = x
            break

    packages[source] = {
        "dsc": dsc,
        "dsc_fields": dsc_fields,
        "buildinfo": buildinfo,
        "buildinfo_fields": bi_fields,
    }


# ----------------------------------------------------------------------
# Build binary -> source mapping
# ----------------------------------------------------------------------

binary_to_source = {}

for source, info in packages.items():
    binaries = info["dsc_fields"].get("Binary", "")

    for binary in binaries.split(","):
        binary = binary.strip()

        if binary:
            binary_to_source[binary] = source


# ----------------------------------------------------------------------
# Declared Build-Depends graph
# ----------------------------------------------------------------------

declared_edges = defaultdict(set)

for consumer, info in packages.items():
    fields = info["dsc_fields"]

    deps = []

    for key in (
        "Build-Depends",
        "Build-Depends-Arch",
        "Build-Depends-Indep",
    ):
        deps.extend(dependency_atoms(fields.get(key, "")))

    for binary in deps:
        provider = binary_to_source.get(binary)

        if provider and provider != consumer:
            declared_edges[(provider, consumer)].add(binary)


# ----------------------------------------------------------------------
# Actual Gen1 -> Gen2 provenance
# ----------------------------------------------------------------------

provenance_edges = defaultdict(set)

installed_rows = []
package_summary = []

fallback_counter = Counter()
fallback_consumers = defaultdict(set)

for consumer, info in packages.items():
    bi = info["buildinfo_fields"]

    installed = installed_dependencies(
        bi.get("Installed-Build-Depends", "")
    )

    rdistro1_count = 0
    debian_count = 0
    rdistro_sources = set()

    for binary, version in installed:

        provider = binary_to_source.get(binary, "")

        if "+rdistro1" in version:
            origin = "rdistro1"
            rdistro1_count += 1

            if provider:
                rdistro_sources.add(provider)

                if provider != consumer:
                    provenance_edges[(provider, consumer)].add(binary)

        elif "+rdistro" in version:
            origin = "other-rdistro"

        else:
            origin = "debian"
            debian_count += 1
            fallback_counter[binary] += 1
            fallback_consumers[binary].add(consumer)

        installed_rows.append({
            "consumer_source": consumer,
            "binary_package": binary,
            "version": version,
            "origin": origin,
            "provider_source": provider,
        })

    total = len(installed)

    package_summary.append({
        "source": consumer,
        "version": bi.get(
            "Version",
            info["dsc_fields"].get("Version", "")
        ),
        "installed_build_deps": total,
        "rdistro1_build_deps": rdistro1_count,
        "debian_build_deps": debian_count,
        "rdistro_percent":
            round(100 * rdistro1_count / total, 2)
            if total else 0,
        "rdistro_source_dependencies": len(rdistro_sources),
    })


# ----------------------------------------------------------------------
# Write CSV data
# ----------------------------------------------------------------------

def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


write_csv(
    OUT / "package-summary.csv",
    package_summary,
    [
        "source",
        "version",
        "installed_build_deps",
        "rdistro1_build_deps",
        "debian_build_deps",
        "rdistro_percent",
        "rdistro_source_dependencies",
    ],
)


write_csv(
    OUT / "installed-build-deps.csv",
    installed_rows,
    [
        "consumer_source",
        "binary_package",
        "version",
        "origin",
        "provider_source",
    ],
)


declared_rows = []

for (provider, consumer), binaries in sorted(declared_edges.items()):
    declared_rows.append({
        "provider_source": provider,
        "consumer_source": consumer,
        "binary_dependencies": ", ".join(sorted(binaries)),
    })

write_csv(
    OUT / "declared-base-edges.csv",
    declared_rows,
    [
        "provider_source",
        "consumer_source",
        "binary_dependencies",
    ],
)


provenance_rows = []

for (provider, consumer), binaries in sorted(provenance_edges.items()):
    provenance_rows.append({
        "gen1_source": provider,
        "gen2_consumer": consumer,
        "binary_packages": ", ".join(sorted(binaries)),
    })

write_csv(
    OUT / "gen1-to-gen2-edges.csv",
    provenance_rows,
    [
        "gen1_source",
        "gen2_consumer",
        "binary_packages",
    ],
)


fallback_rows = []

for binary, count in fallback_counter.most_common():
    fallback_rows.append({
        "binary_package": binary,
        "consumer_count": len(fallback_consumers[binary]),
        "installed_count": count,
        "consumers": ", ".join(sorted(fallback_consumers[binary])),
    })

write_csv(
    OUT / "debian-fallback-ranking.csv",
    fallback_rows,
    [
        "binary_package",
        "consumer_count",
        "installed_count",
        "consumers",
    ],
)


# ----------------------------------------------------------------------
# Graphviz: declared graph
# ----------------------------------------------------------------------

def q(x):
    return '"' + x.replace('"', '\\"') + '"'


def write_dot(path, edges, title):
    with open(path, "w") as f:
        f.write("digraph G {\n")
        f.write('  graph [rankdir=LR, overlap=false, splines=true];\n')
        f.write(
            '  node [shape=box, style="rounded,filled", '
            'fillcolor="#eef4ff", color="#7890b0", '
            'fontname="Helvetica"];\n'
        )
        f.write(
            '  edge [color="#7c8799", '
            'fontname="Helvetica", fontsize=8];\n'
        )
        f.write(f"  label={q(title)};\n")
        f.write("  labelloc=t;\n")
        f.write("  fontsize=20;\n\n")

        for source in base_sources:
            f.write(f"  {q(source)};\n")

        for (provider, consumer), binaries in sorted(edges.items()):
            label = ", ".join(sorted(binaries))

            f.write(
                f"  {q(provider)} -> {q(consumer)} "
                f"[label={q(label)}];\n"
            )

        f.write("}\n")


write_dot(
    OUT / "declared-base.dot",
    declared_edges,
    "R-Distro base source Build-Depends graph",
)

write_dot(
    OUT / "gen1-to-gen2.dot",
    provenance_edges,
    "Actual R-Distro Gen-1 → Gen-2 build provenance",
)


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------

json_data = {
    "packages": package_summary,
    "declared_edges": declared_rows,
    "provenance_edges": provenance_rows,
    "debian_fallback": fallback_rows,
}

with open(OUT / "dependency-data.json", "w") as f:
    json.dump(json_data, f, indent=2)


# ----------------------------------------------------------------------
# Render SVG if graphviz exists
# ----------------------------------------------------------------------

dot = shutil.which("dot")

if dot:
    for name in ("declared-base", "gen1-to-gen2"):
        subprocess.run(
            [
                dot,
                "-Tsvg",
                str(OUT / f"{name}.dot"),
                "-o",
                str(OUT / f"{name}.svg"),
            ],
            check=True,
        )
else:
    print()
    print("Graphviz not installed; DOT files were still generated.")
    print("Install with: brew install graphviz")


# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------

total_rdistro = sum(x["rdistro1_build_deps"] for x in package_summary)
total_debian = sum(x["debian_build_deps"] for x in package_summary)

print()
print("========================================")
print(" R-Distro Pass-2 dependency analysis")
print("========================================")
print()
print(f"Base sources analysed:       {len(package_summary)}")
print(f"Declared base edges:         {len(declared_edges)}")
print(f"Actual Gen1 -> Gen2 edges:   {len(provenance_edges)}")
print(f"R-Distro build deps:         {total_rdistro}")
print(f"Debian fallback build deps:  {total_debian}")

if total_rdistro + total_debian:
    pct = 100 * total_rdistro / (total_rdistro + total_debian)
    print(f"R-Distro environment share:  {pct:.2f}%")

print()
print("Top Debian fallback binaries:")

for row in fallback_rows[:20]:
    print(
        f"  {row['consumer_count']:2d} packages  "
        f"{row['binary_package']}"
    )

print()
print(f"Output: {OUT}")
