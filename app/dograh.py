import httpx

from app.settings import get_settings

WORKFLOW_ID = 1
TELEPHONY_CONFIGURATION_ID = 1
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
    candidates = [
        data.get("extracted"),
        data.get("extracted_variables"),
        data.get("gathered_context"),
        (data.get("initial_context") or {}).get("extracted") if isinstance(data.get("initial_context"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in EXTRACTED_FIELDS:
                if key in candidate and candidate[key] is not None:
                    found[key] = candidate[key]
    if isinstance(found["opted_out"], str):
        found["opted_out"] = found["opted_out"].lower() in {"1", "true", "yes"}
    else:
        found["opted_out"] = bool(found["opted_out"])
    for key in ("caller_name", "requirement", "language", "next_step"):
        found[key] = str(found[key] or "")
    return found


def recording_from(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("recording_url", "recordingUrl", "recording"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    artifacts = data.get("artifacts")
    if isinstance(artifacts, dict):
        for key in ("recording_url", "audio_url"):
            if artifacts.get(key):
                return str(artifacts[key])
    return ""


def transcript_from(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    value = data.get("transcript") or data.get("transcription") or ""
    if isinstance(value, list):
        lines = []
        for turn in value:
            if isinstance(turn, dict):
                lines.append(str(turn.get("text") or turn.get("content") or ""))
            else:
                lines.append(str(turn))
        return "\n".join(line for line in lines if line)
    return str(value or "")


class DograhClient:
    def __init__(self):
        self.transport = None

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = get_settings().dograh_api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
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
            raise DograhError("Dograh did not return a run id")
        return {"run_id": run_id, "payload": payload, "raw": data}

    def fetch_run(self, run_id: str) -> dict:
        data = self.request("GET", f"/api/v1/workflow/{WORKFLOW_ID}/runs/{run_id}", None)
        return {
            "recording_url": recording_from(data),
            "transcript": transcript_from(data),
            "extracted": _dig_extracted(data),
            "raw": data,
        }


dograh = DograhClient()
