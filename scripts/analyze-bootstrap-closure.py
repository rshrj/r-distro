#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
from collections import defaultdict, deque
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

MANIFESTS = ROOT / "manifests"
BASE_MANIFEST = MANIFESTS / "base-sources.txt"
FRONTIER_MANIFEST = MANIFESTS / "bootstrap-frontier-common.txt"

OUT = ROOT / "analysis" / "bootstrap" / "reduced"
CACHE = OUT / "cache"

PASS2_INSTALLED = (
    ROOT / "analysis" / "pass2" / "installed-build-deps.csv"
)

DEFAULT_IMAGE = "rdistro-buildroot:2026-08-13"
DEFAULT_ARCH = "arm64"


# ----------------------------------------------------------------------
# Basic helpers
# ----------------------------------------------------------------------

def read_manifest(path: Path) -> list[str]:
    result = []

    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()

        if line:
            result.append(line)

    return result


def parse_deb822(text: str) -> list[dict[str, str]]:
    paragraphs = []

    current: dict[str, str] = {}
    key = None

    for raw in text.splitlines():
        if not raw.strip():
            if current:
                paragraphs.append(current)
                current = {}
                key = None
            continue

        if raw[0].isspace() and key is not None:
            current[key] += "\n" + raw.strip()
            continue

        if ":" not in raw:
            continue

        key, value = raw.split(":", 1)
        key = key.strip()
        current[key] = value.strip()

    if current:
        paragraphs.append(current)

    return paragraphs


def split_top_level(text: str, separator: str) -> list[str]:
    """
    Split Debian dependency syntax on commas/pipes while ignoring separators
    inside (), [], and <>.
    """

    result = []
    buf = []

    paren = 0
    bracket = 0
    angle = 0

    for ch in text:
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)

        if (
            ch == separator
            and paren == 0
            and bracket == 0
            and angle == 0
        ):
            value = "".join(buf).strip()

            if value:
                result.append(value)

            buf = []
        else:
            buf.append(ch)

    value = "".join(buf).strip()

    if value:
        result.append(value)

    return result


PACKAGE_RE = re.compile(
    r"^\s*([a-z0-9][a-z0-9+.-]*)"
    r"(?::([a-z0-9-]+))?"
)


def dependency_name(text: str) -> str | None:
    m = PACKAGE_RE.match(text)

    return m.group(1) if m else None


# ----------------------------------------------------------------------
# Architecture/profile restriction handling
# ----------------------------------------------------------------------

def arch_pattern_matches(
    pattern: str,
    arch: str,
    os_name: str = "linux",
) -> bool:
    """
    Covers the architecture patterns normally encountered here:
      arm64
      any
      linux-any
      any-arm64
    """

    if pattern == "any":
        return True

    if pattern == arch:
        return True

    if pattern == f"{os_name}-any":
        return True

    if pattern == f"any-{arch}":
        return True

    if pattern.endswith("-any"):
        return pattern[:-4] == os_name

    if pattern.startswith("any-"):
        return pattern[4:] == arch

    return False


def architecture_active(text: str, arch: str) -> bool:
    restrictions = re.findall(r"\[([^\]]+)\]", text)

    if not restrictions:
        return True

    # Debian relations normally contain one [] group.
    tokens = []

    for restriction in restrictions:
        tokens.extend(restriction.split())

    positives = [x for x in tokens if not x.startswith("!")]
    negatives = [x[1:] for x in tokens if x.startswith("!")]

    if any(arch_pattern_matches(x, arch) for x in negatives):
        return False

    if positives:
        return any(
            arch_pattern_matches(x, arch)
            for x in positives
        )

    return True


def profiles_active(
    text: str,
    active_profiles: set[str],
) -> bool:
    """
    Debian profile restrictions are disjunctions of conjunctions:

      <stage1 !nocheck> <cross>

    With no DEB_BUILD_PROFILES set, positive profile requirements are false
    and negative ones are true.
    """

    groups = re.findall(r"<([^>]+)>", text)

    if not groups:
        return True

    for group in groups:
        ok = True

        for token in group.split():
            if token.startswith("!"):
                if token[1:] in active_profiles:
                    ok = False
                    break
            else:
                if token not in active_profiles:
                    ok = False
                    break

        if ok:
            return True

    return False


# ----------------------------------------------------------------------
# Docker metadata collection
# ----------------------------------------------------------------------

