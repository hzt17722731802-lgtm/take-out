"""Acceptance tests for the SQLite task queue.

Run with::

    python3 -m unittest discover -s benchmarks/task_queue -p 'test_*.py' -v
"""

import os
import sqlite3
import tempfile
import threading
import unittest

from task_queue import Claim, TaskQueue


class TaskQueueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "tasks.db")
        self.queue = TaskQueue(self.db_path)

    def tearDown(self):
        self.queue.close()
        self._tmp.cleanup()

    def task_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()

    # -- enqueue / deduplication ---------------------------------------

    def test_same_key_and_payload_is_idempotent(self):
        first = self.queue.enqueue("order-1", {"order": 1, "shop": 2})
        second = self.queue.enqueue("order-1", {"shop": 2, "order": 1})

        self.assertEqual(first, second)
        self.assertEqual(self.task_count(), 1)
        self.assertEqual(self.queue.get(first)["status"], "pending")
        self.assertEqual(self.queue.get(first)["attempts"], 0)

    def test_same_key_different_payload_raises(self):
        self.queue.enqueue("order-1", {"order": 1})

        with self.assertRaises(ValueError):
            self.queue.enqueue("order-1", {"order": 2})
        with self.assertRaises(ValueError):
            self.queue.enqueue("order-1", {"order": 1, "extra": True})

        self.assertEqual(self.task_count(), 1)

    def test_different_keys_create_different_tasks(self):
        first = self.queue.enqueue("order-1", {"order": 1})
        second = self.queue.enqueue("order-2", {"order": 1})

        self.assertNotEqual(first, second)
        self.assertEqual(self.task_count(), 2)

    def test_dedup_after_completion_returns_original_id(self):
        task_id = self.queue.enqueue("order-1", {"order": 1})
        claim = self.queue.claim("worker-a", now=0, lease_seconds=10)
        self.assertTrue(self.queue.ack(task_id, claim.lease_token, now=1))

        self.assertEqual(self.queue.enqueue("order-1", {"order": 1}), task_id)
        self.assertEqual(self.queue.get(task_id)["status"], "done")

    def test_enqueue_validation(self):
        with self.assertRaises(ValueError):
            self.queue.enqueue("bad", {}, max_attempts=0)
        with self.assertRaises(ValueError):
            self.queue.enqueue("bad", {}, max_attempts=-1)
        with self.assertRaises(TypeError):
            self.queue.enqueue("bad", object())

    # -- claim ---------------------------------------------------------

    def test_claim_returns_none_when_empty(self):
        self.assertIsNone(self.queue.claim("worker-a", now=0, lease_seconds=10))

    def test_claim_returns_task_payload_and_token(self):
        task_id = self.queue.enqueue("order-1", {"lines": [1, 2]})
        claim = self.queue.claim("worker-a", now=100, lease_seconds=30)

        self.assertIsInstance(claim, Claim)
        self.assertEqual(claim.task_id, task_id)
        self.assertEqual(claim.payload, {"lines": [1, 2]})
        self.assertTrue(claim.lease_token)
        self.assertEqual(claim["task_id"], task_id)
        self.assertEqual(claim[0], task_id)
        claimed_id, payload, token = claim
        self.assertEqual(
            (claimed_id, payload, token),
            (task_id, claim.payload, claim.lease_token),
        )

        record = self.queue.get(task_id)
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["payload"], {"lines": [1, 2]})
        self.assertEqual(record["worker_id"], "worker-a")

    def test_claim_is_ordered_by_submission(self):
        first = self.queue.enqueue("first", {"n": 1})
        second = self.queue.enqueue("second", {"n": 2})

        claim = self.queue.claim("worker-a", now=0, lease_seconds=10)
        self.assertEqual(claim.task_id, first)
        self.assertTrue(self.queue.ack(first, claim.lease_token, now=1))

        claim = self.queue.claim("worker-a", now=2, lease_seconds=10)
        self.assertEqual(claim.task_id, second)

    def test_claim_twice_does_not_hand_out_live_task(self):
        task_id = self.queue.enqueue("order-1", {})
        first = self.queue.claim("worker-a", now=0, lease_seconds=10)

        second = self.queue.claim("worker-b", now=5, lease_seconds=10)

        self.assertIsNone(second)
        record = self.queue.get(task_id)
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(self.queue.ack(task_id, first.lease_token, now=6))

    def test_claim_validation(self):
        self.queue.enqueue("order-1", {})
        with self.assertRaises(ValueError):
            self.queue.claim("worker-a", now=0, lease_seconds=0)
        with self.assertRaises(ValueError):
            self.queue.claim("worker-a", now=0, lease_seconds=-5)
        with self.assertRaises(TypeError):
            self.queue.claim("worker-a", now="later", lease_seconds=10)

    # -- ack -----------------------------------------------------------

    def test_ack_requires_current_lease(self):
        task_id = self.queue.enqueue("order-1", {})
        claim = self.queue.claim("worker-a", now=0, lease_seconds=10)

        self.assertFalse(self.queue.ack(task_id, "not-the-token", now=1))
        self.assertEqual(self.queue.get(task_id)["status"], "running")

        self.assertTrue(self.queue.ack(task_id, claim.lease_token, now=2))
        self.assertEqual(self.queue.get(task_id)["status"], "done")
        self.assertEqual(self.queue.get(task_id)["attempts"], 1)

    def test_repeated_ack_and_fail_return_false(self):
        task_id = self.queue.enqueue("order-1", {})
        claim = self.queue.claim("worker-a", now=0, lease_seconds=10)
        self.assertTrue(self.queue.ack(task_id, claim.lease_token, now=1))

        self.assertFalse(self.queue.ack(task_id, claim.lease_token, now=2))
        self.assertFalse(self.queue.fail(task_id, claim.lease_token, now=2))
        self.assertEqual(self.queue.get(task_id)["status"], "done")
        self.assertIsNone(self.queue.claim("worker-b", now=100, lease_seconds=10))

    def test_ack_with_expired_token_does_not_change_state(self):
        task_id = self.queue.enqueue("order-1", {})
        claim = self.queue.claim("worker-a", now=0, lease_seconds=5)

        self.assertFalse(self.queue.ack(task_id, claim.lease_token, now=5))
        self.assertFalse(self.queue.fail(task_id, claim.lease_token, now=5))
        record = self.queue.get(task_id)
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["attempts"], 1)

    def test_ack_unknown_task_returns_false(self):
        self.assertFalse(self.queue.ack("missing", "token", now=0))
        self.assertFalse(self.queue.fail("missing", "token", now=0))

    # -- fail / retries ------------------------------------------------

    def test_fail_requeues_and_generates_new_token(self):
        task_id = self.queue.enqueue("order-1", {})
        first = self.queue.claim("worker-a", now=0, lease_seconds=10)
        self.assertTrue(self.queue.fail(task_id, first.lease_token, now=1))

        self.assertFalse(self.queue.ack(task_id, first.lease_token, now=2))
        self.assertEqual(self.queue.get(task_id)["status"], "pending")

        second = self.queue.claim("worker-b", now=3, lease_seconds=10)
        self.assertIsNotNone(second)
        self.assertEqual(second.task_id, task_id)
        self.assertNotEqual(second.lease_token, first.lease_token)
        self.assertEqual(self.queue.get(task_id)["attempts"], 2)
        self.assertTrue(self.queue.ack(task_id, second.lease_token, now=4))

    def test_task_fails_permanently_after_max_attempts(self):
        task_id = self.queue.enqueue("order-1", {}, max_attempts=3)

        for attempt in range(3):
            claim = self.queue.claim(
                f"worker-{attempt}", now=attempt * 10, lease_seconds=5
            )
            self.assertIsNotNone(claim, f"attempt {attempt} should be claimable")
            self.assertTrue(
                self.queue.fail(task_id, claim.lease_token, now=attempt * 10 + 1)
            )

        self.assertIsNone(self.queue.claim("worker-x", now=100, lease_seconds=5))
        record = self.queue.get(task_id)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"], 3)
        self.assertFalse(
            self.queue.fail(task_id, "stale-token", now=101)
        )

    # -- lease expiry --------------------------------------------------

    def test_expired_lease_can_be_reclaimed_and_old_token_is_rejected(self):
        task_id = self.queue.enqueue("order-1", {}, max_attempts=3)
        first = self.queue.claim("worker-a", now=0, lease_seconds=10)

        # Exactly at the expiry instant the lease is already expired.
        second = self.queue.claim("worker-b", now=10, lease_seconds=10)

        self.assertIsNotNone(second)
        self.assertEqual(second.task_id, task_id)
        self.assertNotEqual(second.lease_token, first.lease_token)
        self.assertFalse(self.queue.ack(task_id, first.lease_token, now=11))
        self.assertFalse(self.queue.fail(task_id, first.lease_token, now=11))

        record = self.queue.get(task_id)
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(record["worker_id"], "worker-b")
        self.assertTrue(self.queue.ack(task_id, second.lease_token, now=12))

    def test_expired_lease_on_last_attempt_marks_failed(self):
        task_id = self.queue.enqueue("order-1", {}, max_attempts=1)
        first = self.queue.claim("worker-a", now=0, lease_seconds=5)
        self.assertIsNotNone(first)

        self.assertIsNone(self.queue.claim("worker-b", now=5, lease_seconds=5))
        record = self.queue.get(task_id)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"], 1)
        self.assertFalse(self.queue.ack(task_id, first.lease_token, now=5))

    def test_reclaim_generates_a_new_token_each_time(self):
        self.queue.enqueue("order-1", {})
        tokens = []
        for step in range(3):
            claim = self.queue.claim(
                f"worker-{step}", now=step * 10, lease_seconds=5
            )
            self.assertIsNotNone(claim)
            tokens.append(claim.lease_token)
            # Let the lease expire instead of acking.
        self.assertEqual(len(tokens), len(set(tokens)))

    # -- concurrency ---------------------------------------------------

    def test_ten_workers_race_for_one_task(self):
        task_id = self.queue.enqueue("order-1", {"n": 1}, max_attempts=5)
        worker_count = 10
        barrier = threading.Barrier(worker_count)
        results = []
        errors = []
        results_lock = threading.Lock()

        def worker(index):
            queue = None
            try:
                queue = TaskQueue(self.db_path)
                barrier.wait(timeout=10)
                claim = queue.claim(
                    f"worker-{index}", now=100, lease_seconds=30
                )
                with results_lock:
                    results.append(claim)
            except BaseException as exc:  # pragma: no cover - failure path
                with results_lock:
                    errors.append(exc)
            finally:
                if queue is not None:
                    queue.close()

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertFalse(
            any(thread.is_alive() for thread in threads), "workers did not finish"
        )
        self.assertEqual(errors, [])
        winners = [claim for claim in results if claim is not None]
        self.assertEqual(len(results), worker_count)
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].task_id, task_id)

        record = self.queue.get(task_id)
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(record["worker_id"].startswith("worker-"))

    # -- persistence ---------------------------------------------------

    def test_completed_task_survives_reopen(self):
        task_id = self.queue.enqueue("order-1", {"order": 1})
        claim = self.queue.claim("worker-a", now=0, lease_seconds=10)
        self.assertTrue(self.queue.ack(task_id, claim.lease_token, now=1))
        self.queue.close()

        reopened = TaskQueue(self.db_path)
        self.addCleanup(reopened.close)

        record = reopened.get(task_id)
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["attempts"], 1)
        self.assertIsNone(reopened.claim("worker-b", now=2, lease_seconds=10))
        self.assertEqual(reopened.enqueue("order-1", {"order": 1}), task_id)

    def test_pending_and_running_tasks_survive_reopen(self):
        pending_id = self.queue.enqueue("pending", {"n": 1})
        running_id = self.queue.enqueue("running", {"n": 2})
        claim = self.queue.claim("worker-a", now=0, lease_seconds=10)
        self.assertEqual(claim.task_id, pending_id)
        self.queue.close()

        reopened = TaskQueue(self.db_path)
        self.addCleanup(reopened.close)

        self.assertEqual(reopened.get(pending_id)["status"], "running")
        self.assertEqual(reopened.get(running_id)["status"], "pending")
        # The running lease is still valid at now=5, so only "running" is left.
        next_claim = reopened.claim("worker-b", now=5, lease_seconds=10)
        self.assertEqual(next_claim.task_id, running_id)

    def test_get_unknown_task_returns_none(self):
        self.assertIsNone(self.queue.get("does-not-exist"))

    def test_get_supports_mapping_and_attribute_access(self):
        task_id = self.queue.enqueue("order-1", {"order": 1})
        record = self.queue.get(task_id)

        self.assertEqual(record["status"], "pending")
        self.assertEqual(record.status, "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record.attempts, 0)

    def test_now_accepts_datetime(self):
        import datetime

        task_id = self.queue.enqueue("order-1", {})
        claimed_at = datetime.datetime(2026, 1, 1, 12, 0, 0)
        claim = self.queue.claim("worker-a", now=claimed_at, lease_seconds=30)
        self.assertIsNotNone(claim)

        self.assertTrue(
            self.queue.ack(
                task_id,
                claim.lease_token,
                now=claimed_at + datetime.timedelta(seconds=1),
            )
        )
        self.assertEqual(self.queue.get(task_id)["status"], "done")

    def test_shared_queue_instance_is_thread_safe(self):
        task_id = self.queue.enqueue("order-1", {})
        worker_count = 10
        barrier = threading.Barrier(worker_count)
        results = []
        results_lock = threading.Lock()

        def worker(index):
            barrier.wait(timeout=10)
            claim = self.queue.claim(f"worker-{index}", now=0, lease_seconds=10)
            with results_lock:
                results.append(claim)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        winners = [claim for claim in results if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].task_id, task_id)
        self.assertEqual(self.queue.get(task_id)["attempts"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
