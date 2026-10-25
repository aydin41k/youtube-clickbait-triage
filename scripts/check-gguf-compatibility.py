"""Read metadata only (never tensor weights) before testing custom composition."""
import json
import argparse
import hashlib
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent.parent
FORMATS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


def metadata(path):
    with path.open("rb") as file:
        def number(fmt):
            return struct.unpack("<" + fmt, file.read(struct.calcsize(fmt)))[0]

        def string():
            size = number("Q")
            if size > 2_000_000:
                raise ValueError("oversize GGUF metadata string")
            return file.read(size).decode()

        def value(kind):
            if kind == 8:
                return string()
            if kind == 9:
                element_kind, count = number("I"), number("Q")
                if count > 1_000_000:
                    raise ValueError("oversize GGUF metadata array")
                for _ in range(count):
                    value(element_kind)
                return {"array_elements": count}
            return number(FORMATS[kind])

        if file.read(4) != b"GGUF" or number("I") not in (2, 3):
            raise ValueError("unsupported GGUF")
        tensors, count = number("Q"), number("Q")
        info = {"tensor_count": tensors}
        for _ in range(count):
            key, kind = string(), number("I")
            item = value(kind)
            if key.startswith(("general.", "gemma4.", "clip.", "mmproj.")):
                info[key] = item
        selected_tensors = {}
        for _ in range(tensors):
            name, dimensions = string(), number("I")
            shape = [number("Q") for _ in range(dimensions)]
            number("I")
            number("Q")
            if name.startswith("mm.") or "projector" in name or "projection" in name:
                selected_tensors[name] = shape
        info["projection_tensor_shapes"] = selected_tensors
        return info


parser = argparse.ArgumentParser()
parser.add_argument("--partial", action="store_true", help="Inspect downloaded header only; do not claim hash verification")
args = parser.parse_args()
core = metadata(ROOT / ".runtime/import/gemma4-core.gguf")
projector = metadata(ROOT / ".runtime/import/mmproj.gemma4-e4b.gguf")
if core.get("general.architecture") != "gemma4" or core.get("gemma4.embedding_length") != 2560 or core.get("gemma4.block_count") != 42:
    raise SystemExit("Existing core is not the expected Gemma 4 E4B shape")
if (projector.get("general.architecture") != "clip" or projector.get("general.type") != "mmproj"
        or projector.get("clip.vision.projector_type") != "gemma4v" or not projector.get("clip.has_vision_encoder")
        or projector.get("clip.vision.projection_dim") != core["gemma4.embedding_length"]
        or projector.get("projection_tensor_shapes", {}).get("mm.input_projection.weight") != [768, 2560]):
    raise SystemExit("Projector metadata/tensor shape is incompatible with the Gemma 4 E4B core")
projection_dimensions = {key: value for key, value in projector.items() if "projection" in key or "projector_type" in key}
proof = {"composition": "CUSTOM copied existing Gemma4 E4B decoder + official E4B projector; not official gemma4:e4b", "expected_core_sha256": "90ce98129eb3e8cc57e62433d500c97c624b1e3af1fcc85dd3b55ad7e0313e9f", "expected_projector_sha256": "4cb21b935cd7e79daed76f3154e82f2f160ad7a9d8d22cb5f14c7b93d5fb38ba", "core": core, "projector": projector, "projection_dimensions": projection_dimensions, "actual_vision_gate_still_required": True, "partial_metadata_only": args.partial}
if not args.partial:
    for name, file in (("core", "gemma4-core.gguf"), ("projector", "mmproj.gemma4-e4b.gguf")):
        with (ROOT / ".runtime/import" / file).open("rb") as stream:
            measured = hashlib.file_digest(stream, "sha256").hexdigest()
        if measured != proof[f"expected_{name}_sha256"]:
            raise SystemExit(f"{name} hash mismatch")
        proof[f"{name}_sha256"] = measured
destination = ROOT / ("proof/vision/custom-metadata-partial.json" if args.partial else "proof/vision/custom-composition.json")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(proof, indent=2) + "\n")
print(json.dumps(proof, indent=2))
