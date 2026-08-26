#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rdistro_repo


ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB = ROOT / "work" / "control" / "rdistro.db"

BUILD_SCRIPT = ROOT / "scripts" / "build-package.sh"

CAMPAIGN_ROOT = ROOT / "work" / "campaigns"

CONTROLLER_PID_FILE = "controller.pid"
CONTROLLER_LOG_FILE = "controller.log"
CONTROLLER_CONFIG_FILE = "controller.json"


# ======================================================================
# Helpers
# ======================================================================

def now():
    return dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def sha256_file(path: Path):
    h = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def git_commit():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    except Exception:
        return "unknown"


def read_manifest(path: Path):
    result = []

    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()

        if not line:
            continue

        # Boundary manifests may contain just source names or
        # "source version".
        result.append(line.split()[0])

    return sorted(set(result))


def connect(db_path: Path):
    db_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    con = sqlite3.connect(
        db_path,
        timeout=60,
    )

    con.row_factory = sqlite3.Row

    con.execute(
        "PRAGMA journal_mode=WAL"
    )

    con.execute(
        "PRAGMA synchronous=NORMAL"
    )

    con.execute(
        "PRAGMA busy_timeout=60000"
    )

    return con


# ======================================================================
# Schema
# ======================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    name TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED'
);

CREATE TABLE IF NOT EXISTS jobs (
    campaign TEXT NOT NULL,
    source TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'PENDING',

    attempts INTEGER NOT NULL DEFAULT 0,

    started_at TEXT,
    finished_at TEXT,

    duration_sec REAL,

    exit_code INTEGER,

    failure_category TEXT,
    last_error TEXT,

    latest_attempt_dir TEXT,
    latest_log TEXT,
    latest_buildinfo TEXT,

    total_build_deps INTEGER,
    rdistro_build_deps INTEGER,
    debian_build_deps INTEGER,

    PRIMARY KEY (campaign, source),

    FOREIGN KEY (campaign)
        REFERENCES campaigns(name)
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    campaign TEXT NOT NULL,
    source TEXT NOT NULL,
    attempt INTEGER NOT NULL,

    started_at TEXT NOT NULL,
    finished_at TEXT,

    exit_code INTEGER,
    status TEXT NOT NULL,

    failure_category TEXT,

    attempt_dir TEXT NOT NULL,
    log_path TEXT NOT NULL,
    buildinfo_path TEXT,

    git_commit TEXT,
    build_script_sha256 TEXT,

    environment_json TEXT,

    total_build_deps INTEGER,
    rdistro_build_deps INTEGER,
    debian_build_deps INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_campaign_status
ON jobs(campaign, status);

CREATE INDEX IF NOT EXISTS idx_attempts_campaign_source
ON attempts(campaign, source);
"""


def init_db(db_path: Path):
    with connect(db_path) as con:
        con.executescript(SCHEMA)


# ======================================================================
# Planning
# ======================================================================

def command_plan(args):
    manifest = Path(args.manifest).resolve()

    if not manifest.exists():
        raise SystemExit(
            f"manifest does not exist: {manifest}"
        )

    sources = read_manifest(manifest)

    init_db(args.db)

    manifest_hash = sha256_file(manifest)

    with connect(args.db) as con:

        existing = con.execute(
            """
            SELECT *
            FROM campaigns
            WHERE name = ?
            """,
            (args.campaign,),
        ).fetchone()

        if existing:

            if existing["generation"] != args.generation:
                raise SystemExit(
                    "campaign already exists with "
                    "different generation"
                )

        else:

            con.execute(
                """
                INSERT INTO campaigns (
                    name,
                    generation,
                    manifest_path,
                    manifest_sha256,
                    created_at,
                    git_commit,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'PLANNED')
                """,
                (
                    args.campaign,
                    args.generation,
                    str(manifest),
                    manifest_hash,
                    now(),
                    git_commit(),
                ),
            )

        before = con.execute(
            """
            SELECT COUNT(*) AS n
            FROM jobs
            WHERE campaign = ?
            """,
            (args.campaign,),
        ).fetchone()["n"]

        for source in sources:

            con.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    campaign,
                    source,
                    status
                )
                VALUES (?, ?, 'PENDING')
                """,
                (
                    args.campaign,
                    source,
                ),
            )

        after = con.execute(
            """
            SELECT COUNT(*) AS n
            FROM jobs
            WHERE campaign = ?
            """,
            (args.campaign,),
        ).fetchone()["n"]

    print(
        f"Campaign:    {args.campaign}"
    )

    print(
        f"Generation:  {args.generation}"
    )

    print(
        f"Manifest:    {manifest}"
    )

    print(
        f"Sources:     {len(sources)}"
    )

    print(
        f"New jobs:    {after - before}"
    )

    print(
        f"Total jobs:  {after}"
    )


# ======================================================================
# Job claiming
# ======================================================================

def recover_stale_jobs(
    db_path: Path,
    campaign: str,
):
    with connect(db_path) as con:

        n = con.execute(
            """
            UPDATE jobs
            SET
                status = 'RETRY',
                last_error =
                    'controller restarted while job was BUILDING'
            WHERE campaign = ?
              AND status = 'BUILDING'
            """,
            (campaign,),
        ).rowcount

    if n:
        print(
            f"[recovery] moved {n} stale BUILDING job(s) to RETRY"
        )


def campaign_status(
    db_path: Path,
    campaign: str,
):
    with connect(db_path) as con:

        row = con.execute(
            """
            SELECT status
            FROM campaigns
            WHERE name = ?
            """,
            (campaign,),
        ).fetchone()

    if row is None:
        raise RuntimeError(
            f"unknown campaign: {campaign}"
        )

    return row["status"]


