#!/usr/bin/env python3
import glob
import os
import re
import sys
import time

PATTERN = sys.argv[1] if len(sys.argv) > 1 else \
    "work/campaigns/gen3-bootstrap/llvm-toolchain-21/attempt-*/build.log"

PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")
SOURCE   = re.compile(r"(?:^|\s)-c\s+(\S+)")

# ANSI
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
WHITE  = "\033[97m"

def latest_log():
    files = glob.glob(PATTERN)
    return max(files, key=os.path.getmtime) if files else None

def draw(done, total, filename):
    pct = 100 * done / total if total else 0
    width = min(50, max(20, os.get_terminal_size().columns - 25))
    filled = round(width * done / total) if total else 0

    bar = f"{GREEN}{'━' * filled}{DIM}{'━' * (width-filled)}{RESET}"

    sys.stdout.write(
        "\r\033[2K"
        f"{CYAN}{BOLD}⚡ BUILD{RESET}  "
        f"{bar}  "
        f"{WHITE}{BOLD}{pct:6.2f}%{RESET}  "
        f"{DIM}{done}/{total}{RESET}\n"
        "\033[2K"
        f"   {YELLOW}▸ {BOLD}{filename}{RESET}"
        "\033[1A"
    )
    sys.stdout.flush()

log = None
f = None
done = total = 0
filename = "waiting for build…"

# Hide terminal cursor
sys.stdout.write("\033[?25l")
sys.stdout.flush()

try:
    while True:
        newest = latest_log()

        if newest != log:
            if f:
                f.close()
            log = newest

            if not log:
                time.sleep(0.5)
                continue

            f = open(log, "r", errors="replace")

            # Read existing log so dashboard immediately knows current state.
            for line in f:
                m = PROGRESS.search(line)
                if m:
                    done, total = map(int, m.groups())
                    s = SOURCE.search(line)
                    if s:
                        filename = os.path.basename(s.group(1))

            draw(done, total, filename)

        line = f.readline()

        if not line:
            time.sleep(0.15)
            continue

        m = PROGRESS.search(line)
        if not m:
            continue

        done, total = map(int, m.groups())

        s = SOURCE.search(line)
        if s:
            filename = os.path.basename(s.group(1))

        draw(done, total, filename)

except KeyboardInterrupt:
    pass
finally:
    # Always restore cursor, even on Ctrl-C
    sys.stdout.write("\033[?25h\n")
    sys.stdout.flush()