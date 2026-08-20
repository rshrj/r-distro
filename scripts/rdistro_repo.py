#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO_ROOT = ROOT / "repo"
DEFAULT_BUILDROOT_IMAGE = "rdistro-buildroot:2026-08-13"
DEFAULT_PRIVATE_GNUPG = ROOT / "keys" / "gnupg"
DEFAULT_PUBLIC_KEY = ROOT / "repo" / "keys" / "rdistro-archive.asc"


RELEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    name TEXT PRIMARY KEY,
    campaign TEXT NOT NULL,
    generation INTEGER NOT NULL,
    suite TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    artifact_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    staging_path TEXT,
    release_path TEXT,
    created_at TEXT NOT NULL,
    promoted_at TEXT
);
"""


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_manifest(path: Path) -> list[str]:
    result: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            result.append(line.split()[0])
    return sorted(set(result))


def parse_deb822(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None

    for raw in path.read_text(errors="replace").splitlines():
        if raw[:1].isspace():
            if current is not None:
                fields[current] += "\n" + raw.strip()
            continue

        if ":" not in raw:
            continue

        key, value = raw.split(":", 1)
        key = key.strip()
        fields[key] = value.strip()
        current = key

    return fields


def referenced_dsc_files(dsc: Path) -> list[str]:
    fields = parse_deb822(dsc)
    raw = fields.get("Files", "")
    result: list[str] = []

    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            result.append(parts[-1])

    return result


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(RELEASE_SCHEMA)
    return con


def campaign_record(con: sqlite3.Connection, campaign: str) -> sqlite3.Row:
    row = con.execute(
        """
        SELECT *
        FROM campaigns
        WHERE name = ?
        """,
        (campaign,),
    ).fetchone()

    if row is None:
        raise RuntimeError(f"unknown campaign: {campaign}")

    return row


def successful_attempt(
    con: sqlite3.Connection,
    campaign: str,
    source: str,
) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT *
        FROM attempts
        WHERE campaign = ?
          AND source = ?
          AND status = 'SUCCEEDED'
        ORDER BY attempt DESC
        LIMIT 1
        """,
        (campaign, source),
    ).fetchone()


