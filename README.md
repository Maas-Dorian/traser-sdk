# Traser Python SDK v0.2.0 (development)

An explicit, local-first Python recorder for **actual** agent/tool execution evidence. It records tool boundaries and retry identity; developers can attach external observations and postcondition requirements. It exports the canonical Traser `AgentRun` v0.1 and a separate MCP 0.3-compatible request. The separate TypeScript engine performs investigations. This package never asserts root cause or independently authenticates external state. Developers may opt into their own authorized API/database read callbacks through the new verification helpers; there is no SDK-managed network client, background polling or cloud dependency.

**Status:** v0.1.0 is published on PyPI; the v0.2.0 verification helpers described here are in development and not yet released. The Python SDK is **Apache-2.0 licensed**; see [LICENSE](LICENSE) and [NOTICE](NOTICE). This repository contains only the Python SDK; the separate Traser analysis engine and npm MCP package are not relicensed.

## Local install and quickstart

```sh
python -m pip install -e .
python examples/refund.py
```

```python
from traser_sdk import Traser

traser = Traser()
with traser.run("customer-refund") as run:
    with run.operation("issue_refund", operation_id="refund-request-42") as op:
        response = op.call(lambda: {"refund_id": "r42", "status": "pending"}, attempt=1)
        op.observe(kind="write_receipt", source_kind="api", locator="refund-provider/r42",
                   value=response)
        op.require(kind="terminal_state", required_for="refund reaches completed state")
    run.record_step("Tell customer", kind="agent", status="success",
                    output={"message": "Your refund is complete"})
    run.context(expected_outcome="Refund completed", actual_outcome="Refund still pending")

run.export_run("refund-run.json")          # Canonical run for /investigate browser upload.
run.export_request("refund-request.json")  # Pass full object to MCP traser_investigate.
```

The SDK records **function returned** as step `success`, never **external operation completed**. A receipt or external read is evidence for an engineer to inspect, not authenticated proof the postcondition succeeded. Requirements are user-declared and MCP review keeps `postconditionVerified: false`.

## Verification integrity (new, opt-in)

A verification that reads back its own tool response is not an independent postcondition check. Explicitly annotate the actual verification step:

```python
with Traser().run("refund") as run:
    with run.operation("issue_refund") as op:
        receipt = op.call(lambda: {"refund_id": "r42", "status": "accepted"})
        claim = run.record_step("Verify refund", kind="evaluator", status="success",
                                input_value=receipt, output={"verified": True})
        op.mark_verification(claim, basis="tool_response")  # Not an external read!

        # This callback is supplied by YOUR application and must be read-only and authorized.
        def get_refund(refund_id):
            return {"status": "pending"}  # Synthetic API result for illustration.

        matches = op.verify_external(
            get_refund, "r42", source_kind="api", locator="provider/refunds/r42",
            expected_path="status", expected_value="completed")
        assert not matches
```

Use `await op.averify_external(async_reader, ...)` for async provider/database clients. The SDK calls the exact callback you supply once, records its response as a `post_write_state` observation, links it to the originating write step, and declares `evidence.verificationChecks`. It compares an explicit dot-separated JSON field against a scalar expected value. A `False` result means the supplied observation did not match or lacked the field; it does **not** establish permanent failure during an eventual-consistency window. It never retries, takes credentials, invents state, polls or executes a write by itself. Exceptions propagate and are recorded by **type only**.

The matching MCP investigation release will return `verificationReview` statuses, such as `self_report_only`, `external_state_mismatch` and `external_state_matches_expected_unverified`. An API/database source label is caller-supplied, not independently authenticated by Traser. The currently published MCP package `@maas-dorian/traser@0.3.1` does **not** accept these new `verificationChecks` fields; install a compatible MCP release before sending the new request format.

## Investigation workflow

