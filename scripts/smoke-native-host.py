"""One bounded real framed demo analysis, without saving any evidence."""
import argparse
import json
import os
from pathlib import Path
import selectors
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import poc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 900:
        parser.error("timeout must be 1..900 seconds")
    request = {"version": 1, "op": "classify", "id": "real-demo", "url": poc.DEMO_URL,
               "title": poc.DEMO_TITLE, "thumbnail": poc.DEMO_THUMB}
    process = subprocess.Popen([str(ROOT / "run-native-host")], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "PATH": "/usr/bin:/bin"})
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + args.timeout
    body = json.dumps(request).encode()
    process.stdin.write(struct.pack("=I", len(body)) + body)
    process.stdin.flush()
    data = bytearray()
    target = 4
    try:
        while len(data) < target:
            if not selector.select(max(0, deadline - time.monotonic())):
                print("Real native smoke timed out; cancelling owned session.", file=sys.stderr)
                return 1
            part = process.stdout.read1(target - len(data))
            if not part:
                print("Real native companion disconnected without a result.", file=sys.stderr)
                return 1
            data.extend(part)
            if len(data) == 4:
                length = struct.unpack("=I", data)[0]
                if not 0 < length < 65536:
                    raise ValueError("invalid native frame")
                target = 4 + length
        reply = json.loads(data[4:])
        print(json.dumps({"configured_local_model": poc.MODEL, "reply": reply}, ensure_ascii=False))
        return 0 if reply.get("status") == "scored" else 1
    finally:
        selector.close()
        process.stdin.close()  # Ordinary EOF cancels/reaps only the owned runtime.
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=15)
        process.stdout.close()
        process.stderr.close()


if __name__ == "__main__":
    raise SystemExit(main())
