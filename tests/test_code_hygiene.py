"""Whole-package checks for bugs that tests of individual features can miss."""
import ast
import symtable
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "ixel_mat"


def _module_imports(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return names


def _functions(table):
    for child in table.get_children():
        if child.get_type() == "function":
            yield child
        yield from _functions(child)


def test_no_function_shadows_a_module_import():
    # e.g. `stats = describe_large_paste(...)` in mat.main() made every `stats.`
    # in that function (the /saves command) an UnboundLocalError
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        imported = _module_imports(ast.parse(source))
        for func in _functions(symtable.symtable(source, str(path), "exec")):
            for sym in func.get_symbols():
                if sym.get_name() in imported and sym.is_local() and sym.is_assigned() and not sym.is_imported():
                    problems.append(f"{path.relative_to(PACKAGE.parent)}: {func.get_name()}() assigns "
                                    f"'{sym.get_name()}', hiding the module-level import")
    assert not problems, "\n".join(problems)


def test_help_shows_every_command_usage_verbatim():
    # "[flags]" in a usage string was swallowed as a Rich style tag
    from ixel_mat import cli, mat
    from ixel_mat.commands import build_help_rows

    for module, show, mode in ((cli, cli.cmd_help, "cli"), (mat, mat.print_help, "mat")):
        with module.console.capture() as captured:
            show()
        text = captured.get()
        for usage, _ in build_help_rows(mode):
            assert usage in text, (mode, usage)


def test_text_files_are_opened_with_an_explicit_encoding():
    # Windows' default is the ANSI code page: `ixel setup` wrote config.toml in
    # cp1252 and the loader (UTF-8) couldn't read it back
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            keywords = {k.arg for k in node.keywords}
            # importlib.metadata's distribution(...).read_text(name) takes no encoding: it reads UTF-8 itself
            on_metadata = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call)
                           and getattr(func.value.func, "attr", getattr(func.value.func, "id", "")) == "distribution")
            if name in ("read_text", "write_text") and "encoding" not in keywords and not on_metadata:
                problems.append(f"{path.name}:{node.lineno} {name}() without encoding=")
            if name == "open" and isinstance(func, ast.Name) and "encoding" not in keywords:
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if not (isinstance(mode, ast.Constant) and "b" in str(mode.value)):
                    problems.append(f"{path.name}:{node.lineno} open() in text mode without encoding=")
    assert not problems, "\n".join(problems)
