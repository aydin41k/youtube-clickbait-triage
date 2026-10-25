import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

import poc
from triage_core import (Unavailable, MODEL, MAX_CHUNKS, canonical_url, safe_thumbnail_url,
                         select_captions, normalize_captions, caption_chunks, ensure_context,
                         combine_extractions, validate_audit, validate_verdict, resolve_promises, scoring_ready)


def track(events=10, duration=100, text="spoken words"):
    return {"events": [{"tStartMs": i * duration / events * 1000, "dDurationMs": duration / events * 1000,
                        "segs": [{"utf8": text + str(i)}]} for i in range(events)]}


def extractions(promises):
    thumbnail = {"visible_text": "DOAC", "framing": "Brand logo", "claim_assessment": "no_material_claims", "assessment_reason": "Branding only", "promises": [], "extraction_complete": True}
    title = {"promises": [{"claim": promise["claim"], "delivery_evidence": "requires_audiovisual" if promise["requires_av"] else "caption_content" if promise["absence_checkable_from_captions"] else "caption_support_only"} for promise in promises], "extraction_complete": True}
    return thumbnail, title


class InputTests(unittest.TestCase):
    def test_demo_cannot_silently_ignore_explicit_input(self):
        with patch("sys.argv", ["poc.py", "--demo", "--title", "counterfactual"]), patch("sys.stderr", new_callable=io.StringIO) as error, self.assertRaises(SystemExit) as caught:
            poc.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("cannot be mixed", error.getvalue())

    def test_urls(self):
        for url in ("https://www.youtube.com/watch?v=GlaSnROMqRY&t=30", "https://youtu.be/GlaSnROMqRY", "https://m.youtube.com/shorts/GlaSnROMqRY"):
            self.assertEqual(canonical_url(url), (poc.DEMO_URL, "GlaSnROMqRY"))
        for url in ("http://youtu.be/GlaSnROMqRY", "https://evil.test/watch?v=GlaSnROMqRY", "https://youtube.com@evil.test/watch?v=GlaSnROMqRY", "https://youtube.com:123/watch?v=GlaSnROMqRY", "https://youtu.be/GlaSnROMqRY/extra", "https://youtube.com/watch?v=GlaSnROMqRY&list=abc", "https://youtube.com/watch?v=GlaSnROMqRY&v=AAAAAAAAAAA", "https://youtube.com/watch?v=$(whoami)"):
            with self.subTest(url=url), self.assertRaises(Unavailable):
                canonical_url(url)

    def test_thumbnail_url(self):
        self.assertEqual(safe_thumbnail_url(poc.DEMO_THUMB), poc.DEMO_THUMB)
        for url in ("file:///etc/passwd", "https://127.0.0.1/a", "https://i.ytimg.com.evil.test/a", "https://user:pass@i.ytimg.com/a", "https://i.ytimg.com:123/a"):
            with self.assertRaises(Unavailable):
                safe_thumbnail_url(url)
        with self.assertRaises(Unavailable):
            poc.RestrictedRedirect(safe_thumbnail_url).redirect_request(None, None, 302, "", {}, "https://evil.test/a")

    def test_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            image = Image.new("RGB", (40, 20), "red")
            image.save(path)
            data, info = poc.thumbnail_input(str(path))
            self.assertTrue(data.startswith(b"\xff\xd8"))
            self.assertEqual(info["submitted_dimensions"], [40, 20])
            path.write_bytes(b"not an image")
            with self.assertRaises(Unavailable):
                poc.thumbnail_input(str(path))
            with self.assertRaises(Unavailable):
                poc.thumbnail_input(directory)
        fake = io.BytesIO()
        Image.new("RGB", (3000, 3000)).save(fake, "PNG")
        with patch("poc.download", return_value=fake.getvalue()), self.assertRaises(Unavailable):
            poc.thumbnail_input(poc.DEMO_THUMB)

    def test_manual_preferred_auto_missing(self):
        english = [{"ext": "json3", "url": "https://youtube.com/api/timedtext"}]
        self.assertEqual(select_captions({"subtitles": {"en": english}, "automatic_captions": {"en": english}})[0], "manual")
        self.assertEqual(select_captions({"automatic_captions": {"en-orig": english}})[:2], ("automatic", "en-orig"))
        with self.assertRaises(Unavailable):
            select_captions({"subtitles": {"fr": english}})
        translated = [{"ext": "json3", "url": "https://youtube.com/api/timedtext?lang=fr&tlang=en"}]
        original = [{"ext": "json3", "url": "https://youtube.com/api/timedtext?lang=en"}]
        self.assertEqual(select_captions({"automatic_captions": {"en": translated, "en-orig": original}})[1], "en-orig")
        for source in (translated, [{"ext": "json3", "url": "https://youtube.com/api/timedtext?lang=fr"}]):
            with self.assertRaises(Unavailable):
                select_captions({"automatic_captions": {"en": source}})


