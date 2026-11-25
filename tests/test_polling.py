# SPDX-License-Identifier: Apache-2.0
"""Synthetic tests for opt-in external state polling."""

import asyncio
import json
import unittest

from traser_sdk import PollResult, Traser


class PollingTests(unittest.TestCase):
    def test_sync_delayed_match_keeps_all_observations(self):
        states = iter(["pending", "pending", "completed"])
        with Traser().run("delayed-update") as run:
            with run.operation("update_record", operation_id="update-42") as op:
                op.call(lambda: {"http_status": 200})
                result = op.poll_external(
                    lambda: {"data": {"status": next(states)}},
                    source_kind="api", locator="records/42",
                    expected_path="data.status", expected_value="completed",
                    timeout_s=2, interval_s=0.001, max_attempts=5)
        self.assertIsInstance(result, PollResult)
        self.assertEqual(result.status, "observed_match")
        self.assertTrue(result.matched)
        self.assertEqual(result.attempts, 3)
        request = run.to_request()
        self.assertEqual([o["value"]["data"]["status"] for o in
                          request["evidence"]["observations"]],
                         ["pending", "pending", "completed"])
        self.assertEqual(len(request["evidence"]["verificationChecks"]), 3)
        self.assertEqual(len(request["execution"]["steps"]), 5)
        self.assertEqual(len({s["id"] for s in request["execution"]["steps"]}), 5)
        self.assertEqual(len({s["operationId"] for s in request["execution"]["steps"][:-1]}), 1)
        self.assertEqual(request["execution"]["steps"][-1]["output"]["pollStatus"], "observed_match")

    def test_max_attempts_is_unconfirmed_not_failed(self):
        with Traser().run("still-pending") as run:
            with run.operation("write") as op:
                op.call(lambda: {"status": "accepted"})
                result = op.poll_external(
                    lambda: {"status": "pending"}, source_kind="database",
                    locator="record/1", expected_path="status",
                    expected_value="completed", timeout_s=2,
                    interval_s=0.001, max_attempts=2)
        self.assertEqual(result.status, "unconfirmed_attempt_limit")
        self.assertFalse(result.matched)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(run.to_request()["evidence"]["observations"]), 2)
        self.assertNotIn("postconditionVerified", json.dumps(run.to_request()))

    def test_timeout_is_unconfirmed_after_one_read(self):
        with Traser().run("pending-write") as run:
            with run.operation("write") as op:
                op.call(lambda: {"status": "accepted"})
                result = op.poll_external(
                    lambda: {"status": "pending"}, source_kind="api",
                    locator="record/2", expected_path="status",
                    expected_value="completed", timeout_s=0.001,
                    interval_s=1.0, max_attempts=5)
        self.assertEqual(result.status, "unconfirmed_timeout")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(run.to_request()["evidence"]["verificationChecks"]), 1)

    def test_invalid_limits_reject_before_any_reads(self):
        calls = []
        with Traser().run("invalid-config") as run:
            with run.operation("write") as op:
                op.call(lambda: {"accepted": True})
                for options in [
                    {"timeout_s": 0}, {"interval_s": float("inf")},
                    {"max_attempts": 0}, {"max_attempts": 51},
                    {"max_attempts": True}
                ]:
                    with self.assertRaises(ValueError):
                        op.poll_external(
                            lambda: calls.append(True) or {"status": "ok"},
                            source_kind="api", locator="record/2",
                            expected_path="status", expected_value="ok",
                            **options)
        self.assertEqual(calls, [])
        self.assertEqual(len(run.to_run()["steps"]), 1)

    def test_sync_reader_error_propagates_and_is_redacted(self):
        def broken():
            raise RuntimeError("private-api-token")
        with Traser().run("read-error") as run:
            with run.operation("write") as op:
                op.call(lambda: {"accepted": True})
                with self.assertRaises(RuntimeError):
                    op.poll_external(
                        broken, source_kind="api", locator="record/3",
                        expected_path="status", expected_value="ok",
                        timeout_s=1, interval_s=0.001)
        self.assertEqual(run.to_run()["steps"][1]["status"], "failure")
        self.assertNotIn("private-api-token", json.dumps(run.to_request()))

    def test_async_delayed_match_and_async_error(self):
        responses = iter(["pending", "completed"])

        async def reader():
            await asyncio.sleep(0)
            return {"status": next(responses)}

        async def run_poll():
            with Traser().run("async-update") as run:
                with run.operation("write") as op:
                    op.call(lambda: {"http_status": 200})
                    result = await op.apoll_external(
                        reader, source_kind="api", locator="records/42",
                        expected_path="status", expected_value="completed",
                        timeout_s=2, interval_s=0.001, max_attempts=4)
            return result, run

        result, run = asyncio.run(run_poll())
        self.assertEqual(result.status, "observed_match")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(run.to_request()["evidence"]["observations"]), 2)

    def test_async_timeout_does_not_claim_failure(self):
        async def reader():
            return {"status": "pending"}

        async def run_poll():
            with Traser().run("async-timeout") as run:
                with run.operation("write") as op:
                    op.call(lambda: {"accepted": True})
                    result = await op.apoll_external(
                        reader, source_kind="api", locator="records/43",
                        expected_path="status", expected_value="completed",
                        timeout_s=0.001, interval_s=1, max_attempts=4)
            return result, run

        result, run = asyncio.run(run_poll())
        self.assertEqual(result.status, "unconfirmed_timeout")
        self.assertFalse(result.matched)
        self.assertEqual(result.attempts, 1)
        self.assertNotIn("postconditionVerified", json.dumps(run.to_request()))


if __name__ == "__main__":
    unittest.main()
