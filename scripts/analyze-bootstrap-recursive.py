#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

BASE_MANIFEST = ROOT / "manifests" / "base-sources.txt"
FRONTIER_MANIFEST = ROOT / "manifests" / "bootstrap-frontier-common.txt"

CACHE = ROOT / "analysis" / "bootstrap" / "universe-cache"

DEFAULT_IMAGE = "rdistro-buildroot:2026-08-13"
DEFAULT_ARCH = "arm64"


# ======================================================================
# Progress display
# ======================================================================

def progress(label: str, current: int, total: int, width: int = 36):
    total = max(total, 1)
    ratio = min(max(current / total, 0.0), 1.0)

    done = int(width * ratio)
    bar = "#" * done + "-" * (width - done)

    sys.stdout.write(
        f"\r{label:<30} [{bar}] "
        f"{current:>6}/{total:<6} "
        f"{ratio * 100:6.1f}%"
    )
    sys.stdout.flush()

    if current >= total:
        print()


# ======================================================================
# Debian control-file parsing
# ======================================================================

def parse_paragraph(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current = None

    for raw in text.splitlines():
        if not raw:
            continue

        if raw[0].isspace() and current:
            fields[current] += "\n" + raw.strip()
            continue

        if ":" not in raw:
            continue

        key, value = raw.split(":", 1)
        current = key.strip()
        fields[current] = value.strip()

    return fields


def parse_universe(
    path: Path,
    label: str,
) -> list[dict[str, str]]:

    text = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    blocks = re.split(r"\n\s*\n", text)
    blocks = [x for x in blocks if x.strip()]

    result = []

    print()

    for i, block in enumerate(blocks, 1):
        record = parse_paragraph(block)

        if record:
            result.append(record)

        if i == 1 or i % 500 == 0 or i == len(blocks):
            progress(label, i, len(blocks))

    return result


# ======================================================================
# Manifest handling
# ======================================================================

def read_manifest(path: Path) -> list[str]:
    result = []

    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()

        if line:
            result.append(line)

    return result


# ======================================================================
# Get complete package metadata from the pinned Debian buildroot
# ======================================================================

def collect_universe(
    image: str,
    refresh: bool,
):
    CACHE.mkdir(
        parents=True,
        exist_ok=True,
    )

    binary_cache = CACHE / "binaries.raw"
    source_cache = CACHE / "sources.raw"

    if (
        not refresh
        and binary_cache.exists()
        and source_cache.exists()
        and binary_cache.stat().st_size > 0
        and source_cache.stat().st_size > 0
    ):
        print("Using cached complete Debian package universe.")
        return

    print()
    print("Collecting complete Debian package universe...")
    print()

    script = r'''
set -euo pipefail

# Never let the R-Distro repository influence dependency discovery.
rm -f /etc/apt/sources.list.d/rdistro.sources
rm -f /var/lib/apt/lists/*host.docker.internal* 2>/dev/null || true

echo "@@@STAGE|1|4|APT indices"

apt-get \
    -o Acquire::Retries=5 \
    update \
    --error-on=any \
    >/dev/null

echo "@@@STAGE|2|4|Binary package universe"

apt-cache dumpavail > /cache/binaries.raw

echo "@@@STAGE|3|4|Source package universe"

shopt -s nullglob
source_files=(/var/lib/apt/lists/*_Sources*)

if [ "${#source_files[@]}" -eq 0 ]; then
    echo "ERROR: no Debian Sources indices found." >&2
    echo "The buildroot must have deb-src enabled." >&2
    exit 1
fi

: > /cache/sources.raw

for f in "${source_files[@]}"; do
    /usr/lib/apt/apt-helper cat-file "$f" \
        >> /cache/sources.raw

    printf '\n' >> /cache/sources.raw
done

echo "@@@STAGE|4|4|Metadata complete"

{
    echo "Architecture: $(dpkg --print-architecture)"
    echo "Source index files: ${#source_files[@]}"
} > /cache/snapshot-info.txt
'''

    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
        "-v",
        f"{CACHE}:/cache",
        image,
        "bash",
        "-lc",
        script,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert proc.stdout is not None

    for line in proc.stdout:
        line = line.rstrip()

        if line.startswith("@@@STAGE|"):
            _, cur, total, message = line.split("|", 3)

            progress(
                message,
                int(cur),
                int(total),
            )
        elif line:
            print(line)

    rc = proc.wait()

    if rc != 0:
        raise SystemExit(
            f"Docker metadata collection failed with exit code {rc}"
        )

    if not binary_cache.exists() or not source_cache.exists():
        raise SystemExit(
            "Metadata collection completed but cache files are missing."
        )


# ======================================================================
# Complete source/binary indices
# ======================================================================

def build_source_index(
    records: list[dict[str, str]],
):
    sources: dict[str, dict[str, str]] = {}

    print()

    for i, record in enumerate(records, 1):
        package = record.get("Package")

        if package and package not in sources:
            sources[package] = record

        if i == 1 or i % 500 == 0 or i == len(records):
            progress(
                "Index source packages",
                i,
                len(records),
            )

    return sources


def parse_source_field(
    binary_record: dict[str, str],
):
    binary = binary_record.get("Package", "")
    version = binary_record.get("Version", "")

    raw = binary_record.get("Source", "").strip()

    if not raw:
        return binary, version

    m = re.match(
        r"([a-z0-9][a-z0-9+.-]*)"
        r"(?:\s+\(([^)]+)\))?",
        raw,
    )

    if not m:
        return binary, version

    return m.group(1), (m.group(2) or version)


def split_top_level(
    text: str,
    separator: str,
) -> list[str]:

    result = []
    buffer = []

    paren = 0
    bracket = 0
    angle = 0

    for ch in text:

        if ch == "(":
            paren += 1

        elif ch == ")":
            paren = max(paren - 1, 0)

        elif ch == "[":
            bracket += 1

        elif ch == "]":
            bracket = max(bracket - 1, 0)

        elif ch == "<":
            angle += 1

        elif ch == ">":
            angle = max(angle - 1, 0)

        if (
            ch == separator
            and paren == 0
            and bracket == 0
            and angle == 0
        ):
            value = "".join(buffer).strip()

            if value:
                result.append(value)

            buffer = []

        else:
            buffer.append(ch)

    value = "".join(buffer).strip()

    if value:
        result.append(value)

    return result


PACKAGE_RE = re.compile(
    r"^\s*([a-z0-9][a-z0-9+.-]*)"
    r"(?::[a-z0-9-]+)?"
)


def package_name(text: str):
    m = PACKAGE_RE.match(text)

    if m:
        return m.group(1)

    return None


def build_binary_index(
    records: list[dict[str, str]],
):
    binaries = {}
    virtual = defaultdict(set)
    source_versions = {}

    print()

    for i, record in enumerate(records, 1):

        binary = record.get("Package")

        if not binary:
            continue

        source, source_version = parse_source_field(
            record
        )

        if binary not in binaries:
            binaries[binary] = {
                "binary": binary,
                "version": record.get("Version", ""),
                "source": source,
                "source_version": source_version,
            }

        source_versions.setdefault(
            source,
            source_version,
        )

        provides = record.get("Provides", "")

        for item in split_top_level(
            provides,
            ",",
        ):
            name = package_name(item)

            if name:
                virtual[name].add(source)

        if i == 1 or i % 500 == 0 or i == len(records):
            progress(
                "Index binary packages",
                i,
                len(records),
            )

    return binaries, virtual, source_versions


# ======================================================================
# Architecture/profile handling
# ======================================================================

def arch_matches(
    pattern: str,
    arch: str,
):
    if pattern == "any":
        return True

    if pattern == arch:
        return True

    if pattern == "linux-any":
        return True

    if pattern == f"any-{arch}":
        return True

    return False


def architecture_active(
    relation: str,
    arch: str,
):
    groups = re.findall(
        r"\[([^\]]+)\]",
        relation,
    )

    if not groups:
        return True

    tokens = []

    for group in groups:
        tokens.extend(group.split())

    positive = [
        x
        for x in tokens
        if not x.startswith("!")
    ]

    negative = [
        x[1:]
        for x in tokens
        if x.startswith("!")
    ]

    if any(
        arch_matches(x, arch)
        for x in negative
    ):
        return False

    if positive:
        return any(
            arch_matches(x, arch)
            for x in positive
        )

    return True


def profiles_active(
    relation: str,
    profiles: set[str],
):
    groups = re.findall(
        r"<([^>]+)>",
        relation,
    )

    if not groups:
        return True

    # Groups are OR; entries within a group are AND.
    for group in groups:

        group_ok = True

        for item in group.split():

            if item.startswith("!"):
                if item[1:] in profiles:
                    group_ok = False
                    break

            else:
                if item not in profiles:
                    group_ok = False
                    break

        if group_ok:
            return True

    return False


# ======================================================================
# Dependency resolver
# ======================================================================

def make_resolver(
    binaries,
    virtual,
    source_versions,
):

    def resolve(binary: str):

        if binary in binaries:
            info = binaries[binary]

            return [
                {
                    "source": info["source"],
                    "source_version":
                        info["source_version"],
                    "resolution": "binary",
                }
            ]

        providers = sorted(
            virtual.get(binary, ())
        )

        if not providers:
            return []

        resolution = (
            "virtual"
            if len(providers) == 1
            else "virtual-ambiguous"
        )

        return [
            {
                "source": source,
                "source_version":
                    source_versions.get(source, ""),
                "resolution": resolution,
            }
            for source in providers
        ]

    return resolve


# ======================================================================
# Recursive dependency expansion
# ======================================================================

def dependency_fields(mode: str):

    if mode == "bootstrap":
        return (
            "Build-Depends",
            "Build-Depends-Arch",
        )

    return (
        "Build-Depends",
        "Build-Depends-Arch",
        "Build-Depends-Indep",
    )


def dependencies_for_source(
    consumer: str,
    record: dict[str, str],
    resolve,
    arch: str,
    profiles: set[str],
    mode: str,
):

    edges = []
    unresolved = []

    group_id = 0

    for field in dependency_fields(mode):

        value = record.get(field, "")

        if not value:
            continue

        groups = split_top_level(
            value,
            ",",
        )

        for group in groups:

            group_id += 1

            alternatives = split_top_level(
                group,
                "|",
            )

            active_alternatives = []

            for alt_index, alternative in enumerate(
                alternatives,
                1,
            ):
                if not architecture_active(
                    alternative,
                    arch,
                ):
                    continue

                if not profiles_active(
                    alternative,
                    profiles,
                ):
                    continue

                binary = package_name(
                    alternative
                )

                if not binary:
                    continue

                active_alternatives.append(
                    (
                        alt_index,
                        binary,
                        alternative,
                    )
                )

            if not active_alternatives:
                continue

            chosen = None
            providers = []

            # Debian alternatives are ordered. Choose the first
            # active alternative represented in the pinned package
            # universe.
            for alt_index, binary, raw in active_alternatives:

                resolved = resolve(binary)

                if resolved:
                    chosen = (
                        alt_index,
                        binary,
                        raw,
                    )
                    providers = resolved
                    break

            if chosen is None:
                unresolved.append(
                    {
                        "consumer_source":
                            consumer,
                        "field": field,
                        "group_id":
                            group_id,
                        "relation":
                            group,
                        "active_alternatives":
                            " | ".join(
                                x[1]
                                for x
                                in active_alternatives
                            ),
                    }
                )
                continue

            alt_index, binary, raw = chosen

            active_text = " | ".join(
                x[1]
                for x in active_alternatives
            )

            for provider in providers:

                edges.append(
                    {
                        "provider_source":
                            provider["source"],
                        "provider_version":
                            provider[
                                "source_version"
                            ],
                        "consumer_source":
                            consumer,
                        "binary_dependency":
                            binary,
                        "field":
                            field,
                        "group_id":
                            group_id,
                        "selected_alternative":
                            alt_index,
                        "active_alternatives":
                            active_text,
                        "raw_relation":
                            group,
                        "resolution":
                            provider["resolution"],
                    }
                )

    return edges, unresolved


def recursive_closure(
    core: list[str],
    source_index,
    resolve,
    arch: str,
    profiles: set[str],
    mode: str,
    max_depth: int | None,
):

    discovery_layer = {
        source: 0
        for source in core
    }

    expanded = set()
    edges = []
    unresolved = []
    unresolved_sources = set()

    frontier = set(core)
    depth = 0

    while frontier:

        if (
            max_depth is not None
            and depth > max_depth
        ):
            break

        frontier = {
            x
            for x in frontier
            if x not in expanded
        }

        if not frontier:
            break

        ordered = sorted(frontier)

        print()
        print(
            f"Dependency discovery layer {depth}: "
            f"{len(ordered)} source package(s)"
        )

        next_frontier = set()

        for i, consumer in enumerate(
            ordered,
            1,
        ):

            record = source_index.get(
                consumer
            )

            if record is None:
                unresolved_sources.add(
                    consumer
                )
                expanded.add(consumer)

                progress(
                    f"Expand layer {depth}",
                    i,
                    len(ordered),
                )
                continue

            new_edges, new_unresolved = (
                dependencies_for_source(
                    consumer,
                    record,
                    resolve,
                    arch,
                    profiles,
                    mode,
                )
            )

            edges.extend(new_edges)
            unresolved.extend(new_unresolved)

            for edge in new_edges:

                provider = edge[
                    "provider_source"
                ]

                if provider not in discovery_layer:
                    discovery_layer[
                        provider
                    ] = depth + 1

                    next_frontier.add(
                        provider
                    )

            expanded.add(
                consumer
            )

            if (
                i == 1
                or i % 25 == 0
                or i == len(ordered)
            ):
                progress(
                    f"Expand layer {depth}",
                    i,
                    len(ordered),
                )

        new_count = len(
            [
                x
                for x in next_frontier
                if x not in expanded
            ]
        )

        print(
            f"  New source packages discovered: "
            f"{new_count}"
        )

        if new_count == 0:
            print()
            print(
                "Fixed point reached: "
                "no unseen source dependencies remain."
            )
            break

        frontier = next_frontier
        depth += 1

    return (
        discovery_layer,
        edges,
        unresolved,
        unresolved_sources,
    )


# ======================================================================
# SCC computation
# ======================================================================

def tarjan_scc(
    nodes: set[str],
    edges,
):
    adjacency = defaultdict(set)

    for edge in edges:
        adjacency[
            edge["provider_source"]
        ].add(
            edge["consumer_source"]
        )

    index = 0
    stack = []
    on_stack = set()

    indices = {}
    lowlink = {}

    components = []

    def visit(v):
        nonlocal index

        indices[v] = index
        lowlink[v] = index
        index += 1

        stack.append(v)
        on_stack.add(v)

        for w in adjacency.get(
            v,
            (),
        ):
            if w not in indices:
                visit(w)

                lowlink[v] = min(
                    lowlink[v],
                    lowlink[w],
                )

            elif w in on_stack:
                lowlink[v] = min(
                    lowlink[v],
                    indices[w],
                )

        if lowlink[v] == indices[v]:

            component = []

            while True:
                w = stack.pop()
                on_stack.remove(w)
                component.append(w)

                if w == v:
                    break

            components.append(
                sorted(component)
            )

    print()

    ordered = sorted(nodes)

    for i, node in enumerate(
        ordered,
        1,
    ):
        if node not in indices:
            visit(node)

        if (
            i == 1
            or i % 100 == 0
            or i == len(ordered)
        ):
            progress(
                "Compute SCCs",
                i,
                len(ordered),
            )

    components.sort(
        key=lambda c: (
            -len(c),
            c[0],
        )
    )

    mapping = {}

    for i, component in enumerate(
        components
    ):
        scc_id = f"SCC{i:04d}"

        for node in component:
            mapping[node] = scc_id

    return components, mapping


def condensation_graph(
    components,
    mapping,
    edges,
):
    dag = defaultdict(set)

    indegree = {
        f"SCC{i:04d}": 0
        for i in range(len(components))
    }

    edge_counts = defaultdict(int)

    for edge in edges:

        a = mapping[
            edge["provider_source"]
        ]

        b = mapping[
            edge["consumer_source"]
        ]

        if a == b:
            continue

        edge_counts[(a, b)] += 1

        if b not in dag[a]:
            dag[a].add(b)
            indegree[b] += 1

    queue = deque(
        sorted(
            x
            for x, degree
            in indegree.items()
            if degree == 0
        )
    )

    level = {
        x: 0
        for x in indegree
    }

    while queue:

        u = queue.popleft()

        for v in sorted(
            dag.get(u, ())
        ):
            level[v] = max(
                level[v],
                level[u] + 1,
            )

            indegree[v] -= 1

            if indegree[v] == 0:
                queue.append(v)

    return dag, level, edge_counts


# ======================================================================
# Output helpers
# ======================================================================

def write_csv(
    path: Path,
    rows,
    fields,
):
    with path.open(
        "w",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    field:
                        row.get(field, "")
                    for field in fields
                }
            )


