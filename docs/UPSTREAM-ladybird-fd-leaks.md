# Ladybird: two fd leaks in WebContent (one per HTTP request, one per un-`close()`d MessagePort)

Status: ready to file upstream. Both reproduced locally on a **CMake** build of
`f9e34731` (no Bazel involved); the primary one is present in upstream `master`
(`50eef049`) by inspection of the same code paths.

Ulf's Bazel-built browser died overnight with

```
dup: Too many open files (errno=24) at Libraries/LibWebView/CompositorConnection.cpp:62
```

Two `/proc/<pid>/fd` censuses minutes apart showed **17,423 → 17,497 `socket:`**, with
only 18 `pipe:` and nothing else growing. Monotonic, unbounded, and *sockets only*.

That last detail is what makes this report two bugs instead of one. My first diagnosis
(`MessagePort::entangle_with`) was **wrong for Ulf's crash**: it leaks 4 pipes for every
1 socket, so it cannot produce a sockets-only census. Counting fds by category, rather
than reasoning about which code looked suspicious, is what separated them — and the
per-request leak below matches his census exactly.

---

## Bug 1 (the one that kills the browser): every completed HTTP request leaks one socket fd

### Reproduction

Serve a one-line `index.html` on localhost and load:

```html
<script>
let n = 0;
function round() {
  let ps = [];
  for (let i = 0; i < 5; i++) ps.push(fetch('/index.html?' + (n++)).then(r => r.text()));
  Promise.all(ps).then(() => { if (n < 200) setTimeout(round, 30); });
}
round();
</script>
```

`Build/full/bin/Ladybird --headless=screenshot --screenshot-delay 120 <url>`, then census
the WebContent process:

```
ls -l /proc/$P/fd | awk '{print $NF}' | sed 's/\[[0-9]*\]//' | sort | uniq -c | sort -rn
```

Measured (clean build, no local patches):

| requests served | WebContent `socket:` fds | `pipe:` fds |
|---|---|---|
| 0 (baseline) | 5 | 18 |
| 103 | 107 | 18 |
| 202 | 208 | 18 |
| 595 | 587 | 18 |
| 1164 | 1157 | 18 |

**One socket per request, forever, and the pipe count never moves** — Ulf's census shape.
They are never reclaimed: the count stays flat for minutes after the loop stops, across
repeated explicit `internals.gc()` calls, and with `--disable-http-memory-cache
--disable-http-disk-cache`. `/proc/net/unix` shows them as connected `AF_UNIX`
`SOCK_STREAM` halves — RequestServer's response-body pipe (`RequestPipe::create`,
`Services/RequestServer/RequestPipe.cpp:46`). RequestServer closes its end (its own fd
count is flat at 4 sockets); **WebContent never closes its end.**

It is not specific to `fetch()`: plain subresource loads leak identically — 50 `<img>`
loads give 57 sockets, 200 give 207 (same +1/request against the same baseline of 5–7).

### Cause: an explicit GC root anchoring a cycle that spans the GC heap and the refcount heap

`internals.dumpGCGraph()` labels roots with their source location, which names the holder
outright. After 202 requests and two forced GCs:

```
808 Root nonstandard_resource_loader_file_or_http_network_fetch  Libraries/LibWeb/Fetch/Fetching/Fetching.cpp:2338
333 VM
 35 StackPointer
```

808 = **4 × 202**: the four callbacks passed to `ResourceLoader::load` at
`Fetching.cpp:2338`, one set per request, none ever released. Live cells scale in lockstep
(202 requests → 202 `FetchedDataReceiver`, 606 `Response`, 808 `PendingResponse`, 406
`ReadableStream`).

The loop:

1. `nonstandard_resource_loader_file_or_http_network_fetch` creates four
   `GC::Function`s (`Fetching.cpp:2261`–`2333`) and passes them as
   `GC::Root<...>` into `ResourceLoader::load`
   (`Libraries/LibWeb/Loader/ResourceLoader.h:43`).
2. `GC::Root` is a **strong, explicit root**: `RootImpl`'s constructor registers itself in
   `Heap::m_roots` (`LibGC/Root.cpp:15`, `Heap.h:251`) and `gather_roots` marks every entry
   (`Heap.cpp:841`). A `GC::Root` is uncollectable by construction — it is only released
   when the `RootImpl` is destroyed.
3. `ResourceLoader::load` moves those roots into the lambdas it installs on the
   **refcounted** `Requests::Request` (`ResourceLoader.cpp:469`–`513`, stored as
   `on_headers_received` / `on_finish` / …).
4. `on_headers_received` puts the very same `Requests::Request` **back into the GC heap**:
   `response->set_request_server_request({… .request = request_server_request})`
   (`Fetching.cpp:2276`), and `Response` holds it as a
   `RefPtr<Requests::Request>` (`Fetch/Infrastructure/HTTP/Responses.h:223`/`226`).

