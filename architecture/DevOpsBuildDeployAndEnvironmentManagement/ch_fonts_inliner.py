"""Inline the canonical CH_FONTS snippet into HTML build outputs.

Used at deploy time only. Never writes into the repo source tree —
caller passes paths in a staging / dist directory.
"""
from __future__ import annotations
import shutil
from pathlib import Path
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

MARKER = "<!-- CH_FONTS -->"
_SNIPPET_PATH = (
    Path(__file__).resolve().parents[1] / "CommonFrontEndFeatures" / "ch_fonts_snippet.html"
)


def read_snippet() -> str:
    if not _SNIPPET_PATH.is_file():
        raise ChatHealthyException(
            mode="file_missing",
            component="ch_fonts_inliner",
            message=f"canonical font snippet missing at {_SNIPPET_PATH}")
    return _SNIPPET_PATH.read_text(encoding="utf-8").rstrip("\n")


def inline_into(html_path: Path, snippet: str | None = None) -> bool:
    if snippet is None:
        snippet = read_snippet()
    text = html_path.read_text(encoding="utf-8")
    if MARKER not in text:
        return False
    html_path.write_text(text.replace(MARKER, snippet), encoding="utf-8")
    return True


def stage_and_inline(src_dir: Path, staging_dir: Path) -> int:
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    shutil.copytree(src_dir, staging_dir)
    snippet = read_snippet()
    count = 0
    for html in staging_dir.rglob("*.html"):
        if inline_into(html, snippet):
            count += 1
    return count