def dotq(value):
    return json.dumps(
        str(value)
    )


STATE_COLORS = {
    "rdistro-base-gen2": "#b7e4c7",
    "initial-frontier": "#ffd6a5",
    "recursive-debian": "#ffadad",
}


def generate_full_dot(
    path: Path,
    node_rows,
    edges,
):
    node_map = {
        r["source"]: r
        for r in node_rows
    }

    aggregated = defaultdict(set)

    for edge in edges:
        aggregated[
            (
                edge["provider_source"],
                edge["consumer_source"],
            )
        ].add(
            edge["binary_dependency"]
        )

    with path.open("w") as f:

        f.write("digraph G {\n")
        f.write(
            'graph [overlap=false, '
            'splines=true, bgcolor="white"];\n'
        )

        f.write(
            'node [shape=box, '
            'style="rounded,filled", '
            'fontname="Helvetica", '
            'fontsize=8];\n'
        )

        f.write(
            'edge [color="#8a94a3", '
            'arrowsize=0.5];\n'
        )

        for source, row in sorted(
            node_map.items()
        ):
            fill = STATE_COLORS[
                row["state"]
            ]

            label = (
                f"{source}\\n"
                f"D{row['discovery_layer']} "
                f"· B{row['build_level']}"
            )

            border = (
                "#6a3d9a"
                if row["cyclic"] == 1
                else "#566573"
            )

            penwidth = (
                2.5
                if row["cyclic"] == 1
                else 1.0
            )

            f.write(
                f"{dotq(source)} ["
                f"label={dotq(label)}, "
                f"fillcolor={dotq(fill)}, "
                f"color={dotq(border)}, "
                f"penwidth={penwidth}"
                f"];\n"
            )

        for (
            provider,
            consumer,
        ), binaries in sorted(
            aggregated.items()
        ):
            n = len(binaries)

            width = min(
                4.0,
                0.8 + math.log2(n + 1),
            )

            f.write(
                f"{dotq(provider)} -> "
                f"{dotq(consumer)} "
                f"[penwidth={width:.2f}];\n"
            )

        f.write("}\n")


