from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "recipes"))
import _apple_sdk_thin as thin  # noqa: E402


class _Log:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


def test_prune_manpages_removes_only_optional_documentation(tmp_path: Path):
    sdk = tmp_path / "sdk"
    man = sdk / "usr" / "share" / "man"
    man.mkdir(parents=True)
    (man / "mann").mkdir()
    # Windows cannot create the colon-bearing names found in the source SDK,
    # so use a representative manpage filename for the pruning contract.
    (man / "mann" / "ttk-progressbar.ntcl").write_text("man")
    lib = sdk / "usr" / "lib"
    lib.mkdir(parents=True)
    (lib / "libSystem.tbd").write_text("keep")

    log = _Log()
    removed = thin.prune_manpages(sdk, log)

    assert removed >= 2
    assert not man.exists()
    assert (lib / "libSystem.tbd").read_text() == "keep"
    assert any("manpage" in message for message in log.messages)


def test_prune_manpages_is_noop_when_sdk_has_no_man_dir(tmp_path: Path):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    log = _Log()
    assert thin.prune_manpages(sdk, log) == 0
    assert log.messages == []