class CaptionTests(unittest.TestCase):
    def test_preservation_and_coverage(self):
        raw = track(100, 1000)
        blocks, coverage = normalize_captions(raw, 1000)
        self.assertEqual(coverage["retrieved_events"], 100)
        self.assertEqual(coverage["caption_interval_coverage"], 1)
        self.assertEqual(" ".join(block["text"] for block in blocks), " ".join(event["segs"][0]["utf8"] for event in raw["events"]))
        self.assertIn("spoken words99", blocks[-1]["text"])
        self.assertFalse(coverage["incomplete"])
        self.assertEqual([block for chunk in caption_chunks(blocks) for block in chunk], blocks)

    def test_partial_and_unordered(self):
        for raw in ({"events": []}, track(2, 20), {"events": [track(10)["events"][-1], track(10)["events"][0]]}):
            with self.assertRaises(Unavailable):
                normalize_captions(raw, 100)
        raw = track(100, 1000)
        raw["events"] = raw["events"][:20] + raw["events"][40:]
        with self.assertRaisesRegex(Unavailable, "partial"):
            normalize_captions(raw, 1000)

    def test_chunks_limits_never_omit_tail(self):
        blocks = [{"id": f"b{i}", "start": i, "end": i + 1, "text": "x" * 9000, "events": 1} for i in range(12)]
        self.assertEqual(len(caption_chunks(blocks)), MAX_CHUNKS)
        self.assertEqual(caption_chunks(blocks)[-1][-1], blocks[-1])
        with self.assertRaisesRegex(Unavailable, "too_large"):
            caption_chunks(blocks + [blocks[-1]])
        with self.assertRaises(Unavailable):
            caption_chunks([{**blocks[0], "text": "x" * 21000}])
        with self.assertRaises(Unavailable):
            ensure_context([{"role": "user", "content": "é" * 20000}])


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.blocks = [{"id": "b0001", "text": "The doctor described dreams in the final weeks.", "start": 1.0, "end": 11.0}]
        self.promises = [{"id": "p1", "claim": "dreams", "source": "both", "requires_av": False, "absence_checkable_from_captions": False}]
        self.raw_audit = {"claims": [{"promise_id": "p1", "finding": "support", "note": "Explicit discussion.", "evidence_ids": ["b0001"]}]}
        self.audit = validate_audit(self.raw_audit, self.promises, self.blocks)
        self.coverage = {"incomplete": False, "processed_blocks": 1, "total_blocks": 1, "processed_chunks": 1, "total_chunks": 1}
        self.verdict = {"dominant_class": "sensational", "scores": {"irrelevant": 0, "clickbait": 20, "sensational": 50, "valid": 30}, "reason": "Packaging estimate.", "evidence": [{"promise_id": "p1", "evidence_id": "b0001"}]}

    def check_verdict(self, value, audits, coverage, promises=None):
        if promises is None:
            promises = [{"id": claim["promise_id"], "requires_av": False, "absence_checkable_from_captions": False} for claim in audits[0]["claims"]] if audits else self.promises
        return validate_verdict(value, audits, coverage, promises, self.blocks)

    def test_good_and_unavailable(self):
        validate_audit(self.raw_audit, self.promises, self.blocks)
        self.assertEqual(self.check_verdict(self.verdict, [self.audit], self.coverage)["evidence"][0]["quote"], self.blocks[0]["text"])
        unavailable = {"status": "unavailable", "scores": None, "reason": "Missing relevant evidence.", "evidence": []}
        with self.assertRaises(Unavailable):
            self.check_verdict(unavailable, [], self.coverage)

    def test_bad_scores(self):
        for scores in ({"irrelevant": 0, "clickbait": True, "sensational": 50, "valid": 49},
                       {"irrelevant": 0, "clickbait": 1.0, "sensational": 50, "valid": 49},
                       {"irrelevant": 0, "clickbait": -1, "sensational": 50, "valid": 51},
                       {"irrelevant": 101, "clickbait": 0, "sensational": 0, "valid": -1},
                       {"irrelevant": 0, "clickbait": 1, "sensational": 1, "valid": 1},
                       {"irrelevant": 0, "clickbait": 1, "sensational": 1, "valid": 98, "unknown": 0},
                       {"clickbait": 20, "sensational": 50, "valid": 30},
                       {"irrelevant": 0, "clickbait": 50, "sensational": 50},
                       {"irrelevant": False, "clickbait": 20, "sensational": 50, "valid": 30}, None):
            with self.subTest(scores=scores), self.assertRaises(Unavailable):
                self.check_verdict({**self.verdict, "scores": scores}, [self.audit], self.coverage)
        with self.assertRaises(Unavailable):
            self.check_verdict({**self.verdict, "status": "unavailable"}, [self.audit], self.coverage)

    def test_valid_class_ties_missing_class_and_declared_number_conflicts(self):
        value = {**self.verdict, "dominant_class": "valid", "scores": {"irrelevant": 0, "clickbait": 10, "sensational": 20, "valid": 70}}
        self.assertEqual(self.check_verdict(value, [self.audit], self.coverage)["dominant_class"], "valid")
        self.check_verdict({**value, "scores": {"irrelevant": 0, "clickbait": 0, "sensational": 50, "valid": 50}}, [self.audit], self.coverage)
        for bad in ({**value, "dominant_class": "sensational"}, {key: part for key, part in value.items() if key != "dominant_class"}, {**value, "dominant_class": True}):
            with self.assertRaises(Unavailable):
                self.check_verdict(bad, [self.audit], self.coverage)

    def test_bad_evidence_and_malformed_types(self):
        malformed = copy.deepcopy(self.raw_audit)
        malformed["claims"].append(None)
        with self.assertRaises(Unavailable):
            validate_audit(malformed, self.promises, self.blocks)
        for coverage in ({**self.coverage, "processed_blocks": 0}, {**self.coverage, "incomplete": True}):
            with self.assertRaises(Unavailable):
                self.check_verdict(self.verdict, [self.audit], coverage)
        with self.assertRaises(Unavailable):
            self.check_verdict({**self.verdict, "evidence": [{"promise_id": [], "evidence_id": "b0001"}]}, [self.audit], self.coverage)

    def test_every_material_promise_must_be_resolved_over_whole_track(self):
        for finding in ("unassessable", "not_observed"):
            mixed = copy.deepcopy(self.audit)
            mixed["claims"].append({"promise_id": "p2", "finding": finding, "note": "No usable evidence", "evidence_ids": []})
            with self.assertRaisesRegex(Unavailable, "(unassessable|unresolved)_material"):
                self.check_verdict(self.verdict, [mixed], self.coverage)
        first = copy.deepcopy(self.audit)
        first["claims"][0].update(finding="not_observed", note="Not in this chunk", evidence_ids=[])
        first["sources"] = {}
        self.assertEqual(self.check_verdict(self.verdict, [first, self.audit], {**self.coverage, "total_chunks": 2, "processed_chunks": 2})["scores"], self.verdict["scores"])

    def test_complete_caption_absence_is_scoreable_but_never_fabricated(self):
        absent = {"claims": [{"promise_id": "p1", "finding": "not_observed", "note": "No promised topic", "evidence_ids": []}], "sources": {}}
        promises = [{**self.promises[0], "absence_checkable_from_captions": True}]
        verdict = {**self.verdict, "dominant_class": "clickbait", "scores": {"irrelevant": 0, "clickbait": 80, "sensational": 15, "valid": 5}, "evidence": []}
        resolved = resolve_promises(promises, [absent], self.coverage)
        self.assertEqual(resolved, [{"promise_id": "p1", "kind": "not_observed_in_complete_captions", "audited_chunks": 1, "total_chunks": 1}])
        self.check_verdict(verdict, [absent], self.coverage, promises)
        for coverage in ({**self.coverage, "incomplete": True}, {**self.coverage, "processed_chunks": 0}, {**self.coverage, "total_chunks": 2}, {**self.coverage, "processed_blocks": 0}, {**self.coverage, "processed_chunks": True}):
            with self.assertRaises(Unavailable):
                self.check_verdict(verdict, [absent], coverage, promises)
        with self.assertRaisesRegex(Unavailable, "inconsistent_model"):
            self.check_verdict({**verdict, "scores": {"irrelevant": 0, "clickbait": 10, "sensational": 20, "valid": 70}}, [absent], self.coverage, promises)
        with self.assertRaises(Unavailable):
            self.check_verdict({**verdict, "absence_evidence": resolved}, [absent], self.coverage, promises)
        with self.assertRaises(Unavailable):
            resolve_promises([{**promises[0], "id": "p2"}], [absent], self.coverage)
        for finding, requires_av, checkable in (("unassessable", False, True), ("not_observed", True, False), ("not_observed", False, False)):
            audit = copy.deepcopy(absent)
            audit["claims"][0]["finding"] = finding
            with self.assertRaises(Unavailable):
                resolve_promises([{**promises[0], "requires_av": requires_av, "absence_checkable_from_captions": checkable}], [audit], self.coverage)
        first = copy.deepcopy(absent)
        supported = resolve_promises(promises, [first, self.audit], {**self.coverage, "total_chunks": 2, "processed_chunks": 2})
        self.assertEqual(supported[0]["kind"], "observed_caption_evidence")

    def test_failed_chunk_cannot_score(self):
        thumbnail, title = extractions(self.promises)
        coverage = {"processed_blocks": 0, "total_blocks": 2, "incomplete": False}
        with patch("poc.caption_chunks", return_value=[self.blocks, self.blocks]), patch("poc.model_request", side_effect=[thumbnail, title, self.raw_audit, Unavailable("failed_chunk")]), self.assertRaises(Unavailable):
            poc.classify("title", b"jpeg", self.blocks * 2, coverage, [], time.monotonic() + 30)
        self.assertEqual(coverage["processed_blocks"], 1)

    def test_missing_essential_modality_and_vision(self):
        thumbnail, title = extractions([{**self.promises[0], "requires_av": True}])
        with patch("poc.model_request", side_effect=[thumbnail, title]), self.assertRaisesRegex(Unavailable, "audiovisual"):
            poc.classify("title", b"jpeg", self.blocks, self.coverage, [], time.monotonic() + 30)
        with self.assertRaisesRegex(Unavailable, "vision"):
            poc.require_vision({"details": {"family": "gemma4"}, "capabilities": ["completion", "thinking"]})
        with self.assertRaisesRegex(Unavailable, "family"):
            poc.require_vision({"details": {"family": "qwen"}, "capabilities": ["vision"]})
        with self.assertRaisesRegex(Unavailable, "family"):
            poc.require_vision({"details": {"family": "gemma3"}, "capabilities": ["vision"]})

    def test_actual_image_bytes_and_call_bound_no_fallback(self):
        calls = []
        response = {"message": {"content": "{}"}, "done_reason": "stop"}
        with patch("poc.api", return_value=response) as api:
            poc.model_request("inspect", calls, time.monotonic() + 30, b"real image bytes")
        payload = api.call_args.args[1]
        self.assertEqual(base64.b64decode(payload["messages"][-1]["images"][0]), b"real image bytes")
        self.assertEqual(payload["model"], MODEL)
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        with self.assertRaises(Unavailable):
            poc.model_request("inspect", [{}] * (MAX_CHUNKS + 3), time.monotonic() + 30)

    def test_full_mocked_three_input_pipeline_and_null_failures(self):
        thumbnail, title = extractions(self.promises)
        coverage = {**self.coverage, "processed_blocks": 0}
        with patch("poc.thumbnail_input", return_value=(b"jpeg", {"source": "local"})), patch("poc.retrieve", return_value=(self.blocks, coverage, {"supplied_title": "Original title"})), patch("poc.local_server") as server, patch("poc.model_request", side_effect=[thumbnail, title, self.raw_audit, self.verdict]):
            server.return_value.__enter__.return_value = {"name": MODEL}
            result = poc.triage(poc.DEMO_URL, "Original title", "local")
        self.assertEqual(result["status"], "scored")
        self.assertEqual(sum(result["scores"].values()), 100)
        self.assertTrue(result["coverage"]["all_retrieved_captions_processed"])
        self.assertEqual(result["evidence"][0]["quote"], self.blocks[0]["text"])
        self.assertEqual(result["evidence"][0]["start_seconds"], 1.0)
        with patch("poc.thumbnail_input", side_effect=Unavailable("missing_thumbnail")):
            result = poc.triage(poc.DEMO_URL, "title", "missing")
        self.assertIsNone(result["scores"])
        self.assertEqual(result["status"], "unavailable")
        with patch("poc.retrieve") as retrieve:
            result = poc.triage("https://evil.test/", "title", "missing")
        self.assertIsNone(result["scores"])
        retrieve.assert_not_called()

    def test_source_separation_and_thumbnail_completeness(self):
        thumbnail, title = extractions(self.promises)
        material = {**thumbnail, "visible_text": "An extra promise", "claim_assessment": "material_claims", "assessment_reason": "Explicit claim", "promises": [{"claim": "An extra promise", "delivery_evidence": "caption_content"}]}
        packaging = combine_extractions(material, title)
        self.assertEqual([promise["source"] for promise in packaging["promises"]], ["thumbnail", "title"])
        self.assertEqual(packaging["promises"][0]["claim"], "An extra promise")
        combine_extractions(thumbnail, title)
        for bad in ({**thumbnail, "assessment_reason": ""}, {**material, "promises": []}, {**thumbnail, "claim_assessment": "uncertain"}, {**thumbnail, "extraction_complete": False}):
            with self.assertRaises(Unavailable):
                combine_extractions(bad, title)
        for mode in ("bogus", "unassessable"):
            with self.assertRaises(Unavailable):
                combine_extractions(thumbnail, {**title, "promises": [{"claim": "claim", "delivery_evidence": mode}]})
        coverage = {**self.coverage, "processed_blocks": 0}
        with patch("poc.model_request", side_effect=[thumbnail, title, self.raw_audit, self.verdict]) as model:
            poc.classify("unique displayed title", b"actual image", self.blocks, coverage, [], time.monotonic() + 30)
        image_call, title_call = model.call_args_list[:2]
        self.assertNotIn("unique displayed title", image_call.args[0])
        self.assertNotIn(poc.DEMO_URL, image_call.args[0])
        self.assertEqual(image_call.kwargs["image"], b"actual image")
        self.assertNotIn("image", title_call.kwargs)
        self.assertIn("unique displayed title", title_call.args[0])
        self.assertEqual(image_call.kwargs["schema"], poc.THUMBNAIL_SCHEMA)
        self.assertEqual(title_call.kwargs["schema"], poc.TITLE_SCHEMA)
        audit_call, final_call = model.call_args_list[2:]
        for branch in audit_call.kwargs["schema"]["properties"]["claims"]["items"]["anyOf"]:
            self.assertEqual(branch["properties"]["evidence_ids"]["items"]["enum"], ["b0001"])
        self.assertEqual(final_call.args[0].count(self.blocks[0]["text"]), 1)
        self.assertIn("evidence_id", final_call.kwargs["schema"]["properties"]["evidence"]["items"]["anyOf"][0]["properties"])


