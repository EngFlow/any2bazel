# Ladybird: fd leaks in WebContent — outcome, lessons, and the investigation that got there

**Status.**

- **Bug 1 — one socket fd leaked per completed HTTP request: fixed upstream** by PR
  [#11041](https://github.com/LadybirdBrowser/ladybird/pull/11041) (three commits by
  sideshowbarker). The overlay carries those three patches (`examples/ladybird/patches/`),
  annotated in place, each with an `.effect-grep` so `apply_overlay.sh --verify` recognises
  the merged fix on a tree newer than our pin `71fb301a`. Verified running: Bazel-built
  browser, pages load, census flat, no crash.
- **Bug 2 — a `MessagePort` dropped without `close()` leaks its socketpair + 2 pipe
  pairs: still open**, not reported upstream (todo `dff69fde`). Distinct signature,
  distinct site, unaffected by #11041.
- **Two patches of our own were deleted, not rebased.** Mine were the wrong mechanism and
  one of them **crashed Ulf's browser**. That is the substance of §*Lessons* below.

**How to read this document.** §*Outcome*, §*Lessons*, §*Bug 2*, §*Instruments* and
§*Why one `EMFILE` kills three processes* are current. Everything under
**§Appendix A** is the investigation as it happened, preserved for method — its central
model of the leak (*"the fd has a surviving owner, so close it explicitly at the point the
body is proven complete"*) is **falsified**, and it is the model that produced the
crashing patch. Do not lift a fix out of the appendix.

---

## Outcome: one deferred teardown, missed on three paths

The leak was never that something *held* the fd too long. `Requests::Request` already had
a correct, **deferred** teardown (`defer_teardown()`, `LibRequests/Request.cpp:284`) that
drops the callbacks — and with them the `GC::Root`s and the fd. It was simply **not
reached** on three separate paths. Upstream adds the missing call sites:

| upstream commit | path that never tore down | our state before |
|---|---|---|
| 1/3 `LibRequests+LibWeb: Release response pipes when requests complete` | ordinary completion: `did_finish()` ran `on_finish` and returned | we had this call site, **ordered wrongly** (see §Lessons) |
| 2/3 `LibWeb: Release response pipes when fetches are canceled` | `abort()`/`terminate()` never told the network layer | a class we **never diagnosed** — no census could have shown it |
| 3/3 `LibWeb: Tear down a navigation parked for content sniffing` | headers arrived, fewer bytes than the sniff threshold, navigable destroyed → no document, so `Document::abort()` has no controller to stop and only the arrival callback (which never runs) would release the request | **exactly** the lead we had and could not reproduce |

Patch 1 also adds `openResponsePipeCount()` to `Internals.idl`, which regenerates the
LibWeb bindings; the overlay's codegen picks that up correctly (verified: the IDL change
invalidated and rebuilt the binding set, 1571 actions).

Ours, deleted:

- `0001` (tear down when the body is delivered) — same call site as upstream's 1/3, but
  it called `defer_teardown()` **after** `user_on_finish` instead of before. Latent
  use-after-free; see §Lessons.
- `0002` (`release_response_fd`) — closed the fd synchronously inside the completion
  branch, on the theory that a surviving reference pinned it. **That theory is falsified
  by upstream's patches working**, and the patch itself crashed his browser. It has no
  upstream counterpart, which was the signal.

## Lessons

### 1. The crash: ownership while the stack is still unwound

Ulf ran the Bazel-built browser with my two patches. Pages loaded, the fd census was flat
— *"I also didn't see immediate socket leak, so that's good"* — and then, minutes later:

```
VERIFICATION FAILED: m_ptr at ./AK/OwnPtr.h:134
#0 in AK::Function<void ()>::CallableWrapper<Requests::Request::
     set_up_internal_stream_data(...)::{lambda()#2}>::call()
#1 in Core::Notifier::event(Core::Event&)
```

`{lambda()#2}` is the read notifier's `on_activation` — **the frame that calls
`on_finish`**. My `release_response_fd()`, invoked from inside the completion branch of
`on_finish`, set `m_internal_stream_data->read_stream = nullptr` while `on_activation` was
still on the stack and still about to dereference it:

