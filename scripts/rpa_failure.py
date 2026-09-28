"""Safe structured remote task failures, shared by list and detail collectors."""
import json

class RemoteTaskError(RuntimeError):
    """Raised when the connector cannot safely complete an operation."""

    def __init__(self, message, *, task_group_id=None, errors=None):
        import re
        def safe(value):
            text = str(value)[:2000]
            text = re.sub(r"https?://\S+", "[url redacted]", text)
            text = re.sub(r"(?i)(?:bearer\s+)[\w.+/-]+", "Bearer [redacted]", text)
            return re.sub(r"(?i)(token|secret|password|authorization|cookie)[\"']?\s*[:=]\s*[\"']?[^,;\s\"']+", r"\1=[redacted]", text)
        details = []
        for error in errors if isinstance(errors, list) else [errors] if errors else []:
            if isinstance(error, dict):
                details.append({"code": safe(error.get("code") or error.get("error_code") or "remote_failed"),
                                "message": safe(error.get("message") or error.get("msg") or error.get("error") or "")})
            else:
                details.append({"code": "remote_failed", "message": safe(error)})
        self.failure_details = {"taskGroupId": safe(task_group_id) if task_group_id else None,
                                "errors": details, "retrySubmitted": False}
        super().__init__(safe(message) + (": " + json.dumps(details, ensure_ascii=False) if details else ""))