def collect_snapshot_metadata(
    image: str,
    refresh: bool,
) -> None:

    CACHE.mkdir(parents=True, exist_ok=True)

    source_cache = CACHE / "sources.raw"
    binary_cache = CACHE / "binaries.raw"

    if (
        not refresh
        and source_cache.exists()
        and binary_cache.exists()
        and source_cache.stat().st_size > 0
        and binary_cache.stat().st_size > 0
    ):
        print("Using cached Debian metadata.")
        return

    print("Collecting metadata from pinned Debian buildroot...")

    script = r'''
set -euo pipefail

# This analysis must see Debian only, not the R-Distro repository.
rm -f /etc/apt/sources.list.d/rdistro.sources

apt-get \
    -o Acquire::Retries=5 \
    update \
    --error-on=any \
    >/dev/null

{
    cat /manifests/base-sources.txt
    cat /manifests/bootstrap-frontier-common.txt
} |
    sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' |
    sort -u \
    > /tmp/rdistro-analysis-sources

: > /out/sources.raw

while IFS= read -r src; do
    printf '@@@SOURCE %s\n' "$src" >> /out/sources.raw

    apt-cache showsrc "$src" \
        >> /out/sources.raw

    printf '\n@@@END %s\n' "$src" \
        >> /out/sources.raw
done < /tmp/rdistro-analysis-sources

apt-cache dumpavail > /out/binaries.raw

{
    echo "Architecture:"
    dpkg --print-architecture
    echo
    echo "APT sources:"
    grep -Rh \
        -v '^[[:space:]]*#' \
        /etc/apt/sources.list \
        /etc/apt/sources.list.d \
        2>/dev/null || true
} > /out/snapshot-info.txt
'''

    cmd = [
        "docker", "run", "--rm",
        "--platform", "linux/arm64",
        "-v", f"{MANIFESTS}:/manifests:ro",
        "-v", f"{CACHE}:/out",
        image,
        "bash", "-lc", script,
    ]

    subprocess.run(cmd, check=True)

    if not source_cache.exists() or not binary_cache.exists():
        raise RuntimeError("Docker metadata collection did not produce output.")


# ----------------------------------------------------------------------
# Parse source metadata cache
# ----------------------------------------------------------------------

def load_source_records() -> dict[str, dict[str, str]]:
    text = (CACHE / "sources.raw").read_text(
        errors="replace"
    )

    blocks: dict[str, list[str]] = {}

    current = None
    buffer: list[str] = []

    for line in text.splitlines(keepends=True):
        if line.startswith("@@@SOURCE "):
            current = line[len("@@@SOURCE "):].strip()
            buffer = []
            continue

        if line.startswith("@@@END "):
            if current is not None:
                blocks[current] = buffer[:]

            current = None
            buffer = []
            continue

        if current is not None:
            buffer.append(line)

    result = {}

    for source, lines in blocks.items():
        paragraphs = parse_deb822("".join(lines))

        exact = [
            p for p in paragraphs
            if p.get("Package") == source
        ]

        if not exact:
            raise RuntimeError(
                f"No Debian source record found for {source}"
            )

        # With our pinned single-suite snapshot this should normally be
        # exactly one candidate.
        result[source] = exact[0]

    return result


# ----------------------------------------------------------------------
# Parse binary package universe
# ----------------------------------------------------------------------

def source_from_binary_record(
    record: dict[str, str],
) -> tuple[str, str]:
    binary = record["Package"]

    raw_source = record.get("Source", "").strip()

    if raw_source:
        m = re.match(
            r"([a-z0-9][a-z0-9+.-]*)"
            r"(?:\s+\(([^)]+)\))?",
            raw_source,
        )

        if m:
            source = m.group(1)
            source_version = (
                m.group(2)
                or record.get("Version", "")
            )

            return source, source_version

    return binary, record.get("Version", "")


def load_binary_records():
    paragraphs = parse_deb822(
        (CACHE / "binaries.raw").read_text(
            errors="replace"
        )
    )

    binaries: dict[str, dict] = {}
    virtual_providers: dict[str, set[str]] = defaultdict(set)
    source_versions: dict[str, str] = {}

    for record in paragraphs:
        binary = record.get("Package")

        if not binary:
            continue

        source, source_version = source_from_binary_record(
            record
        )

        # dumpavail generally presents the candidate package.
        # Keep the first record if duplicates happen to occur.
        if binary not in binaries:
            binaries[binary] = {
                "package": binary,
                "version": record.get("Version", ""),
                "source": source,
                "source_version": source_version,
                "architecture": record.get(
                    "Architecture", ""
                ),
            }

        source_versions.setdefault(
            source,
            source_version,
        )

        provides = record.get("Provides", "")

        for part in split_top_level(provides, ","):
            name = dependency_name(part)

            if name:
                virtual_providers[name].add(source)

    return binaries, virtual_providers, source_versions


# ----------------------------------------------------------------------
# Resolve binary package -> source package
# ----------------------------------------------------------------------

