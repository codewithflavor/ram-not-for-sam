import unittest
from importlib.machinery import SourceFileLoader
from unittest.mock import MagicMock, patch

swap_cycle = SourceFileLoader("swap_cycle", "swap-cycle").load_module()
pageout = SourceFileLoader("pageout_2g", "pageout-2g").load_module()
free_2g = SourceFileLoader("free_2g", "free-2g").load_module()


class Free2GTests(unittest.TestCase):
    def _run(self, *, constant=None, failure=None):
        events = []

        class FakeMapping:
            def __setitem__(self, offset, value):
                events.append(("touch", offset, value))
            def madvise(self, advice):
                events.append(("advice", advice))
                if failure:
                    raise OSError(failure)
            def close(self):
                events.append(("close",))

        fake = FakeMapping()
        with patch.object(free_2g, "sys") as sysmock, patch.object(free_2g, "mmap") as mm, \
                patch.object(free_2g, "signal") as sig, \
                patch.object(free_2g, "PAGE_SIZE", free_2g.SIZE):
            sysmock.platform = "linux"
            attrs = {"MAP_ANONYMOUS": 32, "MAP_PRIVATE": 2, "PROT_READ": 1,
                     "PROT_WRITE": 2, "PAGESIZE": free_2g.SIZE,
                     "mmap": MagicMock(return_value=fake)}
            if constant is not None:
                attrs["MADV_FREE"] = constant
            for name, value in attrs.items():
                setattr(mm, name, value)
            if constant is None:
                del mm.MADV_FREE
            sig.pause.side_effect = KeyboardInterrupt
            result = free_2g.main()
        return result, events

    def test_uapi_fallback_and_touch_before_advice_then_cleanup(self):
        result, events = self._run()
        self.assertEqual(result, 0)
        self.assertEqual(events, [("touch", 0, 1), ("advice", 8), ("close",)])

    def test_python_constant_and_advice_failure_cleanup(self):
        result, events = self._run(constant=42, failure="rejected")
        self.assertEqual(result, 1)
        self.assertEqual(events, [("touch", 0, 1), ("advice", 42), ("close",)])


class PageoutTests(unittest.TestCase):
    def _run(self, mapping, *, constant=None, interrupt=True):
        events = []
        class FakeMapping:
            def __setitem__(self, offset, value):
                events.append(("touch", offset))
            def madvise(self, advice):
                events.append(("advice", advice))
            def close(self):
                events.append(("close",))
        fake = mapping if mapping is not None else FakeMapping()
        with patch.object(pageout, "sys") as sysmock, patch.object(pageout, "mmap") as mm, \
                patch.object(pageout, "signal") as sig:
            sysmock.platform = "linux"
            attrs = {"MAP_ANONYMOUS": 32, "MAP_PRIVATE": 2, "PROT_READ": 1,
                     "PROT_WRITE": 2, "PAGESIZE": 4096, "mmap": MagicMock(return_value=fake)}
            if constant is not None:
                attrs["MADV_PAGEOUT"] = constant
            for name, value in attrs.items():
                setattr(mm, name, value)
            if constant is None:
                del mm.MADV_PAGEOUT
            if interrupt:
                sig.pause.side_effect = KeyboardInterrupt
            result = pageout.main()
        return result, events, fake

    def test_touches_before_pageout_and_fallback_then_closes(self):
        result, events, _ = self._run(None)
        self.assertEqual(result, 0)
        self.assertEqual(events[0:2], [("touch", 0), ("touch", 4096)])
        self.assertEqual(events[-2:], [("advice", 21), ("close",)])

    def test_python_constant_is_used_and_advice_failure_closes(self):
        class BadMapping:
            def __setitem__(self, offset, value): pass
            def madvise(self, advice):
                self.advice = advice
                raise OSError("rejected")
            def close(self): self.closed = True
        mapping = BadMapping()
        with patch.object(pageout, "sys") as sysmock, patch.object(pageout, "mmap") as mm:
            sysmock.platform = "linux"
            for name, value in {"MAP_ANONYMOUS": 32, "MAP_PRIVATE": 2, "PROT_READ": 1,
                                "PROT_WRITE": 2, "PAGESIZE": pageout.SIZE,
                                "MADV_PAGEOUT": 42, "mmap": MagicMock(return_value=mapping)}.items():
                setattr(mm, name, value)
            self.assertEqual(pageout.main(), 1)
        self.assertTrue(mapping.closed)
        self.assertEqual(mapping.advice, 42)


