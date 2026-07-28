"""Tests for the seed-sweep determinism check on wrapped code generators.

Finding 2 of the Ladybird migration: generate_libweb_bindings.py emitted
dictionary structs in an order that followed Python's set iteration, so its
output varied with PYTHONHASHSEED. The upstream fix is to *sort* (see
examples/ladybird/upstream-sort-dictionary-order.patch); pinning
PYTHONHASHSEED=0 on the genrule only freezes the symptom.

The durable lesson tested here: a determinism check that does not vary the
source of nondeterminism proves nothing. A seed-dependent generator matches its
reference under *some* seeds (the real one matched at seed 4 and diverged at
seed 1), so a single-run check passes by luck. Only a sweep catches it.
"""

import os
import sys


def _topo_order(nodes, deps, sort):
    """Model of dictionaries_in_dependency_order(): DFS over a dep map.

    deps maps name -> set of dependency names. When sort=False the DFS iterates
    the set directly (order follows set iteration, i.e. the hash seed); when
    sort=True it iterates sorted(), which is what the fix does.
    """
    emitted, out = set(), []

    def visit(n):
        if n in emitted:
            return
        emitted.add(n)
        for d in (sorted(deps.get(n, ())) if sort else deps.get(n, ())):
            if d in nodes:
                visit(d)
        out.append(n)

    for n in nodes:
        visit(n)
    return out


# The real shape from MediaCapabilities.idl: MediaConfiguration depends on both
# VideoConfiguration and AudioConfiguration, which are independent of each other
# -- so their relative order is a pure tie-break, decided by iteration order.
_NODES = ["MediaConfiguration", "VideoConfiguration", "AudioConfiguration"]
_DEPS = {"MediaConfiguration": {"VideoConfiguration", "AudioConfiguration"}}


def test_sorted_iteration_is_stable_across_orderings():
    # Whatever order the dependency set yields, sorting pins the emission order.
    orders = set()
    for perm in ([ "VideoConfiguration", "AudioConfiguration"],
                 ["AudioConfiguration", "VideoConfiguration"]):
        deps = {"MediaConfiguration": perm}
        orders.add(tuple(_topo_order(_NODES, deps, sort=True)))
    assert len(orders) == 1, orders


def test_unsorted_iteration_is_not_stable():
    # Guards the test itself: without sorting, iteration order leaks through.
    orders = set()
    for perm in (["VideoConfiguration", "AudioConfiguration"],
                 ["AudioConfiguration", "VideoConfiguration"]):
        deps = {"MediaConfiguration": perm}
        orders.add(tuple(_topo_order(_NODES, deps, sort=False)))
    assert len(orders) == 2, orders


def test_sorting_preserves_topological_validity():
    # Sorting is only a tie-break: dependencies must still precede dependents.
    order = _topo_order(_NODES, _DEPS, sort=True)
    for dependent, dependencies in _DEPS.items():
        for dep in dependencies:
            assert order.index(dep) < order.index(dependent), order


def test_harness_exposes_a_seed_knob():
    """The parity harness must be able to vary PYTHONHASHSEED.

    Without this, the check inherits one seed and a seed-dependent generator
    slips through (which is exactly what happened).
    """
    harness = os.path.join(
        os.path.dirname(__file__), "..", "examples", "ladybird",
        "bazel_parity_harness.py",
    )
    src = open(harness).read()
    assert "--seed" in src
    assert "PYTHONHASHSEED" in src
    # ...and the seed must actually reach the generator subprocess.
    assert "env=env" in src


def test_upstream_patch_sorts_the_dependency_names():
    patch = os.path.join(
        os.path.dirname(__file__), "..", "examples", "ladybird",
        "upstream-sort-dictionary-order.patch",
    )
    src = open(patch).read()
    assert "+        for dependency_name in sorted(dependency_names_for(dictionary)):" in src
    assert "-        for dependency_name in dependency_names_for(dictionary):" in src


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
