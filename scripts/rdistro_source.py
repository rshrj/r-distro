#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile

from pathlib import Path


# ======================================================================
# Paths / defaults
# ======================================================================

ROOT = Path(__file__).resolve().parents[1]

PACKAGES_ROOT = ROOT / "packages"
OVERRIDES_ROOT = ROOT / "overrides"

EDIT_ROOT = ROOT / "work" / "edit"
SOURCE_TMP_ROOT = ROOT / "work" / "source-tmp"

DEFAULT_BUILDROOT_IMAGE = "rdistro-buildroot:2026-08-13"

DEFAULT_RDISTRO_REPO_URL = (
    "http://host.docker.internal:8080/"
)

DEFAULT_PUBLIC_KEY = (
    ROOT
    / "repo"
    / "keys"
    / "rdistro-archive.asc"
)

EDIT_METADATA_FILE = ".git/rdistro-edit.json"

SOURCE_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9+.-]*$"
)


# ======================================================================
# Generic helpers
# ======================================================================

def die(message: str) -> None:
    raise SystemExit(message)


def run(
    command,
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    capture: bool = False,
    check: bool = True,
):
    if capture:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
    )


def require_program(name: str) -> None:
    if shutil.which(name) is None:
        die(
            f"Required program not found in PATH: {name}"
        )


def validate_source_name(name: str) -> str:
    if not SOURCE_NAME_RE.fullmatch(name):
        die(
            f"Invalid Debian source package name: {name!r}"
        )

    return name


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def git(
    *args: str,
    cwd: Path,
    capture: bool = False,
    check: bool = True,
):
    return run(
        ["git", *args],
        cwd=cwd,
        capture=capture,
        check=check,
    )


def git_output(
    *args: str,
    cwd: Path,
) -> str:
    result = git(
        *args,
        cwd=cwd,
        capture=True,
    )

    return result.stdout.strip()


def run_debian_tool(
    args: list[str],
    *,
    mount: Path | None = None,
    buildroot_image: str = DEFAULT_BUILDROOT_IMAGE,
    capture: bool = False,
):
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
    ]

    if mount is not None:
        command += [
            "-v",
            f"{mount.resolve()}:/src",
            "-w",
            "/src",
        ]

    command += [
        buildroot_image,
        *args,
    ]

    return run(
        command,
        capture=capture,
    )


# ======================================================================
# Source origin
# ======================================================================

def native_package_path(source: str) -> Path:
    return PACKAGES_ROOT / source


def override_path(source: str) -> Path:
    return OVERRIDES_ROOT / source


def patch_root(source: str) -> Path:
    return override_path(source) / "patches"


def patch_series_path(source: str) -> Path:
    return patch_root(source) / "series"


def edit_path(source: str) -> Path:
    return EDIT_ROOT / source


def source_origin(source: str) -> str:
    """
    Returns one of:

        native
        debian
        debian+override
    """

    if native_package_path(source).is_dir():
        return "native"

    if override_path(source).is_dir():
        return "debian+override"

    return "debian"


# ======================================================================
# Patch handling
# ======================================================================

def read_patch_series(source: str) -> list[str]:
    series = patch_series_path(source)

    if not series.is_file():
        return []

    result = []

    for raw in series.read_text().splitlines():
        line = raw.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        # We intentionally support a simple series file:
        #
        #     0001-foo.patch
        #     0002-bar.patch
        #
        # Quilt-style per-patch arguments are deliberately not
        # supported here.
        if any(c.isspace() for c in line):
            die(
                f"Unsupported patch-series entry "
                f"for {source}: {line!r}\n"
                "R-Distro series entries must contain "
                "only the patch filename."
            )

        result.append(line)

    return result


def validate_patch_series(source: str) -> list[Path]:
    names = read_patch_series(source)
    root = patch_root(source)

    seen = set()
    result = []

    for name in names:
        if name in seen:
            die(
                f"Duplicate patch in "
                f"{relative(patch_series_path(source))}: "
                f"{name}"
            )

        seen.add(name)

        path = root / name

        if not path.is_file():
            die(
                f"Patch listed in series does not exist: "
                f"{relative(path)}"
            )

        result.append(path)

    return result


