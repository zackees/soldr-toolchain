"""Guards for .github/workflows/forge-ingest.yml.

The workflow is orchestration, but two of its details are load-bearing and
were wrong in production: it staged a version directory that does not exist
for `v`-prefixed tools (aborting the job before the push), and a rebuild of an
already-published version needs its own branch name.
"""

from __future__ import annotations

from pathlib import Path

from scripts import forge_to_catalogue as fc

WORKFLOW = (
    Path(fc.__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "forge-ingest.yml"
)


def test_workflow_stages_both_version_directory_spellings() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '"${{ inputs.tool }}/${{ inputs.version }}"' in text
    assert '"${{ inputs.tool }}/v${{ inputs.version }}"' in text
    # Tools whose catalogue directory keeps the `v` prefix are exactly why the
    # second spelling is needed.
    assert fc._catalog_version("cargo-chef", "0.1.73").startswith("v")
    assert fc._catalog_version("crgx", "0.1.0").startswith("v")
    assert not fc._catalog_version("cargo-nextest", "0.9.140").startswith("v")


def test_workflow_exposes_the_build_label() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "build_label:" in text
    assert "--build-label" in text
    # A labelled rebuild must not collide with the original ingest branch.
    assert "label_suffix" in text
