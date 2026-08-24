// Copyright 2026 EngFlow GmbH
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Node ESM loader hook. Registered by preload.mjs via module.register().
//
// We redirect bare specifiers `'esbuild'` and `'typescript'` to ESM shims that
// re-export wrapped methods. Each shim:
//   - createRequire()s the real CJS module via the ?real= search param,
//   - wraps the methods we want to capture (esbuild.build/transform,
//     ts.createLanguageService),
//   - exports the wrappers as live ESM bindings so importers see them.
//
// Why a redirect-to-ESM-shim instead of patching the CJS source / module
// cache:
//   * Source-append doesn't compose with Node's CJS-of-ESM translator -- the
//     wrapper code lands in a scope that can't see the CJS module's
//     module-level `var` (ReferenceError).
//   * Mutating require.cache[id].exports doesn't reach ESM consumers in
//     Node 24: the CJS-to-ESM translator snapshots the value once at
//     synthesis time, not live (verified via createLanguageService.name
//     check after a cache-swap proxy install).
//   * Object.defineProperty / direct assignment to the real module fail
//     because esbuild-style `__export(target, all)` defines non-configurable
//     getter-only properties.
//   * Genuine ESM exports from the shim ARE live-bound to the importer.

const ESBUILD_SHIM_URL = new URL('./esbuild-shim.mjs', import.meta.url).href;
const TYPESCRIPT_SHIM_URL = new URL('./typescript-shim.mjs', import.meta.url).href;

function isShimImporter(parentURL, shimURL) {
	// The shim URL has a `?real=` query string; the importer URL Node passes
	// includes the query string too, so startsWith(shimURL) matches both the
	// bare shim and its query-decorated form.
	return parentURL && (parentURL === shimURL || parentURL.startsWith(shimURL));
}

export async function resolve(specifier, context, nextResolve) {
	// esbuild: redirect bare specifier, skip if importer is the esbuild shim.
	if (specifier === 'esbuild' && !isShimImporter(context.parentURL, ESBUILD_SHIM_URL)) {
		const real = await nextResolve(specifier, context);
		const url = `${ESBUILD_SHIM_URL}?real=${encodeURIComponent(real.url)}`;
		return { url, format: 'module', shortCircuit: true };
	}
	// typescript: same pattern. Skipping when the importer is the typescript
	// shim itself lets the shim's `export * from 'typescript'` reach the real
	// module so namespace imports see the full ts surface.
	if (specifier === 'typescript' && !isShimImporter(context.parentURL, TYPESCRIPT_SHIM_URL)) {
		const real = await nextResolve(specifier, context);
		const url = `${TYPESCRIPT_SHIM_URL}?real=${encodeURIComponent(real.url)}`;
		return { url, format: 'module', shortCircuit: true };
	}
	return nextResolve(specifier, context);
}
