"""Smoke test the packaged provider and its on-image fixtures over loopback (no external services)."""

import json
import os
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path

TOKEN = "isolated-image-smoke-credential-0001"


def exchange(method, path, headers=None, body=None):
    conn = HTTPConnection("127.0.0.1", 8081, timeout=2)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        assert response.status == 200, (path, response.status)
        assert response.getheader("Content-Length") == str(len(raw))
        assert response.getheader("Transfer-Encoding") is None
        assert response.getheader("Content-Type") == "application/json"
        return json.loads(raw)
    finally:
        conn.close()


def main():
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8081", "--no-access-log"],
        env={**os.environ, "AI_SERVICE_TOKEN": TOKEN},
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                assert exchange("GET", "/health")["mock"] is True
                break
            except ConnectionRefusedError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        for name, action in (("index", "documents"), ("chat", "answer"), ("quiz", "generate"), ("mastery", "update")):
            f = json.loads(Path("/contracts/fixtures", name + ".json").read_text())
            root = "/ai/v1/" + name + "/"
            headers = {"Authorization": "Bearer " + TOKEN, "X-StudyGraph-Protocol": f["request"]["protocol"]}
            assert exchange("GET", root + "capabilities", headers) == {**f["capabilities"], "mock": True}
            headers["Content-Type"] = "application/json"
            if name == "index":
                headers["Idempotency-Key"] = f["request"]["idempotency_key"]
            body = json.dumps(f["request"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            assert exchange("POST", root + action, headers, body) == {**f["response"], "mock": True}
        print("All eight packaged mock operations returned the expected framed JSON.")
    finally:
        process.terminate()
        process.wait(timeout=5)


if __name__ == "__main__":
    main()
