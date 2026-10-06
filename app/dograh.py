import re

import httpx

from app.settings import get_settings

WORKFLOW_ID = 1
TELEPHONY_CONFIGURATION_ID = 2
FROM_PHONE_NUMBER_ID = 1

EXTRACTED_FIELDS = ("caller_name", "requirement", "language", "next_step", "opted_out")


class DograhError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def build_initiate_payload(phone: str, variables: dict | None) -> dict:
    return {
        "workflow_id": WORKFLOW_ID,
        "telephony_configuration_id": TELEPHONY_CONFIGURATION_ID,
        "from_phone_number_id": FROM_PHONE_NUMBER_ID,
        "phone_number": phone,
        "context_variables": variables or {},
    }


RUN_NAME_RE = re.compile(r"WR-TEL-OUT-\d+")


def run_id_from(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("workflow_run_id", "run_id", "id"):
        if data.get(key):
            return str(data[key])
    run = data.get("run")
    if isinstance(run, dict) and run.get("id"):
        return str(run["id"])
    return ""


def run_name_from(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    match = RUN_NAME_RE.search(str(data.get("message") or ""))
    return match.group(0) if match else ""


def run_id_named(data: dict, name: str) -> str:
    if not isinstance(data, dict) or not name:
        return ""
    runs = data.get("runs") or data.get("data") or data.get("items") or []
    if not isinstance(runs, list):
        return ""
    for run in runs:
        if isinstance(run, dict) and str(run.get("name") or "") == name and run.get("id"):
            return str(run["id"])
    return ""


def empty_extracted() -> dict:
    return {
        "caller_name": "",
        "requirement": "",
        "language": "",
        "next_step": "",
        "opted_out": False,
    }


def _dig_extracted(data: dict) -> dict:
    found = empty_extracted()
    if not isinstance(data, dict):
        return found
    gathered = data.get("gathered_context") if isinstance(data.get("gathered_context"), dict) else {}
    initial = data.get("initial_context") if isinstance(data.get("initial_context"), dict) else {}
    candidates = [
        data.get("extracted"),
        data.get("extracted_variables"),
        gathered.get("extracted_variables"),
        gathered,
        initial.get("extracted"),
        initial.get("extracted_variables"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in EXTRACTED_FIELDS:
                if key in candidate and candidate[key] not in (None, ""):
                    found[key] = candidate[key]
    if isinstance(found["opted_out"], str):
        found["opted_out"] = found["opted_out"].lower() in {"1", "true", "yes"}
    else:
        found["opted_out"] = bool(found["opted_out"])
    for key in ("caller_name", "requirement", "language", "next_step"):
        found[key] = str(found[key] or "")
    return found


def _http_url(value) -> str:
    if isinstance(value, str) and value.startswith("http"):
        return value
    return ""


def recording_from(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("recording_public_url", "recording_url", "recordingUrl", "recording"):
        url = _http_url(data.get(key))
        if url:
            return url
    artifacts = data.get("artifacts")
    if isinstance(artifacts, dict):
        for key in ("recording_url", "audio_url"):
            url = _http_url(artifacts.get(key))
            if url:
                return url
    return ""


FAILED_CALL_STATUSES = {"failed", "busy", "no-answer", "canceled"}


def status_from(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    gathered = data.get("gathered_context") if isinstance(data.get("gathered_context"), dict) else {}
    logs = data.get("logs") if isinstance(data.get("logs"), dict) else {}
    callbacks = logs.get("telephony_status_callbacks") or []
    last = ""
    if isinstance(callbacks, list) and callbacks and isinstance(callbacks[-1], dict):
        last = str(callbacks[-1].get("status") or callbacks[-1].get("CallStatus") or "").lower()
    call_status = str(gathered.get("call_status") or "").lower()
    if last in FAILED_CALL_STATUSES:
        return "failed"
    if data.get("is_completed") or last == "completed" or call_status in {"end_call", "completed"}:
        return "completed"
    if last == "ringing":
        return "ringing"
    if last in {"in-progress", "answered", "in_progress"}:
        return "in_progress"
    return ""


def _lines_from_logs(data: dict) -> list[str]:
    logs = data.get("logs") if isinstance(data.get("logs"), dict) else {}
    events = logs.get("realtime_feedback_events") or []
    if not isinstance(events, list):
        return []
    lines = []
    for event in events:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        if event.get("type") == "rtf-bot-text":
            lines.append(f"Agent: {text}")
        elif event.get("type") == "rtf-user-transcription" and payload.get("final"):
            lines.append(f"Caller: {text}")
    return lines


def transcript_from(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    lines = _lines_from_logs(data)
    if lines:
        return "\n".join(lines)
    value = data.get("transcript") or data.get("transcription") or ""
    if isinstance(value, list):
        lines = []
        for turn in value:
            if isinstance(turn, dict):
                lines.append(str(turn.get("text") or turn.get("content") or ""))
            else:
                lines.append(str(turn))
        return "\n".join(line for line in lines if line)
    if isinstance(value, str) and value.startswith("http"):
        return ""
    return str(value or "")


def audio_payload(content: bytes, media: str) -> tuple[bytes, str]:
    kind = (media or "").split(";")[0].strip().lower()
    if "json" in kind or content[:1] in (b"{", b"["):
        raise DograhError("the recording could not be loaded")
    if kind.startswith("audio/"):
        return content, kind
    if content[:4] == b"RIFF":
        return content, "audio/wav"
    if content[:3] == b"ID3" or content[:2] == b"\xff\xfb":
        return content, "audio/mpeg"
    raise DograhError("the recording could not be loaded")


class DograhClient:
    def __init__(self):
        self.transport = None

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = get_settings().dograh_api_key
        if key:
            headers["X-API-Key"] = key
        return headers

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if self.transport:
            return self.transport(method, path, payload)
        base = get_settings().dograh_base_url.rstrip("/")
        url = base + path
        with httpx.Client(timeout=20) as client:
            response = client.request(method, url, json=payload, headers=self._headers())
        try:
            data = response.json()
        except ValueError:
            data = {"detail": response.text[:500]}
        if response.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else response.text
            raise DograhError(str(detail or "Dograh request failed"), response.status_code)
        return data if isinstance(data, dict) else {"data": data}

    def initiate_call(self, phone: str, variables: dict | None) -> dict:
        payload = build_initiate_payload(phone, variables)
        data = self.request("POST", "/api/v1/telephony/initiate-call", payload)
        run_id = run_id_from(data)
        if not run_id:
            name = run_name_from(data)
            if name:
                listed = self.request("GET", f"/api/v1/workflow/{WORKFLOW_ID}/runs?limit=20&sort_by=created_at&sort_order=desc", None)
                run_id = run_id_named(listed, name)
        if not run_id:
            raise DograhError("Dograh did not return a run id")
        return {"run_id": run_id, "payload": payload, "raw": data}

    def _fetch_url(self, url: str) -> httpx.Response:
        headers = {}
        base = get_settings().dograh_base_url.rstrip("/")
        if url.startswith(base):
            headers = self._headers()
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            return client.get(url, headers=headers)

    def fetch_recording(self, url: str) -> tuple[bytes, str]:
        response = self._fetch_url(url)
        if response.status_code >= 400:
            raise DograhError("the recording could not be loaded", response.status_code)
        return audio_payload(response.content, response.headers.get("content-type", ""))

    def fetch_text(self, url: str) -> str:
        response = self._fetch_url(url)
        if response.status_code >= 400:
            return ""
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if "json" in kind or response.content[:1] in (b"{", b"["):
            return ""
        return response.text

    def fetch_run(self, run_id: str) -> dict:
        data = self.request("GET", f"/api/v1/workflow/{WORKFLOW_ID}/runs/{run_id}", None)
        transcript = transcript_from(data)
        if not transcript:
            transcript_url = _http_url(data.get("transcript_public_url"))
            if transcript_url:
                transcript = self.fetch_text(transcript_url)
        return {
            "status": status_from(data),
            "recording_url": recording_from(data),
            "transcript": transcript,
            "extracted": _dig_extracted(data),
            "raw": data,
        }


dograh = DograhClient()
