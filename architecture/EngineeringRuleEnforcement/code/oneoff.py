# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Rule-006 v2 oneoff wrapper.

The ONLY legal path for Claude to run a command that mutates remote state
outside the four governed chain surfaces (git commit, promote_chathealthy.py,
build_chathealthy.py, deploy_chathealthy.py).

Usage:
    python architecture/EngineeringRuleEnforcement/code/oneoff.py \\
      --explanation "<Claude's own words, <=70 words, describing exactly \\
      what remote state changes and why>" \\
      -- <actual command...>

Rule-006 v2 (llm_classify_shell_invocation_worker.py) detects the wrapper
on the top-level command surface at PreToolUse. If found, the classifier:
  - Reads Claude's --explanation
  - Reads the inner command and every project script it transitively touches
  - Verifies the explanation truthfully describes what the inner command does
  - On honest match: gate_approve → browser popup with command + explanation
    → operator types APPROVE or REJECT
  - On mismatch/evasion: reject with notification

Without this wrapper, any non-chain remote-mutating command is auto-rejected
by Rule-006 v2 with a passive browser notification.

The wrapper itself does nothing more than validate its own arguments and
exec the inner command. All governance happens at Rule-006 v2's PreToolUse
classifier, BEFORE the wrapper ever runs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_EXPLANATION_MAX_WORDS = 70
_EXPLANATION_MIN_WORDS = 4


def _word_count(s: str) -> int:
    return len([w for w in s.strip().split() if w])


def main(argv: list[str]) -> int:
    # We split argv on '--' ourselves — argparse cannot easily preserve
    # everything after '--' as a single command with its own flags.
    if "--" not in argv:
        sys.stderr.write(
            "oneoff.py: missing `--` separator between wrapper flags and the "
            "inner command.\nUsage: oneoff.py --explanation \"...\" -- "
            "<actual command...>\n"
        )
        return 2
    dash_idx = argv.index("--")
    wrapper_argv = argv[:dash_idx]
    inner_argv = argv[dash_idx + 1:]

    parser = argparse.ArgumentParser(
        prog="oneoff.py",
        description="Rule-006 v2 wrapper for non-chain remote mutations.",
    )
    parser.add_argument(
        "--explanation",
        required=True,
        help=(
            f"<={_EXPLANATION_MAX_WORDS} words: plain-English description of "
            "exactly what remote state this command mutates and why. Shown to "
            "the operator on the approval popup."
        ),
    )
    ns = parser.parse_args(wrapper_argv)

    explanation = (ns.explanation or "").strip()
    wc = _word_count(explanation)
    if wc < _EXPLANATION_MIN_WORDS:
        sys.stderr.write(
            f"oneoff.py: --explanation too short ({wc} word(s); minimum "
            f"{_EXPLANATION_MIN_WORDS}). Describe the remote-state effect.\n"
        )
        return 2
    if wc > _EXPLANATION_MAX_WORDS:
        sys.stderr.write(
            f"oneoff.py: --explanation too long ({wc} words; maximum "
            f"{_EXPLANATION_MAX_WORDS}). Tighten it.\n"
        )
        return 2
    if not inner_argv:
        sys.stderr.write("oneoff.py: no inner command after `--`.\n")
        return 2

    # If we reach here, Rule-006 v2's PreToolUse hook already approved the
    # invocation (either allow OR gate_approve+APPROVE). Just exec the inner
    # command with the current environment.
    try:
        result = subprocess.run(inner_argv)
        return result.returncode
    except FileNotFoundError as exc:
        sys.stderr.write(f"oneoff.py: inner command not found: {exc}\n")
        return 127
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
