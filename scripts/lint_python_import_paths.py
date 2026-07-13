#!/usr/bin/env python3
"""Reject runtime Python import-path mutation in the toolchain repository.

Repository tooling is a package rooted at the checkout. Conan entrypoints are
the one unusual boundary: they load helpers from their export directory using
``importlib`` and therefore do not need to mutate the interpreter search path.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _is_name(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Name) and node.id == value


def _is_sys_path(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and _is_name(node.value, "sys")
    )


def _is_pythonpath_subscript(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    if not (isinstance(node.value, ast.Attribute) and node.value.attr == "environ"):
        return False
    if not _is_name(node.value.value, "os"):
        return False
    key = node.slice.value if isinstance(node.slice, ast.Constant) else None
    return key == "PYTHONPATH"


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[Finding] = []

    def _add(self, node: ast.AST, message: str) -> None:
        self.findings.append(Finding(self.path, getattr(node, "lineno", 1), message))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"insert", "append", "extend"}:
            if _is_sys_path(node.func.value):
                self._add(node, "mutates sys.path; use a package/module import or explicit importlib loading")
            if isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "environ" and _is_name(node.func.value.value, "os"):
                self._add(node, "mutates os.environ; do not inject PYTHONPATH at runtime")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._check_target(target)
        self.generic_visit(node)

    def _check_target(self, target: ast.AST) -> None:
        if _is_sys_path(target):
            self._add(target, "assigns sys.path; use a package/module import or explicit importlib loading")
        elif _is_pythonpath_subscript(target):
            self._add(target, "writes os.environ['PYTHONPATH']; use the package environment instead")


def scan_python(path: Path) -> list[Finding]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [Finding(path, 1, f"cannot parse Python source: {exc}")]
    visitor = _PythonVisitor(path)
    visitor.visit(tree)
    return visitor.findings


def tracked_python_files(root: Path) -> Iterable[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return (root / line for line in result.stdout.splitlines() if line)


def scan_repository(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in tracked_python_files(root):
        findings.extend(scan_python(path))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        for number, line in enumerate(pyproject.read_text(encoding="utf-8").splitlines(), 1):
            if "pytest.pythonpath" in line:
                findings.append(Finding(pyproject, number, "pytest.pythonpath changes import resolution; import scripts as a package"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    findings = scan_repository(args.root.resolve())
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"{len(findings)} forbidden import-path mutation(s) found")
        return 1
    print("python import-path lint: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
