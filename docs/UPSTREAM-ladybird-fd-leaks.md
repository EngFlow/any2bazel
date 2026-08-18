# Ladybird: fd leaks in WebContent (per HTTP request, and per un-`close()`d MessagePort)

Status: reproduced locally on a **CMake** build of `f9e34731` (no Bazel involved);
present in upstream `master` (`50eef049`) by inspection of the same code paths.

**Read this first — the per-request leak is two distinct bugs, not one.** An upstream
patch equivalent to the teardown fix below now exists, Ulf applied it, and *his browser
still leaks*. That is not a contradiction: the fix closes the class of retained request
whose peer is already **dead**, and there is a second class whose peer is still **alive**
that no teardown at that point can reach. The discriminator is one column of `ss -np`:

| class | `ss -np` peer inode | what happened | teardown patch |
|---|---|---|---|
| A: completed | `* 0` (**dead**) | request finished, RequestServer closed its half, WebContent retains a corpse | **fixes it** (143 → 0 locally) |
| B: stalled | a real inode (**alive**) | `on_finish` never ran, so no teardown fires at all | **unaffected** (40 → 40 locally) |

Count them before choosing a fix:

```
ss -np | grep "pid=$P," | grep -c ' \* 0 '   # class A (dead peer)
ss -np | grep -c "pid=$P,"                   # all of this process's unix sockets
```

**Two instruments, both usable on any tree, no patch required:**

- `examples/ladybird/fd_census.py` — *how many, which class, and is it still growing.*
- `examples/ladybird/fdtrace.c` + `fdtrace_report.py` — *which call site.* Build once
  (`cc -shared -fPIC -O2 -g -o fdtrace.so fdtrace.c -ldl`), `LD_PRELOAD` it into an
  unmodified browser, and the report says **who sent** each fd that was never
  closed. It hooks `recvmsg`/SCM_RIGHTS because the leaked fd is **received, never
  opened** — a tracer wrapping only `open()`/`socket()` sees nothing — and it reads
  `SO_PEERCRED` on the receiving socket, because the acquisition *stack* of an
  attachment is always the IPC read thread and therefore identical for every peer.
  Validated on the known leak: `143 from RequestServer, 5 from Ladybird`, matching
  the census's 143 dead-peer sockets exactly.

`examples/ladybird/fd_census.py` does the classification from **outside** a running
browser — no patch, no rebuild, no pinned tree:

```
python3 examples/ladybird/fd_census.py --find WebContent
python3 examples/ladybird/fd_census.py <pid> --watch 30
```

It prints the category census, peer DEAD/ALIVE per socket, retained-vs-in-flight by
age (so a freshly restarted process cannot be mistaken for a fix), which process holds
the live peers, and a verdict naming the class. Verified to agree with an
instrumented build on the same workload (143 DEAD / 4 ALIVE either way). The
in-process version is kept as
`examples/ladybird/patches/DIAGNOSTIC-fdleak-census.patch.txt` for the few fields
only it can see (request id, `user_finish_called`, and a one-build A/B of the fix).

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

### Class B: the request that never finishes at all (survives the teardown fix)

The teardown above hangs off the branch in `Request::set_up_internal_stream_data`'s
`on_finish` that decides the body is complete:

```cpp
if (!user_finish_called && (!read_stream || read_stream->is_eof() || has_received_all_reported_bytes)) {
```

If a response never satisfies that *and* never reaches EOF, the block never runs, so
`user_on_finish` never runs, so **no teardown placed inside it can ever fire**. Repro: a
server that sends `Content-Length: 48000`, writes 100 bytes and then holds the socket
open. 40 such loads, censused with the diagnostic build after they have aged past 30s:

```
FDLEAK live: in_flight(<30s)=0 retained(>=30s)=40
FDLEAK retained-bucket 40 x peer=ALIVE torn_down=false did_finish=false user_finish=false
                            request_done=false stream=true eof=false fd_open=true
```

