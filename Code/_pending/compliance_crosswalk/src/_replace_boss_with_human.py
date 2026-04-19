# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# One-shot: replace every standalone "human" (any case, word-bounded) with
# "human" — capitalized only if at start of sentence / header / line.
# Per Human directive 2026-04-19. Scope: .json, .py, .md across the
# project repo AND the per-conversation memory directory.
#
# Word-boundary matching (\bboss\b) protects against false positives like
# "embossed", "bosses", "Skip Snow's human" (skip is a proper name; not
# touched). Possessive forms like "human's" become "human's"/"Human's"
# automatically since the apostrophe isn't part of the match.

from __future__ import annotations

import re
from pathlib import Path

ROOTS = [
    Path(r"c:/chatHealthy/findCare"),
    Path(r"C:/Users/skips/.claude/projects/c--chatHealthy-findCare/memory"),
]
EXTS = {".json", ".py", ".md"}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv",
             ".pytest_cache", "dist", "build", "out"}

WORD = re.compile(r"\bboss\b", re.IGNORECASE)


def _is_sentence_or_header_start(text: str, idx: int) -> bool:
    """Return True if position idx (start of a 'human' match) is at the
    start of a sentence (preceded only by whitespace and a sentence
    terminator . ! ? : or newline) OR start of a markdown header line
    (line starts with one or more # characters)."""
    # Find start of the line containing idx
    line_start = text.rfind("\n", 0, idx) + 1
    line_prefix = text[line_start:idx]
    stripped_prefix = line_prefix.lstrip()
    # Markdown header
    if stripped_prefix.startswith("#") and not stripped_prefix.lstrip("#").lstrip().startswith("`"):
        # The match is on the header line; capitalize if it's the first
        # word after the # marker (or after a colon/period in the header)
        before_match = stripped_prefix.lstrip("#").lstrip()
        if before_match == "" or before_match.endswith((":", ".", "!", "?", "—", "-")):
            return True
    # Beginning of file
    if idx == 0:
        return True
    # Walk back over whitespace
    j = idx - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0:
        return True
    c = text[j]
    if c in ".!?\n:":
        return True
    # Bullet list start: line begins with a list marker followed by space
    if not line_prefix.strip(" \t-*+0123456789.)>"):
        # Whole prefix is bullet markers + whitespace
        return True
    return False


def _replace(match: re.Match, full_text: str) -> str:
    word = match.group(0)
    idx = match.start()
    # Preserve ALL-CAPS distinctly
    if word.isupper() and len(word) >= 2:
        return "HUMAN"
    if _is_sentence_or_header_start(full_text, idx):
        return "Human"
    return "human"


def process(p: Path) -> int:
    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return 0
    if not WORD.search(text):
        return 0
    new_text = WORD.sub(lambda m: _replace(m, text), text)
    if new_text == text:
        return 0
    p.write_text(new_text, encoding="utf-8")
    return len(WORD.findall(text))


def main():
    total = 0
    changed_files: list[tuple[Path, int]] = []
    for root in ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in EXTS:
                continue
            if any(d in p.parts for d in SKIP_DIRS):
                continue
            n = process(p)
            if n > 0:
                changed_files.append((p, n))
                total += n
    print(f"Total replacements: {total}")
    print(f"Files changed: {len(changed_files)}")
    print()
    for p, n in sorted(changed_files, key=lambda x: -x[1])[:40]:
        print(f"  {n:>4}  {p}")


if __name__ == "__main__":
    main()
