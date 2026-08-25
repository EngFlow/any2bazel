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

// ESM shim that user code reaches when it does `import ts from 'typescript'`
// or `import * as ts from 'typescript'`. loader.mjs's resolve hook redirects
// 'typescript' to this file with a `?real=<url-encoded-file-url>` search param
// pointing at typescript/lib/typescript.js (CJS).
//
// We require() the real module (CJS) to get a live module.exports reference,
// wrap createLanguageService, and route both consumption styles through us:
//
//   `import ts from 'typescript'`         -> receives `wrappedDefault` (Proxy)
//   `import * as ts from 'typescript'`    -> receives this module's namespace
//
// The namespace must carry every typescript named export so callers like
// build/lib/nls-analysis.ts (`import * as ts from 'typescript'; ts.SyntaxKind`)
// still work. `export * from 'typescript'` does that via cjs-module-lexer's
// name discovery; the resolver bypasses the redirect when the importer is
// the shim itself so this re-export reaches the real module. Local
// `export const createLanguageService` shadows the re-export with our wrapper.

import * as realNamespace from 'typescript';

import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { openSync, writeSync } from 'node:fs';

const require = createRequire(import.meta.url);

// Parse ?real=... out of our own URL.
const url = new URL(import.meta.url);
const realUrl = url.searchParams.get('real');
if (!realUrl) {
	throw new Error('typescript-shim.mjs invoked without ?real= search param');
}
const realPath = fileURLToPath(realUrl);
const real = require(realPath);

// IR sink (self-contained; same env var as the rest of the preload).
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

// Snapshot of a host's compiler settings, argv-shaped. Only the small set
// that affects output identity.
function tsCompilerOptionsArgv(host) {
	try {
		const o = (host && typeof host.getCompilationSettings === 'function')
			? host.getCompilationSettings() : {};
		const keys = ['target', 'module', 'moduleResolution', 'outDir', 'rootDir',
			'jsx', 'esModuleInterop', 'declaration', 'sourceMap',
			'inlineSources', 'experimentalDecorators', 'useDefineForClassFields'];
		const out = [];
		for (const k of keys) {
			if (Object.prototype.hasOwnProperty.call(o, k)) {
				out.push('--' + k + '=' + String(o[k]));
			}
		}
		return out;
	} catch {
		return [];
	}
}

const origCreate = real.createLanguageService;

// Wrap createLanguageService: every service instance gets an instrumented
// getEmitOutput. gulp-tsb calls service.getEmitOutput(fileName) once per
// source file in the full-build path -- that IS the per-file TS compile.
const patchedCreateLanguageService = function patchedCreateLanguageService(host, registry, ...rest) {
	const svc = origCreate.call(this, host, registry, ...rest);
	try {
		const origEmit = svc.getEmitOutput;
		if (typeof origEmit === 'function') {
			svc.getEmitOutput = function patchedGetEmitOutput(fileName, ...args) {
				const result = origEmit.apply(this, [fileName, ...args]);
				try {
					const outs = (result && result.outputFiles) || [];
					emit({
						mnemonic: 'TsCompile',
						arguments: tsCompilerOptionsArgv(host),
						inputs: [String(fileName)],
						outputs: outs.map(f => String(f.name)),
						cwd: process.cwd(),
					});
				} catch { /* swallow */ }
				return result;
			};
		}
	} catch { /* never let instrumentation fail the build */ }
	return svc;
};

// Default-import surface: a Proxy so `ts.createLanguageService` reads return
// the wrapper while everything else falls through to the real module live.
const wrappedDefault = new Proxy(real, {
	get(target, key, recv) {
		if (key === 'createLanguageService') return patchedCreateLanguageService;
		return Reflect.get(target, key, recv);
	},
});

// Re-export every named typescript export by delegating to the real
// namespace (which the resolve hook served by skipping our redirect when
// the importer is this shim).
export * from 'typescript';

// Override the one name we care about. Local exports shadow `export *`.
export const createLanguageService = patchedCreateLanguageService;

// Touch realNamespace once so bundlers/tree-shakers don't drop the side-effect
// import; the import is the whole point of having a live namespace to mirror.
void realNamespace;

export default wrappedDefault;
