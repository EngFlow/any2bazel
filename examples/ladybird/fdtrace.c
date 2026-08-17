/*
 * fdtrace: name the code path that opened every fd a process never closed.
 *
 * Build once, LD_PRELOAD it into an UNMODIFIED browser. No patch, no rebuild of
 * Ladybird, no particular commit -- which is the point: the fd census can say
 * WHICH CLASS of fd is leaking, but not which code opened it, and every attempt to
 * answer that so far has been me guessing at a tree I cannot reproduce.
 *
 *     cc -shared -fPIC -O2 -g -o fdtrace.so fdtrace.c -ldl
 *     FDTRACE_OUT=/tmp/fdtrace.%d.log \
 *       LD_PRELOAD=$PWD/fdtrace.so ./Build/full/bin/Ladybird   # or the Bazel binary
 *
 * Then, after the leak has grown:
 *
 *     python3 fd_census.py <webcontent-pid>        # how many, and which class
 *     python3 fdtrace_report.py /tmp/fdtrace.<webcontent-pid>.log
 *
 * The report leads with WHO SENT each still-open attachment (SO_PEERCRED on the
 * receiving socket), because that is the field that discriminates. An SCM_RIGHTS fd
 * is materialised by the kernel on the IPC read thread, so every attachment from
 * every peer shares one identical acquisition stack -- grouping by stack alone
 * produces a wall of identical frames and names nothing. `Requests::Request` fds and
 * fds that never reach a Request look completely different here, which is the
 * question the in-process census could not answer (it only ever saw fds that DID
 * reach a Request).
 *
 * How it works: fds enter WebContent from RequestServer as SCM_RIGHTS attachments
 * on recvmsg(), not via open(), so this wraps the acquiring calls (recvmsg,
 * socketpair, socket, dup/dup2/dup3, pipe/pipe2, open/openat, accept) and the
 * releasing ones (close). Each acquisition records a backtrace; each close drops
 * it. Whatever is left at exit -- or whatever the report finds live -- is the leak,
 * with its creation stack.
 *
 * Deliberately simple and allocation-light on the hot path: a fixed-size table
 * indexed by fd, holding raw return addresses only. Symbolisation happens offline
 * in the report script (addr2line), because doing it in-process would be slow and
 * would perturb the very timing being measured.
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <execinfo.h>
#include <fcntl.h>
#include <link.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#define MAX_FDS 65536
#define MAX_FRAMES 24

struct entry {
    int in_use;
    int depth;
    unsigned long seq;
    void *frames[MAX_FRAMES];
    const char *how;
};

static struct entry g_table[MAX_FDS];
static unsigned long g_seq;
static FILE *g_out;
static __thread int g_in_hook; /* re-entrancy: backtrace() itself may call libc */

/* Real symbols, resolved lazily. */
static ssize_t (*real_recvmsg)(int, struct msghdr *, int);
static int (*real_close)(int);
static int (*real_socketpair)(int, int, int, int[2]);
static int (*real_socket)(int, int, int);
static int (*real_dup)(int);
static int (*real_dup2)(int, int);
static int (*real_dup3)(int, int, int);
static int (*real_pipe)(int[2]);
static int (*real_pipe2)(int[2], int);
static int (*real_accept)(int, struct sockaddr *, socklen_t *);
static int (*real_accept4)(int, struct sockaddr *, socklen_t *, int);