def set_campaign_status(
    db_path: Path,
    campaign: str,
    status: str,
):
    with connect(db_path) as con:

        con.execute(
            """
            UPDATE campaigns
            SET status = ?
            WHERE name = ?
            """,
            (
                status,
                campaign,
            ),
        )



def campaign_dir(
    campaign: str,
) -> Path:
    path = CAMPAIGN_ROOT / campaign

    path.mkdir(
        parents=True,
        exist_ok=True,
    )

    return path


def controller_pid_path(
    campaign: str,
) -> Path:
    return (
        campaign_dir(campaign)
        / CONTROLLER_PID_FILE
    )


def controller_log_path(
    campaign: str,
) -> Path:
    return (
        campaign_dir(campaign)
        / CONTROLLER_LOG_FILE
    )


def controller_config_path(
    campaign: str,
) -> Path:
    return (
        campaign_dir(campaign)
        / CONTROLLER_CONFIG_FILE
    )


def pid_is_alive(
    pid: int,
) -> bool:
    if pid <= 0:
        return False

    try:
        os.kill(
            pid,
            0,
        )

    except ProcessLookupError:
        return False

    except PermissionError:
        return True

    return True


def controller_pid(
    campaign: str,
) -> int | None:
    path = controller_pid_path(
        campaign
    )

    try:
        pid = int(
            path.read_text().strip()
        )

    except (
        FileNotFoundError,
        ValueError,
    ):
        return None

    if pid_is_alive(pid):
        return pid

    # Stale PID file.
    try:
        path.unlink()

    except FileNotFoundError:
        pass

    return None


def write_controller_config(
    *,
    db_path: Path,
    campaign: str,
    parallel: int,
    build_jobs: int,
    only_manifest: str | None,
):
    config = {
        "db": str(
            db_path.resolve()
        ),
        "campaign": campaign,
        "parallel": parallel,
        "build_jobs": build_jobs,
        "only_manifest": (
            str(
                Path(
                    only_manifest
                ).resolve()
            )
            if only_manifest
            else None
        ),
        "updated_at": now(),
    }

    path = controller_config_path(
        campaign
    )

    tmp = path.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            config,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    tmp.replace(path)

    return config


def read_controller_config(
    campaign: str,
):
    path = controller_config_path(
        campaign
    )

    if not path.exists():
        return None

    try:
        return json.loads(
            path.read_text()
        )

    except Exception:
        return None


def start_detached_controller(
    *,
    db_path: Path,
    campaign: str,
    parallel: int,
    build_jobs: int,
    only_manifest: str | None,
):
    existing_pid = controller_pid(
        campaign
    )

    if existing_pid is not None:
        raise RuntimeError(
            f"controller already running "
            f"for {campaign} "
            f"(pid {existing_pid})"
        )

    if parallel < 1:
        raise ValueError(
            "--parallel must be >= 1"
        )

    if build_jobs < 1:
        raise ValueError(
            "--build-jobs must be >= 1"
        )

    if only_manifest:
        manifest = Path(
            only_manifest
        ).resolve()

        if not manifest.exists():
            raise FileNotFoundError(
                f"run manifest does not exist: "
                f"{manifest}"
            )

        only_manifest = str(
            manifest
        )

    config = write_controller_config(
        db_path=db_path,
        campaign=campaign,
        parallel=parallel,
        build_jobs=build_jobs,
        only_manifest=only_manifest,
    )

    log_path = controller_log_path(
        campaign
    )

    cmd = [
        sys.executable,
        str(
            Path(
                __file__
            ).resolve()
        ),
        "--db",
        str(
            db_path.resolve()
        ),
        "_controller",
        "--campaign",
        campaign,
        "--parallel",
        str(parallel),
        "--build-jobs",
        str(build_jobs),
    ]

    if only_manifest:
        cmd.extend(
            [
                "--only-manifest",
                only_manifest,
            ]
        )

    log = log_path.open(
        "a",
        buffering=1,
    )

    log.write(
        "\n"
        + "=" * 72
        + "\n"
    )

    log.write(
        f"# controller launch: {now()}\n"
    )

    log.write(
        "# command: "
        + " ".join(cmd)
        + "\n"
    )

    log.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    finally:
        log.close()

    controller_pid_path(
        campaign
    ).write_text(
        f"{proc.pid}\n"
    )

    # Catch immediate startup failures without turning run back
    # into a foreground operation.
    time.sleep(0.15)

    returncode = proc.poll()

    if returncode is not None:
        try:
            controller_pid_path(
                campaign
            ).unlink()

        except FileNotFoundError:
            pass

        tail = ""

        try:
            lines = log_path.read_text(
                errors="replace"
            ).splitlines()

            tail = "\n".join(
                lines[-30:]
            )

        except Exception:
            pass

        raise RuntimeError(
            f"controller exited immediately "
            f"with code {returncode}\n"
            f"{tail}"
        )

    return (
        proc.pid,
        log_path,
        config,
    )


def remove_own_controller_pid(
    campaign: str,
):
    path = controller_pid_path(
        campaign
    )

    try:
        recorded = int(
            path.read_text().strip()
        )

    except (
        FileNotFoundError,
        ValueError,
    ):
        return

    if recorded != os.getpid():
        return

    try:
        path.unlink()

    except FileNotFoundError:
        pass



