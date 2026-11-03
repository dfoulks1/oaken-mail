#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Upload the built Oaken distributions (docs/releasing.md, steps 5, 7 and 8).
#
#   export UV_PUBLISH_TOKEN=pypi-...        # your token; never commit or paste it
#   scripts/release_pypi.sh test            # dry run on TestPyPI, then a clean install of it
#   scripts/release_pypi.sh pypi            # the real upload (IRREVERSIBLE: a version is never reused)
#
# Build first:  rm -rf dist; for p in oaken oaken-cli oaken-core; do uv build --package $p; done
# The three upload in dependency order, `oaken` first, and the script waits for it to be live.
set -euo pipefail
cd "$(dirname "$0")/.."

mode="${1:-}"
version="$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('src/oaken/__init__.py').read()).group(1))")"
[[ "$mode" == test || "$mode" == pypi ]] || { echo "usage: $0 test|pypi" >&2; exit 2; }
[[ -n "${UV_PUBLISH_TOKEN:-}" ]] || { echo "set UV_PUBLISH_TOKEN first" >&2; exit 2; }
for f in oaken-$version oaken_cli-$version oaken_core-$version; do
  ls dist/$f* >/dev/null 2>&1 || { echo "dist/$f* is missing: build first" >&2; exit 2; }
done
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo "the working tree is not clean" >&2; exit 2; }
[[ "$(git branch --show-current)" == main ]] || { echo "release from main" >&2; exit 2; }

uv run --with twine python -m twine check dist/*

if [[ "$mode" == test ]]; then
  url=(--publish-url https://test.pypi.org/legacy/)
else
  read -r -p "Upload oaken $version to PyPI? This cannot be undone. Type the version to go on: " ok
  [[ "$ok" == "$version" ]] || { echo "not confirmed" >&2; exit 1; }
  url=()
fi

uv publish "${url[@]}" dist/oaken-$version*
if [[ "$mode" == test ]]; then
  sleep 20  # the index needs a moment before the others can resolve `oaken`
else
  until pip index versions oaken 2>/dev/null | grep -q "$version"; do echo "waiting for oaken $version..."; sleep 10; done
fi
uv publish "${url[@]}" dist/oaken_cli-$version*
uv publish "${url[@]}" dist/oaken_core-$version*

if [[ "$mode" == test ]]; then
  uvx --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ \
      --from oaken-core oaken --version
else
  uvx --refresh --from "oaken-core==$version" oaken --version
  echo "Check the project pages: https://pypi.org/project/oaken-core/"
fi
