"""Pure, fail-closed logic for the throwaway three-input prototype."""
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

MODEL = "youtube-triage-gemma4-e4b:poc"
NUM_CTX = 32768
NUM_PREDICT = 2048
MAX_CHUNKS = 6
CHUNK_BYTES = 20000
CLASS_ORDER = ("irrelevant", "clickbait", "sensational", "valid")
CLASSES = set(CLASS_ORDER)
DELIVERY_MODES = {"caption_content", "caption_support_only", "requires_audiovisual", "unassessable"}


class Unavailable(ValueError):
    pass


def canonical_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError()
        host = parsed.hostname
        query = parse_qs(parsed.query)
        if "list" in query:
            raise ValueError()
        if host == "youtu.be":
            video_id = parsed.path.removeprefix("/")
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch" and len(query.get("v", [])) == 1:
                video_id = query["v"][0]
            elif parsed.path.startswith("/shorts/"):
                video_id = parsed.path[len("/shorts/"):]
            else:
                raise ValueError()
        else:
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError()
        return f"https://www.youtube.com/watch?v={video_id}", video_id
    except ValueError as exc:
        raise Unavailable("invalid_youtube_url: use one HTTPS watch, youtu.be, or shorts URL without a playlist") from exc


def safe_thumbnail_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in {"i.ytimg.com", "img.youtube.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError()
        return value
    except ValueError as exc:
        raise Unavailable("unsupported_thumbnail_url: HTTPS i.ytimg.com/img.youtube.com or a local image file required") from exc


def safe_caption_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not (parsed.hostname == "youtube.com" or (parsed.hostname or "").endswith(".youtube.com"))):
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise Unavailable("unsafe_caption_url") from exc
    return value


def english_language(value):
    return isinstance(value, str) and re.fullmatch(r"en(?:[-_][A-Za-z0-9]{2,8})*", value, re.IGNORECASE) is not None


def caption_candidates(info):
    """Original English JSON3 alternatives, manual first, without signed-URL rewriting."""
    if not isinstance(info, dict):
        raise Unavailable("invalid_youtube_metadata")
    candidates = []
    seen = set()
    for kind, field in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        tracks = info.get(field) or {}
        if not isinstance(tracks, dict):
            raise Unavailable("invalid_youtube_metadata")
        languages = sorted((language for language in tracks if english_language(language)),
                           key=lambda language: (0 if language == "en-orig" else 1 if language == "en" else 2, language))
        for language in languages:
            entries = tracks[language]
            if not isinstance(entries, list):
                raise Unavailable("invalid_youtube_metadata")
            for track in entries:
                if not isinstance(track, dict) or track.get("ext") != "json3" or not isinstance(track.get("url"), str):
                    continue
                url = track["url"]
                try:
                    safe_caption_url(url)
                    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
                except Unavailable:
                    continue
                if "tlang" in query or not all(english_language(source) for source in query.get("lang", [])) or url in seen:
                    continue
                seen.add(url)
                candidates.append((kind, language, track))
    return candidates


def select_captions(info):
    candidates = caption_candidates(info)
    if candidates:
        return candidates[0]
    raise Unavailable("no_english_captions: original English JSON3 required; translated tracks are unsupported")