def override_digest(source: str) -> str:
    """
    Digest exactly the committed R-Distro patch input for SOURCE.

    Used to detect an edit tree whose external baseline changed
    after source-edit.
    """

    h = hashlib.sha256()

    series = patch_series_path(source)

    if not series.exists():
        h.update(b"no-series\n")
        return h.hexdigest()

    h.update(b"series\0")
    h.update(series.read_bytes())
    h.update(b"\0")

    for patch in validate_patch_series(source):
        h.update(patch.name.encode())
        h.update(b"\0")
        h.update(patch.read_bytes())
        h.update(b"\0")

    return h.hexdigest()


def apply_override_patches(
    source: str,
    tree: Path,
) -> None:
    patches = validate_patch_series(source)

    if not patches:
        return

    require_program("git")

    for patch in patches:
        print(
            f"Applying R-Distro patch: "
            f"{relative(patch)}"
        )

        check = git(
            "apply",
            "--check",
            "--binary",
            str(patch),
            cwd=tree,
            capture=True,
            check=False,
        )

        if check.returncode != 0:
            message = (
                check.stderr.strip()
                or check.stdout.strip()
            )

            die(
                f"Cannot apply R-Distro patch:\n"
                f"  {relative(patch)}\n\n"
                f"{message}"
            )

        git(
            "apply",
            "--binary",
            str(patch),
            cwd=tree,
        )


# ======================================================================
# Debian source acquisition
# ======================================================================

def find_extracted_source(directory: Path) -> Path:
    candidates = []

    for child in directory.iterdir():
        if (
            child.is_dir()
            and
            (child / "debian" / "changelog").is_file()
        ):
            candidates.append(child)

    if len(candidates) != 1:
        formatted = "\n".join(
            f"  {x.name}"
            for x in sorted(candidates)
        )

        die(
            "Expected exactly one extracted Debian "
            "source directory, found "
            f"{len(candidates)}.\n"
            f"{formatted}"
        )

    return candidates[0]


