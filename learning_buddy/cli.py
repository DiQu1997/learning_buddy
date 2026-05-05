from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import Agent
from .catalog import Catalog
from .config import (
    AppConfig,
    apply_kv_update,
    default_config_path,
    load_config,
    save_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="learning-buddy",
        description="Inbox → classify → NotebookLM, with a JSON catalog under git.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Process new files in the inbox and resume any in-flight work.")
    run.add_argument("--limit", type=int, default=None, help="Process at most N active files this run.")
    run.add_argument("--no-push", action="store_true", help="Skip the optional `git push` even if auto_push is on.")

    status = sub.add_parser("status", help="Print catalog counts and the most recent files.")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    status.add_argument("--limit", type=int, default=20, help="Recent files to show (default 20).")

    cfg = sub.add_parser("config", help="Read and modify the config file.")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    cfg_sub.add_parser("show", help="Print the current config.")
    cfg_sub.add_parser("path", help="Print the config file path.")
    set_cmd = cfg_sub.add_parser("set", help="Set a config key.")
    set_cmd.add_argument("key", help="Config key (e.g. inbox, library, database, llm.model).")
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
        if args.command == "config":
            return _cmd_config(args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    agent = Agent(config)
    summary = agent.run(limit=args.limit, skip_push=args.no_push)
    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    paths = config.resolved_paths()
    catalog = Catalog.load(paths["database"])
    counts = catalog.counts()

    if args.json:
        print(json.dumps({"counts": counts, "files": catalog.files[-args.limit :]}, indent=2, ensure_ascii=False))
        return 0

    print(f"catalog: {catalog.path}")
    print(f"updated: {catalog.data.get('updated_at')}")
    for label in ("files_total", "files_done", "files_in_progress", "files_failed", "files_duplicate", "notebooks", "resources"):
        print(f"  {label:>20s}: {counts[label]}")

    print()
    print(f"recent files (last {args.limit}):")
    for entry in catalog.files[-args.limit :]:
        title = entry.get("title") or entry.get("original_filename") or entry["id"]
        cat = "/".join(entry.get("category") or []) or "?"
        print(f"  [{entry['status']:>10s}] {title}  →  {cat}")
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