def validate_scope(
    *,
    db_path: Path,
    campaign: str,
    manifest: Path,
) -> dict:
    manifest = manifest.resolve()

    if not manifest.exists():
        raise RuntimeError(f"manifest does not exist: {manifest}")

    sources = read_manifest(manifest)
    if not sources:
        raise RuntimeError(f"manifest is empty: {manifest}")

    errors: list[str] = []
    validated: list[dict] = []

    with connect(db_path) as con:
        campaign_row = campaign_record(con, campaign)
        generation = int(campaign_row["generation"])
        expected_suffix = f"+rdistro{generation}"

        for source in sources:
            job = con.execute(
                """
                SELECT *
                FROM jobs
                WHERE campaign = ? AND source = ?
                """,
                (campaign, source),
            ).fetchone()

            if job is None:
                errors.append(f"{source}: not present in campaign")
                continue

            if job["status"] != "SUCCEEDED":
                errors.append(f"{source}: status is {job['status']}, not SUCCEEDED")
                continue

            attempt = successful_attempt(con, campaign, source)
            if attempt is None:
                errors.append(f"{source}: no successful attempt record")
                continue

            attempt_dir = Path(attempt["attempt_dir"])
            artifacts = attempt_dir / "artifacts"

            if not artifacts.is_dir():
                errors.append(f"{source}: artifact directory missing: {artifacts}")
                continue

            files = sorted(p for p in artifacts.iterdir() if p.is_file())
            dscs = [p for p in files if p.suffix == ".dsc"]
            buildinfos = [p for p in files if p.name.endswith(".buildinfo")]
            changes = [p for p in files if p.name.endswith(".changes")]
            binaries = [p for p in files if p.name.endswith((".deb", ".udeb"))]

            package_errors: list[str] = []

            if len(dscs) != 1:
                package_errors.append(f"expected exactly 1 .dsc, found {len(dscs)}")

            if not buildinfos:
                package_errors.append("missing .buildinfo")

            if not changes:
                package_errors.append("missing .changes")

            if not binaries:
                package_errors.append("no .deb/.udeb binary artifacts")

            if dscs:
                dsc_fields = parse_deb822(dscs[0])
                dsc_source = dsc_fields.get("Source", dsc_fields.get("Package", ""))
                dsc_version = dsc_fields.get("Version", "")

                if dsc_source and dsc_source != source:
                    package_errors.append(
                        f".dsc source mismatch: expected {source}, got {dsc_source}"
                    )

                if expected_suffix not in dsc_version:
                    package_errors.append(
                        f".dsc version lacks {expected_suffix}: {dsc_version}"
                    )

                for name in referenced_dsc_files(dscs[0]):
                    if not (artifacts / name).is_file():
                        package_errors.append(
                            f".dsc references missing source artifact: {name}"
                        )

            if changes:
                change_fields = parse_deb822(changes[-1])
                change_version = change_fields.get("Version", "")
                architectures = set(change_fields.get("Architecture", "").split())

                if expected_suffix not in change_version:
                    package_errors.append(
                        f".changes version lacks {expected_suffix}: {change_version}"
                    )

                invalid_arches = architectures - {"arm64", "all", "source"}
                if invalid_arches:
                    package_errors.append(
                        "unexpected architecture(s): " + ", ".join(sorted(invalid_arches))
                    )

            for binary in binaries:
                if not (
                    binary.name.endswith("_arm64.deb")
                    or binary.name.endswith("_all.deb")
                    or binary.name.endswith("_arm64.udeb")
                    or binary.name.endswith("_all.udeb")
                ):
                    package_errors.append(
                        f"binary filename is not arm64/all: {binary.name}"
                    )

                if expected_suffix not in binary.name:
                    package_errors.append(
                        f"binary filename lacks {expected_suffix}: {binary.name}"
                    )

            if package_errors:
                errors.extend(f"{source}: {msg}" for msg in package_errors)
                continue

            validated.append(
                {
                    "source": source,
                    "attempt": int(attempt["attempt"]),
                    "attempt_dir": str(attempt_dir),
                    "artifacts_dir": str(artifacts),
                    "files": [str(p) for p in files],
                    "file_count": len(files),
                }
            )

    return {
        "campaign": campaign,
        "generation": generation,
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        "expected_sources": len(sources),
        "validated_sources": len(validated),
        "artifact_count": sum(item["file_count"] for item in validated),
        "sources": validated,
        "errors": errors,
        "valid": not errors and len(validated) == len(sources),
    }


def print_validation(result: dict) -> None:
    print()
    print("========================================")
    print(" R-Distro repository validation")
    print("========================================")
    print(f"Campaign:          {result['campaign']}")
    print(f"Generation:        {result['generation']}")
    print(f"Manifest sources:  {result['expected_sources']}")
    print(f"Validated sources: {result['validated_sources']}")
    print(f"Artifacts:         {result['artifact_count']}")
    print(f"Status:            {'VALID' if result['valid'] else 'INVALID'}")

    if result["errors"]:
        print()
        print("Errors:")
        for error in result["errors"][:100]:
            print(f"  - {error}")
        if len(result["errors"]) > 100:
            print(f"  ... plus {len(result['errors']) - 100} more")


