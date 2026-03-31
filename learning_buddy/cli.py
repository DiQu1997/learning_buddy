from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import normalize_artifact_type
from .engine import WorkflowEngine
from .utils import parse_json_list


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="learning-buddy",
        description="State-machine workflow engine for PDF -> NotebookLM learning artifacts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    process = sub.add_parser("process", help="Process one or more PDFs end-to-end.")
    process.add_argument("pdfs", nargs="+", help="Input PDF paths.")
    process.add_argument("--name", help="Notebook/job name.")
    process.add_argument("--max-pages", type=int, help="Max pages per chunk.")
    process.add_argument("--artifacts", help="Comma-separated artifact list.")
    process.add_argument("--tags", help="Comma-separated tags.")

    status = sub.add_parser("status", help="Show job status.")
    status.add_argument("job_id", help="Job ID.")

    inspect = sub.add_parser("inspect", help="Inspect workflow DB with structured progress output.")
    inspect.add_argument("job_id", nargs="?", help="Optional job ID. If omitted, recent jobs are returned.")
    inspect.add_argument("--limit", type=int, default=10, help="Max recent jobs when job_id is omitted.")
    inspect.add_argument(
        "--include-completed",
        action="store_true",
        help="Include jobs already in DONE state in list mode.",
    )
    inspect.add_argument(
        "--pending-limit",
        type=int,
        default=25,
        help="Max pending task rows included per job.",
    )
    inspect.add_argument(
        "--no-task-details",
        action="store_true",
        help="Disable pending task row details and return counts only.",
    )

    resume = sub.add_parser("resume", help="Resume a failed or interrupted job.")
    resume.add_argument("job_id", help="Job ID.")

    jobs = sub.add_parser("jobs", help="List recent jobs.")
    jobs.add_argument("--limit", type=int, default=50, help="Max rows to return.")

    library = sub.add_parser("library", help="List notebook registry.")
    library.add_argument("--tag", help="Filter by tag.")
    library.add_argument("--type", help="Filter by doc type.")

    info = sub.add_parser("info", help="Show notebook details from local registry.")
    info.add_argument("notebook", help="Notebook name, local ID, or NotebookLM ID.")

    artifacts = sub.add_parser("artifacts", help="List generated artifacts from registry.")
    artifacts.add_argument("--type", help="Artifact type filter.")
    artifacts.add_argument("--notebook", help="Notebook filter (name or ID).")

    download = sub.add_parser("download", help="Re-download an artifact by local/remote ID.")
    download.add_argument("artifact_id", help="Artifact local ID or NotebookLM artifact ID.")

    generate = sub.add_parser("generate", help="Generate additional artifacts for an existing source.")
    generate.add_argument("source_id", help="Source local ID or NotebookLM source ID.")
    generate.add_argument("--artifacts", required=True, help="Comma-separated artifact types.")

    open_cmd = sub.add_parser("open", help="Open NotebookLM in browser for a notebook.")
    open_cmd.add_argument("notebook", help="Notebook name, local ID, or NotebookLM ID.")

    query = sub.add_parser("query", help="Query a notebook.")
    query.add_argument("notebook", help="Notebook name, local ID, or NotebookLM ID.")
    query.add_argument("question", help="Question text.")

    return parser


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = WorkflowEngine(workspace=Path.cwd())
    try:
        if args.command == "process":
            artifacts = parse_json_list(args.artifacts)
            tags = parse_json_list(args.tags)
            result = engine.process(
                input_paths=list(args.pdfs),
                name=args.name,
                tags=tags,
                artifacts=artifacts or None,
                max_pages=args.max_pages,
            )
            _emit(result)
            return 0

        if args.command == "status":
            _emit(engine.status(args.job_id))
            return 0

        if args.command == "inspect":
            _emit(
                engine.inspect(
                    job_id=args.job_id,
                    limit=args.limit,
                    include_completed=args.include_completed,
                    include_task_details=not args.no_task_details,
                    pending_limit=args.pending_limit,
                )
            )
            return 0

        if args.command == "resume":
            _emit(engine.resume(args.job_id))
            return 0

        if args.command == "jobs":
            _emit(engine.jobs(limit=args.limit))
            return 0

        if args.command == "library":
            _emit(engine.library(tag=args.tag, doc_type=args.type))
            return 0

        if args.command == "info":
            _emit(engine.info(args.notebook))
            return 0

        if args.command == "artifacts":
            artifact_type = normalize_artifact_type(args.type) if args.type else None
            _emit(engine.list_artifacts(artifact_type=artifact_type, notebook_identifier=args.notebook))
            return 0

        if args.command == "download":
            _emit(engine.download_artifact(args.artifact_id))
            return 0

        if args.command == "generate":
            artifact_values = parse_json_list(args.artifacts)
            _emit(engine.generate_for_source(args.source_id, artifact_values))
            return 0

        if args.command == "open":
            url = engine.open_notebook(args.notebook)
            _emit({"url": url})
            return 0

        if args.command == "query":
            output = engine.query_notebook(args.notebook, args.question)
            _emit({"answer": output})
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
