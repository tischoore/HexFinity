"""Static guard: every GPU 'POINTS' batch in hexfinity/ must be drawn with a
shader built from 'POINT_UNIFORM_COLOR'.

The generic 'UNIFORM_COLOR' built-in never writes a point size, so on the
Vulkan/Metal GPU backends Blender 5.x uses, gpu.state.point_size_set() is
ignored and the dots render invisibly (the "snap dot disappeared while
drawing a path" bug). This is a source scan, not a render test — bpy/gpu
can't be imported in plain CPython.
"""
import ast
import pathlib

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent / "hexfinity"


def _points_batch_shader_names(func):
    """Names of the shader variable passed to batch_for_shader(<name>, 'POINTS', ...)."""
    names = []
    for node in ast.walk(func):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "batch_for_shader"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "POINTS"):
            arg = node.args[0]
            names.append((arg.id if isinstance(arg, ast.Name) else None, node.lineno))
    return names


def _builtin_assignments(func):
    """{var_name: builtin shader name} for `x = gpu.shader.from_builtin('...')` in `func`."""
    out = {}
    for node in ast.walk(func):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "from_builtin"
                and node.value.args
                and isinstance(node.value.args[0], ast.Constant)):
            out.setdefault(node.targets[0].id, set()).add(node.value.args[0].value)
    return out


def _cases():
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for name, lineno in _points_batch_shader_names(func):
                    yield pytest.param(path.name, func, name, lineno,
                                       id=f"{path.name}:{lineno}")


CASES = list(_cases())


def test_scan_found_point_draws():
    # Sanity: the scan must actually see the known draw sites.
    assert len(CASES) >= 5


@pytest.mark.parametrize("fname,func,name,lineno", CASES)
def test_points_use_point_uniform_color(fname, func, name, lineno):
    assert name is not None, f"{fname}:{lineno} POINTS batch shader isn't a plain variable"
    builtins = _builtin_assignments(func).get(name)
    assert builtins == {"POINT_UNIFORM_COLOR"}, (
        f"{fname}:{lineno} draws POINTS with `{name}` built from {builtins}; "
        "use gpu.shader.from_builtin('POINT_UNIFORM_COLOR') in the same function")