#define BIND(name)                                     \
    do {                                               \
        if (!real_##name)                              \
            real_##name = dlsym(RTLD_NEXT, #name);     \
    } while (0)

static void trace_open(void)
{
    const char *pattern = getenv("FDTRACE_OUT");
    char path[4096];
    if (!pattern)
        pattern = "/tmp/fdtrace.%d.log";
    /* one file per process: the browser is multi-process and they must not
     * interleave, or the report cannot attribute a stack to a pid */
    snprintf(path, sizeof(path), pattern, (int)getpid());
    g_out = fopen(path, "w");
    if (!g_out)
        return;
    setvbuf(g_out, NULL, _IOLBF, 0);
    /* The report needs the load addresses to turn return addresses into
     * file:line, and they differ per run (PIE + ASLR). */
    fprintf(g_out, "# fdtrace pid=%d\n", (int)getpid());
    FILE *maps = fopen("/proc/self/maps", "r");
    if (maps) {
        char line[1024];
        while (fgets(line, sizeof(line), maps))
            if (strstr(line, " r-xp ") || strstr(line, " r--p "))
                fprintf(g_out, "# map %s", line);
        fclose(maps);
    }
}

/* Which IPC connection did an attachment arrive on, and WHO sent it?
 *
 * This is the field the first version lacked, and the reason it mattered: the
 * acquisition stack of an SCM_RIGHTS fd is always the IPC read thread, identical
 * for every attachment from every peer, so a log full of identical stacks says
 * nothing about which subsystem is leaking. SO_PEERCRED on the receiving socket
 * names the SENDER process, which does discriminate: a response pipe from
 * RequestServer is the fd under investigation; an attachment from the Compositor or
 * ImageDecoder is something else entirely.
 *
 * Cached per socket fd -- one getsockopt and one /proc read per connection, not per
 * attachment, because this sits in the IPC hot path.
 */
#define MAX_PEER_CACHE 4096
static struct {
    int valid;
    int pid;
    char comm[32];
} g_peer_cache[MAX_PEER_CACHE];

/* A snapshot of pid -> comm taken at LOAD TIME, which is the only moment a
 * sandboxed process can take one.
 *
 * WHY THIS EXISTS -- the bug that produced `from=?(pid=2261433)`:
 * WebContent installs a landlock policy that grants exactly `/proc/self` and no
 * other path under /proc (Services/RendererSandboxLinux.cpp). So once the sandbox
 * is up, reading /proc/<peer-pid>/comm fails with EACCES -- and so would
 * /proc/<pid>/cmdline and /proc/<pid>/exe, because the barrier is per-path, not
 * per-file. SO_PEERCRED still works, since it is a syscall on a socket rather than
 * a path, which is why the log had a real pid and the name `?`.
 *
 * The constructor runs before main(), hence before the sandbox is installed, so a
 * snapshot taken here can still name the siblings that already exist -- and the
 * report resolves anything else from outside the sandbox. Every peer named `?`
 * before this was a *permission* failure being read as an unknown process. */
#define MAX_PROC_SNAPSHOT 8192
static struct proc_name {
    int pid;
    char comm[32];
} *g_proc_snapshot;
static int g_proc_snapshot_count;

static int read_comm(int pid, char *out, size_t out_size)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/comm", pid);
    FILE *f = fopen(path, "r");
    if (!f)
        return 0;
    int ok = fgets(out, (int)out_size, f) != NULL;
    fclose(f);
    if (!ok)
        return 0;
    char *nl = strchr(out, '\n');
    if (nl)
        *nl = 0;
    return out[0] != 0;
}

static void snapshot_proc_names(void)
{
    /* Best-effort and allocation is fine here: this is load time, single
     * threaded, long before the first IPC message. */
    g_proc_snapshot = calloc(MAX_PROC_SNAPSHOT, sizeof(*g_proc_snapshot));
    if (!g_proc_snapshot)
        return;
    DIR *d = opendir("/proc");
    if (!d)
        return;
    struct dirent *ent;
    while ((ent = readdir(d)) && g_proc_snapshot_count < MAX_PROC_SNAPSHOT) {
        if (ent->d_name[0] < '1' || ent->d_name[0] > '9')
            continue;
        int pid = atoi(ent->d_name);
        struct proc_name *slot = &g_proc_snapshot[g_proc_snapshot_count];
        if (read_comm(pid, slot->comm, sizeof(slot->comm))) {
            slot->pid = pid;
            g_proc_snapshot_count++;
        }
    }
    closedir(d);
}

static const char *snapshot_lookup(int pid)
{
    for (int i = 0; i < g_proc_snapshot_count; i++)
        if (g_proc_snapshot[i].pid == pid)
            return g_proc_snapshot[i].comm;
    return NULL;
}

static void peer_of(int sock, int *out_pid, const char **out_comm)
{
    *out_pid = -1;
    *out_comm = "?";
    if (sock < 0 || sock >= MAX_PEER_CACHE)
        return;
    if (!g_peer_cache[sock].valid) {
        struct ucred cred;
        socklen_t len = sizeof(cred);
        g_peer_cache[sock].valid = 1;
        g_peer_cache[sock].pid = -1;
        strcpy(g_peer_cache[sock].comm, "?");
        if (getsockopt(sock, SOL_SOCKET, SO_PEERCRED, &cred, &len) == 0) {
            int pid = (int)cred.pid;
            const char *how = "comm";
            g_peer_cache[sock].pid = pid;
            if (!read_comm(pid, g_peer_cache[sock].comm,
                           sizeof(g_peer_cache[sock].comm))) {
                /* Denied by the sandbox (or the peer already exited): fall back to
                 * the pre-sandbox snapshot. */
                const char *name = snapshot_lookup(pid);
                how = "snapshot";
                if (name) {
                    snprintf(g_peer_cache[sock].comm,
                             sizeof(g_peer_cache[sock].comm), "%s", name);
                } else {
                    strcpy(g_peer_cache[sock].comm, "?");
                    how = "unresolved";
                }
            }
            /* One line per CONNECTION, so the report can resolve the pid itself --
             * from outside the sandbox, where /proc is readable -- even when this
             * process could not. */
            if (g_out)
                fprintf(g_out, "# peer sock=%d pid=%d comm=%s via=%s\n", sock, pid,
                        g_peer_cache[sock].comm, how);
        } else if (g_out) {
            fprintf(g_out, "# peer sock=%d pid=-1 comm=? via=no-peercred\n", sock);
        }
    }
    *out_pid = g_peer_cache[sock].pid;
    *out_comm = g_peer_cache[sock].comm;
}