def claim_job(
    db_path: Path,
    campaign: str,
    allowed_sources: set[str] | None,
):
    con = connect(db_path)

    try:
        con.execute(
            "BEGIN IMMEDIATE"
        )

        row = con.execute(
            """
            SELECT status
            FROM campaigns
            WHERE name = ?
            """,
            (campaign,),
        ).fetchone()

        if row is None:
            con.rollback()
            return None

        if row["status"] != "RUNNING":
            con.rollback()
            return None

        sql = """
            SELECT source, attempts
            FROM jobs
            WHERE campaign = ?
              AND status IN ('PENDING', 'RETRY')
        """

        params = [campaign]

        if allowed_sources is not None:

            if not allowed_sources:
                con.rollback()
                return None

            placeholders = ",".join(
                "?"
                for _ in allowed_sources
            )

            sql += (
                f" AND source IN ({placeholders})"
            )

            params.extend(
                sorted(allowed_sources)
            )

        sql += """
            ORDER BY
                CASE WHEN status = 'RETRY' THEN 0 ELSE 1 END,
                attempts ASC,
                source ASC
            LIMIT 1
        """

        job = con.execute(
            sql,
            params,
        ).fetchone()

        if job is None:
            con.rollback()
            return None

        source = job["source"]

        attempt = (
            job["attempts"]
            + 1
        )

        con.execute(
            """
            UPDATE jobs
            SET
                status = 'BUILDING',
                attempts = ?,
                started_at = ?,
                finished_at = NULL,
                failure_category = NULL,
                last_error = NULL
            WHERE campaign = ?
              AND source = ?
            """,
            (
                attempt,
                now(),
                campaign,
                source,
            ),
        )

        con.commit()

        return (
            source,
            attempt,
        )

    finally:
        con.close()


# ======================================================================
# Provenance
# ======================================================================

def parse_control_file(path: Path):
    fields = {}
    current = None

    for raw in path.read_text(
        errors="replace"
    ).splitlines():

        if raw[:1].isspace():

            if current:
                fields[current] += (
                    "\n"
                    + raw.strip()
                )

            continue

        if ":" not in raw:
            continue

        key, value = raw.split(
            ":",
            1,
        )

        key = key.strip()

        fields[key] = value.strip()

        current = key

    return fields


BUILD_DEP_RE = re.compile(
    r"([A-Za-z0-9+_.:-]+)"
    r"\s+\(=\s*([^)]+)\)"
)


def provenance_from_buildinfo(
    path: Path,
):
    fields = parse_control_file(path)

    raw = fields.get(
        "Installed-Build-Depends",
        "",
    )

    dependencies = BUILD_DEP_RE.findall(
        raw
    )

    total = len(dependencies)

    rdistro = sum(
        1
        for _, version in dependencies
        if "+rdistro" in version
    )

    debian = total - rdistro

    return {
        "total": total,
        "rdistro": rdistro,
        "debian": debian,
    }


# ======================================================================
# Failure classification
# ======================================================================

FAILURE_PATTERNS = [
    (
        "disk",
        (
            "no space left on device",
        ),
    ),
    (
        "oom",
        (
            "out of memory",
            "cannot allocate memory",
            "oom-kill",
        ),
    ),
    (
        "dependency",
        (
            "unable to correct problems",
            "unmet dependencies",
            "not installable",
            "unable to locate package",
            "mk-build-deps: unable",
            "but it is not installable",
        ),
    ),
    (
        "source-fetch",
        (
            "failed to fetch",
            "apt-get source",
            "unable to find a source package",
        ),
    ),
    (
        "tests",
        (
            "dh_auto_test",
            "tests failed",
            "test suite failed",
            "failures!!!",
        ),
    ),
    (
        "configure",
        (
            "configure: error:",
            "cmake error",
            "meson.build:",
        ),
    ),
    (
        "docker",
        (
            "cannot connect to the docker daemon",
            "docker: error",
            "error response from daemon",
        ),
    ),
]


def classify_failure(log_path: Path):
    try:
        with log_path.open(
            "rb"
        ) as f:

            f.seek(
                0,
                os.SEEK_END,
            )

            size = f.tell()

            f.seek(
                max(
                    0,
                    size - 300_000,
                )
            )

            text = f.read().decode(
                errors="replace"
            ).lower()

    except Exception:
        return "unknown"

    for category, patterns in FAILURE_PATTERNS:

        if any(
            pattern in text
            for pattern in patterns
        ):
            return category

    if (
        "error:" in text
        or "make" in text
        and "***" in text
    ):
        return "compile"

    return "unknown"


# ======================================================================
# Run one package
# ======================================================================

def campaign_generation(
    db_path: Path,
    campaign: str,
):
    with connect(db_path) as con:

        row = con.execute(
            """
            SELECT generation
            FROM campaigns
            WHERE name = ?
            """,
            (campaign,),
        ).fetchone()

    if row is None:
        raise RuntimeError(
            f"unknown campaign: {campaign}"
        )

    return row["generation"]


