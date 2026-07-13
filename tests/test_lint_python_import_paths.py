from pathlib import Path

from scripts.lint_python_import_paths import scan_python


def test_ast_lint_rejects_search_path_mutation(tmp_path: Path) -> None:
    source = tmp_path / "bad.py"
    source.write_text("import sys\nsys.path.insert(0, 'x')\n", encoding="utf-8")
    findings = scan_python(source)
    assert len(findings) == 1
    assert "sys.path" in findings[0].message


def test_ast_lint_rejects_pythonpath_environment_writes(tmp_path: Path) -> None:
    source = tmp_path / "bad.py"
    source.write_text("import os\nos.environ['PYTHONPATH'] = 'x'\n", encoding="utf-8")
    assert scan_python(source)


def test_ast_lint_ignores_comments_and_strings(tmp_path: Path) -> None:
    source = tmp_path / "okay.py"
    source.write_text("# sys.path.insert is forbidden\nmessage = 'os.environ[\\\"PYTHONPATH\\\"]'\n", encoding="utf-8")
    assert scan_python(source) == []
