#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

podman build -t small-llm-test -f Containerfile .

podman run --rm \
  --read-only \
  --tmpfs /tmp \
  --cap-drop=all \
  --security-opt no-new-privileges \
  --pids-limit=256 \
  --memory=3g \
  --cpus=6 \
  --network=slirp4netns \
  --user 10001:10001 \
  -v small-llm-test-cache:/home/appuser/.cache/huggingface \
  small-llm-test