Identical with `LADYBIRD_FDLEAK_TEARDOWN=1` (40 → 40): `did_finish=false` is the tell —
the `request_finished` IPC never arrived, so the fix is not even on the code path. And
`peer=ALIVE` distinguishes it from class A at a glance: RequestServer is still holding its
end, because as far as it knows the body is still being produced.

By contrast every completed-request shape I can construct — plain fetch, XHR, POST,
`response.clone()`, unread bodies, `<img>`/CSS/JS subresources, 404/302/cache hits,
truncated bodies, gzip/chunked, iframe navigations, mid-body `cancel()` — is flat at the
5-socket baseline with the fix on, and shows `peer=DEAD` retention without it.

So the two classes need different fixes, and only one of them is "release the callbacks
when the body is delivered":

- **Class A** is a *liveness* bug in the teardown path → the teardown patch.
- **Class B** is the cycle itself. As long as `Response` holds `Requests::Request` by
  `RefPtr` while the request's callbacks hold `GC::Root`s back into the GC heap, ANY
  request that never completes is retained forever, and there is no completion callback
  left to hook. The fix has to break the cycle at the `Response` end — e.g. hold the
  request weakly, or root the callbacks from something whose lifetime the GC can actually
  end — rather than adding another teardown call site.

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
`user_finish_called`/EOF, which is what the upstream patch and
`patches/0003-*.patch` do — **and** break the cycle at its other end so `Response` does
not keep the `Requests::Request` alive strongly, which is the only thing that can reach
class B. Doing only the first is measurable progress and not a fix: it is the same shape
as the `-fPIE` finding, where the change was necessary, verified, and still left the
symptom alive by another route.

### The fix that actually closes class A everywhere: release the fd, not just the callbacks

The teardown patch above was **necessary and insufficient**, and I only learned that from
Ulf's tree, not mine. With the upstream-equivalent teardown applied he still measured
**97 sockets/min** — 81→823 sockets, 73→815 dead-peer over 458 s — while every local
workload I could build was flat. Two instruments settled what was leaking:

- `fd_census.py`: every leaked socket was **peer=DEAD**, i.e. RequestServer had already
  closed its end. So these were completed requests, class A, on a tree carrying the class A
  fix.
- `fdtrace.c`: every leaked fd was a **received SCM_RIGHTS attachment** whose sender was
  **RequestServer** (`SO_PEERCRED`; his log said `from=?(pid=2261433)` because a renderer's
  landlock policy grants only `/proc/self`, and `ps -p 2261433` named it). That pins the fd
  exactly: the per-request response pipe from `RequestServer::RequestPipe::create()`, handed
  over by `request_started`.

The gap is **ownership, not liveness**. Dropping the callbacks unpins the GC cycle, but the
fd itself is released only by `~Request` — or by the `ReadStream` inside
`m_internal_stream_data`, which the teardown merely *nulls*. So any surviving reference to
the `Requests::Request` (the `RefPtr` in `Response`, a callback captured elsewhere, a
different retention path on a different tree) keeps **one fd per completed request** alive
even though the teardown ran. That is why the same patch zeroes the leak on my tree and
leaves 97/min on his: we differ in what else holds a reference, not in what the teardown
does.

`0004` is the **next patch in the series**, applied on top of `0003` (or upstream's
equivalent teardown fix) — not an alternative to it. Two mistakes on the way there, both
caught by Ulf within minutes and both now pinned by tests:

1. I generated it by diffing against the *clean* commit, so it silently carried `0003`'s
   own hunk and could not apply to the tree it was written for (`patch does not apply`).
2. Correcting that, I shipped *two* variants — one for a tree with the teardown fix, one
   without. That cannot work: `apply_overlay.sh` applies `patches/*.patch` **by glob**, so
   one of two mutually-exclusive patches is guaranteed to fail. `patches/` is a series,
   not a menu; an alternative belongs outside the glob, like `DIAGNOSTIC-*.patch.txt`.
   The clean-tree variant was also strictly weaker — it closed the fd without dropping the
   callbacks, leaving the GC cycle, and with it class B, in place.

A test now reconstructs the pinned versions of every file the patches touch and applies the
whole series in glob order, exactly as the script does, so a patch that conflicts with its
predecessor fails in CI rather than on someone else's clone.