def make_binary_resolver(
    binaries,
    virtual_providers,
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
            virtual_providers.get(binary, set())
        )

        if providers:
            kind = (
                "virtual"
                if len(providers) == 1
                else "virtual-ambiguous"
            )

            return [
                {
                    "source": source,
                    "source_version":
                        source_versions.get(source, ""),
                    "resolution": kind,
                }
                for source in providers
            ]

        return []

    return resolve


# ----------------------------------------------------------------------
# Dependency graph construction
# ----------------------------------------------------------------------

DEPENDENCY_FIELDS = (
    "Build-Depends",
    "Build-Depends-Arch",
    "Build-Depends-Indep",
)


def special_class(source: str) -> str:
    if source in {"gcc-15", "gcc-16", "gcc-defaults"}:
        return "compiler-toolchain"

    if source == "binutils":
        return "binary-toolchain"

    if source == "linux":
        return "kernel-source"

    if source == "build-essential":
        return "meta-build-environment"

    if source in {
        "autoconf",
        "automake",
        "autotools-dev",
        "debhelper",
        "dh-autoreconf",
        "gettext",
        "libtool",
        "m4",
        "make-dfsg",
        "patch",
        "po-debconf",
    }:
        return "build-tool"

    return ""


def build_edges(
    core_sources: list[str],
    source_records,
    resolve_binary,
    arch: str,
    active_profiles: set[str],
    dependency_fields
):
    edges = []
    unresolved = []

    for consumer in core_sources:
        record = source_records[consumer]

        relations = []

        for field in dependency_fields:
            value = record.get(field, "")

            if value:
                relations.append((field, value))

        # Debian source packages implicitly assume the standard
        # build-essential environment. Represent that explicitly as a
        # synthetic dependency so the graph does not pretend it vanishes.
        # if consumer != "build-essential":
        #     relations.append(
        #         (
        #             "Implicit-Build-Essential",
        #             "build-essential",
        #         )
        #     )

        group_id = 0

        for field, relation_text in relations:
            groups = split_top_level(
                relation_text,
                ",",
            )

            for group in groups:
                group_id += 1

                alternatives = split_top_level(
                    group,
                    "|",
                )

                active = []

                for alt_index, alt in enumerate(
                    alternatives,
                    start=1,
                ):
                    if not architecture_active(
                        alt,
                        arch,
                    ):
                        continue

                    if not profiles_active(
                        alt,
                        active_profiles,
                    ):
                        continue

                    binary = dependency_name(alt)

                    if not binary:
                        continue

                    active.append(
                        {
                            "binary": binary,
                            "raw": alt,
                            "alternative_index":
                                alt_index,
                        }
                    )

                # Whole group may be disabled by architecture/profile.
                if not active:
                    continue

                selected = None
                providers = []

                # Debian alternatives are ordered. For the graph we choose
                # the first active alternative present in the pinned
                # package universe.
                for alt in active:
                    resolved = resolve_binary(
                        alt["binary"]
                    )

                    if resolved:
                        selected = alt
                        providers = resolved
                        break

                if selected is None:
                    unresolved.append(
                        {
                            "consumer_source":
                                consumer,
                            "field": field,
                            "group_id": group_id,
                            "relation": group,
                            "active_alternatives":
                                " | ".join(
                                    x["binary"]
                                    for x in active
                                ),
                        }
                    )
                    continue

                alternative_names = " | ".join(
                    x["binary"]
                    for x in active
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
                                selected["binary"],
                            "field": field,
                            "group_id": group_id,
                            "selected_alternative":
                                selected[
                                    "alternative_index"
                                ],
                            "active_alternatives":
                                alternative_names,
                            "raw_relation":
                                group,
                            "resolution":
                                provider["resolution"],
                        }
                    )

    return edges, unresolved


# ----------------------------------------------------------------------
# Pass-2 actual environment fanout
# ----------------------------------------------------------------------

def load_pass2_environment(resolve_binary):
    consumer_sets = defaultdict(set)
    install_counts = defaultdict(int)

    if not PASS2_INSTALLED.exists():
        return consumer_sets, install_counts

    with PASS2_INSTALLED.open() as f:
        for row in csv.DictReader(f):
            binary = row["binary_package"]
            consumer = row["consumer_source"]

            providers = resolve_binary(binary)

            for provider in providers:
                source = provider["source"]

                consumer_sets[source].add(consumer)
                install_counts[source] += 1

    return consumer_sets, install_counts


# ----------------------------------------------------------------------
# SCCs / condensation DAG
# ----------------------------------------------------------------------