def finish_job(
    db_path: Path,
    *,
    campaign: str,
    source: str,
    attempt: int,
    status: str,
    exit_code: int,
    duration: float,
    failure_category: str | None,
    attempt_dir: Path,
    log_path: Path,
    buildinfo: Path | None,
    provenance: dict,
    environment: dict,
):
    finished = now()

    result = {
        "campaign": campaign,
        "source": source,
        "attempt": attempt,
        "status": status,
        "exit_code": exit_code,
        "duration_sec": duration,
        "finished_at": finished,
        "failure_category":
            failure_category,
        "buildinfo":
            str(buildinfo)
            if buildinfo
            else None,
        "provenance": provenance,
        "environment": environment,
    }

    (
        attempt_dir
        / "result.json"
    ).write_text(
        json.dumps(
            result,
            indent=2,
        )
        + "\n"
    )

    with connect(db_path) as con:

        con.execute(
            """
            INSERT INTO attempts (
                campaign,
                source,
                attempt,
                started_at,
                finished_at,
                exit_code,
                status,
                failure_category,
                attempt_dir,
                log_path,
                buildinfo_path,
                git_commit,
                build_script_sha256,
                environment_json,
                total_build_deps,
                rdistro_build_deps,
                debian_build_deps
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                campaign,
                source,
                attempt,
                environment["started_at"],
                finished,
                exit_code,
                status,
                failure_category,
                str(attempt_dir),
                str(log_path),
                str(buildinfo)
                if buildinfo
                else None,
                environment["git_commit"],
                environment[
                    "build_script_sha256"
                ],
                json.dumps(
                    environment,
                    sort_keys=True,
                ),
                provenance["total"],
                provenance["rdistro"],
                provenance["debian"],
            ),
        )

        con.execute(
            """
            UPDATE jobs
            SET
                status = ?,
                finished_at = ?,
                duration_sec = ?,
                exit_code = ?,
                failure_category = ?,
                last_error = ?,
                latest_attempt_dir = ?,
                latest_log = ?,
                latest_buildinfo = ?,
                total_build_deps = ?,
                rdistro_build_deps = ?,
                debian_build_deps = ?
            WHERE campaign = ?
              AND source = ?
            """,
            (
                status,
                finished,
                duration,
                exit_code,
                failure_category,
                failure_category,
                str(attempt_dir),
                str(log_path),
                str(buildinfo)
                if buildinfo
                else None,
                provenance["total"],
                provenance["rdistro"],
                provenance["debian"],
                campaign,
                source,
            ),
        )


def run_one(
    db_path: Path,
    campaign: str,
    source: str,
    attempt: int,
    build_jobs: int,
):
    generation = campaign_generation(
        db_path,
        campaign,
    )

    attempt_dir = (
        CAMPAIGN_ROOT
        / campaign
        / source
        / f"attempt-{attempt:04d}"
    )

    artifacts = (
        attempt_dir
        / "artifacts"
    )

    log_path = (
        attempt_dir
        / "build.log"
    )

    attempt_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    artifacts.mkdir(
        parents=True,
        exist_ok=True,
    )

    started = now()

    environment = {
        "started_at": started,
        "git_commit": git_commit(),
        "build_script_sha256":
            sha256_file(
                BUILD_SCRIPT
            ),
        "generation": generation,
        "use_rdistro": 1,
        "build_jobs": build_jobs,
        "deb_build_options":
            "nocheck nodoc",
        "deb_build_profiles":
            "nocheck nodoc",
        "output_dir":
            str(artifacts),
    }

    env = os.environ.copy()

    env.update(
        {
            "USE_RDISTRO": "1",
            "BUILD_JOBS":
                str(build_jobs),
            "DEB_BUILD_OPTIONS":
                "nocheck nodoc",
            "DEB_BUILD_PROFILES":
                "nocheck nodoc",
            "RDISTRO_OUTPUT_DIR":
                str(artifacts),
        }
    )

    cmd = [
        str(BUILD_SCRIPT),
        source,
        str(generation),
    ]

    print(
        f"[START] {source} "
        f"(attempt {attempt})"
    )

    t0 = time.monotonic()

    with log_path.open(
        "w",
        buffering=1,
    ) as log:

        log.write(
            f"# command: {' '.join(cmd)}\n"
        )

        log.write(
            f"# started: {started}\n"
        )

        log.write(
            "# environment: "
            + json.dumps(
                environment,
                sort_keys=True,
            )
            + "\n\n"
        )

        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )

    duration = (
        time.monotonic()
        - t0
    )

    buildinfos = sorted(
        artifacts.rglob(
            "*.buildinfo"
        ),
        key=lambda p:
            p.stat().st_mtime,
    )

    buildinfo = (
        buildinfos[-1]
        if buildinfos
        else None
    )

    provenance = {
        "total": 0,
        "rdistro": 0,
        "debian": 0,
    }

    if buildinfo:

        try:
            provenance = (
                provenance_from_buildinfo(
                    buildinfo
                )
            )

        except Exception as exc:

            with log_path.open(
                "a"
            ) as log:

                log.write(
                    "\n# provenance parse error: "
                    f"{exc}\n"
                )

    if proc.returncode == 0:

        status = "SUCCEEDED"
        category = None

        print(
            f"[ OK  ] {source} "
            f"{duration:.1f}s "
            f"deps={provenance['total']} "
            f"rdistro={provenance['rdistro']} "
            f"debian={provenance['debian']}"
        )

    else:

        status = "FAILED"

        category = classify_failure(
            log_path
        )

        print(
            f"[FAIL ] {source} "
            f"exit={proc.returncode} "
            f"category={category}"
        )

    finish_job(
        db_path,
        campaign=campaign,
        source=source,
        attempt=attempt,
        status=status,
        exit_code=proc.returncode,
        duration=duration,
        failure_category=category,
        attempt_dir=attempt_dir,
        log_path=log_path,
        buildinfo=buildinfo,
        provenance=provenance,
        environment=environment,
    )


# ======================================================================
# Campaign execution
# ======================================================================

def validate_run_environment(
    *,
    only_manifest: str | None,
):
    if not BUILD_SCRIPT.exists():
        raise SystemExit(
            f"missing {BUILD_SCRIPT}"
        )

    script_text = BUILD_SCRIPT.read_text(
        errors="replace"
    )

    if "RDISTRO_OUTPUT_DIR" not in script_text:
        raise SystemExit(
            "build-package.sh has not been patched "
            "to support RDISTRO_OUTPUT_DIR"
        )

    if only_manifest:
        path = Path(
            only_manifest
        ).resolve()

        if not path.exists():
            raise SystemExit(
                f"run manifest does not exist: "
                f"{path}"
            )


def command_run(args):
    """
    Start a detached campaign controller and immediately return the
    terminal to the user.

    The detached controller remains alive across pause/resume. Its
    output goes to work/campaigns/<campaign>/controller.log.
    """
    init_db(args.db)

    validate_run_environment(
        only_manifest=args.only_manifest,
    )

    # Ensure campaign exists before starting anything.
    with connect(args.db) as con:
        campaign = con.execute(
            """
            SELECT *
            FROM campaigns
            WHERE name = ?
            """,
            (args.campaign,),
        ).fetchone()

    if campaign is None:
        raise SystemExit(
            f"unknown campaign: {args.campaign}; "
            "run plan first"
        )

    existing_pid = controller_pid(
        args.campaign
    )

    if existing_pid is not None:
        status = campaign_status(
            args.db,
            args.campaign,
        )

        raise SystemExit(
            f"controller already running "
            f"(pid {existing_pid}, "
            f"campaign state {status}). "
            "Use status, pause, or resume; "
            "do not start another run."
        )

    set_campaign_status(
        args.db,
        args.campaign,
        "RUNNING",
    )

    try:
        pid, log_path, _ = (
            start_detached_controller(
                db_path=args.db,
                campaign=args.campaign,
                parallel=args.parallel,
                build_jobs=args.build_jobs,
                only_manifest=args.only_manifest,
            )
        )

    except Exception:
        set_campaign_status(
            args.db,
            args.campaign,
            "PLANNED",
        )

        raise

    scope = (
        str(
            Path(
                args.only_manifest
            ).resolve()
        )
        if args.only_manifest
        else "full campaign"
    )

    print(
        f"{args.campaign}: RUNNING"
    )

    print(
        f"Controller PID: {pid}"
    )

    print(
        f"Scope:          {scope}"
    )

    print(
        f"Controller log: {log_path}"
    )

    print()
    print(
        "The controller is detached; "
        "this terminal is free."
    )

    print(
        "Use `status`, `pause`, and `resume` "
        "from any terminal."
    )


def command_controller(args):
    """
    Internal detached controller process.

    Users should invoke `run`, not this command directly.
    """
    init_db(args.db)

    validate_run_environment(
        only_manifest=args.only_manifest,
    )

    # The parent writes this too; writing it here closes the small
    # launch race and guarantees the PID file identifies this process.
    controller_pid_path(
        args.campaign
    ).write_text(
        f"{os.getpid()}\n"
    )

    allowed_sources = None

    if args.only_manifest:
        path = Path(
            args.only_manifest
        ).resolve()

        allowed_sources = set(
            read_manifest(path)
        )

        print(
            f"[controller] scope: "
            f"{len(allowed_sources)} source(s) "
            f"from {path}",
            flush=True,
        )

    else:
        print(
            "[controller] scope: full campaign",
            flush=True,
        )

    recover_stale_jobs(
        args.db,
        args.campaign,
    )

    stop_event = threading.Event()

    def stop_handler(
        signum,
        frame,
    ):
        if not stop_event.is_set():
            print(
                "\n[controller] stop requested; "
                "no new jobs will start",
                flush=True,
            )

            stop_event.set()

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    def worker(worker_id):
        while not stop_event.is_set():

            status = campaign_status(
                args.db,
                args.campaign,
            )

            if status == "PAUSED":
                time.sleep(1)
                continue

            if status != "RUNNING":
                return

            claimed = claim_job(
                args.db,
                args.campaign,
                allowed_sources,
            )

            if claimed is None:
                # A pause may have raced with claim_job().
                status = campaign_status(
                    args.db,
                    args.campaign,
                )

                if status == "PAUSED":
                    time.sleep(1)
                    continue

                return

            source, attempt = claimed

            try:
                run_one(
                    args.db,
                    args.campaign,
                    source,
                    attempt,
                    args.build_jobs,
                )

            except Exception as exc:
                print(
                    f"[CTRL ] {source}: {exc}",
                    flush=True,
                )

                with connect(
                    args.db
                ) as con:
                    con.execute(
                        """
                        UPDATE jobs
                        SET
                            status = 'FAILED',
                            finished_at = ?,
                            failure_category =
                                'controller',
                            last_error = ?
                        WHERE campaign = ?
                          AND source = ?
                        """,
                        (
                            now(),
                            str(exc),
                            args.campaign,
                            source,
                        ),
                    )

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.parallel
        ) as executor:

            futures = [
                executor.submit(
                    worker,
                    i,
                )
                for i in range(
                    args.parallel
                )
            ]

            for future in futures:
                future.result()

        current_status = campaign_status(
            args.db,
            args.campaign,
        )

        if stop_event.is_set():
            # SIGTERM/SIGINT means the detached controller is ending.
            # Do not leave the DB claiming that a controller is running.
            if current_status == "RUNNING":
                set_campaign_status(
                    args.db,
                    args.campaign,
                    "PLANNED",
                )

            print(
                "[controller] stopped",
                flush=True,
            )

            return

        # A normally paused controller never gets here: workers wait
        # in the PAUSED state until resume changes it back to RUNNING.
        with connect(args.db) as con:
            rows = con.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM jobs
                WHERE campaign = ?
                GROUP BY status
                """,
                (args.campaign,),
            ).fetchall()

        counts = {
            row["status"]:
                row["n"]
            for row in rows
        }

        remaining = (
            counts.get(
                "PENDING",
                0,
            )
            + counts.get(
                "RETRY",
                0,
            )
            + counts.get(
                "BUILDING",
                0,
            )
        )

        # A scoped/canary run intentionally leaves the rest of the
        # full campaign pending.
        if allowed_sources is not None:
            set_campaign_status(
                args.db,
                args.campaign,
                "PLANNED",
            )

        elif remaining:
            set_campaign_status(
                args.db,
                args.campaign,
                "PLANNED",
            )

        elif counts.get(
            "FAILED",
            0,
        ):
            set_campaign_status(
                args.db,
                args.campaign,
                "FAILED",
            )

        else:
            set_campaign_status(
                args.db,
                args.campaign,
                "COMPLETE",
            )

        print(
            "[controller] run scope exhausted; exiting",
            flush=True,
        )

    finally:
        remove_own_controller_pid(
            args.campaign
        )



