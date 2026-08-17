#!/usr/bin/env python3

import re
import sys
from pathlib import Path


def paragraphs(path):
    text = Path(path).read_text(errors="replace")

    for p in re.split(r"\n\s*\n", text):
        p = p.strip()

        if p:
            yield p + "\n\n"


def fields(paragraph):
    out = {}
    current = None

    for line in paragraph.splitlines():
        if line.startswith((" ", "\t")) and current:
            out[current] += "\n" + line.strip()
            continue

        if ":" in line:
            key, value = line.split(":", 1)
            current = key
            out[key] = value.strip()

    return out


def binary_source_ref(f):
    binary = f["Package"]
    binary_version = f["Version"]

    raw = f.get("Source", "").strip()

    if not raw:
        return binary, binary_version

    m = re.fullmatch(
        r"([a-z0-9][a-z0-9+.-]*)(?:\s+\(([^)]+)\))?",
        raw,
    )

    if not m:
        raise RuntimeError(
            f"Malformed Source field for {binary}: {raw!r}"
        )

    source = m.group(1)
    source_version = m.group(2) or binary_version

    return source, source_version


if len(sys.argv) != 3:
    raise SystemExit(
        f"usage: {sys.argv[0]} Packages Sources"
    )

packages_file = sys.argv[1]
sources_file = sys.argv[2]


# ------------------------------------------------------------------
# Index all source records
# ------------------------------------------------------------------

by_exact = {}
by_name = {}

for paragraph in paragraphs(sources_file):
    f = fields(paragraph)

    name = f.get("Package")
    version = f.get("Version")

    if not name or not version:
        continue

    by_exact[(name, version)] = paragraph
    by_name.setdefault(name, []).append(
        (version, paragraph)
    )


# ------------------------------------------------------------------
# Resolve every needed binary -> source
# ------------------------------------------------------------------

selected = {}
fallback_count = 0

for paragraph in paragraphs(packages_file):
    f = fields(paragraph)

    if "Package" not in f or "Version" not in f:
        continue

    binary = f["Package"]
    binary_version = f["Version"]

    source, source_version = binary_source_ref(f)

    key = (source, source_version)

    # Normal / explicit Source-version case.
    source_paragraph = by_exact.get(key)

    # Some binary records may lack Source: (...) even for a binNMU.
    # Try stripping the standard +bN binary-only rebuild suffix.
    if source_paragraph is None:
        stripped = re.sub(
            r"\+b[0-9]+$",
            "",
            source_version,
        )

        source_paragraph = by_exact.get(
            (source, stripped)
        )

        if source_paragraph is not None:
            source_version = stripped
            key = (source, source_version)
            fallback_count += 1

    # If the snapshot contains exactly one version of the source,
    # there is no ambiguity.
    if source_paragraph is None:
        candidates = by_name.get(source, [])

        if len(candidates) == 1:
            source_version, source_paragraph = candidates[0]
            key = (source, source_version)
            fallback_count += 1

    if source_paragraph is None:
        versions = [
            version
            for version, _ in by_name.get(source, [])
        ]

        raise RuntimeError(
            "\nCannot map binary to source:\n"
            f"  binary:          {binary}\n"
            f"  binary version:  {binary_version}\n"
            f"  requested source:{source}\n"
            f"  source version:  {source_version}\n"
            f"  available source versions: {versions}\n"
        )

    selected[key] = source_paragraph


for key in sorted(selected):
    sys.stdout.write(selected[key])

print(
    f"[safe-bin2src] mapped to {len(selected)} source records "
    f"({fallback_count} fallback mappings)",
    file=sys.stderr,
)
