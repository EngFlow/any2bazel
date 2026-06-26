// Node ESM loader hook. Registered by preload.mjs via module.register().
//
// We redirect `import * as esbuild from 'esbuild'` (and `import 'esbuild'`)
// to esbuild-shim.mjs. The shim is real ESM that uses createRequire to load
// the actual esbuild module and re-exports wrapped `build` / `transform`,
// passing everything else through unchanged.
//
// Why a redirect-to-ESM-shim instead of patching the CJS source:
//   * Source-append doesn't compose with Node's CJS-of-ESM translator -- the
//     wrapper code lands in a scope that can't see esbuild's module-level
//     `var build` (ReferenceError).
//   * module.exports replacement / Proxy don't propagate to ESM consumers
//     because the CJS-to-ESM translator snapshots named bindings at the
//     first import, not live.
//   * Genuine ESM exports from the shim ARE live-bound to the importer.
//     `esbuild.transform` in user code reads the shim's export, which is OUR
//     wrapper.

const SHIM_URL = new URL('./esbuild-shim.mjs', import.meta.url).href;
const ESBUILD_REAL_SUFFIX = '?esbuild-real=1';

export async function resolve(specifier, context, nextResolve) {
	// Only redirect the bare specifier, and skip if the importer is the shim
	// itself (which loads the real module via a marked URL below).
	if (specifier === 'esbuild' && !context.parentURL?.includes(ESBUILD_REAL_SUFFIX)) {
		// Resolve the REAL esbuild path relative to the original importer, so
		// monorepo-style installs (esbuild in build/node_modules but not at
		// repo root) still find the right copy.
		const real = await nextResolve(specifier, context);
		// Encode the real URL as a search param so the shim can read it
		// without env vars or globals (and so different consumers can in
		// principle resolve different esbuild copies, though we don't expect
		// that here).
		const url = `${SHIM_URL}?real=${encodeURIComponent(real.url)}`;
		return { url, format: 'module', shortCircuit: true };
	}
	return nextResolve(specifier, context);
}