# ======================================================================
# Status / retries / pause
# ======================================================================

def get_counts(
    db_path: Path,
    campaign: str,
):
    with connect(db_path) as con:

        rows = con.execute(
            """
            SELECT
                status,
                COUNT(*) AS n
            FROM jobs
            WHERE campaign = ?
            GROUP BY status
            """,
            (campaign,),
        ).fetchall()

    return {
        row["status"]:
            row["n"]
        for row in rows
    }


def command_status(args):
    with connect(args.db) as con:

        campaign = con.execute(
            """
            SELECT *
            FROM campaigns
            WHERE name = ?
            """,
            (args.campaign,),
        ).fetchone()

        if campaign is None:
            raise SystemExit(
                f"unknown campaign: {args.campaign}"
            )

        counts = get_counts(
            args.db,
            args.campaign,
        )

        provenance = con.execute(
            """
            SELECT
                COALESCE(
                    SUM(total_build_deps),
                    0
                ) AS total,
                COALESCE(
                    SUM(rdistro_build_deps),
                    0
                ) AS rdistro,
                COALESCE(
                    SUM(debian_build_deps),
                    0
                ) AS debian
            FROM jobs
            WHERE campaign = ?
              AND status = 'SUCCEEDED'
            """,
            (args.campaign,),
        ).fetchone()

        active = con.execute(
            """
            SELECT source, started_at
            FROM jobs
            WHERE campaign = ?
              AND status = 'BUILDING'
            ORDER BY started_at
            """,
            (args.campaign,),
        ).fetchall()

    total = sum(
        counts.values()
    )

    succeeded = counts.get(
        "SUCCEEDED",
        0,
    )

    pct = (
        100.0 * succeeded / total
        if total
        else 0.0
    )

    print()
    print(
        "========================================"
    )

    print(
        f" {args.campaign}"
    )

    print(
        "========================================"
    )

    print(
        f"Campaign status: {campaign['status']}"
    )

    pid = controller_pid(
        args.campaign
    )

    print(
        "Controller:      "
        + (
            f"alive (pid {pid})"
            if pid is not None
            else "not running"
        )
    )

    print(
        f"Generation:      {campaign['generation']}"
    )

    print(
        f"Coverage:        "
        f"{succeeded}/{total} "
        f"({pct:.2f}%)"
    )

    print()

    for status in (
        "PENDING",
        "RETRY",
        "BUILDING",
        "SUCCEEDED",
        "FAILED",
    ):
        print(
            f"{status:<12} "
            f"{counts.get(status, 0):>6}"
        )

    print()
    print(
        "Build-dependency provenance "
        "from successful builds:"
    )

    print(
        f"  total:      {provenance['total']}"
    )

    print(
        f"  R-Distro:   {provenance['rdistro']}"
    )

    print(
        f"  Debian:     {provenance['debian']}"
    )

    if active:

        print()
        print(
            "Active:"
        )

        for row in active:

            print(
                f"  {row['source']:<32} "
                f"{row['started_at']}"
            )