def hardlink_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def generate_repository_metadata(
    *,
    stage_root: Path,
    suite: str,
    buildroot_image: str,
    private_gnupg: Path,
    sign: bool,
) -> None:
    private_gnupg = private_gnupg.resolve()

    if sign and not private_gnupg.is_dir():
        raise RuntimeError(
            f"private GnuPG directory missing: {private_gnupg}"
        )

    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
        "-e",
        f"SUITE={suite}",
        "-e",
        f"DO_SIGN={'1' if sign else '0'}",
        "-v",
        f"{stage_root.resolve()}:/repo",
    ]

    if sign:
        cmd += ["-v", f"{private_gnupg}:/keys:ro"]

    cmd += [
        buildroot_image,
        "bash",
        "-lc",
        r'''
set -euo pipefail

if ! command -v apt-ftparchive >/dev/null 2>&1 || \
   ! command -v gzip >/dev/null 2>&1 || \
   { [ "$DO_SIGN" = "1" ] && ! command -v gpg >/dev/null 2>&1; }; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o Acquire::Retries=5 update --error-on=any
    apt-get install -y --no-install-recommends apt-utils gzip gnupg
fi

cd /repo

mkdir -p "dists/$SUITE/main/binary-arm64"
mkdir -p "dists/$SUITE/main/source"

apt-ftparchive packages pool/main/r/rdistro \
    > "dists/$SUITE/main/binary-arm64/Packages"

gzip -n -9 -c \
    "dists/$SUITE/main/binary-arm64/Packages" \
    > "dists/$SUITE/main/binary-arm64/Packages.gz"

apt-ftparchive sources pool/main/r/rdistro \
    > "dists/$SUITE/main/source/Sources"

gzip -n -9 -c \
    "dists/$SUITE/main/source/Sources" \
    > "dists/$SUITE/main/source/Sources.gz"

cat >/tmp/rdistro-release.conf <<EOF
APT::FTPArchive::Release {
  Origin "R-Distro";
  Label "R-Distro";
  Suite "$SUITE";
  Codename "$SUITE";
  Architectures "arm64";
  Components "main";
  Description "Immutable R-Distro release $SUITE";
};
EOF

apt-ftparchive \
    -c /tmp/rdistro-release.conf \
    release "dists/$SUITE" \
    > "dists/$SUITE/Release"

if [ "$DO_SIGN" = "1" ]; then
    rm -rf /tmp/gnupg
    mkdir -p /tmp/gnupg

    # Copy only persistent key material. Do not copy gpg-agent sockets
    # from the host-mounted GNUPGHOME.
    test -f /keys/pubring.kbx
    test -d /keys/private-keys-v1.d
    cp /keys/pubring.kbx /tmp/gnupg/
    cp -a /keys/private-keys-v1.d /tmp/gnupg/
    [ ! -f /keys/trustdb.gpg ] || cp /keys/trustdb.gpg /tmp/gnupg/
    [ ! -f /keys/gpg.conf ] || cp /keys/gpg.conf /tmp/gnupg/

    chmod 700 /tmp/gnupg
    find /tmp/gnupg -type d -exec chmod 700 {} +
    find /tmp/gnupg -type f -exec chmod 600 {} +

    export GNUPGHOME=/tmp/gnupg

    KEY_FPR=$(gpg --batch --with-colons --list-secret-keys | \
        awk -F: '$1 == "fpr" { print $10; exit }')

    test -n "$KEY_FPR"

    gpg --batch --yes --pinentry-mode loopback \
        --local-user "$KEY_FPR" \
        --clearsign \
        --output "dists/$SUITE/InRelease" \
        "dists/$SUITE/Release"

    gpg --batch --yes --pinentry-mode loopback \
        --local-user "$KEY_FPR" \
        --armor --detach-sign \
        --output "dists/$SUITE/Release.gpg" \
        "dists/$SUITE/Release"
fi
''',
    ]

    subprocess.run(cmd, cwd=ROOT, check=True)


def command_validate(args) -> None:
    result = validate_scope(
        db_path=args.db,
        campaign=args.campaign,
        manifest=Path(args.manifest),
    )
    print_validation(result)

    if not result["valid"]:
        raise SystemExit(2)


