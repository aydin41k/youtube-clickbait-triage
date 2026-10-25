import importlib.util
import io
import json
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import select
import threading
import unittest
from unittest.mock import patch, MagicMock

import native_host as host
import poc
from triage_core import Unavailable

REQUEST = {"version": 1, "op": "classify", "id": "one", "url": poc.DEMO_URL,
           "title": "Displayed  title ✨", "thumbnail": poc.DEMO_THUMB}
SCORES = {"irrelevant": 0, "clickbait": 51, "sensational": 49, "valid": 0}


def framed(value):
    data = json.dumps(value).encode()
    return struct.pack("=I", len(data)) + data


class ProtocolTests(unittest.TestCase):
    def test_frames_unicode_partial_reads_and_eof(self):
        class Partial(io.BytesIO):
            def read(self, n):
                return super().read(min(n, 2))
        self.assertEqual(host.read_frame(Partial(framed(REQUEST))), REQUEST)
        self.assertIsNone(host.read_frame(io.BytesIO()))
        output = io.BytesIO()
        host.write_frame(output, REQUEST)
        self.assertEqual(host.read_frame(io.BytesIO(output.getvalue())), REQUEST)
        for data in (b"x", struct.pack("=I", 0), struct.pack("=I", 32769), struct.pack("=I", 3) + b"x", struct.pack("=I", 1) + b"\xff", framed("not an object")):
            if data == framed("not an object"):
                with self.assertRaises(Unavailable):
                    host.validate_request(host.read_frame(io.BytesIO(data)))
            else:
                with self.assertRaises((ValueError, UnicodeError)):
                    host.read_frame(io.BytesIO(data))

    def test_request_validation_does_not_invoke_runtime(self):
        invalid = [{**REQUEST, "version": 2}, {**REQUEST, "version": True}, {**REQUEST, "op": "execute"},
                   {**REQUEST, "save_evidence": "out"}, {**REQUEST, "url": "https://evil.test/a"},
                   {**REQUEST, "url": REQUEST["url"] + "&list=abc"}, {**REQUEST, "title": " "},
                   {**REQUEST, "thumbnail": "/etc/passwd"}, {**REQUEST, "thumbnail": "https://i.ytimg.com:123/a"}]
        runtime = host.Runtime(MagicMock())
        classifier = MagicMock()
        for value in invalid:
            result = host.analyze(value, runtime, classifier)
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["scores"])
        classifier.assert_not_called()
        runtime.factory.assert_not_called()

    def test_bad_result_and_exception_do_not_poison_next_job(self):
        runtime = host.Runtime()
        classifier = MagicMock(side_effect=[RuntimeError("secret input"), {"status": "scored", "scores": {**SCORES, "valid": 1}},
                                           {"status": "scored", "scores": SCORES, "timings_ms": {"total": 2}}])
        self.assertEqual(host.analyze(REQUEST, runtime, classifier)["reason"], "unexpected_analysis_failure")
        self.assertIsNone(host.analyze(REQUEST, runtime, classifier)["scores"])
        result = host.analyze(REQUEST, runtime, classifier)
        self.assertEqual(result["scores"], SCORES)
        self.assertLess(len(json.dumps(result)), host.MAX_OUTPUT)
        self.assertEqual(classifier.call_args.args, (REQUEST["url"], REQUEST["title"], REQUEST["thumbnail"]))


