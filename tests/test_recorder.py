# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from traser_sdk import Traser


class RecorderTests(unittest.TestCase):
    def test_retry_operation_and_explicit_evidence(self):
        with Traser().run("refund", run_id="run-1") as run:
            run.context(expected_outcome="refund completed", actual_outcome="pending")
            with run.operation("issue_refund", operation_id="refund-42",
                               idempotency_key="idem-42", side_effect_target="payment-42") as op:
                self.assertEqual(op.call(lambda **kw: {"status": "pending"}, attempt=1, x=2), {"status": "pending"})
                op.call(lambda: {"status": "pending"}, attempt=2)
                op.observe(kind="write_receipt", source_kind="api", locator="refund/r-42",
                           value={"status": "pending"})
                op.require(kind="terminal_state", required_for="refund completed")
            run.record_step("Report to customer", kind="agent", status="success",
                            output={"message": "Refund completed"})
        request = run.to_request()
        steps = request["execution"]["steps"]
        self.assertEqual(request["execution"]["schemaVersion"], "0.1")
        self.assertEqual(request["execution"]["status"], "success")
        self.assertEqual([s["sequence"] for s in steps], [0, 1, 2])
        self.assertEqual([s["attempt"] for s in steps[:2]], [1, 2])
        self.assertEqual([s["retryCount"] for s in steps[:2]], [0, 1])
        self.assertEqual(steps[0]["operationId"], steps[1]["operationId"])
        self.assertNotEqual(steps[0]["attemptId"], steps[1]["attemptId"])
        self.assertEqual(steps[0]["idempotencyKey"], "idem-42")
        evidence = request["evidence"]
        self.assertEqual(evidence["origin"], "recorded")
        self.assertEqual(evidence["coverage"], "partial")
        self.assertEqual(evidence["observations"][0]["stepId"], steps[1]["id"])
        self.assertEqual(evidence["requirements"][0]["stepId"], steps[1]["id"])
        self.assertIn(steps[1]["id"], evidence["sources"][1]["stepIds"])
        self.assertEqual(request["context"]["expectedOutcome"], "refund completed")
        self.assertNotIn("postconditionVerified", json.dumps(evidence))

    def test_exception_is_rethrown_without_leaking_message(self):
        with Traser().run("exceptions") as run:
            with run.operation("broken") as op:
                with self.assertRaisesRegex(RuntimeError, "secret-123"):
                    op.call(lambda: (_ for _ in ()).throw(RuntimeError("secret-123")))
        step = run.to_run()["steps"][0]
        self.assertEqual(step["status"], "failure")
        self.assertEqual(step["error"], {"type": "RuntimeError"})
        self.assertNotIn("secret-123", json.dumps(run.to_request()))

    def test_exception_outside_call_marks_run_failed(self):
        run = Traser().run("outer")
        with self.assertRaises(ValueError):
            with run:
                raise ValueError("error")
        self.assertEqual(run.to_run()["status"], "failure")

    def test_secret_key_redaction_and_unsupported_capture_are_explicit(self):
        with Traser().run("secrets") as run:
            with run.operation("tool") as op:
                op.call(lambda x: {"authorization": "Bearer secret", "status": "ok"},
                        {"api_key": "secret", "safe": 1})
                op.call(lambda: object(), attempt=2)
        steps = run.to_run()["steps"]
        self.assertEqual(steps[0]["input"]["args"][0]["api_key"], "[REDACTED]")
        self.assertEqual(steps[0]["output"]["authorization"], "[REDACTED]")
        self.assertIn("sensitive_fields_redacted", str(steps[0]["attributes"]))
        self.assertNotIn("output", steps[1])
        self.assertIn("value_omitted", str(steps[1]["attributes"]))
        self.assertEqual(run.to_request()["evidence"]["coverage"], "partial")

    def test_async_capture_and_error(self):
        async def fetch(x):
            await asyncio.sleep(0)
            return {"result": x}
        async def crash():
            raise LookupError("missing")
        async def task():
            with Traser().run("async") as run:
                with run.operation("fetch") as op:
                    self.assertEqual(await op.acall(fetch, 3), {"result": 3})
                    with self.assertRaises(LookupError):
                        await op.acall(crash, attempt=2)
            return run
        steps = asyncio.run(task()).to_run()["steps"]
        self.assertEqual([s["status"] for s in steps], ["success", "failure"])
        self.assertEqual([s["attempt"] for s in steps], [1, 2])

    def test_export_is_private_no_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Traser().run("export") as run:
                run.record_step("test", kind="tool", status="success", output={"ok": True})
            path = Path(tmp) / "run.json"
            request = Path(tmp) / "request.json"
            run.export_run(path)
            run.export_request(request)
            with self.assertRaises(FileExistsError):
                run.export_run(path)
            self.assertEqual(json.loads(path.read_text())["schemaVersion"], "0.1")
            self.assertIn("evidence", json.loads(request.read_text()))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_unknown_observation_is_rejected_and_missing_requirement_remains(self):
        with Traser().run("gaps") as run:
            with run.operation("send") as op:
                op.call(lambda: None)
                with self.assertRaises(ValueError):
                    op.observe(kind="terminal_state", source_kind="api", locator="job/4",
                               value=object())
                op.require(kind="terminal_state", required_for="job finished")
        evidence = run.to_request()["evidence"]
        self.assertEqual(len(evidence["observations"]), 0)
        self.assertEqual(len(evidence["requirements"]), 1)

    def test_limits_and_timestamps(self):
        with self.assertRaises(ValueError):
            Traser(max_value_bytes=0)
        with Traser(max_value_bytes=20).run("small") as run:
            with run.operation("tool") as op:
                op.call(lambda: "x" * 25)
                with self.assertRaises(ValueError):
                    op.require(kind="terminal_state", required_for="done", consistency_deadline="yesterday")
                with self.assertRaises(ValueError):
                    op.observe(kind="write_receipt", source_kind="api", locator="api/1", value="x" * 25)
        self.assertNotIn("output", run.to_run()["steps"][0])


    def test_verification_provenance_marks_tool_response_as_insufficient(self):
        with Traser().run("receipt-only") as run:
            with run.operation("issue_refund") as op:
                op.call(lambda: {"status": "pending"})
                claim = run.record_step("verify from receipt", kind="evaluator",
                                        status="success", output={"complete": True})
                op.mark_verification(claim, basis="tool_response")
        check = run.to_request()["evidence"]["verificationChecks"][0]
        self.assertEqual(check["basis"], "tool_response")
        self.assertEqual(check["claimStepId"], claim)
        self.assertEqual(check["operationStepId"], run.to_run()["steps"][0]["id"])
        self.assertNotIn("observationId", check)

    def test_authorized_external_read_records_state_mismatch_and_match(self):
        calls = []
        def read_provider(refund_id):
            calls.append(refund_id)
            return {"data": {"status": "pending" if len(calls) == 1 else "completed"}}
        with Traser().run("verify refund") as run:
            with run.operation("issue_refund") as op:
                op.call(lambda: {"refund_id": "r42", "status": "accepted"})
                self.assertFalse(op.verify_external(
                    read_provider, "r42", source_kind="api",
                    locator="provider/refunds/r42", expected_path="data.status",
                    expected_value="completed"))
                self.assertTrue(op.verify_external(
                    read_provider, "r42", source_kind="api",
                    locator="provider/refunds/r42", expected_path="data.status",
                    expected_value="completed"))
        self.assertEqual(calls, ["r42", "r42"])
        request = run.to_request()
        checks = request["evidence"]["verificationChecks"]
        self.assertEqual([x["basis"] for x in checks], ["external_state", "external_state"])
        self.assertEqual([x["expectedValue"] for x in checks], ["completed", "completed"])
        self.assertEqual([x["observedValuePath"] for x in checks], ["data.status", "data.status"])
        self.assertEqual(len(request["evidence"]["observations"]), 2)
        self.assertEqual(len(request["execution"]["steps"]), 3)
        self.assertEqual([s["output"]["comparison"] for s in request["execution"]["steps"][1:]],
                         ["mismatched", "matched"])
        self.assertTrue(all(check["operationStepId"] == request["execution"]["steps"][0]["id"]
                            for check in checks))
        self.assertNotIn("postconditionVerified", json.dumps(request))

    def test_external_read_missing_field_and_type_safety(self):
        with Traser().run("verify") as run:
            with run.operation("create") as op:
                op.call(lambda: {"ok": True})
                self.assertFalse(op.verify_external(
                    lambda: {"other": 1}, source_kind="database", locator="orders/42",
                    expected_path="status", expected_value="completed"))
                self.assertFalse(op.verify_external(
                    lambda: {"status": 1}, source_kind="database", locator="orders/42",
                    expected_path="status", expected_value=True))
        steps = run.to_run()["steps"]
        self.assertEqual([s["output"]["comparison"] for s in steps[1:]],
                         ["field_missing", "mismatched"])

    def test_external_read_errors_do_not_claim_verification(self):
        def broken():
            raise RuntimeError("private-provider-token")
        with Traser().run("read failure") as run:
            with run.operation("issue_refund") as op:
                op.call(lambda: {"status": "accepted"})
                with self.assertRaises(RuntimeError):
                    op.verify_external(broken, source_kind="api", locator="provider/r42",
                                       expected_path="status", expected_value="completed")
                touched = []
                with self.assertRaises(ValueError):
                    op.verify_external(lambda: touched.append(True), source_kind="trace",
                                       locator="provider/r42", expected_path="status",
                                       expected_value="completed")
                self.assertEqual(touched, [])
        request = run.to_request()
        self.assertEqual(request["execution"]["steps"][1]["status"], "failure")
        self.assertEqual(request["execution"]["steps"][1]["error"], {"type": "RuntimeError"})
        self.assertNotIn("verificationChecks", request["evidence"])
        self.assertNotIn("private-provider-token", json.dumps(request))

    def test_async_external_state_verification(self):
        async def provider(refund_id):
            await asyncio.sleep(0)
            return {"status": "completed", "refund_id": refund_id}
        async def run_test():
            with Traser().run("async provider") as run:
                with run.operation("issue_refund") as op:
                    op.call(lambda: {"status": "accepted"})
                    self.assertTrue(await op.averify_external(
                        provider, "r42", source_kind="api", locator="provider/r42",
                        expected_path="status", expected_value="completed"))
            return run.to_request()
        request = asyncio.run(run_test())
        self.assertEqual(request["evidence"]["observations"][0]["value"]["status"], "completed")
        self.assertEqual(request["evidence"]["verificationChecks"][0]["basis"], "external_state")


if __name__ == "__main__":
    unittest.main()