def generate_scc_dot(
    path: Path,
    components,
    levels,
    edge_counts,
    discovery_layer,
    base_set,
    frontier_set,
):
    component_map = {
        f"SCC{i:04d}": component
        for i, component
        in enumerate(components)
    }

    with path.open("w") as f:

        f.write("digraph SCC {\n")

        f.write(
            'graph [rankdir=LR, '
            'overlap=false, '
            'splines=true, '
            'bgcolor="white", '
            'ranksep=1.0];\n'
        )

        f.write(
            'node [shape=box, '
            'style="rounded,filled", '
            'fontname="Helvetica", '
            'fontsize=9];\n'
        )

        f.write(
            'edge [color="#73808c", '
            'arrowsize=0.7];\n'
        )

        for scc_id, members in component_map.items():

            build_level = levels[
                scc_id
            ]

            cyclic = len(members) > 1

            if all(
                m in base_set
                for m in members
            ):
                fill = STATE_COLORS[
                    "rdistro-base-gen2"
                ]

            elif all(
                m in frontier_set
                for m in members
            ):
                fill = STATE_COLORS[
                    "initial-frontier"
                ]

            else:
                fill = "#e2d5f9" if cyclic else "#ffe5d9"

            max_discovery = max(
                discovery_layer.get(m, 0)
                for m in members
            )

            if len(members) <= 7:
                body = "\\n".join(
                    members
                )
            else:
                body = (
                    "\\n".join(
                        members[:6]
                    )
                    + f"\\n... +{len(members) - 6}"
                )

            label = (
                f"{scc_id} · B{build_level} "
                f"· D≤{max_discovery}\\n"
                f"{body}"
            )

            border = (
                "#6a3d9a"
                if cyclic
                else "#59636e"
            )

            penwidth = (
                3.0
                if cyclic
                else 1.0
            )

            f.write(
                f"{dotq(scc_id)} ["
                f"label={dotq(label)}, "
                f"fillcolor={dotq(fill)}, "
                f"color={dotq(border)}, "
                f"penwidth={penwidth}"
                f"];\n"
            )

        for (
            a,
            b,
        ), count in sorted(
            edge_counts.items()
        ):
            width = min(
                5.0,
                1.0 + math.log2(
                    count + 1
                ),
            )

            label = (
                str(count)
                if count > 1
                else ""
            )

            f.write(
                f"{dotq(a)} -> {dotq(b)} ["
                f"penwidth={width:.2f}, "
                f"label={dotq(label)}"
                f"];\n"
            )

        f.write("}\n")


