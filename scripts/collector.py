#!/usr/bin/env python3
"""Collect configured Yuntu rankings, details, and highlight frames."""

from __future__ import annotations

import argparse
import getpass
from datetime import datetime, timedelta
import json
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import rpa_dynamic_client as list_dynamic_rpa
import rpa_dynamic_detail_client as detail_dynamic_rpa
from runtime_paths import external_runtime_path

try:
    from yuntu_batch import (
        ConnectorInvoker,
        collection_status,
        fetch_batch,
        retry_highlights_batch,
    )
except ImportError:
    ConnectorInvoker = None
    collection_status = None
    fetch_batch = None
    retry_highlights_batch = None

try:
    from config_specs import (
        ConfigSpecError, resolve_highlight_request, resolve_highlight_spec, resolve_query_spec,
        query_to_rpa_params, detail_to_rpa_params, spec_fingerprint,
        write_resolved_specs,
    )
except ImportError:
    from .config_specs import (
        ConfigSpecError, resolve_highlight_request, resolve_highlight_spec, resolve_query_spec,
        query_to_rpa_params, detail_to_rpa_params, spec_fingerprint,
        write_resolved_specs,
    )

try:
    from fact_packages import FactPackageError, build_fact_packages
except ImportError:
    from .fact_packages import FactPackageError, build_fact_packages

try:
    from rpa_io import write_json_atomically
except ImportError:
    from .rpa_io import write_json_atomically

try:
    from overview_report import OverviewReportError, write_overview
except ImportError:
    from .overview_report import OverviewReportError, write_overview

try:
    from workflow_state import (
        analysis_context_dir,
        begin_model_analysis,
        build_ready_receipt,
        complete_model_analysis,
        fail_model_analysis,
        get_analysis_status,
        get_workflow_status,
        set_stage,
        WorkflowStateError,
    )
except ImportError:
    from .workflow_state import (
        analysis_context_dir,
        begin_model_analysis,
        build_ready_receipt,
        complete_model_analysis,
        fail_model_analysis,
        get_analysis_status,
        get_workflow_status,
        set_stage,
        WorkflowStateError,
    )

try:
    from analysis_facts import build_fact_index, load_selected_facts
    from fact_catalog import build_fact_catalog
    from model_analysis_request import (
        HANDOFF_PROTOCOL as MODEL_ANALYSIS_HANDOFF_PROTOCOL,
        REQUEST_VERSION as MODEL_ANALYSIS_REQUEST_VERSION,
        build_model_analysis_request,
        validate_model_analysis_request,
    )
    from analysis_prompt import build_prompt_packet
    from analysis_report import render_analysis_report
    from analysis_result import (
        AnalysisResultValidationError,
        normalize_analysis_result,
        validate_analysis_result,
    )
    from analysis_spec import AnalysisInputRequired, parse_analysis_spec
except ImportError:
    from .analysis_facts import build_fact_index, load_selected_facts
    from .fact_catalog import build_fact_catalog
    from .model_analysis_request import (
        HANDOFF_PROTOCOL as MODEL_ANALYSIS_HANDOFF_PROTOCOL,
        REQUEST_VERSION as MODEL_ANALYSIS_REQUEST_VERSION,
        build_model_analysis_request,
        validate_model_analysis_request,
    )
    from .analysis_prompt import build_prompt_packet
    from .analysis_report import render_analysis_report
    from .analysis_result import (
        AnalysisResultValidationError,
        normalize_analysis_result,
        validate_analysis_result,
    )
    from .analysis_spec import AnalysisInputRequired, parse_analysis_spec

try:
    from analysis_input import parse_analysis_input, validate_analysis_input
except ImportError:
    from .analysis_input import parse_analysis_input, validate_analysis_input

try:
    from analysis_workflow import (
        apply_fact_selection_result,
        prepare_fact_selection_handoff,
        prepare_model_analysis_handoff,
    )
except ImportError:
    from .analysis_workflow import (
        apply_fact_selection_result,
        prepare_fact_selection_handoff,
        prepare_model_analysis_handoff,
    )


HEADERS = {"Content-Type": "application/json"}
HTTP_REQUEST_TIMEOUT_SECONDS = 30
RPA_REQUEST_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 3600
RUNNING_TASK_STATUSES = {"pending", "running"}
SUCCESS_TASK_STATUSES = {"completed", "partial_success"}
REQUIRED_ENVIRONMENT = (
    "ENV_BACKEND_HOST",
    "YUCE_AUTHORIZATION",
)
DATE_RANGE_TYPES = {"LAST_7_DAYS", "LAST_30_DAYS", "CUSTOM"}
AGE_VALUES = {
    "AGE_18_19",
    "AGE_20_23",
    "AGE_24_30",
    "AGE_31_35",
    "AGE_36_40",
    "AGE_41_45",
    "AGE_46_50",
    "AGE_51_55",
    "AGE_56_59",
    "AGE_60_PLUS",
}
GENDER_VALUES = {"MALE", "FEMALE"}
CROWD_GROUP_VALUES = {
    "TOWN_YOUTH",
    "GENZ",
    "SENIOR_MIDDLE",
    "REFINED_MOM",
    "NEW_WHITE_COLLAR",
    "URBAN_SILVER",
    "TOWN_MIDDLE_ELDER",
    "URBAN_BLUE_COLLAR",
}
RANKING_LIMIT_VALUES = {
    "EXPOSURE_TOP1000",
    "CTR_TOP1000",
    "INTERACTION_RATE_TOP1000",
    "COMPLETION_RATE_TOP1000",
}
BRAND_SCOPE_VALUES = {"ALL_INDUSTRY", "SPECIFIED_BRANDS"}
LIST_FIELDS = {
    "industry",
    "date_type",
    "custom_start_date",
    "custom_end_date",
    "ages",
    "genders",
    "crowd_groups",
    "brand_scope_type",
    "brands",
    "ranking_limit_type",
}
DETAIL_FIELDS = {
    "material_id",
    "date_type",
    "custom_start_date",
    "custom_end_date",
}
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")

class RpaConnectorError(RuntimeError):
    """Raised when the connector cannot safely complete an operation."""


def shanghai_today(now=None):
    if now is None:
        return datetime.now(SHANGHAI_TIMEZONE).date()
    return now.astimezone(SHANGHAI_TIMEZONE).date()


