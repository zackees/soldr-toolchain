import json
from pathlib import Path
import pytest
from scripts.validate_rust_tools import validate

ROOT = Path(__file__).parents[1]


def test_pinned_vertical_slice_has_two_tools_and_eight_platforms():
    doc = validate(ROOT / "managed-rust-tools.json")
    assert set(doc["tools"]) == {"cargo-binstall", "cargo-nextest"}
    assert len(doc["platforms"]) == 8


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
