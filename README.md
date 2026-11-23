# ram-not-for-sam

A Linux-only Python 3 RAM holder. The primary `./ram-not-for-sam` grows toward
maximal resident filler usage, leaving a small explicit emergency reserve:
**256 MiB of `MemAvailable` by default**. It uses ordinary anonymous private
read/write mappings, touches every interior page once, and never calls `madvise`.

```sh
./ram-not-for-sam --help
./ram-not-for-sam --amount 512M
./ram-not-for-sam --reserve 512M
./ram-not-for-sam --amount 16G --reserve 256M --verbose
```

`--amount` optionally caps **currently HELD interior bytes**, not lifetime
allocations. At the cap the program stays running and monitors its mappings;
discarded bytes can be filled again later. Both size options accept integer
bytes or binary `K`, `M`, `G`, `T` suffixes (case insensitive, including zero).
Allocations round down to whole system pages; a sub-page remainder is unused.
No compiler or build step is needed. Run safe tests with `make test`.

The final primary policy is deliberately simple, not the experimental adaptive
allocator below:

* Each pass reads `/proc/meminfo` and `/proc/self/smaps`, then grows by at most
  **one 512 MiB chunk**, limited by `MemAvailable - reserve` and the current
  amount allowance. Passes are separated by **one second**, plus processing
  time. Reserve/headroom stops growth; it never evicts unswapped filler just
  because another workload increases its RAM usage.
* Each chunk reserves its page-rounded interior **plus two untouched guard
  pages**, one on either end. Checked libc `mprotect` calls mark both guards
  `PROT_NONE`, preventing neighboring filler interiors from merging.
* The program attributes exact `Swap:` (not `SwapPss`) and `Rss:` fields only to
  its tracked interiors. Split VMAs must cover an interior completely without
  gaps/overlaps; spanning, missing, duplicate or malformed required accounting
  is fatal rather than guessed. Unrelated Python heap swap is ignored.
* **Every filler chunk with positive mapping Swap is closed**, without reading,
  copying or touching its old contents. Unswapped chunks remain held. No growth
  occurs in that pass. A **10-second monotonic cooldown** follows cleanup;
  monitoring continues, further swapped chunks are discarded, and growth
  automatically resumes after cooldown using fresh telemetry and headroom.
  No global swap counter, PSI or process-wide `VmSwap` drives this policy.

Default stderr logs announce only semantic operation changes, with the usual
sarcastic Sam-themed commentary. `-v`/`--verbose` instead reports each pass:
held bytes, measured mapping resident/swap bytes, available memory, reserve and
remaining cooldown. Resident/swap values are explicitly pre-action samples.
Errors and a cleanup summary are always visible. Linux and readable required
`/proc` files are checked before allocation. Telemetry, mapping, page-touch or
guard-protection failures stop the program with a nonzero exit and cleanup.
Close failures are reported and never counted as freed bytes.

Ctrl-C and SIGTERM release mappings and restore previous signal handlers.
SIGKILL/OOM kills cannot execute Python cleanup; the kernel reclaims mappings
on process exit. **Best effort only, not OOM protection:** the reserve is a growth
budget, not a guarantee. Accounting lag, racing workloads, overcommit, swap
observation delays and allocation/touch failures can defeat it. Host `/proc`
headroom is not a cgroup/container limit. Guard pages consume virtual address
space and mapping/page-table overhead is not included in the interior byte cap.
Tests use mocked policy inputs and a few-page real guarded-mapping smoke test,
never a large allocation or a swap stress test.

## Swap worker cycling

`./swap-cycle` is a separate Linux-only mode; it does not change the allocator
above. It requires configured swap, starts independent worker processes (each
allocates and touches exactly 1 GiB of anonymous memory and requests
`MADV_COLD`). It starts one ready worker immediately, then starts another every
60 seconds while polling swap usage once per second. Once system-wide swap usage
reaches 50%, it indefinitely terminates the oldest worker, waits 60 seconds, and
starts a replacement, without rechecking the threshold. Press Ctrl-C to
terminate and reap all workers. There is deliberately no RAM ceiling, so use
this only where that behavior is intended. The script fails clearly if
`MADV_COLD` is unsupported.

