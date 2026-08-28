#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys

from collections import defaultdict, deque
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path


# ======================================================================
# Paths / policy
# ======================================================================

ROOT = Path(__file__).resolve().parents[1]

BASE_MANIFEST = ROOT / "manifests" / "base-sources.txt"

SOURCE_UNIVERSE = (
    ROOT
    / "analysis"
    / "bootstrap"
    / "universe-cache"
    / "sources.raw"
)

BINARY_UNIVERSE = (
    ROOT
    / "analysis"
    / "bootstrap"
    / "universe-cache"
    / "binaries.raw"
)

DEFAULT_OUT = (
    ROOT
    / "analysis"
    / "selfhost-boundary"
)

TARGET_ARCH = "arm64"
TARGET_OS = "linux"

# Final boundary policy.
ACTIVE_PROFILES = {
    "nocheck",
    "nodoc",
}

SOURCE_BUILD_FIELDS = (
    "Build-Depends",
    "Build-Depends-Arch",
)

BINARY_RUNTIME_FIELDS = (
    "Pre-Depends",
    "Depends",
)

BASE_PRIORITIES = {
    "required",
    "important",
}

NATIVE_BOUNDARY_EXCLUDED_SOURCES = {
    "cross-toolchain-base",
    "cross-toolchain-base-ports",
}


def is_native_boundary_excluded_source(name: str) -> bool:
    """
    R-Distro v1 is a native ARM64 self-hosting boundary.

    Exclude source packages whose purpose is to build cross-compilers
    or foreign-architecture sysroots.
    """
    return (
        name in NATIVE_BOUNDARY_EXCLUDED_SOURCES
        or (
            name.startswith("gcc-")
            and "-cross" in name
        )
    )


def is_native_boundary_excluded_binary_name(name: str) -> bool:
    """
    Exclude binaries whose purpose is cross-compilation or a
    foreign-architecture sysroot.

    R-Distro v1 targets native ARM64 / aarch64-linux-gnu only.
    """

    if name.endswith("-cross"):
        return True

    if name.startswith("crossbuild-essential-"):
        return True

    # GNU target triplets that are foreign to native ARM64.
    foreign_triplets = (
        "arm-linux-gnueabihf",
        "arm-linux-gnueabi",
        "x86_64-linux-gnu",
        "i686-linux-gnu",
        "powerpc64le-linux-gnu",
        "powerpc64-linux-gnu",
        "powerpc-linux-gnu",
        "s390x-linux-gnu",
        "riscv64-linux-gnu",
        "mips-linux-gnu",
        "mipsel-linux-gnu",
        "mips64-linux-gnuabi64",
        "mips64el-linux-gnuabi64",
    )

    return any(
        triplet in name
        for triplet in foreign_triplets
    )


def is_native_boundary_excluded_binary(
    name: str,
    source: str,
) -> bool:
    return (
        is_native_boundary_excluded_source(source)
        or is_native_boundary_excluded_binary_name(name)
    )


# ======================================================================
# Progress
# ======================================================================

def progress(
    label: str,
    current: int,
    total: int,
    width: int = 34,
):
    total = max(total, 1)

    ratio = min(
        max(current / total, 0.0),
        1.0,
    )

    done = int(width * ratio)

    bar = (
        "#" * done
        + "-" * (width - done)
    )

    sys.stdout.write(
        f"\r{label:<28} "
        f"[{bar}] "
        f"{current:>6}/{total:<6} "
        f"{100 * ratio:6.1f}%"
    )

    sys.stdout.flush()

    if current >= total:
        print()


# ======================================================================
# Debian version comparison
#
# Implements the ordering used by dpkg sufficiently directly rather than
# relying on a macOS installation of dpkg/apt_pkg.
# ======================================================================

def split_debian_version(version: str):
    if ":" in version:
        epoch_text, rest = version.split(":", 1)

        try:
            epoch = int(epoch_text)
        except ValueError:
            epoch = 0
    else:
        epoch = 0
        rest = version

    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
    else:
        upstream = rest
        revision = "0"

    return epoch, upstream, revision


def char_order(ch: str) -> int:
    if ch == "~":
        return -1

    if ch == "":
        return 0

    if ch.isalpha():
        return ord(ch)

    return ord(ch) + 256


def verrevcmp(a: str, b: str) -> int:
    ia = 0
    ib = 0

    while ia < len(a) or ib < len(b):

        # ----------------------------------------------------------
        # Compare non-digit portions.
        # ----------------------------------------------------------

        while (
            (ia < len(a) and not a[ia].isdigit())
            or
            (ib < len(b) and not b[ib].isdigit())
        ):
            ca = (
                a[ia]
                if ia < len(a)
                and not a[ia].isdigit()
                else ""
            )

            cb = (
                b[ib]
                if ib < len(b)
                and not b[ib].isdigit()
                else ""
            )

            oa = char_order(ca)
            ob = char_order(cb)

            if oa < ob:
                return -1

            if oa > ob:
                return 1

            if ca:
                ia += 1

            if cb:
                ib += 1

        # ----------------------------------------------------------
        # Compare digit portions numerically without integer overflow.
        # ----------------------------------------------------------

        while ia < len(a) and a[ia] == "0":
            ia += 1

        while ib < len(b) and b[ib] == "0":
            ib += 1

        first_difference = 0

        while (
            ia < len(a)
            and ib < len(b)
            and a[ia].isdigit()
            and b[ib].isdigit()
        ):
            if first_difference == 0:
                first_difference = (
                    ord(a[ia])
                    - ord(b[ib])
                )

            ia += 1
            ib += 1

        if (
            ia < len(a)
            and a[ia].isdigit()
        ):
            return 1

        if (
            ib < len(b)
            and b[ib].isdigit()
        ):
            return -1

        if first_difference < 0:
            return -1

        if first_difference > 0:
            return 1

    return 0


def debian_version_cmp(a: str, b: str) -> int:
    ea, ua, ra = split_debian_version(a)
    eb, ub, rb = split_debian_version(b)

    if ea < eb:
        return -1

    if ea > eb:
        return 1

    x = verrevcmp(ua, ub)

    if x:
        return x

    return verrevcmp(ra, rb)


def version_satisfies(
    actual: str,
    operator: str | None,
    required: str | None,
) -> bool:

    if not operator:
        return True

    if required is None:
        return True

    c = debian_version_cmp(
        actual,
        required,
    )

    return {
        "<<": c < 0,
        "<=": c <= 0,
        "=": c == 0,
        ">=": c >= 0,
        ">>": c > 0,
    }[operator]


# ======================================================================
# Debian control parsing
# ======================================================================

