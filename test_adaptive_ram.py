import argparse
from dataclasses import replace
from importlib.machinery import SourceFileLoader
import signal
import unittest
from unittest.mock import MagicMock, mock_open, patch

adaptive = SourceFileLoader("adaptive_ram", "adaptive-ram").load_module()
MIB = 1024 * 1024


def sample(at, used=50 * MIB, some=0, full=0, swapout=0, swapout_pages=None):
    # Historical pressure scenarios use substantial swap bursts on the tiny host.
    pages = swapout * (adaptive.CHUNK_SIZE // 8 // adaptive.mmap.PAGESIZE)
    return adaptive.Sample(at, 100 * MIB, 100 * MIB - used, some, full,
                           pages if swapout_pages is None else swapout_pages)


class ParserTests(unittest.TestCase):
    def test_sizes_and_ceiling(self):
        for text, expected in (("0", 0), ("12", 12), ("2k", 2048),
                               ("3M", 3 * MIB), ("1G", 1024**3), ("1t", 1024**4)):
            self.assertEqual(adaptive.parse_size(text), expected)
        for text in ("-1", "1.5G", "1MB", "", "inf", " 1"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                adaptive.parse_size(text)
        for text in ("nan", "inf", "-inf", "-1", "101", "bad"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                adaptive.parse_percent(text)
        self.assertEqual(adaptive.parse_percent("89.5"), 89.5)
        self.assertEqual(adaptive.parse_percent("0"), 0)
        self.assertEqual(adaptive.parse_percent("100"), 100)

    def test_meminfo(self):
        self.assertEqual(adaptive.parse_meminfo(
            "MemFree: 1 kB\nMemTotal: 100 kB\nMemAvailable: 25 kB\n"), (102400, 25600))
        for text in ("", "MemTotal: 1 kB", "MemTotal: 0 kB\nMemAvailable: 0 kB",
                     "MemTotal: 1 kB\nMemAvailable: 2 kB",
                     "MemTotal: 1 kB\nMemAvailable: -1 kB",
                     "MemTotal: 1 MB\nMemAvailable: 0 kB",
                     "MemTotal: 1 kB\nMemAvailable: bad kB"):
            with self.subTest(text=text), self.assertRaises((ValueError, KeyError)):
                adaptive.parse_meminfo(text)

    def test_psi_uses_totals_not_averages(self):
        self.assertEqual(adaptive.parse_psi(
            "some avg10=99.00 avg60=99.00 total=123\nfull total=45 avg10=0.0\n"), (123, 45))
        for text in ("", "some total=1", "some total=-1\nfull total=0",
                     "some total=1.0\nfull total=0", "some total=0 total=1\nfull total=0",
                     "some total=0\nsome total=1\nfull total=0"):
            with self.subTest(text=text), self.assertRaises((ValueError, KeyError)):
                adaptive.parse_psi(text)

    def test_vmstat(self):
        self.assertEqual(adaptive.parse_vmstat("pswpin 999\npswpout 42\n"), 42)
        for text in ("", "pswpout -1", "pswpout NaN", "pswpout 1\npswpout 2", "pswpout 1 x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                adaptive.parse_vmstat(text)

    def test_own_swap(self):
        self.assertEqual(adaptive.parse_own_swap("Name: python\nVmSwap: 42 kB\n"), 42 * 1024)
        for text in ("", "VmSwap: -1 kB", "VmSwap: 1 MB", "VmSwap: x kB",
                     "VmSwap: 1 kB\nVmSwap: 2 kB", "VmSwap: 1"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                adaptive.parse_own_swap(text)
        with patch.object(adaptive, "open", mock_open(read_data="VmSwap: 3 kB")) as opened:
            self.assertEqual(adaptive.read_own_swap(), 3072)
        opened.assert_called_once_with("/proc/self/status", encoding="ascii")

    def test_read_all_telemetry(self):
        files = {
            "/proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB",
            "/proc/pressure/memory": "some total=123\nfull total=4",
            "/proc/vmstat": "pswpout 7",
        }
        with patch.object(adaptive, "open", create=True,
                          side_effect=lambda path, **kw: mock_open(read_data=files[path])()), \
                patch.object(adaptive.time, "monotonic", return_value=12):
            self.assertEqual(adaptive.read_sample(), adaptive.Sample(12, 102400, 51200, 123, 4, 7))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        # Small synthetic host and historical timing isolate policy from local tuning.
        for name, value in (("CHUNK_SIZE", MIB), ("QUIET_SECONDS", 30)):
            override = patch.object(adaptive, name, value)
            override.start()
            self.addCleanup(override.stop)
        self.controller = adaptive.Controller(90)
        own_swap = patch.object(adaptive, "read_own_swap", return_value=0)
        own_swap.start()
        self.addCleanup(own_swap.stop)
        self.holdings = adaptive.Holdings()
        self.factory = patch.object(adaptive.mmap, "mmap", side_effect=lambda *a, **kw: MagicMock())
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.addCleanup(self.holdings.close)

    def fill(self, count=20):
        for _ in range(count):
            self.holdings.allocate(MIB)

    def test_ceiling_release_to_band_immediately_even_first_sample(self):
        self.fill()
        self.assertEqual(self.controller.step(sample(0, used=95 * MIB), self.holdings), "release")
        self.assertEqual(self.holdings.held, 12 * MIB)
        self.assertEqual(self.holdings.cumulative, 20 * MIB)

    def test_stale_read_does_not_repeat_release_and_credit_reconciles(self):
        self.fill()
        self.controller.step(sample(0, used=95 * MIB), self.holdings)
        self.controller.step(sample(1, used=95 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 12 * MIB)
        self.controller.step(sample(2, used=91 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 12 * MIB)
        self.assertEqual(self.controller.credit, 4 * MIB)
        self.controller.step(sample(3, used=87 * MIB), self.holdings)
        self.assertEqual(self.controller.credit, 0)

    def test_persistent_ceiling_rechecked_after_settling(self):
        self.fill()
        self.controller.step(sample(0, used=95 * MIB), self.holdings)
        self.controller.step(sample(5, used=95 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 4 * MIB)

    def test_overlapping_releases_expire_independently(self):
        self.fill(30)
        self.controller.step(sample(0, used=95 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 22 * MIB)  # Release 8 MiB.
        self.controller.step(sample(4, used=96 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 21 * MIB)  # Release 1 MiB.
        self.controller.step(sample(8, used=97 * MIB), self.holdings)
        # Only the t=4 credit survives: release 9 MiB, not just another 1 MiB.
        self.assertEqual(self.holdings.held, 12 * MIB)
        self.assertEqual(self.controller.credit, 10 * MIB)
        self.controller.step(sample(8.5, used=97 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 12 * MIB)
        self.controller.step(sample(9, used=97 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 11 * MIB)

    def test_overlapping_credits_reconcile_oldest_first(self):
        self.fill(30)
        self.controller.step(sample(0, used=95 * MIB), self.holdings)
        self.controller.step(sample(4, used=96 * MIB), self.holdings)
        self.controller.step(sample(4.5, used=92 * MIB), self.holdings)
        self.assertEqual(self.controller.credit, 5 * MIB)
        self.assertEqual(self.holdings.held, 21 * MIB)
        self.controller.step(sample(5, used=92 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 17 * MIB)
        self.assertEqual(self.controller.credit, 5 * MIB)

    def test_drop_at_old_credit_expiry_preserves_new_credit(self):
        self.fill(30)
        self.controller.step(sample(0, used=95 * MIB), self.holdings)
        self.controller.step(sample(4, used=96 * MIB), self.holdings)
        self.controller.step(sample(5, used=91 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 18 * MIB)
        self.assertEqual(self.controller.credit, 4 * MIB)
        self.controller.step(sample(6, used=91 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 18 * MIB)

    def test_each_pressure_trigger_alone_and_release_rate_limit(self):
        for change in ({"some": 50000}, {"full": 10000}, {"swapout": 1}):
            with self.subTest(change=change):
                holdings = adaptive.Holdings()
                for _ in range(20):
                    holdings.allocate(MIB)
                controller = adaptive.Controller(90)
                controller.step(sample(0), holdings)
                self.assertEqual(controller.step(sample(1, **change), holdings), "release")
                self.assertEqual(holdings.held, 18 * MIB)
                # More pressure on the next second pauses but does not release again.
                change2 = {key: value * 2 for key, value in change.items()}
                controller.step(sample(2, **change2), holdings)
                self.assertEqual(holdings.held, 18 * MIB)
                change3 = {key: value * 6 for key, value in change.items()}
                controller.step(sample(6, **change3), holdings)
                self.assertEqual(holdings.held, 16 * MIB)
                holdings.close()

    def test_interval_deltas_not_lifetime_psi(self):
        self.fill()
        self.controller.step(sample(0, some=5000000, full=2000000, swapout=100), self.holdings)
        self.assertEqual(self.controller.step(
            sample(10, some=5050000, full=2000000, swapout=100), self.holdings), "cooldown")
        self.assertEqual(self.holdings.held, 20 * MIB)

    def test_one_page_every_five_seconds_pauses_without_draining_or_backoff(self):
        self.fill()
        self.controller.amount = self.holdings.held
        for at in range(301):
            action = self.controller.step(sample(at, swapout_pages=at // 5), self.holdings)
            if at and at % 5 == 0:
                self.assertEqual(action, "swap-pause")
                self.assertIsNone(self.controller.quiet_since)
                self.assertIsNone(self.controller.rate_quiet_since)
            self.assertEqual(self.holdings.held, 20 * MIB)
            self.assertEqual(self.controller.allocation_interval, adaptive.SAMPLE_SECONDS)

    def test_sustained_swap_uses_elapsed_time_and_resets_on_gaps_or_resets(self):
        self.fill()
        # One page/s exceeds 1/512 of this mocked 1MiB chunk per second.
        self.controller.step(sample(0), self.holdings)
        for at in (.5, 1.5, 3, 4.5):
            self.assertEqual(self.controller.step(
                sample(at, swapout_pages=int(at * 2)), self.holdings), "swap-pause")
        self.assertEqual(self.controller.step(sample(5, swapout_pages=10), self.holdings),
                         "release")
        self.assertEqual(self.holdings.held, 18 * MIB)
        self.controller.step(sample(6, swapout_pages=10), self.holdings)
        self.assertEqual(self.controller.swap_busy_seconds, 0)
        self.controller.step(sample(7, swapout_pages=0), self.holdings)
        self.assertEqual(self.controller.swap_busy_seconds, 0)

    def test_long_sparse_interval_does_not_count_as_sustained_swap(self):
        self.fill()
        self.controller.step(sample(0), self.holdings)
        self.assertEqual(self.controller.step(sample(600, swapout_pages=1), self.holdings),
                         "swap-pause")
        self.assertEqual(self.holdings.held, 20 * MIB)

    def test_reset_of_one_counter_does_not_hide_other_trigger(self):
        self.fill()
        self.controller.step(sample(0, some=100000), self.holdings)
        self.assertEqual(self.controller.step(sample(1, some=0, swapout=1), self.holdings), "release")
        self.assertEqual(self.holdings.held, 18 * MIB)

    def test_repeated_tiny_stalls_every_29s_do_not_starve_startup_or_refill(self):
        for after_release in (False, True):
            with self.subTest(after_release=after_release):
                controller = adaptive.Controller(90, amount=MIB)
                holdings = adaptive.Holdings()
                self.addCleanup(holdings.close)
                controller.step(sample(0), holdings)
                swapout = int(after_release)
                if after_release:
                    holdings.allocate(MIB)
                    self.assertEqual(controller.step(sample(1, swapout=1), holdings), "release")
                start = 2 if after_release else 1
                some = full = 0
                for at in range(start, start + 90):
                    if (at - start + 1) % 29 == 0:
                        some += 2000
                        full += 116  # Exceeded old 100 us/s recovery limit.
                    action = controller.step(
                        sample(at, some=some, full=full, swapout=swapout), holdings)
                    self.assertEqual(controller.quiet_since, start)
                    if at < start + 30:
                        self.assertEqual(action, "cooldown")
                        self.assertEqual(holdings.held, 0)
                        self.assertEqual(controller.cooldown_remaining, start + 30 - at)
                    elif at == start + 30:
                        self.assertEqual(action, "refill")
                        self.assertEqual(holdings.held, MIB)
                        self.assertEqual(controller.cooldown_remaining, 0)
                self.assertEqual(holdings.held, MIB)
                self.assertEqual(holdings.cumulative, (1 + after_release) * MIB)

    def test_actual_triggers_release_and_restart_full_recovery_wait(self):
        for change, reason in (({"some": 50000}, "PSI some=5% >= 5%"),
                               ({"full": 10000}, "PSI full=1% >= 1%"),
                               ({"swapout": 1}, "new swap-out (release pressure): pswpout +32 pages"),
                               ({"used": 95 * MIB}, "RAM above ceiling")):
            with self.subTest(change=change):
                controller = adaptive.Controller(90)
                holdings = adaptive.Holdings()
                self.addCleanup(holdings.close)
                holdings.allocate(MIB)
                for at in range(29):
                    controller.step(sample(at), holdings)
                self.assertEqual(controller.step(sample(29, **change), holdings), "release")
                self.assertEqual(controller.reason, reason)
                self.assertIsNone(controller.quiet_since)
                self.assertEqual(controller.cooldown_remaining, 30)
                self.assertEqual(holdings.held, 0)
                counters = {key: value for key, value in change.items() if key != "used"}
                for at in range(30, 60):
                    self.assertEqual(controller.step(sample(at, **counters), holdings), "cooldown")
                    self.assertEqual(holdings.held, 0)
                self.assertEqual(controller.step(sample(60, **counters), holdings), "refill")
                self.assertEqual(holdings.held, MIB)

    def test_quiet_recovery_and_slow_refill(self):
        for at in range(31):
            self.controller.step(sample(at), self.holdings)
        self.assertEqual(self.holdings.held, 0)
        self.controller.step(sample(31), self.holdings)
        self.assertEqual(self.holdings.held, MIB)
        self.controller.step(sample(32), self.holdings)
        self.assertEqual(self.holdings.held, 2 * MIB)

    def test_cap_holds_and_released_bytes_can_be_refilled(self):
        self.controller.amount = 123
        for at in range(33):
            action = self.controller.step(sample(at), self.holdings)
        self.assertEqual(action, "cap/headroom-hold")
        self.assertEqual(self.holdings.held, 123)
        self.controller.step(sample(33, swapout=1), self.holdings)
        self.assertEqual(self.holdings.held, 0)
        for at in range(34, 65):
            self.controller.step(sample(at, swapout=1), self.holdings)
        self.assertEqual(self.holdings.held, 123)
        self.assertEqual(self.holdings.cumulative, 246)

    def test_hysteresis_and_low_ceiling(self):
        for at in range(40):
            self.controller.step(sample(at, used=88 * MIB), self.holdings)
        self.assertEqual(self.holdings.held, 0)
        self.assertIsNone(self.controller.quiet_since)
        self.controller.ceiling = 1
        for at in range(40, 80):
            self.controller.step(sample(at, used=0), self.holdings)
        self.assertEqual(self.holdings.held, 0)

    def test_headroom_and_zero_cap(self):
        for at in range(32):
            self.controller.step(sample(at, used=87 * MIB - 123), self.holdings)
        self.assertEqual(self.holdings.held, 123)
        self.controller.amount = 0
        self.controller.step(sample(32), self.holdings)
        self.assertEqual(self.holdings.held, 123)

    def test_counter_reset_rebaselines_and_restarts_cooldown(self):
        for changed in ({"some": 0}, {"full": 0}, {"swapout": 0},
                        {"at": 0}, {"total": 200 * MIB}, {"some": 3000000}):
            with self.subTest(changed=changed):
                controller = adaptive.Controller(90)
                controller.step(sample(0, some=1000, full=1000, swapout=10), self.holdings)
                controller.step(sample(1, some=1000, full=1000, swapout=10), self.holdings)
                current = replace(sample(2, some=1000, full=1000, swapout=10), **changed)
                controller.step(current, self.holdings)
                self.assertIsNone(controller.quiet_since)
                self.assertEqual(self.holdings.held, 0)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        ControllerTests.setUp(self)
        own_swap = patch.object(adaptive, "read_own_swap", return_value=MIB)
        self.own_swap_reader = own_swap.start()
        self.addCleanup(own_swap.stop)

    fill = ControllerTests.fill

    def recover(self, at=2, **changes):
        self.assertEqual(self.controller.step(sample(at, **changes), self.holdings), "cooldown")
        return self.controller.step(sample(at + 30, **changes), self.holdings)

    def pressure(self):
        self.fill(4)
        self.controller.amount = 4 * MIB
        self.controller.step(sample(0), self.holdings)
        self.assertEqual(self.controller.step(sample(1, swapout=1), self.holdings), "release")
        return [mapping for mapping, _ in self.holdings.mappings]

    def test_finite_once_only_pass_at_cap_and_no_old_reads(self):
        old = self.pressure()
        self.assertEqual(len(old), 3)
        self.assertEqual(self.recover(swapout=1), "refresh-retire")
        self.assertEqual(self.controller.credit, 0)
        self.assertEqual(self.holdings.held, 2 * MIB)
        actions = [self.controller.step(sample(at, swapout=1), self.holdings)
                   for at in range(33, 50)]
        self.assertEqual(actions[:5], ["refresh-refill", "refresh-retire", "refresh-refill",
                                      "refresh-retire", "refresh-refill"])
        self.assertEqual(actions.count("refresh-retire"), 2)
        self.assertEqual(self.holdings.held, 4 * MIB)
        self.assertEqual(self.holdings.cumulative, 8 * MIB)
        self.assertFalse(self.holdings.refresh_eligible)
        for mapping in old:
            mapping.close.assert_called_once_with()
            mapping.__getitem__.assert_not_called()
        # A new pressure episode before stable rearm cannot start a generation.
        self.controller.step(sample(50, swapout=2), self.holdings)
        self.assertFalse(self.holdings.refresh_eligible)

    def test_startup_with_zero_own_swap_has_no_refresh(self):
        self.own_swap_reader.return_value = 0
        self.controller.amount = 3 * MIB
        actions = [self.controller.step(sample(at), self.holdings) for at in range(60)]
        self.assertFalse(any(action.startswith("refresh-") for action in actions))
        self.assertEqual(self.holdings.cumulative, 3 * MIB)

    def test_refresh_retires_even_when_already_at_amount_cap(self):
        self.pressure()
        self.controller.amount = self.holdings.held
        self.assertEqual(self.recover(swapout=1), "refresh-retire")
        self.assertEqual(self.holdings.held, 2 * MIB)
        self.assertEqual(self.controller.step(sample(33, swapout=1), self.holdings), "refresh-refill")
        self.assertEqual(self.holdings.held, self.controller.amount)
        self.assertEqual(self.controller.step(sample(34, swapout=1), self.holdings), "refresh-retire")

    def test_main_reads_new_telemetry_between_retirement_and_replacement(self):
        old = self.pressure()
        events = []
        old[0].close.side_effect = lambda: events.append("retire")
        readings = iter([sample(2, swapout=1), sample(32, swapout=1),
                         sample(33, used=87 * MIB - 123, swapout=1)])

        def read():
            events.append("sample")
            try:
                return next(readings)
            except StopIteration:
                raise KeyboardInterrupt

        def allocate(size):
            events.append(("allocate", size))

        with patch.object(adaptive, "Holdings", return_value=self.holdings), \
                patch.object(adaptive, "Controller", return_value=self.controller), \
                patch.object(adaptive, "read_sample", side_effect=read), \
                patch.object(self.holdings, "allocate", side_effect=allocate), \
                patch.object(adaptive.time, "sleep"), \
                patch.object(adaptive.signal, "signal"), patch("builtins.print") as output:
            self.assertEqual(adaptive.main(["--verbose"]), 0)
        self.assertEqual(events[:5], ["sample", "sample", "retire", "sample", ("allocate", 123)])
        lines = [call.args[0] for call in output.call_args_list]
        self.assertTrue(any(line.startswith("refresh-retire:") for line in lines))
        self.assertTrue(any(line.startswith("refresh-refill:") for line in lines))

    def test_pressure_interrupt_preserves_snapshot_and_excludes_fresh_survivors(self):
        self.pressure()
        self.recover(swapout=1)
        self.controller.step(sample(33, swapout=1), self.holdings)
        fresh = self.holdings.mappings[-1][0]
        remaining = self.holdings.refresh_eligible.copy()
        # Avoid ordinary release here to isolate eligibility from pressure release.
        self.controller.last_release = 34
        self.assertEqual(self.controller.step(sample(34, swapout=2), self.holdings), "pressure-hold")
        self.assertEqual(self.holdings.refresh_eligible, remaining)
        self.assertNotIn(id(fresh), self.holdings.refresh_eligible)
        self.assertIsNone(self.controller.quiet_since)
        self.assertEqual(self.controller.step(sample(35, swapout=2), self.holdings), "cooldown")
        self.assertEqual(self.controller.step(sample(64, swapout=2), self.holdings), "cooldown")
        self.assertEqual(self.controller.step(sample(65, swapout=2), self.holdings), "refresh-retire")
        for at in range(66, 75):
            self.controller.step(sample(at, swapout=2), self.holdings)
        self.assertFalse(self.holdings.refresh_eligible)
        self.assertFalse(self.controller.refresh_active)
        fresh.close.assert_not_called()

    def test_zero_or_unreadable_own_swap_skips_refresh_without_blocking_growth(self):
        for result in (0, OSError("denied"), ValueError("bad VmSwap")):
            with self.subTest(result=result):
                self.controller = adaptive.Controller(90)
                self.holdings.close()
                with patch.object(adaptive, "read_own_swap", side_effect=
                                  result if isinstance(result, Exception) else None,
                                  return_value=result):
                    self.pressure()
                    self.assertFalse(self.holdings.refresh_eligible)
                    self.assertEqual(self.recover(swapout=1), "refill")
                if isinstance(result, Exception):
                    self.assertIn("VmSwap unavailable", self.controller.refresh_status)
                else:
                    self.assertIn("skipped:", self.controller.refresh_status)

    def test_zero_own_swap_cancels_pending_pass_and_allows_growth(self):
        self.pressure()
        with patch.object(adaptive, "read_own_swap", return_value=0):
            self.assertEqual(self.recover(swapout=1), "refill")
        self.assertFalse(self.controller.refresh_active)
        self.assertFalse(self.holdings.refresh_eligible)
        self.assertIn("zero own swap", self.controller.refresh_status)

    def test_unreadable_own_swap_pauses_active_pass_and_recovers(self):
        self.pressure()
        eligible = self.holdings.refresh_eligible.copy()
        with patch.object(adaptive, "read_own_swap", side_effect=OSError("denied")):
            self.assertEqual(self.recover(swapout=1), "refresh-hold")
        self.assertTrue(self.controller.refresh_active)
        self.assertEqual(self.holdings.refresh_eligible, eligible)
        self.assertIn("VmSwap unavailable", self.controller.refresh_status)
        self.assertEqual(self.controller.step(sample(33, swapout=1), self.holdings),
                         "refresh-retire")
        self.assertEqual(self.controller.refresh_generation, 1)

    def test_empty_snapshot_after_safety_release_does_not_block_growth(self):
        self.fill(1)
        self.controller.step(sample(0), self.holdings)
        self.controller.step(sample(1, swapout=1), self.holdings)
        self.assertFalse(self.holdings.refresh_eligible)
        self.assertEqual(self.recover(swapout=1), "refill")
        self.assertFalse(self.controller.refresh_active)

    def test_generation_cooldown_is_bounded_despite_intermittent_pressure(self):
        self.pressure()
        with patch.object(adaptive, "read_own_swap", return_value=0):
            self.recover(swapout=1)
        ready = self.controller.refresh_ready_at
        self.controller.step(sample(33, swapout=1), self.holdings)
        self.controller.step(sample(34, swapout=2), self.holdings)
        self.assertFalse(self.controller.refresh_active)
        self.assertFalse(self.controller.refresh_armed)
        swapout = 2
        for at in range(35, int(ready)):
            if at % 10 == 0:
                swapout += 1
                self.controller.last_release = at
            self.controller.step(sample(at, used=88 * MIB, swapout=swapout), self.holdings)
            self.assertFalse(self.controller.refresh_armed)
        self.controller.step(sample(ready, used=88 * MIB, swapout=swapout), self.holdings)
        self.assertTrue(self.controller.refresh_armed)
        self.assertTrue(self.controller.refresh_active)
        self.assertEqual(self.controller.refresh_generation, 2)

    def test_pressure_release_removes_only_original_ids_during_pass(self):
        self.pressure()
        self.recover(swapout=1)
        self.controller.step(sample(33, swapout=1), self.holdings)
        fresh = self.holdings.mappings[-1][0]
        remaining = self.holdings.refresh_eligible.copy()
        oldest = self.holdings.mappings[0][0]
        self.assertEqual(self.controller.step(sample(34, swapout=2), self.holdings), "release")
        fresh.close.assert_not_called()
        oldest.close.assert_called_once_with()
        self.assertEqual(self.holdings.refresh_eligible, remaining - {id(oldest)})

    def test_no_headroom_keeps_one_retirement_deficit_without_credit(self):
        old = self.pressure()
        self.assertEqual(self.recover(used=87 * MIB, swapout=1), "refresh-retire")
        for at in range(33, 38):
            action = self.controller.step(sample(at, used=87 * MIB, swapout=1), self.holdings)
            self.assertEqual(action, "refresh-hold")
            self.assertEqual(self.controller.credit, 0)
        self.assertEqual(self.holdings.held, 2 * MIB)
        self.assertEqual(self.holdings.cumulative, 4 * MIB)
        old[0].close.assert_called_once_with()
        for mapping in old[1:]:
            mapping.close.assert_not_called()
        self.assertEqual(self.controller.refresh_pending, MIB)

    def test_refill_uses_next_samples_headroom_not_retired_size(self):
        old = self.pressure()
        self.recover(swapout=1)
        old[0].close.assert_called_once_with()
        cumulative = self.holdings.cumulative
        self.assertEqual(self.controller.step(
            sample(33, used=87 * MIB - 123, swapout=1), self.holdings), "refresh-refill")
        self.assertEqual(self.holdings.cumulative, cumulative + 123)
        self.assertEqual(self.controller.refresh_pending, MIB - 123)
        self.controller.step(sample(34, used=87 * MIB, swapout=1), self.holdings)
        self.assertEqual(self.controller.step(sample(35, used=88 * MIB, swapout=1),
                                              self.holdings), "recovery-hold")
        self.assertEqual(self.holdings.cumulative, cumulative + 123)

    def test_retirement_close_failure_keeps_accounting_for_cleanup(self):
        old = self.pressure()
        old[0].close.side_effect = OSError("close")
        with self.assertRaises(OSError):
            self.recover(swapout=1)
        self.assertEqual(self.holdings.held, 3 * MIB)
        self.assertEqual(len(self.holdings.refresh_eligible), 3)
        old[0].close.side_effect = None
        self.holdings.close()
        self.assertEqual(self.holdings.held, 0)
        self.assertFalse(self.holdings.refresh_eligible)

    def test_partial_replacement_blocks_next_retirement_until_fully_replaced(self):
        old = self.pressure()
        self.recover(swapout=1)
        eligible = self.holdings.refresh_eligible.copy()
        for at in range(33, 36):
            self.assertEqual(self.controller.step(
                sample(at, used=87 * MIB - 123, swapout=1), self.holdings), "refresh-refill")
            self.assertEqual(self.controller.refresh_pending, MIB - 123 * (at - 32))
            self.assertEqual(self.holdings.refresh_eligible, eligible)
            for mapping in old[1:]:
                mapping.close.assert_not_called()
        self.assertEqual(self.controller.step(sample(36, swapout=1), self.holdings),
                         "refresh-refill")
        self.assertEqual(self.controller.refresh_pending, 0)
        self.assertEqual(self.controller.step(sample(37, swapout=1), self.holdings),
                         "refresh-retire")

    def test_safety_release_cancels_pending_target_and_never_allocates(self):
        for pressure in ({"some": 50000}, {"full": 10000},
                         {"swapout": 2}, {"used": 95 * MIB}):
            with self.subTest(pressure=pressure):
                self.controller = adaptive.Controller(90)
                self.holdings.close()
                self.pressure()
                self.recover(swapout=1)
                cumulative = self.holdings.cumulative
                changes = {"swapout": 1, **pressure}
                self.assertEqual(self.controller.step(sample(33, **changes), self.holdings),
                                 "release")
                self.assertEqual(self.controller.refresh_pending, 0)
                self.assertEqual(self.holdings.cumulative, cumulative)
                counters = {key: val for key, val in changes.items() if key != "used"}
                for at in range(34, 66):
                    self.controller.step(sample(at, **counters), self.holdings)
                self.assertGreater(self.holdings.cumulative, cumulative)

    def test_cap_reduction_reconciles_pending_without_permanent_block(self):
        self.pressure()
        self.recover(swapout=1)
        self.controller.amount = self.holdings.held + 123
        self.assertEqual(self.controller.step(sample(33, swapout=1), self.holdings),
                         "refresh-refill")
        self.assertEqual(self.controller.refresh_pending, 0)
        for at in range(34, 45):
            self.controller.step(sample(at, swapout=1), self.holdings)
        self.assertFalse(self.controller.refresh_active)
        self.assertEqual(self.holdings.held, self.controller.amount)

    def test_unchanged_own_swap_recovery_clears_unavailable_status_without_rearming(self):
        self.fill(1)
        self.controller.amount = MIB
        for at in range(35):
            self.controller.step(sample(at), self.holdings)
        ready = self.controller.refresh_ready_at
        generation = self.controller.refresh_generation
        cumulative = self.holdings.cumulative
        self.assertFalse(self.controller.refresh_active)
        self.assertFalse(self.controller.refresh_evidence)
        with patch.object(adaptive, "read_own_swap", side_effect=OSError("denied")):
            self.controller.step(sample(ready), self.holdings)
        self.assertIn("VmSwap unavailable", self.controller.refresh_status)
        self.controller.step(sample(ready + 5), self.holdings)
        self.assertEqual(self.controller.own_swap, MIB)
        self.assertEqual(self.controller.refresh_status, "idle")
        self.assertFalse(self.controller.refresh_active)
        self.assertFalse(self.controller.refresh_armed)
        self.assertFalse(self.controller.refresh_evidence)
        self.assertFalse(self.holdings.refresh_eligible)
        self.assertEqual(self.controller.refresh_generation, generation)
        self.assertEqual(self.holdings.cumulative, cumulative)

    def test_own_swap_increase_rearms_even_after_unavailable_read(self):
        self.fill(1)
        self.controller.amount = MIB
        for at in range(35):
            self.controller.step(sample(at), self.holdings)
        ready = self.controller.refresh_ready_at
        with patch.object(adaptive, "read_own_swap", side_effect=OSError("denied")):
            self.controller.step(sample(ready), self.holdings)
        self.own_swap_reader.return_value = MIB + 4096
        self.controller.step(sample(ready + 5), self.holdings)
        self.assertEqual(self.controller.refresh_generation, 2)

    def test_replacement_touch_failure_registered_for_cleanup(self):
        self.pressure()
        self.recover(swapout=1)
        broken = MagicMock()
        broken.__setitem__.side_effect = MemoryError("touch")
        with patch.object(adaptive.mmap, "mmap", return_value=broken), self.assertRaises(MemoryError):
            self.controller.step(sample(33, swapout=1), self.holdings)
        self.assertEqual(self.holdings.held, 3 * MIB)
        self.assertEqual(self.holdings.cumulative, 5 * MIB)
        self.holdings.close()
        broken.close.assert_called_once_with()
        self.assertEqual(self.holdings.held, 0)

    def test_delayed_own_swap_is_discovered_inside_pressure_episode(self):
        self.own_swap_reader.return_value = 0
        self.fill(20)
        self.controller.step(sample(0), self.holdings)
        self.controller.step(sample(1, swapout=1), self.holdings)
        self.assertFalse(self.controller.refresh_active)
        self.own_swap_reader.return_value = MIB
        for at in range(2, 6):
            self.controller.step(sample(at, swapout=at), self.holdings)
        self.assertTrue(self.controller.in_pressure)
        self.assertTrue(self.controller.refresh_active)
        self.assertEqual(self.controller.refresh_generation, 1)
        self.assertEqual(self.own_swap_reader.call_count, 2)

    def test_persistent_own_swap_schedules_without_pressure_edge(self):
        self.fill(3)
        self.controller.amount = 3 * MIB
        actions = [self.controller.step(sample(at), self.holdings) for at in range(40)]
        self.assertIn("refresh-retire", actions)
        self.assertFalse(self.controller.in_pressure)
        self.assertEqual(self.controller.refresh_generation, 1)
        self.assertFalse(self.controller.refresh_active)
        self.assertEqual(self.holdings.cumulative, 6 * MIB)

    def test_hysteresis_band_keeps_one_unreplaced_retirement(self):
        old = self.pressure()
        self.assertEqual(self.controller.step(
            sample(2, used=88 * MIB, swapout=1), self.holdings), "recovery-hold")
        for at in range(32, 38):
            action = self.controller.step(sample(at, used=88 * MIB, swapout=1), self.holdings)
            self.assertEqual(action, "refresh-retire" if at == 32 else "recovery-hold")
            self.assertEqual(self.controller.credit, 0)
        self.assertEqual(self.holdings.held, 2 * MIB)
        self.assertEqual(self.holdings.cumulative, 4 * MIB)
        self.assertTrue(self.controller.refresh_active)
        old[0].close.assert_called_once_with()
        for mapping in old[1:]:
            mapping.close.assert_not_called()
            mapping.__getitem__.assert_not_called()

    def test_unchanged_residual_swap_needs_fresh_evidence_after_cooldown(self):
        self.fill(2)
        self.controller.amount = 2 * MIB
        old = [mapping for mapping, _ in self.holdings.mappings]
        for at in range(35):
            self.controller.step(sample(at), self.holdings)
        self.assertFalse(self.controller.refresh_active)
        self.assertEqual(self.controller.refresh_generation, 1)
        ready = self.controller.refresh_ready_at
        cumulative = self.holdings.cumulative
        for at in range(35, int(ready)):
            self.controller.step(sample(at), self.holdings)
            self.assertEqual(self.controller.refresh_generation, 1)
            self.assertEqual(self.holdings.cumulative, cumulative)
        for at in range(int(ready), int(ready) + 120):
            self.controller.step(sample(at), self.holdings)
        self.assertEqual(self.controller.refresh_generation, 1)
        self.assertEqual(self.holdings.cumulative, cumulative)
        self.controller.step(sample(ready + 120, swapout_pages=1), self.holdings)
        self.assertEqual(self.controller.refresh_generation, 2)
        for mapping in old:
            mapping.close.assert_called_once_with()

    def test_retirement_independent_of_backoff_but_replacement_obeys_it_and_cap(self):
        self.pressure()
        self.controller.last_allocation = 31
        self.controller.allocation_interval = adaptive.MAX_ALLOCATION_SECONDS
        self.assertEqual(self.recover(swapout=1), "refresh-retire")
        cumulative = self.holdings.cumulative
        self.assertEqual(self.controller.step(sample(33, swapout=1), self.holdings),
                         "allocation-wait")
        self.controller.amount = self.holdings.held + 123
        self.assertEqual(self.controller.step(sample(39, swapout=1), self.holdings),
                         "refresh-refill")
        self.assertEqual(self.holdings.cumulative, cumulative + 123)

    def test_ordinary_release_is_oldest_first_without_reading(self):
        self.fill(4)
        old = [mapping for mapping, _ in self.holdings.mappings]
        self.holdings.release(MIB)
        self.holdings.allocate(MIB)
        self.holdings.release(MIB)
        for mapping in old[:2]:
            mapping.close.assert_called_once_with()
            mapping.__getitem__.assert_not_called()
        for mapping in old[2:]:
            mapping.close.assert_not_called()


class ThresholdAndTuningTests(unittest.TestCase):
    def test_default_swap_burst_boundary_and_sustained_rate_window(self):
        burst_pages = 64 * MIB // adaptive.mmap.PAGESIZE
        for pages in (burst_pages - 1, burst_pages):
            with self.subTest(pages=pages), \
                    patch.object(adaptive, "read_own_swap", return_value=0):
                controller = adaptive.Controller(90)
                holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
                holdings.release.return_value = 512 * MIB
                controller.step(sample(0), holdings)
                self.assertEqual(controller.step(sample(1, swapout_pages=pages), holdings),
                                 "release" if pages == burst_pages else "swap-pause")
        rate_pages = MIB // adaptive.mmap.PAGESIZE
        for rate in (rate_pages - 1, rate_pages):
            with self.subTest(rate=rate), \
                    patch.object(adaptive, "read_own_swap", return_value=0):
                controller = adaptive.Controller(90)
                holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
                holdings.release.return_value = 512 * MIB
                controller.step(sample(0), holdings)
                for at in range(1, 6):
                    action = controller.step(sample(at, swapout_pages=rate * at), holdings)
                    self.assertEqual(action, "release" if rate == rate_pages and at == 5
                                     else "swap-pause")

    def test_five_second_polling_one_page_does_not_drain_default_holdings(self):
        controller = adaptive.Controller(90)
        holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
        with patch.object(adaptive, "read_own_swap", return_value=0):
            controller.step(sample(0), holdings)
            for at in range(5, 301, 5):
                self.assertEqual(controller.step(sample(at, swapout_pages=at // 5), holdings),
                                 "swap-pause")
        holdings.release.assert_not_called()
        holdings.allocate.assert_not_called()
        self.assertEqual(controller.allocation_interval, adaptive.SAMPLE_SECONDS)

    def test_backoff_bounds_allocation_rate_and_recovers_only_after_quiet(self):
        controller = adaptive.Controller(90)
        holdings = MagicMock(held=0, refresh_eligible=set())
        controller.step(sample(0), holdings)
        controller.step(sample(1, swapout=1), holdings)
        controller.step(sample(2, swapout=1), holdings)
        self.assertEqual(controller.step(sample(7, swapout=1), holdings), "refill")
        self.assertEqual(controller.allocation_interval, 2 * adaptive.SAMPLE_SECONDS)
        self.assertEqual(controller.step(sample(7.5, swapout=1), holdings), "allocation-wait")
        self.assertEqual(controller.step(sample(8, swapout=1), holdings), "refill")
        self.assertEqual(holdings.allocate.call_count, 2)
        controller.step(sample(2 + adaptive.RATE_RECOVERY_SECONDS, swapout=1), holdings)
        self.assertEqual(controller.allocation_interval, adaptive.SAMPLE_SECONDS)
        for at in range(40, 100, adaptive.SETTLE_SECONDS):
            controller.step(sample(at, swapout=at), holdings)
        self.assertEqual(controller.allocation_interval, adaptive.MAX_ALLOCATION_SECONDS)

    def test_all_safety_releases_remain_active_during_allocation_backoff(self):
        for change in ({"some": 50000}, {"full": 10000}, {"swapout": 1},
                       {"used": 95 * MIB}):
            with self.subTest(change=change), patch.object(adaptive, "read_own_swap", return_value=0):
                controller = adaptive.Controller(90)
                holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
                holdings.release.return_value = 512 * MIB
                controller.step(sample(0), holdings)
                controller.last_allocation = 0.9
                controller.allocation_interval = adaptive.MAX_ALLOCATION_SECONDS
                self.assertEqual(controller.step(sample(1, **change), holdings), "release")
                holdings.release.assert_called_once()
                holdings.allocate.assert_not_called()

    def test_default_and_custom_interval_boundaries(self):
        # Two-second intervals ensure thresholds use elapsed time, not raw totals.
        for kwargs, thresholds in (({}, {"some": 5, "full": 1}),
                                   ({"psi_some": 12.5, "psi_full": 2.5},
                                    {"some": 12.5, "full": 2.5})):
            for counter, threshold in thresholds.items():
                boundary = int(threshold * 20000)
                for delta in (boundary - 1, boundary, boundary + 1):
                    with self.subTest(kwargs=kwargs, counter=counter, delta=delta):
                        controller = adaptive.Controller(90, **kwargs)
                        holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
                        holdings.release.return_value = 512 * MIB
                        controller.step(sample(0), holdings)
                        action = controller.step(sample(2, **{counter: delta}), holdings)
                        self.assertEqual(action, "release" if delta >= boundary else "cooldown")
                        if delta >= boundary:
                            self.assertIn(f"PSI {counter}=", controller.reason)
                            self.assertIn(f">= {threshold:g}%", controller.reason)
                        else:
                            holdings.release.assert_not_called()

    def test_fractional_interval_boundaries(self):
        for counter in ("some", "full"):
            for elapsed in (1, 2):
                boundary = 700 * elapsed  # Exactly 0.07% of the interval.
                for delta in (boundary - 1, boundary):
                    with self.subTest(counter=counter, elapsed=elapsed, delta=delta):
                        controller = adaptive.Controller(90, psi_some=100, psi_full=100)
                        setattr(controller, f"psi_{counter}", 0.07)
                        holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
                        holdings.release.return_value = 512 * MIB
                        controller.step(sample(0), holdings)
                        action = controller.step(sample(elapsed, **{counter: delta}), holdings)
                        if delta == boundary:
                            self.assertEqual(action, "release")
                            self.assertEqual(controller.reason, f"PSI {counter}=0.07% >= 0.07%")
                            holdings.release.assert_called_once_with(512 * MIB)
                        else:
                            self.assertEqual(action, "cooldown")
                            holdings.release.assert_not_called()

    def test_extreme_thresholds_and_invalid_counter_not_a_zero_trigger(self):
        for threshold in (0, 100):
            for counter in ("some", "full"):
                with self.subTest(threshold=threshold, counter=counter):
                    controller = adaptive.Controller(90, psi_some=100, psi_full=100)
                    setattr(controller, f"psi_{counter}", threshold)
                    holdings = MagicMock(held=MIB, refresh_eligible=set())
                    holdings.release.return_value = MIB
                    controller.step(sample(0), holdings)
                    self.assertEqual(controller.step(
                        sample(1, **{counter: threshold * 10000}), holdings), "release")
        controller = adaptive.Controller(90, psi_some=0, psi_full=100)
        holdings = MagicMock(held=MIB, refresh_eligible=set())
        controller.step(sample(0, some=1), holdings)
        self.assertEqual(controller.step(sample(1), holdings), "recovery-hold")
        holdings.release.assert_not_called()

    def test_swapout_independent_of_psi_thresholds_and_reset(self):
        controller = adaptive.Controller(90, psi_some=100, psi_full=100)
        holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
        holdings.release.return_value = 512 * MIB
        controller.step(sample(0, some=1, swapout=10), holdings)
        self.assertEqual(controller.step(sample(1, swapout=11), holdings), "release")
        self.assertEqual(controller.reason,
                         "new swap-out (release pressure): pswpout +16384 pages")
        holdings.release.assert_called_once_with(512 * MIB)
        self.assertEqual(controller.step(sample(2, swapout=12), holdings), "pressure-hold")
        self.assertEqual(holdings.release.call_count, 1)

    def test_all_simultaneous_triggers_are_logged_with_measurements(self):
        controller = adaptive.Controller(90)
        holdings = MagicMock(held=1024 * MIB, refresh_eligible=set())
        holdings.release.return_value = 512 * MIB
        controller.step(sample(0, swapout=100), holdings)
        self.assertEqual(controller.step(sample(1, used=95 * MIB, some=60000,
                                                full=20000, swapout=103), holdings), "release")
        self.assertEqual(controller.reason,
                         "RAM above ceiling, PSI some=6% >= 5%, PSI full=2% >= 1%, "
                          "new swap-out (release pressure): pswpout +49152 pages")

    def test_actual_chunk_size_and_cooldown_status(self):
        self.assertEqual(adaptive.CHUNK_SIZE, 512 * MIB)
        self.assertEqual(adaptive.QUIET_SECONDS, 5)
        controller = adaptive.Controller(90)
        holdings = MagicMock(held=0, refresh_eligible=set())
        total = 16 * 1024 * MIB
        reading = adaptive.Sample(0, total, total, 0, 0, 0)
        controller.step(reading, holdings)
        self.assertEqual(controller.cooldown_remaining, adaptive.QUIET_SECONDS)
        controller.step(replace(reading, at=1), holdings)
        self.assertEqual(controller.reason,
                         f"waiting for {adaptive.QUIET_SECONDS:g}s below pressure triggers in recovery band")
        self.assertEqual(controller.step(
            replace(reading, at=1 + adaptive.QUIET_SECONDS), holdings), "refill")
        holdings.allocate.assert_called_once_with(512 * MIB)

    def test_cli_defaults_and_custom_thresholds_are_forwarded(self):
        for argv, expected in (([], (5, 1)),
                               (["--psi-some", "0", "--psi-full", "100"], (0, 100)),
                               (["--psi-some", "2.5", "--psi-full", "0.25"], (2.5, .25))):
            with self.subTest(argv=argv), \
                    patch.object(adaptive, "Controller") as controller, \
                    patch.object(adaptive, "read_sample", side_effect=KeyboardInterrupt), \
                    patch.object(adaptive.signal, "signal"), patch("builtins.print"):
                self.assertEqual(adaptive.main(argv), 0)
                controller.assert_called_once_with(90, None, *expected)

    def test_cli_rejects_invalid_thresholds_before_allocating(self):
        for flag in ("--psi-some", "--psi-full"):
            for value in ("nan", "inf", "-inf", "-0.1", "100.1", "bad"):
                with self.subTest(flag=flag, value=value), \
                        patch.object(adaptive, "Holdings") as holdings, \
                        patch.object(adaptive.sys, "stderr"), \
                        self.assertRaises(SystemExit) as error:
                    adaptive.main([f"{flag}={value}"])
                self.assertEqual(error.exception.code, 2)
                holdings.assert_not_called()


class StatusLoggerTests(unittest.TestCase):
    def setUp(self):
        self.controller = adaptive.Controller(90)
        self.holdings = MagicMock(held=123, cumulative=456)

    def test_default_logs_only_semantic_transitions_and_reentry(self):
        actions = ["recovery-hold", "recovery-hold", "cooldown", "cooldown",
                   "refill", "allocation-wait", "refill", "allocation-wait",
                   "release", "pressure-hold", "release", "pressure-hold",
                   "cooldown", "refill", "cap/headroom-hold", "cap/headroom-hold"]
        logger = adaptive.StatusLogger()
        with patch("builtins.print") as output:
            for index, action in enumerate(actions):
                # Changing counters/reasons must not produce default log spam.
                self.controller.reason = f"sample {index}"
                self.holdings.held = index
                logger.log(action, self.controller, self.holdings)
        operations = ["recovery", "cooldown", "refill", "pressure-release",
                      "cooldown", "refill", "holding"]
        self.assertEqual([call.args[0] for call in output.call_args_list],
                         [logger.MESSAGES[operation] for operation in operations])
        for call in output.call_args_list:
            self.assertIs(call.kwargs["file"], adaptive.sys.stderr)
            self.assertTrue(call.kwargs["flush"])

    def test_refresh_actions_and_backoff_are_one_operation(self):
        logger = adaptive.StatusLogger()
        self.assertIn("Cleaning up swapped", logger.MESSAGES["refresh"])
        self.assertIn("Sam", logger.MESSAGES["refresh"])
        with patch("builtins.print") as output:
            for action in ("refresh-retire", "allocation-wait", "refresh-refill",
                           "refresh-retire", "refresh-hold", "refresh-retire",
                           "allocation-wait", "refresh-refill"):
                self.controller.refresh_refill = action == "allocation-wait"
                logger.log(action, self.controller, self.holdings)
            logger.log("cap/headroom-hold", self.controller, self.holdings)
            logger.log("refresh-retire", self.controller, self.holdings)
        self.assertEqual([call.args[0] for call in output.call_args_list],
                         [logger.MESSAGES[operation] for operation in
                          ("refresh", "holding", "refresh")])

    def test_verbose_preserves_details_for_every_sample(self):
        logger = adaptive.StatusLogger(verbose=True)
        actions = ["refill", "allocation-wait", "refill", "release", "pressure-hold",
                   "refresh-retire", "refresh-refill", "refresh-hold"]
        with patch("builtins.print") as output:
            for action in actions:
                logger.log(action, self.controller, self.holdings)
        self.assertEqual(len(output.call_args_list), len(actions))
        for action, call in zip(actions, output.call_args_list):
            self.assertEqual(call.args[0],
                             f"{action}: held=123 cumulative=456 bytes "
                             "reason=awaiting telemetry; cooldown_remaining=5.0s; "
                              "allocation_interval=0.5s; refresh=idle; ownswap_bytes=None; "
                              "eligible=0; generation=0; generation_cooldown=0.0s; "
                               "refresh_active=False; replacement_pending=0; retirement_cooldown=5.0s")

    def test_minor_swap_pause_is_not_logged_as_pressure_release(self):
        logger = adaptive.StatusLogger()
        with patch("builtins.print") as output:
            for action in ("swap-pause", "swap-pause", "pressure-hold"):
                logger.log(action, self.controller, self.holdings)
        self.assertEqual([call.args[0] for call in output.call_args_list],
                         [logger.MESSAGES[operation] for operation in
                          ("swap-pause", "pressure-release")])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        # Lifecycle coverage uses tiny mocked mappings and a fixed recovery timeline.
        for name, value in (("CHUNK_SIZE", MIB), ("QUIET_SECONDS", 30)):
            override = patch.object(adaptive, name, value)
            override.start()
            self.addCleanup(override.stop)

    def run_main(self, readings, mapping=None, sleep=None, argv=()):
        mapping = mapping if mapping is not None else MagicMock()
        with patch.object(adaptive, "read_sample", side_effect=readings), \
                patch.object(adaptive.mmap, "mmap", return_value=mapping) as factory, \
                patch.object(adaptive.time, "sleep", side_effect=sleep), \
                patch.object(adaptive.signal, "signal") as signals, \
                patch.object(adaptive, "sys") as sysmock:
            sysmock.platform = "linux"
            result = adaptive.main(["--amount", str(MIB), *argv])
        return result, factory, signals

    def test_status_logs_reason_and_cooldown_remaining(self):
        readings = [sample(at) for at in range(32)] + [sample(32, swapout=1), KeyboardInterrupt()]
        with patch("builtins.print") as output:
            result, _, _ = self.run_main(readings, argv=["--verbose"])
        self.assertEqual(result, 0)
        lines = [call.args[0] for call in output.call_args_list]
        self.assertIn("reason=telemetry baseline/reset or invalid interval; cooldown_remaining=30.0s", lines[0])
        self.assertIn("reason=waiting for 30s below pressure triggers in recovery band; cooldown_remaining=1.0s", lines[30])
        self.assertIn("reason=recovery complete; headroom available; cooldown_remaining=0.0s", lines[31])
        self.assertIn("reason=new swap-out (release pressure): pswpout +32 pages; cooldown_remaining=30.0s", lines[32])
        self.assertIn(f"allocation_interval={2 * adaptive.SAMPLE_SECONDS:g}s", lines[32])
        self.assertIn("refresh=", lines[32])

    def test_default_cli_logs_transitions_and_shutdown_summary(self):
        readings = [sample(at) for at in range(35)] + [KeyboardInterrupt()]
        with patch("builtins.print") as output:
            result, _, _ = self.run_main(readings)
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in output.call_args_list],
                         [adaptive.StatusLogger.MESSAGES[operation] for operation in
                          ("recovery", "cooldown", "refill", "holding")] +
                         [f"Released mappings; cumulative={MIB} bytes"])

    def test_verbose_flags_enable_per_sample_logs(self):
        for flag in ("-v", "--verbose"):
            with self.subTest(flag=flag), patch("builtins.print") as output:
                result, _, _ = self.run_main(
                    [sample(0), sample(1), sample(2), KeyboardInterrupt()], argv=[flag])
            self.assertEqual(result, 0)
            self.assertEqual(len(output.call_args_list), 4)
            self.assertTrue(output.call_args_list[0].args[0].startswith("recovery-hold:"))
            self.assertTrue(output.call_args_list[1].args[0].startswith("cooldown:"))
            self.assertTrue(output.call_args_list[2].args[0].startswith("cooldown:"))

    def test_errors_and_shutdown_are_visible_in_both_modes(self):
        for argv in ([], ["--verbose"]):
            for error in (OSError("telemetry"), ValueError("invalid"), MemoryError("allocation")):
                with self.subTest(argv=argv, error=error), patch("builtins.print") as output:
                    result, _, _ = self.run_main([error], argv=argv)
                self.assertEqual(result, 1)
                self.assertEqual([call.args[0] for call in output.call_args_list],
                                 [f"adaptive-ram failed: {error}",
                                  "Released mappings; cumulative=0 bytes"])

    def test_cleanup_errors_are_visible_in_both_modes(self):
        for argv in ([], ["--verbose"]):
            mapping = MagicMock()
            mapping.close.side_effect = OSError("close")
            with self.subTest(argv=argv), patch("builtins.print") as output:
                result, _, _ = self.run_main(
                    [sample(at) for at in range(32)] + [KeyboardInterrupt()],
                    mapping, argv=argv)
            self.assertEqual(result, 1)
            lines = [call.args[0] for call in output.call_args_list]
            self.assertIn("mapping cleanup failed: close", lines)
            self.assertEqual(lines[-1],
                             f"Mapping cleanup incomplete; held={MIB} bytes; cumulative={MIB} bytes")

    def test_cap_keeps_monitoring_then_interrupt_cleans_up(self):
        mapping = MagicMock()
        readings = [sample(at) for at in range(35)] + [KeyboardInterrupt()]
        result, factory, _ = self.run_main(readings, mapping)
        self.assertEqual(result, 0)
        factory.assert_called_once_with(-1, MIB, flags=adaptive.mmap.MAP_PRIVATE | adaptive.mmap.MAP_ANONYMOUS,
                                        prot=adaptive.mmap.PROT_READ | adaptive.mmap.PROT_WRITE)
        mapping.close.assert_called_once_with()
        mapping.madvise.assert_not_called()
        self.assertEqual(mapping.__setitem__.call_count, len(range(0, MIB, adaptive.mmap.PAGESIZE)))

    def test_telemetry_errors_cleanup(self):
        for error in (OSError("unreadable"), ValueError("invalid"), KeyError("full")):
            with self.subTest(error=error):
                mapping = MagicMock()
                result, _, _ = self.run_main([sample(at) for at in range(32)] + [error], mapping)
                self.assertEqual(result, 1)
                mapping.close.assert_called_once_with()

    def test_partial_touch_failure_closes_current_and_existing(self):
        holdings = adaptive.Holdings()
        first, second = MagicMock(), MagicMock()
        second.__setitem__.side_effect = [None, MemoryError("touch")]
        with patch.object(adaptive.mmap, "mmap", side_effect=[first, second]):
            holdings.allocate(2 * adaptive.mmap.PAGESIZE)
            with self.assertRaises(MemoryError):
                holdings.allocate(2 * adaptive.mmap.PAGESIZE)
        holdings.close()
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        self.assertEqual(holdings.held, 0)

    def test_partial_allocation_failure_main(self):
        mapping = MagicMock()
        mapping.__setitem__.side_effect = MemoryError("touch")
        result, _, _ = self.run_main([sample(at) for at in range(32)], mapping)
        self.assertEqual(result, 1)
        mapping.close.assert_called_once_with()

    def test_mmap_failure_and_startup_telemetry_failure(self):
        with patch.object(adaptive.mmap, "mmap", side_effect=OSError("mmap")):
            holdings = adaptive.Holdings()
            with self.assertRaises(OSError):
                holdings.allocate(1)
            holdings.close()
            self.assertEqual(holdings.cumulative, 0)
        result, factory, _ = self.run_main([OSError("telemetry")])
        self.assertEqual(result, 1)
        factory.assert_not_called()

    def test_sigterm_handler_exits_and_restores_handlers(self):
        def terminate(_):
            adaptive.interrupted(signal.SIGTERM, None)
        result, _, signals = self.run_main([sample(0)], sleep=terminate)
        self.assertEqual(result, 0)
        self.assertEqual(signals.call_args_list[0].args, (signal.SIGINT, adaptive.interrupted))
        self.assertEqual(signals.call_args_list[1].args, (signal.SIGTERM, adaptive.interrupted))
        self.assertEqual(signals.call_count, 6)

    def test_cleanup_continues_after_close_error(self):
        holdings = adaptive.Holdings()
        first, second = MagicMock(), MagicMock()
        first.close.side_effect = OSError("close")
        holdings.mappings = [(first, 1), (second, 1)]
        holdings.held = 2
        with self.assertRaises(RuntimeError):
            holdings.close()
        second.close.assert_called_once_with()
        self.assertEqual(holdings.mappings, [(first, 1)])
        self.assertEqual(holdings.held, 1)
        first.close.side_effect = None
        holdings.close()
        self.assertEqual(holdings.held, 0)


if __name__ == "__main__":
    unittest.main()
