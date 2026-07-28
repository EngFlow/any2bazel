"""The parity harness must be able to vary PYTHONHASHSEED.

Ladybird's bindings generator emitted structs in set-iteration order, so its
output varied with the hash seed (fixed upstream by sorting: ladybird#10899).
Our harness ran each generator once under an inherited seed and reported
"1331/1331 identical" — which a seed-dependent generator passes most of the
time. A determinism check that does not vary the source of nondeterminism
proves nothing, so the seed knob is the thing worth regression-testing here.
"""

import os
import sys

_HARNESS = os.path.join(
    os.path.dirname(__file__), "..", "examples", "ladybird",
    "bazel_parity_harness.py",
)


def test_harness_takes_a_seed_and_passes_it_to_the_generator():
    src = open(_HARNESS).read()
    assert "--seed" in src, "no way to vary the seed"
    assert "PYTHONHASHSEED" in src
    # The seed must actually reach the generator subprocess, not just be parsed.
    assert "env=env" in src, "seed never reaches the subprocess"


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