def parse_control_paragraph(
    text: str,
) -> dict[str, str]:

    fields: dict[str, str] = {}
    current = None

    for raw in text.splitlines():
        if not raw:
            continue

        if (
            raw[0].isspace()
            and current is not None
        ):
            fields[current] += (
                "\n"
                + raw.strip()
            )

            continue

        if ":" not in raw:
            continue

        key, value = raw.split(":", 1)

        key = key.strip()

        fields[key] = value.strip()

        current = key

    return fields


def load_control_file(
    path: Path,
    label: str,
) -> list[dict[str, str]]:

    text = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    blocks = [
        x
        for x in re.split(
            r"\n\s*\n",
            text,
        )
        if x.strip()
    ]

    records = []

    for i, block in enumerate(
        blocks,
        1,
    ):
        record = parse_control_paragraph(
            block
        )

        if record:
            records.append(record)

        if (
            i == 1
            or i % 500 == 0
            or i == len(blocks)
        ):
            progress(
                label,
                i,
                len(blocks),
            )

    return records


# ======================================================================
# Package records
# ======================================================================

@dataclass(frozen=True)
class SourceRecord:
    name: str
    version: str
    fields: dict[str, str]


@dataclass(frozen=True)
class BinaryRecord:
    name: str
    version: str
    architecture: str

    source: str
    source_version: str

    fields: dict[str, str]


def binary_source_reference(
    fields: dict[str, str],
):
    binary = fields["Package"]
    binary_version = fields["Version"]

    raw = fields.get(
        "Source",
        "",
    ).strip()

    if not raw:
        return (
            binary,
            binary_version,
        )

    match = re.fullmatch(
        r"([a-z0-9][a-z0-9+.-]*)"
        r"(?:\s+\(([^)]+)\))?",
        raw,
    )

    if not match:
        raise RuntimeError(
            f"Malformed Source field "
            f"for {binary}: {raw!r}"
        )

    source = match.group(1)

    source_version = (
        match.group(2)
        or binary_version
    )

    return (
        source,
        source_version,
    )


# ======================================================================
# Build current source/binary indices
# ======================================================================

def make_source_index(
    raw_records,
):
    versions = defaultdict(list)

    for fields in raw_records:
        name = fields.get("Package")
        version = fields.get("Version")

        if is_native_boundary_excluded_source(name):
            continue

        if not name or not version:
            continue

        versions[name].append(
            SourceRecord(
                name=name,
                version=version,
                fields=fields,
            )
        )

    result = {}

    for name, records in versions.items():

        records.sort(
            key=cmp_to_key(
                lambda a, b:
                    debian_version_cmp(
                        a.version,
                        b.version,
                    )
            ),
            reverse=True,
        )

        # Current source version in the pinned suite.
        result[name] = records[0]

    return result


def make_binary_index(
    raw_records,
    arch: str,
):
    versions = defaultdict(list)

    for fields in raw_records:

        name = fields.get("Package")
        version = fields.get("Version")
        binary_arch = fields.get(
            "Architecture",
            "",
        )

        if not name or not version:
            continue

        if binary_arch not in {
            arch,
            "all",
        }:
            continue

        source, source_version = (
            binary_source_reference(
                fields
            )
        )

        # R-Distro v1 boundary is native ARM64 only.
        # Cross-compilation sysroot packages are deliberately outside it.
        if is_native_boundary_excluded_binary(name, source):
            continue

        versions[name].append(
            BinaryRecord(
                name=name,
                version=version,
                architecture=binary_arch,
                source=source,
                source_version=source_version,
                fields=fields,
            )
        )

    result = {}

    for name, records in versions.items():

        def compare(a, b):
            x = debian_version_cmp(
                a.version,
                b.version,
            )

            if x:
                return x

            # Same version:
            # prefer target-native over Architecture: all.
            if (
                a.architecture == arch
                and b.architecture != arch
            ):
                return 1

            if (
                b.architecture == arch
                and a.architecture != arch
            ):
                return -1

            return 0

        records.sort(
            key=cmp_to_key(compare),
            reverse=True,
        )

        result[name] = records[0]

    return result


# ======================================================================
# Dependency syntax
# ======================================================================

def split_top_level(
    text: str,
    separator: str,
) -> list[str]:

    out = []
    buf = []

    paren = 0
    bracket = 0

    for ch in text:

        if ch == "(":
            paren += 1

        elif ch == ")":
            paren = max(paren - 1, 0)

        elif ch == "[":
            bracket += 1

        elif ch == "]":
            bracket = max(bracket - 1, 0)

        if (
            ch == separator
            and paren == 0
            and bracket == 0
        ):
            value = "".join(buf).strip()

            if value:
                out.append(value)

            buf = []

        else:
            buf.append(ch)

    value = "".join(buf).strip()

    if value:
        out.append(value)

    return out


ALT_PACKAGE_RE = re.compile(
    r"^\s*"
    r"([a-z0-9][a-z0-9+.-]*)"
    r"(?::([a-z0-9-]+))?"
)


VERSION_RE = re.compile(
    r"\(\s*"
    r"(<<|<=|=|>=|>>)"
    r"\s*"
    r"([^)]+?)"
    r"\s*\)"
)


@dataclass(frozen=True)
class Alternative:
    package: str
    qualifier: str | None

    operator: str | None
    version: str | None

    raw: str


def parse_alternative(
    text: str,
) -> Alternative | None:

    match = ALT_PACKAGE_RE.match(
        text
    )

    if not match:
        return None

    version_match = VERSION_RE.search(
        text
    )

    operator = None
    version = None

    if version_match:
        operator = version_match.group(1)
        version = version_match.group(2)

    return Alternative(
        package=match.group(1),
        qualifier=match.group(2),
        operator=operator,
        version=version,
        raw=text,
    )


# ======================================================================
# Architecture restrictions
# ======================================================================

def arch_pattern_matches(
    pattern: str,
    arch: str,
    os_name: str,
) -> bool:

    if pattern in {
        "any",
        "any-any",
    }:
        return True

    if pattern == arch:
        return True

    if pattern == f"{os_name}-any":
        return True

    if pattern == f"any-{arch}":
        return True

    if pattern == f"{os_name}-{arch}":
        return True

    return False


def architecture_active(
    text: str,
    arch: str,
    os_name: str,
) -> bool:

    groups = re.findall(
        r"\[([^\]]+)\]",
        text,
    )

    if not groups:
        return True

    # Debian permits at most one arch restriction list
    # for an alternative, but joining is harmless here.
    tokens = []

    for group in groups:
        tokens.extend(
            group.split()
        )

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
        arch_pattern_matches(
            x,
            arch,
            os_name,
        )
        for x in negative
    ):
        return False

    if positive:
        return any(
            arch_pattern_matches(
                x,
                arch,
                os_name,
            )
            for x in positive
        )

    return True


