# SPDX-License-Identifier: Apache-2.0
"""Move or rename a file in the repository and fix everything that refers to it.

    uv run python docs/tools/move_doc.py docs/prompts/CORE_28-compose-and-send.md \\
        docs/prompts/CORE_28-send.md [--dry-run]

It does ``git mv`` and then rewrites, in every text file git tracks (Markdown, Python, TOML, JSON,
YAML, plain text; never the generated HTML, which ``build_site.py`` rebuilds):

* **Markdown links** that resolve to the old file, as a link relative to the file it is in;
* **the file's own relative links**, so they still resolve from the new place;
* **repository paths** (``docs/prompts/CORE_28-compose-and-send.md``) written in prose or code;
* **bare file names** (``CORE_28-compose-and-send.md``), but only when that name belongs to no
  other tracked file, so ``README.md`` is never rewritten by guesswork.

Then it says how many references it fixed and in how many files. Rebuild the site afterwards
(``build_site.py``) and run ``check_docs.py``, which also fails on any prompt reference that
dangles. A move never overwrites a file that exists.
"""

import argparse
import os
import re
import subprocess  # noqa: S404 (git, fixed arguments)
import sys
from dataclasses import dataclass
from pathlib import Path

TEXT = {".md", ".py", ".toml", ".json", ".yml", ".yaml", ".txt"}
LINK = re.compile(r"(\]\()([^)\s#]+)(#[^)\s]*)?(\))")
SKIP = {"uv.lock"}


class MoveError(Exception):
    pass


@dataclass
class Result:
    moved: tuple[str, str]
    references: int
    files: list[str]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603, S607
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _tracked(root: Path) -> list[str]:
    try:
        return [n for n in _git(root, "ls-files").split("\n") if n]
    except (OSError, subprocess.CalledProcessError):
        return [
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        ]


def _rel(root: Path, p: str) -> str:
    path = (root / p).resolve()
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        raise MoveError(f"{p} is outside the repository") from None


def _is_relative_link(target: str) -> bool:
    return not re.match(r"^[a-z][a-z0-9+.-]*:", target) and not target.startswith(("/", "#"))


def _link_to(from_dir: str, target: str) -> str:
    return os.path.relpath(target, from_dir or ".").replace(os.sep, "/")


def _rewrite(
    text: str, here_old: str, here_new: str, old: str, new: str, bare: bool
) -> tuple[str, int]:
    """``text`` of the file that was at ``here_old`` and is now at ``here_new``, with every
    reference to ``old`` made to point at ``new``. Returns it and how many references changed."""
    count = 0
    old_dir, new_dir = os.path.dirname(here_old), os.path.dirname(here_new)

    def link(m: re.Match[str]) -> str:
        nonlocal count
        target = m.group(2)
        if not _is_relative_link(target):
            return m.group(0)
        resolved = os.path.normpath(os.path.join(old_dir, target)).replace(os.sep, "/")
        goes_to = new if resolved == old else resolved
        out = _link_to(new_dir, goes_to)
        if out == target:
            return m.group(0)
        count += 1
        return f"{m.group(1)}{out}{m.group(3) or ''}{m.group(4)}"

    text = LINK.sub(link, text)
    pat = re.compile(rf"(?<![A-Za-z0-9_./-]){re.escape(old)}(?![A-Za-z0-9_-])")
    text, n = pat.subn(new, text)
    count += n
    if bare and os.path.basename(old) != os.path.basename(new):
        pat = re.compile(
            rf"(?<![A-Za-z0-9_./-]){re.escape(os.path.basename(old))}(?![A-Za-z0-9_-])"
        )
        text, n = pat.subn(os.path.basename(new), text)
        count += n
    return text, count


def move(root: Path, old_arg: str, new_arg: str, *, dry_run: bool = False) -> Result:
    root = root.resolve()
    old, new = _rel(root, old_arg), _rel(root, new_arg)
    if not (root / old).is_file():
        raise MoveError(f"{old} is not a file")
    if (root / new).exists():
        raise MoveError(f"{new} already exists; a move never overwrites")
    tracked = _tracked(root)
    names = [os.path.basename(t) for t in tracked]
    bare = names.count(os.path.basename(old)) == 1  # a name nobody else has can be found by name
    if not dry_run:
        (root / new).parent.mkdir(parents=True, exist_ok=True)
        try:
            _git(root, "mv", old, new)
        except (OSError, subprocess.CalledProcessError):
            (root / old).rename(root / new)
    total, touched = 0, []
    for rel in tracked:
        path_now = new if rel == old else rel
        if Path(path_now).suffix not in TEXT or rel in SKIP or rel.endswith(".html"):
            continue
        source = root / (old if dry_run and rel == old else path_now)
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        fixed, n = _rewrite(text, rel, path_now, old, new, bare)
        if n:
            total += n
            touched.append(path_now)
            if not dry_run:
                source.write_text(fixed, encoding="utf-8")
    return Result((old, new), total, sorted(touched))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    ap.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = ap.parse_args(argv)
    try:
        result = move(args.repo_root, args.old, args.new, dry_run=args.dry_run)
    except MoveError as exc:
        print(f"FAIL: {exc}")
        return 1
    verb = "would move" if args.dry_run else "moved"
    print(f"{verb} {result.moved[0]} -> {result.moved[1]}")
    print(f"{result.references} reference(s) in {len(result.files)} file(s)")
    for f in result.files:
        print(f"  {f}")
    if not args.dry_run:
        print(
            "next: uv run --group docs python docs/tools/build_site.py && "
            "uv run --group docs python docs/tools/check_docs.py"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