- Browser: upload the `export_run()` canonical JSON to [Traser browser investigator](https://traser.dev/investigate).
- MCP: using the published `@maas-dorian/traser@0.3.1` server, call `traser_investigate` with the JSON object from `export_request()`, including `execution`, `evidence`, and optional `context`. The file is an MCP **argument object**, not a complete browser trace; `tracePath` expects `export_run()` instead. The SDK does not call MCP on your behalf.
- No comparison run is required. A reference run can be supplied separately, but is not ground truth.

## API and guarantees

- `with Traser().run(name, run_id=...) as run:` captures a bounded in-memory run. Without an exception, run `status=success` means local control flow completed, **not** that the agent achieved the requested real-world result. Use `run.set_status(...)` to label partial/unknown states.
- `with run.operation(name, operation_id=..., idempotency_key=..., side_effect_target=...) as operation:` groups logical retries. If omitted, operation ID is SDK-generated and cannot prove cross-process identity.
- `operation.call(func, *args, attempt=1, **kwargs)` and `await operation.acall(async_func, ...)` record actual called inputs, observed return values, exception **type** (not message), start/end timestamps, stable logical operation ID and unique attempt ID. Exceptions are re-raised. Instrumentation is opt-in; it is not an automatic framework integration.
- `run.record_step(...)` records an explicitly supplied agent/LLM/other execution step; never invent recorded steps from source code.
- `operation.observe(kind=..., value=..., source_kind=..., locator=..., observed_at=...)` adds a separately attributed observation linked to the latest recorded step; `operation.require(kind=..., required_for=..., consistency_deadline=...)` declares a question or postcondition. Neither changes historical tool outputs. External reads cannot establish what the agent saw earlier.
- `operation.mark_verification(claim_step_id, basis=...)` labels a recorded check as `tool_response`, `agent_output`, `external_state` or `unknown`. `operation.verify_external(...)` / `await operation.averify_external(...)` invoke your authorized read callback, record its state and explicitly compare one scalar field; they are not general-purpose provider integrations.
- `run.export_run(path)` writes canonical run JSON; `run.export_request(path)` includes provenance and evidence as separate fields. Exports write **only when called** and will not overwrite a file without `overwrite=True`. On POSIX, new exports use mode 0600.

## Boundaries, privacy, and limitations

- No global instrumentation, analytics, background threads, cloud uploads, database dependencies, SDK-managed API credentials, automatic state reads or recovery actions. The optional external verification callback can make a real network request *only when your code explicitly calls it*.
- Capture is opt-in and **may record private data**. Common secret-looking object keys (e.g. `authorization`, `password`, `api_key`, `access_token`, `cookie`) are replaced with `[REDACTED]`; this is not a comprehensive PII/secret scrubber. Do not record credentials or customer data without explicit authorization. Audit the export **before** sharing it with a coding agent or third-party model provider. Avoid credentials in `locator`, `idempotency_key`, `side_effect_target`, context text and free-form strings; the SDK does not sanitize those.
- Only JSON-native values (and tuples represented as arrays) can be captured. Unsupported/oversize tool values are **omitted and marked with capture warnings**, never converted into guessed `repr()` strings. `Traser(max_value_bytes=...)` defaults to 128 KiB per value. Oversize or unsupported *observations* raise, since silently discarding explicit evidence would be misleading.
- Evidence `coverage=partial` by default because opt-in recording cannot establish full trace coverage. `origin=recorded` is a **caller assertion**, not source authentication. Source locators link relevant recorded steps but are not independently verified. No observation changes candidate ranking in the current MCP release.
- Maximum 5,000 steps/run, 100 sources, 100 observations, 50 requirements and 10 MiB/run export. The outer MCP request has its own combined request limit; keep captures small and scoped.
- No automatic thread/async context propagation. Concurrent steps are ordered by **call entry**, which is not necessarily causal order. Custom integration and full async framework coverage need separate validation.

## Developer tests

From a local checkout, run the following after installing the SDK with `python -m pip install -e .`:


```sh
python -m unittest discover -s tests -v
python -m pip wheel . --no-deps -w /tmp/traser-wheels
```

Before publishing v0.2.0, run the cross-repository MCP integration test with the matching MCP release, validate real authorized read-only integrations, check package metadata and approve the release. The npm MCP package is separate and not covered by the SDK license.

## Open-source license and contributions

All code, tests, examples and documentation in this repository are licensed under Apache-2.0, with the license and attribution included in published archives. You may use, modify, and redistribute this SDK under those terms. The Traser analysis engine, website, and npm MCP package are separate projects and are not included here.

Please use synthetic reproductions for issues and pull requests. Never submit credentials, customer traces, or sensitive production logs.
