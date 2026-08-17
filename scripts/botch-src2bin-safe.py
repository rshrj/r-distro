#!/usr/bin/env python3

import re
import sys
from pathlib import Path


def paragraphs(path):
    text = Path(path).read_text(errors="replace")

    for p in re.split(r"\n\s*\n", text):
        if p.strip():
            yield p.strip() + "\n\n"


def fields(paragraph):
    out = {}
    current = None

    for line in paragraph.splitlines():
        if line.startswith((" ", "\t")) and current:
            out[current] += "\n" + line.strip()
            continue

        if ":" in line:
            k, v = line.split(":", 1)
            current = k
            out[k] = v.strip()

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
        f"usage: {sys.argv[0]} Sources Packages"
    )

sources_file = sys.argv[1]
packages_file = sys.argv[2]


# Exact source package/version pairs we currently own.
wanted = set()

for p in paragraphs(sources_file):
    f = fields(p)

    if "Package" in f and "Version" in f:
        wanted.add(
            (f["Package"], f["Version"])
        )


selected = 0

for p in paragraphs(packages_file):
    f = fields(p)

    if "Package" not in f or "Version" not in f:
        continue

    source, source_version = binary_source_ref(f)

    if (source, source_version) in wanted:
        sys.stdout.write(p)
        selected += 1

print(
    f"[safe-src2bin] selected {selected} binary records "
    f"from {len(wanted)} source records",
    file=sys.stderr,
)