def render_graphs(
    closure_dot: Path,
    scc_dot: Path,
):
    dot = shutil.which("dot")
    sfdp = shutil.which("sfdp")

    if not dot:
        print()
        print(
            "Graphviz not installed; "
            "DOT files were generated."
        )
        print(
            "Install with: brew install graphviz"
        )
        return

    print()
    print("Rendering Graphviz visualizations...")

    renderer = sfdp or dot

    progress(
        "Render closure graph",
        0,
        1,
    )

    subprocess.run(
        [
            renderer,
            "-Tsvg",
            str(closure_dot),
            "-o",
            str(
                closure_dot.with_suffix(
                    ".svg"
                )
            ),
        ],
        check=True,
    )

    progress(
        "Render closure graph",
        1,
        1,
    )

    progress(
        "Render SCC graph",
        0,
        1,
    )

    subprocess.run(
        [
            dot,
            "-Tsvg",
            str(scc_dot),
            "-o",
            str(
                scc_dot.with_suffix(
                    ".svg"
                )
            ),
        ],
        check=True,
    )

    progress(
        "Render SCC graph",
        1,
        1,
    )


# ======================================================================
# Main
# ======================================================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=(
            "bootstrap",
            "full",
        ),
        default="bootstrap",
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
    )

    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
    )

    parser.add_argument(
        "--arch",
        default=DEFAULT_ARCH,
    )

    parser.add_argument(
        "--profile",
        action="append",
        default=[],
    )

    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--no-render",
        action="store_true",
    )

    args = parser.parse_args()

    if args.mode == "bootstrap":
        profiles = {
            "nocheck",
            "nodoc",
            *args.profile,
        }

        out = (
            ROOT
            / "analysis"
            / "bootstrap"
            / "recursive"
        )

    else:
        profiles = set(
            args.profile
        )

        out = (
            ROOT
            / "analysis"
            / "bootstrap"
            / "recursive-full"
        )

    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    base = read_manifest(
        BASE_MANIFEST
    )

    frontier = read_manifest(
        FRONTIER_MANIFEST
    )

    overlap = set(base) & set(frontier)

    if overlap:
        raise SystemExit(
            "Base/frontier overlap:\n  "
            + "\n  ".join(
                sorted(overlap)
            )
        )

    core = base + frontier

    print(
        f"R-Distro base sources:      {len(base)}"
    )
    print(
        f"Initial frontier sources:   {len(frontier)}"
    )
    print(
        f"Initial core:               {len(core)}"
    )
    print(
        f"Mode:                       {args.mode}"
    )
    print(
        f"Profiles:                   "
        f"{', '.join(sorted(profiles)) or '(none)'}"
    )

    collect_universe(
        args.image,
        args.refresh,
    )

    source_records = parse_universe(
        CACHE / "sources.raw",
        "Parse source universe",
    )

    binary_records = parse_universe(
        CACHE / "binaries.raw",
        "Parse binary universe",
    )

    source_index = build_source_index(
        source_records
    )

    (
        binaries,
        virtual,
        source_versions,
    ) = build_binary_index(
        binary_records
    )

    print()
    print(
        f"Debian source universe:     "
        f"{len(source_index)}"
    )
    print(
        f"Debian binary universe:     "
        f"{len(binaries)}"
    )
    print(
        f"Virtual package names:      "
        f"{len(virtual)}"
    )

    resolve = make_resolver(
        binaries,
        virtual,
        source_versions,
    )

    (
        discovery_layer,
        edges,
        unresolved,
        unresolved_sources,
    ) = recursive_closure(
        core,
        source_index,
        resolve,
        args.arch,
        profiles,
        args.mode,
        args.max_depth,
    )

    nodes = set(
        discovery_layer
    )

    print()
    print(
        f"Recursive closure:          "
        f"{len(nodes)} source packages"
    )
    print(
        f"Dependency edges:           "
        f"{len(edges)}"
    )

    components, mapping = tarjan_scc(
        nodes,
        edges,
    )

    (
        dag,
        build_levels,
        condensation_counts,
    ) = condensation_graph(
        components,
        mapping,
        edges,
    )

    self_edges = {
        edge["provider_source"]
        for edge in edges
        if (
            edge["provider_source"]
            == edge["consumer_source"]
        )
    }

    cyclic_sccs = set()

    for i, component in enumerate(
        components
    ):
        scc_id = f"SCC{i:04d}"

        if (
            len(component) > 1
            or any(
                x in self_edges
                for x in component
            )
        ):
            cyclic_sccs.add(
                scc_id
            )

    base_set = set(base)
    frontier_set = set(frontier)

    provider_count = defaultdict(set)
    consumer_count = defaultdict(set)

    for edge in edges:
        provider_count[
            edge["consumer_source"]
        ].add(
            edge["provider_source"]
        )

        consumer_count[
            edge["provider_source"]
        ].add(
            edge["consumer_source"]
        )

    node_rows = []

    for source in sorted(nodes):

        if source in base_set:
            state = "rdistro-base-gen2"

        elif source in frontier_set:
            state = "initial-frontier"

        else:
            state = "recursive-debian"

        scc_id = mapping[source]

        node_rows.append(
            {
                "source":
                    source,
                "state":
                    state,
                "discovery_layer":
                    discovery_layer[source],
                "scc_id":
                    scc_id,
                "cyclic":
                    int(
                        scc_id
                        in cyclic_sccs
                    ),
                "build_level":
                    build_levels[scc_id],
                "provider_count":
                    len(
                        provider_count[
                            source
                        ]
                    ),
                "consumer_count":
                    len(
                        consumer_count[
                            source
                        ]
                    ),
                "version":
                    source_index
                    .get(source, {})
                    .get("Version", ""),
            }
        )

    scc_rows = []

    for i, members in enumerate(
        components
    ):
        scc_id = f"SCC{i:04d}"

        scc_rows.append(
            {
                "scc_id":
                    scc_id,
                "build_level":
                    build_levels[scc_id],
                "size":
                    len(members),
                "cyclic":
                    int(
                        scc_id
                        in cyclic_sccs
                    ),
                "members":
                    ", ".join(
                        members
                    ),
            }
        )

    scc_rows.sort(
        key=lambda r: (
            -int(r["size"]),
            r["scc_id"],
        )
    )

    discovery_rows = sorted(
        (
            {
                "source":
                    source,
                "discovery_layer":
                    layer,
            }
            for source, layer
            in discovery_layer.items()
        ),
        key=lambda r: (
            int(
                r["discovery_layer"]
            ),
            r["source"],
        ),
    )

    build_rows = sorted(
        (
            {
                "source":
                    row["source"],
                "build_level":
                    row["build_level"],
                "scc_id":
                    row["scc_id"],
                "cyclic":
                    row["cyclic"],
                "state":
                    row["state"],
            }
            for row in node_rows
        ),
        key=lambda r: (
            int(r["build_level"]),
            r["source"],
        ),
    )

    condensation_rows = []

    for (
        provider_scc,
        consumer_scc,
    ), count in sorted(
        condensation_counts.items()
    ):
        condensation_rows.append(
            {
                "provider_scc":
                    provider_scc,
                "consumer_scc":
                    consumer_scc,
                "dependency_edge_count":
                    count,
            }
        )

    write_csv(
        out / "nodes.csv",
        node_rows,
        [
            "source",
            "state",
            "discovery_layer",
            "scc_id",
            "cyclic",
            "build_level",
            "provider_count",
            "consumer_count",
            "version",
        ],
    )

    write_csv(
        out / "edges.csv",
        edges,
        [
            "provider_source",
            "provider_version",
            "consumer_source",
            "binary_dependency",
            "field",
            "group_id",
            "selected_alternative",
            "active_alternatives",
            "raw_relation",
            "resolution",
        ],
    )

    write_csv(
        out / "discovery-layers.csv",
        discovery_rows,
        [
            "source",
            "discovery_layer",
        ],
    )

    write_csv(
        out / "strongly-connected-components.csv",
        scc_rows,
        [
            "scc_id",
            "build_level",
            "size",
            "cyclic",
            "members",
        ],
    )

    write_csv(
        out / "build-levels.csv",
        build_rows,
        [
            "source",
            "build_level",
            "scc_id",
            "cyclic",
            "state",
        ],
    )

    write_csv(
        out / "condensation-edges.csv",
        condensation_rows,
        [
            "provider_scc",
            "consumer_scc",
            "dependency_edge_count",
        ],
    )

    write_csv(
        out / "unresolved-dependencies.csv",
        unresolved,
        [
            "consumer_source",
            "field",
            "group_id",
            "relation",
            "active_alternatives",
        ],
    )

    write_csv(
        out / "unresolved-sources.csv",
        [
            {
                "source": x
            }
            for x
            in sorted(
                unresolved_sources
            )
        ],
        [
            "source",
        ],
    )

    # Create a per-discovery-depth source manifest.
    layer_dir = (
        out
        / "discovery-manifests"
    )

    layer_dir.mkdir(
        exist_ok=True,
    )

    by_discovery = defaultdict(
        list
    )

    for source, layer in discovery_layer.items():
        by_discovery[layer].append(
            source
        )

    for layer, sources in sorted(
        by_discovery.items()
    ):
        (
            layer_dir
            / f"depth-{layer:02d}.txt"
        ).write_text(
            "\n".join(
                sorted(sources)
            )
            + "\n"
        )

    data = {
        "mode":
            args.mode,
        "profiles":
            sorted(profiles),
        "architecture":
            args.arch,
        "counts": {
            "base_sources":
                len(base),
            "initial_frontier":
                len(frontier),
            "recursive_closure":
                len(nodes),
            "dependency_edges":
                len(edges),
            "scc_count":
                len(components),
            "cyclic_scc_count":
                len(cyclic_sccs),
            "unresolved_dependency_groups":
                len(unresolved),
            "unresolved_sources":
                len(
                    unresolved_sources
                ),
            "max_discovery_depth":
                max(
                    discovery_layer.values(),
                    default=0,
                ),
            "max_build_level":
                max(
                    build_levels.values(),
                    default=0,
                ),
        },
        "nodes":
            node_rows,
        "edges":
            edges,
        "sccs":
            scc_rows,
        "condensation_edges":
            condensation_rows,
    }

    (
        out
        / "dependency-data.json"
    ).write_text(
        json.dumps(
            data,
            indent=2,
        )
    )

    closure_dot = (
        out
        / "bootstrap-closure.dot"
    )

    scc_dot = (
        out
        / "bootstrap-scc.dot"
    )

    generate_full_dot(
        closure_dot,
        node_rows,
        edges,
    )

    generate_scc_dot(
        scc_dot,
        components,
        build_levels,
        condensation_counts,
        discovery_layer,
        base_set,
        frontier_set,
    )

    if not args.no_render:
        render_graphs(
            closure_dot,
            scc_dot,
        )

    largest_cycles = [
        row
        for row in scc_rows
        if row["cyclic"] == 1
    ]

    summary = [
        "# R-Distro Recursive Bootstrap Analysis",
        "",
        f"- Mode: **{args.mode}**",
        f"- Profiles: **{', '.join(sorted(profiles)) or 'none'}**",
        f"- Initial R-Distro sources: **{len(base)}**",
        f"- Initial frontier: **{len(frontier)}**",
        f"- Complete recursive closure: **{len(nodes)}**",
        f"- Dependency edges: **{len(edges)}**",
        f"- Discovery depth: **{max(discovery_layer.values(), default=0)}**",
        f"- SCCs: **{len(components)}**",
        f"- Cyclic SCCs: **{len(cyclic_sccs)}**",
        f"- Unresolved dependency groups: **{len(unresolved)}**",
        f"- Missing source records: **{len(unresolved_sources)}**",
        "",
        "## Largest cyclic SCCs",
        "",
    ]

    if largest_cycles:

        for row in largest_cycles[:10]:
            summary.append(
                f"- **{row['scc_id']}** "
                f"({row['size']} sources, "
                f"build level {row['build_level']}): "
                f"{row['members']}"
            )

    else:
        summary.append(
            "No cyclic SCCs."
        )

    summary.extend(
        [
            "",
            "## Discovery layers",
            "",
        ]
    )

    for layer in sorted(
        by_discovery
    ):
        summary.append(
            f"- Depth {layer}: "
            f"**{len(by_discovery[layer])}** sources"
        )

    (
        out
        / "summary.md"
    ).write_text(
        "\n".join(summary)
        + "\n"
    )

    print()
    print(
        "========================================"
    )
    print(
        " R-Distro recursive bootstrap closure"
    )
    print(
        "========================================"
    )
    print()

    print(
        f"Initial core:                  {len(core)}"
    )
    print(
        f"Complete recursive closure:    {len(nodes)}"
    )
    print(
        f"Dependency edges:              {len(edges)}"
    )
    print(
        f"Discovery depth:               "
        f"{max(discovery_layer.values(), default=0)}"
    )
    print(
        f"SCCs:                          {len(components)}"
    )
    print(
        f"Cyclic SCCs:                   {len(cyclic_sccs)}"
    )
    print(
        f"Unresolved dependency groups:  {len(unresolved)}"
    )
    print(
        f"Unresolved source packages:    {len(unresolved_sources)}"
    )

    print()
    print("Discovery layers:")

    for layer in sorted(
        by_discovery
    ):
        print(
            f"  depth {layer:2d}: "
            f"{len(by_discovery[layer]):4d} source packages"
        )

    print()
    print("Largest cyclic SCCs:")

    if largest_cycles:

        for row in largest_cycles[:5]:
            members = row[
                "members"
            ].split(", ")

            preview = ", ".join(
                members[:8]
            )

            if len(members) > 8:
                preview += (
                    f", ... +{len(members) - 8}"
                )

            print(
                f"  {row['scc_id']} "
                f"size={row['size']} "
                f"level={row['build_level']}: "
                f"{preview}"
            )

    else:
        print("  none")

    print()
    print(f"Output: {out}")

    print()
    print("Key files:")
    print(
        f"  {out / 'summary.md'}"
    )
    print(
        f"  {out / 'nodes.csv'}"
    )
    print(
        f"  {out / 'edges.csv'}"
    )
    print(
        f"  {out / 'discovery-layers.csv'}"
    )
    print(
        f"  {out / 'strongly-connected-components.csv'}"
    )
    print(
        f"  {out / 'build-levels.csv'}"
    )

    if not args.no_render:
        print(
            f"  {out / 'bootstrap-closure.svg'}"
        )
        print(
            f"  {out / 'bootstrap-scc.svg'}"
        )


if __name__ == "__main__":
    main()
