# SPDX-License-Identifier: Apache-2.0
"""Synthetic, network-free delayed external-write example.

Run with python examples/external_polling.py after installing this branch.
"""

from traser_sdk import Traser


def main() -> None:
    states = iter(["pending", "pending", "completed"])

    def read_customer():
        return {"address": {"zip": "60608" if next(states) == "completed" else "60607"}}

    with Traser().run("synthetic-delayed-address-update") as run:
        with run.operation("update_customer", operation_id="customer-42") as op:
            op.call(lambda: {"http_status": 200, "accepted": True})
            result = op.poll_external(
                read_customer, source_kind="api", locator="customers/42",
                expected_path="address.zip", expected_value="60608",
                timeout_s=3, interval_s=0.01, max_attempts=5)

    print("Poll status:", result.status)
    print("Attempts:", result.attempts)
    print("Observed states:", [
        item["value"]["address"]["zip"]
        for item in run.to_request()["evidence"]["observations"]
    ])
    print("Synthetic demonstration only; no real customer API was called.")


if __name__ == "__main__":
    main()