def command_retry_failed(args):
    with connect(args.db) as con:

        n = con.execute(
            """
            UPDATE jobs
            SET
                status = 'RETRY',
                failure_category = NULL,
                last_error = NULL
            WHERE campaign = ?
              AND status = 'FAILED'
            """,
            (args.campaign,),
        ).rowcount

    print(
        f"Marked {n} failed job(s) for retry."
    )


def command_pause(args):
    with connect(args.db) as con:
        campaign = con.execute(
            """
            SELECT status
            FROM campaigns
            WHERE name = ?
            """,
            (args.campaign,),
        ).fetchone()

    if campaign is None:
        raise SystemExit(
            f"unknown campaign: {args.campaign}"
        )

    set_campaign_status(
        args.db,
        args.campaign,
        "PAUSED",
    )

    pid = controller_pid(
        args.campaign
    )

    print(
        f"{args.campaign}: PAUSED"
    )

    if pid is not None:
        print(
            f"Controller PID {pid} remains alive."
        )

        print(
            "Running package builds are allowed to finish; "
            "no new packages will be claimed."
        )

    else:
        print(
            "No live controller was found."
        )


def command_resume(args):
    with connect(args.db) as con:
        campaign = con.execute(
            """
            SELECT status
            FROM campaigns
            WHERE name = ?
            """,
            (args.campaign,),
        ).fetchone()

    if campaign is None:
        raise SystemExit(
            f"unknown campaign: {args.campaign}"
        )

    pid = controller_pid(
        args.campaign
    )

    if pid is not None:
        set_campaign_status(
            args.db,
            args.campaign,
            "RUNNING",
        )

        print(
            f"{args.campaign}: RUNNING"
        )

        print(
            f"Controller PID {pid} resumed."
        )

        return

    # If the controller disappeared while paused (machine reboot,
    # crash, etc.), resume can restart it using the last run config.
    config = read_controller_config(
        args.campaign
    )

    if config is None:
        raise SystemExit(
            "No live controller and no saved run configuration. "
            "Run `rdistroctl.py run ...` once."
        )

    set_campaign_status(
        args.db,
        args.campaign,
        "RUNNING",
    )

    try:
        pid, log_path, _ = (
            start_detached_controller(
                db_path=args.db,
                campaign=args.campaign,
                parallel=int(
                    config["parallel"]
                ),
                build_jobs=int(
                    config["build_jobs"]
                ),
                only_manifest=config.get(
                    "only_manifest"
                ),
            )
        )

    except Exception:
        set_campaign_status(
            args.db,
            args.campaign,
            "PAUSED",
        )

        raise

    print(
        f"{args.campaign}: RUNNING"
    )

    print(
        f"Controller restarted as PID {pid}."
    )

    print(
        f"Controller log: {log_path}"
    )



