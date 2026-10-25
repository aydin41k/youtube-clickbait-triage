"""Register exactly one unpacked extension in the current user's Chrome profile."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
HOST = "com.youtube_clickbait_triage.local"


def install(extension_id, directory):
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("extension ID must be exactly 32 lowercase letters a-p")
    launcher = ROOT / "run-native-host"
    if not launcher.is_file() or not launcher.stat().st_mode & 0o111:
        raise ValueError("executable run-native-host is missing")
    if not (ROOT / ".venv/bin/python").is_file():
        raise ValueError("project interpreter missing: run uv sync first")
    manifest = {"name": HOST, "description": "Local Gemma YouTube packaging triage",
                "path": str(launcher), "type": "stdio",
                "allowed_origins": [f"chrome-extension://{extension_id}/"]}
    destination = directory / f"{HOST}.json"
    if destination.exists():
        if json.loads(destination.read_text()) != manifest:
            raise ValueError(f"conflicting registration: explicitly remove {destination} before replacing it")
        return destination
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive creation refuses races/conflicts; no unrelated manifest is touched.
    with destination.open("x") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extension_id", help="ID copied from chrome://extensions")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/google-chrome/NativeMessagingHosts",
                        help="native-host directory (default: current-user Google Chrome)")
    args = parser.parse_args()
    try:
        print(install(args.extension_id, args.config_dir))
    except (ValueError, OSError) as exc:
        print(f"Registration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