def fetch_debian_source(
    source: str,
    destination: Path,
    *,
    buildroot_image: str,
) -> None:
    """
    Fetch SOURCE using the pinned APT configuration embedded in the
    R-Distro buildroot.

    The host does not independently select a Debian mirror/snapshot;
    therefore source-edit sees the same Debian source universe as the
    official package builder.
    """

    require_program("docker")

    SOURCE_TMP_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f"{source}-",
            dir=SOURCE_TMP_ROOT,
        )
    )

    uid = os.getuid()
    gid = os.getgid()

    print(
        f"Fetching pinned Debian source: {source}"
    )

    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",

        "-e",
        f"PKG={source}",

        "-e",
        f"HOST_UID={uid}",

        "-e",
        f"HOST_GID={gid}",

        "-v",
        f"{temporary.resolve()}:/out",

        buildroot_image,

        "bash",
        "-lc",
        r'''
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get \
    -o Acquire::Retries=5 \
    update \
    --error-on=any

cd /out

apt-get source "$PKG"

# Bind-mounted files created as root are inconvenient on a normal
# Linux host. Docker Desktop on macOS largely hides this, but fixing
# ownership makes the workflow portable.
chown -R "$HOST_UID:$HOST_GID" /out
''',
    ]

    try:
        run(
            command,
            cwd=ROOT,
        )

        extracted = find_extracted_source(
            temporary
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.move(
            str(extracted),
            str(destination),
        )

    finally:
        remove_tree(temporary)


# ======================================================================
# Edit-tree metadata
# ======================================================================

def metadata_path(tree: Path) -> Path:
    return tree / EDIT_METADATA_FILE


def read_edit_metadata(
    tree: Path,
) -> dict:
    path = metadata_path(tree)

    if not path.is_file():
        die(
            f"Not an R-Distro edit tree: "
            f"{relative(tree)}\n"
            "Run source-edit again."
        )

    try:
        return json.loads(
            path.read_text()
        )
    except Exception as exc:
        die(
            f"Invalid edit metadata "
            f"{relative(path)}: {exc}"
        )


def write_edit_metadata(
    tree: Path,
    metadata: dict,
) -> None:
    path = metadata_path(tree)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            metadata,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def source_version(tree: Path) -> str:
    changelog = tree / "debian" / "changelog"

    if not changelog.is_file():
        return "unknown"

    for raw in changelog.read_text(
        errors="replace"
    ).splitlines():
        line = raw.strip()

        if not line:
            continue

        match = re.match(
            r"^[^\s]+\s+\(([^)]+)\)",
            line,
        )

        if match:
            return match.group(1)

        break

    return "unknown"


# ======================================================================
# Disposable local git baseline
# ======================================================================

def initialize_edit_git(
    source: str,
    tree: Path,
    origin: str,
) -> None:
    require_program("git")

    # apt source should not contain .git, and native package copies
    # explicitly exclude it, but refuse rather than silently destroy
    # an unexpected repository.
    if (tree / ".git").exists():
        die(
            f"Unexpected .git already present in "
            f"{relative(tree)}"
        )

    git(
        "init",
        "-q",
        cwd=tree,
    )

    git(
        "add",
        "-A",
        cwd=tree,
    )

    git(
        "-c",
        "user.name=R-Distro Source Workspace",
        "-c",
        "user.email=workspace@rdistro.invalid",
        "commit",
        "-q",
        "-m",
        "R-Distro editable source baseline",
        cwd=tree,
    )

    metadata = {
        "schema": 1,
        "source": source,
        "origin": origin,
        "source_version": source_version(tree),
        "override_digest": (
            override_digest(source)
            if origin != "native"
            else None
        ),
        "created_at": (
            dt.datetime.now()
            .astimezone()
            .isoformat(timespec="seconds")
        ),
        "baseline_commit": git_output(
            "rev-parse",
            "HEAD",
            cwd=tree,
        ),
    }

    write_edit_metadata(
        tree,
        metadata,
    )


def ensure_clean_external_baseline(
    source: str,
    tree: Path,
    metadata: dict,
) -> None:
    """
    Refuse source-save if tracked patches changed outside this edit
    session after source-edit.

    Otherwise a patch generated against the stale working baseline
    might not apply to the current R-Distro source definition.
    """

    if metadata["origin"] == "native":
        return

    expected = metadata.get(
        "override_digest"
    )

    current = override_digest(source)

    if expected != current:
        die(
            "The committed R-Distro patch baseline changed "
            "since this edit tree was created.\n\n"
            f"Edit tree: {relative(tree)}\n"
            f"Package:   {source}\n\n"
            "Do not save this tree blindly. Review/copy your "
            "changes, then recreate the workspace with:\n\n"
            f"  python3 scripts/rdistroctl.py "
            f"source-edit {shlex.quote(source)} --force"
        )


# ======================================================================
# source-edit
# ======================================================================

def command_source_edit(args) -> None:
    source = validate_source_name(
        args.source
    )

    tree = edit_path(source)

    if tree.exists():
        if not args.force:
            die(
                f"Edit tree already exists:\n"
                f"  {relative(tree)}\n\n"
                "Use source-status to inspect it, or "
                "source-edit --force to discard it and "
                "start again."
            )

        print(
            f"Removing existing edit tree: "
            f"{relative(tree)}"
        )

        remove_tree(tree)

    origin = source_origin(source)

    EDIT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if origin == "native":
        src = native_package_path(source)

        print(
            f"Materializing R-Distro-native source: "
            f"{relative(src)}"
        )

        shutil.copytree(
            src,
            tree,
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                "*.pyc",
            ),
        )

    else:
        fetch_debian_source(
            source,
            tree,
            buildroot_image=
                args.buildroot_image,
        )

        if origin == "debian+override":
            apply_override_patches(
                source,
                tree,
            )

    initialize_edit_git(
        source,
        tree,
        origin,
    )

    print()
    print("Editable source ready")
    print(f"  source:   {source}")
    print(f"  origin:   {origin}")
    print(
        f"  version:  "
        f"{source_version(tree)}"
    )
    print(
        f"  patches:  "
        f"{len(read_patch_series(source))}"
    )
    print(f"  path:     {relative(tree)}")
    print()
    print("Edit normally, then inspect with:")
    print(
        f"  python3 scripts/rdistroctl.py "
        f"source-status {source}"
    )
    print()
    print("For an incremental build environment:")
    print(
        f"  python3 scripts/rdistroctl.py "
        f"source-shell {source}"
    )
    print()
    print("When satisfied:")
    print(
        f"  python3 scripts/rdistroctl.py "
        f"source-save {source}"
    )


