# `generate_libweb_bindings.py` output depends on `PYTHONHASHSEED`

`dictionaries_in_dependency_order()` in `Meta/Generators/libweb_bindings/to_idl_value.py`
iterates a *set* of dependency names (`for dependency_name in dependency_names_for(dictionary)`),
so when a dictionary has two independent dependencies their emission order follows Python's
randomized string hash. `MediaConfiguration` is such a case, and `Bindings/MediaCapabilities.h`
comes out with `AudioConfiguration`/`VideoConfiguration` swapped depending on the seed. Both
orderings compile — it's a reproducibility bug, not a miscompile — but it causes spurious cache
misses and noisy codegen diffs. Repro (no build needed, ~5s):

```bash
for s in 0 1 2 3; do rm -rf /tmp/g && mkdir -p /tmp/g
  PYTHONHASHSEED=$s python3 Meta/Generators/generate_libweb_bindings.py -o /tmp/g \
    Libraries/LibWeb/MediaCapabilitiesAPI/MediaCapabilities.idl \
    Libraries/LibWeb/EncryptedMediaExtensions/MediaKeySystemAccess.idl
  echo "seed=$s $(grep -o '^struct \(Audio\|Video\)Configuration' /tmp/g/MediaCapabilities.h | tr '\n' ' ')"
done
# seed=0 AudioConfiguration VideoConfiguration
# seed=1 VideoConfiguration AudioConfiguration   <-- differs
# seed=2 AudioConfiguration VideoConfiguration
# seed=3 VideoConfiguration AudioConfiguration   <-- differs
```

Fix: `for dependency_name in sorted(dependency_names_for(dictionary)):`. Sorting names preserves
the topological order and keeps current output unchanged — verified byte-identical to today's
generated bindings (1332 files) under seeds 0, 1, 2, 7, 12345. Found while migrating the build
to Bazel, where sandboxed codegen made it show up as intermittent parity failures.
