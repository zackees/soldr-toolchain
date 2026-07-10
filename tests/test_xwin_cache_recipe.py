import ast
from pathlib import Path


RECIPE = (
    Path(__file__).resolve().parents[1]
    / "recipes"
    / "xwin-cache-windows-arm64"
    / "conanfile.py"
)


def _string_assignment(name: str) -> str:
    tree = ast.parse(RECIPE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            return value
    raise AssertionError(f"missing {name} assignment in {RECIPE}")


def test_xwin_pin_contains_arm64_msi_fix() -> None:
    # xwin 0.6.7 fixed upstream issue #126, where ARM64 SDK MSI files
    # without CAB payloads aborted `xwin splat`.
    assert _string_assignment("XWIN_VERSION") == "0.9.0"


def test_xwin_recipe_splats_the_arm64_sdk() -> None:
    recipe = RECIPE.read_text(encoding="utf-8")
    assert '"--arch", "aarch64"' in recipe
    assert '"--preserve-ms-arch-notation"' in recipe
    assert '"target_triple": "aarch64-pc-windows-msvc"' in recipe