def tarjan_scc(nodes: set[str], edges):
    adjacency = defaultdict(set)

    for edge in edges:
        adjacency[edge["provider_source"]].add(
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

        for w in adjacency.get(v, ()):
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

    for node in sorted(nodes):
        if node not in indices:
            visit(node)

    components.sort(
        key=lambda x: x[0]
    )

    mapping = {}

    for i, component in enumerate(components):
        scc_id = f"SCC{i:03d}"

        for node in component:
            mapping[node] = scc_id

    return components, mapping


def condensation_levels(
    components,
    scc_mapping,
    edges,
):
    scc_nodes = {
        f"SCC{i:03d}"
        for i in range(len(components))
    }

    dag = defaultdict(set)
    indegree = {
        node: 0
        for node in scc_nodes
    }

    for edge in edges:
        a = scc_mapping[
            edge["provider_source"]
        ]
        b = scc_mapping[
            edge["consumer_source"]
        ]

        if a == b:
            continue

        if b not in dag[a]:
            dag[a].add(b)
            indegree[b] += 1

    queue = deque(
        sorted(
            node
            for node, degree in indegree.items()
            if degree == 0
        )
    )

    level = {
        node: 0
        for node in scc_nodes
    }

    while queue:
        u = queue.popleft()

        for v in sorted(dag.get(u, ())):
            level[v] = max(
                level[v],
                level[u] + 1,
            )

            indegree[v] -= 1

            if indegree[v] == 0:
                queue.append(v)

    return level, dag


# ----------------------------------------------------------------------
# CSV/JSON output
# ----------------------------------------------------------------------

def write_csv(
    path: Path,
    rows: list[dict],
    fields: list[str],
):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    key: row.get(key, "")
                    for key in fields
                }
            )


# ----------------------------------------------------------------------
# Graphviz
# ----------------------------------------------------------------------

STATE_STYLE = {
    "rdistro-base-gen2": {
        "fill": "#b8e6c1",
        "border": "#237a3b",
        "short": "R-Distro G2",
    },
    "debian-frontier": {
        "fill": "#ffd89c",
        "border": "#b06a00",
        "short": "Frontier",
    },
    "external-debian": {
        "fill": "#f4b7b7",
        "border": "#a63030",
        "short": "External Debian",
    },
}


def dotq(value) -> str:
    return json.dumps(str(value))


def aggregate_edge_pairs(edges):
    result = {}

    for edge in edges:
        key = (
            edge["provider_source"],
            edge["consumer_source"],
        )

        if key not in result:
            result[key] = {
                "binaries": set(),
                "fields": set(),
            }

        result[key]["binaries"].add(
            edge["binary_dependency"]
        )

        result[key]["fields"].add(
            edge["field"]
        )

    return result


def write_dependency_dot(
    path: Path,
    nodes: dict,
    edges,
    title: str,
    include_external: bool,
    cyclic_nodes: set[str],
):
    if include_external:
        selected_nodes = set(nodes)
    else:
        selected_nodes = {
            node
            for node, info in nodes.items()
            if info["state"] != "external-debian"
        }

    selected_edges = [
        edge
        for edge in edges
        if (
            edge["provider_source"] in selected_nodes
            and edge["consumer_source"] in selected_nodes
        )
    ]

    pairs = aggregate_edge_pairs(
        selected_edges
    )

    with path.open("w") as f:
        f.write("digraph RDistBootstrap {\n")
        f.write(
            '  graph ['
            'rankdir=LR, '
            'overlap=false, '
            'splines=spline, '
            'nodesep=0.35, '
            'ranksep=1.0, '
            'pad=0.25, '
            'bgcolor="white"'
            '];\n'
        )

        f.write(
            '  node ['
            'shape=box, '
            'style="rounded,filled", '
            'fontname="Helvetica", '
            'fontsize=10, '
            'margin="0.12,0.07"'
            '];\n'
        )

        f.write(
            '  edge ['
            'fontname="Helvetica", '
            'fontsize=8, '
            'color="#7e8794", '
            'arrowsize=0.7'
            '];\n'
        )

        f.write(
            f"  label={dotq(title)};\n"
        )
        f.write('  labelloc="t";\n')
        f.write("  fontsize=22;\n\n")

        for source in sorted(selected_nodes):
            info = nodes[source]
            style = STATE_STYLE[info["state"]]

            env_count = info[
                "pass2_env_consumer_count"
            ]

            font_size = min(
                16.0,
                9.0
                + 1.4
                * math.log2(env_count + 1),
            )

            border = style["border"]
            penwidth = 1.4

            if source in cyclic_nodes:
                border = "#6847b7"
                penwidth = 3.0

            label = (
                f"{source}\\n"
                f"{style['short']} · "
                f"L{info['build_level']}"
            )

            if env_count:
                label += (
                    f"\\nPass2 env: "
                    f"{env_count} builds"
                )

            tooltip = (
                f"{source}; "
                f"state={info['state']}; "
                f"level={info['build_level']}; "
                f"SCC={info['scc_id']}; "
                f"Pass2 consumers={env_count}"
            )

            f.write(
                f"  {dotq(source)} ["
                f"label={dotq(label)}, "
                f"fillcolor={dotq(style['fill'])}, "
                f"color={dotq(border)}, "
                f"penwidth={penwidth:.2f}, "
                f"fontsize={font_size:.1f}, "
                f"tooltip={dotq(tooltip)}"
                f"];\n"
            )

        f.write("\n")

        for (provider, consumer), data in sorted(
            pairs.items()
        ):
            binaries = sorted(
                data["binaries"]
            )

            count = len(binaries)

            penwidth = min(
                5.0,
                1.0 + math.log2(count + 1),
            )

            edge_label = (
                str(count)
                if count > 1
                else ""
            )

            tooltip = (
                f"{provider} -> {consumer}: "
                + ", ".join(binaries)
            )

            f.write(
                f"  {dotq(provider)} -> "
                f"{dotq(consumer)} ["
                f"penwidth={penwidth:.2f}, "
                f"label={dotq(edge_label)}, "
                f"tooltip={dotq(tooltip)}"
                f"];\n"
            )

        f.write("}\n")


