// ESM shim that user code reaches when it does `import * as esbuild from
// 'esbuild'`. Loader.mjs's resolve hook redirects 'esbuild' to this file
// with a `?real=<url-encoded-file-url>` search param pointing at the real
// esbuild's CJS main.js.
//
// We require() the real module (CJS, so this works), wrap build / transform
// with the IR-emitting hooks, and re-export everything else verbatim. Since
// our exports here are genuine ESM exports, an `import * as esbuild` against
// this shim picks up our wrappers live -- no Module._cache games, no source
// rewriting.

import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { openSync, writeSync } from 'node:fs';

const require = createRequire(import.meta.url);

// Parse ?real=... out of our own URL to find the real esbuild main.js. The
// loader hook encoded it for us so we don't have to re-resolve here.
const url = new URL(import.meta.url);
const realUrl = url.searchParams.get('real');
if (!realUrl) {
	throw new Error('esbuild-shim.mjs invoked without ?real= search param');
}
const realPath = fileURLToPath(realUrl);
const real = require(realPath);

// IR sink. Self-contained: read the path from the same env var the rest of
// the preload uses; if it's unset we silently no-op (the shim still passes
// build/transform through correctly).
const OUT = process.env.VSCODE_EMIT_BUILD_IR;
let emit = () => { /* no-op */ };
if (OUT) {
	const fd = openSync(OUT, 'a');
	emit = (rec) => {
		try {
			writeSync(fd, JSON.stringify({ ts: Date.now(), pid: process.pid, ...rec }) + '\n');
		} catch { /* never let an IR write failure surface */ }
	};
}

// Deterministic argv-shaped flattening of an esbuild BuildOptions object.
// Plugin closures are reduced to their `.name` strings -- the IR records
// WHICH plugins ran without smuggling functions into the model.
function flattenOpts(opts) {
	const out = [];
	for (const k of Object.keys(opts).sort()) {
		const v = opts[k];
		if (k === 'plugins' && Array.isArray(v)) {
			out.push('--plugins=' + v.map(p => (p && p.name) || '<anon>').join(','));
		} else if (typeof v === 'function') {
			// drop
		} else if (Array.isArray(v) || (v !== null && typeof v === 'object')) {
			out.push('--' + k + '=' + JSON.stringify(v));
		} else {
			out.push('--' + k + '=' + String(v));
		}
	}
	return out;
}

// Wrap build(): inject metafile:true to get esbuild's resolved inputs /
// outputs as the IR's `Action.inputs` / `outputs` annotation. Without that
// we'd have to parse argv to guess what got compiled; metafile is the
// authoritative answer esbuild already computed.
export const build = async function build(opts = {}) {
	const wantMeta = opts.metafile !== true;
	const passOpts = wantMeta ? { ...opts, metafile: true } : opts;
	const result = await real.build.call(this, passOpts);
	try {
		const meta = result && result.metafile;
		const inputs = meta ? Object.keys(meta.inputs).sort() : [];
		const outputs = meta
			? Object.keys(meta.outputs).sort()
			: (opts.outfile ? [opts.outfile]
				: (opts.outdir ? [opts.outdir] : []));
		emit({
			mnemonic: 'EsbuildBundle',
			arguments: flattenOpts(opts),
			inputs, outputs,
			cwd: opts.absWorkingDir || process.cwd(),
		});
	} catch { /* see emit() */ }
	return result;
};

// Wrap transform(): no metafile here; identity is the optional sourcefile
// annotation. The per-file transpile pass calls this ~7k times in a single
// vscode build; emit one NDJSON line per call.
export const transform = async function transform(input, opts) {
	const result = await real.transform.apply(this, arguments);
	try {
		const o = opts || {};
		emit({
			mnemonic: 'EsbuildTransform',
			arguments: flattenOpts(o),
			inputs: o.sourcefile ? [String(o.sourcefile)] : [],
			outputs: [],
			cwd: process.cwd(),
		});
	} catch { /* see emit() */ }
	return result;
};

// Pass everything else through verbatim. Keeping the full surface available
// (buildSync, context, formatMessages, ...) means the shim is a drop-in
// replacement, not a partial reimplementation that breaks corner cases.
export const buildSync = real.buildSync;
export const context = real.context;
export const transformSync = real.transformSync;
export const formatMessages = real.formatMessages;
export const formatMessagesSync = real.formatMessagesSync;
export const analyzeMetafile = real.analyzeMetafile;
export const analyzeMetafileSync = real.analyzeMetafileSync;
export const initialize = real.initialize;
export const stop = real.stop;
export const version = real.version;

// IMPORTANT: callers that use `import esbuild from 'esbuild'` (default import)
// receive THIS object, and read `esbuild.transform` / `esbuild.build` off it.
// If we just re-exported `real` here, those reads would bypass our wrappers
// (gulp-tsb's transpiler.ts uses exactly this style). Proxy lets us intercept
// the two methods we care about and pass everything else through.
const wrappedDefault = new Proxy(real.default ?? real, {
	get(target, key, recv) {
		if (key === 'build') return build;
		if (key === 'transform') return transform;
		return Reflect.get(target, key, recv);
	},
});
export default wrappedDefault;
