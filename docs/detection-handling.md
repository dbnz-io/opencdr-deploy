# Detection handling

Use the existing signed `api request` and `fleet request` commands. `triage` is now an accepted connection tier. Agent registration accepts `allow_triage` separately from configuration `allow_writes`; the reviewed bootstrap plan includes the new triage/API roles only when opted in.

See the canonical [Core operator and acceptance guide](../../opencdr-internal/docs/detection-handling.md) in the multi-repository workspace. In standalone checkouts use the matching Core source release’s `docs/detection-handling.md`. The public release must package matching API/contract versions.

Every new edit supplies `expected_rev`, a stable 32-hex `request_id`, a reason and changes. Retry an uncertain request unchanged. Manual SIEM status remains unverified and independent of local handling. Fleet acceptance reports pending projection, not completed synchronization. No case cutover, live deployment or release acceptance is claimed.