# ======================================================================
# source-status
# ======================================================================

def command_source_status(args) -> None:
    source = validate_source_name(
        args.source
    )

    tree = edit_path(source)

    if not tree.is_dir():
        die(
            f"No edit tree for {source}.\n"
            f"Run:\n"
            f"  python3 scripts/rdistroctl.py "
            f"source-edit {source}"
        )

    metadata = read_edit_metadata(tree)

    status = git_output(
        "status",
        "--short",
        "--untracked-files=all",
        cwd=tree,
    )

    print()
    print("========================================")
    print(" R-Distro editable source")
    print("========================================")
    print(f"Source:       {source}")
    print(
        f"Origin:       "
        f"{metadata['origin']}"
    )
    print(
        f"Version:      "
        f"{metadata.get('source_version', 'unknown')}"
    )
    print(
        f"Patch count:  "
        f"{len(read_patch_series(source))}"
    )
    print(f"Edit tree:    {relative(tree)}")

    if metadata["origin"] != "native":
        current = override_digest(source)

        baseline_ok = (
            current
            ==
            metadata.get("override_digest")
        )

        print(
            "Patch baseline:"
            f" {'current' if baseline_ok else 'CHANGED'}"
        )

    print()
    print("Local changes:")

    if status:
        print(status)
    else:
        print("  (clean)")


# ======================================================================
# Patch naming
# ======================================================================

def slugify_patch_name(value: str) -> str:
    value = value.strip().lower()

    value = re.sub(
        r"[^a-z0-9.+-]+",
        "-",
        value,
    )

    value = value.strip("-")

    if not value:
        value = "rdistro-changes"

    return value


def next_patch_name(
    source: str,
    requested: str | None,
) -> str:
    existing = read_patch_series(source)

    number = len(existing) + 1

    if requested:
        name = slugify_patch_name(
            requested
        )
    else:
        name = "rdistro-changes"

    if name.endswith(".patch"):
        name = name[:-6]

    return (
        f"{number:04d}-"
        f"{name}.patch"
    )


# ======================================================================
# Save Debian-derived changes as patch
# ======================================================================

