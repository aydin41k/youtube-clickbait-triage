#!/usr/bin/env bash
# Throwaway POC: never modifies the system Ollama installation or model cache.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"
if [[ $# -gt 1 ]] || [[ $# -eq 1 && "$1" != --official-fallback ]]; then
  echo 'Usage: ./scripts/setup-local-ollama.sh [--official-fallback]' >&2
  exit 2
fi
runtime_dir="$project_dir/.runtime"
mkdir -p "$runtime_dir" "$project_dir/.models"
exec 9>"$runtime_dir/ownership.lock"
flock -n 9 || { echo 'Private runtime busy: another project session owns it.' >&2; exit 1; }
archive="$runtime_dir/ollama-linux-amd64-v0.40.10.tar.zst"
if [[ ! -x "$runtime_dir/bin/ollama" ]]; then
  zstd_binary="$(command -v zstd || true)"
  if [[ -z "$zstd_binary" ]]; then
    echo 'zstd is required to extract the pinned runtime.' >&2
    exit 1
  fi
  curl --fail --location --retry 2 --continue-at - --output "$archive" https://github.com/ollama/ollama/releases/download/v0.40.10/ollama-linux-amd64.tar.zst
  echo '1e8ad2d2a394efdd235bee74deec237badd95142ffba77588140ab2a9ecec227  '"$archive" | sha256sum --check
  tar --use-compress-program="$zstd_binary" -xf "$archive" -C "$runtime_dir"
fi
if (exec 8<>/dev/tcp/127.0.0.1/11435) 2>/dev/null; then
  echo 'Port 11435 is occupied; refusing to commandeer an existing server.' >&2
  exit 1
fi
export OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS="$project_dir/.models" OLLAMA_NO_CLOUD=1 OLLAMA_NOHISTORY=1 OLLAMA_DEBUG_LOG_REQUESTS=0
"$runtime_dir/bin/ollama" serve 9>&- >"$runtime_dir/setup-server.log" 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true' EXIT
for ((i=0;i<60;i++)); do
  kill -0 "$server_pid" 2>/dev/null || { echo 'Owned setup server exited.' >&2; exit 1; }
  if curl --silent --fail http://127.0.0.1:11435/api/version; then
    kill -0 "$server_pid" 2>/dev/null || exit 1
    break
  fi
  sleep 1
done
curl --silent --fail http://127.0.0.1:11435/api/version >/dev/null
if [[ "${1:-}" == --official-fallback ]]; then
  # Explicitly resumable alternative download only; never a hidden model switch.
  "$runtime_dir/bin/ollama" pull gemma4:e4b
  "$runtime_dir/bin/ollama" show gemma4:e4b
else
  ./scripts/prepare-custom-gemma.sh
  uv run python scripts/check-gguf-compatibility.py
  "$runtime_dir/bin/ollama" create youtube-triage-gemma4-e4b:poc -f CustomGemma.Modelfile
  "$runtime_dir/bin/ollama" show youtube-triage-gemma4-e4b:poc
fi