```cpp
        } while (true);

        if (m_internal_stream_data->read_stream->is_eof())   // Request.cpp:376
            m_internal_stream_data->read_notifier->close();
```

`AK::OwnPtr::operator->` is `VERIFY(m_ptr)` (`AK/OwnPtr.h:134`), so the null became a trap
and then `SIGILL`. **A use-after-null one stack frame up from the code I changed.**

My framing was upside down. I had argued `0002` was needed because *"collectable"* and
*"closed"* are different claims, and for an fd received over IPC only the second is the
bug. That sentence is still true, and it was the wrong question. The question is not
*when is the fd closed* but **who owns the state while the stack is still unwound**. The
completion branch runs **inside** the read loop's callback; anything it destroys, the
caller may still touch. Upstream's patches do not close the fd at all — they make the
existing **deferred** teardown *reachable*, and it is deferred precisely so it cannot
destroy state a live frame is using. The mechanism I reached for (destroy it now,
synchronously, at the point I can prove the body is done) was in direct conflict with the
one the code already had.

Generalisable: **when a codebase already has a teardown and it is deferred, the deferral
is the design, not an accident to be optimised past.**

### 2. "Never observed here" is a statement about my workloads

While reading #11041 I *had already spotted* the same class of bug in my own `0001`:
upstream calls `defer_teardown()` **before** `user_on_finish`, mine called it after.

```cpp
m_internal_stream_data->user_finish_called = true;
defer_teardown();                 // upstream: BEFORE
user_on_finish(...);
                                  // ours (0001): AFTER
```

Upstream's commit message states the reason: *"the deferred task also keeps the Request
alive if the callback drops the last ref"* — `defer_teardown()` captures
`NonnullRefPtr(*this)` inside the deferred lambda, so calling it first pins the `Request`
across `user_on_finish`. Mine ran after the callback returned, and `user_on_finish` is the
fetch completion path, which is exactly where the last reference can go away (a `Response`
holds its `Request` by `RefPtr`, `Responses.h:226`). I wrote at the time that this was
*"the kind of latent ordering bug that shows up as a rare crash on someone else's
machine"* — and then carried the patch anyway, because it had never fired for me.

It fired for him.

### 3. A falsified workload is evidence about the workload

