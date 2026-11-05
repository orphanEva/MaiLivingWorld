"""Foreground reply reserve shares one atomic, persistent model call counter."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from MaiLivingWorld.self_state import SelfState


class ReplyBudgetContract:
    def reserve(self, store=None, **changes):
        options = dict(now=1100, day_start=1000, min_interval_seconds=0, daily_limit=10,
                       reserve_for_reply=3, purpose="background")
        options.update(changes)
        return (store or self.db).reserve_thought(**options)

    def test_background_stops_at_total_less_reserve_foreground_uses_remainder(self):
        self.assertEqual(sum(self.reserve() for _ in range(12)), 7)
        self.assertEqual(self.db.state()["thought_count"], 7)
        self.assertEqual(sum(self.reserve(purpose="foreground") for _ in range(5)), 3)
        self.assertEqual(self.db.state()["thought_count"], 10)
        self.assertFalse(self.reserve())

    def test_foreground_consumption_also_counts_toward_background_ceiling(self):
        self.assertTrue(all(self.reserve(purpose="foreground") for _ in range(4)))
        self.assertEqual(sum(self.reserve() for _ in range(10)), 3)
        self.assertEqual(sum(self.reserve(purpose="foreground") for _ in range(10)), 3)
        self.assertEqual(self.db.state()["thought_count"], 10)

    def test_small_limit_all_reserved_disables_background_without_creating_identity(self):
        for total in (0, 1, 3, 12):
            self.assertFalse(self.reserve(daily_limit=total, reserve_for_reply=12))
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM self_identity").fetchone()[0], 0)
        self.assertTrue(self.reserve(daily_limit=1, reserve_for_reply=12, purpose="foreground"))
        self.assertFalse(self.reserve(daily_limit=1, reserve_for_reply=12, purpose="foreground"))

    def test_default_contract_keeps_full_limit_and_cooldown(self):
        options = dict(day_start=1000, min_interval_seconds=60, daily_limit=2)
        self.assertTrue(self.db.reserve_thought(now=1100, **options))
        self.assertFalse(self.db.reserve_thought(now=1159, **options))
        self.assertTrue(self.db.reserve_thought(now=1160, **options))
        self.assertFalse(self.db.reserve_thought(now=1220, **options))

    def test_reserve_and_counts_survive_reopen_and_owner_change(self):
        self.assertEqual(sum(self.reserve() for _ in range(8)), 7)
        identity = self.db.configure_owner("12345678", now=1200)
        self.db.close()
        self.db = self.new_store()
        self.assertFalse(self.reserve(now=1201))
        self.assertEqual(self.db.configure_owner("87654321", now=1202)["agent_id"], identity["agent_id"])
        self.assertEqual(self.db.state()["thought_count"], 7)
        self.assertTrue(self.reserve(now=1203, purpose="foreground"))

    def test_new_day_reset_does_not_allow_stale_day_or_skip_cooldown(self):
        self.assertTrue(self.reserve(now=1100, min_interval_seconds=60, daily_limit=2, reserve_for_reply=1))
        self.assertFalse(self.reserve(now=1101, purpose="foreground", min_interval_seconds=60))
        self.assertTrue(self.reserve(now=1160, purpose="foreground", min_interval_seconds=60, daily_limit=2))
        self.assertTrue(self.reserve(now=87500, day_start=87400, daily_limit=2, reserve_for_reply=1))
        self.assertEqual(self.db.state()["thought_count"], 1)
        self.assertFalse(self.reserve(now=87501, day_start=1000, purpose="foreground"))
        self.assertEqual(self.db.state()["budget_day"], 87400)

    def test_invalid_reserve_and_purpose_never_mutate_budget(self):
        self.assertTrue(self.reserve())
        before = self.db.state()
        for value in (-1, True, 1.5, "1", None):
            with self.subTest(reserve=value), self.assertRaises(ValueError):
                self.reserve(reserve_for_reply=value)
        for value in (None, [], {}, True, "Foreground", "unknown"):
            with self.subTest(purpose=value), self.assertRaises(ValueError):
                self.reserve(purpose=value)
        self.assertEqual(self.db.state(), before)

    def test_persistent_connections_compete_across_days_without_overusing_reserve(self):
        # Create schema once, then retain three separate connections across days.
        barrier = threading.Barrier(3)
        def worker(_):
            store = self.new_store()
            try:
                counts = []
                for day in (1000, 87400, 173800):
                    barrier.wait(timeout=15)
                    counts.append(sum(self.reserve(store, now=day+100, day_start=day,
                        daily_limit=9, reserve_for_reply=4) for _ in range(9)))
                    barrier.wait(timeout=15)
                return counts
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=3) as pool:
            counts = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in counts) for i in range(3)], [5, 5, 5])
        self.assertEqual(sum(self.reserve(now=173900, day_start=173800, daily_limit=9,
                         reserve_for_reply=4, purpose="foreground") for _ in range(6)), 4)
        self.assertEqual(self.db.state()["thought_count"], 9)

    def test_mixed_foreground_background_competition_shares_atomic_total(self):
        barrier = threading.Barrier(3)
        def worker(purpose):
            store = self.new_store()
            try:
                barrier.wait(timeout=15)
                return purpose, sum(self.reserve(store, daily_limit=12, reserve_for_reply=4,
                                                 purpose=purpose) for _ in range(20))
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, ("foreground", "background", "background")))
        foreground = sum(count for purpose, count in results if purpose == "foreground")
        background = sum(count for purpose, count in results if purpose == "background")
        self.assertEqual(foreground+background, 12)
        self.assertLessEqual(background, 8)
        self.assertGreaterEqual(foreground, 4)
        self.assertEqual(self.db.state()["thought_count"], 12)


class ReplyBudgetSQLiteTests(ReplyBudgetContract, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"self.sqlite3"
        self.db = self.new_store()

    def new_store(self):
        return SelfState(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()


if __name__ == "__main__":
    unittest.main()