def command_stage(args) -> None:
    result = validate_scope(
        db_path=args.db,
        campaign=args.campaign,
        manifest=Path(args.manifest),
    )
    print_validation(result)

    if not result["valid"]:
        raise SystemExit(2)

    repo_root = Path(args.repo_root).resolve()
    staging_parent = repo_root / "staging"
    releases_parent = repo_root / "releases"
    staging_parent.mkdir(parents=True, exist_ok=True)
    releases_parent.mkdir(parents=True, exist_ok=True)

    final_stage = staging_parent / args.release
    final_release = releases_parent / args.release

    if final_release.exists():
        raise SystemExit(
            f"immutable release already exists: {final_release}"
        )

    if final_stage.exists():
        if not args.force:
            raise SystemExit(
                f"staging release already exists: {final_stage}\n"
                "Use --force to rebuild staging."
            )
        shutil.rmtree(final_stage)

    tmp_stage = staging_parent / f".{args.release}.tmp-{os.getpid()}"
    if tmp_stage.exists():
        shutil.rmtree(tmp_stage)

    tmp_stage.mkdir(parents=True)

    try:
        artifact_count = 0

        for item in result["sources"]:
            source = item["source"]
            pool_dir = tmp_stage / "pool" / "main" / "r" / "rdistro" / source
            pool_dir.mkdir(parents=True, exist_ok=True)

            for raw_path in item["files"]:
                src = Path(raw_path)
                dst = pool_dir / src.name
                if dst.exists():
                    raise RuntimeError(
                        f"artifact collision while staging: {dst}"
                    )
                hardlink_or_copy(src, dst)
                artifact_count += 1

        public_key = Path(args.public_key).resolve()
        if public_key.is_file():
            key_dst = tmp_stage / "keys" / "rdistro-archive.asc"
            hardlink_or_copy(public_key, key_dst)

        release_metadata = {
            "release": args.release,
            "suite": args.suite,
            "campaign": result["campaign"],
            "generation": result["generation"],
            "manifest": result["manifest"],
            "manifest_sha256": result["manifest_sha256"],
            "source_count": result["validated_sources"],
            "artifact_count": artifact_count,
            "created_at": now(),
            "git_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                text=True,
                capture_output=True,
            ).stdout.strip() or "unknown",
            "signed": not args.no_sign,
            "status": "STAGED",
        }

        (tmp_stage / "rdistro-release.json").write_text(
            json.dumps(release_metadata, indent=2) + "\n"
        )

        print()
        print("Generating APT metadata and signatures...")

        generate_repository_metadata(
            stage_root=tmp_stage,
            suite=args.suite,
            buildroot_image=args.buildroot_image,
            private_gnupg=Path(args.gnupg_home),
            sign=not args.no_sign,
        )

        # Only expose the named staging repository after everything succeeds.
        os.replace(tmp_stage, final_stage)

        with connect(args.db) as con:
            con.execute(
                """
                INSERT INTO releases (
                    name, campaign, generation, suite,
                    manifest_path, manifest_sha256,
                    source_count, artifact_count,
                    status, staging_path, release_path,
                    created_at, promoted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'STAGED', ?, NULL, ?, NULL)
                ON CONFLICT(name) DO UPDATE SET
                    campaign=excluded.campaign,
                    generation=excluded.generation,
                    suite=excluded.suite,
                    manifest_path=excluded.manifest_path,
                    manifest_sha256=excluded.manifest_sha256,
                    source_count=excluded.source_count,
                    artifact_count=excluded.artifact_count,
                    status='STAGED',
                    staging_path=excluded.staging_path,
                    release_path=NULL,
                    created_at=excluded.created_at,
                    promoted_at=NULL
                """,
                (
                    args.release,
                    result["campaign"],
                    result["generation"],
                    args.suite,
                    result["manifest"],
                    result["manifest_sha256"],
                    result["validated_sources"],
                    artifact_count,
                    str(final_stage),
                    release_metadata["created_at"],
                ),
            )

        print()
        print("STAGED")
        print(f"  release: {args.release}")
        print(f"  suite:   {args.suite}")
        print(f"  sources: {result['validated_sources']}")
        print(f"  files:   {artifact_count}")
        print(f"  path:    {final_stage}")

    except Exception:
        if tmp_stage.exists():
            shutil.rmtree(tmp_stage, ignore_errors=True)
        raise