The worker mode is internal (`./swap-cycle --worker`). Unit tests cover swap
meminfo parsing and threshold logic without starting workers or allocating
large mappings.

## Requesting pageout

`./pageout-2g` is a separate Linux-only script. It maps exactly 2 GiB of
anonymous private read/write memory, touches each page, requests `MADV_PAGEOUT`,
and holds the mapping until Ctrl-C. The request asks the kernel to reclaim the
pages; it is not a guarantee that they will be evicted. Anonymous pages may be
swapped, so btop may show swap usage increasing rather than RAM usage.
Allocation and pageout errors are reported, and the mapping is closed on exit.

## Lazy free hint

`./free-2g` is a distinct Linux-only script. It maps exactly 2 GiB of anonymous
private read/write memory, touches each page, requests `MADV_FREE` (using its
Linux UAPI value 8 if Python omits the constant), and holds the mapping until
Ctrl-C. Unlike `MADV_PAGEOUT`, `MADV_FREE` marks pages as lazily discardable:
their contents remain available until the kernel reclaims them under memory
pressure. It is not immediate pageout and does not promise that pages will use
swap. Errors are reported, and the mapping is closed on exit.

## Adaptive RAM holder

`./adaptive-ram` is a separate Linux-only feedback allocator. Existing scripts
and tuning are unchanged. It uses ordinary anonymous private read/write mappings,
touches every `mmap.PAGESIZE` page, and never calls `madvise`.

```sh
./adaptive-ram --ceiling 90 --amount 512M
./adaptive-ram --psi-some 5 --psi-full 1
./adaptive-ram --amount 512M --verbose
```

`--ceiling` defaults to 90 and accepts finite percentages from 0 through 100.
`--psi-some` and `--psi-full` set interval stall thresholds (defaults **5%** and
**1%**, respectively). Both accept finite percentages from 0 through 100;
equality triggers release, and zero triggers even on a valid zero-stall interval.
These flags do not disable or change the independent swap-out safeguard.
`--amount` optionally caps **currently held** bytes, not lifetime allocations;
it accepts integer bytes or binary K/M/G/T suffixes (case insensitive, including
zero). At the cap it keeps monitoring and may clean up swapped old mappings;
released bytes may later be refilled. Verbose status reports held bytes separately from cumulative
successfully mapped bytes (including mappings whose page touching later fails).

The independent, deliberately conservative policy is:

* Sample `/proc/meminfo` (`MemTotal`, `MemAvailable`),
  `/proc/pressure/memory` (`some` and `full` cumulative microsecond totals), and
  `/proc/vmstat` (`pswpout` cumulative pages) every **0.5 seconds** (plus any
  allocation/touch time exceeding that sample budget). PSI decisions
  use **interval deltas divided by monotonic elapsed time**, not rolling averages.
* If used RAM (`MemTotal - MemAvailable`) exceeds the ceiling, immediately close
  enough whole mappings to target **3 percentage points below** the ceiling
  (clamped at zero). This may release all held memory if necessary.
* Independently, PSI `some` stalls of at least **5%** of the interval, `full`
  stalls of at least **1%** (configurable above), or substantial/sustained swap-out trigger release of
  **10% of held bytes, at least 512 MiB**, rounded up to whole mappings. These
  pressure releases occur at most once every **5 seconds** while pressure persists.
  Any new `pswpout` page pauses growth and cleanup retirement and resets both
  recovery timers, but minor activity alone neither releases holdings nor increases
  backoff. Swap-out becomes release pressure at **64 MiB in one measured interval**
  (1/8 chunk), or **at least 1 MiB/s for 5 continuous seconds** (1/512 chunk/s).
  Convert pages with `mmap.PAGESIZE`; sustained time accumulates actual monotonic
  interval lengths only while each interval meets the rate. A slower/zero interval,
  counter reset, invalid elapsed time or changed RAM total resets this window.
  Long sparse intervals are rate-diluted, not counted as sustained traffic merely
  because time passed; the independent burst test still applies. Thresholds scale
  with `CHUNK_SIZE`. One stray page every five seconds is not a RAM eviction order.