def runtime_config(environ=None):
    environment = os.environ if environ is None else environ
    missing = [
        name
        for name in REQUIRED_ENVIRONMENT[:2]
        if not str(environment.get(name) or "").strip()
    ]
    if not str(
        environment.get("YUCE_SESSION_ID")
        or environment.get("YCSESSIONID")
        or ""
    ).strip():
        missing.append("YUCE_SESSION_ID or YCSESSIONID")
    if missing:
        raise RpaConnectorError(
            "missing RPA runtime environment variables: " + ", ".join(missing)
        )

    api_base = environment["ENV_BACKEND_HOST"].strip().rstrip("/")
    parsed = urlsplit(api_base)
    try:
        valid_port = parsed.port is None or parsed.port > 0
    except ValueError:
        valid_port = False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or "@" in parsed.netloc
        or parsed.query
        or parsed.fragment
        or not valid_port
    ):
        raise RpaConnectorError(
            "ENV_BACKEND_HOST must be an HTTP(S) gateway URL without credentials, query, or fragment"
        )

    authorization = environment["YUCE_AUTHORIZATION"].strip()
    session_id = str(
        environment.get("YUCE_SESSION_ID")
        or environment.get("YCSESSIONID")
    ).strip()
    # The gateway's existing runtime contract also expects the authorization
    # context in YCSESSIONID when a browser cookie is not separately injected.
    cookie_session = str(environment.get("YCSESSIONID") or authorization).strip()
    return api_base, authorization, session_id, {"YCSESSIONID": cookie_session}


def _cli_runtime(args):
    """Resolve one complete configuration without mutating the parent environment."""
    keys = ("ENV_BACKEND_HOST", "YUCE_AUTHORIZATION", "YUCE_SESSION_ID", "YCSESSIONID")
    if args.runtime_config_stdin:
        # One JSON line permits both a pipe and a persistent terminal without EOF.
        raw = getpass.getpass("Runtime configuration JSON (hidden): ") if sys.stdin.isatty() else sys.stdin.readline()
        try:
            environment = json.loads(raw)
        except (ValueError, TypeError):
            raise RpaConnectorError("runtime stdin must contain one JSON object on one line") from None
        if not isinstance(environment, dict) or set(environment) - set(keys):
            raise RpaConnectorError("runtime stdin accepts only the four documented runtime variables")
        if any(not isinstance(value, str) for value in environment.values()):
            raise RpaConnectorError("runtime variable values must be strings")
        source = "stdin"
    else:
        environment = {key: os.environ[key] for key in keys if key in os.environ}
        source = "environment"
    gateway, _, _, _ = runtime_config(environment)
    if args.expected_gateway and gateway != args.expected_gateway.strip().rstrip("/"):
        raise RpaConnectorError(
            f"gateway mismatch: actual={gateway}; apply the explicitly requested runtime configuration"
        )
    receipt = {
        "gateway": gateway,
        "source": source,
        "variables_present": {key: bool(environment.get(key, "").strip()) for key in keys},
        "authentication_verified": False,
    }
    return environment, receipt


def _parse_date(value, field_name):
    if not isinstance(value, str):
        raise RpaConnectorError(
            f"{field_name} must use YYYYMMDD or YYYY-MM-DD format"
        )
    for date_format in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            pass
    raise RpaConnectorError(f"{field_name} must use YYYYMMDD or YYYY-MM-DD format")


def _normalize_multivalue(value, field_name, allowed_values=None):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                values = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise RpaConnectorError(
                    f"{field_name} must be a comma-separated string or JSON array"
                ) from exc
        else:
            values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        raise RpaConnectorError(
            f"{field_name} must be a comma-separated string or JSON array"
        )

    if not isinstance(values, list) or not values:
        raise RpaConnectorError(f"{field_name} must contain one or more strings")
    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise RpaConnectorError(f"{field_name} values must be strings")
        item = item.strip()
        if not item:
            raise RpaConnectorError(f"{field_name} values cannot be blank")
        if item in normalized:
            raise RpaConnectorError(f"{field_name} cannot contain duplicates")
        if allowed_values is not None and item not in allowed_values:
            raise RpaConnectorError(
                f"{field_name} contains an unsupported value: {item}"
            )
        normalized.append(item)
    return normalized


def validate_list_business_params(raw_params):
    if not isinstance(raw_params, dict):
        raise RpaConnectorError("list business_params must be an object")
    if "time_range_type" in raw_params:
        raise RpaConnectorError("time_range_type is obsolete for new requests; use date_type")
    unexpected = set(raw_params) - LIST_FIELDS
    if unexpected:
        raise RpaConnectorError(
            "unexpected list business parameters: " + ", ".join(sorted(unexpected))
        )

    params = {}
    if "industry" in raw_params:
        industry = raw_params["industry"]
        if not isinstance(industry, str) or not industry.strip():
            raise RpaConnectorError("industry must be a nonblank string")
        params["industry"] = industry.strip()

    date_type = raw_params.get("date_type")
    if date_type is None:
        raise RpaConnectorError("date_type is required for list requests")
    if not isinstance(date_type, str) or date_type not in DATE_RANGE_TYPES:
        raise RpaConnectorError(
            "date_type must be one of: " + ", ".join(sorted(DATE_RANGE_TYPES))
        )
    params["date_type"] = date_type

    custom_start = raw_params.get("custom_start_date")
    custom_end = raw_params.get("custom_end_date")
    if date_type == "CUSTOM":
        if not custom_start or not custom_end:
            raise RpaConnectorError("CUSTOM requires custom_start_date and custom_end_date")
        start = _parse_date(custom_start, "custom_start_date")
        end = _parse_date(custom_end, "custom_end_date")
        today = shanghai_today()
        if start < today - timedelta(days=369):
            raise RpaConnectorError("custom_start_date cannot be more than 369 days before today")
        if end > today - timedelta(days=4):
            raise RpaConnectorError("custom_end_date cannot be later than today-4")
        if end < start:
            raise RpaConnectorError("custom_end_date cannot be earlier than custom_start_date")
        if (end - start).days > 44:
            raise RpaConnectorError("custom date span cannot exceed 44 days")
        params["custom_start_date"] = custom_start
        params["custom_end_date"] = custom_end
    elif custom_start is not None or custom_end is not None:
        raise RpaConnectorError(
            "custom_start_date and custom_end_date are only valid for CUSTOM"
        )

    for field_name in ("ages", "genders", "crowd_groups"):
        if field_name in raw_params:
            params[field_name] = ",".join(
                _normalize_multivalue(raw_params[field_name], field_name)
            )

    scope = raw_params.get("brand_scope_type")
    if scope is not None:
        if not isinstance(scope, str) or scope not in BRAND_SCOPE_VALUES:
            raise RpaConnectorError(
                "brand_scope_type must be one of: "
                + ", ".join(sorted(BRAND_SCOPE_VALUES))
            )
        params["brand_scope_type"] = scope
    if "brands" in raw_params:
        brands = _normalize_multivalue(raw_params["brands"], "brands")
        if scope != "SPECIFIED_BRANDS":
            raise RpaConnectorError("brands requires brand_scope_type SPECIFIED_BRANDS")
        if not 3 <= len(brands) <= 10:
            raise RpaConnectorError("brands must contain 3 to 10 values")
        params["brands"] = ",".join(brands)
    elif scope == "SPECIFIED_BRANDS":
        raise RpaConnectorError("SPECIFIED_BRANDS requires brands")

    if "ranking_limit_type" in raw_params:
        ranking_limit_type = raw_params["ranking_limit_type"]
        if (
            not isinstance(ranking_limit_type, str)
            or ranking_limit_type not in RANKING_LIMIT_VALUES
        ):
            raise RpaConnectorError(
                "ranking_limit_type must be one of: "
                + ", ".join(sorted(RANKING_LIMIT_VALUES))
            )
        params["ranking_limit_type"] = ranking_limit_type
    return params


