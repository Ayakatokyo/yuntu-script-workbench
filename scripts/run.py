#!/usr/bin/env python3
"""Independent Yuntu script workbench: collect, transcribe, report, then analyze."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from collector import main as collector_main
from fact_packages import FactPackageError, build_fact_packages
from runtime_paths import RuntimePathError, external_runtime_path
from script_report import ScriptReportError, write_script_report
from script_transcription import FasterWhisperProvider, TranscriptionError, transcribe_run


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: run.py acquire|process-scripts|render-script-report|<collector command> ...")
        print("collector commands: collect, status, validate, runtime-check, list-accounts, schema, build-facts, render-overview, workflow, analyze")
        return 0
    if args[0] not in {"acquire", "process-scripts", "render-script-report"}:
        return collector_main(args)
    parser = argparse.ArgumentParser(prog=f"run.py {args[0]}")
    if args[0] == "acquire":
        parser.add_argument("--shop-id", required=True)
        parser.add_argument("--query-spec", required=True)
        parser.add_argument("--highlight-spec")
        parser.add_argument("--highlight-mode", choices=("on", "off"), default="off")
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--model-dir", default=os.environ.get("YUNTU_ASR_MODEL_DIR"))
    else:
        parser.add_argument("--run-dir", required=True)
        if args[0] == "process-scripts":
            parser.add_argument("--model-dir", default=os.environ.get("YUNTU_ASR_MODEL_DIR"))
    parsed = parser.parse_args(args[1:])
    try:
        if args[0] == "acquire":
            collect_args = ["collect", "--shop-id", parsed.shop_id, "--query-spec", parsed.query_spec,
                            "--highlight-mode", parsed.highlight_mode, "--output-dir", parsed.output_dir]
            if parsed.highlight_spec:
                collect_args += ["--highlight-spec", parsed.highlight_spec]
            status = collector_main(collect_args)
            if status != 0:
                return status
            run_dir = external_runtime_path(parsed.output_dir, label="run directory")
            model_dir = Path(parsed.model_dir) if parsed.model_dir else None
        else:
            run_dir = external_runtime_path(parsed.run_dir, label="run directory")
            model_dir = Path(parsed.model_dir) if args[0] == "process-scripts" and parsed.model_dir else None
        processing_counts = {}
        if args[0] != "render-script-report":
            manifest = transcribe_run(run_dir, provider=FasterWhisperProvider(model_dir))
            processing_counts = manifest["counts"]
            print(json.dumps({"transcription": processing_counts}, ensure_ascii=False))
            build_fact_packages(run_dir)
        receipt = write_script_report(run_dir)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 2 if any(processing_counts.get(state) for state in (
            "transcription_failed", "audio_decode_failed", "audio_timeout", "ffmpeg_unavailable", "audio_empty"
        )) else 0
    except (TranscriptionError, FactPackageError, ScriptReportError, RuntimePathError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