def save_debian_override(
    source: str,
    tree: Path,
    metadata: dict,
    *,
    patch_name: str | None,
) -> None:
    ensure_clean_external_baseline(
        source,
        tree,
        metadata,
    )

    git(
        "add",
        "-A",
        cwd=tree,
    )

    diff = run(
        [
            "git",
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "HEAD",
        ],
        cwd=tree,
        capture=True,
    ).stdout

    if not diff:
        print(
            f"No unsaved source changes for {source}."
        )
        return

    root = patch_root(source)

    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    series = patch_series_path(source)

    name = next_patch_name(
        source,
        patch_name,
    )

    patch = root / name

    if patch.exists():
        die(
            f"Refusing to overwrite existing patch: "
            f"{relative(patch)}"
        )

    patch.write_text(diff)

    # Verify the generated patch is applicable to exactly the local
    # baseline from which it was generated.
    check = git(
        "apply",
        "--check",
        "--binary",
        "--reverse",
        str(patch),
        cwd=tree,
        capture=True,
        check=False,
    )

    # Because TREE currently contains the edited state, reverse
    # application must succeed. This catches malformed/truncated
    # generated patches.
    if check.returncode != 0:
        patch.unlink(
            missing_ok=True
        )

        message = (
            check.stderr.strip()
            or check.stdout.strip()
        )

        die(
            "Generated patch failed verification:\n"
            f"{message}"
        )

    old_series = (
        series.read_text()
        if series.exists()
        else ""
    )

    try:
        with series.open(
            "a",
            encoding="utf-8",
        ) as f:
            if (
                old_series
                and
                not old_series.endswith("\n")
            ):
                f.write("\n")

            f.write(name + "\n")

        # Locally commit the just-saved state. This commit exists only
        # inside work/edit/<source>/.git and lets the next source-save
        # generate a delta relative to this save rather than repeating
        # earlier changes.
        git(
            "-c",
            "user.name=R-Distro Source Workspace",
            "-c",
            "user.email=workspace@rdistro.invalid",
            "commit",
            "-q",
            "-m",
            f"Saved as {name}",
            cwd=tree,
        )

        metadata["override_digest"] = (
            override_digest(source)
        )

        metadata["baseline_commit"] = (
            git_output(
                "rev-parse",
                "HEAD",
                cwd=tree,
            )
        )

        metadata["saved_at"] = (
            dt.datetime.now()
            .astimezone()
            .isoformat(timespec="seconds")
        )

        write_edit_metadata(
            tree,
            metadata,
        )

    except Exception:
        patch.unlink(
            missing_ok=True
        )

        series.write_text(
            old_series
        )

        raise

    print()
    print("R-Distro patch created")
    print(f"  source: {source}")
    print(
        f"  patch:  "
        f"{relative(patch)}"
    )
    print(
        f"  series: "
        f"{relative(series)}"
    )
    print()
    print("Review before committing:")
    print(
        f"  git diff -- "
        f"{relative(override_path(source))}"
    )


# ======================================================================
# Save R-Distro-native source
# ======================================================================

def copy_tree_without_git(
    source: Path,
    destination: Path,
) -> None:
    remove_tree(destination)

    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            "__pycache__",
            "*.pyc",
        ),
    )


def save_native_source(
    source: str,
    tree: Path,
) -> None:
    git(
        "add",
        "-A",
        cwd=tree,
    )

    diff = git_output(
        "diff",
        "--cached",
        "--name-only",
        "HEAD",
        cwd=tree,
    )

    if not diff:
        print(
            f"No unsaved source changes for {source}."
        )
        return

    destination = native_package_path(
        source
    )

    temporary = (
        PACKAGES_ROOT
        / f".{source}.save-{os.getpid()}"
    )

    remove_tree(temporary)

    shutil.copytree(
        tree,
        temporary,
        ignore=shutil.ignore_patterns(
            ".git",
            "__pycache__",
            "*.pyc",
        ),
    )

    backup = None

    if destination.exists():
        backup = (
            PACKAGES_ROOT
            / f".{source}.backup-{os.getpid()}"
        )

        remove_tree(backup)

        os.rename(
            destination,
            backup,
        )

    try:
        os.rename(
            temporary,
            destination,
        )

        if backup is not None:
            remove_tree(backup)

    except Exception:
        if destination.exists():
            remove_tree(destination)

        if backup is not None:
            os.rename(
                backup,
                destination,
            )

        remove_tree(temporary)

        raise

    git(
        "-c",
        "user.name=R-Distro Source Workspace",
        "-c",
        "user.email=workspace@rdistro.invalid",
        "commit",
        "-q",
        "-m",
        "Saved native R-Distro source",
        cwd=tree,
    )

    metadata = read_edit_metadata(tree)

    metadata["baseline_commit"] = (
        git_output(
            "rev-parse",
            "HEAD",
            cwd=tree,
        )
    )

    metadata["saved_at"] = (
        dt.datetime.now()
        .astimezone()
        .isoformat(timespec="seconds")
    )

    write_edit_metadata(
        tree,
        metadata,
    )

    print()
    print("R-Distro native source updated")
    print(f"  source: {source}")
    print(
        f"  path:   "
        f"{relative(destination)}"
    )
    print()
    print("Review before committing:")
    print(
        f"  git diff -- "
        f"{relative(destination)}"
    )