def validate_detail_business_params(raw_params):
    if not isinstance(raw_params, dict):
        raise RpaConnectorError("detail business_params must be an object")
    unexpected = set(raw_params) - DETAIL_FIELDS
    if unexpected:
        raise RpaConnectorError(
            "unexpected detail business parameters: " + ", ".join(sorted(unexpected))
        )
    material_id = raw_params.get("material_id")
    if not isinstance(material_id, str) or not material_id.isdigit():
        raise RpaConnectorError("material_id must contain digits only")
    date_type = raw_params.get("date_type")
    if date_type not in DATE_RANGE_TYPES:
        raise RpaConnectorError(
            "date_type must be one of: " + ", ".join(sorted(DATE_RANGE_TYPES))
        )

    params = {"material_id": material_id, "date_type": date_type}
    start_date = raw_params.get("custom_start_date")
    end_date = raw_params.get("custom_end_date")
    if date_type == "CUSTOM":
        if not start_date or not end_date:
            raise RpaConnectorError("CUSTOM requires custom_start_date and custom_end_date")
        start = _parse_date(start_date, "custom_start_date")
        end = _parse_date(end_date, "custom_end_date")
        if end < start:
            raise RpaConnectorError(
                "custom_end_date cannot be earlier than custom_start_date"
            )
        params["custom_start_date"] = start_date
        params["custom_end_date"] = end_date
    elif start_date is not None or end_date is not None:
        raise RpaConnectorError(
            "custom_start_date and custom_end_date are only valid for CUSTOM"
        )
    return params


def _default_session():
    return requests.Session()


def _raise_for_status(response, action):
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RpaConnectorError(f"unable to {action}: {exc}") from exc


def _response_json(response, action):
    try:
        return response.json()
    except Exception as exc:
        raise RpaConnectorError(
            f"unable to decode {action} response as JSON: {exc}"
        ) from exc


def _require_successful_result(result, *, gateway=None, http_status=None):
    if not isinstance(result, dict):
        raise RpaConnectorError("gateway returned a malformed result")
    if result.get("success") is not True:
        message = str(result.get("msg") or result.get("message") or "")
        category = "unsuccessful_result"
        if "过期" in message and ("token" in message.lower() or "令牌" in message):
            category = "credential_expired"
        elif "未登录" in message or "鉴权失败" in message or http_status in (401, 403):
            category = "authentication_failed"
        # Do not echo arbitrary server messages (they can contain credentials).
        code = str(result.get("code", "unknown"))
        code = code if re.fullmatch(r"[0-9]{1,8}", code) else "unknown"
        trace = str(result.get("traceId", "unavailable"))
        trace = trace if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", trace) else "unavailable"
        guidance = (
            " Check the target gateway and credential configuration source; "
            "this does not establish the Yuntu website login state."
            if category in {"authentication_failed", "credential_expired"} else ""
        )
        raise RpaConnectorError(
            f"RPA gateway {category} (gateway={gateway}, HTTP={http_status}, code={code}, traceId={trace})."
            + guidance
        )
    return result


def _gateway_result(response, action, gateway):
    status = getattr(response, "status_code", None)
    status = status if isinstance(status, int) else None
    if status in (401, 403):
        try:
            body = response.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        return _require_successful_result(
            {**body, "success": False}, gateway=gateway, http_status=status
        )
    _raise_for_status(response, action)
    return _require_successful_result(
        _response_json(response, action), gateway=gateway, http_status=status
    )


def _operation_client(operation):
    if operation == "list":
        return list_dynamic_rpa
    if operation == "detail":
        return detail_dynamic_rpa
    raise RpaConnectorError("operation must be list or detail")


