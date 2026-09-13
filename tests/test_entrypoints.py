import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize("path", [Path("sender/sender.py"), Path("receiver/receiver.py")])
def test_module_main_is_guarded(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))

    unguarded_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "main"
    ]
    assert not unguarded_calls

    guarded = any(
        isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "main"
            for child in ast.walk(node)
        )
        for node in tree.body
    )
    assert guarded