* Ordinary pressure-release unmaps count as pending release credit until their effect appears in
  `MemAvailable`, or for **5 seconds**. This avoids repeatedly releasing against
  the same stale RAM reading; fresh additional excess can still trigger release.
  After settling, persistent excess is evaluated again. Host activity can obscure
  release accounting, so this is only a heuristic, not precise attribution.
  Ordinary releases close **oldest-first**, so fresh allocations cannot indefinitely
  protect older swapped mappings from release. They never read old contents.
* Refill requires **5 continuous seconds** of valid telemetry at or below the
  recovery band with **no pressure triggers**: no new swap-out, PSI `some` below
  the configured `--psi-some` threshold and `full` below `--psi-full`. Isolated subtrigger stalls (for example, 116 us
  of `full` in a one-second interval) do not restart the timer. Startup also waits
  for this recovery period. Refill is at most **one 512 MiB chunk per sample**
  (at most 1 GiB/s before backoff), constrained by recovery-band headroom and the amount cap. Pressure,
  insufficient recovery headroom, or a counter reset restarts the quiet period.
  Ceilings below 3% consequently allow no refill.
* Hard pressure (ceiling, PSI, or substantial/sustained swap-out) doubles the minimum interval between allocations, from the local
  **0.5-second** baseline up to **8 seconds**, at most once per **5 seconds** of
  pressure. Chunk size stays **512 MiB** (except headroom/cap-limited partial
  chunks); both normal growth and refresh replacements obey this backoff.
  Every **30 continuous seconds** of valid telemetry below pressure triggers in
  the recovery band halves the interval toward the local baseline. Pressure,
  invalid telemetry, or insufficient recovery headroom resets that recovery timer.
  Telemetry polling and all ceiling/PSI/substantial-swap-out releases remain active during
  allocation waits; pressure is never dismissed as self-generated.
* While mappings exist, read this process's `/proc/self/status` **`VmSwap`**
  every **5 seconds**, or every sample during an active cleanup pass. Positive
  own swap schedules an initial **finite refresh pass**, even during an existing
  pressure episode or with no new pressure edge. Later passes require both the
  generation cooldown and **fresh evidence since the previous pass ended**: a
  new global `pswpout` delta or an observed increase in own `VmSwap` (including
  zero-to-positive). Evidence survives the cooldown and optional read failures;
  own-swap increases compare against the last successful read. Global traffic
  alone is insufficient: own swap must still be positive. Zero own swap skips or ends an
  unnecessary pass. Unknown/unreadable/malformed own swap pauses an active pass
  and retries, **preserving its snapshot**; without an active pass it does not
  block ordinary growth. This optional telemetry never disables safety releases.
* Snapshot existing mappings once per pass. Ordinary oldest-first releases remove
  closed mappings from eligibility. Cleanup takes priority over normal growth,
  including at the amount cap. Retirement requires **5 continuous seconds** of
  valid, trigger-free telemetry at or below the ceiling, **not** recovery-band
  allocation headroom. Thus cleanup can proceed in the hysteresis band. Close at
  most one oldest eligible survivor per sample, **without reading its contents**,
  then offer replacement on a **later sample** with fresh telemetry. Retirement
  never allocates in the same sample or creates optimistic release credit:
  closing swapped pages may free swap rather than resident RAM.
  Replacement still requires the full recovery-band cooldown, measured headroom,
  amount allowance, chunk bound and allocation backoff. Track the retired size as
  **one outstanding byte deficit**; partial replacements reduce it, and **no next
  retirement occurs until it is fully replaced**. No headroom (including the
  hysteresis band) leaves that deficit pending, not permission to drain the rest.
  Replacement cannot exceed the deficit. A lower amount cap shrinks the target;
  an actual safety release abandons the pending replacement target so cleanup
  cannot undo the safety reduction. Remaining snapshot survivors may resume only
  after recovery. Zero own swap cancels cleanup and its deficit; ordinary growth
  can then resume under the same safety gates. This is discard/replacement, not
  preservation or swap-in of old data.
