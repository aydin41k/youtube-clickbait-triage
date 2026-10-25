import json
import io
import os
import hashlib
import subprocess
import unittest
from unittest.mock import patch

import poc
from triage_core import Unavailable, caption_candidates, select_captions

RAW = json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 100000, "segs": [{"utf8": "Actual complete English captions"}]}]}).encode()


def track(language="en", suffix="manual"):
    return {"ext": "json3", "url": f"https://www.youtube.com/api/timedtext?lang={language}&name={suffix}"}


def metadata(manual=None, automatic=None):
    return {"id": "GlaSnROMqRY", "duration": 100, "subtitles": manual or {}, "automatic_captions": automatic or {}}


def extracted(info):
    return subprocess.CompletedProcess(["fake"], 0, json.dumps(info).encode(), b"")


class RetrievalRegressions(unittest.TestCase):
    def test_regional_original_english_is_retrieved(self):
        with patch("poc.subprocess.run", return_value=extracted(metadata({"en-AU": [track("en-AU")]}))), patch("poc.download", return_value=RAW):
            blocks, coverage, provenance = poc.retrieve(poc.DEMO_URL, "Displayed title")
        self.assertEqual(provenance["caption_language"], "en-AU")
        self.assertFalse(coverage["incomplete"])
        self.assertEqual(len(blocks), 1)

    def test_failed_preferred_track_falls_back_to_complete_auto(self):
        info = metadata({"en": [track()]}, {"en-orig": [track(suffix="auto")]})
        with patch("poc.subprocess.run", return_value=extracted(info)), patch("poc.download", side_effect=[Unavailable("retrieval_error"), RAW]) as download:
            _, coverage, provenance = poc.retrieve(poc.DEMO_URL, "Displayed title")
        self.assertEqual(download.call_count, 2)
        self.assertEqual(provenance["caption_kind"], "automatic")
        self.assertFalse(coverage["incomplete"])

    def test_invalid_empty_and_partial_candidates_fall_back_without_combining(self):
        bad_values = [b"not json", b'{"events":[]}', json.dumps({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "partial"}]}]}).encode()]
        for bad in bad_values:
            with self.subTest(bad=bad), patch("poc.subprocess.run", return_value=extracted(metadata({"en": [track()]}, {"en-orig": [track(suffix="auto")]}))), patch("poc.download", side_effect=[bad, RAW]):
                blocks, coverage, provenance = poc.retrieve(poc.DEMO_URL, "Displayed title")
            self.assertEqual(blocks[0]["text"], "Actual complete English captions")
            self.assertEqual(provenance["caption_sha256"], hashlib.sha256(RAW).hexdigest())
            self.assertEqual(provenance["caption_kind"], "automatic")
            self.assertFalse(coverage["incomplete"])

    def test_all_bad_evidence_abstains_without_relabeling_as_missing(self):
        for raw, reason in [(b"bad", "invalid_caption_track"), (b'{"events":[]}', "empty_captions"), (b'{"events":[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"partial"}]}]}', "partial_captions")]:
            with self.subTest(reason=reason), patch("poc.subprocess.run", return_value=extracted(metadata({"en": [track()]}))) as extraction, patch("poc.download", return_value=raw) as download:
                with self.assertRaisesRegex(Unavailable, reason): poc.retrieve(poc.DEMO_URL, "Title")
                self.assertEqual(extraction.call_count, 2); self.assertEqual(download.call_count, 1)

    def test_four_downloads_and_two_metadata_extractions_are_hard_bounds(self):
        info = metadata({"en": [track(suffix=str(n)) for n in range(8)]})
        with patch("poc.subprocess.run", return_value=extracted(info)) as extraction, patch("poc.download", side_effect=Unavailable("retrieval_error")) as download:
            with self.assertRaisesRegex(Unavailable, "retrieval_error"): poc.retrieve(poc.DEMO_URL, "Title")
        self.assertEqual(extraction.call_count, 2); self.assertEqual(download.call_count, 4)

    def test_refresh_recovers_a_blocked_expired_url_and_records_actual_winner(self):
        with patch("poc.subprocess.run", side_effect=[extracted(metadata({"en": [track(suffix="old")]})), extracted(metadata({"en-AU": [track("en-AU", "fresh")]}))]) as extraction, patch("poc.download", side_effect=[poc.RetrievalHTTPError(403), RAW]):
            _, _, provenance = poc.retrieve(poc.DEMO_URL, "Title")
        self.assertEqual(provenance["caption_language"], "en-AU")
        self.assertEqual(provenance["metadata_attempts"], 2)
        self.assertEqual(provenance["caption_download_attempts"], 2)
        self.assertIn("--no-remote-components", extraction.call_args.args[0])
        self.assertIn("--js-runtimes", extraction.call_args.args[0])

    def test_429_backoff_retries_same_url_once_then_stops_not_cross_candidate(self):
        waits = []
        current = [0]
        def wait(seconds): waits.append(seconds); current[0] += seconds
        info = metadata({"en": [track(suffix="one"), track(suffix="two")]})
        with patch("poc.subprocess.run", return_value=extracted(info)) as extraction, patch("poc.download", side_effect=poc.RetrievalHTTPError(429, 3)) as download:
            with self.assertRaisesRegex(Unavailable, "caption_rate_limited"):
                poc.retrieve(poc.DEMO_URL, "Title", clock=lambda: current[0], wait=wait)
        self.assertEqual(waits, [3]); self.assertEqual(download.call_count, 2); self.assertEqual(extraction.call_count, 1)
        self.assertEqual(download.call_args_list[0].args[0], download.call_args_list[1].args[0])

    def test_large_retry_after_stops_immediately_with_honest_rate_limit(self):
        with patch("poc.subprocess.run", return_value=extracted(metadata({"en": [track()]}))) as extraction, patch("poc.download", side_effect=poc.RetrievalHTTPError(429, 60)) as download, patch("poc.time.sleep") as wait:
            with self.assertRaisesRegex(Unavailable, "caption_rate_limited"): poc.retrieve(poc.DEMO_URL, "Title")
        wait.assert_not_called(); self.assertEqual(download.call_count, 1); self.assertEqual(extraction.call_count, 1)

    def test_transient_503_can_recover_within_shared_download_budget(self):
        with patch("poc.subprocess.run", return_value=extracted(metadata({"en": [track()]}))), patch("poc.download", side_effect=[poc.RetrievalHTTPError(503, 2), RAW]), patch("poc.time.sleep") as wait:
            _, _, provenance = poc.retrieve(poc.DEMO_URL, "Title")
        wait.assert_called_once_with(2); self.assertEqual(provenance["caption_download_attempts"], 2)

    def test_one_track_transport_timeout_retries_and_recovers_without_metadata_refresh(self):
        with patch("poc.subprocess.run", return_value=extracted(metadata({"en": [track()]}))) as extraction, patch("poc.download", side_effect=[poc.RetrievalNetworkError("TimeoutError"), RAW]) as download, patch("poc.time.sleep") as wait:
            _, _, provenance = poc.retrieve(poc.DEMO_URL, "Title")
        self.assertEqual(extraction.call_count, 1); self.assertEqual(download.call_count, 2)
        self.assertEqual(provenance["caption_download_attempts"], 2); wait.assert_called_once_with(1)

    def test_cancellation_during_backoff_starts_no_more_fetches_or_metadata(self):
        scope = poc.RetrievalChildren()
        with poc.request_scope(scope=scope), patch.object(scope, "run", return_value=extracted(metadata({"en": [track()]}))) as extraction, patch("poc.download", side_effect=poc.RetrievalHTTPError(429, 2)) as download:
            with self.assertRaisesRegex(Unavailable, "analysis_cancelled"):
                poc.retrieve(poc.DEMO_URL, "Title", wait=lambda _: scope.close())
        self.assertEqual(download.call_count, 1); self.assertEqual(extraction.call_count, 1)

    def test_deadline_is_not_reset_on_metadata_refresh(self):
        now = [0]
        def download(*args, **kwargs):
            now[0] = 121
            raise Unavailable("retrieval_error")
        with patch("poc.subprocess.run", return_value=extracted(metadata({"en": [track(), track(suffix="two")]}))) as extraction, patch("poc.download", side_effect=download):
            with self.assertRaisesRegex(Unavailable, "caption_retrieval_deadline"):
                poc.retrieve(poc.DEMO_URL, "Title", clock=lambda: now[0])
        self.assertEqual(extraction.call_count, 1)

    def test_exit_zero_po_token_warning_is_access_failure_not_caption_absence(self):
        result = extracted(metadata()); result.stderr = b"WARNING: caption languages withheld; missing PO Token"
        with patch("poc.subprocess.run", return_value=result) as extraction, patch("poc.download") as download:
            with self.assertRaisesRegex(Unavailable, "caption_access_blocked"): poc.retrieve(poc.DEMO_URL, "Title")
        self.assertEqual(extraction.call_count, 2); download.assert_not_called()

    def test_exit_zero_known_extraction_warning_is_not_proven_caption_absence(self):
        result = extracted(metadata()); result.stderr = b"WARNING: Unable to extract player response"
        with patch("poc.subprocess.run", return_value=result) as extraction:
            with self.assertRaisesRegex(Unavailable, "youtube_extraction_incomplete"): poc.retrieve(poc.DEMO_URL, "Title")
        self.assertEqual(extraction.call_count, 2)

    def test_private_access_and_malformed_metadata_are_not_retried_as_absence(self):
        for result, reason in [(subprocess.CompletedProcess([], 1, b"", b"Private video; sign in"), "youtube_access_blocked"), (extracted({"subtitles": []}), "unsupported_video"), (extracted({**metadata(), "subtitles": ["malformed"]}), "invalid_youtube_metadata")]:
            with self.subTest(reason=reason), patch("poc.subprocess.run", return_value=result) as extraction:
                with self.assertRaisesRegex(Unavailable, reason): poc.retrieve(poc.DEMO_URL, "Title")
                self.assertEqual(extraction.call_count, 1)

    def test_optional_node_missing_does_not_prevent_successful_jsless_retrieval(self):
        with patch("poc.resolve_node", return_value=None), patch("poc.subprocess.run", return_value=extracted(metadata({"en": [track()]}))), patch("poc.download", return_value=RAW):
            _, _, provenance = poc.retrieve(poc.DEMO_URL, "Title")
        self.assertEqual(provenance["metadata_attempts"], 1)