# ======================================================================
# source-save
# ======================================================================

def command_source_save(args) -> None:
    source = validate_source_name(
        args.source
    )

    tree = edit_path(source)

    if not tree.is_dir():
        die(
            f"No edit tree for {source}."
        )

    metadata = read_edit_metadata(tree)

    if metadata.get("source") != source:
        die(
            "Edit metadata source mismatch."
        )

    origin = metadata.get("origin")

    if origin == "native":
        save_native_source(
            source,
            tree,
        )

        return

    if origin not in {
        "debian",
        "debian+override",
    }:
        die(
            f"Unknown edit origin: {origin!r}"
        )

    save_debian_override(
        source,
        tree,
        metadata,
        patch_name=args.name,
    )


# ======================================================================
# source-shell
# ======================================================================

def command_source_shell(args) -> None:
    source = validate_source_name(
        args.source
    )

    if args.use_rdistro and not args.repo_suite:
        die(
            "--repo-suite is required when "
            "--use-rdistro is enabled"
        )

    tree = edit_path(source)

    if not tree.is_dir():
        die(
            f"No edit tree for {source}.\n"
            "Run source-edit first."
        )

    read_edit_metadata(tree)

    require_program("docker")

    public_key = Path(
        args.public_key
    ).resolve()

    command = [
        "docker",
        "run",
        "--rm",
        "-it",
        "--platform",
        "linux/arm64",

        "-e",
        f"PKG={source}",

        "-e",
        (
            "USE_RDISTRO="
            f"{'1' if args.use_rdistro else '0'}"
        ),

        "-e",
        (
            "RDISTRO_REPO_URL="
            f"{args.repo_url}"
        ),

        "-e",
        (
            "RDISTRO_REPO_SUITE="
            f"{args.repo_suite}"
        ),

        "-e",
        (
            "DEB_BUILD_OPTIONS="
            f"{args.deb_build_options}"
        ),

        "-e",
        (
            "DEB_BUILD_PROFILES="
            f"{args.deb_build_profiles}"
        ),

        "-v",
        (
            f"{tree.resolve()}:"
            "/build/src"
        ),
    ]

    if (
        args.use_rdistro
        and
        public_key.is_file()
    ):
        command += [
            "-v",
            (
                f"{public_key}:"
                "/etc/apt/keyrings/"
                "rdistro-archive.asc:ro"
            ),
        ]

    command += [
        args.buildroot_image,
        "bash",
        "-lc",
        r'''
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

if [ "$USE_RDISTRO" = "1" ]; then
    if [ ! -f \
        /etc/apt/keyrings/rdistro-archive.asc ]; then
        echo >&2 \
            "R-Distro public APT key is not mounted"
        exit 2
    fi

    cat >/etc/apt/sources.list.d/rdistro.sources <<EOF
Types: deb deb-src
URIs: $RDISTRO_REPO_URL
Suites: $RDISTRO_REPO_SUITE
Components: main
Signed-By: /etc/apt/keyrings/rdistro-archive.asc
EOF
fi

apt-get \
    -o Acquire::Retries=5 \
    update \
    --error-on=any

apt-get install \
    -y \
    --no-install-recommends \
    build-essential \
    devscripts \
    equivs \
    fakeroot \
    git \
    quilt

# Install package build dependencies without creating generated
# helper files in the editable source tree.
mkdir -p /tmp/rdistro-builddeps
cd /tmp/rdistro-builddeps

export DEB_BUILD_OPTIONS
export DEB_BUILD_PROFILES

mk-build-deps \
    --install \
    --remove \
    --tool \
    "apt-get -y --no-install-recommends" \
    /build/src/debian/control

chown -R builder:builder /build/src
chown builder:builder /build

cd /build/src

echo
echo "========================================"
echo " R-Distro incremental source shell"
echo "========================================"
echo
echo "Source:             $PKG"
echo "Tree:               /build/src"
echo "DEB_BUILD_OPTIONS:  $DEB_BUILD_OPTIONS"
echo "DEB_BUILD_PROFILES: $DEB_BUILD_PROFILES"
echo
echo "The source tree is persistent."
echo "Compiler/build outputs remain here after"
echo "the container exits."
echo

exec runuser -u builder -- \
    env \
        HOME=/home/builder \
        DEB_BUILD_OPTIONS="$DEB_BUILD_OPTIONS" \
        DEB_BUILD_PROFILES="$DEB_BUILD_PROFILES" \
        bash
''',
    ]

    run(
        command,
        cwd=ROOT,
    )