def write_scc_dot(
    path: Path,
    core_sources: set[str],
    components,
    mapping,
    levels,
    edges,
):
    core_sccs = sorted(
        {
            mapping[source]
            for source in core_sources
        }
    )

    component_by_id = {
        f"SCC{i:03d}": component
        for i, component in enumerate(components)
    }

    pair_counts = defaultdict(int)
    external_targets = set()

    for edge in edges:
        provider = edge["provider_source"]
        consumer = edge["consumer_source"]

        if consumer not in core_sources:
            continue

        b = mapping[consumer]

        if provider not in core_sources:
            external_targets.add(b)
            continue

        a = mapping[provider]

        if a != b:
            pair_counts[(a, b)] += 1

    with path.open("w") as f:
        f.write("digraph SCC {\n")
        f.write(
            '  graph ['
            'rankdir=LR, overlap=false, '
            'splines=spline, '
            'nodesep=0.4, ranksep=1.2, '
            'bgcolor="white"'
            '];\n'
        )

        f.write(
            '  node ['
            'shape=box, '
            'style="rounded,filled", '
            'fontname="Helvetica", '
            'fillcolor="#e9eef5", '
            'color="#556270"'
            '];\n'
        )

        f.write(
            '  edge ['
            'color="#697684", '
            'arrowsize=0.8'
            '];\n'
        )

        f.write(
            '  label="R-Distro bootstrap SCC / '
            'partial build-order graph";\n'
        )
        f.write('  labelloc="t"; fontsize=22;\n')

        f.write(
            '  "__EXTERNAL__" ['
            'label="Unexpanded Debian\\n'
            'dependencies", '
            'fillcolor="#f4b7b7", '
            'color="#a63030", '
            'penwidth=2.0'
            '];\n'
        )

        for scc_id in core_sccs:
            members = [
                x
                for x in component_by_id[scc_id]
                if x in core_sources
            ]

            level = levels[scc_id]

            if len(members) <= 6:
                member_text = "\\n".join(
                    members
                )
            else:
                member_text = (
                    "\\n".join(members[:5])
                    + f"\\n… +{len(members)-5}"
                )

            cyclic = len(
                component_by_id[scc_id]
            ) > 1

            border = (
                "#6847b7"
                if cyclic
                else "#556270"
            )

            penwidth = (
                3.0
                if cyclic
                else 1.5
            )

            label = (
                f"{scc_id} · level {level}\\n"
                f"{member_text}"
            )

            f.write(
                f"  {dotq(scc_id)} ["
                f"label={dotq(label)}, "
                f"color={dotq(border)}, "
                f"penwidth={penwidth}"
                f"];\n"
            )

        for scc_id in sorted(external_targets):
            f.write(
                f'  "__EXTERNAL__" -> '
                f'{dotq(scc_id)};\n'
            )

        for (a, b), count in sorted(
            pair_counts.items()
        ):
            penwidth = min(
                5.0,
                1.0 + math.log2(count + 1),
            )

            label = (
                str(count)
                if count > 1
                else ""
            )

            f.write(
                f"  {dotq(a)} -> {dotq(b)} ["
                f"penwidth={penwidth:.2f}, "
                f"label={dotq(label)}"
                f"];\n"
            )

        f.write("}\n")