class CandidateTests(unittest.TestCase):
    def test_regional_script_aliases_manual_order_and_exact_url_deduplication(self):
        one = track("en-AU", "same")
        candidates = caption_candidates(metadata({"en-AU": [one], "en-Latn-US": [track("en-Latn-US")], "en": [track()]}, {"en-orig": [one, track(suffix="auto")]}))
        self.assertEqual([candidate[0] for candidate in candidates], ["manual", "manual", "manual", "automatic"])
        self.assertEqual(candidates[0][1], "en")
        self.assertEqual(len({candidate[2]["url"] for candidate in candidates}), 4)
        self.assertEqual(select_captions(metadata({"en-AU": [one]}))[1], "en-AU")

    def test_translation_wrong_source_language_unsafe_urls_and_malformed_entries_ignored(self):
        entries = [track("es"), {"ext": "json3", "url": track()["url"] + "&tlang=en"}, {"ext": "json3", "url": "https://evil.test/api/timedtext?lang=en"}, {"ext": "json3", "url": "https://www.youtube.com:999/api/timedtext?lang=en"}, "bad", {"ext": "vtt", "url": track()["url"]}]
        self.assertEqual(caption_candidates(metadata({"en": entries}, {"es": [track()]})), [])

    def test_missing_url_lang_is_allowed_only_for_english_metadata_key(self):
        entry = {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?v=GlaSnROMqRY"}
        self.assertEqual(len(caption_candidates(metadata({"en-AU": [entry], "es": [entry]}))), 1)


class RuntimeSetupTests(unittest.TestCase):
    def test_transport_exception_is_typed_for_retry_not_invalid_evidence(self):
        with patch("poc.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("untrusted network detail")
            with self.assertRaises(poc.RetrievalNetworkError) as caught: poc.download(track()["url"], 100, poc.caption_url)
        self.assertEqual(caught.exception.error_type, "TimeoutError")
        self.assertEqual(str(caught.exception), "retrieval_error")

    def test_thumbnail_http_failures_are_not_mislabeled_as_caption_failures(self):
        for status, reason in [(403, "thumbnail_access_blocked"), (429, "thumbnail_rate_limited"), (503, "thumbnail_http_error")]:
            with self.subTest(status=status), patch("poc.download", side_effect=poc.RetrievalHTTPError(status)):
                with self.assertRaisesRegex(Unavailable, f"^{reason}$"): poc.thumbnail_input(poc.DEMO_THUMB)

    def test_rate_limit_detection_uses_http_error_phrase_not_signed_url_digits(self):
        self.assertEqual(poc.extractor_reason(b"WARNING: https://youtube.com/api/timedtext?signature=abc429xyz"), "youtube_retrieval_failed")
        self.assertEqual(poc.extractor_reason(b"ERROR: HTTP Error 429: Too Many Requests"), "caption_rate_limited")

    def test_gui_minimal_path_resolves_standard_absolute_node_without_profiles(self):
        with patch.dict(os.environ, {"PATH": "/minimal/gui/path"}, clear=True), patch("poc.shutil.which", return_value=None), patch("poc.os.path.isfile", side_effect=lambda path: path == "/usr/bin/node"), patch("poc.os.access", return_value=True), patch("poc.os.path.realpath", side_effect=lambda path: path):
            self.assertEqual(poc.resolve_node(), "/usr/bin/node")
        command = poc.metadata_command(poc.DEMO_URL, 90, "/usr/bin/node")
        self.assertIn("node:/usr/bin/node", command)
        for flag in ("--no-remote-components", "--no-js-runtimes", "--ignore-config", "--no-cache-dir", "--skip-download"):
            self.assertIn(flag, command)

    def test_invalid_node_override_is_not_executed_or_sourced(self):
        with patch.dict(os.environ, {"TRIAGE_NODE": "relative/not-node"}), patch("poc.subprocess.run") as execute:
            self.assertIsNone(poc.resolve_node())
        execute.assert_not_called()

    def test_http_status_retry_after_preserved_without_forwarding_raw_response(self):
        error = poc.urllib.error.HTTPError("https://www.youtube.com/api/timedtext?secret=signed", 429, "secret detail", {"Retry-After": "3"}, io.BytesIO(b"untrusted text"))
        with patch("poc.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(poc.RetrievalHTTPError) as caught:
                poc.download(track()["url"], 100, poc.caption_url)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 3)
        self.assertEqual(str(caught.exception), "caption_rate_limited")
