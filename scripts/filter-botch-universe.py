#!/usr/bin/env python3

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NODES = ROOT / "analysis/bootstrap/recursive-full/nodes.csv"
SOURCES_IN = ROOT / "analysis/bootstrap/universe-cache/sources.raw"
PACKAGES_IN = ROOT / "analysis/bootstrap/universe-cache/binaries.raw"

OUT = ROOT / "analysis/bootstrap/botch-rdistro"
OUT.mkdir(parents=True, exist_ok=True)


def paragraphs(path):
    text = path.read_text(errors="replace")

    for p in re.split(r"\n\s*\n", text):
        p = p.strip()
        if p:
            yield p + "\n\n"


def field(paragraph, name):
    m = re.search(
        rf"^{re.escape(name)}:\s*(.+)$",
        paragraph,
        flags=re.MULTILINE,
    )
    return m.group(1).strip() if m else ""


wanted = set()

with NODES.open() as f:
    for row in csv.DictReader(f):
        wanted.add(row["source"])


print(f"Closure source packages: {len(wanted)}")


# ------------------------------------------------------------
# Sources
# ------------------------------------------------------------

source_count = 0

with (OUT / "Sources").open("w") as out:
    for p in paragraphs(SOURCES_IN):
        source = field(p, "Package")

        if source in wanted:
            out.write(p)
            source_count += 1


# ------------------------------------------------------------
# Packages
# ------------------------------------------------------------

binary_count = 0

with (OUT / "Packages").open("w") as out:
    for p in paragraphs(PACKAGES_IN):

        binary = field(p, "Package")
        raw_source = field(p, "Source")

        if raw_source:
            source = raw_source.split()[0]
        else:
            source = binary

        if source in wanted:
            out.write(p)
            binary_count += 1


print(f"Filtered Sources records: {source_count}")
print(f"Filtered binary records:  {binary_count}")

print(f"Output: {OUT}")