def render_graphviz(dot_files: list[Path]):
    dot = shutil.which("dot")

    if dot is None:
        print()
        print("Graphviz not installed.")
        print("DOT files were still generated.")
        print()
        print("Install it with:")
        print("  brew install graphviz")
        return

    for path in dot_files:
        svg = path.with_suffix(".svg")

        subprocess.run(
            [
                dot,
                "-Tsvg",
                str(path),
                "-o",
                str(svg),
            ],
            check=True,
        )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh Debian metadata using Docker",
    )

    parser.add_argument(
        "--mode",
        choices=("full", "bootstrap"),
        default="full",
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
        help="Active DEB_BUILD_PROFILES entry",
    )

    parser.add_argument(
        "--no-render",
        action="store_true",
    )

    args = parser.parse_args()

    OUT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not BASE_MANIFEST.exists():
        raise SystemExit(
            f"Missing {BASE_MANIFEST}"
        )

    if not FRONTIER_MANIFEST.exists():
        raise SystemExit(
            f"Missing {FRONTIER_MANIFEST}"
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
            + "\n  ".join(sorted(overlap))
        )

    core = base + frontier
    core_set = set(core)

    print(
        f"R-Distro base sources:     {len(base)}"
    )
    print(
        f"Debian frontier sources:   {len(frontier)}"
    )
    print(
        f"Combined core:             {len(core)}"
    )
    print()

    collect_snapshot_metadata(
        args.image,
        args.refresh,
    )

    source_records = load_source_records()

    missing_records = [
        source
        for source in core
        if source not in source_records
    ]

    if missing_records:
        raise SystemExit(
            "Missing source metadata:\n  "
            + "\n  ".join(missing_records)
        )

    (
        binaries,
        virtual_providers,
        source_versions,
    ) = load_binary_records()

    resolve_binary = make_binary_resolver(
        binaries,
        virtual_providers,
        source_versions,
    )

    if args.mode == "bootstrap":
        dependency_fields = (
            "Build-Depends",
            "Build-Depends-Arch",
        )

        active_profiles = set(args.profile)
        active_profiles.update({
            "nocheck",
            "nodoc",
        })
    else:
        dependency_fields = DEPENDENCY_FIELDS
        active_profiles = set(args.profile)

    edges, unresolved = build_edges(
        core,
        source_records,
        resolve_binary,
        args.arch,
        active_profiles,
        dependency_fields,
    )

    # Actual Pass-2 installed environment metrics.
    env_consumers, env_install_counts = (
        load_pass2_environment(
            resolve_binary
        )
    )

    all_nodes = set(core)

    for edge in edges:
        all_nodes.add(
            edge["provider_source"]
        )
        all_nodes.add(
            edge["consumer_source"]
        )

    external = all_nodes - core_set

    # SCC calculation.
    components, scc_mapping = tarjan_scc(
        all_nodes,
        edges,
    )

    levels, condensation = (
        condensation_levels(
            components,
            scc_mapping,
            edges,
        )
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

    for i, component in enumerate(components):
        scc_id = f"SCC{i:03d}"

        if (
            len(component) > 1
            or any(x in self_edges for x in component)
        ):
            cyclic_sccs.add(scc_id)

    cyclic_nodes = {
        node
        for node in all_nodes
        if scc_mapping[node] in cyclic_sccs
    }

    # Degree / fanout information.
    source_consumers = defaultdict(set)
    source_providers = defaultdict(set)

    edge_binary_counts = defaultdict(set)

    external_required_binaries = defaultdict(set)
    external_consumers = defaultdict(set)

    external_versions = {}

    for edge in edges:
        provider = edge["provider_source"]
        consumer = edge["consumer_source"]

        source_consumers[provider].add(
            consumer
        )

        source_providers[consumer].add(
            provider
        )

        edge_binary_counts[
            (provider, consumer)
        ].add(
            edge["binary_dependency"]
        )

        if provider in external:
            external_required_binaries[
                provider
            ].add(
                edge["binary_dependency"]
            )

            external_consumers[
                provider
            ].add(consumer)

            if edge["provider_version"]:
                external_versions.setdefault(
                    provider,
                    edge["provider_version"],
                )

    nodes = {}

    node_rows = []

    for source in sorted(all_nodes):
        if source in base:
            state = "rdistro-base-gen2"
        elif source in frontier:
            state = "debian-frontier"
        else:
            state = "external-debian"

        scc_id = scc_mapping[source]
        level = levels[scc_id]

        if source in source_records:
            version = source_records[
                source
            ].get("Version", "")

            binaries_provided = (
                source_records[source]
                .get("Binary", "")
                .replace("\n", " ")
            )
        else:
            version = external_versions.get(
                source,
                source_versions.get(source, ""),
            )
            binaries_provided = ""

        row = {
            "source": source,
            "state": state,
            "version": version,
            "special_class":
                special_class(source),
            "scc_id": scc_id,
            "cyclic":
                int(scc_id in cyclic_sccs),
            "build_level": level,
            "provider_source_count":
                len(source_providers[source]),
            "consumer_source_count":
                len(source_consumers[source]),
            "pass2_env_consumer_count":
                len(env_consumers[source]),
            "pass2_env_install_count":
                env_install_counts[source],
            "binaries_provided":
                binaries_provided,
        }

        nodes[source] = row
        node_rows.append(row)

    # External source ranking.
    external_rows = []

    for source in sorted(
        external,
        key=lambda x: (
            -len(external_consumers[x]),
            -len(env_consumers[x]),
            x,
        ),
    ):
        external_rows.append(
            {
                "source": source,
                "declared_consumer_count":
                    len(
                        external_consumers[
                            source
                        ]
                    ),
                "pass2_env_consumer_count":
                    len(
                        env_consumers[
                            source
                        ]
                    ),
                "dependency_binaries":
                    ", ".join(
                        sorted(
                            external_required_binaries[
                                source
                            ]
                        )
                    ),
                "consumers":
                    ", ".join(
                        sorted(
                            external_consumers[
                                source
                            ]
                        )
                    ),
                "special_class":
                    special_class(source),
            }
        )

    # SCC rows.
    scc_rows = []

    for i, component in enumerate(components):
        scc_id = f"SCC{i:03d}"

        states = sorted(
            {
                nodes[node]["state"]
                for node in component
            }
        )

        scc_rows.append(
            {
                "scc_id": scc_id,
                "build_level":
                    levels[scc_id],
                "size": len(component),
                "cyclic":
                    int(
                        scc_id
                        in cyclic_sccs
                    ),
                "states":
                    ", ".join(states),
                "members":
                    ", ".join(component),
            }
        )

    build_level_rows = sorted(
        (
            {
                "source": source,
                "state":
                    nodes[source]["state"],
                "scc_id":
                    nodes[source]["scc_id"],
                "build_level":
                    nodes[source][
                        "build_level"
                    ],
                "cyclic":
                    nodes[source]["cyclic"],
            }
            for source in all_nodes
        ),
        key=lambda x: (
            int(x["build_level"]),
            x["source"],
        ),
    )

    # Write data.
    write_csv(
        OUT / "nodes.csv",
        node_rows,
        [
            "source",
            "state",
            "version",
            "special_class",
            "scc_id",
            "cyclic",
            "build_level",
            "provider_source_count",
            "consumer_source_count",
            "pass2_env_consumer_count",
            "pass2_env_install_count",
            "binaries_provided",
        ],
    )

    write_csv(
        OUT / "edges.csv",
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
        OUT / "external-sources.csv",
        external_rows,
        [
            "source",
            "declared_consumer_count",
            "pass2_env_consumer_count",
            "dependency_binaries",
            "consumers",
            "special_class",
        ],
    )

    write_csv(
        OUT / "strongly-connected-components.csv",
        scc_rows,
        [
            "scc_id",
            "build_level",
            "size",
            "cyclic",
            "states",
            "members",
        ],
    )

    write_csv(
        OUT / "build-levels.csv",
        build_level_rows,
        [
            "source",
            "state",
            "scc_id",
            "build_level",
            "cyclic",
        ],
    )

    write_csv(
        OUT / "unresolved-dependencies.csv",
        unresolved,
        [
            "consumer_source",
            "field",
            "group_id",
            "relation",
            "active_alternatives",
        ],
    )

    data = {
        "architecture": args.arch,
        "active_profiles":
            sorted(args.profile),
        "counts": {
            "base_sources": len(base),
            "frontier_sources":
                len(frontier),
            "core_sources":
                len(core),
            "external_sources":
                len(external),
            "nodes":
                len(all_nodes),
            "selected_edges":
                len(edges),
            "sccs":
                len(components),
            "cyclic_sccs":
                len(cyclic_sccs),
            "unresolved_dependencies":
                len(unresolved),
        },
        "nodes": node_rows,
        "edges": edges,
        "external_sources":
            external_rows,
        "sccs": scc_rows,
        "build_levels":
            build_level_rows,
        "unresolved":
            unresolved,
    }

    with (
        OUT / "dependency-data.json"
    ).open("w") as f:
        json.dump(
            data,
            f,
            indent=2,
        )

    # Graphviz.
    core_dot = OUT / "bootstrap-core.dot"
    full_dot = (
        OUT
        / "bootstrap-with-external.dot"
    )
    scc_dot = OUT / "bootstrap-scc.dot"

    write_dependency_dot(
        core_dot,
        nodes,
        edges,
        (
            "R-Distro bootstrap core "
            f"({len(base)} Gen-2 + "
            f"{len(frontier)} frontier sources)"
        ),
        include_external=False,
        cyclic_nodes=cyclic_nodes,
    )

    write_dependency_dot(
        full_dot,
        nodes,
        edges,
        (
            "R-Distro bootstrap graph "
            "with immediate external "
            "Debian dependencies"
        ),
        include_external=True,
        cyclic_nodes=cyclic_nodes,
    )

    write_scc_dot(
        scc_dot,
        core_set,
        components,
        scc_mapping,
        levels,
        edges,
    )

    if not args.no_render:
        render_graphviz(
            [
                core_dot,
                full_dot,
                scc_dot,
            ]
        )

    # Markdown summary.
    cyclic_core = [
        row
        for row in scc_rows
        if (
            row["cyclic"]
            and any(
                member in core_set
                for member
                in row["members"].split(", ")
            )
        )
    ]

    top_external = external_rows[:20]

    summary = []

    summary.append(
        "# R-Distro Bootstrap Closure Analysis\n"
    )

    summary.append(
        f"- R-Distro Gen-2 sources: **{len(base)}**"
    )
    summary.append(
        f"- Current Debian frontier sources: **{len(frontier)}**"
    )
    summary.append(
        f"- Combined core: **{len(core)}**"
    )
    summary.append(
        f"- Immediate external source dependencies: **{len(external)}**"
    )
    summary.append(
        f"- Selected source dependency edges: **{len(edges)}**"
    )
    summary.append(
        f"- SCCs: **{len(components)}**"
    )
    summary.append(
        f"- Cyclic SCCs: **{len(cyclic_sccs)}**"
    )
    summary.append(
        f"- Unresolved dependency groups: **{len(unresolved)}**"
    )
    summary.append("")

    summary.append(
        "## Important interpretation\n"
    )
    summary.append(
        "The build levels are **partial bootstrap levels**. "
        "External Debian source nodes have not yet had their own "
        "Build-Depends expanded."
    )
    summary.append("")
    summary.append(
        "Dependency alternatives are resolved to the first active "
        "alternative available in the pinned snapshot."
    )
    summary.append("")
    summary.append(
        "`build-essential` is represented as a synthetic implicit "
        "dependency because Debian source packages normally do not "
        "declare the standard build-essential environment explicitly."
    )
    summary.append("")

    summary.append(
        "## Top immediate external sources\n"
    )
    summary.append("")
    summary.append(
        "| Source | Declared consumers | Pass-2 env consumers |"
    )
    summary.append(
        "|---|---:|---:|"
    )

    for row in top_external:
        summary.append(
            f"| {row['source']} | "
            f"{row['declared_consumer_count']} | "
            f"{row['pass2_env_consumer_count']} |"
        )

    summary.append("")
    summary.append(
        "## Core cyclic SCCs\n"
    )
    summary.append("")

    if cyclic_core:
        for row in cyclic_core:
            summary.append(
                f"- **{row['scc_id']}**: "
                f"{row['members']}"
            )
    else:
        summary.append(
            "No cycles were found inside the current core."
        )

    summary.append("")

    (OUT / "summary.md").write_text(
        "\n".join(summary)
    )

    # Console report.
    print()
    print(
        "========================================"
    )
    print(
        " R-Distro bootstrap closure analysis"
    )
    print(
        "========================================"
    )
    print()

    print(
        f"R-Distro Gen-2 sources:      {len(base)}"
    )
    print(
        f"Frontier sources:            {len(frontier)}"
    )
    print(
        f"Combined core:               {len(core)}"
    )
    print(
        f"Immediate external sources:  {len(external)}"
    )
    print(
        f"Selected dependency edges:   {len(edges)}"
    )
    print(
        f"SCCs:                       {len(components)}"
    )
    print(
        f"Cyclic SCCs:                {len(cyclic_sccs)}"
    )
    print(
        f"Unresolved dependency sets: {len(unresolved)}"
    )

    print()
    print(
        "Top immediate external sources:"
    )

    for row in top_external[:15]:
        print(
            f"  "
            f"{int(row['declared_consumer_count']):3d} declared  "
            f"{int(row['pass2_env_consumer_count']):3d} env  "
            f"{row['source']}"
        )

    print()
    print(f"Output: {OUT}")

    if not args.no_render:
        print()
        print("Visualizations:")
        print(
            f"  {OUT / 'bootstrap-core.svg'}"
        )
        print(
            f"  {OUT / 'bootstrap-with-external.svg'}"
        )
        print(
            f"  {OUT / 'bootstrap-scc.svg'}"
        )


if __name__ == "__main__":
    main()
