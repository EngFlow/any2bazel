# `generate_libweb_bindings.py` output depends on `PYTHONHASHSEED` (dictionary emission order)

## Summary

`generate_libweb_bindings.py` is not reproducible: the order in which it emits
dictionary `struct`s into a generated `Bindings/*.h` depends on Python's
per-process string hash seed. Two runs of the same generator, on the same input
IDL, with the same version of everything, can produce byte-different output.

Concretely, in `Bindings/MediaCapabilities.h` the `AudioConfiguration` and
`VideoConfiguration` structs swap places depending on `PYTHONHASHSEED`.

The generated code is *valid* either way (see [Impact](#impact) — this is not a
miscompile), but the non-reproducibility defeats build caching, makes generated
output diff noisily between machines/runs, and would make any future
bit-for-bit reproducible-build effort fail intermittently.

## Environment

- Ladybird `f9e34731b85fea1c3517941d8388566cd33277c4`
- Python 3.14.4 (any Python 3.3+ with hash randomization on, which is the default)
- Linux x86-64. No CMake/Ninja/Bazel involvement — the generator alone.

## Reproduction

`Meta/Generators/libweb_bindings/to_idl_value.py` cannot be run directly (it is a
library module: no `main()`, and its imports need `Meta/` on `sys.path`). Drive
it through its entry point. Only two IDL files are needed — no build required:

```bash
cd /path/to/ladybird
for s in 0 1 2 3 4 5; do
  rm -rf /tmp/gen && mkdir -p /tmp/gen
  PYTHONHASHSEED=$s python3 Meta/Generators/generate_libweb_bindings.py -o /tmp/gen \
    Libraries/LibWeb/MediaCapabilitiesAPI/MediaCapabilities.idl \
    Libraries/LibWeb/EncryptedMediaExtensions/MediaKeySystemAccess.idl
  echo "seed=$s $(grep -o '^struct \(Audio\|Video\)Configuration' \
      /tmp/gen/MediaCapabilities.h | tr '\n' ' ')"
done
```

(`MediaKeySystemAccess.idl` is only there to supply the `MediaKeysRequirement`
enum; without it the generator exits with `Unsupported string default value type
'MediaKeysRequirement'`.)

### Actual output

```
seed=0  struct AudioConfiguration struct VideoConfiguration
seed=1  struct VideoConfiguration struct AudioConfiguration   <-- differs
seed=2  struct AudioConfiguration struct VideoConfiguration
seed=3  struct VideoConfiguration struct AudioConfiguration   <-- differs
seed=4  struct AudioConfiguration struct VideoConfiguration
seed=5  struct AudioConfiguration struct VideoConfiguration
```

### Expected

Identical output for every seed.

Note that **4 of 6 seeds produce the "right" answer**, so this is intermittent
and easy to miss: whether you see it depends on the seed your Python process
happened to get.

## Root cause

`dictionaries_in_dependency_order()` in
`Meta/Generators/libweb_bindings/to_idl_value.py` topologically sorts the
dictionaries with a DFS, and the DFS iterates a **`set`** of dependency names:

```python
    def dependency_names_for(dictionary: Dictionary) -> set[str]:
        dependency_names = {dictionary.parent_name} if dictionary.parent_name else set()
        for member in dictionary.members:
            dependency_names.update(context.dictionary_type_names(member.type))
        dependency_names.discard(dictionary.name)
        return dependency_names
    ...
        for dependency_name in dependency_names_for(dictionary):   # <-- set iteration
            dependency = local_dictionaries.get(dependency_name)
            if dependency is not None:
                visit(dependency)
```

That set is built as a comprehension in
`GenerationContext.dictionary_type_names()` (`context.py`), so its iteration
order follows the hash of the type-name strings, which is randomized per
process. Visit order therefore varies, and so does the order items are appended
to `ordered_dictionaries`.

It only becomes visible when a dictionary has **two or more mutually independent
dependencies**, because then their relative order is a pure tie-break that the
topological constraint does not pin down. `MediaConfiguration` is exactly that
case:

```webidl
dictionary MediaConfiguration {
    VideoConfiguration video;
    AudioConfiguration audio;
};
```

## Impact

To be precise about severity:

- **Not** a correctness bug in the generated C++. Both orderings are valid:
  dependencies still precede dependents, so both compile. Sorting the lines of
  the two variants gives identical checksums — it is a pure permutation of
  top-level declarations.
- It **is** a reproducibility bug. Effects:
  - Build caches (ccache, and any content-addressed/remote cache) miss
    spuriously: `MediaCapabilities.h` is included by 5 generated TUs plus
    `MediaCapabilitiesAPI/MediaCapabilities.cpp`, so a spurious change to it
    re-triggers their compiles.
  - Generated-output diffs between two machines/runs are non-empty for no
    reason, which makes "did my change alter codegen?" harder to answer.
  - Any bit-for-bit reproducible-build or codegen-parity check fails
    intermittently rather than deterministically.

Scope today is small: sweeping the full 1332-file bindings output over several
seeds, `MediaCapabilities.h` is the *only* file that differs (seeds 1 and 3
differ; 13 and 99 match). But the defect is in the shared ordering routine, so
any future IDL with two independent dictionary dependencies inherits it.

## Suggested fix

Sort the dependency **names** before visiting them:

```diff
--- a/Meta/Generators/libweb_bindings/to_idl_value.py
+++ b/Meta/Generators/libweb_bindings/to_idl_value.py
@@ -189,7 +189,9 @@ def dictionaries_in_dependency_order(dictionaries: List[Dictionary], context: Ge
             raise RuntimeError(f"Dictionary '{dictionary.name}' depends on itself")
 
         visiting.add(dictionary.name)
-        for dependency_name in dependency_names_for(dictionary):
+        # Sorted: dependency_names_for() returns a set, so iterating it directly
+        # makes the emitted order depend on PYTHONHASHSEED.
+        for dependency_name in sorted(dependency_names_for(dictionary)):
             dependency = local_dictionaries.get(dependency_name)
             if dependency is not None:
                 visit(dependency)
```

Sorting names (not `Dictionary` objects) preserves the topological ordering — it
only determinizes the tie-break among mutually independent dependencies.

Deliberately **not** changed in the same function:

- `emitted` / `visiting` are sets but are only membership-tested, never
  iterated, so they cannot leak order.
- `for dictionary in local_dictionaries.values()` is a `dict` built from the
  `dictionaries` *list*, so it is insertion-ordered (= IDL argument order) and
  already deterministic. Sorting it would additionally *change* today's output.

### Verification

- Repro script above: one ordering across all seeds after the patch.
- Full bindings run (all 661 IDL args, 1332 output files) reproduces the
  existing CMake-generated output **byte-identically under `PYTHONHASHSEED` 0,
  1, 2, 7 and 12345**. Unpatched, seed 1 diverges. So the patch fixes the
  nondeterminism *and* keeps current output unchanged — it happens to pin the
  order to what unseeded runs already produced most of the time.

## Aside: a similar seed-stability check for the generators

Because this reproduces under only some seeds, a determinism check that runs a
generator once (inheriting whatever seed the process has) cannot detect it — it
passes by luck. If you want a regression guard, it needs to sweep
`PYTHONHASHSEED` explicitly. The repro script above exits non-zero when the
ordering varies, so it can be used directly as one.

---

*Found while migrating Ladybird's build to Bazel: hermetic, sandboxed actions
made the mismatch show up as an intermittent codegen-parity failure. Happy to
open a PR with the one-line patch.*
