// Copyright 2026 EngFlow Inc.
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

// Node preload that instruments an npm/gulp/esbuild build IN-PROCESS, without
// modifying the target repo. Activated via:
//
//   NODE_OPTIONS='--import file:///abs/path/to/preload.mjs'
//   VSCODE_EMIT_BUILD_IR=/abs/path/to/actions.ndjson
//
// NODE_OPTIONS propagates into every child node process, so spawned tsgo /
// esbuild service / nested `node build/...` invocations all get instrumented
// from the same env vars -- no per-tool wrappers needed.
//
// We hook two surfaces:
//   1. esbuild.build / esbuild.transform -- by mutating the CJS module.exports
//      BEFORE any user code imports it. ESM named imports from CJS snapshot
//      module.exports at first import; running this from --import beats user
//      code to the punch, so importers see the wrapped versions.
//   2. child_process.{spawn,spawnSync,execFile,execFileSync} -- catch-all for
//      tsgo and any other subprocess. Argv straight from the call site.
//
// Each hook appends ONE NDJSON line per action to $VSCODE_EMIT_BUILD_IR. The
// extractor (extract_npm.py) reads the file post-build; the build itself is
// not aware of the IR sink beyond what its own logs say.

import { createRequire, register } from 'node:module';
import { openSync, writeSync } from 'node:fs';

const OUT = process.env.VSCODE_EMIT_BUILD_IR;
if (OUT) {
	// Install the ESM loader that wraps esbuild's main.js at LOAD time. The
	// loader runs in a worker; the appended wrapper executes in the main
	// thread inside esbuild's CJS scope. The wrapper reads VSCODE_EMIT_BUILD_IR
	// itself, so no globals need to bridge the worker/main boundary.
	register(new URL('./loader.mjs', import.meta.url));

	const fd = openSync(OUT, 'a');
	// Use the CJS module record so we can mutate its exports. The ESM
	// namespace object for `node:child_process` is frozen and rejects writes.
	const require = createRequire(import.meta.url);
	const cp = require('node:child_process');
	const emit = (rec) => {
		try {
			writeSync(fd, JSON.stringify({ ts: Date.now(), pid: process.pid, ...rec }) + '\n');
		} catch {
			// Don't let an IR write failure crash the build.
		}
	};

	// --- child_process catch-all ------------------------------------------
	// Patch the four spawn entry points. Argv shape: [file, ...args].
	for (const name of ['spawn', 'spawnSync', 'execFile', 'execFileSync']) {
		const orig = cp[name];
		if (typeof orig !== 'function') { continue; }
		cp[name] = function patched(file, args, options) {
			// args may be the options object if no args array is supplied.
			const argv = Array.isArray(args) ? args : [];
			const opts = (args && !Array.isArray(args) && typeof args === 'object') ? args : options;
			const cwd = (opts && typeof opts === 'object' && opts.cwd) || process.cwd();
			emit({ mnemonic: 'Spawn', arguments: [String(file), ...argv.map(String)], cwd });
			return orig.apply(this, arguments);
		};
	}

	// esbuild.build / esbuild.transform are wrapped by loader.mjs (see above),
	// which appends a self-contained wrapper to esbuild/lib/main.js. We do
	// NOT wrap them here -- attempts to do so via Module._cache.exports
	// replacement or Proxy don't reach ESM consumers in Node 24 (the CJS->ESM
	// translator snapshots named bindings at first import).

	// typescript.createLanguageService -- wrapped by loader.mjs (see above),
	// which redirects `'typescript'` to typescript-shim.mjs. Same reasoning
	// as esbuild: Module._cache.exports swapping and direct mutation BOTH
	// fail in Node 24 (snapshot at synth + non-configurable getter-only
	// properties), so the only path to a live wrapper is a shim with real
	// ESM exports.
}
