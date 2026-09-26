"""RPA gateway result IO helpers for Yuntu analysis workflows."""

import ast
import csv
import io
import json
import os
from pathlib import Path
import shutil
import tempfile

import requests


_EDGE_CONTROL_CHARACTERS = "".join(chr(value) for value in range(32))


class RpaIoError(RuntimeError):
    """Raised when RPA gateway output cannot be materialized safely."""


class GatewayResult:
    def __init__(self, kind, records=None, file_url=None):
        self.kind = kind
        self.records = records
        self.file_url = file_url


def _decode_json_cell(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    if text[0] not in "[{":
        return value
    for decoder, errors in (
        (json.loads, (json.JSONDecodeError,)),
        (ast.literal_eval, (ValueError, SyntaxError)),
    ):
        try:
            decoded = decoder(text)
        except errors:
            continue
        if isinstance(decoded, (dict, list)):
            return decoded
    return value


def decode_structured_cell(value):
    """Decode nested JSON/Python containers and retain parse-failure evidence."""
    if not isinstance(value, str):
        return value, False
    text = value.strip()
    if not text or text[0] not in "[{":
        return value, False
    for decoder, errors in (
        (json.loads, (json.JSONDecodeError,)),
        (ast.literal_eval, (ValueError, SyntaxError)),
    ):
        try:
            decoded = decoder(text)
        except errors:
            continue
        if isinstance(decoded, (dict, list)):
            return decoded, False
    return value, True


def _clean_csv_value(value):
    if isinstance(value, str):
        return value.strip(_EDGE_CONTROL_CHARACTERS)
    return value


def _is_csv_padding_row(row):
    values = [value for value in row.values() if value not in (None, "")]
    return bool(values) and all(
        isinstance(value, str) and not value.strip(" ,") for value in values
    )


def _records_from_payload(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RpaIoError("RPA payload string must contain valid JSON records") from exc
    if not isinstance(payload, list):
        raise RpaIoError("RPA gateway payload must be a record list")
    return payload


def _records_from_completed_task(payload):
    records = _records_from_payload(payload)
    if not records:
        raise RpaIoError("RPA async task contains no records")
    return records


def _file_url_from_envelope(result):
    if result.get("success") is not True or result.get("code") not in (200, "200"):
        raise RpaIoError("RPA gateway files response was not successful")
    data = result.get("data")
    content = data.get("result_content") if isinstance(data, dict) else None
    if not isinstance(content, dict):
        raise RpaIoError("RPA gateway files response is missing result_content")
    if content.get("status") != "COMPLETED" or content.get("failedInstances") != 0:
        raise RpaIoError("RPA gateway files task did not complete successfully")
    files = content.get("files")
    if not isinstance(files, list) or not files or not isinstance(files[0], dict):
        raise RpaIoError("RPA gateway files response has no fileUrl")
    file_url = files[0].get("fileUrl")
    if not isinstance(file_url, str) or not file_url.strip():
        raise RpaIoError("RPA gateway files response fileUrl is missing")
    return file_url.strip()


def _file_url_from_task(result):
    """Extract one file from a completed async task response."""
    status = result.get("status")
    if status not in {"completed", "partial_success", "COMPLETED", "PARTIAL_SUCCESS"}:
        raise RpaIoError("RPA async task did not complete successfully")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    files = result.get("files")
    if files is None:
        files = data.get("files")
    if files is None and isinstance(result.get("result_content"), dict):
        files = result["result_content"].get("files")
    if files is None and isinstance(data.get("result_content"), dict):
        files = data["result_content"].get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise RpaIoError("RPA async task must contain exactly one fileUrl")
    file_url = files[0].get("fileUrl") or files[0].get("file_url")
    if not isinstance(file_url, str) or not file_url.strip():
        raise RpaIoError("RPA async task fileUrl is missing")
    return file_url.strip()


def unpack_gateway_result(result):
    if isinstance(result, list):
        return GatewayResult(kind="records", records=result)
    if not isinstance(result, dict):
        raise RpaIoError("RPA gateway result must be a list or object")

    if "status" in result:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        has_result_files = (
            isinstance(result.get("result_content"), dict) and "files" in result["result_content"]
        ) or (isinstance(data.get("result_content"), dict) and "files" in data["result_content"])
        if (
            "files" in result
            or "files" in data
            or has_result_files
            or "payload" in result
            or "payload" in data
            or "records" in data
        ):
            if "files" in result or "files" in data or has_result_files:
                return GatewayResult(kind="files", file_url=_file_url_from_task(result))
            if result.get("status") not in {"completed", "partial_success", "COMPLETED", "PARTIAL_SUCCESS"}:
                raise RpaIoError("RPA async task did not complete successfully")
            payload = result.get("payload", data.get("payload", data.get("records")))
            if isinstance(payload, dict):
                payload = [payload]
            return GatewayResult(kind="records", records=_records_from_completed_task(payload))

    data = result.get("data")
    if isinstance(data, dict) and "files" in data:
        return GatewayResult(kind="files", file_url=_file_url_from_task(result))
    if isinstance(data, dict) and "records" in data:
        return GatewayResult(kind="records", records=_records_from_payload(data.get("records")))
    if isinstance(data, dict) and data.get("result_type") == "files":
        return GatewayResult(kind="files", file_url=_file_url_from_envelope(result))
    if isinstance(data, dict) and data.get("result_type") == "payload":
        return GatewayResult(
            kind="records", records=_records_from_payload(data.get("payload"))
        )
    if isinstance(data, list):
        return GatewayResult(kind="records", records=data)
    if "payload" in result:
        return GatewayResult(kind="records", records=_records_from_payload(result.get("payload")))
    if result.get("success") is True:
        return GatewayResult(kind="records", records=[result])
    raise RpaIoError("RPA gateway result did not contain records or files")


def download_file(file_url, target_path, *, session=None, timeout=300):
    session = requests.Session() if session is None else session
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as staging_file:
        staging_path = Path(staging_file.name)
        try:
            response = session.get(file_url, stream=True, timeout=timeout)
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    staging_file.write(chunk)
        except Exception as exc:
            try:
                staging_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RpaIoError(f"unable to download RPA file: {exc}") from exc
    try:
        if staging_path.stat().st_size == 0:
            raise RpaIoError("downloaded RPA file is empty")
        os.replace(staging_path, target)
    except Exception:
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def materialize_local_artifact(input_dir, target_path, *, suffix):
    """Atomically copy the only nonempty matching runtime artifact into a run."""
    source_dir = Path(input_dir)
    if not source_dir.is_dir():
        raise RpaIoError("local RPA artifact directory is missing")
    candidates = sorted(
        path
        for path in source_dir.iterdir()
        if path.is_file() and path.suffix.lower() == suffix.lower() and path.stat().st_size
    )
    if not candidates:
        raise RpaIoError("local RPA artifact is missing")
    if len(candidates) != 1:
        raise RpaIoError("local RPA artifact is ambiguous")

    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".tmp", dir=target.parent, delete=False
    ) as staging_file:
        staging_path = Path(staging_file.name)
        try:
            with candidates[0].open("rb") as source_file:
                shutil.copyfileobj(source_file, staging_file)
        except Exception as exc:
            staging_path.unlink(missing_ok=True)
            raise RpaIoError(f"unable to materialize local RPA artifact: {exc}") from exc
    try:
        if staging_path.stat().st_size == 0:
            raise RpaIoError("materialized local RPA artifact is empty")
        os.replace(staging_path, target)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return target


def _detect_delimiter(header_line):
    for delimiter in ("\x01", ",", "\t"):
        if delimiter in header_line:
            return delimiter
    raise RpaIoError("RPA CSV header must use a supported delimiter")


def parse_csv_records(path):
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise RpaIoError(f"unable to read RPA CSV {source}: {exc}") from exc
    first_line = text.partition("\n")[0]
    delimiter = _detect_delimiter(first_line)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = reader.fieldnames
    if not headers or any(not header for header in headers):
        raise RpaIoError(f"RPA CSV {source} has an invalid header")
    if len(headers) != len(set(headers)):
        raise RpaIoError(f"RPA CSV {source} has duplicate header names")

    records = []
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise RpaIoError(f"RPA CSV {source} row {row_number} has too many fields")
        cleaned_row = {key: _clean_csv_value(value) for key, value in row.items()}
        if not any(value not in (None, "") for value in cleaned_row.values()):
            continue
        if _is_csv_padding_row(cleaned_row):
            continue
        record = {}
        parse_failed_fields = []
        for key, value in cleaned_row.items():
            decoded, parse_failed = decode_structured_cell(value)
            record[key] = decoded
            if parse_failed:
                parse_failed_fields.append(key)
        if parse_failed_fields:
            record["__parse_failed_fields__"] = parse_failed_fields
        records.append(record)
    if not records:
        raise RpaIoError(f"RPA CSV {source} has no records")
    return records


def write_json_atomically(result, output_path):
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as staging_file:
        staging_path = Path(staging_file.name)
        try:
            json.dump(result, staging_file, ensure_ascii=False, indent=2)
            staging_file.write("\n")
        except Exception:
            try:
                staging_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    try:
        os.replace(staging_path, target)
    except Exception:
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target
