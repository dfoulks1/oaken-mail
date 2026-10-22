# Releasing Oaken

Oaken ships as four distributions that release **in lockstep at one version** (ADR 0014):

| Distribution | What it is |
|---|---|
| `oaken` | the engine: pipeline, API daemon, MCP server, first-party plugins |
| `oaken-cli` | the `oaken` command line (and the stdio MCP bridge) |
| `oaken-core` | the product bundle: `pip install oaken-core` → `oaken[vec]` + `oaken-cli` |
| `oaken-webui` | the web UI (a separate product on the API/CLI) |

The version is declared **once**, in `src/oaken/__init__.py` (`__version__`); the three apps read
it dynamically, and `oaken-core` pins its members to it. `tests/test_version_lockstep.py` fails if
they drift. Everything below uses `uv`.

> **The one irreversible step is `uv publish` to PyPI.** A version, once uploaded, can never be
> reused or truly deleted. Do the TestPyPI dry run first, and only upload when you mean it.

## 1. Preflight (nothing is released yet)

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run lint-imports
uv run pytest
uv run --group docs python docs/tools/check_docs.py
```

Confirm the working tree is clean (`git status`) and you are on `main` and up to date.

## 2. Set the version (one place)

Edit `src/oaken/__init__.py`:

```python
__version__ = "0.1.0"
```

Then update `oaken-core`'s pins in `apps/core/pyproject.toml` to the same version
(`oaken[vec]==X`, `oaken-cli==X`) — the lockstep test enforces this — and add a `CHANGELOG.md`
entry for the version. Re-run `uv run pytest tests/test_version_lockstep.py`.

## 3. Build all four

```bash
rm -rf dist
for pkg in oaken oaken-cli oaken-webui oaken-core; do uv build --package "$pkg"; done
ls dist/   # sdists (.tar.gz) and wheels (.whl) for all four, all at the same version
```

## 4. Verify the artifacts

```bash
uv run --with twine python -m twine check dist/*      # metadata + classifiers must all PASS
```

Spot-check that the engine wheel bundles its data (the stats template, the logo) and the plugins:

```bash
python3 -c "import zipfile,glob; z=zipfile.ZipFile(sorted(glob.glob('dist/oaken-*.whl'))[-1]); \
print([n for n in z.namelist() if 'api/templates' in n])"
```

## 5. Dry run on TestPyPI (reversible, strongly recommended)

Upload to TestPyPI and install from it in a throwaway environment, publishing in dependency order
(`oaken` first, since the others require it):

```bash
# needs a TestPyPI token in ~/.pypirc or UV_PUBLISH_TOKEN
uv publish --publish-url https://test.pypi.org/legacy/ dist/oaken-*
uv publish --publish-url https://test.pypi.org/legacy/ dist/oaken_cli-* dist/oaken_webui-*
uv publish --publish-url https://test.pypi.org/legacy/ dist/oaken_core-*

# verify a clean install of the product bundle (deps come from real PyPI, package from TestPyPI)
uvx --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ \
    --from oaken-core oaken --version
```

## 6. Tag and mark it on GitHub

```bash
git tag -a v0.1.0 -m "Oaken 0.1.0"      # annotated tag
git push origin v0.1.0
gh release create v0.1.0 --title "Oaken 0.1.0" --notes-file CHANGELOG.md --verify-tag
# a pre-release instead of a full release:  gh release create v0.1.0 --prerelease ...
```

## 7. Publish to PyPI (IRREVERSIBLE)

Only after the TestPyPI dry run looks right. Same dependency order:

```bash
# needs a PyPI token (UV_PUBLISH_TOKEN or ~/.pypirc); never commit it
uv publish dist/oaken-*
uv publish dist/oaken_cli-* dist/oaken_webui-*
uv publish dist/oaken_core-*
```

## 8. Verify the real release

```bash
uvx --from oaken-core oaken --version    # installs the published bundle and runs the CLI
```

Check the project pages render (README, classifiers, keywords) on
<https://pypi.org/project/oaken-core/> and the others.

## Notes

- **Dependency order matters** both on TestPyPI and PyPI: `oaken` must exist before `oaken-cli`,
  `oaken-webui` and `oaken-core` can resolve.
- **Credentials never live in the repo.** Use `UV_PUBLISH_TOKEN` or `~/.pypirc`; the token is the
  releaser's, not the project's.
- **Yanking, not deleting.** If a release is broken you can *yank* it on PyPI (hides it from new
  resolves) but you cannot reuse the version number. Bump to `0.1.1` and release again.
- The docs site deploys from `main` via GitHub Pages (`.github/workflows/pages.yml`), independently
  of a PyPI release.