# ======================================================================
# Build profile restriction formulas
#
# Debian profile formulas are DNF:
#   terms inside one <> are AND
#   multiple <> groups are OR
# ======================================================================

def extract_profile_groups(
    text: str,
) -> list[str]:

    groups = []

    paren = 0
    bracket = 0
    i = 0

    while i < len(text):

        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if ch == "(":
            paren += 1
            i += 1
            continue

        if ch == ")":
            paren = max(paren - 1, 0)
            i += 1
            continue

        if ch == "[" and paren == 0:
            bracket += 1
            i += 1
            continue

        if ch == "]" and paren == 0:
            bracket = max(bracket - 1, 0)
            i += 1
            continue

        # A real profile starts with <foo> or <!foo>.
        # Do NOT mistake version operators << or <= for profiles.
        if (
            ch == "<"
            and paren == 0
            and bracket == 0
            and nxt not in ("<", "=")
        ):
            end = text.find(">", i + 1)

            if end != -1:
                groups.append(
                    text[i + 1:end].strip()
                )

                i = end + 1
                continue

        i += 1

    return groups


def profiles_active(
    text: str,
    enabled: set[str],
) -> bool:

    groups = extract_profile_groups(
        text
    )

    if not groups:
        return True

    # Debian build-profile restrictions:
    #
    # tokens inside one <...> group are AND
    # multiple <...> groups are OR

    for group in groups:

        group_true = True

        for token in group.split():

            if token.startswith("!"):
                if token[1:] in enabled:
                    group_true = False
                    break

            else:
                if token not in enabled:
                    group_true = False
                    break

        if group_true:
            return True

    return False


def active_alternatives(
    relation_group: str,
    arch: str,
    os_name: str,
    profiles: set[str],
):
    result = []

    for raw in split_top_level(
        relation_group,
        "|",
    ):
        if not architecture_active(
            raw,
            arch,
            os_name,
        ):
            continue

        if not profiles_active(
            raw,
            profiles,
        ):
            continue

        alt = parse_alternative(
            raw
        )

        if alt:
            result.append(alt)

    return result


# ======================================================================
# Virtual package Providers
# ======================================================================

@dataclass(frozen=True)
class Provider:
    binary: BinaryRecord
    provided_version: str | None


def make_provider_index(
    binaries: dict[str, BinaryRecord],
):
    providers = defaultdict(list)

    for binary in binaries.values():

        raw = binary.fields.get(
            "Provides",
            "",
        )

        if not raw:
            continue

        for item in split_top_level(
            raw,
            ",",
        ):
            alt = parse_alternative(
                item
            )

            if not alt:
                continue

            provided_version = (
                alt.version
                if alt.operator == "="
                else None
            )

            providers[
                alt.package
            ].append(
                Provider(
                    binary=binary,
                    provided_version=
                        provided_version,
                )
            )

    return providers


# ======================================================================
# Deterministic dependency resolver
# ======================================================================

PRIORITY_RANK = {
    "required": 0,
    "important": 1,
    "standard": 2,
    "optional": 3,
    "extra": 4,
}


def provider_sort_key(
    provider: Provider,
):
    binary = provider.binary

    essential = (
        binary.fields.get(
            "Essential",
            "",
        ) == "yes"
    )

    build_essential = (
        binary.fields.get(
            "Build-Essential",
            "",
        ) == "yes"
    )

    priority = PRIORITY_RANK.get(
        binary.fields.get(
            "Priority",
            "",
        ),
        99,
    )

    return (
        0 if essential else 1,
        0 if build_essential else 1,
        priority,
        binary.name,
    )


class Resolver:
    def __init__(
        self,
        binaries,
        providers,
    ):
        self.binaries = binaries
        self.providers = providers

        self.ambiguous = {}

    def resolve(
        self,
        alt: Alternative,
        context: str,
    ) -> tuple[
        BinaryRecord | None,
        str,
    ]:

        # ----------------------------------------------------------
        # Prefer an actual package with the requested name.
        # ----------------------------------------------------------

        direct = self.binaries.get(
            alt.package
        )

        if direct and version_satisfies(
            direct.version,
            alt.operator,
            alt.version,
        ):
            return (
                direct,
                "direct",
            )

        # ----------------------------------------------------------
        # Otherwise resolve through Provides.
        # ----------------------------------------------------------

        candidates = []

        for provider in self.providers.get(
            alt.package,
            (),
        ):
            if alt.operator:

                # Versioned dependency can only be satisfied
                # by a versioned Provides.
                if (
                    provider.provided_version
                    is None
                ):
                    continue

                if not version_satisfies(
                    provider.provided_version,
                    alt.operator,
                    alt.version,
                ):
                    continue

            candidates.append(
                provider
            )

        if not candidates:
            return (
                None,
                "unresolved",
            )

        candidates.sort(
            key=provider_sort_key
        )

        chosen = candidates[0]

        if len(candidates) > 1:

            key = (
                context,
                alt.package,
            )

            self.ambiguous[key] = {
                "context": context,
                "virtual_package":
                    alt.package,
                "chosen_binary":
                    chosen.binary.name,
                "candidates":
                    ", ".join(
                        x.binary.name
                        for x in candidates
                    ),
            }

        return (
            chosen.binary,
            "virtual",
        )


# ======================================================================
# Graph / closure state
# ======================================================================

def add_cause(
    mapping,
    key,
    cause,
):
    mapping[key].add(cause)


def source_provider_name(
    binary: BinaryRecord,
):
    return binary.source


# ======================================================================
# Tarjan SCC
# ======================================================================

def tarjan_scc(
    nodes: set[str],
    adjacency: dict[str, set[str]],
):
    index = 0

    stack = []
    on_stack = set()

    indices = {}
    lowlink = {}

    components = []

    sys.setrecursionlimit(
        max(
            10000,
            len(nodes) * 2 + 100,
        )
    )

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
                "Compute source SCCs",
                i,
                len(ordered),
            )

    components.sort(
        key=lambda x: (
            -len(x),
            x[0],
        )
    )

    mapping = {}

    for i, component in enumerate(
        components
    ):
        scc = f"SCC{i:04d}"

        for source in component:
            mapping[source] = scc

    return (
        components,
        mapping,
    )


# ======================================================================
# CSV helpers
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


