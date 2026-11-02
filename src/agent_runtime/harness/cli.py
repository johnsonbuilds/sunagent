"""Harness management CLI: tree, derive, record, report, annotate."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_runtime import evaluation, lineage

from .schema import HarnessError


def main(argv: list[str] | None = None) -> int:
    """Harness management CLI: tree, derive, record, report, annotate."""
    parser = argparse.ArgumentParser(
        prog="python -m agent_runtime.harness",
        description="Manage harness manifests, evaluation records, and reports.")
    commands = parser.add_subparsers(dest="command", required=True)

    tree_parser = commands.add_parser("tree", help="show the harness family tree")
    tree_parser.add_argument("--harnesses-dir", type=Path,
                             default=lineage.DEFAULT_HARNESSES_DIR)

    derive_parser = commands.add_parser(
        "derive", help="create a child manifest from a parent")
    derive_parser.add_argument("child_id")
    derive_parser.add_argument("--from", dest="parent", required=True,
                               metavar="PARENT_ID")
    derive_parser.add_argument("--mutation", required=True,
                               help="one-line description of the gene change")
    derive_parser.add_argument("--reason", default=None)
    derive_parser.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                               help="gene override, e.g. verification.enabled=true "
                                    "(repeatable)")
    derive_parser.add_argument("--harnesses-dir", type=Path,
                               default=lineage.DEFAULT_HARNESSES_DIR)
    derive_parser.add_argument("--yes", action="store_true",
                               help="write without showing the diff first")

    record_parser = commands.add_parser(
        "record", help="extract an evaluation record from a jobs directory")
    record_parser.add_argument("job_dir", type=Path)
    record_parser.add_argument("--model", default=None)
    record_parser.add_argument("--benchmark", default=None,
                               help="override the benchmark name")
    record_parser.add_argument("--notes", default=None)
    record_parser.add_argument("--records-dir", type=Path,
                               default=evaluation.DEFAULT_RECORDS_DIR)

    report_parser = commands.add_parser(
        "report", help="aggregate records along the harness lineage")
    report_parser.add_argument("--harnesses-dir", type=Path,
                               default=lineage.DEFAULT_HARNESSES_DIR)
    report_parser.add_argument("--records-dir", type=Path,
                               default=evaluation.DEFAULT_RECORDS_DIR)

    annotate_parser = commands.add_parser(
        "annotate", help="set the failure mode of one task in a record")
    annotate_parser.add_argument("record")
    annotate_parser.add_argument("task")
    annotate_parser.add_argument("--mode", required=True)
    annotate_parser.add_argument("--records-dir", type=Path,
                                 default=evaluation.DEFAULT_RECORDS_DIR)

    args = parser.parse_args(argv)

    if args.command == "tree":
        specs = lineage.load_all_harnesses(args.harnesses_dir)
        problems = lineage.validate_lineage(specs)
        print(lineage.format_tree(specs))
        if problems:
            print()
            for problem in problems:
                print(f"warning: {problem}")
            return 1
        return 0

    if args.command == "derive":
        specs = lineage.load_all_harnesses(args.harnesses_dir)
        parent = specs.get(args.parent)
        if parent is None:
            raise HarnessError(
                f"parent harness {args.parent!r} not found in {args.harnesses_dir}")
        if args.child_id in specs:
            raise HarnessError(f"harness id {args.child_id!r} already exists")
        sets = []
        for item in args.set:
            path, sep, raw = item.partition("=")
            if not sep or not path or not raw:
                raise HarnessError(f"--set expects PATH=VALUE, got {item!r}")
            sets.append((path, raw))
        child, diff = lineage.derive(parent, args.child_id, args.mutation,
                                     args.reason, sets)
        target = args.harnesses_dir / f"{child.id}.yaml"
        print(f"deriving {child.id} from {parent.id}")
        print(diff)
        if not args.yes:
            try:
                answer = input(f"\nwrite {target}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in {"y", "yes"}:
                print("aborted")
                return 1
        target.write_text(lineage.manifest_text(child), encoding="utf-8")
        print(f"wrote {target}")
        return 0

    if args.command == "record":
        record = evaluation.extract_record(args.job_dir, model=args.model,
                                           notes=args.notes,
                                           benchmark=args.benchmark)
        path = evaluation.write_record(record, args.records_dir)
        score = record["score"]
        print(f"wrote {path}")
        print(f"harness={record['harness_id']} benchmark={record['benchmark']} "
              f"rewards={score['rewards']}/{score['total']} "
              f"accuracy={score['accuracy']}")
        return 0

    if args.command == "report":
        specs = lineage.load_all_harnesses(args.harnesses_dir)
        problems = lineage.validate_lineage(specs)
        records = evaluation.load_records(args.records_dir)
        rows = evaluation.build_report(specs, records)
        print(evaluation.format_report(rows))
        if problems:
            print()
            for problem in problems:
                print(f"warning: {problem}")
            return 1
        return 0

    if args.command == "annotate":
        path = evaluation.annotate_record(args.record, args.task, args.mode,
                                          args.records_dir)
        print(f"annotated {path}")
        return 0

    return 2
