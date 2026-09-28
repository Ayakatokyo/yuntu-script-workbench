#!/usr/bin/env python3
"""Independent Yuntu script workbench: collect, prepare audio, import text, report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid
import time

from collector import main as collector_main
from fact_packages import FactPackageError, build_fact_packages
from runtime_paths import RuntimePathError, external_runtime_path
from script_report import ScriptReportError, write_script_report
from script_transcription import TranscriptionError, prepare_audio, import_transcript, mark_unavailable, finalize_transcripts
from analysis_delivery import read_page, timed_phase
from analysis_delivery_adapter import render_analysis_delivery, analysis_delivery_status, verified_context

# Record only actual local phase calls; waiting for an external Agent remains unmeasured.
prepare_audio = timed_phase("prepare_audio")(prepare_audio)
import_transcript = timed_phase("import_transcript")(import_transcript)
mark_unavailable = timed_phase("mark_unavailable")(mark_unavailable)
finalize_transcripts = timed_phase("finalize_transcripts")(finalize_transcripts)
build_fact_packages = timed_phase("build_facts")(build_fact_packages)
write_script_report = timed_phase("write_script_report")(write_script_report)


DEFAULT_OUTPUT_FOLDER = "云图素材脚本数据"


def _require_completed_report(args: list[str]) -> None:
    if "--run-dir" not in args or args.index("--run-dir") + 1 >= len(args):
        raise ValueError("analysis_requires_run_dir")
    root = external_runtime_path(args[args.index("--run-dir") + 1], label="run directory")
    receipt_path = root / "script-report" / "receipt.json"
    facts_path = root / "facts" / "manifest.json"
    transcript_path = root / "transcripts" / "manifest.json"
    if not all(path.is_file() for path in (receipt_path, facts_path, transcript_path)):
        raise ValueError("script_report_not_finalized")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict) or not isinstance(facts, dict) or not isinstance(facts.get("source_artifacts"), list):
        raise ValueError("script_report_receipt_invalid")
    source = next((item for item in facts["source_artifacts"]
                   if isinstance(item, dict) and item.get("path") == "transcripts/manifest.json"), None)
    if (receipt.get("factsManifestSha256") != hashlib.sha256(facts_path.read_bytes()).hexdigest()
            or not source or source.get("sha256") != hashlib.sha256(transcript_path.read_bytes()).hexdigest()):
        raise ValueError("script_report_stale")


def _acquire_run_dir(output_root: str | None, output_dir: str | None) -> Path:
    if output_dir:
        return external_runtime_path(output_dir, label="run directory")
    root = external_runtime_path(output_root or Path.cwd() / DEFAULT_OUTPUT_FOLDER, label="output root")
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    run_dir = root / run_id
    if run_dir.exists():
        raise RuntimePathError(f"run directory already exists: {run_dir}")
    return run_dir


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: run.py acquire|prepare-audio|import-transcript|mark-unavailable|finalize-scripts|render-script-report|<collector command> ...")
        print("collector commands: collect, status, validate, runtime-check, list-accounts, schema, build-facts, render-overview, workflow, analyze")
        print("analysis delivery: analysis-page, analysis-status, render-analysis-report --analysis-root PATH")
        return 0
    if args[0] in {"analysis-page", "analysis-status", "render-analysis-report"}:
        parser = argparse.ArgumentParser(prog=f"run.py {args[0]}")
        parser.add_argument("--analysis-root", required=True)
        if args[0] == "analysis-page":
            parser.add_argument("--page", type=int, required=True)
        parsed = parser.parse_args(args[1:])
        try:
            root = external_runtime_path(parsed.analysis_root, label="analysis directory")
            if args[0] == "analysis-page":
                verified_context(root)
                reply = read_page(root, parsed.page)
            else:
                reply = (analysis_delivery_status if args[0] == "analysis-status" else render_analysis_delivery)(root)
            print(json.dumps(reply, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, KeyError) as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
    if args[0] not in {"acquire", "prepare-audio", "import-transcript", "mark-unavailable", "finalize-scripts", "render-script-report"}:
        if args[0] in {"workflow", "analyze"}:
            try:
                _require_completed_report(args)
            except (RuntimePathError, OSError, ValueError, json.JSONDecodeError) as exc:
                print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
                return 2
        return collector_main(args)
    parser = argparse.ArgumentParser(prog=f"run.py {args[0]}")
    if args[0] == "acquire":
        parser.add_argument("--shop-id", required=True)
        parser.add_argument("--query-spec", required=True)
        destination = parser.add_mutually_exclusive_group()
        destination.add_argument("--output-root", help=f"output root; defaults to cwd/{DEFAULT_OUTPUT_FOLDER}")
        destination.add_argument("--output-dir", help="exact run directory for an existing workflow")
    else:
        parser.add_argument("--run-dir", required=True)
        if args[0] in {"import-transcript", "mark-unavailable"}:
            parser.add_argument("--video-sha", required=True, help="video SHA-256 from transcripts/manifest.json")
            if args[0] == "import-transcript":
                parser.add_argument("--text-file", required=True, help="UTF-8 file containing only the Agent transcript")
            parser.add_argument("--response-file", required=True, help="UTF-8 file with the unedited Agent tool response")
            if args[0] == "mark-unavailable":
                parser.add_argument("--review-note", required=True, help="why this reviewed recognition has no usable speech text")
    parsed = parser.parse_args(args[1:])
    run_dir = None
    stage = args[0]
    try:
        if args[0] == "acquire":
            run_dir = _acquire_run_dir(parsed.output_root, parsed.output_dir)
            print(json.dumps({"runDir": str(run_dir), "status": "starting"}, ensure_ascii=False))
            stage = "collect"
            collect_args = ["collect", "--shop-id", parsed.shop_id, "--query-spec", parsed.query_spec,
                            "--highlight-mode", "off", "--output-dir", str(run_dir)]
            status = collector_main(collect_args)
            if status != 0:
                print(json.dumps({"status": "failed", "stage": stage, "runDir": str(run_dir),
                                  "exitCode": status}, ensure_ascii=False), file=sys.stderr)
                return status
        else:
            run_dir = external_runtime_path(parsed.run_dir, label="run directory")
        if args[0] in {"acquire", "prepare-audio"}:
            stage = "prepare_audio"
            manifest = prepare_audio(run_dir)
            print(json.dumps({"runDir": str(run_dir),
                              "status": "awaiting_agent_transcripts" if manifest["counts"].get("pending_agent") else "ready_to_finalize",
                              "transcription": manifest["counts"]}, ensure_ascii=False))
            if args[0] == "prepare-audio" or manifest["counts"].get("pending_agent"):
                return 0
        if args[0] == "import-transcript":
            stage = "import_transcript"
            manifest = import_transcript(run_dir, video_sha=parsed.video_sha,
                                         text_file=Path(parsed.text_file), response_file=Path(parsed.response_file))
            print(json.dumps({"transcription": manifest["counts"]}, ensure_ascii=False))
            return 0
        if args[0] == "mark-unavailable":
            stage = "mark_unavailable"
            manifest = mark_unavailable(run_dir, video_sha=parsed.video_sha,
                                        response_file=Path(parsed.response_file), review_note=parsed.review_note)
            print(json.dumps({"transcription": manifest["counts"]}, ensure_ascii=False))
            return 0
        stage = "finalize_transcripts"
        manifest = finalize_transcripts(run_dir)
        stage = "build_facts"
        build_fact_packages(run_dir)
        stage = "write_report"
        receipt = write_script_report(run_dir)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 2 if any(manifest["counts"].get(state) for state in (
            "audio_decode_failed", "audio_timeout", "ffmpeg_unavailable", "audio_empty"
        )) else 0
    except (TranscriptionError, FactPackageError, ScriptReportError, RuntimePathError, OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "stage": stage, "runDir": str(run_dir) if run_dir else None,
                          "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None, *, emit_timing: bool = False) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    started, tick = datetime.now(timezone.utc).isoformat(), time.monotonic()
    code = 1
    try:
        code = _main(args)
        return code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        raise
    finally:
        event = {"event": "command_completed", "command": args[0] if args else "help", "exitCode": code,
                          "startedAt": started, "endedAt": datetime.now(timezone.utc).isoformat(),
                          "wallSeconds": round(time.monotonic()-tick, 6),
                          "scope": "local_command_not_background_job"}
        if emit_timing:
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(emit_timing=True))
