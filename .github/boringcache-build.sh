#!/usr/bin/env bash
set -euo pipefail
python3 .github/boringcache-time.py build uv run --no-default-groups python scripts/build/build_macos.py --arch arm64 --skip-verify
python3 .github/boringcache-time.py verify scripts/build/verify_built_binary.sh dist/nuitka/lintro
python3 .github/boringcache-time.py smoke python3 scripts/ci/smoke-test-binary.py dist/nuitka/lintro
du -sk .nuitka-cache > "$RUNNER_TEMP/validation/cache-kib.txt"
shasum -a 256 dist/nuitka/lintro > "$RUNNER_TEMP/validation/binary.sha256"
