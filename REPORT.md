# Final Report — Hobby Server Monitor

## Time Spent

Rough breakdown, not a timesheet. These are the wall-clock lengths of the
AI-assisted working sessions on 2026-10-05 (see [AI Tool Usage](#ai-tool-usage));
time spent reading and reviewing the code afterwards is not included.

| Area | Time |
| --- | --- |
| Backend (API, auth, authorization), v1 then the Falcon rewrite | ~2 h |
| Dashboard (Astro frontend) | ~1 h |
| LXD integration (pylxd, exec/terminal, storage pools) | ~0.5 h |
| Background collector / TSDB (incl. TinyFlux benchmarks) | ~1 h |
| Debugging (clock skew, Falcon/TinyFlux quirks, WSL/Windows ports, OAuth setup) | ~1.5 h |
| Documentation / report / measurements | ~1 h |
| **Total** | **~7 h** |

## Key Decisions

The brief lists twelve open questions. All are answered below, the first five
in most depth.

### 1. Where is an authorization decision made, and how is a new endpoint kept from missing it?

**Chosen.** One Falcon middleware (`hsm/web/policy.py`) runs before every
responder. Each resource class *declares* a policy: `PUBLIC`, `USER`, `ADMIN`,
or `CONTAINER` (admin, owner or assignee of the `{name}` in the URL). It can
be one policy for the class or a per-method dict.

Default deny works at three levels:
- **Runtime:** a responder without a policy gets a 500 and is logged.
- **Startup:** `check_routes()` refuses to start the app if any route lacks a
  policy.
- **Tests:** two tests walk the route table and assert 401 without a session
  for every protected responder, and 403 for a container user on every admin
  responder.

So "next month's endpoint" either declares a policy or fails CI and refuses
to boot. CSRF is enforced in the same place for every non-GET. Roles and
grants are re-read from SQLite on each request, never taken from the client.

**Rejected:**
- Per-handler decorators or checks: forgetting one is silent.
- Checking inside each SQL query: it scatters the policy.
- A Falcon `before` hook per resource: still opt-in.

### 2. How does a signed-in user stay signed in, and what does logging out actually do?

**Chosen: server-side sessions.**
- The cookie (`HttpOnly; SameSite=Lax; Secure` behind TLS) holds an
  HMAC-signed random token.
- SQLite stores `sha256(token)`, the CSRF token, an absolute expiry (8 h) and
  `last_seen_at` for a 60-minute idle timeout. That timestamp is written at
  most once a minute, not on every request.
- **Logout deletes the row**, so the cookie is dead everywhere at once.
  Revoking a user cascades to all of their sessions.
- Long-lived connections re-check: SSE on every push, the terminal every 5 s.
  An open shell closes within seconds of logout, expiry, demotion or
  unassignment.

**Rejected: a stateless signed or JWT cookie.** It can't be revoked before it
expires, and logout would only clear the browser's copy.

**What I got wrong first:** v1 signed the cookie with itsdangerous'
`TimestampSigner`. That signer rejects "future" timestamps, and on WSL2 the
wall clock stepped *backwards* by up to 1.45 s twice in 30 s, which randomly
logged users out (about 1 test run in 10 failed). Expiry now lives only in
the DB row, and the signature carries no time.

### 3. What exactly does a quota measure, and what happens when someone reaches theirs?

**Chosen: allocation, not usage.** A quota caps the sum of `limits.cpu`,
`limits.memory` and root disk size over the containers a user **owns**.
- Every container has at most one owner, and creating a container charges the
  chosen owner.
- Allocation is what the host has promised, and it is stable. A usage-based
  quota would flap with load and could only be enforced by killing work.

**At the limit:**
- Creating a container, raising a limit, or giving someone ownership is
  refused (409/422 with the numbers).
- Nothing running is stopped or shrunk. Lowering limits is always allowed.
- If an admin lowers a quota below current allocation, the user shows as
  *over quota* and can only shrink or delete.

**No overshoot under concurrency:** the check plus the reservation run under
one `asyncio.Lock`, and in-flight create jobs count against the quota.

**Bounds** come from `min(host capacity, owner's remaining quota)`, using live
`/1.0/resources` and storage-pool free space. The server re-checks them on
every request.

**Rejected:**
- Counting assigned containers (assign to two users, then charge whom?).
- Usage-based quotas (explained above).

### 4. pylxd needs privileged access to LXD. What does that grant, and what did I do about it?

Access to the LXD socket is **root on the host**: you can create a privileged
container that mounts `/`. The services run as a non-root `hsm` user, but that
user is in `lxd`, so I don't pretend it is unprivileged. What I did:
1. **No raw LXD surface.**
   - Fixed config keys only.
   - `security.privileged=false` and `security.nesting=false` are forced.
   - An image allowlist; pools, networks and profiles are validated against
     what LXD reports.
   - `limits.processes`; no device, idmap or raw config input.
2. **Two sandboxed units:**
   - `NoNewPrivileges`, `ProtectSystem=strict`, no capabilities,
     `@system-service` syscall filter, `MemoryDenyWriteExecute`.
   - The collector has **no network** (`PrivateNetwork=yes`, AF_UNIX only),
     so even a compromised collector can't exfiltrate anything.
3. **Admin is treated as root:** a tiny admin set, a config-managed bootstrap
   admin, a full audit trail.

**Not built (time):** LXD's TLS API with a *restricted* client certificate
confined to one project (`restricted=true`). That would remove root
equivalence, and it is the first follow-up.

### 5. How does the dashboard find out that something changed, and what does that cost while nobody is looking?

**Chosen.** The collector always polls (one LXD call every 10 s) and
atomically rewrites `latest.json`. The web process runs **one** watcher that
`stat`s that file once a second **only while at least one SSE client is
connected**, and pushes each new snapshot to every tab, filtered per user.
- With nobody looking, the web process does nothing.
- With N tabs, the cost is N small writes per tick. The collector's cost is
  independent of tabs (measured below).
- Actions such as start, stop and create update the UI from their own
  response; the next snapshot confirms within 10 s.

**Rejected:**
- Per-tab polling of LXD (cost scales with tabs).
- LXD's `/1.0/events` stream: it reports lifecycle changes but not metrics, so
  a poll would still be needed. It's a good future addition for instant state
  changes.
- WebSockets for metrics: SSE is one-way, simpler, and auto-reconnects.

### 6. What is in the metric store after a month of uptime?

Three tiers of TinyFlux files, each split into time segments:
- **raw 10 s:** hourly files, kept 6 h;
- **5-minute rollups:** daily files, kept 7 d;
- **1-hour rollups:** daily files, kept 30 d.

Retention deletes whole expired files, and nothing is rewritten in place.
At steady state that is 6 raw files, 8 `5m` files and 31 `1h` files, about
**13.6 MB for 10 containers**. That figure comes from the measured row size
(228 B/raw row; see `test_storage_is_bounded`).

Why it looks like this (measured, not guessed):
- A single TinyFlux file with a day of raw samples for 5 containers took
  **0.34 s and ~60 MB RSS** per query. That is why the data is split into
  segments.
- TinyFlux repeats field names on every CSV row. Keeping 5-minute rows for 30
  days measured at **45 MB**, which is why the tiers exist.

### 7. How real is the terminal, and what can an authenticated user do with it that I did not intend?

**It's fully real:**
- an interactive PTY via pylxd's `raw_interactive_execute`, bridged over
  WebSockets to xterm.js, with resize support;
- plus a one-shot "run a command" panel using pylxd `execute`, with a 30 s
  `timeout -s KILL` wrapper and output capped at 64 KiB *while streaming*.

**What a user can do:** anything root can do **inside that container**. That
is intended. Unintended risks, and what limits them:
- **Container breakout via a kernel or LXD bug:** unprivileged container,
  nesting off, AppArmor/seccomp, no devices. Keep the host patched.
- **Resource abuse:** CPU, memory and process limits, plus quotas.
- **Attacking the LAN from the container:** not mitigated beyond the
  `lxdbr0` NAT; this is a known gap.
- **Exhausting the dashboard:** at most 2 terminals per user and 8 in total,
  rate limits, idle timeout, and a 64 KiB frame cap.

The command text is never interpreted on the host. It is one argv element
passed to `/bin/sh -c` *inside* the container.

### 8. What does the schema do when a container is renamed, or deleted while assigned?

Everything is keyed by LXD's `volatile.uuid`, not by name.
- **Renamed outside the tool:** the collector updates the name within one
  tick (in two steps, so a swap can't violate `UNIQUE(name)`). Grants,
  ownership and TSDB history all follow the uuid.
- **Deleted outside the tool:** the collector sees the uuid vanish, deletes
  the row, lets the grants cascade, and audits `container_vanished`.
- **New container with a reused name:** it has a new uuid, so it does not
  inherit grants.
- **Race guard:** rows recorded in the last 30 s are never dropped, in case a
  create finished during the poll.
- **Ownership recovery:** ownership is also stored in LXD
  (`user.hsm.owner_id`), so a container created while the web API restarted
  is re-adopted with its owner.

### 9. How much data does a 24-hour chart move, and how much reaches the browser?

A 24 h chart reads two daily `5m` segment files: 288 rows for this
container, plus the other containers' rows in those files, which the query
skips. The server buckets the result to at most 360 points. Measured with 10
containers in the segments: **288 points, 58 KiB of JSON, 43 ms**. The browser
never sees raw samples for long ranges.
The 15 m chart (~90 points) comes from the raw tier, and 30 d from the `1h`
tier (720 rows bucketed to ~360).

### 10. What does a user see when LXD is down, slow, or answers unexpectedly?

- **pylxd timeouts:** connect 3 s, read 30 s. A hung LXD can't hang a request
  forever.
- **API:** mutations return **503 "LXD is unreachable"**, or LXD's own 4xx
  message.
- **Collector:** keeps ticking, records `up=0`, and publishes the last known
  values with `lxd_ok=false`.
- **Dashboard:** keeps showing those values, dimmed, under a red banner:
  "LXD is not answering since 11:02, showing last known values". If the
  collector itself stops, the banner says the data is stale and for how long.
- **Unexpected output:** parse errors in a TinyFlux segment are retried, then
  that segment is skipped, so one bad file doesn't fail the request.

### 11. How does the first admin come to exist, and why can't that be abused?

`BOOTSTRAP_ADMIN_EMAIL` is invited as admin by `init_db`, and is re-asserted
on every login. It can't be demoted or deleted from the UI.

**Rejected: "first sign-in wins"**, because it's a race. Anyone who finds the
URL before the owner gets root-equivalent control.

The environment variable is changed only by someone with write access to the
server's config, which is the same person who already owns the box. It
requires Google `email_verified=true`. Everyone else must be invited first,
and an uninvited account gets no DB row at all.

### 12. How does this run on the machine, and how does it come back after a reboot?

There are two systemd units, `hsm-collector` and `hsm-web`, both
`WantedBy=multi-user.target` and both ordered after `snap.lxd.daemon`.
`deploy/install.sh` sets them up:
- creates the user;
- copies the code to `/opt` (read-only);
- builds a venv;
- keeps secrets in `/etc/hobby-server-monitor/env` (0640, root:hsm) and state
  in `/var/lib/hobby-server-monitor`;
- runs `init_db` and enables the units.

Restart policy: the collector uses `Restart=always`, the web API
`Restart=on-failure`. Each has its own `MemoryMax` and `CPUQuota`. History
survives restarts because it's on disk; the collector flushes partial rollup
windows on SIGTERM.

## Issues Encountered and Solutions

1. **Built to the wrong brief first.** v1 (still in git history) followed a
   paraphrase of the task. It used FastAPI, polled LXD only while a browser
   was open, had no TSDB, quotas or invites, and let any Google account sign
   in. Reading the real brief, the collector requirement ("whether or not a
   browser is open") contradicted my core v1 optimisation. I rewrote it on
   the preferred stack: Falcon, pylxd, TinyFlux.
2. **Random logouts from clock skew.** Explained in decision 2. I measured
   the backward steps in WSL (2 in 30 s, up to 1.45 s) before changing
   anything.
3. **TinyFlux cost.** One big file was 0.34 s and 60 MB per query, so I split
   it into segments. Rows were ~320 B because field names repeat; I dropped
   redundant fields, stored integers as integers, and added tiering:
   63 MB, then 45 MB, then **13.6 MB** per month for 10 containers.
4. **Rollup bias.** The first sample after a start has no CPU rate. I counted
   it as 0, which skewed 5-minute averages (4.5% vs 5%). Now it's excluded.
5. **Falcon quirks:**
   - The test client runs the lifespan shutdown after every simulated
     request, which closed my DB connection.
   - `App` uses `__slots__`.
   - A WebSocket can't `receive` either text or binary, so I switched to a
     binary protocol with a type byte.
   - Rejecting a WebSocket before `accept` makes the browser show only 1006,
     so auth failures now accept-then-close with a 44xx code.
   - Redirects are now set on the response rather than raised, so the session
     cookie is kept.
6. **authlib's `jose` module is deprecated.** I verify ID tokens with
   `joserfc` directly and implemented PKCE S256 with `hashlib`, which removed
   authlib as a dependency.
7. **systemd environment pitfalls.** `EnvironmentFile=` overrides
   `Environment=`, and it doesn't strip inline comments. My `.env.example`
   would have pointed the service at a read-only path and crashed it on
   `int("6  # …")`. I fixed the example and the install script.
8. **Disk quotas on `dir` pools** can't be enforced or reported. I added a
   btrfs pool for testing. The form hides the disk slider for `dir` pools,
   and usage shows "n/a" there.
9. **Charts:** a stretched SVG `viewBox` made the axis text about 5 px. Charts
   now redraw at their real pixel width using a ResizeObserver.
10. **Dev-environment surprises:**
    - Docker Desktop holds port 8000 on Windows, so I used 8001.
    - `pkill -f` matched my own shell.
    - Headless Chromium needed `libnss3` and `libasound`, which I installed
      locally without root.
11. **Secret hygiene.** Real OAuth credentials once ended up in `.env.example`.
    I caught it before the first commit; the secret was blanked and `.env*`
    is gitignored.

## What You Learned

- Measure before optimising storage. TinyFlux's per-row field names, and the
  cost of parsing a whole file per query, were invisible until benchmarked.
- Wall-clock time isn't monotonic, even on a laptop. Anything security-related
  that compares timestamps needs tolerance, or should use server state.
- Default-deny authorization is cheap when the framework has one choke point
  (middleware) and the route table can be introspected in tests.
- On LXD, "unprivileged service user" is meaningless while that user is in
  the `lxd` group. The real fix is LXD's restricted TLS clients and projects.
- systemd sandboxing details matter: environment precedence, comment parsing,
  and `PrivateNetwork` still allows unix sockets.

## Bonus Features Implemented

- **116 pytest tests:**
  - route-table walks for 401 and 403;
  - real RS256 ID-token forgery, audience, nonce and expiry tests;
  - quota concurrency;
  - collector rename, delete and outage handling;
  - live tests against the real LXD socket.
- **Hardened systemd units** for both processes, plus an idempotent
  `install.sh`.
- **Interactive terminal** (real PTY, resize, idle timeout, revocation)
  alongside the required one-shot command runner.
- **Audit log** of logins, denials, every container and user action, and
  terminal and exec sessions, with an admin view.
- **Usage accounting page** (host vs allocation, per-user quota, per-container
  consumption from the TSDB), plus a quota panel for container users.
- **Freeze/unfreeze, CPU allowance (hard cap), ephemeral, autostart, network
  and profile selection**, all discovered from LXD at runtime.
- **Rename-safe identity** (uuid keys), and ownership recovery from LXD config.
- **Threat model** in the README.

## Resource Measurements

**Setup**
- **Machine:** WSL2 Ubuntu 24.04 on a 12-thread laptop, Python 3.12.3,
  LXD 5.21.8, 2 containers.
- **How the processes ran:** each process was a transient systemd **user**
  service with the units' limits:
  - collector: `MemoryMax=128M CPUQuota=10% Nice=10`;
  - web: `MemoryMax=192M CPUQuota=25%`.

  I had no sudo on the test machine to install the system units.

**Method**
- **CPU** = the change in the cgroup's `CPUUsageNSec` over the window, as a
  percentage of one core.
- **Memory** = `ps -o rss` (RSS), and the cgroup's `MemoryCurrent` from
  `systemctl show`.
- **Conditions:**
  - idle for 300 s after a 30 s warm-up;
  - then 60 s with 1 live SSE viewer, and 60 s with 10;
  - then 60 s idle again.

  The script is `deploy/measure.sh`, so anyone can reproduce the run.

Measured 2026-10-05, 11:42 to 11:52 (IST).

| Window | Collector CPU | Collector RSS (cgroup) | Web CPU | Web RSS (cgroup) |
|---|---|---|---|---|
| Idle, 0 browsers (300 s) | **0.048 %** | **39.8 MiB (22.2 MiB)** | **0.235 %** | **62.9 MiB (39.2 MiB)** |
| 1 live viewer (60 s) | 0.054 % | 39.8 MiB (22.2 MiB) | 0.247 % | 62.9 MiB (39.2 MiB) |
| 10 live viewers (60 s) | 0.046 % | 39.8 MiB (22.3 MiB) | 0.310 % | 62.9 MiB (39.3 MiB) |
| Idle again (60 s)* | 0.059 % | 39.8 MiB (22.3 MiB) | 0.669 % | 67.6 MiB (43.5 MiB) |

\* During this last window I also ran manual `curl` checks of the OAuth
redirect against the same web process, which is why its CPU and RSS are
higher.

Other observations:
- `systemd-cgtop` at the end: web 5 tasks, 0.4 %, 43.4 MiB; collector 1 task,
  0.0 %, 22.3 MiB.
- A viewer received 8 snapshots in 75 s (one per 10 s collector tick).
- TinyFlux on disk after ~1 h of collection for 2 containers: 67 KiB.
- Per-tier storage at steady state: ~13.6 MB for 10 containers (calculated
  from the measured 228 B/row).
- The web process's idle CPU floor (~0.24 %) is mostly uvicorn's own 0.1 s
  housekeeping tick.
- RSS is larger than the cgroup figure because shared-library pages are not
  charged to the service.

**Reading the numbers:** the collector's cost stays flat whether 0 or 10
browsers are open, because browsers never reach it.

## Known Limitations

- **The LXD socket is root-equivalent.** Restricted TLS client + project
  isolation is not implemented (decision 4).
- **One web process by design.** Rate limits, terminal counters and the
  create-job table live in its memory. A create that is in flight while the
  web API restarts finishes in LXD and is re-adopted by the collector with
  its owner, but its job status is lost.
- **Disk:** `dir` pools can't enforce or report disk, so the slider is hidden
  and usage shows n/a. Disk usage on btrfs is the *exclusive* usage from
  qgroups, which can look small for fresh containers that share blocks with
  their image.
- **Network:** containers on `lxdbr0` can reach the LAN; there are no egress
  rules.
- **Quota scope:** no per-user quota on the *number* of containers, or on
  network or IO.
- **Images:** the allowlist is in config, not editable from the UI. Alpine
  needs `/bin/sh` for the terminal, which the fallback handles.
- **Collector timing:** a missed tick (LXD slow for more than 10 s) shows as a
  gap in charts, which is honest but can look odd.
- **Not exercised end to end against Google in CI.** ID-token verification is
  tested with locally signed RS256 tokens. A real Google sign-in was done by
  hand in the browser on the dev machine.
- **OAuth state cookie:** it carries a timestamp. A backward clock step during
  the 10-minute login window can fail one sign-in, and a retry fixes it.

## AI Tool Usage

**Tool:** Claude Code (Anthropic, model Claude Opus), used as an agent in my
terminal and editor.

**What it did:** it wrote most of the code, tests and documentation in this
repository, working from my instructions and the task brief. It also ran the
tests, LXD commands and headless-browser checks.

**What I did:**
- set up Google OAuth and the WSL environment;
- tested the UI in my browser, including the real Google sign-in;
- made the decisions it asked me about: btrfs pool, commit identity;
- reviewed the code and design so I can explain and defend it.

**Accepted** after review: the overall architecture (separate collector,
segmented TinyFlux tiers, default-deny middleware, allocation-based quotas),
the pylxd wrapper, the test strategy, and the systemd hardening.

**Rejected, changed, or corrected along the way** (each visible in the commit
history):
- **The v1 design.** It polled only while browsers were open, which
  contradicts the brief, and it used FastAPI because it was familiar. It was
  rewritten on Falcon, pylxd and TinyFlux.
- **Session signing.** `TimestampSigner` caused random logouts under clock
  skew; it was replaced.
- **Bugs the AI introduced and then fixed after tests or review caught them:**
  - a non-reentrant lock that would deadlock inside transactions;
  - a rollup average biased by a missing first sample;
  - inline comments in `.env.example` that would crash systemd;
  - path variables in the env file overriding the unit's paths;
  - chart colours read before the chart existed (both lines would have been
    the same colour);
  - a stretched-SVG chart with unreadable text;
  - a slider value clamped by the HTML default `max`.
- **Over-reach.** It deleted nothing without asking. A blanket `rm -rf` of
  the earlier attempt was blocked by my tooling's guard, and it kept the files
  instead.
- **Attribution.** I asked for AI attribution to be kept out of the commit
  trailers; it is disclosed here instead.