* Renewed pressure or invalid host telemetry pauses cleanup and resets its safe
  retirement wait, but **preserves the original snapshot**. Fresh replacements
  never join that same pass. Once the finite pass completes or reliable zero
  cancels it, a **60-second monotonic generation cooldown** starts. Pressure and
  intermittent recovery do **not** restart this timer. Cooldown expiry alone does
  not rearm unchanged residual swap: Sam does not get a fresh eviction parade
  every minute. New swap evidence can rearm a later pass even when aggregate
  own swap remains unchanged, so new swapping within replacement mappings is not
  permanently suppressed. `VmSwap` includes unrelated Python heap pages and does
  not identify which mappings are swapped. Consequently global activity can cause
  an unnecessary later pass when only heap swap remains; decreases/increases
  between polls, or swap not reflected in either observed counter, cannot be
  attributed precisely. This is an aggregate heuristic, not per-mapping residency
  tracking. Finite snapshots exclude fresh replacements from the current pass.

By default, stderr announces only semantic operation changes with sarcastic,
Sam Altman RAM-themed lines, for example “Occupying spare RAM. Sam can cope.”
Recovery, cooldown, refill, minor swap-pause, pressure-release, refresh, and holding are distinct
operations. Repeated samples stay silent: refill/allocation-wait are grouped,
as are release/pressure-hold and refresh-retire/refill/hold (including refresh
allocation waits). Returning to an operation after another one announces it again.
Logging does not change allocation, telemetry polling, or safety checks.

Use `-v` or `--verbose` for the original detailed **per-sample** logs instead.
Each verbose status line includes the action's reason, `cooldown_remaining` in seconds,
the current `allocation_interval` in seconds, and `refresh` state/pause reason.
It also reports measured `ownswap_bytes` (`None` when unknown), eligible mapping
count, generation number, active state and remaining generation cooldown.
`retirement_cooldown` separately shows the below-ceiling retirement wait;
`replacement_pending` reports the outstanding retirement deficit in bytes.
The action reason identifies the current blocking condition.
`allocation-wait` indicates backoff without pausing safety checks.
`refresh-retire`, `refresh-refill`, and `refresh-hold` distinguish refresh actions
from ordinary pressure release and growth.
Reasons list every active release trigger, including measured interval PSI and
its configured threshold, and the increase in `pswpout` pages for new swap-out.
When recovery is blocked, the cooldown is reset to 5 seconds; it counts down only
while the recovery conditions hold. Chunk size and cooldown reflect the script's
`CHUNK_SIZE` and `QUIET_SECONDS` constants; polling/baseline allocation rate uses
`SAMPLE_SECONDS`, bounded backoff uses `MAX_ALLOCATION_SECONDS`, gradual recovery
uses `RATE_RECOVERY_SECONDS`, generation cooldown uses `REFRESH_REARM_SECONDS`,
and idle own-swap discovery uses `OWN_SWAP_POLL_SECONDS`.
Status is derived from the actual interval and cooldown state, not fixed timing text.
Errors and a short shutdown/cleanup summary are always visible in both modes;
optional own-swap refresh skip/pause diagnostics appear in verbose status.

Counter decreases (resets), changed RAM totals, nonpositive elapsed intervals, or
impossible PSI deltas rebaseline and inhibit refill rather than creating negative
rates. A reset in one counter does not hide valid triggers in other counters.
Missing/unreadable/malformed required host telemetry, negative counters, or allocation
errors cause a nonzero exit with cleanup. Optional own-swap read failures only pause
or defer cleanup, as above. Ctrl-C and SIGTERM close mappings and
exit cleanly; previous signal handlers are restored. SIGKILL/OOM kills cannot run
Python cleanup (the kernel still reclaims the process's mappings).

**Best effort only, not OOM protection.** These are host-wide `/proc` signals,
not cgroup/container memory limits. A container can hit its own limit while the
host looks healthy. Other workloads, accounting lag, overcommit and allocation
or page-touch failures can defeat this policy before the next sample. There is
no guaranteed reserve, immediate reaction, or guarantee against swapping/OOM.
The safe unit tests mock telemetry and mappings; no stress test is run.
