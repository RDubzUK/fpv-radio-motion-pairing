"""Finish a local deployment without cancelling current extraction or exports.

Run once after code is installed. Never resumes processing or changes its data.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def busy(data):
    active = {"queued", "running", "cancelling"}
    for path in (data / "jobs").glob("*/job.json"):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            # Do not interrupt a task whose durable state is unreadable.
            return True
        if job.get("status") in active:
            return True
        if any(item.get("status") in active for collection in ("charts", "exports", "proxies")
               for item in job.get(collection, {}).values()):
            return True
    return False


def main():
    data = Path(os.environ.get("FPV_MATCHER_DATA", str(Path.home() / ".fpv-radio-motion-pairing")))
    deadline = time.monotonic() + 86400
    print("Deployment waiting for processing to finish", flush=True)
    while time.monotonic() < deadline:
        if not busy(data):
            # Require two idle observations to avoid restarting during a
            # transition between clips/tasks; new work postpones deployment.
            time.sleep(5)
            if not busy(data):
                subprocess.run(["systemctl", "--user", "restart", "fpv-radio-motion-pairing.service"], check=True)
                print("Deployment complete: idle viewer restarted", flush=True)
                return
        time.sleep(20)
    raise RuntimeError("Deployment deferred: processing did not become idle within 24 hours")


if __name__ == "__main__":
    main()
