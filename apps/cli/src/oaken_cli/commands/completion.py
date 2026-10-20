# SPDX-License-Identifier: Apache-2.0
"""``oaken completion bash|zsh|fish``: a completion script generated from the real parser.

The command names and options are read from :func:`oaken_cli.main.build_parser`, so a command added
to the CLI is completed with no second list to keep in step. Operation names for ``oaken operation``
are not completed (they come from the running core).
"""

from __future__ import annotations

import argparse
import sys

from oaken_cli.exits import ExitCode


def _commands() -> dict[str, list[str]]:
    from oaken_cli.main import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    found: dict[str, list[str]] = {}
    for name, p in sub.choices.items():
        opts = [o for a in p._actions for o in a.option_strings if o.startswith("--")]
        nested = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
        found[name] = sorted(set(opts) | (set(nested.choices) if nested else set()))
    return found


def bash(commands: dict[str, list[str]]) -> str:
    cases = "\n".join(f'    {name}) opts="{" ".join(opts)}" ;;' for name, opts in commands.items())
    return f"""_oaken() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}" cmd="${{COMP_WORDS[1]}}" opts=""
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "{" ".join(commands)} --help --version" -- "$cur") )
    return
  fi
  case "$cmd" in
{cases}
  esac
  COMPREPLY=( $(compgen -W "$opts --json --url --token-file --verbose --help" -- "$cur") )
}}
complete -F _oaken oaken
"""


def zsh(commands: dict[str, list[str]]) -> str:
    return "autoload -U +X bashcompinit && bashcompinit\n" + bash(commands)


def fish(commands: dict[str, list[str]]) -> str:
    lines = [f"complete -c oaken -f -n __fish_use_subcommand -a {name}" for name in commands]
    for name, opts in commands.items():
        for opt in opts:
            flag = f"-l {opt[2:]}" if opt.startswith("--") else f"-a {opt}"
            lines.append(f"complete -c oaken -f -n '__fish_seen_subcommand_from {name}' {flag}")
    return "\n".join(lines) + "\n"


def cmd_completion(args: argparse.Namespace) -> int:
    commands = _commands()
    script = {"bash": bash, "zsh": zsh, "fish": fish}[args.shell](commands)
    sys.stdout.write(script)
    return int(ExitCode.OK)