`patches/0004-*.patch` closes the fd explicitly, at the point the code has *already proven*
the body is complete (the `user_finish_called` branch), deregistering the notifier before
closing so the event loop is never left polling a closed descriptor. It does **not** close
on `request_finished` alone — that truncates bodies (verified: blank pages), because
RequestServer finishes writing long before WebContent drains the pipe.

Measured A/B, same binary, same 200-completed-request workload, only this function differing:

| variant | sockets after 200 completed requests | peer DEAD |
|---|---|---|
| clean | 208 | 203 |
| `0004` applied | **6** | **0** |

and body delivery is intact — `text=10 | stream=10/bytes=48000 | cancel=5` (plain fetch,
incremental `ReadableStream` reads, mid-body `cancel()`), plus a rendered 800x600 screenshot.
The generalisable lesson: **"the object is collectable" and "the fd is closed" are different
claims**, and for a descriptor received over IPC only the second one is the bug. A fix
verified through the object graph can pass while the resource still leaks.

### Open: the leak persists on Ulf's machine after 0004, and my blind spot

`0004` takes my measurements to 0 and his tree is still leaking. Two things I got wrong
about how I was measuring, both worth recording because they are the reason the loop
keeps failing:

1. **Every census I requested was of WebContent.** That was my hypothesis, not a finding.
   RequestServer creates the response pipes *and* the cache body files, and the UI
   process and Compositor hold fds too — a leak in any of them is invisible to every
   number I have collected so far. `fd_census.py --all` now censuses every
   Ladybird-family process and ranks by growth rate, so the data names the process
   instead of me naming it.
2. **My server never exercised the disk cache.** Ulf runs
   `--http-disk-cache-mode enabled` against real sites; my test server sent no cache
   headers at all, so `handle_read_cache_state` never ran in any A/B I did. That matters
   because the large-cache-hit branch (`body_size >= PAGE_SIZE`) takes
   `take_body_file()` → `send_transferred_body_file_to_client()`: it sends a **body
   file** and never creates a response pipe, so it is a completed-request path that
   `release_response_fd()` cannot reach. I built that workload (300 cache hits over
   small/large/revalidated entries, disk cache on by default) and it stays flat at 5
   sockets here — so it is not sufficient on its own, but it is the first path found
   that my fix structurally does not cover.

What that means for the diagnosis: `0004` is verified to fix the response-pipe class
(208 → 5 sockets, 203 → 0 dead peers, bodies intact), and that class is real. It is
evidently not the whole of what Ulf is seeing, and the next measurement has to come from
his machine with `--all`, because I have now falsified every workload I can construct
locally — including the cache paths I had never tested.

### Correction: it IS WebContent, and the instrument now reads its own build

Ulf's next census settled the process question against me. His `--all` run (pid 19291,
4880 s, 4386 samples, flags
`--site-isolation=top-level --enable-http-memory-cache`):

```
fds: total=7514  socket:=7487
sockets: 7487  peer DEAD=7479  peer ALIVE=6  unknown=2
retained(>=30s)=7436, of retained: peer DEAD=7428
growth over 4880s: sockets +7458 (+91.7/min), all peer DEAD
```

So the accumulation is in **WebContent**, it is **class A** (peer=DEAD = completed
requests), and 7428 of 7436 retained fds are *not* in flight. The `--all` detour above
was a wrong turn: censusing every process was the right instrument to build, and it
answered "WebContent", which is where I had been looking all along.

But the number I could not interpret was the rate: **91.7/min against 97/min measured
before the fix**. That is equally consistent with two opposite conclusions —

- `0004` does not address this leak, or
- `0004` was not in that binary.

and they demand opposite next steps. My instinct was to *ask*. That would have been
another round trip, about a build that had already happened, answered from memory —
and it is the fourth time in this investigation that a number arrived without the
identity of the code that produced it. **A leak rate without its build provenance is
not a measurement anyone can reproduce, including me.**

The answer was never in anyone's memory: it is in the binary, and the binary is still
mapped by the process being censused. `fd_census.py` now reads it:

```
$ python3 fd_census.py <pid> --build
build: HAS 0004 (release the response fd on completion)
build: HAS 0003 (tear down the request when the body is delivered)
```

and the same block is printed next to every verdict, because the verdict is only
interpretable together with it. Implementation: a pure-Python ELF reader over the
`.dynstr`/`.strtab` of the executable and every mapped `.so` (no binutils dependency
on someone else's machine), looking for `Requests::Request::release_response_fd` and
`::defer_teardown`. Statically linked builds — Ulf's is one — are covered by probing
the executable when no `lagom-requests` library is mapped.

The load-bearing part is the **negative control**. `set_up_internal_stream_data` exists
in every build of that file, patched or not; if it is absent, the symbols were not
readable at all (stripped, LTO'd, or the code is somewhere we did not look) and the
probe reports *"cannot tell"* rather than *"fix absent"*. Without that control, a
stripped binary would read as an unpatched one, which would aim the next round of work
at exactly the wrong code — the same class of error as the two method mistakes above,
so it gets a guard rather than a caveat.

Verified end-to-end against two genuinely different builds of the same library: with
`release_response_fd` renamed away and relinked, the probe reports `does NOT have 0004`
while the control stays present; restored and relinked, `HAS 0004`. A non-Ladybird
process reports "cannot tell". Both directions are tested, because a probe that can
only confirm a fix is present is useless for the question that prompted it.

#### Correction: the probe was wrong, and Ulf was right

Ulf's reply to the above was *"the tool says it's not, but I'm sure it was applied"* —
and then *"I have all the patches applied."* **He was right and the probe was wrong.**
The flaw is worth recording in full because it is the same class of error as the
measurements this whole document is about.

Ladybird sets `ENABLE_LTO_FOR_RELEASE=ON` (`Meta/CMake/cmake_options.cmake:46`). In a
**static** build — Ulf's — LTO inlines a small internal-only method like
`release_response_fd()` into its single caller and leaves **no symbol and no string
behind at all**. Reproduced from first principles: a private method called only within
its TU, compiled `-O3 -flto` and linked, is absent from both `nm` and `strings` in the
linked executable. My own build kept it only because a **shared** library has to export
it. So the probe's answer depended on *how the binary was linked*, not on whether the
fix was in it — and the one build shape it had to get right was the one it got wrong.

The negative control did not save it, and that is the instructive part. `set_up_internal_stream_data`
is vulnerable to the *same* optimisation: verified that a larger internal-only function
also vanishes under LTO. **A negative control only rules out "unreadable" if it cannot
disappear for the same reason as the thing it guards.** Mine failed in a different mode
than the one it was guarding against, so it certified readability it had not
established. I had written that the control was "the load-bearing part"; it was
load-bearing in the wrong direction.

Two fixes, both verified:

1. **`.debug_str` is read as well as `.dynstr`/`.strtab`.** Debug info names
   inlined-away functions, and Ladybird's `RelWithDebInfo` compiles with `-g` (and
   `-g1`, also verified sufficient), so the answer survives there for exactly the
   LTO/static case.
2. **A fix is never reported MISSING unless a symbol that inlining cannot erase is
   visible** (`UNINLINABLE_CONTROLS`: vtable/IPC-dispatched entry points such as
   `request_started`, `headers_became_available`, and `Request`'s header-declared
   out-of-line methods). If none is readable, the probe now says *"cannot tell whether
   0004 is present"* and names inlining as the reason. Tested both ways: an
   Ulf-shaped blob (0004 inlined away, control present, nothing uninlinable) yields
   "cannot tell"; a genuinely unpatched-but-readable binary is still called out as
   missing 0004, re-verified end-to-end against a rebuilt shared library.

**What this means for the diagnosis.** With `0004` confirmed applied, the ~92/min
class-A leak is *not* the response-pipe path `0004` closes. The fd has a **third
owner**, and the two candidates below are no longer "still open" — they are the
diagnosis.

One more thing the probe caught on the way. Running the full census against a
*healthy* browser to check the new block, the verdict line read
`mixed DEAD/ALIVE -> both classes present` for 0 dead and 5 live sockets: both
classification branches required a 10x majority, so zero-of-a-class fell between them
and got reported as a class that had no members — while also reading the ordinary IPC
mesh as a leak. Fixed with explicit `dead == 0` / `alive == 0` cases. It only surfaced
because the healthy case finally got looked at; a verdict that is wrong on healthy
input will be believed when it is wrong on broken input too.

### The remaining owner: two paths `0004` structurally cannot reach

With `0004` confirmed present in the leaking binary, these are no longer speculative
alternatives — one of them is the leak.

Reading the ownership chain for the class-A case again, with "what else holds a
`Requests::Request` after completion" as the question, turns up two paths that are
consistent with completed requests (peer=DEAD) retaining an fd:

1. **`Response::m_request_server_request` holds the request by `RefPtr`**
   (`Fetch/Infrastructure/HTTP/Responses.h:68`), and `clone()` *copies that struct*
   (`Responses.cpp:209-210`) — so every clone of a response is another owner of the
   same `Requests::Request`. `0004` closes the fd from inside the completion branch, so
   it should still win for any request that reaches that branch; what it cannot cover
   is a request that never does.
2. **Paused body delivery.** A document navigation starts with
   `set_body_delivery_paused(true)` (`Fetching.cpp:2340`) and only
   `resume_body_delivery()` re-enables the notifier. The completion branch in
   `set_up_internal_stream_data` is driven by the read notifier and `request_done`; a
   request whose delivery is paused and never resumed never reaches
   `user_finish_called`, so `release_response_fd()` is never called on it — while
   RequestServer, having finished writing, has already closed its end. **That produces
   exactly the observed signature: peer=DEAD, retained, and unaffected by `0004`.**
   `LocalNavigable` has ~8 separate resume/stop call sites for this
   (`:488, :517, :564, :2271, :2299, :2310, :2328` plus
   `stop_or_resume_response_body_delivery`), i.e. it is a
   "every early return must remember to resume" contract — the shape that leaks on the
   path nobody enumerated. Site isolation (`--site-isolation=top-level`, on in Ulf's
   run and not in my earlier ones) adds cross-process navigation paths through exactly
   this code.

#### Falsified locally, and the measurement that replaces the guess

I built the workload for (2): 330 top-level navigations to a URL whose body dribbles,
each abandoned 250 ms in (before the body completes), with `--site-isolation=top-level`
and the memory cache on. **Flat — 0 dead, no growth.** So (2) as I constructed it is not
sufficient either, and I am now two falsified hypotheses deep on a leak I cannot
reproduce.

That is the point at which guessing again is the wrong move. The two candidates above
look identical in every column the census prints, but they differ in one field `ss`
already reports and I was throwing away: **`Recv-Q`, the bytes sitting unread in the
socket.**

- `Recv-Q > 0` — the body was **never drained**. The consumer stopped reading, so the
  completion branch that closes the fd was never reached. The fix is resume-or-close on
  the abandoned path.
- `Recv-Q == 0` — the body **was fully read**; the descriptor is merely still *owned*.
  The fix is dropping the surviving reference (the `RefPtr` in
  `Response::m_request_server_request`, which `clone()` copies).

Same signature, opposite fixes, one field. The census now prints
`of retained peer=DEAD: body UNREAD=N body drained=M` and names which mechanism the
numbers imply. Ulf's existing `--all` run already collects the `ss` line this comes
from, so this costs nothing beyond re-running the census he has run before.

Of the two, (2) is the one that explains the *combination* Ulf measured — completed-and-closed by the peer,
retained, and indifferent to a fix that works locally — rather than only part of it.
The memory cache, which I suspected next because `--enable-http-memory-cache` was on
his command line and I had never tested it, is **ruled out** by reading
`HTTP::MemoryCache::Entry` (`Libraries/LibHTTP/Cache/MemoryCache.h:23`): it stores
status, headers and `Core::ImmutableBytes`, and never holds a `Requests::Request`, so
it cannot retain a descriptor.

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
