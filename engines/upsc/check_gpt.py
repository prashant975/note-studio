from __future__ import annotations

import argparse
import json
import os

import pw_access
from src.ai_client import GeminiClient
from src.config import DEFAULT_MODEL


def main():
    parser = argparse.ArgumentParser(
        description="Check the GPT text model through the PW proxy. "
        "Prints the proxy's raw replies so the request/response shape of its "
        "/api/gpt/generate route can be confirmed. Pass a signed-in @pw.live Google token."
    )
    parser.add_argument("--google-token", default=os.getenv("PW_GOOGLE_TOKEN", ""), help="Signed-in @pw.live Google access/id token (or set PW_GOOGLE_TOKEN)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    token = args.google_token.strip()
    if not token:
        raise SystemExit("No token. Pass --google-token or set PW_GOOGLE_TOKEN.")
    status = pw_access.check_allowed_status(token)
    print(f"Allowlist status: {status}")
    if status != "allowed":
        raise SystemExit("Not allowed (or proxy/token error). Fix access before testing generation.")
    task_id = pw_access.new_task_id("connection-test")

    print(f"\n== Text: {args.model}")
    raw = pw_access.gpt_generate(
        token, model=args.model, task_id=task_id, filename="(connection test)", count=0,
        request={"messages": [{"role": "user", "content": "Reply with exactly: OK"}], "max_completion_tokens": 2000},
    )
    print("Raw proxy reply:", json.dumps(raw, indent=2)[:2000])

    # Same call through the app's client, with no Gemini fallback, so a
    # translation problem shows up here instead of being papered over.
    client = GeminiClient(token, model=args.model, fallback_model="", task_id=task_id,
                          filename="(connection test)", count=0)
    resp = client.test_connection()
    print(f"Client read: text={resp.text!r} model={resp.model} usage={resp.usage} finish={resp.finish_reason}")


if __name__ == "__main__":
    main()
