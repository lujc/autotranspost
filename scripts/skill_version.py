#!/usr/bin/env python3
"""Lightweight git version-management helper for the autopublish skill.

The autopublish skill directory IS a git repository. This script wraps the
common versioning operations so skill edits can be snapshotted and rolled
back without remembering git plumbing.

The skill root is auto-detected as the parent directory of this file, so
the helper works no matter where it is invoked from. Nothing outside the
skill directory is ever touched.

Usage:
    python skill_version.py commit "message"   # stage everything + commit
    python skill_version.py status             # short status + diff stat
    python skill_version.py log [N]            # last N commits (default 10)
    python skill_version.py diff  [ref]        # diff vs HEAD (or a ref)
    python skill_version.py init               # git init if not already
    python skill_version.py tag <name> [msg]  # lightweight tag a release

Exit codes:
    0  success
    1  git error / bad arguments
    2  nothing to commit
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
GIT = "git"


def _run(args, capture=True, check=True):
    """Run a git command inside the skill root. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        [GIT, *args],
        cwd=str(SKILL_ROOT),
        capture_output=capture,
        text=True,
    )
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr or f"git {' '.join(args)} failed\n")
        sys.exit(1)
    return proc.returncode, proc.stdout, proc.stderr


def _ensure_repo():
    if not (SKILL_ROOT / ".git").exists():
        _run(["init"], check=True)
        print(f"Initialized git repo at {SKILL_ROOT}")
    # Make sure a commit identity exists (use a local default if none set).
    rc_name, name, _ = _run(["config", "user.name"], check=False)
    rc_mail, mail, _ = _run(["config", "user.email"], check=False)
    if rc_name != 0 or not name.strip() or rc_mail != 0 or not mail.strip():
        _run(["config", "user.name", "autopublish-skill"], check=True)
        _run(["config", "user.email", "autopublish@local"], check=True)
        print("note: set repo-local git identity (autopublish-skill <autopublish@local>)")
    return True


def cmd_init(_args):
    _ensure_repo()
    rc, out, _ = _run(["rev-parse", "--short", "HEAD"], check=False)
    if rc == 0:
        print(f"Repository already initialized. HEAD = {out.strip()}")
    else:
        print("Repository initialized (no commits yet).")


def cmd_status(_args):
    _ensure_repo()
    rc, out, _ = _run(["status", "-s"], check=False)
    if rc == 0 and not out.strip():
        print("working tree clean — no changes to version.")
    else:
        print("=== git status ===")
        print(out or "")
        print("=== unstaged/staged diffstat ===")
        _, ds, _ = _run(["diff", "--stat", "HEAD"], check=False)
        print(ds or "(no diff vs HEAD)")


def cmd_log(args):
    _ensure_repo()
    n = args[0] if args else "10"
    _, out, _ = _run(["log", f"-{n}", "--oneline", "--decorate"], check=False)
    if not out.strip():
        print("(no commits yet)")
        return
    print(out.rstrip())


def cmd_diff(args):
    _ensure_repo()
    ref = args[0] if args else "HEAD"
    _, out, _ = _run(["diff", ref], check=False)
    print(out or f"(no diff vs {ref})")


def cmd_tag(args):
    if not args:
        sys.stderr.write("usage: skill_version.py tag <name> [message]\n")
        sys.exit(1)
    _ensure_repo()
    name = args[0]
    msg = args[1] if len(args) > 1 else f"release: {name}"
    _run(["tag", "-a", name, "-m", msg], check=True)
    print(f"tagged {name}")


def cmd_commit(args):
    if not args:
        sys.stderr.write('usage: skill_version.py commit "message"\n')
        sys.exit(1)
    message = args[0]
    _ensure_repo()
    _run(["add", "-A"], check=True)
    rc, _, _ = _run(["diff", "--cached", "--quiet"], check=False)
    if rc == 0:
        print("nothing to commit — working tree matches last commit.")
        sys.exit(2)
    _run(["commit", "-m", message], check=True)
    _, head, _ = _run(["log", "-1", "--oneline", "--decorate"], check=False)
    print(f"committed: {head.strip()}")
    print("--- remaining status ---")
    _, st, _ = _run(["status", "-s"], check=False)
    print(st or "(clean)")


_COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "log": cmd_log,
    "diff": cmd_diff,
    "tag": cmd_tag,
    "commit": cmd_commit,
}


def main(argv):
    if not argv:
        print(__doc__)
        sys.exit(0)
    cmd, rest = argv[0], argv[1:]
    handler = _COMMANDS.get(cmd)
    if handler is None:
        sys.stderr.write(f"unknown command: {cmd}\n\n{__doc__}\n")
        sys.exit(1)
    handler(rest)


if __name__ == "__main__":
    main(sys.argv[1:])
