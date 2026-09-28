#!/usr/bin/env python3
"""Independent Yuntu script workbench: collect, transcribe, report, then analyze."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

from collector import main as collector_main
from fact_packages import FactPackageError, build_fact_packages
from runtime_paths import RuntimePathError, external_runtime_path
from script_report import ScriptReportError, write_script_report
from script_transcription import FasterWhisperProvider, TranscriptionError, transcribe_run


DEFAULT_OUTPUT_FOLDER = "云图素材脚本数据"


def _acquire_run_dir(output_root: str | None, output_dir: str | None) -> Path:
    if output_dir:
        return external_runtime_path(output_dir, label="run directory")
    root = external_runtime_path(output_root or Path.cwd() / DEFAULT_OUTPUT_FOLDER, label="output root")
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    run_dir = root / run_id
    if run_dir.exists():
        raise RuntimePathError(f"run directory already exists: {run_dir}")
    return run_dir


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
        destination = parser.add_mutually_exclusive_group()
        destination.add_argument("--output-root", help=f"output root; defaults to cwd/{DEFAULT_OUTPUT_FOLDER}")
        destination.add_argument("--output-dir", help="exact run directory for an existing workflow")
        parser.add_argument("--model-dir", default=os.environ.get("YUNTU_ASR_MODEL_DIR"))
        parser.add_argument("--asr-hotwords", default="", help="optional brand/product terms to guide ASR (max 256 characters)")
    else:
        parser.add_argument("--run-dir", required=True)
        if args[0] == "process-scripts":
            parser.add_argument("--model-dir", default=os.environ.get("YUNTU_ASR_MODEL_DIR"))
            parser.add_argument("--asr-hotwords", default="", help="optional brand/product terms to guide ASR (max 256 characters)")
            parser.add_argument("--retry-low-memory", action="store_true",
                                help="retry cached memory_safe transcripts in quality mode; preserve old text on failure")
    parsed = parser.parse_args(args[1:])
    try:
        if args[0] == "acquire":
            run_dir = _acquire_run_dir(parsed.output_root, parsed.output_dir)
            model_dir = Path(parsed.model_dir) if parsed.model_dir else None
            provider = FasterWhisperProvider(model_dir, hotwords=parsed.asr_hotwords)
            provider.preflight()
            print(json.dumps({"runDir": str(run_dir), "status": "starting"}, ensure_ascii=False))
            collect_args = ["collect", "--shop-id", parsed.shop_id, "--query-spec", parsed.query_spec,
                            "--highlight-mode", "off", "--output-dir", str(run_dir)]
            status = collector_main(collect_args)
            if status != 0:
                return status
        else:
            run_dir = external_runtime_path(parsed.run_dir, label="run directory")
            model_dir = Path(parsed.model_dir) if args[0] == "process-scripts" and parsed.model_dir else None
            provider = FasterWhisperProvider(model_dir, hotwords=parsed.asr_hotwords) if args[0] == "process-scripts" else None
        processing_counts = {}
        manifest = {}
        if args[0] != "render-script-report":
            provider.preflight()
            manifest = transcribe_run(run_dir, provider=provider,
                                      retry_low_memory=args[0] == "process-scripts" and parsed.retry_low_memory)
            processing_counts = manifest["counts"]
            print(json.dumps({"transcription": processing_counts,
                              "qualityRetries": manifest.get("qualityRetries")}, ensure_ascii=False))
            build_fact_packages(run_dir)
        receipt = write_script_report(run_dir)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 2 if manifest.get("qualityRetries", {}).get("failedVideos", 0) or any(processing_counts.get(state) for state in (
            "transcription_failed", "audio_decode_failed", "audio_timeout", "ffmpeg_unavailable", "audio_empty"
        )) else 0
    except (TranscriptionError, FactPackageError, ScriptReportError, RuntimePathError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
