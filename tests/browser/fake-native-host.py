"""Deterministic test adapter, never used by the production launcher."""
from pathlib import Path
import sys
import os
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import native_host


def record_event(value):
    # Test-only operational counters in the isolated profile's temporary directory.
    # Never record request IDs, URLs, titles, thumbnails, captions or results.
    target = os.environ.get("TRIAGE_TEST_NATIVE_EVENTS")
    if target:
        with open(target, "a") as stream:
            stream.write(value + "\n")


record_event("start")


def classify(url, title, thumbnail, runtime_provider):
    record_event("request")
    if title.startswith("Slow") and os.environ.get("TRIAGE_TEST_SLOW_GATE"):
        gate = Path(os.environ["TRIAGE_TEST_SLOW_GATE"])
        deadline = time.monotonic() + 20
        while not gate.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    else:
        time.sleep(0.01)
    category = next((key for key in ("clickbait", "irrelevant", "sensational") if title.lower().startswith(key)), "valid")
    if title.startswith("No majority"):
        scores = {"clickbait": 50, "irrelevant": 50, "sensational": 0, "valid": 0}
    elif title.startswith("Unavailable"):
        return {"status": "unavailable", "scores": None, "reason": "no_english_captions"}
    else:
        scores = {key: 100 if key == category else 0 for key in ("clickbait", "irrelevant", "sensational", "valid")}
    return {"status": "scored", "scores": scores, "timings_ms": {"total": 10}}


native_host.main(classify)
