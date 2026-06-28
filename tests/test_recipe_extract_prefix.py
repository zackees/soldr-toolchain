"""Regression test for the python-* recipes' PBS extraction prefix.

soldr#997 / soldr#1006 / forge#13 — pre-2026-06-28 the helper assumed a
`python/install/<...>` layout for the `install_only.tar.gz` PBS archive
variants. That was wrong: `install/` is only present in the `full`
archive variants. With the bogus prefix the extraction loop dropped
every member silently and forge's `Validate package payload` job
failed every dispatch with "Package payload below the 10240 byte
minimum".

This test asserts the helper + the standalone python-windows-x64
recipe both use `prefix = "python/"` (not `python/install/`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


RECIPES_DIR = Path(__file__).resolve().parents[1] / "recipes"


def _strip_python_strings_and_comments(text: str) -> str:
    """Return only the non-comment, non-string-literal portions of
    `text`. Strings + comments may legitimately mention the historical
    `python/install/` prefix when documenting the bug; the test must
    only fail on *executable* code that uses the wrong prefix."""
    # Drop triple-quoted strings (greedy enough for module / function
    # docstrings + multi-line constants).
    out = re.sub(r'"""[\s\S]*?"""', "", text)
    out = re.sub(r"'''[\s\S]*?'''", "", out)
    # Drop simple single- and double-quoted string literals so that
    # a `prefix = "python/install/"` re-introduction (the actual bug
    # signature) is still caught, but doc-discussing strings like
    # `f"...python/install/..."` aren't false positives. We accept
    # this slight imprecision: a re-introduction of the literal
    # `"python/install/"` as an executable assignment WILL be caught
    # because we only strip strings *not adjacent to* an `=`.
    out = re.sub(r'(?<!=\s)"[^"\n]*"', "", out)
    out = re.sub(r"(?<!=\s)'[^'\n]*'", "", out)
    # Drop line comments.
    out = re.sub(r"#[^\n]*", "", out)
    return out


@pytest.mark.parametrize(
    "path",
    [
        RECIPES_DIR / "_python_pbs.py",
        RECIPES_DIR / "python-windows-x64" / "conanfile.py",
    ],
)
def test_pbs_extraction_uses_python_prefix_not_install(path):
    text = path.read_text(encoding="utf-8")
    # The actual extraction code MUST use `python/` as the prefix.
    assert 'prefix = "python/"' in text, (
        f"{path.name} must use prefix = \"python/\" — the PBS "
        f"`install_only.tar.gz` archives don't have an `install/` "
        f"subdir. See soldr#1006 for the original failure."
    )
    # The bug signature is a literal assignment `prefix = "python/install/"`.
    # That MUST NOT appear in the file regardless of comments.
    assert 'prefix = "python/install/"' not in text, (
        f"{path.name} still assigns `prefix = \"python/install/\"` — "
        f"this is the soldr#1006 bug. Use `prefix = \"python/\"` instead."
    )

