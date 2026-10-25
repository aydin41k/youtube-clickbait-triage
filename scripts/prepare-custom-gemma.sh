#!/usr/bin/env bash
# Supported two-FROM GGUF import; never modifies or hardlinks the global core.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
import_dir="$project_dir/.runtime/import"
mkdir -p "$import_dir"
core_source="${TRIAGE_GEMMA_CORE:-$HOME/.ollama/models/blobs/sha256-90ce98129eb3e8cc57e62433d500c97c624b1e3af1fcc85dd3b55ad7e0313e9f}"
if [[ ! -f "$import_dir/gemma4-core.gguf" ]]; then
  if [[ "$core_source" != /* ]] || [[ ! -f "$core_source" ]]; then
    echo 'Matching decoder missing: set TRIAGE_GEMMA_CORE to an absolute path to the exact documented GGUF.' >&2
    exit 1
  fi
  cp --reflink=auto "$core_source" "$import_dir/gemma4-core.gguf"
fi
echo '90ce98129eb3e8cc57e62433d500c97c624b1e3af1fcc85dd3b55ad7e0313e9f  '"$import_dir/gemma4-core.gguf" | sha256sum --check
projector="$import_dir/mmproj.gemma4-e4b.gguf"
if [[ ! -f "$projector" ]] || [[ "$(stat -c %s "$projector")" != 991552256 ]]; then
  curl --fail --location --retry 2 --continue-at - --output "$projector" https://registry.ollama.ai/v2/library/gemma4/blobs/sha256:4cb21b935cd7e79daed76f3154e82f2f160ad7a9d8d22cb5f14c7b93d5fb38ba
fi
echo '4cb21b935cd7e79daed76f3154e82f2f160ad7a9d8d22cb5f14c7b93d5fb38ba  '"$projector" | sha256sum --check
echo 'Hashes verified; custom composition prepared. The setup script will verify GGUF compatibility and import youtube-triage-gemma4-e4b:poc on the scoped server.'
