# SPDX-License-Identifier: Apache-2.0
"""Synthetic, network-free pending-refund example."""

from pathlib import Path
from traser_sdk import Traser


def mock_refund_provider(amount: int, *, payment_id: str) -> dict:
    return {"refund_id": "r-demo-7", "amount": amount, "status": "pending", "payment_id": payment_id}


def main() -> None:
    traser = Traser()
    with traser.run("synthetic-refund-discrepancy") as run:
        run.record_step("Customer requests refund", kind="agent", status="success",
                        output={"requested": "refund payment demo-42"})
        with run.operation("issue_refund", operation_id="refund:demo-42",
                           idempotency_key="refund-demo-42") as operation:
            receipt = operation.call(mock_refund_provider, 50, payment_id="demo-42", attempt=1)
            operation.observe(kind="write_receipt", source_kind="api",
                              locator="synthetic-refund-provider:refund/r-demo-7", value=receipt)
            operation.require(kind="terminal_state",
                              required_for="refund r-demo-7 reaches completed state")
        run.record_step("Tell customer", kind="agent", status="success",
                        input_value={"refund_status": "pending"},
                        output={"message": "Your refund has been completed."})
        run.set_final_output({"message": "Your refund has been completed."})
        run.context(expected_outcome="Refund completed",
                    actual_outcome="Provider returned pending, terminal state not observed")

    out = Path(__file__).resolve().parent
    path_run = run.export_run(out / "refund-run.json", overwrite=True)
    path_request = run.export_request(out / "refund-request.json", overwrite=True)
    print(f"Canonical run: {path_run}")
    print(f"MCP request: {path_request}")
    print("Synthetic example only; not a recorded customer incident or proof of refund failure.")


if __name__ == "__main__":
    main()