class SwapCycleTests(unittest.TestCase):
    def test_worker_ready_is_sent_only_after_madvise(self):
        events = []

        class Mapping:
            def __setitem__(self, offset, value):
                events.append("touch")

            def madvise(self, advice):
                events.append("madvise")

        with patch.object(swap_cycle, "sys") as sysmock, \
                patch.object(swap_cycle, "mmap") as mm, \
                patch.object(swap_cycle, "os") as osmock:
            sysmock.platform = "linux"
            for name, value in {"MAP_ANONYMOUS": 32, "MAP_PRIVATE": 2,
                                "PROT_READ": 1, "PROT_WRITE": 2,
                                "mmap": MagicMock(return_value=Mapping()), "MADV_COLD": 20}.items():
                setattr(mm, name, value)
            osmock.write.side_effect = lambda fd, data: events.append("ready")
            with patch.object(swap_cycle, "PAGE_SIZE", swap_cycle.WORKER_SIZE), \
                    patch.object(swap_cycle.signal, "pause", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    swap_cycle.worker(123)
        self.assertEqual(events, ["touch", "madvise", "ready"])

    def test_worker_madvise_failure_never_signals_ready(self):
        mapping = MagicMock()
        mapping.madvise.side_effect = OSError("unsupported")
        with patch.object(swap_cycle, "sys") as sysmock, \
                patch.object(swap_cycle, "mmap") as mm, \
                patch.object(swap_cycle, "os") as osmock:
            sysmock.platform = "linux"
            for name, value in {"MAP_ANONYMOUS": 32, "MAP_PRIVATE": 2,
                                "PROT_READ": 1, "PROT_WRITE": 2,
                                "mmap": MagicMock(return_value=mapping), "MADV_COLD": 20}.items():
                setattr(mm, name, value)
            with self.assertRaises(OSError):
                swap_cycle.worker(123)
            osmock.write.assert_not_called()

    def test_stop_workers_kills_and_reaps_timeout(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [swap_cycle.subprocess.TimeoutExpired("worker", 2), 0]
        process.wait.side_effect = [swap_cycle.subprocess.TimeoutExpired("worker", 2), 0]
        swap_cycle.stop_workers([process])
        process.terminate.assert_called_once_with()
        process.terminate.assert_called_once_with()
        process.wait.assert_called()
        self.assertEqual(process.wait.call_count, 2)

    def test_stop_workers_reaps_even_when_terminate_raises(self):
        process = MagicMock()
        process.poll.return_value = None
        process.terminate.side_effect = OSError("terminate failed")
        process.wait.side_effect = [swap_cycle.subprocess.TimeoutExpired("worker", 2), 0]
        swap_cycle.stop_workers([process])
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_ready_handshake_has_bounded_timeout(self):
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(swap_cycle, "sys") as sysmock, \
                patch.object(swap_cycle, "swap_stats", return_value=(100, 0)), \
                patch.object(swap_cycle.subprocess, "Popen", return_value=process), \
                patch.object(swap_cycle, "select") as select_mock, \
                patch.object(swap_cycle, "os") as osmock:
            sysmock.platform = "linux"
            sysmock.executable = "/python"
            sysmock.argv = ["swap-cycle"]
            sysmock.path = swap_cycle.sys.path
            osmock.pipe.return_value = (10, 11)
            osmock.close = MagicMock()
            select_mock.select.return_value = ([], [], [])
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                swap_cycle.supervise()
        select_mock.select.assert_called_once_with([10], [], [],
                                                   swap_cycle.WORKER_READY_TIMEOUT_SECONDS)
        process.terminate.assert_called_once_with()
        process.wait.assert_called()

    def test_waits_full_minute_before_worker_two_then_recycles_at_threshold(self):
        processes = [MagicMock() for _ in range(3)]
        for process in processes:
            process.poll.return_value = None
        stats = [(100, 0)] * 61 + [(100, 50)]
        elapsed = [0]
        spawn_times = []

        def record_spawn(*args, **kwargs):
            spawn_times.append(elapsed[0])
            return processes[len(spawn_times) - 1]

        def advance(seconds):
            elapsed[0] += seconds

        with patch.object(swap_cycle, "SWAP_THRESHOLD_PERCENT", 50), \
                patch.object(swap_cycle, "REPLACEMENT_DELAY_SECONDS", 60), \
                patch.object(swap_cycle, "sys") as sysmock, \
                patch.object(swap_cycle, "swap_stats", side_effect=stats), \
                patch.object(swap_cycle.subprocess, "Popen", side_effect=record_spawn) as popen, \
                patch.object(swap_cycle, "select") as select_mock, \
                patch.object(swap_cycle, "os") as osmock, \
                patch.object(swap_cycle.time, "sleep", side_effect=advance) as sleep:
            sysmock.platform = "linux"
            sysmock.executable = "/python"
            sysmock.argv = ["swap-cycle"]
            sysmock.path = swap_cycle.sys.path
            osmock.pipe.side_effect = [(10, 11), (12, 13)]
            osmock.close = MagicMock()
            osmock.read.return_value = b"R"
            select_mock.select.return_value = ([10], [], [])
            # Interrupt during recycle wait after confirming the transition.
            sleep.side_effect = lambda seconds: (
                advance(seconds) if seconds != swap_cycle.REPLACEMENT_DELAY_SECONDS
                else (_ for _ in ()).throw(KeyboardInterrupt())
            )
            with self.assertRaises(KeyboardInterrupt):
                swap_cycle.supervise()
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(spawn_times, [0, 60])
        self.assertEqual(sleep.call_args_list[:60], [unittest.mock.call(1)] * 60)
        self.assertGreaterEqual(processes[0].terminate.call_count, 1)
        self.assertEqual(sleep.call_args_list[60], unittest.mock.call(1))
        self.assertEqual(sleep.call_args_list[61], unittest.mock.call(60))

    def test_swap_meminfo_parsing(self):
        total, used = swap_cycle.parse_swap_meminfo(
            "MemTotal: 1000 kB\nSwapTotal: 500 kB\nSwapFree: 125 kB\n")
        self.assertEqual((total, used), (500 * 1024, 375 * 1024))

    def test_swap_meminfo_rejects_missing_or_disabled_swap(self):
        for contents in ("SwapTotal: 0 kB\nSwapFree: 0 kB\n", "MemTotal: 1 kB\n"):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                swap_cycle.parse_swap_meminfo(contents)

    def test_threshold_is_inclusive_and_integer_exact(self):
        self.assertFalse(swap_cycle.reached_threshold(1000, 499, 50))
        self.assertTrue(swap_cycle.reached_threshold(1000, 500, 50))
        self.assertTrue(swap_cycle.reached_threshold(3, 2, 50))


if __name__ == "__main__":
    unittest.main()
