# SPDX-License-Identifier: Apache-2.0
"""The prompt series stays consistent: names, the index, the status table, and every reference.

A prompt is ``docs/prompts/TRACK_NN-name.md``. Four things must hold, and ``check_docs.check`` runs
this through the same extension hook as ``check_commands``:

* every prompt file is named that way, with a known track and a number nobody else has;
* the index (``docs/prompts/README.md``) links every prompt, and every link in it resolves;
* the status table in ``CONTRIBUTING.md`` has one row per prompt, naming its file;
* every mention of a prompt file anywhere in the repository (a link, a path, a bare file name)
  points at a file that exists, so a prompt that was renamed or removed cannot leave a dead
  reference behind. ``docs/tools/move_doc.py`` is how a file is moved without leaving one.

``00-context.md``, ``README.md`` and ``TEMPLATE.md`` are in the folder but are not prompts.
"""

import re
import subprocess  # noqa: S404 (git ls-files, a fixed command)
from pathlib import Path

from model import Config

TRACKS = ("CORE", "CLI", "MCP", "PLUGIN", "ENRICHER", "DOCS")
NOT_PROMPTS = {"00-context.md", "README.md", "TEMPLATE.md"}
NAME = re.compile(rf"^({'|'.join(TRACKS)})_(\d+[a-z]?)-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
# A mention of a prompt file: by its path, or by its bare name (anywhere in the repository).
MENTION = re.compile(
    rf"(?<![A-Za-z0-9_])(?:docs/prompts/)?((?:{'|'.join(TRACKS)})_\d+[a-z]?-[a-z0-9-]*[a-z0-9]\.md)"
)
OLD_STYLE = re.compile(r"docs/prompts/(\d+[a-z]?-[a-z0-9-]*[a-z0-9]\.md)")  # no track: stale
LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
TEXT = {".md", ".py", ".toml", ".json", ".yml", ".yaml", ".txt"}


def prompt_files(prompts: Path) -> list[Path]:
    return sorted(p for p in prompts.glob("*.md") if p.name not in NOT_PROMPTS)


def _tracked(root: Path) -> list[Path]:
    """The repository's text files (those git tracks; every text file when there is no git)."""
    try:
        out = subprocess.run(  # noqa: S603, S607
            ["git", "ls-files"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\n")
        names = [n for n in out if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    skip = ("docs/prompts/TEMPLATE.md", "uv.lock")
    return [
        root / n
        for n in names
        if Path(n).suffix in TEXT
        and n not in skip
        and not n.startswith(("docs/tools/", ".venv/"))  # the tools' docstrings show examples
        and (root / n).is_file()
    ]


def _names(prompts: Path) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}
    for p in prompt_files(prompts):
        m = NAME.match(p.name)
        if m is None:
            problems.append(
                f"docs/prompts/{p.name} is not named TRACK_NN-name.md (tracks: {', '.join(TRACKS)})"
            )
            continue
        number = m.group(2)
        if number in seen:
            problems.append(
                f"prompt {number} is used twice: {seen[number]} and {p.name}; "
                "a prompt's number is its permanent ID, so take the next free one"
            )
        seen[number] = p.name
    return problems


def _index(prompts: Path) -> list[str]:
    readme = prompts / "README.md"
    if not readme.is_file():
        return ["docs/prompts/README.md (the prompt index) is missing"]
    text = readme.read_text(encoding="utf-8")
    linked = {t for t in LINK.findall(text) if "/" not in t and t not in NOT_PROMPTS}
    problems = [
        f"docs/prompts/README.md does not link {p.name}; add its row to the index"
        for p in prompt_files(prompts)
        if p.name not in linked
    ]
    for name in sorted(linked):
        if not (prompts / name).is_file():
            problems.append(f"docs/prompts/README.md links {name}, which does not exist")
    return problems + _index_status(prompts, text)


def _index_status(prompts: Path, readme: str) -> list[str]:
    """The index's Status column must say what the status table in CONTRIBUTING.md says."""
    contributing = prompts.parents[1] / "CONTRIBUTING.md"
    if not contributing.is_file():
        return []

    def plain(t: str) -> str:
        return t.replace("**", "").strip()

    table = {
        n: plain(st)
        for n, st in re.findall(r"^\| (\d+[a-z]?) \| [A-Za-z0-9_-]+ \| ([^|]+) \|",
                                contributing.read_text("utf-8"), re.M)
    }  # fmt: skip
    problems: list[str] = []
    for number, cell in re.findall(
        r"^\| (\d+[a-z]?) \| \[[^\]]+\]\([^)]+\) \| ([^|]+) \|", readme, re.M
    ):
        if number in table and plain(cell) != table[number]:
            problems.append(
                f"docs/prompts/README.md says prompt {number} is {plain(cell)!r} but "
                f"CONTRIBUTING.md says {table[number]!r}; the status table is the one to edit"
            )
    return problems


def _status_table(root: Path, prompts: Path) -> list[str]:
    path = root / "CONTRIBUTING.md"
    if not path.is_file():
        return []
    rows = dict(
        re.findall(r"^\| (\d+[a-z]?) \| ([A-Za-z0-9_-]+) \|", path.read_text("utf-8"), re.M)
    )
    problems: list[str] = []
    stems = {p.stem for p in prompt_files(prompts)}
    by_number = {NAME.match(s + ".md").group(2): s for s in stems if NAME.match(s + ".md")}  # type: ignore[union-attr]
    for number, stem in sorted(by_number.items()):
        if number not in rows:
            problems.append(f"CONTRIBUTING.md has no status row for prompt {number} ({stem})")
        elif rows[number] != stem:
            problems.append(
                f"CONTRIBUTING.md row {number} says {rows[number]!r} but the file is {stem!r}"
            )
    for number in sorted(set(rows) - set(by_number)):
        problems.append(f"CONTRIBUTING.md has a status row for prompt {number}, which has no file")
    return problems


def _references(root: Path, prompts: Path) -> list[str]:
    problems: list[str] = []
    for path in _tracked(root):
        rel = path.relative_to(root)
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in sorted(set(MENTION.findall(text))):
            if not (prompts / name).is_file():
                problems.append(f"{rel} mentions docs/prompts/{name}, which does not exist")
        for old in sorted(set(OLD_STYLE.findall(text)) - NOT_PROMPTS):
            problems.append(
                f"{rel} mentions docs/prompts/{old}, a name from before prompts had a track"
            )
        if path.parent == prompts:  # relative links between prompts
            for target in LINK.findall(text):
                if "/" not in target and not target.startswith("http"):
                    if not (prompts / target).is_file():
                        problems.append(f"{rel} links {target}, which does not exist")
    return problems


def check(cfg: Config) -> list[str]:
    root, prompts = cfg.repo_root, cfg.repo_root / "docs" / "prompts"
    if not (prompts / "00-context.md").is_file():  # no prompt series here (a scratch docs tree)
        return []
    return [*_names(prompts), *_index(prompts), *_status_table(root, prompts),
            *_references(root, prompts)]  # fmt: skip