def command_promote(args) -> None:
    repo_root = Path(args.repo_root).resolve()
    stage = repo_root / "staging" / args.release
    release = repo_root / "releases" / args.release

    if not stage.is_dir():
        raise SystemExit(f"staging release does not exist: {stage}")

    if release.exists():
        raise SystemExit(f"immutable release already exists: {release}")

    marker = stage / "rdistro-release.json"
    if not marker.is_file():
        raise SystemExit(f"staging marker missing: {marker}")

    metadata = json.loads(marker.read_text())

    required = [
        stage / "dists" / metadata["suite"] / "Release",
        stage / "dists" / metadata["suite"] / "main" / "binary-arm64" / "Packages",
        stage / "dists" / metadata["suite"] / "main" / "source" / "Sources",
    ]

    if metadata.get("signed", True):
        required += [
            stage / "dists" / metadata["suite"] / "InRelease",
            stage / "dists" / metadata["suite"] / "Release.gpg",
        ]

    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise SystemExit(
            "cannot promote; repository metadata is incomplete:\n  "
            + "\n  ".join(missing)
        )

    promoted_at = now()
    metadata["status"] = "PROMOTED"
    metadata["promoted_at"] = promoted_at
    marker.write_text(json.dumps(metadata, indent=2) + "\n")

    # Same repo filesystem: rename is atomic.
    os.rename(stage, release)

    with connect(args.db) as con:
        con.execute(
            """
            UPDATE releases
            SET status='PROMOTED',
                staging_path=NULL,
                release_path=?,
                promoted_at=?
            WHERE name=?
            """,
            (str(release), promoted_at, args.release),
        )

    print()
    print("PROMOTED")
    print(f"  release: {args.release}")
    print(f"  path:    {release}")
    print()
    print("APT endpoint when serving repo/:")
    print(f"  http://<host>:<port>/releases/{args.release}/")
    print(f"  suite: {metadata['suite']}")


def register_subcommands(subparsers) -> None:
    p = subparsers.add_parser(
        "validate",
        help="validate successful build artifacts for a manifest scope",
    )
    p.add_argument("--campaign", required=True)
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=command_validate)

    p = subparsers.add_parser(
        "stage",
        help="create a validated, signed staging APT repository",
    )
    p.add_argument("--campaign", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--release", required=True)
    p.add_argument(
        "--suite",
        default=None,
        help="APT suite/codename; defaults to --release",
    )
    p.add_argument(
        "--repo-root",
        default=str(DEFAULT_REPO_ROOT),
    )
    p.add_argument(
        "--buildroot-image",
        default=DEFAULT_BUILDROOT_IMAGE,
    )
    p.add_argument(
        "--gnupg-home",
        default=str(DEFAULT_PRIVATE_GNUPG),
    )
    p.add_argument(
        "--public-key",
        default=str(DEFAULT_PUBLIC_KEY),
    )
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--no-sign",
        action="store_true",
        help="testing only: generate repository metadata without signatures",
    )
    p.set_defaults(func=_normalize_stage_args)

    p = subparsers.add_parser(
        "promote",
        help="atomically promote a staging repository to an immutable release",
    )
    p.add_argument("--release", required=True)
    p.add_argument(
        "--repo-root",
        default=str(DEFAULT_REPO_ROOT),
    )
    p.set_defaults(func=command_promote)


def _normalize_stage_args(args) -> None:
    if args.suite is None:
        args.suite = args.release
    command_stage(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="R-Distro repository promotion helper")
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "work" / "control" / "rdistro.db",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    register_subcommands(sub)
    args = parser.parse_args()
    args.func(args)