def sha256_file(
    path: Path,
):
    h = hashlib.sha256()

    with path.open("rb") as f:

        while True:
            chunk = f.read(
                1024 * 1024
            )

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def explain_why_source(
    target: str,
    required_sources,
    base_source_set,
    seed_rows,
    build_edges,
    runtime_edges,
):
    """
    Print one shortest dependency chain explaining why TARGET
    is inside the self-hosting source boundary.

    Direction here is:
        requiring source -> required source
    """

    if target not in required_sources:
        print()
        print(
            f"{target!r} is not in the self-hosting boundary."
        )
        return

    # --------------------------------------------------------------
    # Boundary roots / seeds.
    # --------------------------------------------------------------

    root_reasons = defaultdict(list)

    for source in sorted(base_source_set):
        if source in required_sources:
            root_reasons[source].append(
                "base source target"
            )

    for row in seed_rows:
        source = row["source"]

        if source in required_sources:
            root_reasons[source].append(
                f"binary seed {row['binary']} "
                f"({row['reasons']})"
            )

    # --------------------------------------------------------------
    # Requirement graph:
    #
    # consumer source -> provider source
    #
    # Note that the SCC graph later in this script deliberately uses
    # the opposite orientation (provider -> consumer).
    # --------------------------------------------------------------

    graph = defaultdict(list)
    required_by = defaultdict(list)

    for edge in build_edges:
        consumer = edge["consumer_source"]
        provider = edge["provider_source"]

        detail = (
            f"{edge['field']}: "
            f"{edge['selected_alternative']} "
            f"-> {edge['binary_dependency']}"
        )

        graph[consumer].append(
            (provider, detail)
        )

        required_by[provider].append(
            (consumer, detail)
        )

    for edge in runtime_edges:
        consumer = edge["consumer_source"]
        provider = edge["provider_source"]

        detail = (
            f"{edge['consumer_binary']} "
            f"{edge['field']}: "
            f"{edge['selected_alternative']} "
            f"-> {edge['provider_binary']}"
        )

        graph[consumer].append(
            (provider, detail)
        )

        required_by[provider].append(
            (consumer, detail)
        )

    # --------------------------------------------------------------
    # BFS from every seed. This gives one shortest explanation and
    # cannot loop forever inside the large SCC.
    # --------------------------------------------------------------

    roots = sorted(root_reasons)

    queue = deque(roots)

    previous = {
        root: None
        for root in roots
    }

    while queue:
        current = queue.popleft()

        if current == target:
            break

        for child, detail in sorted(
            graph.get(current, []),
            key=lambda x: (x[0], x[1]),
        ):
            if child not in required_sources:
                continue

            if child in previous:
                continue

            previous[child] = (
                current,
                detail,
            )

            queue.append(child)

    print()
    print("========================================")
    print(f" WHY {target}")
    print("========================================")
    print()

    if target not in previous:
        print(
            "No path from a recorded boundary seed was found."
        )
        return

    # --------------------------------------------------------------
    # Reconstruct path.
    # --------------------------------------------------------------

    path = []

    node = target

    while previous[node] is not None:
        parent, detail = previous[node]

        path.append(
            (
                parent,
                node,
                detail,
            )
        )

        node = parent

    root = node

    path.reverse()

    print(f"Seed source: {root}")

    for reason in sorted(
        set(root_reasons[root])
    ):
        print(f"  seed reason: {reason}")

    print()

    if not path:
        print(
            f"{target} is itself a boundary seed."
        )
    else:
        print(root)

        for parent, child, detail in path:
            print(
                f"  -> {child}"
            )
            print(
                f"     [{detail}]"
            )

    # --------------------------------------------------------------
    # Also answer the immediate "what needs it?" question.
    # --------------------------------------------------------------

    incoming = sorted(
        set(required_by.get(target, [])),
        key=lambda x: (x[0], x[1]),
    )

    print()
    print("Directly required by:")

    if not incoming:
        print("  (none; it is a seed)")
    else:
        for consumer, detail in incoming:
            print(
                f"  {consumer}"
            )
            print(
                f"    [{detail}]"
            )


