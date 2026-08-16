import json
from pathlib import Path
import pytest
from scripts.validate_rust_tools import validate

ROOT = Path(__file__).parents[1]


def test_pinned_vertical_slice_has_dylint_triplet_and_eight_platforms():
    doc = validate(ROOT / "managed-rust-tools.json")
    assert set(doc["tools"]) == {
        "cargo-binstall",
        "cargo-nextest",
        "cargo-dylint",
        "dylint-link",
        "dylint-driver",
        "soldr-maturin",
    }
    assert len(doc["platforms"]) == 8
    assert doc["tools"]["dylint-driver"]["driver_identity"] == {
        "dylint_version": "6.0.3",
        "toolchain": "nightly-2026-05-28",
        "rustc_release": "1.98.0-nightly",
        "rustc_commit": "57d06900fd7d9ee06d3a7f323bb77f17ab3cfaf8",
    }


def test_latest_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platforms": [str(i) for i in range(8)],
                "tools": {
                    "cargo-binstall": {
                        "version": "latest",
                        "source": "x",
                        "binary": "x",
                    },
                    "cargo-nextest": {"version": "1", "source": "x", "binary": "x"},
                },
            }
        )
    )
    with pytest.raises(ValueError):
        validate(path)