def fetch_connector_detail(operation, *, environ=None, session=None):
    client = _operation_client(operation)
    session = _default_session() if session is None else session
    api_base, _authorization, _session_id, cookies = runtime_config(environ)
    try:
        response = session.post(
            f"{api_base}{client.DETAIL_PATH}",
            json={"connectorCode": client.CONNECTOR_CODE},
            headers={"Content-Type": "application/json;charset=UTF-8"},
            cookies=cookies,
            timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise RpaConnectorError(
            f"unable to fetch RPA {operation} connector detail: transport request failed"
        ) from exc
    body = _gateway_result(response, f"fetch RPA {operation} connector detail", api_base)
    data = body.get("data")
    if not isinstance(data, list):
        raise RpaConnectorError(f"RPA {operation} connector detail data must be a list")
    matches = [
        item for item in data
        if isinstance(item, dict) and item.get("connectorCode") == client.CONNECTOR_CODE
    ]
    if not matches:
        raise RpaConnectorError(
            f"RPA {operation} connector is unavailable, offline, or unsupported for BIZ_EXPERT"
        )
    if len(matches) != 1:
        raise RpaConnectorError(f"RPA {operation} connector detail is ambiguous")
    try:
        client.normalized_detail(matches[0])
    except client.ContractError as exc:
        raise RpaConnectorError(str(exc)) from exc
    return matches[0]


def list_accounts(environ=None, session=None):
    session = _default_session() if session is None else session
    api_base, authorization, _session_id, cookies = runtime_config(environ)
    detail = fetch_connector_detail("list", environ=environ, session=session)
    try:
        platform = list_dynamic_rpa.resolve_platform(detail)
    except list_dynamic_rpa.ContractError as exc:
        raise RpaConnectorError(str(exc)) from exc
    try:
        response = session.post(
            f"{api_base}/adg/v1/agent/rpa/authorizations/query",
            json={
                "authorization": authorization,
                "platform": platform,
            },
            headers=HEADERS,
            cookies=cookies,
            timeout=30,
        )
    except Exception as exc:
        raise RpaConnectorError(
            "unable to list RPA accounts: transport request failed"
        ) from exc
    return _gateway_result(response, "list RPA accounts", api_base)


def _authorized_rpa_shop_ids(payload, platform):
    """Return normalized IDs from the gateway authorization response."""
    accounts = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(accounts, dict):
        accounts = accounts.get("accounts") or accounts.get("list") or accounts.get("data")
    if not isinstance(accounts, list):
        return set()
    return {
        str(item.get("accountId") or item.get("account_id") or item.get("id") or
            item.get("shop_id") or item.get("shopId") or "").strip()
        for item in accounts
        if isinstance(item, dict)
        and item.get("platform") in (None, "", platform)
        and str(item.get("accountId") or item.get("account_id") or item.get("id") or
                item.get("shop_id") or item.get("shopId") or "").strip()
    }


def _require_authorized_rpa_shop(shop_id, platform, *, api_base, authorization, cookies, session):
    """Stop before a task submission when the selected form account expired."""
    try:
        response = session.post(
            f"{api_base}/adg/v1/agent/rpa/authorizations/query",
            json={"authorization": authorization, "platform": platform},
            headers=HEADERS,
            cookies=cookies,
            timeout=30,
        )
    except Exception as exc:
        raise RpaConnectorError("unable to verify selected RPA account") from exc
    authorized = _authorized_rpa_shop_ids(
        _gateway_result(response, "verify selected RPA account", api_base), platform
    )
    if shop_id.strip() not in authorized:
        raise RpaConnectorError(
            "rpa_shop_not_authorized: selected RPA shop ID is not authorized for the current credentials"
        )


def _operation_function_code(operation):
    return _operation_client(operation).CONNECTOR_CODE


def _response_data(response, action, gateway):
    result = _gateway_result(response, action, gateway)
    data = result.get("data")
    if not isinstance(data, dict):
        raise RpaConnectorError(f"gateway {action} response is missing an object data field")
    return data


def submit_fetch_task(operation, shop_id, business_params, *, environ=None, session=None):
    client = _operation_client(operation)
    function_code = client.CONNECTOR_CODE
    if not isinstance(shop_id, str) or not shop_id.strip():
        raise RpaConnectorError("shop_id is required")
    session = _default_session() if session is None else session
    api_base, authorization, session_id, cookies = runtime_config(environ)
    detail = fetch_connector_detail(operation, environ=environ, session=session)
    try:
        client.validate_business_params(detail, business_params)
        platform = client.resolve_platform(detail)
    except client.ContractError as exc:
        raise RpaConnectorError(str(exc)) from exc
    _require_authorized_rpa_shop(
        shop_id, platform, api_base=api_base, authorization=authorization,
        cookies=cookies, session=session,
    )
    try:
        response = session.post(
            f"{api_base}/adg/v1/agent/fetch/tasks",
            json={
                "authorization": authorization,
                "agent_session_id": session_id,
                "data_source_type": client.DATA_SOURCE_TYPE,
                "platform": platform,
                "function_code": function_code,
                "business_params": business_params,
                "shop_id": shop_id.strip(),
            },
            headers=HEADERS,
            cookies=cookies,
            timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise RpaConnectorError(
            f"unable to submit RPA {operation}: transport request failed"
        ) from exc
    data = _response_data(response, f"submit RPA {operation}", api_base)
    task_group_id = data.get("task_group_id")
    if not isinstance(task_group_id, str) or not task_group_id.strip():
        raise RpaConnectorError(f"submit RPA {operation} response is missing task_group_id")
    return task_group_id.strip()


def query_fetch_task(
    task_group_id, *, environ=None, session=None,
    request_timeout_seconds=HTTP_REQUEST_TIMEOUT_SECONDS,
):
    if not isinstance(task_group_id, str) or not task_group_id.strip():
        raise RpaConnectorError("task_group_id is required")
    if request_timeout_seconds <= 0:
        raise RpaConnectorError("request_timeout_seconds must be positive")
    session = _default_session() if session is None else session
    api_base, authorization, _session_id, cookies = runtime_config(environ)
    try:
        response = session.post(
            f"{api_base}/adg/v1/agent/fetch/tasks/status",
            json={"authorization": authorization, "task_group_id": task_group_id.strip()},
            headers=HEADERS,
            cookies=cookies,
            timeout=request_timeout_seconds,
        )
    except Exception as exc:
        raise RpaConnectorError("unable to query RPA task status: transport request failed") from exc
    return _response_data(response, "query RPA task status", api_base)


def wait_fetch_task(
    task_group_id, *, environ=None, session=None,
    timeout_seconds=POLL_TIMEOUT_SECONDS, poll_interval_seconds=POLL_INTERVAL_SECONDS,
):
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise RpaConnectorError("task polling timeout and interval must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RpaConnectorError(f"poll timeout: {task_group_id}")
        time.sleep(min(poll_interval_seconds, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RpaConnectorError(f"poll timeout: {task_group_id}")
        result = query_fetch_task(
            task_group_id, environ=environ, session=session,
            request_timeout_seconds=min(HTTP_REQUEST_TIMEOUT_SECONDS, remaining),
        )
        status = str(result.get("status") or "").lower()
        if status in SUCCESS_TASK_STATUSES:
            return {**result, "task_group_id": task_group_id}
        if status == "failed":
            raise RpaConnectorError(f"RPA task failed: {task_group_id}")
        if status not in RUNNING_TASK_STATUSES:
            raise RpaConnectorError(f"RPA task returned unknown status {status!r}: {task_group_id}")


def _invoke_list_async(shop_id, business_params, environ=None, session=None):
    task_group_id = submit_fetch_task(
        "list", shop_id, validate_list_business_params(business_params),
        environ=environ, session=session,
    )
    return wait_fetch_task(
        task_group_id, environ=environ, session=session
    )


def _invoke_detail_async(shop_id, business_params, environ=None, session=None):
    task_group_id = submit_fetch_task(
        "detail", shop_id, validate_detail_business_params(business_params),
        environ=environ, session=session,
    )
    return wait_fetch_task(task_group_id, environ=environ, session=session)


def invoke_list(shop_id, business_params, environ=None, session=None):
    return _invoke_list_async(
        shop_id, business_params, environ=environ, session=session
    )


def invoke_detail(shop_id, business_params, environ=None, session=None):
    return _invoke_detail_async(
        shop_id, business_params, environ=environ, session=session
    )


def _batch_invoker(environ=None):
    def detail_params(params):
        return {name: params[name] for name in DETAIL_FIELDS if name in params}

    return ConnectorInvoker(
        lambda sid, params: invoke_list(sid, params, environ=environ),
        lambda sid, params: invoke_detail(sid, detail_params(params), environ=environ),
        submit_detail=lambda sid, params: submit_fetch_task(
            "detail", sid, validate_detail_business_params(detail_params(params)), environ=environ
        ),
        wait_detail=lambda task_group_id: wait_fetch_task(task_group_id, environ=environ),
    )


def _positive_int_argument(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config-stdin", action="store_true", help="Read a complete task runtime configuration as one JSON line from stdin")
    parser.add_argument("--expected-gateway", help="Reject a runtime gateway different from this URL before any request")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("runtime-check", help="Show redacted runtime configuration without making network requests")
    commands.add_parser("list-accounts", help="List available RPA accounts")
    commands.add_parser("schema", help="Print collection parameter metadata")
    commands.add_parser("self-test", help="Run offline connector checks")
    status_parser = commands.add_parser("status", help="Read collection lifecycle state")
    status_parser.add_argument("--output-dir", required=True)
    validate_parser = commands.add_parser("validate", help="Validate and print resolved configuration")
    validate_parser.add_argument("--query-spec", required=True)
    validate_parser.add_argument("--highlight-spec")
    validate_parser.add_argument("--highlight-mode", choices=("on", "off"))
    retry_highlights_parser = commands.add_parser(
        "retry-highlights",
        help="Retry missing or unavailable highlighter assets from persisted details",
    )
    retry_highlights_parser.add_argument("--output-dir", required=True)
    facts_parser = commands.add_parser(
        "build-facts", help="Build source-only fact packages from an existing run"
    )
    facts_parser.add_argument("--run-dir", required=True)
    overview_parser = commands.add_parser(
        "render-overview", help="Render a verified offline material leaderboard overview"
    )
    overview_parser.add_argument("--run-dir", required=True)
    overview_parser.add_argument("--output-dir")
    analyze_parser = commands.add_parser(
        "analyze", help="Analyze selected fact packages and render an auditable report"
    )
    analyze_parser.add_argument("--run-dir", required=True)
    analyze_parser.add_argument("--input", help="AnalysisInput JSON path, JSON object, or natural language")
    analyze_parser.add_argument("--spec", help="Legacy AnalysisSpec JSON path")
    analyze_parser.add_argument("--output", required=True, help="Markdown report output path")
    analyze_parser.add_argument(
        "--analysis-result", help="Optional model-produced AnalysisResult JSON path"
    )
    workflow_parser = commands.add_parser(
        "workflow", help="Run phase-two fact selection and current-model analysis handoffs"
    )
    workflow_parser.add_argument("--run-dir", required=True)
    workflow_parser.add_argument("--business-question", required=True)
    workflow_parser.add_argument("--analysis-id", help="Resume one isolated phase-two analysis context")
    workflow_parser.add_argument("--fact-selection-result")
    workflow_parser.add_argument("--analysis-result", help="Optional current-model AnalysisResult JSON path")
    workflow_parser.add_argument("--output", help="Report path inside analysis/<analysis_id>; defaults to workflow.md in that context")
    fetch_parser = commands.add_parser("collect", help="Collect configured rankings, details, and frames")
    fetch_parser.add_argument("--shop-id", required=True)
    fetch_parser.add_argument("--query-spec", required=True)
    fetch_parser.add_argument("--highlight-spec")
    fetch_parser.add_argument("--highlight-mode", choices=("on", "off"))
    fetch_parser.add_argument("--output-dir", required=True)
    return parser


def _enforce_external_runtime_paths(args):
    if args.command in {"collect", "retry-highlights"}:
        args.output_dir = str(external_runtime_path(args.output_dir, label="run directory"))
    if args.command in {"build-facts", "render-overview", "analyze", "workflow"}:
        args.run_dir = str(external_runtime_path(args.run_dir, label="run directory"))
    if args.command == "analyze" or (args.command == "workflow" and args.output):
        args.output = str(external_runtime_path(args.output, label="analysis output"))
    if args.command == "render-overview" and args.output_dir:
        args.output_dir = str(external_runtime_path(args.output_dir, label="overview output directory"))


def _cmd_self_test():
    validate_list_business_params({"date_type": "LAST_7_DAYS", "industry": "食品饮料"})
    validate_detail_business_params(
        {"material_id": "1", "date_type": "LAST_7_DAYS"}
    )
    print("self-test passed")


def _load_saved_specs(output_dir):
    root = Path(output_dir)
    query = resolve_query_spec(json.loads((root / "resolved-query-spec.json").read_text(encoding="utf-8")))
    highlight_path = root / "resolved-highlight-spec.json"
    highlights = resolve_highlight_spec(json.loads(highlight_path.read_text(encoding="utf-8"))) if highlight_path.is_file() else None
    summary = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))
    contract = summary.get("collectionContract") if isinstance(summary, dict) else None
    expected_fingerprint = (
        contract.get("configurationFingerprint") if isinstance(contract, dict) else None
    )
    if expected_fingerprint != spec_fingerprint(query, highlights):
        raise ConfigSpecError(
            "saved configuration fingerprint does not match the collection contract; choose a new output directory"
        )
    return query, highlights


def _build_facts_after_run(output_dir: str | Path) -> dict:
    """Build facts for a changed run and return its manifest."""
    return build_fact_packages(output_dir)


def _print_batch_result(output_dir, facts_manifest):
    receipt = build_ready_receipt(output_dir)
    status = collection_status(output_dir=output_dir)
    print(json.dumps({
        "firstBatchCsvGate": status.get("firstBatchCsvGate"),
        "rpa_paused": status.get("rpa_paused", False),
        "batch_manifest_path": str(Path(output_dir) / "batch-manifest.json"),
        "facts_manifest_path": str(Path(output_dir) / "facts/manifest.json"),
        "facts_coverage": facts_manifest["coverage"],
        "ready_receipt": receipt,
    }, ensure_ascii=False, indent=2))


def _load_analysis_results(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    raise ValueError("analysis result must be an object, result array, or {results: [...]}")


def _read_analysis_input(value):
    path = Path(value)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return value


def _analysis_context(run_dir, spec_path=None, input_payload=None):
    root = Path(run_dir)
    manifest_path = root / "facts" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"facts manifest is missing or unreadable: {manifest_path}") from exc
    workflow_status = get_workflow_status(root)
    if input_payload is not None:
        spec = parse_analysis_input(input_payload, workflow_status, manifest)
        input_errors = validate_analysis_input(spec, workflow_status, manifest)
        if input_errors:
            raise ValueError("invalid analysis input: " + "; ".join(input_errors))
    else:
        if not spec_path:
            raise ValueError("analyze requires --input or legacy --spec")
        if workflow_status["status"] not in {"ready_for_analysis", "analysis_failed", "analysis_completed"}:
            raise ValueError("run must be ready_for_analysis before analysis can start")
        spec = parse_analysis_spec(json.loads(Path(spec_path).read_text(encoding="utf-8")))
    selected = load_selected_facts(root, spec)
    fact_index = build_fact_index(selected)
    packet = build_prompt_packet(spec, selected, fact_index)
    intent_path = root / "analysis" / "analysis-intent.json"
    standardized_intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.is_file() else None
    catalog = build_fact_catalog(root)
    request = build_model_analysis_request(
        spec,
        selected,
        fact_index,
        standardized_intent=standardized_intent,
        capability_snapshot=catalog.to_dict(),
    )
    request_errors = validate_model_analysis_request(request)
    if request_errors:
        raise ValueError("ModelAnalysisRequest 校验失败: " + "; ".join(request_errors))
    return {
        "root": root,
        "spec": spec,
        "selected": selected,
        "fact_index": fact_index,
        "packet": packet,
        "request": request,
    }


def _workflow_analysis_context(run_dir, analysis_id):
    """Load the persisted local selection for the final workflow handoff."""
    root = Path(run_dir)
    manifest = json.loads((root / "facts" / "manifest.json").read_text(encoding="utf-8"))
    context_root = analysis_context_dir(root, analysis_id)
    input_payload = json.loads((context_root / "analysis-input.json").read_text(encoding="utf-8"))
    spec = parse_analysis_input(input_payload, get_workflow_status(root), manifest)
    selected_context = json.loads(
        (context_root / "selected-fact-context.json").read_text(encoding="utf-8")
    )
    if not isinstance(selected_context, dict) or not isinstance(selected_context.get("sections"), dict):
        raise ValueError("selected fact context is missing or invalid")
    selection_payload = json.loads(
        (context_root / "fact-selection-spec.json").read_text(encoding="utf-8")
    )
    selected_packages = selection_payload.get("packages", []) if isinstance(selection_payload, dict) else []
    selected_sections = [item for item in selected_packages if item in {
        "material_ranking", "audience", "script", "highlight_frames"
    }]
    if selected_sections:
        spec_payload = spec.to_dict()
        spec_payload["sections"] = selected_sections
        spec = parse_analysis_spec(spec_payload)
    selected = {
        "manifest": manifest,
        "sections": selected_context["sections"],
        "aggregates": selected_context.get("aggregates", {}),
        "data_gaps": selected_context.get("data_gaps", []),
    }
    fact_index = build_fact_index(selected)
    packet = build_prompt_packet(spec, selected, fact_index)
    intent_path = context_root / "analysis-intent.json"
    standardized_intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.is_file() else None
    catalog = build_fact_catalog(root)
    request = build_model_analysis_request(
        spec,
        selected,
        fact_index,
        standardized_intent=standardized_intent,
        capability_snapshot=catalog.to_dict(),
    )
    request_errors = validate_model_analysis_request(request)
    if request_errors:
        raise ValueError("ModelAnalysisRequest 校验失败: " + "; ".join(request_errors))
    phase_two = {
        "business_question": spec.business_question,
        "intent": json.loads((context_root / "analysis-intent.json").read_text(encoding="utf-8")),
        "selection_spec": selection_payload,
        "selection_summary": selected_context.get("selection_summary", {}),
        "aggregates": selected_context.get("aggregates", {}),
        "data_gaps": selected_context.get("data_gaps", []),
    }
    return {
        "root": root,
        "spec": spec,
        "selected": selected,
        "fact_index": fact_index,
        "packet": packet,
        "request": request,
        "phase_two": phase_two,
    }


def _workflow_output_path(run_dir, analysis_id, output):
    """Keep each workflow report inside its analysis-specific artifact directory."""
    context_root = analysis_context_dir(Path(run_dir), analysis_id).resolve()
    path = (Path(output) if output else context_root / "workflow.md").resolve()
    try:
        path.relative_to(context_root)
    except ValueError as exc:
        raise ValueError(
            f"workflow output must be inside analysis context: {context_root}"
        ) from exc
    return path


def _analysis_pipeline(run_dir, spec_path=None, result_path=None, input_payload=None, *, context=None):
    if not result_path:
        raise ValueError(
            "当前对话模型尚未返回 AnalysisResult；请先交接 ModelAnalysisRequest，"
            "再使用 --analysis-result 回放结构化结果。"
        )
    context = context or _analysis_context(run_dir, spec_path, input_payload)
    root = context["root"]
    spec = context["spec"]
    selected = context["selected"]
    fact_index = context["fact_index"]
    packet = context["packet"]
    results = _load_analysis_results(result_path)
    normalized_results = [normalize_analysis_result(result) for result in results]
    result_errors = [
        error
        for result in normalized_results
        for error in validate_analysis_result(result, fact_index)
    ]
    if result_errors:
        raise AnalysisResultValidationError(result_errors)
    manifest = selected["manifest"]
    ranking_selection = selected.get("sections", {}).get("material_ranking", {})
    metadata = {
        "run_id": spec.run_id,
        "analysis_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "facts_version": manifest.get("schema_version"),
        "prompt_version": packet["prompt_version"],
        "request_version": MODEL_ANALYSIS_REQUEST_VERSION,
        "fact_index": fact_index,
        "coverage": manifest.get("coverage", {}),
        "brand_classification": manifest.get("brand_classification", {}),
        "brand_audit_records": ranking_selection.get("records", []),
        "package_paths": [
            package["path"]
            for package in manifest.get("packages", {}).values()
            if isinstance(package, dict) and isinstance(package.get("path"), str)
        ],
    }
    if context.get("phase_two"):
        metadata["phase_two"] = context["phase_two"]
    report = render_analysis_report(spec, normalized_results, metadata)
    result_document = {
        "analysis_id": spec.analysis_id,
        "run_id": spec.run_id,
        "analysis_time": metadata["analysis_time"],
        "facts_version": metadata["facts_version"],
        "prompt_version": metadata["prompt_version"],
        "request_type": "yuntu_commercial_analysis",
        "request_version": MODEL_ANALYSIS_REQUEST_VERSION,
        "results": normalized_results,
    }
    return report, result_document, packet, context["request"]


def _analysis_output_paths(output_path):
    output = Path(output_path)
    return {
        "report": output,
        "result": output.with_suffix(".analysis-result.json"),
        "prompt": output.with_suffix(".prompt-packet.json"),
        "request": output.with_suffix(".model-analysis-request.json"),
        "error": output.with_suffix(".analysis-error.json"),
    }


def _write_analysis_handoff(output_path, request, packet):
    paths = _analysis_output_paths(output_path)
    paths["request"].parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(request, paths["request"])
    write_json_atomically(packet, paths["prompt"])
    return paths


def _write_analysis_error(output_path, request, error):
    paths = _analysis_output_paths(output_path)
    errors = getattr(error, "errors", None)
    if not isinstance(errors, list):
        errors = [{
            "code": "analysis_failed",
            "path": "$",
            "message": str(error),
            "suggestion": "检查分析输入、事实引用和模型返回的 JSON。",
        }]
    legacy_message = str(error)
    write_json_atomically({
        "request_type": request.get("request_type"),
        "request_version": request.get("request_version"),
        "analysis_id": request.get("host_context", {}).get("analysis_id"),
        "run_id": request.get("host_context", {}).get("run_id"),
        "error": legacy_message,
        "code": "analysis_result_invalid" if getattr(error, "errors", None) else "analysis_failed",
        "path": errors[0].get("path", "$") if errors else "$",
        "message": legacy_message,
        "suggestion": errors[0].get("suggestion", "") if errors else "",
        "errors": errors,
    }, paths["error"])
    return paths["error"]


def _write_text_atomically(text, output_path):
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp", dir=target.parent, delete=False) as staging:
        staging_path = Path(staging.name)
        staging.write(text)
    try:
        os.replace(staging_path, target)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise


def _write_analysis_outputs(output_path, report, result_document, packet):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    result_output = output.with_suffix(".analysis-result.json")
    prompt_output = output.with_suffix(".prompt-packet.json")
    staging = Path(tempfile.mkdtemp(prefix=".analysis-", dir=output.parent))
    staged_outputs = (
        (staging / output.name, output, "report"),
        (staging / result_output.name, result_output, "result"),
        (staging / prompt_output.name, prompt_output, "prompt"),
    )
    try:
        _write_text_atomically(report, staged_outputs[0][0])
        write_json_atomically(result_document, staged_outputs[1][0])
        write_json_atomically(packet, staged_outputs[2][0])
        # Publish the report last so a failed preparation never leaves a report
        # without its structured result and prompt packet.
        for staged, target, _ in (staged_outputs[1], staged_outputs[2], staged_outputs[0]):
            os.replace(staged, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _enforce_external_runtime_paths(args)
        environment = None
        if args.command in {"runtime-check", "list-accounts", "schema", "collect"}:
            environment, receipt = _cli_runtime(args)
            if args.command == "runtime-check":
                print(json.dumps(receipt, ensure_ascii=False, indent=2))
                return 0
            print(json.dumps({"runtime": receipt}, ensure_ascii=False), file=sys.stderr)
        elif args.runtime_config_stdin or args.expected_gateway:
            raise RpaConnectorError("runtime options are only supported for runtime-check and RPA commands")
        if args.command == "render-overview":
            output_dir = Path(args.output_dir) if args.output_dir else Path(args.run_dir) / "overview"
            receipt = write_overview(
                Path(args.run_dir),
                output_dir,
                SCRIPT_DIR.parent / "config" / "yuntu-material-field-registry-v1.json",
            )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        if args.command == "list-accounts":
            print(json.dumps(list_accounts(environ=environment), ensure_ascii=False, indent=2))
            return 0
        if args.command == "schema":
            details = {
                operation: _operation_client(operation).normalized_detail(
                    fetch_connector_detail(operation, environ=environment)
                )
                for operation in ("list", "detail")
            }
            print(json.dumps(details, ensure_ascii=False, indent=2))
            return 0
        if args.command == "self-test":
            _cmd_self_test()
            return 0
        if args.command == "validate":
            query = resolve_query_spec(json.loads(Path(args.query_spec).read_text(encoding="utf-8")))
            raw_highlights = json.loads(Path(args.highlight_spec).read_text(encoding="utf-8")) if args.highlight_spec else None
            highlights = resolve_highlight_request(raw_highlights, enabled={"on": True, "off": False}.get(args.highlight_mode))["spec"]
            print(json.dumps({"query": query, "highlights": highlights, "fingerprint": spec_fingerprint(query, highlights)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "status":
            if collection_status is None:
                raise RpaConnectorError("batch status support is unavailable")
            output = collection_status(output_dir=args.output_dir)
            output["ready_receipt"] = build_ready_receipt(args.output_dir)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "retry-highlights":
            specs = _load_saved_specs(args.output_dir)
            status = retry_highlights_batch(
                output_dir=args.output_dir, highlight_spec=specs[1]
            )
            facts_manifest = _build_facts_after_run(args.output_dir)
            _print_batch_result(args.output_dir, facts_manifest)
            return status
        if args.command == "build-facts":
            manifest = build_fact_packages(args.run_dir)
            print(json.dumps({
                "manifest_path": str(Path(args.run_dir) / "facts/manifest.json"),
                "coverage": manifest["coverage"],
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "workflow":
            if not args.fact_selection_result and not args.analysis_result:
                if args.analysis_id:
                    context_status = get_analysis_status(args.run_dir, args.analysis_id)
                    if context_status["status"] not in {"facts_selected", "analysis_failed"}:
                        raise ValueError(
                            "analysis context requires facts_selected or analysis_failed before model handoff"
                        )
                    request = prepare_model_analysis_handoff(args.run_dir, args.analysis_id)
                    context_root = analysis_context_dir(args.run_dir, args.analysis_id)
                    print(json.dumps({
                        "status": "awaiting_model_analysis",
                        "analysis_id": request["host_context"]["analysis_id"],
                        "run_id": request["host_context"]["run_id"],
                        "model_analysis_request_path": str(context_root / "model-analysis-request.json"),
                        "host_handoff": {
                            "model": "current_conversation_model",
                            "response_schema": "AnalysisResult",
                            "response_format": "JSON object or {\"results\": [...]} with no prose",
                        },
                    }, ensure_ascii=False, indent=2))
                    return 0
                request = prepare_fact_selection_handoff(args.run_dir, args.business_question)
                analysis_id = request["host_context"]["analysis_id"]
                context_root = analysis_context_dir(args.run_dir, analysis_id)
                print(json.dumps({
                    "status": "awaiting_fact_selection",
                    "analysis_id": analysis_id,
                    "run_id": request["host_context"]["run_id"],
                    "fact_selection_request_path": str(context_root / "fact-selection-request.json"),
                    "host_handoff": {
                        "model": "current_conversation_model",
                        "response_schema": "FactSelectionSpec",
                        "response_format": "JSON object with intent, selection_spec, and run_id",
                        "resume_with": "将模型返回的 JSON 保存后，使用同一 run-dir、analysis_id 和 --fact-selection-result 重新执行 workflow。",
                    },
                }, ensure_ascii=False, indent=2))
                return 0
            if not args.analysis_id:
                raise ValueError("--analysis-id is required when resuming a workflow analysis")
            context_root = analysis_context_dir(args.run_dir, args.analysis_id)
            if args.fact_selection_result:
                workflow_status = get_analysis_status(args.run_dir, args.analysis_id)
                if workflow_status["status"] == "awaiting_fact_selection":
                    selection_payload = json.loads(Path(args.fact_selection_result).read_text(encoding="utf-8"))
                    selected = apply_fact_selection_result(args.run_dir, args.analysis_id, selection_payload)
                elif workflow_status["status"] == "facts_selected":
                    selected_context = json.loads(
                        (context_root / "selected-fact-context.json").read_text(encoding="utf-8")
                    )
                    selected = {
                        "selection_summary": selected_context.get("selection_summary", {}),
                    }
                else:
                    raise ValueError("fact selection requires awaiting_fact_selection or facts_selected")
            else:
                selected = {"selection_summary": {}}
            request = prepare_model_analysis_handoff(args.run_dir, args.analysis_id)
            if args.analysis_result:
                context = _workflow_analysis_context(args.run_dir, args.analysis_id)
                request_id = context["request"]["host_context"]["analysis_id"]
                output = _workflow_output_path(args.run_dir, args.analysis_id, args.output)
                begin_model_analysis(args.run_dir, request_id, args.analysis_id)
                try:
                    report, result_document, packet, _ = _analysis_pipeline(
                        args.run_dir,
                        input_payload=json.loads(
                            (context_root / "analysis-input.json").read_text(encoding="utf-8")
                        ),
                        result_path=args.analysis_result,
                        context=context,
                    )
                    _write_analysis_outputs(output, report, result_document, packet)
                    complete_model_analysis(
                        args.run_dir,
                        args.analysis_id,
                        _analysis_output_paths(output)["result"],
                        _analysis_output_paths(output)["report"],
                    )
                except Exception as exc:
                    try:
                        fail_model_analysis(args.run_dir, args.analysis_id, request_id, str(exc))
                        _write_analysis_error(output, context["request"], exc)
                    except WorkflowStateError:
                        pass
                    raise
                print(json.dumps({
                    "status": "analysis_completed",
                    "report_path": str(_analysis_output_paths(output)["report"]),
                    "analysis_result_path": str(_analysis_output_paths(output)["result"]),
                    "model_analysis_request_path": str(_analysis_output_paths(output)["request"]),
                    "prompt_packet_path": str(_analysis_output_paths(output)["prompt"]),
                }, ensure_ascii=False, indent=2))
                return 0
            print(json.dumps({
                "status": "awaiting_model_analysis",
                "analysis_id": request["host_context"]["analysis_id"],
                "run_id": request["host_context"]["run_id"],
                "selected_record_counts": selected["selection_summary"]["selected_record_counts"],
                "model_analysis_request_path": str(context_root / "model-analysis-request.json"),
                "host_handoff": {
                    "model": "current_conversation_model",
                    "response_schema": "AnalysisResult",
                    "response_format": "JSON object or {\"results\": [...]} with no prose",
                },
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "analyze":
            if not args.input and not args.spec:
                parser.error("analyze requires --input or legacy --spec")
            input_payload = _read_analysis_input(args.input) if args.input else None
            if not args.analysis_result:
                context = _analysis_context(args.run_dir, args.spec, input_payload)
                host_context = context["request"]["host_context"]
                paths = _write_analysis_handoff(
                    args.output, context["request"], context["packet"]
                )
                print(json.dumps({
                    "status": "awaiting_model_analysis",
                    "analysis_id": host_context["analysis_id"],
                    "run_id": host_context["run_id"],
                    "model_analysis_request_path": str(paths["request"]),
                    "prompt_packet_path": str(paths["prompt"]),
                    "model_analysis_request": context["request"],
                    "host_handoff": {
                        "protocol": MODEL_ANALYSIS_HANDOFF_PROTOCOL,
                        "model": "current_conversation_model",
                        "request": context["request"],
                        "response_schema": "AnalysisResult",
                        "response_format": "JSON object or {\"results\": [...]} with no prose",
                        "resume_with": "将模型返回的 JSON 保存后，使用同一 run-dir 和 --analysis-result 重新执行 analyze。",
                    },
                }, ensure_ascii=False, indent=2))
                return 0

            context = _analysis_context(args.run_dir, args.spec, input_payload)
            request = context["request"]
            request_id = request["host_context"]["analysis_id"]
            _write_analysis_handoff(args.output, request, context["packet"])
            begin_model_analysis(args.run_dir, request_id)
            try:
                report, result_document, packet, _ = _analysis_pipeline(
                    args.run_dir,
                    args.spec,
                    args.analysis_result,
                    input_payload,
                    context=context,
                )
                _write_analysis_outputs(args.output, report, result_document, packet)
                complete_model_analysis(
                    args.run_dir,
                    request_id,
                    _analysis_output_paths(args.output)["result"],
                    _analysis_output_paths(args.output)["report"],
                )
            except Exception as exc:
                try:
                    fail_model_analysis(args.run_dir, request_id, request_id, str(exc))
                    _write_analysis_error(args.output, request, exc)
                except WorkflowStateError:
                    pass
                raise
            print(json.dumps({
                "status": "analysis_completed",
                "report_path": str(_analysis_output_paths(args.output)["report"]),
                "analysis_result_path": str(_analysis_output_paths(args.output)["result"]),
                "model_analysis_request_path": str(_analysis_output_paths(args.output)["request"]),
                "prompt_packet_path": str(_analysis_output_paths(args.output)["prompt"]),
            }, ensure_ascii=False, indent=2))
            return 0
        query = resolve_query_spec(json.loads(Path(args.query_spec).read_text(encoding="utf-8")))
        raw_highlights = json.loads(Path(args.highlight_spec).read_text(encoding="utf-8")) if args.highlight_spec else None
        highlights = resolve_highlight_request(raw_highlights, enabled={"on": True, "off": False}.get(args.highlight_mode))["spec"]
        write_resolved_specs(args.output_dir, query, highlights)
        fingerprint = spec_fingerprint(query, highlights)
        status = fetch_batch(
            shop_id=args.shop_id, list_params=query_to_rpa_params(query),
            detail_date_params=detail_to_rpa_params(query),
            target_top_n=query["collection"]["target_top_n"],
            candidate_top_n=query["collection"]["candidate_top_n"],
            material_list=query["material_list"],
            output_dir=args.output_dir, invoker=_batch_invoker(environ=environment),
            highlight_spec=highlights, configuration_fingerprint=fingerprint,
        )
        facts_manifest = _build_facts_after_run(args.output_dir)
        _print_batch_result(args.output_dir, facts_manifest)
        return status
    except AnalysisInputRequired as exc:
        workflow_status = get_workflow_status(args.run_dir) if args.command == "analyze" else None
        if not isinstance(workflow_status, dict) or workflow_status["status"] not in {"ready_for_analysis", "analysis_completed"}:
            parser.error(str(exc))
        print(json.dumps({
            "status": "analysis_input_required",
            "stage": workflow_status["status"],
            "ready_for_analysis": workflow_status["ready_for_analysis"],
            "message": "请直接输入分析思路或商业问题；收到前不会启动商业分析。",
        }, ensure_ascii=False, indent=2))
        return 0
    except (RpaConnectorError, ConfigSpecError, FactPackageError, WorkflowStateError, OverviewReportError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