# ======================================================================
# Dashboard
# ======================================================================

def dashboard_html(
    db_path: Path,
    campaign_name: str,
    status_filter: str | None,
):
    with connect(db_path) as con:

        campaign = con.execute(
            """
            SELECT *
            FROM campaigns
            WHERE name = ?
            """,
            (campaign_name,),
        ).fetchone()

        if campaign is None:
            return (
                "<h1>Unknown campaign</h1>"
            )

        counts = get_counts(
            db_path,
            campaign_name,
        )

        total = sum(
            counts.values()
        )

        succeeded = counts.get(
            "SUCCEEDED",
            0,
        )

        pct = (
            100.0 * succeeded / total
            if total
            else 0
        )

        provenance = con.execute(
            """
            SELECT
                COALESCE(
                    SUM(total_build_deps),
                    0
                ) total,
                COALESCE(
                    SUM(rdistro_build_deps),
                    0
                ) rdistro,
                COALESCE(
                    SUM(debian_build_deps),
                    0
                ) debian
            FROM jobs
            WHERE campaign = ?
              AND status = 'SUCCEEDED'
            """,
            (campaign_name,),
        ).fetchone()

        failures = con.execute(
            """
            SELECT
                COALESCE(
                    failure_category,
                    'unknown'
                ) category,
                COUNT(*) n
            FROM jobs
            WHERE campaign = ?
              AND status = 'FAILED'
            GROUP BY category
            ORDER BY n DESC
            """,
            (campaign_name,),
        ).fetchall()

        sql = """
            SELECT *
            FROM jobs
            WHERE campaign = ?
        """

        params = [campaign_name]

        if status_filter:

            sql += (
                " AND status = ?"
            )

            params.append(
                status_filter
            )

        sql += """
            ORDER BY
                CASE status
                    WHEN 'BUILDING' THEN 0
                    WHEN 'FAILED' THEN 1
                    WHEN 'RETRY' THEN 2
                    WHEN 'SUCCEEDED' THEN 3
                    ELSE 4
                END,
                COALESCE(
                    finished_at,
                    started_at,
                    ''
                ) DESC,
                source
            LIMIT 500
        """

        jobs = con.execute(
            sql,
            params,
        ).fetchall()

    status_cards = ""

    for status in (
        "PENDING",
        "RETRY",
        "BUILDING",
        "SUCCEEDED",
        "FAILED",
    ):

        status_cards += f"""
        <a class="card"
           href="/?status={status}">
            <div class="big">
                {counts.get(status, 0)}
            </div>
            <div>{status}</div>
        </a>
        """

    failure_html = ""

    for row in failures:

        failure_html += (
            "<span class='failure'>"
            f"{html.escape(row['category'])}: "
            f"{row['n']}"
            "</span> "
        )

    rows = ""

    for job in jobs:

        log_link = ""

        if job["latest_log"]:

            log_link = (
                f"<a href='/log?"
                f"source="
                f"{urllib.parse.quote(job['source'])}"
                f"'>log</a>"
            )

        rows += f"""
        <tr>
            <td>{html.escape(job['source'])}</td>
            <td>{html.escape(job['status'])}</td>
            <td>{job['attempts']}</td>
            <td>{job['duration_sec'] or ''}</td>
            <td>{job['rdistro_build_deps'] or 0}</td>
            <td>{job['debian_build_deps'] or 0}</td>
            <td>{html.escape(job['failure_category'] or '')}</td>
            <td>{log_link}</td>
        </tr>
        """

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>R-Distro · {html.escape(campaign_name)}</title>

<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    max-width: 1500px;
    margin: 30px auto;
    padding: 0 24px;
    background: #f5f5f7;
    color: #18181b;
}}

h1 {{
    margin-bottom: 4px;
}}

.muted {{
    color: #6b7280;
}}

.cards {{
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin: 22px 0;
}}

.card {{
    text-decoration: none;
    color: inherit;
    background: white;
    padding: 16px 22px;
    border-radius: 12px;
    min-width: 120px;
    box-shadow: 0 1px 4px #0001;
}}

.big {{
    font-size: 28px;
    font-weight: 700;
}}

.progress {{
    height: 18px;
    background: #ddd;
    border-radius: 10px;
    overflow: hidden;
}}

.progress > div {{
    height: 100%;
    width: {pct:.3f}%;
    background: #111;
}}

