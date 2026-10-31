"""Bounded production smoke for the password-protected Short Notes web app.

The script never retries generation and never prints passwords, cookies, CSRF
tokens, signed Blob URLs, or provider errors. Node runs the same official
``uploadPresigned`` helper as the browser so the direct-upload contract is
covered end to end.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests


POLL_SECONDS = 5
JOB_DEADLINE_SECONDS = 15 * 60
REQUEST_TIMEOUT = (10, 45)
MAX_PDF_BYTES = 100 * 1024 * 1024

_UPLOAD_HELPER = r"""
import { readFile } from "node:fs/promises";
import { uploadPresigned } from "@vercel/blob/client";

const bytes = await readFile(process.env.SHORTNOTES_SMOKE_PDF);
const body = new Blob([bytes], { type: "application/pdf" });
const result = await uploadPresigned(
  process.env.SHORTNOTES_SMOKE_PATHNAME,
  body,
  {
    access: "private",
    contentType: "application/pdf",
    handleUploadUrl: `${process.env.SHORTNOTES_SMOKE_ORIGIN}/api/blob-upload`,
    clientPayload: process.env.SHORTNOTES_SMOKE_REQUEST_ID,
    multipart: bytes.byteLength >= 8 * 1024 * 1024,
    headers: {
      Cookie: process.env.SHORTNOTES_SMOKE_COOKIE,
      Origin: process.env.SHORTNOTES_SMOKE_ORIGIN,
      "X-CSRF-Token": process.env.SHORTNOTES_SMOKE_CSRF,
    },
  },
);
process.stdout.write(JSON.stringify({
  url: result.url,
  pathname: result.pathname,
  size: bytes.byteLength,
}));
"""


class SmokeFailure(RuntimeError):
    pass


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SmokeFailure("Base URL must be one HTTPS origin")
    return f"https://{parsed.hostname}"


def _json_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected: set[int],
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    response = session.request(
        method,
        url,
        headers=headers,
        json=body,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    if response.status_code not in expected:
        raise SmokeFailure(
            f"Unexpected HTTP status {response.status_code} for {urlsplit(url).path}"
        )
    try:
        value = response.json()
    except ValueError as error:
        raise SmokeFailure("Endpoint returned invalid JSON") from error
    if not isinstance(value, dict):
        raise SmokeFailure("Endpoint returned an invalid object")
    return response.status_code, value


def _cookie_header(session: requests.Session) -> str:
    pairs = [f"{cookie.name}={cookie.value}" for cookie in session.cookies]
    if not pairs:
        raise SmokeFailure("Login did not establish a session")
    return "; ".join(pairs)


def _direct_upload(
    *,
    origin: str,
    pdf_path: Path,
    pathname: str,
    request_id: str,
    csrf: str,
    cookie: str,
) -> dict:
    environment = os.environ.copy()
    environment.update(
        {
            "SHORTNOTES_SMOKE_ORIGIN": origin,
            "SHORTNOTES_SMOKE_PDF": str(pdf_path.resolve()),
            "SHORTNOTES_SMOKE_PATHNAME": pathname,
            "SHORTNOTES_SMOKE_REQUEST_ID": request_id,
            "SHORTNOTES_SMOKE_CSRF": csrf,
            "SHORTNOTES_SMOKE_COOKIE": cookie,
        }
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", _UPLOAD_HELPER],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeFailure("Official direct-upload helper failed")
    try:
        value = json.loads(completed.stdout)
    except ValueError as error:
        raise SmokeFailure("Upload helper returned invalid output") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"url", "pathname", "size"}
        or not isinstance(value["url"], str)
        or value["pathname"] != pathname
        or value["size"] != pdf_path.stat().st_size
    ):
        raise SmokeFailure("Upload helper returned an invalid receipt")
    return value


def run(origin: str, pdf_path: Path, password: str, output_dir: Path) -> Path:
    if not pdf_path.is_file():
        raise SmokeFailure("Input PDF does not exist")
    size = pdf_path.stat().st_size
    if size < 5 or size > MAX_PDF_BYTES:
        raise SmokeFailure("Input PDF size is outside the app limits")
    with pdf_path.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise SmokeFailure("Input file is not a PDF")

    session = requests.Session()
    browser_headers = {"Origin": origin}

    _, health = _json_request(
        session,
        "GET",
        f"{origin}/api/web?action=health",
        expected={200},
    )
    if health != {"ok": True}:
        raise SmokeFailure("Health check failed")
    print("health: 200")

    _, auth = _json_request(
        session,
        "POST",
        f"{origin}/api/web?action=auth",
        expected={200},
        headers=browser_headers,
        body={"password": password},
    )
    csrf = auth.get("csrf")
    if not isinstance(csrf, str) or not csrf:
        raise SmokeFailure("Login response is invalid")
    state_headers = {"Origin": origin, "X-CSRF-Token": csrf}
    cookie = _cookie_header(session)
    print("login: 200")

    request_id = uuid.uuid4().hex
    print(f"request: {request_id}")
    _, prepared = _json_request(
        session,
        "POST",
        f"{origin}/api/blob-upload?action=prepare",
        expected={200},
        headers=state_headers,
        body={"requestId": request_id},
    )
    pathname = prepared.get("pathname")
    if (
        not isinstance(pathname, str)
        or not pathname.startswith("shortnotes/v1/inputs/")
        or not pathname.endswith(f"/{request_id}.pdf")
    ):
        raise SmokeFailure("Upload preparation returned an invalid path")

    uploaded = _direct_upload(
        origin=origin,
        pdf_path=pdf_path,
        pathname=pathname,
        request_id=request_id,
        csrf=csrf,
        cookie=cookie,
    )
    print(f"upload: {uploaded['size']} bytes")

    start_body = {
        "requestId": request_id,
        "inputUrl": uploaded["url"],
        "inputSize": uploaded["size"],
        "filename": "deployment-smoke.pdf",
        "teacherName": "Smoke Test",
        "subject": "chemistry",
        "language": "english",
        "instructions": "Keep this deployment smoke concise.",
        "watermark": False,
        "logoWatermark": True,
    }
    _, started = _json_request(
        session,
        "POST",
        f"{origin}/api/web?action=start",
        expected={200, 202},
        headers=state_headers,
        body=start_body,
    )
    if started.get("jobId") != request_id:
        raise SmokeFailure("Start response has the wrong job ID")
    print(f"start: {started.get('state', 'unknown')}")

    allowed = {
        "queued": {"queued", "running", "failed"},
        "running": {"running", "complete", "failed"},
    }
    state = started.get("state")
    deadline = time.monotonic() + JOB_DEADLINE_SECONDS
    while state not in {"complete", "failed"}:
        if state not in allowed or time.monotonic() >= deadline:
            raise SmokeFailure("Job did not reach a terminal state within its bound")
        time.sleep(POLL_SECONDS)
        _, status = _json_request(
            session,
            "GET",
            f"{origin}/api/web?action=status&jobId={request_id}",
            expected={200},
        )
        next_state = status.get("state")
        if next_state not in allowed[state]:
            raise SmokeFailure("Job state regressed or is invalid")
        if next_state != state:
            print(f"state: {state} -> {next_state}")
        state = next_state

    if state != "complete":
        raise SmokeFailure("Generation finished in the failed state")

    _, signed = _json_request(
        session,
        "POST",
        f"{origin}/api/blob-download",
        expected={200},
        headers=state_headers,
        body={"jobId": request_id},
    )
    signed_url = signed.get("url")
    if not isinstance(signed_url, str) or not signed_url.startswith("https://"):
        raise SmokeFailure("Download signing response is invalid")
    download = session.get(
        signed_url,
        timeout=(10, 120),
        allow_redirects=False,
    )
    if download.status_code != 200 or not download.content.startswith(b"%PDF-"):
        raise SmokeFailure("Downloaded output is not a complete PDF")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"production-smoke-{request_id}.pdf"
    output_path.write_bytes(download.content)
    print(f"download: {len(download.content)} bytes")

    _, acknowledged = _json_request(
        session,
        "POST",
        f"{origin}/api/web?action=downloaded",
        expected={200},
        headers=state_headers,
        body={"jobId": request_id, "fetched": True},
    )
    if acknowledged != {"ok": True, "deleted": True}:
        raise SmokeFailure("Cleanup acknowledgement was not confirmed")
    status_code, _ = _json_request(
        session,
        "GET",
        f"{origin}/api/web?action=status&jobId={request_id}",
        expected={404},
    )
    print(f"cleanup proof: status {status_code}")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin", help="Production HTTPS origin")
    parser.add_argument("pdf", type=Path, help="Small source PDF")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/pdf"),
        help="Local directory for the verified result",
    )
    args = parser.parse_args()
    password = os.getenv("SHORTNOTES_SMOKE_PASSWORD") or getpass.getpass(
        "App password: "
    )
    try:
        output = run(_origin(args.origin), args.pdf, password, args.output_dir)
    except SmokeFailure as error:
        print(f"smoke failed: {error}", file=sys.stderr)
        return 1
    except requests.RequestException:
        print("smoke failed: network request failed", file=sys.stderr)
        return 1
    except subprocess.SubprocessError:
        print("smoke failed: upload helper process failed", file=sys.stderr)
        return 1
    print(f"verified PDF: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
