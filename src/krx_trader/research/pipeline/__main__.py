from __future__ import annotations

import argparse
import json
from pathlib import Path

from krx_trader.research.pipeline.config import write_static_phase14_artifacts
from krx_trader.research.pipeline.runner import run_phase14


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy-free Phase 14 factor viability pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser(
        "freeze", help="write/freeze ledger, registry, degrees of freedom, and config hash"
    )
    freeze.add_argument("--output", type=Path, default=Path("runtime/research/phase14"))
    run = subparsers.add_parser("run", help="run the frozen pre-strategy factor viability pipeline")
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.add_argument("--output", type=Path, default=Path("runtime/research/phase14"))
    args = parser.parse_args()
    if args.command == "freeze":
        print(json.dumps(write_static_phase14_artifacts(args.output), indent=2, sort_keys=True))
        return 0
    output = run_phase14(args.repo.resolve(), args.output.resolve())
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
