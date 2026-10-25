"""Versioned Chrome stdio adapter; business data lives only in memory."""
from contextlib import contextmanager, ExitStack
import json
import os
import queue
import signal
import struct
import sys
import threading

import poc
from triage_core import CLASS_ORDER, Unavailable, canonical_url, safe_thumbnail_url

HOST_NAME = "com.youtube_clickbait_triage.local"
MAX_INPUT = 32768
MAX_OUTPUT = 65536
CONCURRENCY = 3
MAX_PENDING = 64


class Shutdown(BaseException):
    """Disconnect bypasses the classifier's ordinary unavailable catches."""


def read_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        part = stream.read(count - len(data))
        if not part:
            if data:
                raise ValueError("truncated_frame")
            return None
        data.extend(part)
    return bytes(data)


def read_frame(stream):
    header = read_exact(stream, 4)
    if header is None:
        return None
    length = struct.unpack("=I", header)[0]
    if not 0 < length <= MAX_INPUT:
        raise ValueError("frame_size")
    body = read_exact(stream, length)
    if body is None:
        raise ValueError("truncated_frame")
    return json.loads(body.decode("utf-8"))


def write_frame(stream, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(data) >= MAX_OUTPUT:
        raise ValueError("output_size")
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {"version", "op", "id", "url", "title", "thumbnail"}:
        raise Unavailable("invalid_request_fields")
    if type(value["version"]) is not int or value["version"] != 1 or value["op"] != "classify":
        raise Unavailable("unsupported_protocol")
    if not isinstance(value["id"], str) or not 1 <= len(value["id"]) <= 100:
        raise Unavailable("invalid_request_id")
    if not isinstance(value["title"], str) or not value["title"].strip() or len(value["title"]) > 1000:
        raise Unavailable("invalid_title")
    if any(not isinstance(value[field], str) or len(value[field]) > 4096 for field in ("url", "thumbnail")):
        raise Unavailable("invalid_url")
    canonical_url(value["url"])
    safe_thumbnail_url(value["thumbnail"])
    return value


def scores_valid(scores):
    return (isinstance(scores, dict) and set(scores) == set(CLASS_ORDER)
            and all(type(number) is int and 0 <= number <= 100 for number in scores.values())
            and sum(scores.values()) == 100)


class Runtime:
    """Lazy lifetime ownership behind the same context-factory seam as the CLI."""
    def __init__(self, factory=poc.local_server, events=None, closing=None):
        self.stack = ExitStack()
        self.factory = factory
        self.model = None
        self.events = events
        self.closing = closing or threading.Event()
        self.closed = False
        self.lock = threading.RLock()

    def ensure(self):
        # In the companion only the coordinator calls this; synchronous callers
        # retain a small, testable factory seam without serializing inference.
        with self.lock:
            if self.closed or self.closing.is_set():
                raise Unavailable("analysis_cancelled")
            if self.model is None:
                self.model = self.stack.enter_context(self.factory())
            return self.model

    def coordinate(self, operation, model=None):
        if self.closed or self.closing.is_set():
            raise Unavailable("analysis_cancelled")
        if self.events is None:
            return self.ensure() if operation == "lease" else self.invalidate(model)
        event = threading.Event()
        answer = []
        self.events.put((operation, (model, event, answer)))
        while not event.wait(0.05):
            if self.closing.is_set():
                raise Unavailable("analysis_cancelled")
        if isinstance(answer[0], BaseException):
            raise answer[0]
        return answer[0]

    @contextmanager
    def provide(self):
        model = self.coordinate("lease")
        try:
            guard = getattr(model, "require_alive", lambda: None)
            guard()
            with poc.request_scope(lease=model if isinstance(model, poc.OwnedRuntime) else None):
                yield model
            guard()
        finally:
            if not self.closed and not self.closing.is_set():
                try:
                    guard()
                except Unavailable:
                    self.coordinate("invalidate", model)

    def invalidate(self, model):
        with self.lock:
            if self.model is model:
                self.model = None
                self.stack.close()
        return None

    def close(self):
        # Mark before cleanup. Outstanding leases can never borrow a new owner.
        self.closed = True
        self.closing.set()
        self.invalidate(self.model)


def analyze(value, runtime, classifier=poc.triage):
    request_id = value.get("id", "") if isinstance(value, dict) else ""
    if not isinstance(request_id, str) or len(request_id) > 100:
        request_id = ""
    reply = {"version": 1, "id": request_id, "status": "unavailable", "scores": None,
             "reason": "invalid_request", "timings_ms": {}}
    try:
        validate_request(value)
        result = classifier(value["url"], value["title"], value["thumbnail"], runtime_provider=runtime.provide)
        if result.get("status") == "scored" and scores_valid(result.get("scores")):
            reply.update(status="scored", scores=result["scores"], reason="")
        else:
            # Never forward raw model/HTTP text (which can contain untrusted input).
            reason = result.get("reason", "classification_unavailable")
            token = reason.split(":", 1)[0] if isinstance(reason, str) else "classification_unavailable"
            reply["reason"] = token[:100] if token.replace("_", "").isalnum() else "classification_unavailable"
        total = result.get("timings_ms", {}).get("total")
        if type(total) is int and 0 <= total <= 2_000_000:
            reply["timings_ms"] = {"total": total}
    except Unavailable as exc:
        reply["reason"] = str(exc).split(":", 1)[0][:100]
    except Exception:
        reply["reason"] = "unexpected_analysis_failure"
    return reply


def main(classifier=poc.triage, runtime_factory=poc.local_server):
    shutdown_requested = False
    closing = threading.Event()
    def shutdown(*_):
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        closing.set()
        if getattr(poc._request_context, "spawning", False):
            return
        raise Shutdown()

    events = queue.Queue(maxsize=2 * (MAX_PENDING + CONCURRENCY))
    jobs = queue.Queue(maxsize=MAX_PENDING + CONCURRENCY)

    def reader():
        try:
            # A daemon must not hold BufferedReader's lock when SIGTERM exits Python.
            stream = os.fdopen(sys.stdin.fileno(), "rb", buffering=0, closefd=False)
            while not closing.is_set():
                value = read_frame(stream)
                if value is None:
                    break
                events.put_nowait(("input", value))
        except (ValueError, UnicodeError, queue.Full, OSError, RecursionError):
            pass
        finally:
            if not closing.is_set():
                # Process-directed delivery can hit this reader, leaving the main
                # thread asleep in I/O with only a pending Python signal handler.
                signal.pthread_kill(threading.main_thread().ident, signal.SIGUSR1)

    runtime = Runtime(runtime_factory, events, closing)
    children = poc.RetrievalChildren(closing)

    def worker():
        while not closing.is_set():
            value = jobs.get()
            if closing.is_set():
                return
            with poc.request_scope(scope=children):
                reply = analyze(value, runtime, classifier)
            if not closing.is_set():
                events.put(("result", reply))

    try:
        # All handlers are inside the protected region and installed before work.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            signal.signal(sig, shutdown)
        threading.Thread(target=reader, daemon=True).start()
        for _ in range(CONCURRENCY):
            threading.Thread(target=worker, daemon=True).start()
        outstanding = 0
        while not closing.is_set():
            kind, value = events.get()
            if kind == "input":
                if outstanding >= MAX_PENDING + CONCURRENCY:
                    raise Shutdown()
                outstanding += 1
                jobs.put_nowait(value)
            elif kind == "result":
                outstanding -= 1
                write_frame(sys.stdout.buffer, value)
            else:
                model, ready, answer = value
                try:
                    with poc.request_scope(scope=children):
                        answer.append(runtime.ensure() if kind == "lease" else runtime.invalidate(model))
                except Exception as exc:
                    answer.append(exc)
                finally:
                    ready.set()
    except Shutdown:
        pass
    except BrokenPipeError:
        # Avoid a second buffered-stdout flush failure during interpreter exit.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
    finally:
        closing.set()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            signal.signal(sig, signal.SIG_IGN)
        try:
            children.close()
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