def normalize_captions(raw, duration):
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        raise Unavailable("invalid_caption_track")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise Unavailable("missing_video_duration")
    events = []
    for event in raw.get("events", []):
        if not isinstance(event, dict) or not isinstance(event.get("segs", []), list):
            raise Unavailable("invalid_caption_event")
        if any(not isinstance(seg, dict) or not isinstance(seg.get("utf8", ""), str) for seg in event.get("segs", [])):
            raise Unavailable("invalid_caption_segment")
        text = " ".join("".join(seg.get("utf8", "") for seg in event.get("segs", [])).split())
        if not text:
            continue
        if type(event.get("tStartMs", 0)) not in (int, float) or type(event.get("dDurationMs", 0)) not in (int, float):
            raise Unavailable("invalid_caption_timestamps")
        start = event.get("tStartMs", 0) / 1000
        end = start + event.get("dDurationMs", 0) / 1000
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= duration and start <= end <= duration + 30):
            raise Unavailable("invalid_caption_timestamps")
        events.append((start, min(end, duration), text))
    if not events:
        raise Unavailable("empty_captions")
    if any(events[i][0] > events[i + 1][0] for i in range(len(events) - 1)):
        raise Unavailable("unordered_captions")
    # Never deduplicate repeated words: every nonblank retrieved event survives.
    blocks = []
    for start, end, text in events:
        if blocks and start - blocks[-1]["start"] < 10:
            blocks[-1]["text"] += " " + text
            blocks[-1]["end"] = max(blocks[-1]["end"], end)
            blocks[-1]["events"] += 1
        else:
            blocks.append({"id": f"b{len(blocks) + 1:04}", "start": start, "end": end, "text": text, "events": 1})
    cursor = 0.0
    covered = 0.0
    gaps = []
    for start, end, _ in events:
        if start > cursor:
            gaps.append({"start": round(cursor, 3), "end": round(start, 3), "seconds": round(start - cursor, 3)})
        covered += max(0, end - max(cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        gaps.append({"start": round(cursor, 3), "end": duration, "seconds": round(duration - cursor, 3)})
    ratio = covered / duration
    max_gap = max((gap["seconds"] for gap in gaps), default=0)
    incomplete = ratio < 0.85 or max_gap > 60 or events[0][0] > 30 or duration - cursor > 30
    coverage = {"retrieved_events": len(events), "total_blocks": len(blocks), "processed_blocks": 0,
                "caption_text_bytes": sum(len(block["text"].encode()) for block in blocks),
                "duration_seconds": duration, "first_caption_seconds": events[0][0], "last_caption_end_seconds": cursor,
                "caption_interval_coverage": round(ratio, 4), "max_gap_seconds": max_gap,
                "material_gaps": [gap for gap in gaps if gap["seconds"] > 10], "incomplete": incomplete,
                "completeness_method": "heuristic caption interval coverage, not proof of full audiovisual coverage"}
    if incomplete:
        raise Unavailable("partial_captions: sparse intervals, missing endpoints, or a gap over 60 seconds")
    return blocks, coverage


def caption_chunks(blocks):
    chunks = [[]]
    size = 0
    for block in blocks:
        encoded = len(json.dumps(block, ensure_ascii=False).encode()) + 2
        if encoded > CHUNK_BYTES:
            raise Unavailable("transcript_too_large: one caption block exceeds the bounded context budget")
        if size + encoded > CHUNK_BYTES and chunks[-1]:
            chunks.append([])
            size = 0
        chunks[-1].append(block)
        size += encoded
    if len(chunks) > MAX_CHUNKS:
        raise Unavailable("transcript_too_large: more than six complete caption chunks")
    assert sum(len(chunk) for chunk in chunks) == len(blocks)
    return chunks


def ensure_context(messages, num_ctx=NUM_CTX, num_predict=NUM_PREDICT, image=False):
    # One UTF-8 byte per token is intentionally conservative, unlike chars/4.
    text = json.dumps([{key: value for key, value in message.items() if key != "images"} for message in messages], ensure_ascii=False)
    reserve = 4096 if image else 2048  # Template overhead + separately reserved image tokens.
    if len(text.encode()) + num_predict + reserve > num_ctx:
        raise Unavailable("context_budget_exceeded")


def bounded_string(value, maximum, name):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Unavailable(f"invalid_model_output: {name}")
    return value


def extraction_promises(value):
    promises = value["promises"]
    if not isinstance(promises, list) or len(promises) > 6:
        raise Unavailable("invalid_model_output: promises")
    for promise in promises:
        if not isinstance(promise, dict) or set(promise) != {"claim", "delivery_evidence"}:
            raise Unavailable("invalid_model_output: promise keys")
        bounded_string(promise["claim"], 300, "claim")
        if not isinstance(promise["delivery_evidence"], str) or promise["delivery_evidence"] not in DELIVERY_MODES:
            raise Unavailable("invalid_model_output: delivery evidence mode")
    return value


def combine_extractions(thumbnail, title):
    if not isinstance(thumbnail, dict) or set(thumbnail) != {"visible_text", "framing", "claim_assessment", "assessment_reason", "promises", "extraction_complete"}:
        raise Unavailable("invalid_model_output: thumbnail extraction keys")
    if not isinstance(title, dict) or set(title) != {"promises", "extraction_complete"}:
        raise Unavailable("invalid_model_output: title extraction keys")
    if type(thumbnail["extraction_complete"]) is not bool or type(title["extraction_complete"]) is not bool or not thumbnail["extraction_complete"] or not title["extraction_complete"]:
        raise Unavailable("incomplete_packaging_extraction")
    if not isinstance(thumbnail["visible_text"], str) or len(thumbnail["visible_text"]) > 600:
        raise Unavailable("invalid_model_output: visible text")
    bounded_string(thumbnail["framing"], 600, "thumbnail framing")
    bounded_string(thumbnail["assessment_reason"], 300, "thumbnail assessment reason")
    if not isinstance(thumbnail["claim_assessment"], str) or thumbnail["claim_assessment"] not in {"material_claims", "no_material_claims", "uncertain"}:
        raise Unavailable("invalid_model_output: thumbnail claim assessment")
    extraction_promises(thumbnail)
    extraction_promises(title)
    if thumbnail["claim_assessment"] == "uncertain":
        raise Unavailable("uncertain_thumbnail_claims")
    if (thumbnail["claim_assessment"] == "material_claims") != bool(thumbnail["promises"]):
        raise Unavailable("inconsistent_thumbnail_claim_assessment")
    combined = []
    for source, extraction in (("thumbnail", thumbnail), ("title", title)):
        for promise in extraction["promises"]:
            mode = promise["delivery_evidence"]
            if mode == "unassessable":
                raise Unavailable("unassessable_material_packaging_promise")
            combined.append({"id": f"p{len(combined) + 1}", "claim": promise["claim"], "source": source,
                             "delivery_evidence": mode, "requires_av": mode == "requires_audiovisual",
                             "absence_checkable_from_captions": mode == "caption_content"})
    if not 1 <= len(combined) <= 6:
        raise Unavailable("packaging_promise_limit: one to six complete material promises required; none truncated")
    return {"thumbnail_text": thumbnail["visible_text"], "framing": thumbnail["framing"], "promises": combined,
            "thumbnail_assessment": {key: thumbnail[key] for key in ("claim_assessment", "assessment_reason", "extraction_complete")},
            "title_extraction_complete": title["extraction_complete"]}


def validate_audit(value, promises, blocks):
    if not isinstance(value, dict) or set(value) != {"claims"} or not isinstance(value["claims"], list):
        raise Unavailable("invalid_model_output: audit")
    if any(not isinstance(claim, dict) for claim in value["claims"]) or [claim.get("promise_id") for claim in value["claims"]] != [promise["id"] for promise in promises]:
        raise Unavailable("invalid_model_output: audit must assess every promise in order")
    by_id = {block["id"]: block for block in blocks}
    sources = {}
    for claim in value["claims"]:
        if set(claim) != {"promise_id", "finding", "note", "evidence_ids"} or not isinstance(claim["finding"], str) or claim["finding"] not in {"support", "contradiction", "not_observed", "unassessable"}:
            raise Unavailable("invalid_model_output: claim assessment")
        bounded_string(claim["note"], 300, "claim note")
        ids = claim["evidence_ids"]
        if not isinstance(ids, list) or len(ids) > 2 or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
            raise Unavailable("invalid_model_output: evidence ids")
        if claim["finding"] in {"support", "contradiction"} and not ids:
            raise Unavailable("invalid_model_output: supported finding lacks evidence")
        if claim["finding"] in {"not_observed", "unassessable"} and ids:
            raise Unavailable("invalid_model_output: unobserved finding cannot cite evidence")
        for evidence_id in ids:
            if evidence_id not in by_id:
                raise Unavailable("invalid_model_output: evidence id outside supplied chunk")
            block = by_id[evidence_id]
            if len(block["text"]) > 512 or len(block["text"].encode("utf-8")) > 1024:
                raise Unavailable("evidence_block_too_large: no source text truncated")
            sources[evidence_id] = {key: block[key] for key in ("text", "start", "end")}
    return {"claims": [{**claim, "evidence_ids": list(claim["evidence_ids"])} for claim in value["claims"]], "sources": sources}


def scoring_ready(promises, audits, coverage, blocks):
    """Derive eligibility from original audits/coverage, never caller-authored states."""
    resolved = resolve_promises(promises, audits, coverage)
    original = {block["id"]: {key: block[key] for key in ("text", "start", "end")} for block in (blocks or [])}
    sources = {}
    for audit in audits:
        references = {evidence_id for claim in audit["claims"] for evidence_id in claim["evidence_ids"]}
        if not isinstance(audit.get("sources"), dict) or set(audit["sources"]) != references:
            raise Unavailable("invalid_source_provenance: audit source catalog incomplete")
        for evidence_id, source in audit["sources"].items():
            if evidence_id not in original or source != original[evidence_id]:
                raise Unavailable("invalid_source_provenance: audit source differs from original captions")
            sources[evidence_id] = source
    ready = []
    for promise, outcome in zip(promises, resolved):
        item = {**promise, **outcome}
        if outcome["kind"] == "observed_caption_evidence":
            item["observations"] = [{**claim, "evidence_ids": list(claim["evidence_ids"])} for audit in audits for claim in audit["claims"] if claim["promise_id"] == promise["id"] and claim["finding"] in {"support", "contradiction"}]
        else:
            item["delivery_finding"] = "Material promise/claim not observed in any accepted caption chunk"
            item["chunk_assessments"] = [{"chunk_index": index, "assessment_type": "model_assessment_not_verified_quote", "note": claim["note"]}
                                         for index, audit in enumerate(audits, 1) for claim in audit["claims"] if claim["promise_id"] == promise["id"]]
        ready.append(item)
    return {"promises": ready, "sources": sources}


def validate_verdict(value, audits, coverage, promises, blocks):
    if not isinstance(value, dict) or set(value) != {"reason", "dominant_class", "scores", "evidence"}:
        raise Unavailable("invalid_model_output: verdict keys")
    bounded_string(value["reason"], 1200, "reason")
    if not isinstance(value["dominant_class"], str) or value["dominant_class"] not in CLASSES:
        raise Unavailable("invalid_model_output: dominant class")
    if not isinstance(value["evidence"], list) or len(value["evidence"]) > 8:
        raise Unavailable("invalid_model_output: evidence")
    ready = scoring_ready(promises, audits, coverage, blocks)
    sources = ready["sources"]
    allowed = {(claim["promise_id"], evidence_id)
               for audit in audits for claim in audit["claims"] for evidence_id in claim["evidence_ids"]}
    selections = []
    seen = set()
    for item in value["evidence"]:
        if not isinstance(item, dict) or set(item) != {"promise_id", "evidence_id"} or any(not isinstance(part, str) for part in item.values()):
            raise Unavailable("invalid_model_output: final evidence was not verified")
        pair = (item["promise_id"], item["evidence_id"])
        if pair not in allowed or pair in seen or item["evidence_id"] not in sources:
            raise Unavailable("invalid_model_output: final evidence was not verified")
        seen.add(pair)
        source = sources[item["evidence_id"]]
        selections.append({"promise_id": item["promise_id"], "block_id": item["evidence_id"], "quote": source["text"], "start_seconds": source["start"], "end_seconds": source["end"]})
    result = {**value, "status": "scored", "evidence": selections}
    scores = value["scores"]
    if not isinstance(scores, dict) or set(scores) != CLASSES or any(type(score) is not int or not 0 <= score <= 100 for score in scores.values()) or sum(scores.values()) != 100:
        raise Unavailable("invalid_model_output: scores must be exactly four integers summing to 100")
    if scores[value["dominant_class"]] != max(scores.values()):
        raise Unavailable("inconsistent_model_output: dominant class does not match maximum percentage")
    absence = [item for item in ready["promises"] if item["kind"] == "not_observed_in_complete_captions"]
    if not value["evidence"] and not absence:
        raise Unavailable("incomplete_evidence: neither verified quotes nor accepted complete-caption absence")
    if absence and max(scores["irrelevant"], scores["clickbait"]) < max(scores["sensational"], scores["valid"]):
        raise Unavailable("inconsistent_model_output: material absent promise cannot be outranked by fair/sensational packaging")
    return result


def resolve_promises(promises, audits, coverage):
    """Host-derived observed/absence states; never invent quotes for absence."""
    if type(coverage.get("incomplete")) is not bool or any(type(coverage.get(key)) is not int for key in ("total_blocks", "processed_blocks", "total_chunks", "processed_chunks")):
        raise Unavailable("invalid_coverage_counters")
    if coverage.get("incomplete") or coverage.get("total_blocks", 0) <= 0 or coverage.get("processed_blocks") != coverage.get("total_blocks"):
        raise Unavailable("incomplete_evidence: all accepted caption blocks required")
    total_chunks = coverage.get("total_chunks", 0)
    if total_chunks <= 0 or coverage.get("processed_chunks") != total_chunks or len(audits) != total_chunks:
        raise Unavailable("incomplete_evidence: all planned caption chunks required")
    if not promises or not audits or not audits[0]["claims"]:
        raise Unavailable("unresolved_material_promises: no caption assessments")
    ids = [promise["id"] for promise in promises]
    if any([claim["promise_id"] for claim in audit["claims"]] != ids for audit in audits):
        raise Unavailable("incomplete_promise_assessments")
    resolved = []
    for promise in promises:
        promise_id = promise["id"]
        findings = [claim for audit in audits for claim in audit["claims"] if claim["promise_id"] == promise_id]
        if promise["requires_av"] or any(claim["finding"] == "unassessable" for claim in findings):
            raise Unavailable("unassessable_material_promise: " + promise_id)
        observed = [claim for claim in findings if claim["finding"] in {"support", "contradiction"} and claim["evidence_ids"]]
        if observed:
            resolved.append({"promise_id": promise_id, "kind": "observed_caption_evidence", "findings": sorted({claim["finding"] for claim in observed})})
        elif promise["absence_checkable_from_captions"] and all(claim["finding"] == "not_observed" for claim in findings):
            resolved.append({"promise_id": promise_id, "kind": "not_observed_in_complete_captions", "audited_chunks": len(audits), "total_chunks": total_chunks})
        else:
            raise Unavailable("unresolved_material_promise: " + promise_id)
    return resolved
