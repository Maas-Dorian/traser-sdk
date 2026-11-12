# Contributing to Traser SDK

Thanks for helping make agent execution evidence easier to investigate.

## Start locally

Use Python 3.10 or newer:

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/refund.py
```

Please include a focused test with behavior changes. Keep the SDK opt-in and local-first; do not introduce network calls, telemetry, background uploads, or hidden framework instrumentation without explicit design review.

## Protect user data

Only use synthetic examples in issues, pull requests, and tests. Do not upload real customer traces, credentials, access tokens, private prompts, or production logs. The SDK's key-based redaction is not a comprehensive secret or PII filter.

## Product boundaries

The public Python SDK records evidence; it does not independently authenticate its sources, verify real-world postconditions, or diagnose root cause. The separate Traser investigation engine and npm MCP implementation are not part of this repository.

Contributions to this repository are provided under its [Apache-2.0 license](LICENSE).