# ======================================================================
# Main
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compute the final fixed-point ARM64 "
            "R-Distro self-hosting boundary."
        )
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
    )

    parser.add_argument(
        "--arch",
        default=TARGET_ARCH,
    )

    parser.add_argument(
        "--no-render",
        action="store_true",
    )

    parser.add_argument(
        "--why",
        metavar="SOURCE",
        help=(
            "Explain why a source package is in "
            "the self-hosting boundary."
        ),
    )

    args = parser.parse_args()

    out = args.out.resolve()

    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------------
    # Preconditions
    # --------------------------------------------------------------

    for path in (
        BASE_MANIFEST,
        SOURCE_UNIVERSE,
        BINARY_UNIVERSE,
    ):
        if not path.exists():
            raise SystemExit(
                f"Missing required file: {path}"
            )

    base_sources = [
        line.split("#", 1)[0].strip()
        for line in BASE_MANIFEST.read_text().splitlines()
        if line.split("#", 1)[0].strip()
    ]

    base_source_set = set(
        base_sources
    )

    print()
    print(
        "========================================"
    )
    print(
        " R-Distro self-host boundary"
    )
    print(
        "========================================"
    )
    print()

    print(
        f"Target architecture:       {args.arch}"
    )

    print(
        f"Base source targets:       {len(base_sources)}"
    )

    print(
        "Build fields:              "
        + ", ".join(
            SOURCE_BUILD_FIELDS
        )
    )

    print(
        "Runtime fields:            "
        + ", ".join(
            BINARY_RUNTIME_FIELDS
        )
    )

    print(
        "Build profiles:            "
        + ", ".join(
            sorted(
                ACTIVE_PROFILES
            )
        )
    )

    print(
        "Build-Depends-Indep:       EXCLUDED"
    )

    print(
        "Recommends/Suggests:       EXCLUDED"
    )

    print()

    # --------------------------------------------------------------
    # Parse universe.
    # --------------------------------------------------------------

    source_raw = load_control_file(
        SOURCE_UNIVERSE,
        "Parse source universe",
    )

    binary_raw = load_control_file(
        BINARY_UNIVERSE,
        "Parse binary universe",
    )

    print()

    print(
        "Indexing current source versions..."
    )

    sources = make_source_index(
        source_raw
    )

    print(
        "Indexing ARM64/all binary candidates..."
    )

    binaries = make_binary_index(
        binary_raw,
        args.arch,
    )

    providers = make_provider_index(
        binaries
    )

    resolver = Resolver(
        binaries,
        providers,
    )

    print()

    print(
        f"Current source packages:   {len(sources)}"
    )

    print(
        f"Current binary packages:   {len(binaries)}"
    )

    print(
        f"Virtual package names:     {len(providers)}"
    )

    # --------------------------------------------------------------
    # Closure state.
    # --------------------------------------------------------------

    required_sources: dict[
        str,
        SourceRecord,
    ] = {}

    required_binaries: dict[
        str,
        BinaryRecord,
    ] = {}

    source_depth = {}
    binary_depth = {}

    source_causes = defaultdict(set)
    binary_causes = defaultdict(set)

    processed_sources = set()
    processed_binaries = set()

    build_edges = []
    runtime_edges = []

    build_edge_keys = set()
    runtime_edge_keys = set()

    unresolved = []
    unresolved_keys = set()

    version_mismatches = {}
    seed_rows = []

    # --------------------------------------------------------------
    # Add helpers.
    # --------------------------------------------------------------

    def record_unresolved(
        kind,
        owner,
        field,
        relation,
        detail,
    ):
        key = (
            kind,
            owner,
            field,
            relation,
            detail,
        )

        if key in unresolved_keys:
            return

        unresolved_keys.add(key)

        unresolved.append(
            {
                "kind": kind,
                "owner": owner,
                "field": field,
                "relation": relation,
                "detail": detail,
            }
        )

    def add_source(
        name: str,
        depth: int,
        cause: str,
    ):
        record = sources.get(name)

        if record is None:
            record_unresolved(
                "source",
                name,
                "",
                "",
                "source package absent from Sources universe",
            )

            return False

        if name not in required_sources:
            required_sources[name] = record
            source_depth[name] = depth

        else:
            source_depth[name] = min(
                source_depth[name],
                depth,
            )

        add_cause(
            source_causes,
            name,
            cause,
        )

        return True

    def add_binary(
        record: BinaryRecord,
        depth: int,
        cause: str,
    ):
        name = record.name

        if name not in required_binaries:

            required_binaries[name] = record
            binary_depth[name] = depth

        else:
            binary_depth[name] = min(
                binary_depth[name],
                depth,
            )

        add_cause(
            binary_causes,
            name,
            cause,
        )

    # --------------------------------------------------------------
    # Seed 1: the 49 R-Distro base source targets.
    # --------------------------------------------------------------

    for source in base_sources:

        add_source(
            source,
            0,
            "base-source-target",
        )

    # --------------------------------------------------------------
    # Seed 2:
    #
    # Binary base system:
    #   Priority required/important
    #
    # Build baseline:
    #   Essential: yes
    #   Build-Essential: yes
    #
    # This makes omitted implicit build-essential dependencies explicit.
    # --------------------------------------------------------------

    for binary in binaries.values():

        reasons = []

        priority = binary.fields.get(
            "Priority",
            "",
        )

        if priority in BASE_PRIORITIES:
            reasons.append(
                f"priority:{priority}"
            )

        if (
            binary.fields.get(
                "Essential",
                "",
            ) == "yes"
        ):
            reasons.append(
                "Essential:yes"
            )

        if (
            binary.fields.get(
                "Build-Essential",
                "",
            ) == "yes"
        ):
            reasons.append(
                "Build-Essential:yes"
            )

        if not reasons:
            continue

        reason_text = ",".join(
            reasons
        )

        add_binary(
            binary,
            0,
            "seed:" + reason_text,
        )

        seed_rows.append(
            {
                "binary": binary.name,
                "version": binary.version,
                "architecture":
                    binary.architecture,
                "source": binary.source,
                "source_version":
                    binary.source_version,
                "reasons": reason_text,
            }
        )

    print()
    print(
        f"Initial required sources:  {len(required_sources)}"
    )

    print(
        f"Initial binary seeds:      {len(required_binaries)}"
    )

    # --------------------------------------------------------------
    # Relation resolution functions.
    # --------------------------------------------------------------

    def resolve_build_group(
        consumer: SourceRecord,
        field: str,
        group: str,
    ):
        alternatives = active_alternatives(
            group,
            args.arch,
            TARGET_OS,
            ACTIVE_PROFILES,
        )

        # Cross-compilation alternatives are deliberately outside the
        # native ARM64 self-hosting boundary.
        alternatives = [
            alt
            for alt in alternatives
            if not is_native_boundary_excluded_binary_name(alt.package)
        ]

        if not alternatives:
            return

        # Debian autobuilder semantics:
        # after arch filtering, use first package name;
        # alternatives with a different package name are discarded.
        # first_name = alternatives[0].package

        # alternatives = [
        #     x
        #     for x in alternatives
        #     if x.package == first_name
        # ]

        selected = None
        resolution = None
        selected_alt = None

        for alt in alternatives:

            selected, resolution = (
                resolver.resolve(
                    alt,
                    context=(
                        f"build:{consumer.name}:"
                        f"{field}:{group}"
                    ),
                )
            )

            if selected is not None:
                selected_alt = alt
                break

        if selected is None:

            record_unresolved(
                "build-dependency",
                consumer.name,
                field,
                group,
                (
                    "no satisfiable active alternative: "
                    + " | ".join(
                        x.package
                        for x in alternatives
                    )
                ),
            )

            return

        add_binary(
            selected,
            source_depth[
                consumer.name
            ] + 1,
            (
                f"builddep:{consumer.name}:"
                f"{field}:{group}"
            ),
        )

        edge_key = (
            selected.source,
            consumer.name,
            selected.name,
            field,
            group,
        )

        if edge_key not in build_edge_keys:

            build_edge_keys.add(
                edge_key
            )

            build_edges.append(
                {
                    "provider_source":
                        selected.source,
                    "consumer_source":
                        consumer.name,
                    "binary_dependency":
                        selected.name,
                    "binary_version":
                        selected.version,
                    "field":
                        field,
                    "relation":
                        group,
                    "selected_alternative":
                        selected_alt.raw
                        if selected_alt
                        else "",
                    "resolution":
                        resolution,
                }
            )

    def resolve_runtime_group(
        consumer: BinaryRecord,
        field: str,
        group: str,
    ):
        alternatives = active_alternatives(
            group,
            args.arch,
            TARGET_OS,
            set(),
        )

        alternatives = [
            alt
            for alt in alternatives
            if not is_native_boundary_excluded_binary_name(alt.package)
        ]

        if not alternatives:
            return

        selected = None
        resolution = None
        selected_alt = None

        # Runtime package alternatives:
        # deterministically take the first satisfiable alternative.
        for alt in alternatives:

            selected, resolution = (
                resolver.resolve(
                    alt,
                    context=(
                        f"runtime:{consumer.name}:"
                        f"{field}:{group}"
                    ),
                )
            )

            if selected is not None:
                selected_alt = alt
                break

        if selected is None:

            record_unresolved(
                "runtime-dependency",
                consumer.name,
                field,
                group,
                "no satisfiable alternative",
            )

            return

        add_binary(
            selected,
            binary_depth[
                consumer.name
            ] + 1,
            (
                f"runtime:{consumer.name}:"
                f"{field}:{group}"
            ),
        )

        edge_key = (
            consumer.name,
            selected.name,
            field,
            group,
        )

        if edge_key not in runtime_edge_keys:

            runtime_edge_keys.add(
                edge_key
            )

            runtime_edges.append(
                {
                    "consumer_binary":
                        consumer.name,
                    "consumer_source":
                        consumer.source,
                    "provider_binary":
                        selected.name,
                    "provider_source":
                        selected.source,
                    "field":
                        field,
                    "relation":
                        group,
                    "selected_alternative":
                        selected_alt.raw
                        if selected_alt
                        else "",
                    "resolution":
                        resolution,
                }
            )

    # --------------------------------------------------------------
    # Fixed-point recursion.
    # --------------------------------------------------------------

    round_number = 0

    while True:

        source_batch = [
            name
            for name in required_sources
            if name not in processed_sources
        ]

        binary_batch = [
            name
            for name in required_binaries
            if name not in processed_binaries
        ]

        if (
            not source_batch
            and not binary_batch
        ):
            break

        print()
        print(
            "========================================"
        )

        print(
            f" Closure round {round_number}"
        )

        print(
            "========================================"
        )

        print(
            f"Sources queued:  {len(source_batch)}"
        )

        print(
            f"Binaries queued: {len(binary_batch)}"
        )

        # ----------------------------------------------------------
        # Expand source -> build binaries.
        # ----------------------------------------------------------

        for i, source_name in enumerate(
            source_batch,
            1,
        ):
            source = required_sources[
                source_name
            ]

            for field in SOURCE_BUILD_FIELDS:

                raw = source.fields.get(
                    field,
                    "",
                )

                if not raw:
                    continue

                for group in split_top_level(
                    raw,
                    ",",
                ):
                    resolve_build_group(
                        source,
                        field,
                        group,
                    )

            processed_sources.add(
                source_name
            )

            if (
                i == 1
                or i % 25 == 0
                or i == len(source_batch)
            ):
                progress(
                    f"Sources round {round_number}",
                    i,
                    len(source_batch),
                )

        # ----------------------------------------------------------
        # Anything discovered while processing sources should be
        # included in this round's binary processing.
        # ----------------------------------------------------------

        binary_batch = [
            name
            for name in required_binaries
            if name not in processed_binaries
        ]

        # ----------------------------------------------------------
        # Expand binary:
        #
        #   binary -> its source
        #   binary -> Pre-Depends / Depends binaries
        # ----------------------------------------------------------

        for i, binary_name in enumerate(
            binary_batch,
            1,
        ):
            binary = required_binaries[
                binary_name
            ]

            # ------------------------------------------------------
            # Every binary inside the boundary must itself come from
            # a source package inside the boundary.
            # ------------------------------------------------------

            add_source(
                binary.source,
                binary_depth[
                    binary_name
                ] + 1,
                (
                    f"produces-binary:"
                    f"{binary.name}="
                    f"{binary.version}"
                ),
            )

            canonical_source = sources.get(
                binary.source
            )

            if (
                canonical_source
                and
                binary.source_version
                != canonical_source.version
            ):
                key = (
                    binary.name,
                    binary.version,
                    binary.source,
                    binary.source_version,
                    canonical_source.version,
                )

                version_mismatches[key] = {
                    "binary":
                        binary.name,
                    "binary_version":
                        binary.version,
                    "source":
                        binary.source,
                    "binary_references_source_version":
                        binary.source_version,
                    "boundary_source_version":
                        canonical_source.version,
                }

            # ------------------------------------------------------
            # Runtime closure of the build/base binary.
            # ------------------------------------------------------

            for field in BINARY_RUNTIME_FIELDS:

                raw = binary.fields.get(
                    field,
                    "",
                )

                if not raw:
                    continue

                for group in split_top_level(
                    raw,
                    ",",
                ):
                    resolve_runtime_group(
                        binary,
                        field,
                        group,
                    )

            processed_binaries.add(
                binary_name
            )

            if (
                i == 1
                or i % 25 == 0
                or i == len(binary_batch)
            ):
                progress(
                    f"Binaries round {round_number}",
                    i,
                    len(binary_batch),
                )

        print(
            f"Total sources:  {len(required_sources)}"
        )

        print(
            f"Total binaries: {len(required_binaries)}"
        )

        round_number += 1

    # --------------------------------------------------------------
    # Fixed point.
    # --------------------------------------------------------------

    print()
    print(
        "========================================"
    )

    print(
        " FIXED POINT REACHED"
    )

    print(
        "========================================"
    )

    print()

    print(
        f"Boundary sources:          {len(required_sources)}"
    )

    print(
        f"Boundary binaries:         {len(required_binaries)}"
    )

    print(
        f"Build dependency edges:    {len(build_edges)}"
    )

    print(
        f"Runtime dependency edges:  {len(runtime_edges)}"
    )

    print(
        f"Ambiguous virtual choices: {len(resolver.ambiguous)}"
    )

    print(
        f"Source-version fallbacks:  {len(version_mismatches)}"
    )

    print(
        f"Unresolved relations:      {len(unresolved)}"
    )

    # --------------------------------------------------------------
    # Source-level graph.
    #
    # provider source -> consumer source
    # --------------------------------------------------------------

    adjacency = defaultdict(set)

    source_edge_types = defaultdict(set)

    for edge in build_edges:

        provider = edge[
            "provider_source"
        ]

        consumer = edge[
            "consumer_source"
        ]

        if (
            provider in required_sources
            and consumer in required_sources
        ):
            adjacency[
                provider
            ].add(
                consumer
            )

            source_edge_types[
                (provider, consumer)
            ].add(
                "build"
            )

    for edge in runtime_edges:

        provider = edge[
            "provider_source"
        ]

        consumer = edge[
            "consumer_source"
        ]

        if (
            provider in required_sources
            and consumer in required_sources
        ):
            adjacency[
                provider
            ].add(
                consumer
            )

            source_edge_types[
                (provider, consumer)
            ].add(
                "runtime"
            )

    (
        components,
        scc_mapping,
    ) = tarjan_scc(
        set(
            required_sources
        ),
        adjacency,
    )

    cyclic_components = []

    for i, component in enumerate(
        components
    ):
        cyclic = (
            len(component) > 1
            or any(
                node
                in adjacency.get(
                    node,
                    set(),
                )
                for node in component
            )
        )

        if cyclic:
            cyclic_components.append(
                (
                    f"SCC{i:04d}",
                    component,
                )
            )

    # --------------------------------------------------------------
    # Output manifests
    # --------------------------------------------------------------

    source_rows = []

    for name in sorted(
        required_sources
    ):
        record = required_sources[
            name
        ]

        source_rows.append(
            {
                "source": name,
                "version":
                    record.version,
                "depth":
                    source_depth[name],
                "base_target":
                    int(
                        name
                        in base_source_set
                    ),
                "causes":
                    " | ".join(
                        sorted(
                            source_causes[name]
                        )
                    ),
            }
        )

    binary_rows = []

    for name in sorted(
        required_binaries
    ):
        record = required_binaries[
            name
        ]

        binary_rows.append(
            {
                "binary": name,
                "version":
                    record.version,
                "architecture":
                    record.architecture,
                "source":
                    record.source,
                "source_version":
                    record.source_version,
                "depth":
                    binary_depth[name],
                "priority":
                    record.fields.get(
                        "Priority",
                        "",
                    ),
                "essential":
                    record.fields.get(
                        "Essential",
                        "",
                    ),
                "build_essential":
                    record.fields.get(
                        "Build-Essential",
                        "",
                    ),
                "causes":
                    " | ".join(
                        sorted(
                            binary_causes[name]
                        )
                    ),
            }
        )

    write_csv(
        out / "sources.csv",
        source_rows,
        [
            "source",
            "version",
            "depth",
            "base_target",
            "causes",
        ],
    )

    write_csv(
        out / "binaries.csv",
        binary_rows,
        [
            "binary",
            "version",
            "architecture",
            "source",
            "source_version",
            "depth",
            "priority",
            "essential",
            "build_essential",
            "causes",
        ],
    )

    write_csv(
        out / "build-edges.csv",
        build_edges,
        [
            "provider_source",
            "consumer_source",
            "binary_dependency",
            "binary_version",
            "field",
            "relation",
            "resolution",
        ],
    )

    write_csv(
        out / "runtime-edges.csv",
        runtime_edges,
        [
            "consumer_binary",
            "consumer_source",
            "provider_binary",
            "provider_source",
            "field",
            "relation",
            "selected_alternative",
            "resolution",
        ],
    )

    write_csv(
        out / "seeds.csv",
        seed_rows,
        [
            "binary",
            "version",
            "architecture",
            "source",
            "source_version",
            "reasons",
        ],
    )

    write_csv(
        out / "ambiguous-virtuals.csv",
        sorted(
            resolver.ambiguous.values(),
            key=lambda r: (
                r["virtual_package"],
                r["context"],
            ),
        ),
        [
            "context",
            "virtual_package",
            "chosen_binary",
            "candidates",
        ],
    )

    write_csv(
        out / "source-version-mismatches.csv",
        sorted(
            version_mismatches.values(),
            key=lambda r: (
                r["source"],
                r["binary"],
            ),
        ),
        [
            "binary",
            "binary_version",
            "source",
            "binary_references_source_version",
            "boundary_source_version",
        ],
    )

    write_csv(
        out / "unresolved.csv",
        unresolved,
        [
            "kind",
            "owner",
            "field",
            "relation",
            "detail",
        ],
    )

    scc_rows = []

    for i, component in enumerate(
        components
    ):
        scc = f"SCC{i:04d}"

        cyclic = (
            len(component) > 1
            or any(
                x
                in adjacency.get(
                    x,
                    set(),
                )
                for x in component
            )
        )

        scc_rows.append(
            {
                "scc":
                    scc,
                "size":
                    len(component),
                "cyclic":
                    int(cyclic),
                "members":
                    ", ".join(
                        component
                    ),
            }
        )

    write_csv(
        out / "source-sccs.csv",
        scc_rows,
        [
            "scc",
            "size",
            "cyclic",
            "members",
        ],
    )

    # --------------------------------------------------------------
    # Plain manifests used by future build scripts.
    # --------------------------------------------------------------

    (
        out
        / "boundary-source-names.txt"
    ).write_text(
        "".join(
            f"{name}\n"
            for name in sorted(
                required_sources
            )
        )
    )

    (
        out
        / "boundary-sources.txt"
    ).write_text(
        "".join(
            f"{name} {required_sources[name].version}\n"
            for name in sorted(
                required_sources
            )
        )
    )

    (
        out
        / "boundary-binaries.txt"
    ).write_text(
        "".join(
            (
                f"{name}:"
                f"{required_binaries[name].architecture} "
                f"{required_binaries[name].version} "
                f"{required_binaries[name].source}\n"
            )
            for name in sorted(
                required_binaries
            )
        )
    )

    # --------------------------------------------------------------
    # SCC condensation visualization.
    #
    # We deliberately do NOT render the full thousands-of-node graph.
    # --------------------------------------------------------------

    condensation_edges = defaultdict(set)

    for (
        provider,
        consumer,
    ), edge_types in source_edge_types.items():

        a = scc_mapping[
            provider
        ]

        b = scc_mapping[
            consumer
        ]

        if a != b:
            condensation_edges[
                (a, b)
            ].update(
                edge_types
            )

    dot_path = (
        out
        / "boundary-scc.dot"
    )

    with dot_path.open(
        "w"
    ) as f:

        f.write(
            "digraph G {\n"
        )

        f.write(
            'graph [rankdir=LR, '
            'overlap=false, '
            'splines=true, '
            'bgcolor="white"];\n'
        )

        f.write(
            'node [shape=box, '
            'style="rounded,filled", '
            'fontname="Helvetica"];\n'
        )

        f.write(
            'edge [color="#7d8791"];\n'
        )

        for i, component in enumerate(
            components
        ):
            scc = f"SCC{i:04d}"

            cyclic = (
                len(component) > 1
                or any(
                    x
                    in adjacency.get(
                        x,
                        set(),
                    )
                    for x in component
                )
            )

            if len(component) <= 6:
                preview = "\\n".join(
                    component
                )
            else:
                preview = (
                    "\\n".join(
                        component[:5]
                    )
                    + f"\\n... +{len(component)-5}"
                )

            label = (
                f"{scc} · {len(component)} source(s)"
                f"\\n{preview}"
            )

            fill = (
                "#efd9ff"
                if cyclic
                else "#e7eef7"
            )

            penwidth = (
                3
                if cyclic
                else 1
            )

            f.write(
                f"{json.dumps(scc)} ["
                f"label={json.dumps(label)}, "
                f"fillcolor={json.dumps(fill)}, "
                f"penwidth={penwidth}"
                f"];\n"
            )

        for (
            a,
            b,
        ), types in sorted(
            condensation_edges.items()
        ):

            label = ",".join(
                sorted(types)
            )

            f.write(
                f"{json.dumps(a)} -> "
                f"{json.dumps(b)} "
                f"[label={json.dumps(label)}];\n"
            )

        f.write(
            "}\n"
        )

    if not args.no_render:

        dot = shutil.which(
            "dot"
        )

        if dot:

            try:
                subprocess.run(
                    [
                        dot,
                        "-Tsvg",
                        str(dot_path),
                        "-o",
                        str(
                            out
                            / "boundary-scc.svg"
                        ),
                    ],
                    check=True,
                )

            except subprocess.CalledProcessError as exc:

                print(
                    "WARNING: Graphviz rendering failed:"
                )

                print(
                    f"  {exc}"
                )

                print(
                    "DOT file is still available."
                )

    # --------------------------------------------------------------
    # Boundary policy and summary.
    # --------------------------------------------------------------

    max_source_depth = max(
        source_depth.values(),
        default=0,
    )

    max_binary_depth = max(
        binary_depth.values(),
        default=0,
    )

    largest_scc = max(
        (
            len(x)
            for x in components
        ),
        default=0,
    )

    summary = {
        "status":
            (
                "VALID"
                if not unresolved
                else "INVALID"
            ),

        "boundary_definition": {
            "architecture":
                args.arch,
            "os":
                TARGET_OS,
            "target_sources":
                len(base_sources),
            "build_fields":
                list(
                    SOURCE_BUILD_FIELDS
                ),
            "runtime_fields":
                list(
                    BINARY_RUNTIME_FIELDS
                ),
            "profiles":
                sorted(
                    ACTIVE_PROFILES
                ),
            "seed_binary_rules": [
                "Priority: required",
                "Priority: important",
                "Essential: yes",
                "Build-Essential: yes",
            ],
            "excluded": [
                "Build-Depends-Indep",
                "Recommends",
                "Suggests",
                "Enhances",
                "other architectures",
                "test dependencies disabled by nocheck",
                "documentation dependencies disabled by nodoc",
                "native ARM64 boundary excludes Debian cross-compilers and foreign-architecture sysroot packages",
            ],
            "stop_condition":
                (
                    "fixed point: no new required "
                    "source or binary packages"
                ),
        },

        "counts": {
            "boundary_sources":
                len(
                    required_sources
                ),
            "boundary_binaries":
                len(
                    required_binaries
                ),
            "build_edges":
                len(
                    build_edges
                ),
            "runtime_edges":
                len(
                    runtime_edges
                ),
            "source_sccs":
                len(
                    components
                ),
            "cyclic_source_sccs":
                len(
                    cyclic_components
                ),
            "largest_source_scc":
                largest_scc,
            "ambiguous_virtual_choices":
                len(
                    resolver.ambiguous
                ),
            "source_version_mismatches":
                len(
                    version_mismatches
                ),
            "unresolved":
                len(
                    unresolved
                ),
            "closure_rounds":
                round_number,
            "max_source_depth":
                max_source_depth,
            "max_binary_depth":
                max_binary_depth,
        },

        "input_sha256": {
            "base_sources":
                sha256_file(
                    BASE_MANIFEST
                ),
            "sources_raw":
                sha256_file(
                    SOURCE_UNIVERSE
                ),
            "binaries_raw":
                sha256_file(
                    BINARY_UNIVERSE
                ),
        },
    }

    (
        out
        / "summary.json"
    ).write_text(
        json.dumps(
            summary,
            indent=2,
        )
        + "\n"
    )

    boundary_md = f"""# R-Distro ARM64 Self-Hosting Boundary

Status: **{summary["status"]}**

## Definition

R-Distro's ARM64 base system is considered self-hosting when every binary
inside this boundary can be produced from a source package inside the same
boundary and every source package can be architecture-built using only
binaries inside the same boundary.

### Included

- The {len(base_sources)} source packages in `manifests/base-sources.txt`.
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

- Boundary sources: **{len(required_sources)}**
- Boundary binaries: **{len(required_binaries)}**
- Build dependency edges: **{len(build_edges)}**
- Runtime dependency edges: **{len(runtime_edges)}**
- Source SCCs: **{len(components)}**
- Cyclic SCCs: **{len(cyclic_components)}**
- Largest SCC: **{largest_scc}**
- Closure rounds: **{round_number}**
- Unresolved relations: **{len(unresolved)}**
- Ambiguous virtual choices: **{len(resolver.ambiguous)}**
- Source-version mismatches: **{len(version_mismatches)}**

## Interpretation

`boundary-source-names.txt` is the canonical R-Distro self-hosting source
boundary for this snapshot and architecture.

This is not the full Debian archive and it is not the closure required to
build every architecture-independent/documentation package. It is the
explicit boundary chosen for rebuilding the ARM64 R-Distro base system.
"""

    (
        out
        / "BOUNDARY.md"
    ).write_text(
        boundary_md
    )

    # --------------------------------------------------------------
    # Final console result.
    # --------------------------------------------------------------

    print()
    print(
        "========================================"
    )

    print(
        " FINAL BOUNDARY"
    )

    print(
        "========================================"
    )

    print()

    print(
        f"Status:                    {summary['status']}"
    )

    print(
        f"Boundary sources:          {len(required_sources)}"
    )

    print(
        f"Boundary binaries:         {len(required_binaries)}"
    )

    print(
        f"Build edges:               {len(build_edges)}"
    )

    print(
        f"Runtime edges:             {len(runtime_edges)}"
    )

    print(
        f"Closure rounds:            {round_number}"
    )

    print(
        f"Source SCCs:               {len(components)}"
    )

    print(
        f"Cyclic SCCs:               {len(cyclic_components)}"
    )

    print(
        f"Largest SCC:               {largest_scc}"
    )

    print(
        f"Ambiguous virtual choices: {len(resolver.ambiguous)}"
    )

    print(
        f"Source version mismatches: {len(version_mismatches)}"
    )

    print(
        f"Unresolved relations:      {len(unresolved)}"
    )

    print()
    print(
        f"Output: {out}"
    )

    print()
    print(
        "Canonical boundary manifest:"
    )

    print(
        f"  {out / 'boundary-source-names.txt'}"
    )

    print()
    print(
        "Read:"
    )

    print(
        f"  {out / 'BOUNDARY.md'}"
    )
    
    if args.why:
        explain_why_source(
            args.why,
            required_sources,
            base_source_set,
            seed_rows,
            build_edges,
            runtime_edges,
        )

    if unresolved:

        print()
        print(
            "ERROR: boundary is INVALID because "
            "some dependency relations could not "
            "be resolved."
        )

        print(
            f"Inspect: {out / 'unresolved.csv'}"
        )

        raise SystemExit(2)


if __name__ == "__main__":
    main()
