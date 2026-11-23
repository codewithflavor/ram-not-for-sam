"""Safe primary-allocator tests: fake mappings except a few-page Linux smoke."""

import ctypes
import io
import signal
import sys
import unittest
from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from unittest.mock import MagicMock, call, patch

program = SourceFileLoader("primary_holder", "ram-not-for-sam").load_module()
P = program.PAGE_SIZE


def vma(start, end, swap=0, rss=0, extra=""):
    return (f"{start:x}-{end:x} rw-p 00000000 00:00 0\n"
            f"Rss: {rss} kB\nSwap: {swap} kB\n{extra}")


class FakeMapping:
    def __init__(self, failure=None, close_failure=None):
        self.events = []
        self.failure = failure
        self.close_failure = close_failure

    def __getitem__(self, key):
        raise AssertionError("old filler contents must never be read")

    def __setitem__(self, offset, value):
        self.events.append(("touch", offset, value))
        if self.failure:
            raise self.failure

    def close(self):
        self.events.append(("close",))
        if self.close_failure:
            raise self.close_failure


class SizingTests(unittest.TestCase):
    def test_binary_sizes(self):
        for value, expected in (("0", 0), ("123", 123), ("2K", 2048),
                                ("2m", 2 * 1024**2), ("3G", 3 * 1024**3),
                                ("1t", 1024**4)):
            self.assertEqual(program.parse_size(value), expected)
        for value in ("-1", "1.5G", "1MB", "foo", ""):
            with self.subTest(value=value), self.assertRaises(Exception):
                program.parse_size(value)

    def test_page_alignment_headroom_cap_and_chunk_bound(self):
        self.assertEqual(program.next_chunk(10 * P + 1, 0, None, P), 9 * P)
        self.assertEqual(program.next_chunk(10 * P, 2 * P, 4 * P + 1, 0), 2 * P)
        self.assertEqual(program.next_chunk(10 * P, 4 * P, 4 * P, 0), 0)
        self.assertEqual(program.next_chunk(P, 0, None, 2 * P), 0)
        self.assertEqual(program.next_chunk(P - 1, 0, None, 0), 0)
        self.assertEqual(program.next_chunk(10**30, 0, None, 0), 512 * 1024**2)
        # Large integer headroom must not pass through floating-point arithmetic.
        size = program.next_chunk(10**30, 10**30 - P - 1, 10**30, 0)
        self.assertEqual(size, P)
        self.assertIsInstance(size, int)

    def test_meminfo_does_not_require_global_swap(self):
        self.assertEqual(program.parse_meminfo("MemTotal: 10 kB\nMemAvailable: 4 kB\n"),
                         (10240, 4096))
        for text in ("", "MemTotal: 1 kB\n", "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
                     "MemTotal: 1 kB\nMemAvailable: 2 kB\n",
                     "MemTotal: 10 kB\nMemAvailable: -1 kB\n",
                     "MemTotal: 10 kB\nMemAvailable: 1 MB\n",
                     "MemTotal: 10 kB\nMemAvailable: 1 kB\nMemAvailable: 1 kB\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                program.parse_meminfo(text)


class AttributionTests(unittest.TestCase):
    def chunk(self, start=0x10000, length=2 * P):
        return program.Chunk(FakeMapping(), length, start)

    def test_exact_swap_not_swappss_and_unrelated_swap_ignored(self):
        chunk = self.chunk()
        text = (vma(0x1000, 0x2000, swap=4)
                + vma(chunk.address, chunk.address + chunk.length, rss=4,
                      extra="SwapPss: 4 kB\n"))
        self.assertEqual(program.parse_smaps(text, [chunk]), {chunk: (4096, 0)})

    def test_split_vmas_sum_and_ignore_guards(self):
        chunk = self.chunk()
        start = chunk.address
        text = (vma(start - P, start) + vma(start, start + P, swap=1)
                + vma(start + P, start + 2 * P, rss=1)
                + vma(start + 2 * P, start + 3 * P))
        self.assertEqual(program.parse_smaps(text, [chunk]), {chunk: (1024, 1024)})

    def test_rss_plus_swap_need_not_equal_length(self):
        chunk = self.chunk(length=P)
        text = vma(chunk.address, chunk.address + P, rss=P // 1024, swap=P // 1024)
        self.assertEqual(program.parse_smaps(text, [chunk])[chunk], (P, P))

    def test_fail_closed_for_coverage_and_fields(self):
        chunk = self.chunk()
        start, end = chunk.address, chunk.address + chunk.length
        texts = [
            "", "not smaps\n", vma(start - P, end), vma(start, end + P),
            vma(start + P, end), vma(start, end - P),
            vma(start, start + P) + vma(start + P + 1, end),
            vma(start, end) + vma(start + P, end),
            vma(start, end).replace("Swap: 0 kB\n", ""),
            vma(start, end).replace("Swap: 0 kB", "Swap: no kB"),
            vma(start, end).replace("Swap: 0 kB", "Swap: -1 kB"),
            vma(start, end).replace("Swap: 0 kB", "Swap: 0 MB"),
            vma(start, end, extra="Swap 0 kB\n"),
            vma(start, end, extra=" Swap: 0 kB\n"),
            vma(start, end, extra="Swap: 0 kB\n"),
            vma(start, end, extra="Swap: 0 kB\nSwap: 0 kB\n"),
            vma(start, end).replace("Rss: 0 kB\n", ""),
            vma(start, end, extra="Rss: 0 kB\n"),
            vma(start, end, swap=chunk.length // 1024 + 1),
            vma(start, end, rss=chunk.length // 1024 + 1),
            vma(start, end).replace("rw-p", "oops"),
            vma(start, end) + "zzzz-qqqq rw-p 00000000 00:00 0\n",
        ]
        for text in texts:
            with self.subTest(text=text), self.assertRaises(ValueError):
                program.parse_smaps(text, [chunk])

    def test_missing_swap_cannot_be_replaced_by_swappss(self):
        chunk = self.chunk()
        text = vma(chunk.address, chunk.address + chunk.length).replace("Swap:", "SwapPss:")
        with self.assertRaises(ValueError):
            program.parse_smaps(text, [chunk])

    def test_multiple_chunks_sort_and_half_open_edges(self):
        first = self.chunk(length=P)
        second = self.chunk(first.address + P, P)
        text = vma(first.address, second.address, swap=1) + vma(
            second.address, second.address + P, rss=2)
        result = program.parse_smaps(text, [second, first])
        self.assertEqual(result, {first: (0, 1024), second: (2048, 0)})
        with self.assertRaises(ValueError):
            program.parse_smaps(vma(first.address, second.address + P), [first, second])

    def test_overlapping_or_uninitialized_tracking_rejected(self):
        first = self.chunk()
        second = self.chunk(first.address + P)
        text = vma(first.address, first.address + 3 * P)
        with self.assertRaises(ValueError):
            program.parse_smaps(text, [first, second])
        with self.assertRaises(ValueError):
            program.parse_smaps(text, [program.Chunk(FakeMapping(), P)])

    def test_read_once_per_file_and_passes_only_tracked_chunks(self):
        chunk = self.chunk()
        text = vma(chunk.address, chunk.address + chunk.length)
        memfile = MagicMock()
        memfile.__enter__.return_value.read.return_value = (
            "MemTotal: 100 kB\nMemAvailable: 20 kB\n")
        smapsfile = MagicMock()
        smapsfile.__enter__.return_value.read.return_value = text
        with patch.object(program, "open", create=True, side_effect=[memfile, smapsfile]) as op:
            available, stats = program.read_telemetry([chunk])
        self.assertEqual(available, 20 * 1024)
        self.assertEqual(stats, {chunk: (0, 0)})
        self.assertEqual(op.call_args_list,
                         [call("/proc/meminfo", encoding="ascii"),
                           call("/proc/self/smaps", encoding="utf-8",
                                errors="surrogateescape")])
        memfile.__enter__.return_value.read.assert_called_once_with()
        smapsfile.__enter__.return_value.read.assert_called_once_with()

    def test_reader_accepts_non_ascii_and_raw_filename_bytes(self):
        chunk = self.chunk()
        for filename in ("/tmp/café".encode("utf-8"), b"/tmp/raw-\xff"):
            with self.subTest(filename=filename):
                unrelated = vma(0x1000, 0x2000, rss=4, swap=4).encode("ascii")
                unrelated = unrelated.replace(b"00:00 0\n", b"00:00 0 " + filename + b"\n")
                files = {
                    "/proc/meminfo": b"MemTotal: 100 kB\nMemAvailable: 20 kB\n",
                    "/proc/self/smaps": unrelated + vma(
                        chunk.address, chunk.address + chunk.length,
                        rss=2, swap=1).encode("ascii"),
                }

                def open_bytes(path, **kwargs):
                    return io.TextIOWrapper(io.BytesIO(files[path]), **kwargs)

                with patch.object(program, "open", create=True, side_effect=open_bytes):
                    available, stats = program.read_telemetry([chunk])
                self.assertEqual(available, 20 * 1024)
                self.assertEqual(stats, {chunk: (2048, 1024)})


class LifecycleTests(unittest.TestCase):
    def holder(self, protect=None):
        return program.Holder(0, None, protect or MagicMock(return_value=0))

    def test_guards_checked_before_touch_and_only_interior_touched(self):
        mapping = FakeMapping()
        events = mapping.events
        protect = MagicMock(side_effect=lambda *args: events.append(("guard", *args)) or 0)
        holder = self.holder(protect)
        base = 2**40  # C pointers must not be truncated to a 32-bit int.
        with patch.object(program.mmap, "mmap", return_value=mapping) as factory, \
                patch.object(program, "mapping_address", return_value=base):
            chunk = holder.allocate(2 * P)
        factory.assert_called_once_with(
            -1, 4 * P, flags=program.mmap.MAP_PRIVATE | program.mmap.MAP_ANONYMOUS,
            prot=program.mmap.PROT_READ | program.mmap.PROT_WRITE)
        self.assertEqual(events, [("guard", base, P, 0), ("guard", base + 3 * P, P, 0),
                                  ("touch", P, 1), ("touch", 2 * P, 1)])
        self.assertEqual((chunk.address, chunk.length, holder.held), (base + P, 2 * P, 2 * P))
        self.assertTrue(holder.cleanup())
        self.assertEqual(holder.held, 0)

    def test_pointer_safe_ctypes_signature(self):
        libc = MagicMock()
        with patch.object(program.ctypes, "CDLL", return_value=libc) as load:
            protect = program.load_mprotect()
        load.assert_called_once_with(None, use_errno=True)
        self.assertEqual(protect.argtypes,
                         [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int])
        self.assertIs(protect.restype, ctypes.c_int)

    def test_first_or_second_guard_failure_tracks_mapping_for_cleanup(self):
        for results in ([-1], [0, -1]):
            holder = self.holder(MagicMock(side_effect=results))
            mapping = FakeMapping()
            with patch.object(program.mmap, "mmap", return_value=mapping), \
                    patch.object(program, "mapping_address", return_value=0x10000), \
                    patch.object(program.ctypes, "get_errno", return_value=12):
                with self.assertRaises(OSError) as caught:
                    holder.allocate(P)
            self.assertEqual(caught.exception.errno, 12)
            self.assertEqual(holder.held, P)
            self.assertEqual(mapping.events, [])
            self.assertTrue(holder.cleanup())
            self.assertEqual(mapping.events, [("close",)])

    def test_address_and_partial_touch_failure_cleanup_and_counters(self):
        for failure in (OSError("fault"), MemoryError(), KeyboardInterrupt()):
            holder = self.holder()
            mapping = FakeMapping(failure=failure)
            with patch.object(program.mmap, "mmap", return_value=mapping), \
                    patch.object(program, "mapping_address", return_value=0x10000):
                with self.assertRaises(type(failure)):
                    holder.allocate(2 * P)
            self.assertEqual(holder.held, 2 * P)
            holder.cleanup()
            self.assertEqual(holder.held, 0)
            self.assertEqual(mapping.events, [("touch", P, 1), ("close",)])
        holder = self.holder()
        mapping = FakeMapping()
        with patch.object(program.mmap, "mmap", return_value=mapping), \
                patch.object(program, "mapping_address", side_effect=ValueError("buffer")):
            with self.assertRaises(ValueError):
                holder.allocate(P)
        holder.cleanup()
        self.assertEqual(mapping.events, [("close",)])

    def test_mmap_failure_never_increments_held(self):
        holder = self.holder()
        with patch.object(program.mmap, "mmap", side_effect=OSError("no RAM")):
            with self.assertRaises(OSError):
                holder.allocate(P)
        self.assertEqual(holder.held, 0)

    def test_failure_after_first_touch_preserves_full_mapping_ownership(self):
        mapping = FakeMapping()
        holder = self.holder()
        original_touch = mapping.__setitem__

        def touch(offset, value):
            original_touch(offset, value)
            if offset == 2 * P:
                raise OSError("later page fault")

        with patch.object(FakeMapping, "__setitem__", side_effect=touch), \
                patch.object(program.mmap, "mmap", return_value=mapping), \
                patch.object(program, "mapping_address", return_value=0x10000):
            with self.assertRaises(OSError):
                holder.allocate(3 * P)
        self.assertEqual(holder.held, 3 * P)
        self.assertTrue(holder.cleanup())
        self.assertEqual(holder.held, 0)
        self.assertEqual(mapping.events, [("touch", P, 1), ("touch", 2 * P, 1), ("close",)])

    def test_cleanup_continues_and_does_not_claim_failed_close_freed(self):
        for failure in (BufferError("export"), KeyboardInterrupt()):
            holder = self.holder()
            bad, good = FakeMapping(close_failure=failure), FakeMapping()
            holder.chunks = [program.Chunk(bad, P), program.Chunk(good, 2 * P)]
            with redirect_stderr(io.StringIO()) as output:
                self.assertFalse(holder.cleanup())
            self.assertEqual(holder.held, P)
            self.assertEqual(len(holder.chunks), 1)
            self.assertIn("Cannot close filler mapping", output.getvalue())
            self.assertEqual(good.events, [("close",)])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux guarded mmap smoke")
    def test_real_few_page_guards_attribution_and_no_buffer_leak(self):
        holder = self.holder(program.load_mprotect())
        try:
            first = holder.allocate(2 * P)
            second = holder.allocate(P)
            with open("/proc/self/smaps", encoding="utf-8", errors="surrogateescape") as source:
                text = source.read()
            stats = program.parse_smaps(text, holder.chunks)
            self.assertEqual(set(stats), {first, second})
            for chunk in holder.chunks:
                self.assertEqual(stats[chunk][1], 0)
                for address in (chunk.address - P, chunk.address + chunk.length):
                    # Adjacent PROT_NONE guards may merge with one another;
                    # only the RW interiors must remain distinct.
                    guards = [line for line in text.splitlines()
                              if program.HEADER_RE.fullmatch(line)
                              and line.split()[1] == "---p"]
                    self.assertTrue(any(
                        int(line.split("-")[0], 16) <= address
                        and int(line.split()[0].split("-")[1], 16) >= address + P
                        for line in guards))
        finally:
            self.assertTrue(holder.cleanup())  # BufferError would expose a leaked export.
        self.assertEqual(holder.held, 0)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.holder = program.Holder(P, 4 * P, MagicMock(return_value=0))
        self.chunks = [program.Chunk(FakeMapping(), P, (i + 1) * 0x10000)
                       for i in range(3)]
        self.holder.chunks = self.chunks.copy()

    def test_disposes_all_and_only_swapped_no_old_read_no_growth_then_resumes(self):
        stats = {self.chunks[0]: (0, 1024), self.chunks[1]: (P, 0),
                 self.chunks[2]: (0, 2048)}
        with patch.object(program, "read_telemetry", return_value=(100 * P, stats)) as read, \
                patch.object(program.time, "monotonic", return_value=20), \
                patch.object(self.holder, "allocate") as allocate:
            self.assertEqual(self.holder.step()[0], "cleanup")
            allocate.assert_not_called()
            read.assert_called_once_with(self.holder.chunks)
        self.assertEqual(self.holder.chunks, [self.chunks[1]])
        self.assertEqual(self.holder.held, P)
        self.assertEqual(self.holder.cooldown_until, 30)
        self.assertEqual([chunk.mapping.events for chunk in self.chunks],
                         [[("close",)], [], [("close",)]])
        fresh_stats = {self.chunks[1]: (P, 0)}
        with patch.object(program, "read_telemetry", return_value=(100 * P, fresh_stats)) as read, \
                patch.object(program.time, "monotonic", return_value=29), \
                patch.object(self.holder, "allocate") as allocate:
            self.assertEqual(self.holder.step()[0], "cooldown")
            allocate.assert_not_called()
            self.assertEqual(read.call_count, 1)
        # Fresh low headroom at cooldown expiry blocks growth, not old availability.
        with patch.object(program, "read_telemetry", return_value=(P, fresh_stats)), \
                patch.object(program.time, "monotonic", return_value=30), \
                patch.object(self.holder, "allocate") as allocate:
            self.assertEqual(self.holder.step()[0], "hold")
            allocate.assert_not_called()
        with patch.object(program, "read_telemetry", return_value=(100 * P, fresh_stats)), \
                patch.object(program.time, "monotonic", return_value=31), \
                patch.object(self.holder, "allocate") as allocate:
            self.assertEqual(self.holder.step()[0], "grow")
            allocate.assert_called_once_with(3 * P)

    def test_new_swap_during_cooldown_disposed_and_timer_restarts(self):
        self.holder.cooldown_until = 100
        stats = {chunk: (0, 1024 if i == 1 else 0)
                 for i, chunk in enumerate(self.chunks)}
        with patch.object(program, "read_telemetry", return_value=(0, stats)), \
                patch.object(program.time, "monotonic", return_value=95), \
                patch.object(self.holder, "allocate") as allocate:
            self.assertEqual(self.holder.step()[0], "cleanup")
            allocate.assert_not_called()
        self.assertEqual(self.holder.cooldown_until, 105)

    def test_low_headroom_does_not_unmap_unswapped_chunks(self):
        stats = {chunk: (P, 0) for chunk in self.chunks}
        with patch.object(program, "read_telemetry", return_value=(0, stats)):
            self.assertEqual(self.holder.step()[0], "hold")
        self.assertEqual(self.holder.held, 3 * P)
        self.assertTrue(all(chunk.mapping.events == [] for chunk in self.chunks))

    def test_stop_during_telemetry_prevents_new_mapping(self):
        self.holder.stopped = lambda: True
        with patch.object(program, "read_telemetry", return_value=(100 * P, {})), \
                patch.object(self.holder, "allocate") as allocate:
            with self.assertRaises(KeyboardInterrupt):
                self.holder.step()
            allocate.assert_not_called()

    def test_cap_holds_and_still_checks_swap(self):
        self.holder.amount = self.holder.held
        stats = {chunk: (P, 0) for chunk in self.chunks}
        with patch.object(program, "read_telemetry", return_value=(100 * P, stats)) as read:
            self.assertEqual(self.holder.step()[0], "hold")
            self.assertEqual(self.holder.step()[0], "hold")
            self.assertEqual(read.call_count, 2)
        stats[self.chunks[0]] = (0, 1024)
        with patch.object(program, "read_telemetry", return_value=(100 * P, stats)):
            self.assertEqual(self.holder.step()[0], "cleanup")

    def test_ambiguous_required_telemetry_prevents_growth_and_disposal(self):
        with patch.object(program, "read_telemetry", side_effect=ValueError("ambiguous")), \
                patch.object(self.holder, "allocate") as allocate:
            with self.assertRaises(ValueError):
                self.holder.step()
            allocate.assert_not_called()
        self.assertTrue(all(chunk.mapping.events == [] for chunk in self.chunks))

    def test_default_semantic_logs_and_verbose_details(self):
        with redirect_stderr(io.StringIO()) as output:
            for action in ("grow", "grow", "hold", "hold", "cleanup", "cooldown", "grow"):
                self.holder.log(action, 10 * P, {}, False)
        self.assertEqual(len(output.getvalue().splitlines()), 5)
        self.assertIn("Sam", output.getvalue())
        self.holder.cooldown_until = 15
        with patch.object(program.time, "monotonic", return_value=10), \
                redirect_stderr(io.StringIO()) as output:
            self.holder.log("cooldown", 10 * P, {self.chunks[0]: (P, 1024)}, True)
        for field in (f"held={3 * P}", f"resident_sample={P}", "swap_sample=1024",
                      f"reserve={P}", "cooldown=5.0s"):
            self.assertIn(field, output.getvalue())


class MainTests(unittest.TestCase):
    def test_unsupported_fails_before_allocation(self):
        with patch.object(program.sys, "platform", "darwin"), \
                patch.object(program.mmap, "mmap") as factory, \
                redirect_stderr(io.StringIO()) as output:
            self.assertEqual(program.main([]), 1)
        factory.assert_not_called()
        self.assertIn("Linux", output.getvalue())

    def test_required_preflight_read_failure_and_malformed_smaps(self):
        for error in (OSError("smaps denied"), ValueError("empty smaps")):
            with patch.object(program, "load_mprotect", return_value=MagicMock()), \
                    patch.object(program, "read_telemetry", side_effect=error), \
                    patch.object(program.mmap, "mmap") as factory, \
                    redirect_stderr(io.StringIO()) as output:
                self.assertEqual(program.main([]), 1)
            factory.assert_not_called()
            self.assertIn("failed:", output.getvalue())

    def test_signals_cleanup_restore_handlers_and_cap_keeps_running(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers = {}
            previous = {signal.SIGINT: object(), signal.SIGTERM: object()}

            def install(which, handler):
                old = handlers.get(which, previous[which])
                handlers[which] = handler
                return old

            def terminate(_):
                handlers[sig](sig, None)

            with patch.object(program, "load_mprotect", return_value=MagicMock()), \
                    patch.object(program.signal, "signal", side_effect=install), \
                    patch.object(program, "read_telemetry", return_value=(100 * P, {})) as read, \
                    patch.object(program.time, "sleep", side_effect=terminate) as sleep, \
                    patch.object(program.mmap, "mmap") as factory, \
                    redirect_stderr(io.StringIO()) as output:
                self.assertEqual(program.main(["--amount", "0"]), 0)
            sleep.assert_called_once_with(1)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(handlers, previous)
            factory.assert_not_called()
            self.assertIn("still held=0", output.getvalue())

    def test_touch_failure_or_interrupt_main_cleans_tracked_mapping(self):
        for failure, expected in ((OSError("touch failed"), 1), (KeyboardInterrupt(), 0)):
            mapping = FakeMapping(failure=failure)
            with patch.object(program, "load_mprotect", return_value=MagicMock(return_value=0)), \
                    patch.object(program, "read_telemetry", return_value=(10 * P, {})), \
                    patch.object(program, "mapping_address", return_value=0x10000), \
                    patch.object(program.mmap, "mmap", return_value=mapping), \
                    redirect_stderr(io.StringIO()) as output:
                self.assertEqual(program.main(["--reserve", "0", "--amount", str(P)]), expected)
            self.assertEqual(mapping.events, [("touch", P, 1), ("close",)])
            self.assertIn("still held=0", output.getvalue())

    def test_cleanup_close_failure_main_returns_error_and_reports_held(self):
        mapping = FakeMapping(failure=OSError("touch"), close_failure=BufferError("busy"))
        with patch.object(program, "load_mprotect", return_value=MagicMock(return_value=0)), \
                patch.object(program, "read_telemetry", return_value=(10 * P, {})), \
                patch.object(program, "mapping_address", return_value=0x10000), \
                patch.object(program.mmap, "mmap", return_value=mapping), \
                redirect_stderr(io.StringIO()) as output:
            self.assertEqual(program.main(["--reserve", "0", "--amount", str(P)]), 1)
        self.assertIn(f"still held={P}", output.getvalue())
        self.assertIn("Cannot close", output.getvalue())

    def test_required_telemetry_failure_after_allocation_cleans_up(self):
        mapping = FakeMapping()
        with patch.object(program, "load_mprotect", return_value=MagicMock(return_value=0)), \
                patch.object(program, "read_telemetry",
                             side_effect=[(10 * P, {}), ValueError("incomplete coverage")]), \
                patch.object(program, "mapping_address", return_value=0x10000), \
                patch.object(program.time, "sleep") as sleep, \
                patch.object(program.mmap, "mmap", return_value=mapping), \
                redirect_stderr(io.StringIO()) as output:
            self.assertEqual(program.main(["--reserve", "0", "--amount", str(P)]), 1)
        sleep.assert_called_once_with(1)
        self.assertEqual(mapping.events, [("touch", P, 1), ("close",)])
        self.assertIn("incomplete coverage", output.getvalue())

    def test_swapped_mapping_close_failure_is_fatal_and_retried_on_shutdown(self):
        mapping = FakeMapping(close_failure=BufferError("export"))

        def telemetry(chunks):
            return 10 * P, {chunk: (0, 1024) for chunk in chunks}

        with patch.object(program, "load_mprotect", return_value=MagicMock(return_value=0)), \
                patch.object(program, "read_telemetry", side_effect=telemetry), \
                patch.object(program, "mapping_address", return_value=0x10000), \
                patch.object(program.time, "sleep"), \
                patch.object(program.mmap, "mmap", return_value=mapping), \
                redirect_stderr(io.StringIO()) as output:
            self.assertEqual(program.main(["--reserve", "0", "--amount", str(P)]), 1)
        self.assertEqual(mapping.events, [("touch", P, 1), ("close",), ("close",)])
        self.assertIn("RAM holder failed: export", output.getvalue())
        self.assertIn(f"still held={P}", output.getvalue())


if __name__ == "__main__":
    unittest.main()
