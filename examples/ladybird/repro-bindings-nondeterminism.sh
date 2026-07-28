#!/usr/bin/env bash
# Reproduce the generate_libweb_bindings.py dictionary-ordering nondeterminism
# (finding 2 of the any2bazel Ladybird migration).
#
# Usage:  ./repro-bindings-nondeterminism.sh [/path/to/ladybird]
#
# No build, no CMake, no Bazel required -- just python3 and a Ladybird checkout.
# to_idl_value.py itself is NOT runnable: it is a library module with no main()
# and imports that need Meta/ on sys.path. Drive it through its entry point,
# generate_libweb_bindings.py.
#
# Expected: on unpatched upstream the struct order flips with PYTHONHASHSEED
# (AudioConfiguration/VideoConfiguration swap at seeds 1 and 3 below); with the
# sorted() patch applied, every seed gives the same order.
set -u
LADYBIRD="${1:-$PWD}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

# MediaCapabilities.idl declares MediaConfiguration, which depends on both
# VideoConfiguration and AudioConfiguration -- two dictionaries independent of
# each other, so their relative emission order is a pure tie-break decided by
# set iteration order. MediaKeySystemAccess.idl only supplies an enum the first
# file references (MediaKeysRequirement); without it the generator errors out.
IDLS=(
  "$LADYBIRD/Libraries/LibWeb/MediaCapabilitiesAPI/MediaCapabilities.idl"
  "$LADYBIRD/Libraries/LibWeb/EncryptedMediaExtensions/MediaKeySystemAccess.idl"
)
for f in "${IDLS[@]}"; do
  [ -f "$f" ] || { echo "not found: $f"; echo "pass the path to your Ladybird checkout"; exit 2; }
done

echo "generator: $LADYBIRD/Meta/Generators/generate_libweb_bindings.py"
echo
printf '%-8s %s\n' "seed" "emission order in MediaCapabilities.h"
seen=""
for seed in 0 1 2 3 4 5; do
  rm -rf "$OUT/gen"; mkdir -p "$OUT/gen"
  if ! PYTHONHASHSEED="$seed" python3 \
        "$LADYBIRD/Meta/Generators/generate_libweb_bindings.py" \
        -o "$OUT/gen" "${IDLS[@]}" >"$OUT/log" 2>&1; then
    printf '%-8s FAILED: %s\n' "$seed" "$(tail -1 "$OUT/log")"
    continue
  fi
  order=$(grep -o '^struct \(Audio\|Video\)Configuration' "$OUT/gen/MediaCapabilities.h" \
          | sed 's/^struct //' | tr '\n' ' ')
  printf '%-8s %s\n' "$seed" "$order"
  case "$seen" in *"[$order]"*) ;; *) seen="$seen[$order]";; esac
done

echo
n=$(printf '%s' "$seen" | tr -cd '[' | wc -c)
if [ "$n" -gt 1 ]; then
  echo "NONDETERMINISTIC: $n distinct orderings across seeds -> bug reproduced."
  exit 1
else
  echo "STABLE: one ordering across all seeds -> sorted() patch is in effect."
fi