The lead that turned out to be right (upstream's 3/3) was open in this document for days
because I built two workloads for it — 330 abandoned dribbling navigations with site
isolation + memory cache, and 300 disk/memory cache hits — and **both came out flat**. I
came close to treating that as evidence against the hypothesis. Upstream's reproducer is
more specific than either: an **iframe removed** while its response has headers but fewer
bytes than the sniff threshold, i.e. the navigable must be *destroyed* while parked, not
merely the navigation abandoned. My workloads exercised everything except the condition
that mattered.

### 4. Some classes are reading findings, not measurement findings

Upstream's 2/3 (cancel paths never telling the network layer) is invisible to
`fd_census.py`: a cancelled fetch leaking one fd looks identical to any other retained
`peer=DEAD` socket. No amount of census data would have produced it. It came from asking
*do the two entry points that mark a fetch cancelled actually tell the network layer?* —
a question about the code, not about the numbers.

---

## Bug 2 (independent, still open): a `MessagePort` dropped without `close()` leaks its socketpair

Distinguishable by signature: this one leaks **2 pipes + 1 socket per port** (each
`TransportSocket` makes two `pipe2` pairs at `TransportSocket.cpp:165,170`), so a page
doing this shows a *pipe*-dominated census — which is how it was ruled out as the cause of
Ulf's sockets-only crash.

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

`MessagePort::entangle_with` (`LibWeb/HTML/MessagePort.cpp:222`/`230`) installs read hooks
capturing `GC::make_root(this)` and `GC::make_root(m_remote_port)` — strong explicit roots,
stored in a callback owned by the transport owned by the port, so **the port roots itself**.
`close()` reaches `disentangle()` (`:472`), the only thing that closes the transport; a port
that is merely dropped never gets there.

---

## Instruments (still valid, no patch or rebuild required)

- **`examples/ladybird/fd_census.py`** — *how many, which class, and is it still growing.*

  ```
  python3 examples/ladybird/fd_census.py --find WebContent
  python3 examples/ladybird/fd_census.py <pid> --watch 30
  python3 examples/ladybird/fd_census.py --all --watch 30     # every Ladybird-family process, ranked by growth
  ```

  Prints the category census, `peer DEAD`/`ALIVE` per socket, retained-vs-in-flight by
  age (so a freshly restarted process cannot be mistaken for a fix), `Recv-Q`-derived
  `body UNREAD=N body drained=M` for retained dead-peer sockets, which process holds the
  live peers, and a verdict. Verified to agree with an instrumented build on the same
  workload (143 DEAD / 4 ALIVE either way).

  **Read the rate, not the level**, and census a browser that has been *used*: a
  `--headless=text` snapshot is taken before the async loops finish. The workload that
  reproduces bug 1 is kept as `examples/ladybird/fdleak_workload.html` (it needs a real
  HTTP origin — `python3 -m http.server` next to it is enough).

- **`examples/ladybird/fdtrace.c` + `fdtrace_report.py`** — *which call site.* Build once
  (`cc -shared -fPIC -O2 -g -o fdtrace.so fdtrace.c -ldl`), `LD_PRELOAD` into an
  unmodified browser, and the report names **who sent** each fd that was never closed. It
  hooks `recvmsg`/SCM_RIGHTS because the leaked fd is **received, never opened** — a tracer
  wrapping `open()`/`socket()` sees nothing — and reads `SO_PEERCRED` on the receiving
  socket, because the acquisition *stack* of an attachment is always the IPC read thread
  and therefore identical for every peer. Validated on the known leak: `143 from
  RequestServer, 5 from Ladybird`, matching the census exactly. On a static build pass
  `--frames 20`; the default 14 stop at `TransportSocket::io_thread_loop`.

- **`examples/ladybird/patches/DIAGNOSTIC-fdleak-census.patch.txt`** — the in-process
  version, for the few fields only it can see (request id, `user_finish_called`, one-build
  A/B of a fix). Deliberately outside the `patches/*.patch` glob, so `apply_overlay.sh`
  never applies it.

- **`fd_census.py --build`: what the binary actually contains.** A pure-Python ELF reader
  over `.dynstr`/`.strtab`/`.debug_str` of the executable and every mapped `.so`, so a
  leak rate is reported next to the identity of the code that produced it. The reusable
  lesson is in its bug, not its feature: **a negative control only rules out
  "unreadable" if it cannot disappear for the same reason as the thing it guards.** With
  `ENABLE_LTO_FOR_RELEASE=ON` and a *static* link, a small internal-only method is inlined
  away leaving no symbol and no string — and so is the larger function I had chosen as the
  control. The probe therefore reported a fix "absent" when it was present, and Ulf was
  right and the tool was wrong. It now (a) reads `.debug_str` too, and (b) never reports a
  fix MISSING unless a symbol inlining *cannot* erase is visible (`UNINLINABLE_CONTROLS`:
  vtable/IPC-dispatched entry points), otherwise saying *"cannot tell"* and naming
  inlining. Both directions are tested against genuinely different builds.

**Census one-liner that works** (the one that does not is `sed 's/[0-9]*$//'`, which
leaves `pipe:[123]` unmerged and cost a wrong theory):

```
ls -l /proc/$P/fd | awk '{print $NF}' | sed 's/\[[0-9]*\]//' | sort | uniq -c | sort -rn
```

---

## Why one `EMFILE` kills three processes, not just a tab

`EMFILE` in WebContent surfaces through `MUST()` on an encode
(`LibWebView/CompositorConnection.cpp:62`) — an abort, not a propagated error. WebContent
dies; the Compositor's `VERIFY(connection)`
(`Services/Compositor/ConnectionFromClient.cpp:68`) then aborts; and the UI process's
`MUST` in `initialize_client` follows. **One `EMFILE` kills three processes**, which is
also why the failure is unattributable after the fact: nothing in the log names the
resource that ran out. Ulf's original report was

```
dup: Too many open files (errno=24) at Libraries/LibWebView/CompositorConnection.cpp:62
```

and two `/proc/<pid>/fd` censuses minutes apart — **17,423 → 17,497 `socket:`**, 18
`pipe:`, nothing else growing — is what turned it into a bug report. Monotonic, unbounded,
*sockets only*: that last word is what separated bug 1 from bug 2, and counting fds by
category rather than reasoning about which code looked suspicious is what produced it.

---
---

# Appendix A: the investigation as it happened — **superseded, do not lift fixes from here**

Everything below is the live reasoning of the investigation, in the order it was written.
It is kept because the method is worth reading and because several of the corrections are
corrections *of me*, which is the useful part. But its central model is wrong:

> **Falsified.** The appendix builds towards "the teardown ran and the fd is still owned
> by a surviving reference, therefore close the fd explicitly in the completion branch".
> Upstream's #11041 shows the teardown was simply **never reached** on three paths, and
> the explicit close is a use-after-null (§Lessons 1). The A/B tables, the census
> readings and the GC-root analysis are all sound; the **conclusion drawn from them** is
> not.

One classification that appears throughout and *is* still useful for reading a census —
just not for choosing a fix:

| class | `ss -np` peer inode | what happened |
|---|---|---|
| A: completed | `* 0` (**dead**) | request finished, RequestServer closed its half, WebContent retains a corpse |
| B: stalled | a real inode (**alive**) | `on_finish` never ran |

```
ss -np | grep "pid=$P," | grep -c ' \* 0 '   # class A (dead peer)
ss -np | grep -c "pid=$P,"                   # all of this process's unix sockets
```

The appendix's claim that *no teardown call site can reach class B* is the part upstream
disproved: patch 3/3 reaches it, by tearing down when the parked navigable is destroyed.

Original status line: reproduced locally on a **CMake** build of `f9e34731` (no Bazel
involved); present in upstream `master` (`50eef049`) by inspection.

---

## A.1 Bug 1: every completed HTTP request leaks one socket fd

### Reproduction

Serve a one-line `index.html` on localhost and load (now
`examples/ladybird/fdleak_workload.html`):

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
the WebContent process. Measured (clean build, no local patches):

| requests served | WebContent `socket:` fds | `pipe:` fds |
|---|---|---|
| 0 (baseline) | 5 | 18 |
| 103 | 107 | 18 |
| 202 | 208 | 18 |
| 595 | 587 | 18 |
| 1164 | 1157 | 18 |

**One socket per request, forever, and the pipe count never moves** — Ulf's census shape.
They are never reclaimed: flat for minutes after the loop stops, across repeated explicit
`internals.gc()` calls, and with `--disable-http-memory-cache --disable-http-disk-cache`.
`/proc/net/unix` shows them as connected `AF_UNIX` `SOCK_STREAM` halves — RequestServer's
response-body pipe (`RequestPipe::create`, `Services/RequestServer/RequestPipe.cpp:46`).
RequestServer closes its end (flat at 4 sockets); **WebContent never closes its end.**

Not specific to `fetch()`: plain subresource loads leak identically — 50 `<img>` loads
give 57 sockets, 200 give 207.

### Cause: an explicit GC root anchoring a cycle that spans the GC heap and the refcount heap

`internals.dumpGCGraph()` labels roots with their source location. After 202 requests and
two forced GCs:

```
808 Root nonstandard_resource_loader_file_or_http_network_fetch  Libraries/LibWeb/Fetch/Fetching/Fetching.cpp:2338
333 VM
 35 StackPointer
```

808 = **4 × 202**: the four callbacks passed to `ResourceLoader::load`, one set per
request, none ever released. Live cells scale in lockstep (202 requests → 202
`FetchedDataReceiver`, 606 `Response`, 808 `PendingResponse`, 406 `ReadableStream`).

The loop:

1. `nonstandard_resource_loader_file_or_http_network_fetch` creates four `GC::Function`s
   (`Fetching.cpp:2261`–`2333`) and passes them as `GC::Root<...>` into
   `ResourceLoader::load` (`Libraries/LibWeb/Loader/ResourceLoader.h:43`).
2. `GC::Root` is a **strong, explicit root**: `RootImpl`'s constructor registers itself in
   `Heap::m_roots` (`LibGC/Root.cpp:15`, `Heap.h:251`) and `gather_roots` marks every entry
   (`Heap.cpp:841`). It is only released when the `RootImpl` is destroyed.
3. `ResourceLoader::load` moves those roots into the lambdas it installs on the
   **refcounted** `Requests::Request` (`ResourceLoader.cpp:469`–`513`).
4. `on_headers_received` puts the same `Requests::Request` **back into the GC heap**:
   `response->set_request_server_request({… .request = request_server_request})`
   (`Fetching.cpp:2276`), held as `RefPtr<Requests::Request>` (`Responses.h:223`/`226`).

So: `Heap::m_roots` → `GC::Function` → captured `pending_response` / `stream` /
`fetched_data_receiver` → `Response` → `RefPtr<Requests::Request>` → the lambdas holding
the `GC::Root`s. Neither mechanism can break it. `Requests::Request` owns the response fd
(`m_fd`, closed only in `~Request`, `Request.cpp:58`, kept open meanwhile by the
`ReadStream` / `Core::Notifier`), so **the fd leaks with the cycle**.

The only thing that clears those callbacks is `Request::defer_teardown()` (`Request.cpp:284`),
called only from `stop()` (`:69`) and `did_transfer()` (`:274`). **Normal completion never
calls it** — `did_finish()` (`:251`) invokes `on_finish` and returns, and
`RequestClient::request_finished` (`RequestClient.cpp:205`) only removes its map entry.
*(This paragraph is the one upstream's 1/3 acts on, and it is correct.)*

### A/B that confirms it

Same page, but each fetch is immediately `AbortController.abort()`ed — `abort` reaches
`Request::stop()`, which *does* call `defer_teardown()`:

| variant | 200 requests |
|---|---|
| completed fetches | +200 sockets (207) |
| aborted fetches | **+0 sockets (flat 207 → 207)** |

The path that tears down does not leak; the path that succeeds does.

*(Note in hindsight: this A/B is also the shape of upstream's 2/3 — `abort()` reaching
`stop()` here is the JS `AbortController`, whereas the leaking cancel paths are
`Fetch`-level `abort()`/`terminate()`. The A/B answered "teardown works when reached" and
I read it as "only completion is unreached".)*

### Class B: the request that never finishes at all

The teardown hangs off the branch in `set_up_internal_stream_data`'s `on_finish` that
decides the body is complete:

```cpp
if (!user_finish_called && (!read_stream || read_stream->is_eof() || has_received_all_reported_bytes)) {
```

If a response never satisfies that *and* never reaches EOF, the block never runs, so
`user_on_finish` never runs. Repro: a server that sends `Content-Length: 48000`, writes 100
bytes and holds the socket open. 40 such loads, aged past 30 s:

```
FDLEAK live: in_flight(<30s)=0 retained(>=30s)=40
FDLEAK retained-bucket 40 x peer=ALIVE torn_down=false did_finish=false user_finish=false
                            request_done=false stream=true eof=false fd_open=true
```

`did_finish=false` is the tell; `peer=ALIVE` distinguishes it from class A at a glance.

Every completed-request shape I could construct — plain fetch, XHR, POST,
`response.clone()`, unread bodies, `<img>`/CSS/JS subresources, 404/302/cache hits,
truncated bodies, gzip/chunked, iframe navigations, mid-body `cancel()` — is flat at the
5-socket baseline with the fix on.

> **Superseded conclusion.** I wrote here that class B "is the cycle itself… there is no
> completion callback left to hook… the fix has to break the cycle at the `Response` end".
> Upstream's 3/3 hooks something else entirely: the **destruction of the parked
> navigable**. "No callback left to hook" was a failure of enumeration, not a property of
> the code.

### On fixing it — one obvious patch is wrong, and I verified that

Calling `defer_teardown()` at the end of `did_finish()` looks like the one-line fix. **It
is not**: I applied exactly that, rebuilt `liblagom-requests`, and the leak went to zero
(flat 5 sockets) *because loading broke* — pages rendered blank, since the body may still
be draining when `did_finish` arrives (`on_finish` re-checks `read_stream->is_eof()` /
`user_finish_called` for precisely this reason). Reverted, re-verified the clean build
renders and leaks again; the numbers above are all from the unpatched build. *(This result
stands, and it is why upstream's 1/3 hooks the user-finish callback, not `did_finish`.)*

### A.2 The wrong turn: "release the fd, not just the callbacks"

> **This is the falsified section, and it produced the patch that crashed his browser.**
> Kept in full because the reasoning is plausible end to end, which is the point.

The teardown patch was **necessary and insufficient** — as measured. With the
upstream-equivalent teardown applied Ulf still measured **97 sockets/min** (81→823
sockets, 73→815 dead-peer over 458 s) while every local workload was flat. Two instruments
said what was leaking:

- `fd_census.py`: every leaked socket was **peer=DEAD** — completed requests, class A, on a
  tree carrying the class A fix.
- `fdtrace.c`: every leaked fd was a **received SCM_RIGHTS attachment** whose sender was
  **RequestServer** (`SO_PEERCRED`; his log said `from=?(pid=2261433)` because a renderer's
  landlock policy grants only `/proc/self`, and `ps -p` named it). That pins it to the
  per-request response pipe from `RequestPipe::create()`, handed over by `request_started`.

I concluded the gap was **ownership, not liveness**: dropping the callbacks unpins the GC
cycle, but the fd is released only by `~Request` — or by the `ReadStream` inside
`m_internal_stream_data`, which the teardown merely *nulls*. So any surviving reference to
the `Requests::Request` keeps one fd per completed request alive even though the teardown
ran.

**Where that was wrong:** his tree's teardown was reached on *some* paths and not on the
three upstream fixes; the retained fds were requests whose teardown never ran at all, not
requests whose teardown ran and lost a race with a surviving owner. Both stories predict
"peer=DEAD, retained, indifferent to my fix". Nothing in the census distinguishes them —
what distinguishes them is reading which call sites exist.

`0004` (later renumbered `0002`) closed the fd explicitly at the point the code has
*already proven* the body is complete, deregistering the notifier before closing. Measured
A/B, same binary, same 200-completed-request workload:

| variant | sockets after 200 completed requests | peer DEAD |
|---|---|---|
| clean | 208 | 203 |
| `0004` applied | **6** | **0** |

with body delivery intact (`text=10 | stream=10/bytes=48000 | cancel=5`, plus a rendered
800x600 screenshot). It nulled `read_stream` from inside a callback whose caller
dereferences it two statements later, and that is the crash in §Lessons 1. **A green A/B
on my workload certified a patch that abort-crashed on his.**

Two process mistakes on the way there, both caught by Ulf within minutes and both now
pinned by tests:

1. I generated it by diffing against the *clean* commit, so it silently carried the
   predecessor's hunk and could not apply to the tree it was written for.
2. Correcting that, I shipped *two* variants — one for a tree with the teardown fix, one
   without. `apply_overlay.sh` applies `patches/*.patch` **by glob**, so one of two
   mutually-exclusive patches is guaranteed to fail. `patches/` is a **series, not a
   menu**; an alternative belongs outside the glob, like `DIAGNOSTIC-*.patch.txt`.

A test now reconstructs the pinned versions of every file the patches touch and applies
the whole series in glob order, exactly as the script does, so a patch that conflicts with
its predecessor fails in CI rather than on someone else's clone.

### A.3 Chasing the wrong process, then the wrong build

`0004` took my measurements to 0 and his tree was still leaking. Two things I got wrong
about **how I was measuring** — the reason the loop kept failing:

1. **Every census I requested was of WebContent.** That was my hypothesis, not a finding.
   `fd_census.py --all` now censuses every Ladybird-family process and ranks by growth
   rate, so the data names the process instead of me naming it.
2. **My server never exercised the disk cache.** Ulf ran `--http-disk-cache-mode enabled`
   against real sites; my test server sent no cache headers, so `handle_read_cache_state`
   never ran in any A/B. The large-cache-hit branch (`body_size >= PAGE_SIZE`) takes
   `take_body_file()` → `send_transferred_body_file_to_client()` — a **body file**, no
   response pipe, so a completed-request path `release_response_fd()` cannot reach. I built
   that workload (300 cache hits) and it stayed flat here.

His `--all` run settled the process question against me (pid 19291, 4880 s, 4386 samples,
`--site-isolation=top-level --enable-http-memory-cache`):

```
fds: total=7514  socket:=7487
sockets: 7487  peer DEAD=7479  peer ALIVE=6  unknown=2
retained(>=30s)=7436, of retained: peer DEAD=7428
growth over 4880s: sockets +7458 (+91.7/min), all peer DEAD
```

**WebContent**, **class A**, not in flight. The `--all` detour was the right instrument
answering a question I should not have needed to ask.

The number I could not interpret was the rate: **91.7/min against 97/min before the fix** —
equally consistent with *"`0004` does not address this leak"* and *"`0004` was not in that
binary"*, which demand opposite next steps. My instinct was to **ask**; that would have
been another round trip about a build that had already happened, answered from memory, and
it was the fourth time a number had arrived without the identity of the code that produced
it. **A leak rate without its build provenance is not a measurement anyone can reproduce,
including me.** The answer was in the binary, still mapped by the censused process, so
`fd_census.py --build` now reads it — and then got it wrong under LTO, which is the
negative-control lesson recorded in §Instruments.

One more thing the probe caught: run against a *healthy* browser, the verdict read
`mixed DEAD/ALIVE -> both classes present` for 0 dead and 5 live sockets, because both
classification branches required a 10x majority and zero-of-a-class fell between them —
while also reading the ordinary IPC mesh as a leak. Fixed with explicit `dead == 0` /
`alive == 0` cases. It surfaced only because the healthy case finally got looked at; **a
verdict that is wrong on healthy input will be believed when it is wrong on broken input
too.**

### A.4 The two candidate owners, and the field that would have told them apart

With `0004` confirmed present in the leaking binary I read the ownership chain again,
asking "what else holds a `Requests::Request` after completion":

1. **`Response::m_request_server_request` holds the request by `RefPtr`**
   (`Responses.h:68`), and `clone()` *copies that struct* (`Responses.cpp:209-210`) — so
   every clone is another owner of the same `Requests::Request`.
2. **Paused body delivery.** A document navigation starts with
   `set_body_delivery_paused(true)` (`Fetching.cpp:2340`); only `resume_body_delivery()`
   re-enables the notifier, and the completion branch is driven by that notifier +
   `request_done`. A request paused and never resumed never reaches `user_finish_called`,
   so `release_response_fd()` is never called on it — while RequestServer, done writing,
   has closed its end. **Exactly the observed signature.** `LocalNavigable` has ~8
   resume/stop call sites (`:488, :517, :564, :2271, :2299, :2310, :2328` plus
   `stop_or_resume_response_body_delivery`) — an "every early return must remember to
   resume" contract, the shape that leaks on the path nobody enumerated. Site isolation
   (on in his run, off in my earlier ones) adds cross-process paths through this code.

(2) was the right neighbourhood and became upstream's 3/3. My workload for it — 330
abandoned dribbling navigations, site isolation, memory cache — was **flat**, for the
reason in §Lessons 3.

Rather than guess a third time, I added the one field `ss` reports and I had been
discarding: **`Recv-Q`, the bytes sitting unread in the socket.**

- `Recv-Q > 0` — the body was **never drained**; the completion branch was never reached.
- `Recv-Q == 0` — the body **was fully read**; the descriptor is merely still *owned*.

Same signature, opposite fixes, one field; the census now prints `of retained peer=DEAD:
body UNREAD=N body drained=M`. That instinct was right and generalises: **when two
hypotheses predict identical numbers, look for the column you are throwing away before
building a third workload.**

The memory cache was **ruled out** by reading `HTTP::MemoryCache::Entry`
(`Libraries/LibHTTP/Cache/MemoryCache.h:23`): it stores status, headers and
`Core::ImmutableBytes`, never a `Requests::Request`, so it cannot retain a descriptor.

### A.5 Earlier wrong theories (kept as a count)

Three earlier theories about Ulf's crash were wrong (lost fd acks; an
`AnonymousBuffer`/image-cache miss; leaked `TransportSocket`s), and one was wrong only
because my census one-liner was broken (`sed 's/[0-9]*$//'` does not strip `pipe:[123]`,
and piping through `head` hid the answer). My first diagnosis of his crash —
`MessagePort::entangle_with`, bug 2 — was wrong *for his crash* because it leaks 4 pipes
per socket and his census was sockets-only.

Everything in this appendix is a counted fd, a labelled GC root, or an A/B against a
rebuilt binary. The conclusion was still wrong, which is the whole lesson: **the data was
never the weak link — the enumeration of possible causes was.**