static void record_from(int fd, const char *how, int sock)
{
    if (fd < 0 || fd >= MAX_FDS)
        return;
    if (g_in_hook)
        return;
    g_in_hook = 1;

    struct entry *e = &g_table[fd];
    e->in_use = 1;
    e->how = how;
    e->seq = __sync_fetch_and_add(&g_seq, 1);
    e->depth = backtrace(e->frames, MAX_FRAMES);

    if (g_out) {
        fprintf(g_out, "+ fd=%d seq=%lu how=%s", fd, e->seq, how);
        if (sock >= 0) {
            int pid;
            const char *comm;
            peer_of(sock, &pid, &comm);
            fprintf(g_out, " sock=%d from=%s(pid=%d)", sock, comm, pid);
        }
        fprintf(g_out, " stack:");
        for (int i = 0; i < e->depth; i++)
            fprintf(g_out, " %p", e->frames[i]);
        fprintf(g_out, "\n");
    }
    g_in_hook = 0;
}

static void record(int fd, const char *how)
{
    record_from(fd, how, -1);
}

static void forget(int fd)
{
    if (fd < 0 || fd >= MAX_FDS)
        return;
    if (g_table[fd].in_use && g_out && !g_in_hook) {
        g_in_hook = 1;
        fprintf(g_out, "- fd=%d seq=%lu\n", fd, g_table[fd].seq);
        g_in_hook = 0;
    }
    g_table[fd].in_use = 0;
}

__attribute__((constructor)) static void fdtrace_init(void)
{
    trace_open();
    /* Before main(), so before the renderer's landlock policy narrows /proc down to
     * /proc/self. After that point no peer name can be read from inside. */
    snapshot_proc_names();
}

ssize_t recvmsg(int sockfd, struct msghdr *msg, int flags)
{
    BIND(recvmsg);
    ssize_t r = real_recvmsg(sockfd, msg, flags);
    if (r < 0 || !msg)
        return r;
    /* SCM_RIGHTS is how a response pipe arrives from RequestServer: the fd is
     * created by the KERNEL here, so no open()-style hook can ever see it. This
     * is the hook that matters for the Ladybird leak. */
    for (struct cmsghdr *c = CMSG_FIRSTHDR(msg); c; c = CMSG_NXTHDR(msg, c)) {
        if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS)
            continue;
        size_t payload = c->cmsg_len - CMSG_LEN(0);
        size_t count = payload / sizeof(int);
        int *fds = (int *)CMSG_DATA(c);
        for (size_t i = 0; i < count; i++)
            record_from(fds[i], "recvmsg/SCM_RIGHTS", sockfd);
    }
    return r;
}

int close(int fd)
{
    BIND(close);
    forget(fd);
    return real_close(fd);
}

int socketpair(int d, int t, int p, int sv[2])
{
    BIND(socketpair);
    int r = real_socketpair(d, t, p, sv);
    if (r == 0) {
        record(sv[0], "socketpair");
        record(sv[1], "socketpair");
    }
    return r;
}

int socket(int d, int t, int p)
{
    BIND(socket);
    int r = real_socket(d, t, p);
    if (r >= 0)
        record(r, "socket");
    return r;
}

int dup(int old)
{
    BIND(dup);
    int r = real_dup(old);
    if (r >= 0)
        record(r, "dup");
    return r;
}

int dup2(int old, int new_fd)
{
    BIND(dup2);
    int r = real_dup2(old, new_fd);
    if (r >= 0)
        record(r, "dup2");
    return r;
}

int dup3(int old, int new_fd, int flags)
{
    BIND(dup3);
    int r = real_dup3(old, new_fd, flags);
    if (r >= 0)
        record(r, "dup3");
    return r;
}

int pipe(int fds[2])
{
    BIND(pipe);
    int r = real_pipe(fds);
    if (r == 0) {
        record(fds[0], "pipe");
        record(fds[1], "pipe");
    }
    return r;
}

int pipe2(int fds[2], int flags)
{
    BIND(pipe2);
    int r = real_pipe2(fds, flags);
    if (r == 0) {
        record(fds[0], "pipe2");
        record(fds[1], "pipe2");
    }
    return r;
}

int accept(int s, struct sockaddr *a, socklen_t *l)
{
    BIND(accept);
    int r = real_accept(s, a, l);
    if (r >= 0)
        record(r, "accept");
    return r;
}

int accept4(int s, struct sockaddr *a, socklen_t *l, int f)
{
    BIND(accept4);
    int r = real_accept4(s, a, l, f);
    if (r >= 0)
        record(r, "accept4");
    return r;
}