# ======================================================================
# source-discard
# ======================================================================

def command_source_discard(args) -> None:
    source = validate_source_name(
        args.source
    )

    tree = edit_path(source)

    if not tree.exists():
        print(
            f"No edit tree exists for {source}."
        )
        return

    if not args.force:
        status = git_output(
            "status",
            "--short",
            "--untracked-files=all",
            cwd=tree,
        )

        if status:
            die(
                "Edit tree contains unsaved changes.\n\n"
                f"{status}\n\n"
                "Use --force if you really want to "
                "discard them."
            )

    remove_tree(tree)

    print(
        f"Discarded edit tree: "
        f"{relative(tree)}"
    )


# ======================================================================
# source-origin
# ======================================================================

def command_source_origin(args) -> None:
    source = validate_source_name(
        args.source
    )

    origin = source_origin(source)

    print(origin)

    if origin == "native":
        print(
            f"  {relative(native_package_path(source))}"
        )

    elif origin == "debian+override":
        print(
            "  pinned Debian source"
        )

        print(
            f"  + "
            f"{relative(override_path(source))}"
        )

    else:
        print(
            "  pinned Debian source"
        )


# ======================================================================
# package-new
# ======================================================================

def debian_timestamp() -> str:
    return dt.datetime.now(
        dt.timezone.utc
    ).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )


def command_package_new(args) -> None:
    source = validate_source_name(
        args.source
    )

    destination = native_package_path(
        source
    )

    if destination.exists():
        die(
            f"Package already exists: "
            f"{relative(destination)}"
        )

    if override_path(source).exists():
        die(
            f"An override already exists for {source}: "
            f"{relative(override_path(source))}"
        )

    debian = destination / "debian"

    (
        debian
        / "source"
    ).mkdir(
        parents=True,
        exist_ok=False,
    )

    control = f"""\
Source: {source}
Section: {args.section}
Priority: optional
Maintainer: {args.maintainer}
Build-Depends: debhelper-compat (= 13)
Standards-Version: 4.7.0
Rules-Requires-Root: no

Package: {source}
Architecture: all
Depends: ${{misc:Depends}}
Description: {args.description}
 R-Distro native package.
"""

    (
        debian
        / "control"
    ).write_text(
        control
    )

    changelog = f"""\
{source} ({args.version}) unstable; urgency=medium

  * Initial R-Distro package.

 -- {args.maintainer}  {debian_timestamp()}
"""

    (
        debian
        / "changelog"
    ).write_text(
        changelog
    )

    rules = (
        "#!/usr/bin/make -f\n"
        "\n"
        "%:\n"
        "\tdh $@\n"
    )

    rules_path = (
        debian
        / "rules"
    )

    rules_path.write_text(
        rules
    )

    rules_path.chmod(
        0o755
    )

    (
        debian
        / "source"
        / "format"
    ).write_text(
        "3.0 (native)\n"
    )

    (
        destination
        / "README.md"
    ).write_text(
        f"# {source}\n\n"
        "R-Distro native package.\n"
    )

    print()
    print("Created R-Distro native package")
    print(f"  source:  {source}")
    print(
        f"  version: {args.version}"
    )
    print(
        f"  path:    "
        f"{relative(destination)}"
    )
    print()
    print("Edit it directly, or create a workspace:")
    print(
        f"  python3 scripts/rdistroctl.py "
        f"source-edit {source}"
    )


# ======================================================================
# CLI registration
# ======================================================================