class RuntimeTests(unittest.TestCase):
    def test_retrieval_exception_reaps_child_and_close_blocks_future_spawn_and_api(self):
        scope = poc.RetrievalChildren()
        child = MagicMock(pid=12345)
        child.poll.side_effect = [None, 0]
        child.communicate.side_effect = OSError("failed pipe")
        with patch("poc.subprocess.Popen", return_value=child), patch("poc.os.killpg") as kill:
            with self.assertRaises(OSError): scope.run(["fake"])
            kill.assert_called_once_with(12345, signal.SIGTERM)
        scope.close()
        with patch("poc.subprocess.Popen") as spawn, patch("poc.urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(Unavailable, "cancelled"): scope.run(["fake"])
            with poc.request_scope(scope=scope), self.assertRaisesRegex(Unavailable, "cancelled"):
                poc.api("/api/chat", {})
            spawn.assert_not_called(); opener.assert_not_called()

    def test_three_shared_leases_do_not_serialize_inference_or_share_metadata(self):
        entered = []
        barrier = threading.Barrier(3)
        model = {"name": "fake", "nested": []}
        @contextmanager
        def factory():
            entered.append("enter")
            try:
                yield model
            finally:
                entered.append("exit")
        runtime = host.Runtime(factory)
        results = []
        def run():
            with runtime.provide() as lease:
                import copy
                metadata = copy.deepcopy(lease)
                barrier.wait(timeout=3)
                metadata["nested"].append(threading.get_ident())
                results.append(metadata)
        workers = [threading.Thread(target=run) for _ in range(3)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=4)
        self.assertEqual(len(results), 3)
        self.assertEqual(entered, ["enter"])
        self.assertEqual(model["nested"], [])
        self.assertEqual(len({id(result["nested"]) for result in results}), 3)
        runtime.close()
        self.assertEqual(entered, ["enter", "exit"])
        with self.assertRaisesRegex(Unavailable, "cancelled"), runtime.provide():
            pass

    def test_closed_or_replaced_leases_never_borrow_a_new_owner(self):
        owner = MagicMock(); owner.poll.return_value = None
        replacement = MagicMock(); replacement.poll.return_value = None
        leases = [poc.OwnedRuntime({"name": "fake"}, owner) for _ in range(3)]
        with patch("poc.urllib.request.build_opener") as opener:
            for current in (None, replacement):
                with patch("poc._runtime_owner", current):
                    for lease in leases:
                        with poc.request_scope(lease=lease), self.assertRaisesRegex(Unavailable, "owner_exited"):
                            poc.api("/api/chat", {})
            with patch("poc._runtime_owner", owner):
                for lease in leases:
                    lease.closed = True
                    with poc.request_scope(lease=lease), self.assertRaisesRegex(Unavailable, "owner_exited"):
                        poc.api("/api/chat", {})
            opener.assert_not_called()

    def test_owned_runtime_overrides_ambient_parallelism_only_in_child(self):
        with tempfile.TemporaryDirectory() as temporary, patch("poc.ROOT", Path(temporary)):
            directory = Path(temporary) / ".runtime/bin"
            directory.mkdir(parents=True); (directory / "ollama").touch()
            child = MagicMock(pid=12345); child.poll.return_value = None
            responses = [{"version": "0.40.10"}, {"details": {"family": "gemma4"}, "capabilities": ["vision"]}, {"models": [{"name": poc.MODEL, "digest": "fake"}]}]
            with patch.dict(os.environ, {"OLLAMA_NUM_PARALLEL": "1"}), patch("poc.socket.socket") as probe, patch("poc.subprocess.Popen", return_value=child) as spawn, patch("poc.api", side_effect=responses), patch("poc.os.killpg"):
                probe.return_value.__enter__.return_value.connect_ex.return_value = 1
                with poc.local_server(): pass
                self.assertEqual(spawn.call_args.kwargs["env"]["OLLAMA_NUM_PARALLEL"], "3")
                self.assertEqual(os.environ["OLLAMA_NUM_PARALLEL"], "1")

    def test_two_analyses_share_lazy_runtime_and_independent_metadata(self):
        entered = []
        model = {"capabilities": ["vision"]}
        @contextmanager
        def factory():
            entered.append("enter")
            try:
                yield model
            finally:
                entered.append("exit")
        runtime = host.Runtime(factory)
        blocks = [{"id": "b1", "text": "words", "start": 0, "end": 10}]
        with patch("poc.thumbnail_input", return_value=(b"jpeg", {})), patch("poc.retrieve", return_value=(blocks, {}, {})), patch("poc.classify", return_value=({"status": "scored", "scores": SCORES}, {}, [])):
            first = poc.triage(REQUEST["url"], REQUEST["title"], REQUEST["thumbnail"], runtime_provider=runtime.provide)
            second = poc.triage(REQUEST["url"], REQUEST["title"], REQUEST["thumbnail"], runtime_provider=runtime.provide)
        self.assertEqual(entered, ["enter"])
        self.assertIsNot(first["model"], second["model"])
        self.assertNotIn("actual_request_count", model)
        runtime.close()
        self.assertEqual(entered, ["enter", "exit"])

    def test_missing_evidence_never_loads(self):
        factory = MagicMock()
        for exception in (Unavailable("no_english_captions"), Unavailable("transcript_too_large")):
            with patch("poc.thumbnail_input", return_value=(b"jpeg", {})), patch("poc.retrieve", side_effect=exception):
                self.assertEqual(poc.triage(poc.DEMO_URL, "Title", poc.DEMO_THUMB, runtime_provider=factory)["status"], "unavailable")
        factory.assert_not_called()
        with patch("poc.thumbnail_input", return_value=(b"jpeg", {})), patch("poc.retrieve", return_value=([{"text": "x" * 25000}], {}, {})):
            self.assertEqual(poc.triage(poc.DEMO_URL, "Title", poc.DEMO_THUMB, runtime_provider=factory)["status"], "unavailable")
        self.assertEqual(poc.triage(poc.DEMO_URL, " ", poc.DEMO_THUMB, runtime_provider=factory)["status"], "unavailable")
        factory.assert_not_called()

    def test_fresh_setup_and_lock_and_occupied_fail_before_spawn(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temporary, patch("poc.ROOT", Path(temporary)), patch("poc.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(Unavailable, "setup_required"), poc.local_server():
                pass
            runtime = Path(temporary) / ".runtime/bin"
            runtime.mkdir(parents=True)
            (runtime / "ollama").touch()
            with (runtime.parent / "ownership.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(Unavailable, "busy"), poc.local_server():
                    pass
            with patch("poc.socket.socket") as probe:
                probe.return_value.__enter__.return_value.connect_ex.return_value = 0
                with self.assertRaisesRegex(Unavailable, "occupied"), poc.local_server():
                    pass
            spawn.assert_not_called()
            self.assertFalse((runtime.parent / "server.log").exists())

    def test_readiness_version_vision_and_inference_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary, patch("poc.ROOT", Path(temporary)):
            runtime = Path(temporary) / ".runtime/bin"
            runtime.mkdir(parents=True)
            (runtime / "ollama").touch()
            valid_show = {"details": {"family": "gemma4"}, "capabilities": ["vision"]}
            cases = [({"version": "bad"}, valid_show, "unexpected_runtime_version"),
                     ({"version": "0.40.10"}, {"details": {"family": "gemma4"}, "capabilities": []}, "vision_unavailable"),
                     ({"version": "0.40.10"}, valid_show, "inference_error")]
            for version, show, reason in cases:
                process = MagicMock(pid=12345)
                process.poll.return_value = None
                with patch("poc.socket.socket") as probe, patch("poc.subprocess.Popen", return_value=process), patch("poc.os.killpg") as kill, patch("poc.api", side_effect=[version, show, {"models": [{"name": poc.MODEL, "digest": "test"}]}]):
                    probe.return_value.__enter__.return_value.connect_ex.return_value = 1
                    with self.assertRaisesRegex(Unavailable, reason), poc.local_server():
                        raise Unavailable("inference_error")
                    kill.assert_called_once_with(12345, signal.SIGTERM)
                    process.wait.assert_called_once()

    def test_exited_child_cannot_borrow_contender_readiness(self):
        with tempfile.TemporaryDirectory() as temporary, patch("poc.ROOT", Path(temporary)):
            runtime = Path(temporary) / ".runtime/bin"
            runtime.mkdir(parents=True)
            (runtime / "ollama").touch()
            process = MagicMock(pid=12345)
            process.poll.side_effect = [None, 1, 1, 1]
            with patch("poc.socket.socket") as probe, patch("poc.subprocess.Popen", return_value=process), patch("poc.api", return_value={"version": "0.40.10"}), patch("poc.time.sleep"), patch("poc.os.killpg") as kill:
                probe.return_value.__enter__.return_value.connect_ex.return_value = 1
                with self.assertRaisesRegex(Unavailable, "private_server_failed"), poc.local_server():
                    pass
                kill.assert_not_called()

    def test_readiness_timeout_reaps_owned_runtime(self):
        with tempfile.TemporaryDirectory() as temporary, patch("poc.ROOT", Path(temporary)):
            runtime = Path(temporary) / ".runtime/bin"
            runtime.mkdir(parents=True)
            (runtime / "ollama").touch()
            process = MagicMock(pid=12345)
            process.poll.return_value = None
            with patch("poc.socket.socket") as probe, patch("poc.subprocess.Popen", return_value=process), patch("poc.time.monotonic", side_effect=[0, 61]), patch("poc.os.killpg") as kill:
                probe.return_value.__enter__.return_value.connect_ex.return_value = 1
                with self.assertRaisesRegex(Unavailable, "readiness_timeout"), poc.local_server():
                    pass
                kill.assert_called_once_with(12345, signal.SIGTERM)
                process.wait.assert_called_once()

    def test_setup_lock_and_non_http_occupant_leave_logs_and_processes_untouched(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            script = scripts / "setup-local-ollama.sh"
            shutil.copy(poc.ROOT / "scripts/setup-local-ollama.sh", script)
            runtime = root / ".runtime/bin"
            runtime.mkdir(parents=True)
            executable = runtime / "ollama"
            executable.write_text("#!/bin/sh\nexit 99\n")
            executable.chmod(0o755)
            log = runtime.parent / "setup-server.log"
            log.write_text("untouched")
            with (runtime.parent / "ownership.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(["bash", str(script)], capture_output=True, timeout=3)
                self.assertIn(b"busy", result.stderr)
            with socket.socket() as occupant:
                try:
                    occupant.bind(("127.0.0.1", 11435))
                except OSError:
                    self.skipTest("private port already occupied; no takeover")
                occupant.listen()
                result = subprocess.run(["bash", str(script)], capture_output=True, timeout=3)
                self.assertIn(b"occupied", result.stderr)
                self.assertEqual(log.read_text(), "untouched")

    def test_reused_owner_death_rejects_matching_replacement_and_recovers_only_when_port_free(self):
        with tempfile.TemporaryDirectory() as temporary, patch("poc.ROOT", Path(temporary)):
            directory = Path(temporary) / ".runtime/bin"
            directory.mkdir(parents=True)
            (directory / "ollama").touch()
            first = MagicMock(pid=12345); first.poll.return_value = None
            recovered = MagicMock(pid=12346); recovered.poll.return_value = None
            requests = []
            def open_request(request, timeout):
                endpoint = request.full_url.rsplit("/", 1)[-1]
                requests.append(endpoint)
                values = {"version": {"version": "0.40.10"}, "show": {"details": {"family": "gemma4"}, "capabilities": ["vision"]},
                          "tags": {"models": [{"name": poc.MODEL, "digest": "same-version-replacement"}]}, "chat": {"ok": True}}
                return io.BytesIO(json.dumps(values[endpoint]).encode())
            def classify(*args):
                poc.api("/api/chat", {})
                return {"status": "scored", "scores": SCORES}, {}, []
            runtime = host.Runtime(poc.local_server)
            with patch("poc.socket.socket") as probe, patch("poc.subprocess.Popen", side_effect=[first, recovered]) as spawn, patch("poc.os.killpg") as kill, patch("poc.urllib.request.build_opener") as opener, patch("poc.thumbnail_input", return_value=(b"jpeg", {})), patch("poc.retrieve", return_value=([{"text": "words"}], {}, {})), patch("poc.classify", side_effect=classify):
                socket_probe = probe.return_value.__enter__.return_value
                socket_probe.connect_ex.return_value = 1
                opener.return_value.open.side_effect = open_request
                self.assertEqual(host.analyze(REQUEST, runtime)["status"], "scored")
                count = len(requests)
                first.poll.return_value = 1
                socket_probe.connect_ex.return_value = 0  # A matching replacement now owns the port.
                result = host.analyze(REQUEST, runtime)
                self.assertEqual(result["reason"], "private_runtime_owner_exited")
                self.assertIsNone(result["scores"])
                self.assertIsNone(runtime.model)
                self.assertEqual(len(requests), count)
                result = host.analyze(REQUEST, runtime)
                self.assertEqual(result["reason"], "private_port_occupied")
                self.assertEqual(len(requests), count)
                self.assertEqual(spawn.call_count, 1)
                kill.assert_not_called()
                socket_probe.connect_ex.return_value = 1
                self.assertEqual(host.analyze(REQUEST, runtime)["status"], "scored")
                self.assertEqual(spawn.call_count, 2)
                runtime.close()
                kill.assert_called_once_with(12346, signal.SIGTERM)

    def test_api_checks_owner_before_each_request_and_after_response(self):
        owner = MagicMock()
        owner.poll.return_value = None
        with patch("poc._runtime_owner", owner), patch("poc.urllib.request.build_opener") as opener:
            def replacement_response(*args, **kwargs):
                owner.poll.return_value = 1
                return io.BytesIO(b'{"same_version":true}')
            opener.return_value.open.side_effect = replacement_response
            with self.assertRaisesRegex(Unavailable, "owner_exited"):
                poc.api("/api/chat", {})
            with self.assertRaisesRegex(Unavailable, "owner_exited"):
                poc.api("/api/chat", {})
            opener.return_value.open.assert_called_once()


class ProcessTests(unittest.TestCase):
    def test_three_resistant_retrieval_children_are_reaped_on_disconnect_or_signals(self):
        for terminate in (False, True, "both"):
            with self.subTest(terminate=terminate), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                child_code = f'''import os,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
open({str(directory)!r}+"/"+str(os.getpid()),"w").write("ready")
time.sleep(30)
'''
                code = f'''import native_host,poc,subprocess,sys
original=subprocess.Popen
def spawn(*args,**kwargs):
 return original([sys.executable,"-c",{child_code!r}],**kwargs)
poc.subprocess.Popen=spawn
def classify(url,title,thumbnail,runtime_provider):
 poc.retrieve(url,title)
 raise AssertionError("cancelled retrieval must not start inference")
native_host.main(classify)
'''
                sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
                process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                owned = []
                try:
                    for n in range(3): process.stdin.write(framed({**REQUEST, "id": str(n)}))
                    process.stdin.flush()
                    deadline = time.monotonic() + 5
                    while len(list(directory.iterdir())) < 3 and time.monotonic() < deadline: time.sleep(0.01)
                    owned = [int(path.name) for path in directory.iterdir()]
                    self.assertEqual(len(owned), 3)
                    start = time.monotonic()
                    if terminate: process.terminate()
                    if not terminate or terminate == "both": process.stdin.close()
                    process.wait(timeout=3)
                    self.assertLess(time.monotonic() - start, 2.5, "three resistant children share one grace period")
                    for pid in owned:
                        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                    self.assertIsNone(sentinel.poll())
                    self.assertEqual(process.stdout.read(), b"")
                    self.assertEqual(process.stderr.read(), b"")
                finally:
                    if process.poll() is None: process.kill(); process.wait()
                    sentinel.terminate(); sentinel.wait()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if not stream.closed: stream.close()

    def test_shutdown_inside_runtime_spawn_registration_reaps_owned_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "owned.pid"
            code = f'''import native_host,poc,subprocess,sys,os,signal
from pathlib import Path
from unittest.mock import patch
poc.ROOT=Path({temporary!r})
(poc.ROOT/".runtime/bin").mkdir(parents=True)
(poc.ROOT/".runtime/bin/ollama").touch()
original=subprocess.Popen
def spawn(*args,**kwargs):
 child=original([sys.executable,"-c","import time;time.sleep(30)"],**kwargs)
 open({str(marker)!r},"w").write(str(child.pid))
 os.kill(os.getpid(),signal.SIGTERM)
 return child
poc.subprocess.Popen=spawn
def classify(*args,runtime_provider):
 with runtime_provider(): raise AssertionError("closed startup cannot yield")
with patch("poc.socket.socket") as probe:
 probe.return_value.__enter__.return_value.connect_ex.return_value=1
 native_host.main(classify)
'''
            process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for n in range(3): process.stdin.write(framed({**REQUEST, "id": str(n)}))
                process.stdin.flush(); process.wait(timeout=5)
                self.assertTrue(marker.exists())
                with self.assertRaises(ProcessLookupError): os.kill(int(marker.read_text()), 0)
                self.assertEqual(process.stdout.read(), b""); self.assertEqual(process.stderr.read(), b"")
            finally:
                if process.poll() is None: process.kill(); process.wait()
                for stream in (process.stdin, process.stdout, process.stderr): stream.close()

    def test_one_worker_failure_isolated_from_other_framed_replies(self):
        code = '''import native_host
def classify(url,title,thumbnail,runtime_provider):
 if title=="failure": raise RuntimeError("untrusted content")
 return {"status":"scored","scores":{"irrelevant":0,"clickbait":51,"sensational":49,"valid":0}}
native_host.main(classify)
'''
        process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            for n in range(3): process.stdin.write(framed({**REQUEST, "id": str(n), "title": "failure" if n == 0 else "ok"}))
            process.stdin.flush()
            replies = []
            for _ in range(3):
                self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                replies.append(host.read_frame(process.stdout))
            self.assertEqual(sum(reply["status"] == "scored" for reply in replies), 2)
            self.assertEqual(next(reply for reply in replies if reply["id"] == "0")["reason"], "unexpected_analysis_failure")
        finally:
            process.stdin.close(); process.wait(timeout=5)
            self.assertEqual(process.stderr.read(), b"")
            process.stdout.close(); process.stderr.close()

    def test_three_framed_jobs_overlap_and_fourth_waits_with_out_of_order_ids(self):
        # stdout frames are the barrier notifications, not timing-based throughput guesses.
        code = '''import native_host,threading
from contextlib import contextmanager
barrier=threading.Barrier(3)
release=threading.Event()
lock=threading.Lock()
active=0
finished=0
starts=0
@contextmanager
def runtime():
 global starts
 starts+=1
 assert starts==1
 yield {}
def classify(url,title,thumbnail,runtime_provider):
 global active,finished
 with lock:
  active+=1
  assert active<=3
  if title=="fourth": assert finished>=1
 with runtime_provider():
  if title != "fourth":
   barrier.wait(timeout=3)
   if title == "0": release.wait(timeout=3)
  else: release.set()
 with lock:
  active-=1
  finished+=1
 return {"status":"scored","scores":{"irrelevant":0,"clickbait":51,"sensational":49,"valid":0}}
native_host.main(classify,runtime)
'''
        process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        try:
            for n in range(4):
                process.stdin.write(framed({**REQUEST, "id": str(n), "title": str(n) if n < 3 else "fourth"}))
            process.stdin.flush()
            replies = []
            for _ in range(4):
                self.assertTrue(select.select([process.stdout], [], [], 5)[0], "framed response must arrive")
                replies.append(host.read_frame(process.stdout))
            self.assertEqual({reply["id"] for reply in replies}, {str(n) for n in range(4)})
            self.assertTrue(all(reply["status"] == "scored" for reply in replies))
            self.assertNotEqual(replies[0]["id"], "0")
        finally:
            process.stdin.close(); process.wait(timeout=5)
            self.assertEqual(process.stderr.read(), b"")
            process.stdout.close(); process.stderr.close()

    def test_eof_notification_targets_main_thread_to_interrupt_blocking_analysis(self):
        handlers={}
        def setup(sig,handler):
            if callable(handler):handlers[sig]=handler
        def deliver(ident,sig):handlers[sig]()
        class FakeThread:
            def __init__(self,*,target,daemon):self.target=target
            def start(self):self.target()
        with patch("native_host.signal.signal",side_effect=setup),patch("native_host.threading.Thread",FakeThread),patch("native_host.read_frame",return_value=None),patch("native_host.os.fdopen"),patch("native_host.os.kill") as process_signal,patch("native_host.signal.pthread_kill",side_effect=deliver) as thread_signal:
            host.main()
            thread_signal.assert_called_once_with(host.threading.main_thread().ident,signal.SIGUSR1)
            process_signal.assert_not_called()
    def test_repeated_shutdown_signal_handler_is_idempotent(self):
        handlers = {}
        interrupted = []
        def signal_setup(sig, handler):
            if callable(handler): handlers[sig] = handler
        class FakeThread:
            def __init__(self, *args, **kwargs): pass
            def start(self):
                try: handlers[signal.SIGTERM]()
                except host.Shutdown:
                    # Repeated EOF/termination arrives while the first exception unwinds.
                    handlers[signal.SIGUSR1](); handlers[signal.SIGTERM](); interrupted.append(True)
                    raise
        with patch("native_host.signal.signal",side_effect=signal_setup),patch("native_host.threading.Thread",FakeThread):
            host.main()
        self.assertEqual(interrupted,[True])
    def test_disconnect_and_sigterm_interrupt_long_job_only_clean_owned_child(self):
        for terminate in (False, True, "both"):
            with tempfile.TemporaryDirectory() as temporary:
                marker = Path(temporary) / "owned.pid"
                sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
                code = f'''import native_host,subprocess,sys,time,os,signal
from contextlib import contextmanager
@contextmanager
def runtime():
 p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],start_new_session=True)
 open({str(marker)!r},"w").write(str(p.pid))
 try: yield {{}}
 finally:
  os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=3)
def classify(*args,runtime_provider):
 with runtime_provider(): time.sleep(30)
native_host.main(classify,runtime)
'''
                process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    for n in range(3):
                        process.stdin.write(framed({**REQUEST, "id": str(n)}))
                    process.stdin.flush()
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists())
                    owned = int(marker.read_text())
                    if terminate:
                        process.terminate()
                    if not terminate or terminate == "both":
                        process.stdin.close()
                    process.wait(timeout=5)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(owned, 0)
                    self.assertIsNone(sentinel.poll())
                    self.assertEqual(process.stdout.read(), b"")
                    self.assertEqual(process.stderr.read(), b"")
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait()
                    sentinel.terminate(); sentinel.wait()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if not stream.closed: stream.close()

    def test_broken_pipe_unwinds_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "closed"
            code = f'''import native_host,time
from contextlib import contextmanager
@contextmanager
def runtime():
 try: yield {{}}
 finally: open({str(marker)!r},"w").write("closed")
def classify(*args,runtime_provider):
 with runtime_provider():
  time.sleep(0.2)
  return {{"status":"scored","scores":{SCORES!r}}}
native_host.main(classify,runtime)
'''
            process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                process.stdout.close()  # stdin stays open: only broken output triggers cleanup.
                process.stdin.write(framed(REQUEST)); process.stdin.flush()
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(marker.read_text(), "closed")
                self.assertEqual(process.stderr.read(), b"")
            finally:
                if process.poll() is None: process.kill(); process.wait()
                process.stdin.close(); process.stderr.close()


class InstallTests(unittest.TestCase):
    def test_exact_registration_idempotent_and_conflict_refused(self):
        spec = importlib.util.spec_from_file_location("installer", poc.ROOT / "scripts/install-native-host.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="triage config spaces ") as temporary:
            directory = Path(temporary)
            identifier = "a" * 32
            target = module.install(identifier, directory)
            manifest = json.loads(target.read_text())
            self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{identifier}/"])
            self.assertEqual(manifest["path"], str(poc.ROOT / "run-native-host"))
            self.assertEqual(module.install(identifier, directory), target)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                module.install("b" * 32, directory)
            with self.assertRaises(ValueError):
                module.install("bad", directory)
            self.assertEqual(len(list(directory.iterdir())), 1)
