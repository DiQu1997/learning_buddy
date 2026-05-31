"""
CLI entry point. Three subcommands: `run`, `status`, `config`.

`run` is a single, one-shot pass. For long-running, wrap in cron/launchd —
no internal supervisor. A simple file lock at <metadata>/.learning-buddy.lock
prevents two concurrent invocations on the same machine.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from . import catalog as cat
from .agent import Agent
from .catalog import Catalog
from .config import (
    apply_kv_update,
    default_config_path,
    load_config,
    save_config,
)
from .store import Store, meta_key, queue_key
from .utils import utc_now_iso


LOCK_FILENAME = ".learning-buddy.lock"


class LockBusy(RuntimeError):
    pass


def _run_lock_path(repo: Path) -> Path:
    """Local lock for one machine. Live inside the clone's .git dir so it is never
    tracked or pushed; fall back to the repo root if .git isn't a directory yet."""
    repo = Path(repo).expanduser()
    git_dir = repo / ".git"
    return (git_dir if git_dir.is_dir() else repo) / LOCK_FILENAME


@contextmanager
def acquire_run_lock(lock_path: Path):
    """Take an exclusive flock for the duration of a run (one run per machine)."""
    lock_path = Path(lock_path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise LockBusy(
                f"Another learning-buddy run is in progress (lock at {lock_path}). Aborting."
            )
        fh.write(f"pid={os.getpid()}\nstarted={utc_now_iso()}\n")
        fh.flush()
        try:
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fh.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="learning-buddy",
        description="Inbox → classify → NotebookLM. Git-backed catalog via gitkv.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="One-shot pass: intake new files, advance unfinished tasks.")

    status = sub.add_parser("status", help="Print catalog summary and recent resources.")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    status.add_argument("--limit", type=int, default=20, help="Recent resources to show.")

    migrate = sub.add_parser(
        "migrate", help="Import a legacy JSON metadata dir into the gitkv store."
    )
    migrate.add_argument(
        "--from",
        dest="from_dir",
        required=True,
        help="Old metadata dir containing catalog.json and resources/*.json.",
    )

    cfg = sub.add_parser("config", help="Read and modify the config file.")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    cfg_sub.add_parser("show", help="Print the current config.")
    cfg_sub.add_parser("path", help="Print the config file path.")
    set_cmd = cfg_sub.add_parser("set", help="Set a config key.")
    set_cmd.add_argument("key", help="Config key (e.g. inbox, library, kv.repo, llm.model).")
    set_cmd.add_argument("value", help="Value as string.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "migrate":
            return _cmd_migrate(args)
        if args.command == "config":
            return _cmd_config(args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _cmd_run(_args: argparse.Namespace) -> int:
    config = load_config()
    config.resolved_paths()  # validate inbox/library are set
    repo = config.kv_repo()

    auth_rc = _ensure_nlm_auth()
    if auth_rc != 0:
        return auth_rc

    try:
        with acquire_run_lock(_run_lock_path(repo)):
            store = Store.open(repo, config.kv.table)
            agent = Agent(config, store=store)
            summary = agent.run()
            print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
            return 0
    except LockBusy as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _ensure_nlm_auth() -> int:
    """
    Check NotebookLM authentication via `nlm login --check`. If expired:
    - interactive terminal: spawn `nlm login` for the user to complete, then re-check
    - non-interactive (cron/launchd): print a clear error and return a non-zero code
    Returns 0 on success, non-zero on failure.
    """
    if _nlm_check_ok():
        return 0

    if not sys.stdin.isatty():
        print(
            "NLM authentication failed and no terminal is attached for interactive `nlm login`.\n"
            "Run `nlm login` manually, then re-run `learning-buddy run`.",
            file=sys.stderr,
        )
        return 3

    print("NLM authentication failed. Launching `nlm login` interactively...", file=sys.stderr)
    login = subprocess.run(["nlm", "login"])
    if login.returncode != 0:
        print(f"`nlm login` exited {login.returncode}; aborting.", file=sys.stderr)
        return 3

    if not _nlm_check_ok():
        print("Still not authenticated after `nlm login`; aborting.", file=sys.stderr)
        return 3

    return 0


def _nlm_check_ok() -> bool:
    proc = subprocess.run(["nlm", "login", "--check"], capture_output=True, text=True)
    return proc.returncode == 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store.open(config.kv_repo(), config.kv.table)
    catalog = Catalog.load(store)
    counts = catalog.counts()

    if args.json:
        print(
            json.dumps(
                {"counts": counts, "resources": catalog.resources[-args.limit :]},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(f"kv repo: {config.kv_repo()}")
    print(f"table:   {config.kv.table}")
    for label in ("total", "new", "in_progress", "done", "failed"):
        print(f"  {label:>14s}: {counts[label]}")

    print()
    print(f"recent resources (last {args.limit}):")
    for entry in catalog.resources[-args.limit :]:
        title = entry.get("title") or entry["id"]
        category = "/".join(entry.get("category") or []) or "?"
        status = entry.get("overall_status") or "?"
        print(f"  [{status:>11s}] {title}  →  {category}")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store.open(config.kv_repo(), config.kv.table)
    src = Path(args.from_dir).expanduser()
    if not src.is_dir():
        print(f"no such directory: {src}", file=sys.stderr)
        return 1

    n_meta = 0
    catalog_path = src / "catalog.json"
    if catalog_path.exists():
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        for entry in data.get("resources", []):
            rid = entry.get("id")
            if not rid:
                continue
            store.write(meta_key(rid), json.dumps(entry, indent=2, ensure_ascii=False))
            n_meta += 1

    n_queue = 0
    resources_dir = src / "resources"
    if resources_dir.is_dir():
        for path in sorted(resources_dir.glob("*.json")):
            store.write(queue_key(path.stem), path.read_text(encoding="utf-8"))
            n_queue += 1

    print(f"migrated {n_meta} meta + {n_queue} queue records into {config.kv_repo()}")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "show":
        config = load_config()
        print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
        return 0
    if args.config_command == "path":
        print(default_config_path())
        return 0
    if args.config_command == "set":
        config = load_config()
        apply_kv_update(config, args.key, args.value)
        save_config(config)
        print(f"updated {args.key}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