def register_subcommands(
    subparsers,
) -> None:

    # --------------------------------------------------------------
    # source-edit
    # --------------------------------------------------------------

    p = subparsers.add_parser(
        "source-edit",
        help=(
            "materialize the current R-Distro "
            "source into work/edit/"
        ),
    )

    p.add_argument(
        "source",
    )

    p.add_argument(
        "--buildroot-image",
        default=DEFAULT_BUILDROOT_IMAGE,
    )

    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "discard an existing edit tree "
            "and recreate it"
        ),
    )

    p.set_defaults(
        func=command_source_edit,
    )

    # --------------------------------------------------------------
    # source-status
    # --------------------------------------------------------------

    p = subparsers.add_parser(
        "source-status",
        help=(
            "show the state of an editable "
            "R-Distro source tree"
        ),
    )

    p.add_argument(
        "source",
    )

    p.set_defaults(
        func=command_source_status,
    )

    # --------------------------------------------------------------
    # source-save
    # --------------------------------------------------------------

    p = subparsers.add_parser(
        "source-save",
        help=(
            "save edit-tree changes into "
            "tracked R-Distro source inputs"
        ),
    )

    p.add_argument(
        "source",
    )

    p.add_argument(
        "--name",
        default=None,
        help=(
            "short patch description, e.g. "
            "'use-rdistro-branding'"
        ),
    )

    p.set_defaults(
        func=command_source_save,
    )

    # --------------------------------------------------------------
    # source-shell
    # --------------------------------------------------------------

    p = subparsers.add_parser(
        "source-shell",
        help=(
            "open a persistent incremental "
            "build shell for an edit tree"
        ),
    )

    p.add_argument(
        "source",
    )

    p.add_argument(
        "--buildroot-image",
        default=DEFAULT_BUILDROOT_IMAGE,
    )

    p.add_argument(
        "--repo-url",
        default=DEFAULT_RDISTRO_REPO_URL,
    )

    p.add_argument(
        "--repo-suite",
        default=None,
        help=(
            "APT suite of the R-Distro repository "
            "(for example gen3-canary)"
        ),
    )

    p.add_argument(
        "--public-key",
        default=str(DEFAULT_PUBLIC_KEY),
    )

    p.add_argument(
        "--use-rdistro",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "enable the local signed "
            "R-Distro APT repository"
        ),
    )

    p.add_argument(
        "--deb-build-options",
        default="nocheck nodoc",
    )

    p.add_argument(
        "--deb-build-profiles",
        default="nocheck nodoc",
    )

    p.set_defaults(
        func=command_source_shell,
    )

    # --------------------------------------------------------------
    # source-discard
    # --------------------------------------------------------------

    p = subparsers.add_parser(
        "source-discard",
        help=(
            "remove a disposable source edit tree"
        ),
    )

    p.add_argument(
        "source",
    )

    p.add_argument(
        "--force",
        action="store_true",
    )

    p.set_defaults(
        func=command_source_discard,
    )

    # --------------------------------------------------------------
    # source-origin
    # --------------------------------------------------------------

    p = subparsers.add_parser(
        "source-origin",
        help=(
            "show whether a source is Debian, "
            "overridden Debian, or R-Distro-native"
        ),
    )

    p.add_argument(
        "source",
    )

    p.set_defaults(
        func=command_source_origin,
    )

    # --------------------------------------------------------------
    # package-new
    # --------------------------------------------------------------

    p = subparsers.add_parser(
        "package-new",
        help=(
            "create a new R-Distro-native "
            "Debian source package"
        ),
    )

    p.add_argument(
        "source",
    )

    p.add_argument(
        "--version",
        default="0.1+rdistro1",
    )

    p.add_argument(
        "--section",
        default="misc",
    )

    p.add_argument(
        "--maintainer",
        default=(
            "R-Distro Project "
            "<packages@rdistro.invalid>"
        ),
    )

    p.add_argument(
        "--description",
        default="R-Distro package",
    )

    p.set_defaults(
        func=command_package_new,
    )


# ======================================================================
# Standalone entry point
# ======================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "R-Distro source development helper"
        )
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    register_subcommands(
        sub
    )

    args = parser.parse_args()

    args.func(args)


if __name__ == "__main__":
    main()