class FourWayContractTests(unittest.TestCase):
    """Mocks prove taxonomy plumbing and fail-closed contracts, not model accuracy."""

    def setUp(self):
        self.blocks = [{"id": "b0001", "text": "The doctor discusses deathbed dreams, sometimes weeks before death.", "start": 0.0, "end": 10.0}]
        self.coverage = {"incomplete": False, "total_blocks": 1, "processed_blocks": 0}

    def test_irrelevant_source_combinations_preserve_independent_extraction(self):
        for off_topic_sources in ({"title"}, {"thumbnail"}, {"title", "thumbnail"}):
            with self.subTest(sources=off_topic_sources):
                thumbnail = {"visible_text": "DOAC", "framing": "Two people", "claim_assessment": "material_claims", "assessment_reason": "Material subject", "extraction_complete": True,
                             "promises": [{"claim": "World Cup final" if "thumbnail" in off_topic_sources else "Deathbed dreams", "delivery_evidence": "caption_content"}]}
                title = {"promises": [{"claim": "World Cup final" if "title" in off_topic_sources else "Deathbed dreams", "delivery_evidence": "caption_content"}], "extraction_complete": True}
                audit = {"claims": [{"promise_id": f"p{index}", "finding": "not_observed" if source in off_topic_sources else "support",
                                    "note": "Deathbed dreams, different substantive subject" if source in off_topic_sources else "Relevant discussion",
                                    "evidence_ids": [] if source in off_topic_sources else ["b0001"]}
                                   for index, source in enumerate(("thumbnail", "title"), 1)]}
                final = {"reason": "Off-topic: " + ", ".join(sorted(off_topic_sources)), "dominant_class": "irrelevant",
                         "scores": {"irrelevant": 85, "clickbait": 10, "sensational": 3, "valid": 2},
                         "evidence": [{"promise_id": claim["promise_id"], "evidence_id": "b0001"} for claim in audit["claims"] if claim["evidence_ids"]]}
                with patch("poc.model_request", side_effect=[thumbnail, title, audit, final]) as model:
                    result, packaging, _ = poc.classify("unique title", b"actual thumbnail", self.blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
                self.assertEqual(result["dominant_class"], "irrelevant")
                self.assertEqual([promise["source"] for promise in packaging["promises"]], ["thumbnail", "title"])
                self.assertEqual(model.call_count, 4)
                image_call, title_call = model.call_args_list[:2]
                self.assertEqual(image_call.kwargs["image"], b"actual thumbnail")
                self.assertNotIn("unique title", image_call.args[0])
                self.assertNotIn("image", title_call.kwargs)
                self.assertIn("salient depicted subjects", image_call.args[0])
                prompt = model.call_args_list[-1].args[0]
                self.assertIn("title OR thumbnail", prompt)
                self.assertIn("one aggregate distribution", prompt)
                self.assertIn("Do not average away an unrelated", prompt)
                self.assertIn("Shared presenter", prompt)

    def test_all_absence_notes_and_indices_reach_prompt_without_becoming_citations(self):
        promises = [{"id": "p1", "claim": "Exactly 30 days", "requires_av": False, "absence_checkable_from_captions": True}]
        thumbnail, title = extractions(promises)
        notes = ["This chunk concerns childhood: different subject.", "Deathbed dreams: same subject, exact 30-day detail absent."]
        raw = [{"claims": [{"promise_id": "p1", "finding": "not_observed", "note": note, "evidence_ids": []}]} for note in notes]
        final = {"reason": "Same-topic detail undelivered", "dominant_class": "clickbait", "scores": {"irrelevant": 5, "clickbait": 85, "sensational": 8, "valid": 2}, "evidence": []}
        coverage = {**self.coverage, "total_blocks": 2}
        with patch("poc.caption_chunks", return_value=[self.blocks, self.blocks]), patch("poc.model_request", side_effect=[thumbnail, title, *raw, final]) as model:
            result, packaging, audits = poc.classify("exactly 30 days", b"jpeg", self.blocks * 2, coverage, [], time.monotonic() + 30)
        ready = scoring_ready(packaging["promises"], audits, coverage, self.blocks)
        self.assertEqual([item["note"] for item in ready["promises"][0]["chunk_assessments"]], notes)
        self.assertEqual([item["chunk_index"] for item in ready["promises"][0]["chunk_assessments"]], [1, 2])
        self.assertTrue(all(item["assessment_type"] == "model_assessment_not_verified_quote" for item in ready["promises"][0]["chunk_assessments"]))
        self.assertEqual(ready["sources"], {})
        prompt = model.call_args_list[-1].args[0]
        for note in notes:
            self.assertIn(note, prompt)
        self.assertIn("Same-subject missing or false details remain clickbait", prompt)
        self.assertEqual(model.call_args_list[-1].kwargs["schema"]["properties"]["evidence"]["maxItems"], 0)
        self.assertEqual(result["evidence"], [])
        # Either negative class can lead; no mechanical inference or score repair.
        irrelevant = {**final, "dominant_class": "irrelevant", "scores": {"irrelevant": 85, "clickbait": 5, "sensational": 8, "valid": 2}}
        self.assertEqual(validate_verdict(irrelevant, audits, coverage, packaging["promises"], self.blocks)["scores"], irrelevant["scores"])
        tie = {**final, "scores": {"irrelevant": 45, "clickbait": 45, "sensational": 5, "valid": 5}}
        validate_verdict(tie, audits, coverage, packaging["promises"], self.blocks)
        for dominant, scores in (("valid", {"irrelevant": 5, "clickbait": 5, "sensational": 10, "valid": 80}),
                                 ("sensational", {"irrelevant": 5, "clickbait": 5, "sensational": 80, "valid": 10})):
            with self.assertRaisesRegex(Unavailable, "material absent promise"):
                validate_verdict({**final, "dominant_class": dominant, "scores": scores}, audits, coverage, packaging["promises"], self.blocks)

    def test_same_subject_contradiction_remains_clickbait_not_automatic_irrelevance(self):
        blocks = [{**self.blocks[0], "text": "Some dying patients never report any visions."}]
        promises = [{"id": "p1", "claim": "Every patient exactly 30 days", "requires_av": False, "absence_checkable_from_captions": True}]
        thumbnail, title = extractions(promises)
        raw = {"claims": [{"promise_id": "p1", "finding": "contradiction", "note": "Some patients never report visions, incompatible with every patient having one.", "evidence_ids": ["b0001"]}]}
        final = {"reason": "Title overstates the same subject", "dominant_class": "clickbait", "scores": {"irrelevant": 2, "clickbait": 90, "sensational": 5, "valid": 3}, "evidence": [{"promise_id": "p1", "evidence_id": "b0001"}]}
        with patch("poc.model_request", side_effect=[thumbnail, title, raw, final]):
            result, _, _ = poc.classify("Every patient exactly 30 days", b"jpeg", blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
        self.assertEqual(result["dominant_class"], "clickbait")
        self.assertEqual(result["absence_evidence"], [])

    def test_off_topic_source_takes_priority_over_other_relevant_misleading_source(self):
        blocks = [{**self.blocks[0], "text": "Some dying patients never report visions."}]
        thumbnail = {"visible_text": "World Cup final", "framing": "Football subject", "claim_assessment": "material_claims", "assessment_reason": "Material subject",
                     "promises": [{"claim": "World Cup final", "delivery_evidence": "caption_content"}], "extraction_complete": True}
        title = {"promises": [{"claim": "Every dying patient has visions", "delivery_evidence": "caption_content"}], "extraction_complete": True}
        raw = {"claims": [{"promise_id": "p1", "note": "Deathbed visions, not football: different subject.", "finding": "not_observed", "evidence_ids": []},
                          {"promise_id": "p2", "note": "Some patients never report visions, not every patient.", "finding": "contradiction", "evidence_ids": ["b0001"]}]}
        final = {"reason": "Thumbnail off-topic; title relevant but overclaims.", "dominant_class": "irrelevant",
                 "scores": {"irrelevant": 80, "clickbait": 15, "sensational": 3, "valid": 2}, "evidence": [{"promise_id": "p2", "evidence_id": "b0001"}]}
        with patch("poc.model_request", side_effect=[thumbnail, title, raw, final]) as model:
            result, packaging, _ = poc.classify("Every dying patient has visions", b"jpeg", blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
        self.assertEqual(result["dominant_class"], "irrelevant")
        self.assertEqual([promise["source"] for promise in packaging["promises"]], ["thumbnail", "title"])
        self.assertIn("supported OR misleading", model.call_args_list[-1].args[0])
        self.assertIn("NOT clickbait", model.call_args_list[-1].args[0])

    def test_assessment_first_audit_schema_couples_findings_to_id_cardinality(self):
        promises = [{"id": "p1", "claim": "Deathbed dreams", "requires_av": False, "absence_checkable_from_captions": True}]
        thumbnail, title = extractions(promises)
        raw = {"claims": [{"promise_id": "p1", "note": "Supported discussion", "finding": "support", "evidence_ids": ["b0001"]}]}
        final = {"reason": "Both sources assessed", "dominant_class": "valid", "scores": {"irrelevant": 0, "clickbait": 0, "sensational": 10, "valid": 90},
                 "evidence": [{"promise_id": "p1", "evidence_id": "b0001"}]}
        with patch("poc.model_request", side_effect=[thumbnail, title, raw, final]) as model:
            _, packaging, _ = poc.classify("Deathbed dreams", b"jpeg", self.blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
        audit_call = model.call_args_list[2]
        branches = audit_call.kwargs["schema"]["properties"]["claims"]["items"]["anyOf"]
        self.assertEqual(len(branches), 2)
        for branch in branches:
            self.assertEqual(branch["required"], ["promise_id", "note", "finding", "evidence_ids"])
            self.assertEqual(list(branch["properties"]), branch["required"])
        for finding in ("support", "contradiction", "not_observed", "unassessable"):
            branch = next(branch for branch in branches if finding in branch["properties"]["finding"]["enum"])
            ids = branch["properties"]["evidence_ids"]
            self.assertEqual((ids["minItems"], ids["maxItems"]), (1, 2) if finding in ("support", "contradiction") else (0, 0))
            invalid_ids = [] if finding in ("support", "contradiction") else ["b0001"]
            bad = {"claims": [{**raw["claims"][0], "finding": finding, "evidence_ids": invalid_ids}]}
            with self.assertRaises(Unavailable):
                validate_audit(bad, packaging["promises"], self.blocks)
        self.assertIn("promise_id, note, finding, evidence_ids", audit_call.args[0])
        self.assertIn("POSITIVELY STATES an incompatible fact", audit_call.args[0])
        self.assertIn("less-specific wording is NOT contradiction", audit_call.args[0])

    def test_supported_subject_can_be_sensational_or_valid(self):
        promises = [{"id": "p1", "claim": "Deathbed dreams", "requires_av": False, "absence_checkable_from_captions": True}]
        thumbnail, title = extractions(promises)
        raw = {"claims": [{"promise_id": "p1", "finding": "support", "note": "Same subject supported", "evidence_ids": ["b0001"]}]}
        for dominant, scores in (("sensational", {"irrelevant": 2, "clickbait": 3, "sensational": 85, "valid": 10}),
                                 ("valid", {"irrelevant": 2, "clickbait": 3, "sensational": 10, "valid": 85})):
            final = {"reason": "Supported subject framing", "dominant_class": dominant, "scores": scores, "evidence": [{"promise_id": "p1", "evidence_id": "b0001"}]}
            with patch("poc.model_request", side_effect=[thumbnail, title, raw, final]):
                result, _, _ = poc.classify("Deathbed dreams", b"jpeg", self.blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
            self.assertEqual(result["dominant_class"], dominant)
            self.assertEqual(result["scores"], scores)


class EvidenceIdInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.blocks = [{"id": "b0082", "text": "have you ever felt a dead family member visit you", "start": 12.0, "end": 22.0}]
        self.promises = [{"id": "p1", "requires_av": False, "absence_checkable_from_captions": True}]
        self.raw = {"claims": [{"promise_id": "p1", "finding": "support", "note": "Reported experience", "evidence_ids": ["b0082"]}]}
        self.coverage = {"incomplete": False, "total_blocks": 1, "processed_blocks": 1, "total_chunks": 1, "processed_chunks": 1}

    def test_id_materializes_exact_source_not_model_quote(self):
        normalized = validate_audit(self.raw, self.promises, self.blocks)
        verdict = {"dominant_class": "sensational", "scores": {"irrelevant": 0, "clickbait": 20, "sensational": 70, "valid": 10}, "reason": "Estimate", "evidence": [{"promise_id": "p1", "evidence_id": "b0082"}]}
        result = validate_verdict(verdict, [normalized], self.coverage, self.promises, self.blocks)
        self.assertEqual(result["evidence"], [{"promise_id": "p1", "block_id": "b0082", "quote": self.blocks[0]["text"], "start_seconds": 12.0, "end_seconds": 22.0}])

    def test_unknown_duplicate_legacy_and_cross_promise_rejected(self):
        for ids in (["b9999"], ["b0082", "b0082"], ["b0082"] * 3, [True]):
            raw = copy.deepcopy(self.raw)
            raw["claims"][0]["evidence_ids"] = ids
            with self.assertRaises(Unavailable):
                validate_audit(raw, self.promises, self.blocks)
        raw = copy.deepcopy(self.raw)
        raw["claims"][0]["quote"] = "have ever felt"
        with self.assertRaises(Unavailable):
            validate_audit(raw, self.promises, self.blocks)
        normalized = validate_audit(self.raw, self.promises, self.blocks)
        verdict = {"dominant_class": "sensational", "scores": {"irrelevant": 0, "clickbait": 20, "sensational": 70, "valid": 10}, "reason": "Estimate", "evidence": [{"promise_id": "p2", "evidence_id": "b0082"}]}
        with self.assertRaises(Unavailable):
            validate_verdict(verdict, [normalized], self.coverage, self.promises, self.blocks)
        altered = copy.deepcopy(normalized)
        altered["sources"]["b0082"]["text"] = "have ever felt"
        with self.assertRaises(Unavailable):
            validate_verdict({**verdict, "evidence": [{"promise_id": "p1", "evidence_id": "b0082"}]}, [altered], self.coverage, self.promises, self.blocks)
        for sources in ({}, {"b0082": {**normalized["sources"]["b0082"], "start": 13.0}}):
            with self.assertRaises(Unavailable):
                validate_verdict({**verdict, "evidence": [{"promise_id": "p1", "evidence_id": "b0082"}]}, [{**normalized, "sources": sources}], self.coverage, self.promises, self.blocks)

    def test_out_of_chunk_oversize_absent_and_legacy_selections(self):
        with self.assertRaises(Unavailable):
            validate_audit(self.raw, self.promises, [{**self.blocks[0], "id": "b0083"}])
        for text in ("x" * 513, "字" * 400):
            with self.assertRaisesRegex(Unavailable, "too_large"):
                validate_audit(self.raw, self.promises, [{**self.blocks[0], "text": text}])
        with self.assertRaises(Unavailable):
            validate_audit({"claims": [{**self.raw["claims"][0], "finding": "not_observed"}]}, self.promises, self.blocks)
        normalized = validate_audit(self.raw, self.promises, self.blocks)
        for evidence in ([{"promise_id": "p1", "block_id": "b0082", "quote": "have ever felt"}], [{"promise_id": "p1", "evidence_id": "b0082"}] * 2):
            with self.assertRaises(Unavailable):
                validate_verdict({"dominant_class": "sensational", "scores": {"irrelevant": 0, "clickbait": 20, "sensational": 70, "valid": 10}, "reason": "Estimate", "evidence": evidence}, [normalized], self.coverage, self.promises, self.blocks)


class ScoringReadyTests(unittest.TestCase):
    def setUp(self):
        self.blocks = [{"id": "b0001", "text": "The experience happened, but only once.", "start": 0.0, "end": 10.0}]
        self.promises = [{"id": "p1", "claim": "Experience", "source": "title", "requires_av": False, "absence_checkable_from_captions": True}, {"id": "p2", "claim": "Another topic", "source": "title", "requires_av": False, "absence_checkable_from_captions": True}]
        self.raw = {"claims": [{"promise_id": "p1", "finding": "support", "note": "Reported", "evidence_ids": ["b0001"]}, {"promise_id": "p2", "finding": "not_observed", "note": "Absent", "evidence_ids": []}]}
        self.coverage = {"incomplete": False, "total_blocks": 1, "processed_blocks": 0}
        self.final = {"dominant_class": "clickbait", "scores": {"irrelevant": 0, "clickbait": 80, "sensational": 15, "valid": 5}, "reason": "Undelivered topic", "evidence": [{"promise_id": "p1", "evidence_id": "b0001"}]}

    def test_mixed_observed_absence_ready_view_and_no_status_branch(self):
        thumbnail, title = extractions(self.promises)
        with patch("poc.model_request", side_effect=[thumbnail, title, self.raw, self.final]) as model:
            verdict, _, _ = poc.classify("title", b"jpeg", self.blocks, self.coverage, [], time.monotonic() + 30)
        self.assertEqual(verdict["status"], "scored")
        scorer = model.call_args_list[-1]
        self.assertIn("SCORING_READY_PROMISES=", scorer.args[0])
        self.assertIn("not_observed_in_complete_captions", scorer.args[0])
        self.assertNotIn("AUDIT_OUTCOMES=", scorer.args[0])
        self.assertNotIn("status", scorer.kwargs["schema"]["properties"])

    def test_unassessable_or_partial_never_reaches_scorer(self):
        thumbnail, title = extractions(self.promises)
        raw = copy.deepcopy(self.raw)
        raw["claims"][0].update(finding="unassessable", evidence_ids=[])
        for audit, coverage in ((raw, self.coverage), (self.raw, {**self.coverage, "incomplete": True})):
            with patch("poc.model_request", side_effect=[thumbnail, title, audit]) as model, self.assertRaises(Unavailable):
                poc.classify("title", b"jpeg", self.blocks, copy.deepcopy(coverage), [], time.monotonic() + 30)
            self.assertEqual(model.call_count, 3)

    def test_pure_absence_scores_without_quotes_and_legacy_or_forged_states_fail(self):
        raw = copy.deepcopy(self.raw)
        raw["claims"][0].update(finding="not_observed", evidence_ids=[])
        thumbnail, title = extractions(self.promises)
        final = {**self.final, "evidence": []}
        coverage = copy.deepcopy(self.coverage)
        with patch("poc.model_request", side_effect=[thumbnail, title, raw, final]) as model:
            result, _, audits = poc.classify("title", b"jpeg", self.blocks, coverage, [], time.monotonic() + 30)
        self.assertEqual(model.call_count, 4)
        self.assertEqual(result["evidence"], [])
        self.assertEqual(len(result["absence_evidence"]), 2)
        for value in ({**final, "status": "scored"}, {**final, "scores": {"irrelevant": 0, "clickbait": 0, "sensational": 0, "valid": 0}}, {**final, "scoring_ready": True}, {**final, "absence_evidence": result["absence_evidence"]}):
            with self.assertRaises(Unavailable):
                validate_verdict(value, audits, coverage, self.promises, self.blocks)
        with self.assertRaises(Unavailable):
            validate_verdict(final, audits, {**coverage, "processed_chunks": 0}, self.promises, self.blocks)

    def test_missing_source_catalog_never_reaches_scorer(self):
        thumbnail, title = extractions(self.promises)
        normalized = validate_audit(self.raw, self.promises, self.blocks)
        normalized["sources"] = {}
        with patch("poc.model_request", side_effect=[thumbnail, title, self.raw]) as model, patch("poc.validate_audit", return_value=normalized), self.assertRaisesRegex(Unavailable, "source_provenance"):
            poc.classify("title", b"jpeg", self.blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
        self.assertEqual(model.call_count, 3)
        with patch("poc.thumbnail_input", return_value=(b"jpeg", {})), patch("poc.retrieve", side_effect=Unavailable("missing_captions")), patch("poc.model_request") as model:
            result = poc.triage(poc.DEMO_URL, "title", "local")
        self.assertIsNone(result["scores"])
        model.assert_not_called()

    def test_ready_view_retains_every_support_and_contradiction_source(self):
        second = [{"id": "b0002", "text": "Other accounts disagree and timing is unclear.", "start": 10.0, "end": 20.0}]
        support = validate_audit(self.raw, self.promises, self.blocks)
        raw = copy.deepcopy(self.raw)
        raw["claims"][0].update(finding="contradiction", note="Qualification", evidence_ids=["b0002"])
        contradiction = validate_audit(raw, self.promises, second)
        coverage = {"incomplete": False, "total_blocks": 2, "processed_blocks": 2, "total_chunks": 2, "processed_chunks": 2}
        ready = scoring_ready(self.promises, [support, contradiction], coverage, self.blocks + second)
        self.assertEqual(ready["promises"][0]["findings"], ["contradiction", "support"])
        self.assertEqual([item["evidence_ids"] for item in ready["promises"][0]["observations"]], [["b0001"], ["b0002"]])
        self.assertEqual(set(ready["sources"]), {"b0001", "b0002"})
        self.assertEqual(ready["sources"]["b0002"]["text"], second[0]["text"])
        self.assertEqual(ready["promises"][1]["audited_chunks"], 2)
        self.assertNotIn("observations", ready["promises"][1])

    def test_assessment_first_schema_order_and_taxonomy(self):
        thumbnail, title = extractions(self.promises)
        final = {**self.final, "dominant_class": "clickbait"}
        with patch("poc.model_request", side_effect=[thumbnail, title, self.raw, final]) as model:
            poc.classify("title", b"jpeg", self.blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
        scorer = model.call_args_list[-1]
        schema = scorer.kwargs["schema"]
        self.assertEqual(schema["required"], ["reason", "dominant_class", "scores", "evidence"])
        self.assertEqual(list(schema["properties"]), schema["required"])
        self.assertEqual(schema["properties"]["dominant_class"]["enum"], ["irrelevant", "clickbait", "sensational", "valid"])
        self.assertIn("mutually exclusive", scorer.args[0])

    def test_declared_class_must_match_argmax(self):
        audit = validate_audit(self.raw, self.promises, self.blocks)
        coverage = {**self.coverage, "processed_blocks": 1, "total_chunks": 1, "processed_chunks": 1}
        for dominant in ("sensational", "valid", "other", None):
            with self.assertRaises(Unavailable):
                validate_verdict({**self.final, "dominant_class": dominant}, [audit], coverage, self.promises, self.blocks)
        validate_verdict({**self.final, "dominant_class": "clickbait"}, [audit], coverage, self.promises, self.blocks)

    def test_final_schema_couples_exact_pairs_and_omits_absence_promise(self):
        thumbnail, title = extractions(self.promises)
        with patch("poc.model_request", side_effect=[thumbnail, title, self.raw, self.final]) as model:
            poc.classify("title", b"jpeg", self.blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
        schema = model.call_args_list[-1].kwargs["schema"]["properties"]["evidence"]
        self.assertEqual(schema["maxItems"], 1)
        self.assertTrue(schema["uniqueItems"])
        choices = schema["items"]["anyOf"]
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["properties"]["promise_id"]["enum"], ["p1"])
        self.assertEqual(choices[0]["properties"]["evidence_id"]["enum"], ["b0001"])

    def test_final_schema_no_sources_only_empty_array(self):
        raw = copy.deepcopy(self.raw)
        raw["claims"][0].update(finding="not_observed", evidence_ids=[])
        thumbnail, title = extractions(self.promises)
        with patch("poc.model_request", side_effect=[thumbnail, title, raw, {**self.final, "evidence": []}]) as model:
            poc.classify("title", b"jpeg", self.blocks, copy.deepcopy(self.coverage), [], time.monotonic() + 30)
        schema = model.call_args_list[-1].kwargs["schema"]["properties"]["evidence"]
        self.assertEqual(schema["maxItems"], 0)
        self.assertNotIn("anyOf", json.dumps(schema))

    def test_distinct_sources_cannot_cross_pair_and_schema_choices_are_deduplicated(self):
        blocks = self.blocks + [{"id": "b0002", "text": "Another topic actually discussed.", "start": 10.0, "end": 20.0}]
        raw = copy.deepcopy(self.raw)
        raw["claims"][1].update(finding="support", evidence_ids=["b0002"])
        audit = validate_audit(raw, self.promises, blocks)
        schema = poc.evidence_selection_schema([audit, audit])
        self.assertEqual(schema["maxItems"], 2)
        choices = schema["items"]["anyOf"]
        self.assertEqual([(item["properties"]["promise_id"]["enum"], item["properties"]["evidence_id"]["enum"]) for item in choices], [(["p1"], ["b0001"]), (["p2"], ["b0002"])])
        coverage = {"incomplete": False, "total_blocks": 2, "processed_blocks": 2, "total_chunks": 1, "processed_chunks": 1}
        for evidence in ([{"promise_id": "p1", "evidence_id": "b0002"}], [{"promise_id": "p2", "evidence_id": "b0001"}], [{"promise_id": "p2", "evidence_id": "N/A"}], self.final["evidence"] * 9):
            with self.assertRaises(Unavailable):
                validate_verdict({**self.final, "evidence": evidence}, [audit], coverage, self.promises, blocks)


if __name__ == "__main__":
    unittest.main()
