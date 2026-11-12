# Engine KPI dictionary — version 2, 2026-11-11

Use UTC half-open windows `[start,end)` and retain the query, dimensions, timestamped
samples, release hashes and fixture manifest. The threshold source is
`opencdr-internal/docs/roadmaps/p0-p1-performance-budgets.json`; never change a threshold
because a measurement failed. The owner explicitly deferred PERF-04 (100000
records/10readers plus concurrent distribution) on2026-11-11. Its thresholds and
failed result are retained; it is excluded from tomorrow's sequential candidate
handoff, not passed. All other budgets remain unchanged. Separate cold starts, normal scheduling and assisted
recovery. These are engineering acceptance metrics, not an AWS invoice.

| KPI | Collection and formula | Threshold / interpretation |
| --- | --- | --- |
| List latency | Client monotonic elapsed time for each completed indexed list page; nearest-rank p95/p99, report N and errors separately | p95 ≤2s; p99 ≤8s |
| Summary latency | Same per-page timing; total elapsed to terminal cursor | Page p95 ≤12s; complete summary ≤120s |
| Query amplification | Trace actual API operations and `ConsumedCapacity`, including indexes; no inference from returned item count | List ≤16 index queries, ≤1000 examined, ≤2000 read units; summary page ≤32/5000/10000 |
| Query fanout | Count member requests, S3 GETs and DynamoDB scans in traced list/summary path | All zero |
| Delivery lag | End of first successful destination observation minus source `committed_at`; report conservative upper bound and N | Steady p95 ≤180s |
| Burst catch-up | Final matching ACK observation minus burst start, normal scheduler, 2000 records | ≤900s; no duplicates/loss |
| Receipt durability | Inject sink interruption and lost ACK response; compare exact receipt and unique stored identities | No acknowledged-record loss; no reset |
| Retained scan RTO | Completed job observation minus start after required drain | ≤1800s; disclose unavailable/expired history |
| Physical restore RPO | Compare source-specific snapshot timestamps, retained data and permanent effect claims | Report provable gaps; no blanket zero-RPO assertion |
| DDB consumption | CloudWatch `AWS/DynamoDB`, `ConsumedReadCapacityUnits` / `ConsumedWriteCapacityUnits`, Sum/60s, `TableName`; query each GSI with `GlobalSecondaryIndexName` separately | Sum table and index units once; include observer traffic |
| Concurrency capacity | Record `lambda get-account-settings`, applied regional quota, simultaneous readers and workers; reconcile `AWS/Lambda` `Throttles` Sum/60s | Sequential handoff: record actual activity and any throttle. PERF-04 stress/headroom is owner-deferred; no quota increase is required for this handoff. |
| Lambda compute | Parse CloudWatch Logs `REPORT` billed duration and memory, deduplicate request ID; supplement `AWS/Lambda` Invocations/Errors/Throttles, `FunctionName`, Sum/60s | Use billed milliseconds × configured GiB × regional architecture rate; Duration alone omits billing effects |
| API requests | `AWS/ApiGateway`, `Count`, `ApiName` and `Stage`, Sum/60s | Missing samples are unknown, not zero |
| S3 requests | Scoped CloudTrail data events, deduplicate `eventID`, verify probe `requestID`, group resource and API operation within window | Price request class; missing events are not zero |
| SQS requests | Scoped CloudTrail queue data events, deduplicate `eventID`, group queue/action; compare SDK receipts where available | Message counters are not API-call counts; payload bodies may be redacted |
| SQS billed units | Exact payload sizes where independently captured, round each billable payload to 64KiB units; otherwise label a justified conservative upper bound | Never claim redacted payload sizes as measured |
| Delivery cost | Dated regional rates × measured usage, no free tier; retain rounding, missing telemetry and excluded cost categories | Complete stated request-cost measurement ≤$5; incomplete subtotal cannot pass |
| Query cost | Same measured basis per1000 list pages / full summary | ≤$2 / ≤$0.10 |

CloudWatch query example (repeat for each exact physical table/index and explicit window):

```sh
aws cloudwatch get-metric-statistics --profile "$CORE_PROFILE" --region "$REGION" \
  --namespace AWS/DynamoDB --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value="$TABLE_NAME" --statistics Sum --period 60 \
  --start-time "$WINDOW_START" --end-time "$WINDOW_END"
```

Use `get-metric-data` with pagination for a complete multi-resource collection. Resolve
physical identities from the current template and restored bindings; old logical names
can point at retained inactive tables. Store each query/result and distinguish empty,
partial and complete responses. Align CloudWatch windows to enclosing minute boundaries
and disclose background/observer activity.

The candidate Deploy `usage-telemetry plan` creates only scoped audit resources through
CloudFormation. Review and execute its exact change set with `release execute`. Validate
known request IDs before the sample; stop using `usage-telemetry stop-plan` with the
original file/SHA, then review/execute that plan. Retain logs and verify logging stopped.
Do not edit an engine stack or bypass recovery guards to add counters.

Official billing references: [SQS request/payload billing](https://aws.amazon.com/sqs/pricing/),
[SQS CloudTrail event coverage](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/logging-using-cloudtrail.html),
[CloudTrail data-event pricing](https://aws.amazon.com/cloudtrail/pricing/).
The measurement must independently capture dated regional numeric rates for all services.

Run baseline, representative delivery, normal burst, outage/recovery and retention
scenarios with independent windows. Reuse still-applicable prior functional/performance
evidence after unrelated changes; repeat affected paths after substantial changes.
Public release verification and owner acceptance remain separate worksheet entries.


### Approved SQS bound for P1

On 2026-11-11 the owner approved a documented conservative SQS billing upper bound,
with the $5 measurement ceiling unchanged. Keep actual API counts separate from billed
units. A receive can return up to ten messages; bounding every queue call at only sixteen
64KiB units is not a valid general receive-batch bound. Use at least160 units per call
when bounding ten maximum-size1MiB messages, and include any applicable queue surcharge.
Use a tighter bound only with retained evidence of the relevant request/response limits.
This does not excuse missing API events or incomplete service coverage. Earlier partial
estimates using16 units per call cannot be final acceptance evidence.


## Independent run measurements

For the sequential handoff, use a new UTC window and report each request's elapsed
milliseconds, count and errors. Do not infer a p95/p99 from a handful of smoke calls.
The blank worksheet distinguishes `not_run`, `blocked`, `fail`, `pass`,
`deferred_by_user` and `not_applicable`; use the last only with a scope rationale.
Prior P1 cost acceptance used measured API counts plus the explicitly approved
conservative SQS billing bound. Copying that historical number is not a new invoice
or independent owner cost measurement. A blank CloudWatch response is not zero.