.panel {{
    background: white;
    margin-top: 18px;
    padding: 18px;
    border-radius: 12px;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

th, td {{
    padding: 8px 10px;
    text-align: left;
    border-bottom: 1px solid #eee;
    font-size: 13px;
}}

.failure {{
    display: inline-block;
    margin: 3px;
    padding: 5px 9px;
    background: #fee2e2;
    border-radius: 8px;
}}

a {{
    color: #2563eb;
}}
</style>
</head>

<body>

<h1>R-Distro</h1>

<div class="muted">
{html.escape(campaign_name)}
· generation {campaign['generation']}
· {campaign['status']}
</div>

<div class="panel">
<strong>
{succeeded} / {total}
({pct:.2f}%)
</strong>

<div class="progress">
<div></div>
</div>
</div>

<div class="cards">
{status_cards}
</div>

<div class="panel">
<h3>Build provenance</h3>

R-Distro:
<strong>{provenance['rdistro']}</strong>
&nbsp;&nbsp;

Debian:
<strong>{provenance['debian']}</strong>
&nbsp;&nbsp;

Total:
<strong>{provenance['total']}</strong>
</div>

<div class="panel">
<h3>Failure categories</h3>
{failure_html or "None"}
</div>

<div class="panel">
<h3>Jobs</h3>

<a href="/">all</a>
&nbsp;

<a href="/?status=FAILED">failed</a>
&nbsp;

<a href="/?status=BUILDING">building</a>
&nbsp;

<a href="/?status=SUCCEEDED">succeeded</a>

<table>
<thead>
<tr>
<th>source</th>
<th>status</th>
<th>attempt</th>
<th>seconds</th>
<th>rdistro deps</th>
<th>debian deps</th>
<th>failure</th>
<th></th>
</tr>
</thead>

<tbody>
{rows}
</tbody>
</table>
</div>

</body>
</html>
"""


def command_dashboard(args):
    db_path = args.db
    campaign = args.campaign

    class Handler(
        BaseHTTPRequestHandler
    ):
        def log_message(
            self,
            fmt,
            *values,
        ):
            return

        def do_GET(self):
            parsed = urllib.parse.urlparse(
                self.path
            )

            query = urllib.parse.parse_qs(
                parsed.query
            )

            if parsed.path == "/log":

                source = query.get(
                    "source",
                    [""],
                )[0]

                with connect(db_path) as con:

                    row = con.execute(
                        """
                        SELECT latest_log
                        FROM jobs
                        WHERE campaign = ?
                          AND source = ?
                        """,
                        (
                            campaign,
                            source,
                        ),
                    ).fetchone()

                if (
                    row is None
                    or not row["latest_log"]
                ):
                    body = (
                        "No log available."
                    )

                else:

                    path = Path(
                        row["latest_log"]
                    )

                    if path.exists():

                        lines = path.read_text(
                            errors="replace"
                        ).splitlines()

                        body = "\n".join(
                            lines[-500:]
                        )

                    else:
                        body = (
                            "Log file missing."
                        )

                data = body.encode()

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "text/plain; charset=utf-8",
                )

                self.end_headers()

                self.wfile.write(data)

                return

            status_filter = query.get(
                "status",
                [None],
            )[0]

            body = dashboard_html(
                db_path,
                campaign,
                status_filter,
            ).encode()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8",
            )

            self.end_headers()

            self.wfile.write(body)

    server = ThreadingHTTPServer(
        (
            args.host,
            args.port,
        ),
        Handler,
    )

    print(
        f"Dashboard: "
        f"http://{args.host}:{args.port}"
    )

    server.serve_forever()


# ======================================================================
# Doctor
# ======================================================================

def command_doctor(args):
    errors = []

    if not BUILD_SCRIPT.exists():

        errors.append(
            f"missing: {BUILD_SCRIPT}"
        )

    else:

        if "RDISTRO_OUTPUT_DIR" not in (
            BUILD_SCRIPT.read_text(
                errors="replace"
            )
        ):
            errors.append(
                "build-package.sh does not "
                "support RDISTRO_OUTPUT_DIR"
            )

    if shutil.which(
        "docker"
    ) is None:

        errors.append(
            "docker not found"
        )

    manifest = Path(
        args.manifest
    ).resolve()

    if not manifest.exists():

        errors.append(
            f"manifest missing: {manifest}"
        )

    if errors:

        print(
            "FAILED:"
        )

        for error in errors:
            print(
                f"  {error}"
            )

        raise SystemExit(1)

    print(
        "R-Distro controller doctor: OK"
    )

    print(
        f"Build script SHA256: "
        f"{sha256_file(BUILD_SCRIPT)}"
    )

    print(
        f"Git commit: "
        f"{git_commit()}"
    )

    print(
        f"Boundary sources: "
        f"{len(read_manifest(manifest))}"
    )


# ======================================================================
# CLI
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "R-Distro build campaign controller"
        )
    )

    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    p = sub.add_parser(
        "doctor"
    )

    p.add_argument(
        "--manifest",
        default=(
            "manifests/"
            "selfhost-boundary-arm64.txt"
        ),
    )

    p.set_defaults(
        func=command_doctor
    )

    p = sub.add_parser(
        "plan"
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.add_argument(
        "--generation",
        type=int,
        required=True,
    )

    p.add_argument(
        "--manifest",
        required=True,
    )

    p.set_defaults(
        func=command_plan
    )

    p = sub.add_parser(
        "run"
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.add_argument(
        "--parallel",
        type=int,
        default=2,
    )

    p.add_argument(
        "--build-jobs",
        type=int,
        default=4,
    )

    p.add_argument(
        "--only-manifest",
        default=None,
    )

    p.set_defaults(
        func=command_run
    )

    # Internal detached controller. Kept out of normal help output.
    p = sub.add_parser(
        "_controller",
        help=argparse.SUPPRESS,
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.add_argument(
        "--parallel",
        type=int,
        required=True,
    )

    p.add_argument(
        "--build-jobs",
        type=int,
        required=True,
    )

    p.add_argument(
        "--only-manifest",
        default=None,
    )

    p.set_defaults(
        func=command_controller
    )

    p = sub.add_parser(
        "status"
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.set_defaults(
        func=command_status
    )

    p = sub.add_parser(
        "retry-failed"
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.set_defaults(
        func=command_retry_failed
    )

    p = sub.add_parser(
        "pause"
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.set_defaults(
        func=command_pause
    )

    p = sub.add_parser(
        "resume"
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.set_defaults(
        func=command_resume
    )

    p = sub.add_parser(
        "dashboard"
    )

    p.add_argument(
        "--campaign",
        required=True,
    )

    p.add_argument(
        "--host",
        default="127.0.0.1",
    )

    p.add_argument(
        "--port",
        type=int,
        default=8765,
    )

    p.set_defaults(
        func=command_dashboard
    )

    rdistro_repo.register_subcommands(sub)

    args = parser.parse_args()

    init_db(args.db)

    args.func(args)


if __name__ == "__main__":
    main()