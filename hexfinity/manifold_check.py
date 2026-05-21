"""Post-build manifold verification for HexFinity tiles.

A closed 2-manifold mesh has every edge shared by exactly two faces and no
unreferenced vertices. This module raises `ManifoldError` if either condition
fails, so the operator can refuse to link a broken tile into the scene.
"""


class ManifoldError(RuntimeError):
    pass


def assert_two_manifold(verts, faces):
    edge_count = {}
    referenced = set()
    for face in faces:
        n = len(face)
        if n < 3:
            raise ManifoldError(f"Face with fewer than 3 vertices: {face}")
        for k in range(n):
            a = face[k]
            b = face[(k + 1) % n]
            if a == b:
                raise ManifoldError(f"Degenerate edge {a}-{b} in face {face}")
            referenced.add(a)
            referenced.add(b)
            key = (a, b) if a < b else (b, a)
            edge_count[key] = edge_count.get(key, 0) + 1

    bad = [(e, c) for e, c in edge_count.items() if c != 2]
    if bad:
        sample = bad[:5]
        raise ManifoldError(
            f"{len(bad)} non-manifold edge(s); first few: {sample}"
        )

    orphans = sorted(set(range(len(verts))) - referenced)
    if orphans:
        raise ManifoldError(f"Orphan vertices: {orphans[:10]}")
