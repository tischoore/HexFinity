import sys
from pathlib import Path

# Import the bpy-free submodules directly, bypassing hexfinity/__init__.py
# (which registers Blender classes and isn't loadable in plain CPython).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hexfinity"))