So: `Heap::m_roots` → `GC::Function` → captured `pending_response` / `stream` /
`fetched_data_receiver` → `Response` → `RefPtr<Requests::Request>` → the lambdas holding
the `GC::Root`s. Neither mechanism can break it — the GC sees a live root, and the
refcount never reaches zero. `Requests::Request` owns the response fd (`m_fd`, closed only
in `~Request`, `LibRequests/Request.cpp:58`, and kept open meanwhile by the `ReadStream` /
`Core::Notifier`), so **the fd leaks with the cycle**.

The only thing that ever clears those callbacks is `Request::defer_teardown()`
(`Request.cpp:284`), and its only callers are `stop()` (`:69`) and `did_transfer()`
(`:274`). **Normal completion never calls it**: `did_finish()` (`:251`) invokes
`on_finish` and returns, and `RequestClient::request_finished` (`RequestClient.cpp:205`)
only removes its map entry — the `RefPtr` inside `Response` keeps the request, its
callbacks, its roots and its fd alive for the lifetime of the process.

### A/B that confirms it

Same page, but each fetch is immediately `AbortController.abort()`ed — `abort` reaches
`Request::stop()`, which *does* call `defer_teardown()`:

| variant | 200 requests |
|---|---|
| completed fetches | +200 sockets (207) |
| aborted fetches | **+0 sockets (flat 207 → 207)** |

The path that tears down does not leak; the path that succeeds does.

### On fixing it — one obvious patch is wrong, and I verified that

Calling `defer_teardown()` at the end of `did_finish()` looks like the one-line fix. **It
is not**: I applied exactly that, rebuilt `liblagom-requests`, and the leak went to zero
(flat 5 sockets) *because loading broke* — pages rendered blank, since the body may still
be draining when `did_finish` arrives (`on_finish` re-checks
`read_stream->is_eof()` / `user_finish_called` for precisely this reason). I reverted it
and re-verified the clean build renders and leaks again; the numbers above are all from
the unpatched build.

A correct fix has to release the callbacks (and with them the `GC::Root`s) only once the
body is genuinely finished — e.g. at the point `on_finish` decides
`user_finish_called`/EOF — or break the cycle at its other end so `Response` does not keep
the `Requests::Request` alive strongly. Which end upstream prefers is your call; the
diagnosis is what I'm confident in.

### Why it takes the whole browser down, not just a tab

`EMFILE` in WebContent surfaces through `MUST()` on an encode
(`LibWebView/CompositorConnection.cpp:62`) — an abort, not a propagated error. WebContent
dies; the Compositor's `VERIFY(connection)`
(`Services/Compositor/ConnectionFromClient.cpp:68`) then aborts; and the UI process's
`MUST` in `initialize_client` follows. **One `EMFILE` kills three processes**, which is
also why the failure is unattributable after the fact: nothing in the log names the
resource that ran out. Two `/proc/<pid>/fd` censuses a few minutes apart is what turned it
into a bug report.

---

## Bug 2 (independent, smaller): a `MessagePort` dropped without `close()` leaks its socketpair

Distinguishable by signature: this one leaks **2 pipes + 1 socket per port** (each
`TransportSocket` makes two `pipe2` pairs at `TransportSocket.cpp:165,170`), so a page doing
this shows a *pipe*-dominated census — not what Ulf saw.

```html
<script>
for (let i = 0; i < 4000; i++) { let c = new MessageChannel(); c = null; }  // no close()
</script>
```

Result: WebContent reaches 1,628 `pipe:` + 409 `socket:` and dies with

```
UNEXPECTED ERROR: pipe2: Too many open files (errno=24) at Libraries/LibIPC/TransportSocket.cpp:165
```

The same page with `c.port1.close(); c.port2.close()` stays flat at 18 pipes — so the leak
is scoped to ports dropped **without** `close()`, which is the ordinary case in real pages.

`MessagePort::entangle_with` (`LibWeb/HTML/MessagePort.cpp:222`/`230` upstream) installs
read hooks capturing `GC::make_root(this)` and `GC::make_root(m_remote_port)` — again
strong explicit roots, stored in a callback owned by the transport owned by the port, so
the port roots itself. `close()` reaches `disentangle()` (`:472`), which is the only thing
that closes the transport; a port that is merely dropped never gets there. Same *shape* as
bug 1 (a `GC::Root` captured into something the rooted object owns), different site.

---

## Method note

Three of my earlier theories about Ulf's crash were wrong (lost fd acks; an
`AnonymousBuffer`/image-cache miss; leaked `TransportSocket`s), and one was wrong only
because my own census one-liner was broken — `sed 's/[0-9]*$//'` does not strip
`pipe:[123]`, and piping through `head` hid the answer. The census command that works:

```
ls -l /proc/$P/fd | awk '{print $NF}' | sed 's/\[[0-9]*\]//' | sort | uniq -c | sort -rn
```

Everything above is either a counted fd, a labelled GC root, or an A/B against a rebuilt
binary.
