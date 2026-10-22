# Raw benchmark runs

Every run this benchmark has made, including the failures, as written by
`benchmarks.metrics.write_runs`. Tracked in git on purpose: specification 14.4
publishes raw logs because a token-reduction figure that cannot be checked against
the runs behind it is a claim rather than a measurement.

Each file holds one invocation. Each record carries the model, the timestamps, the
task, the input value, the tool-call count, the provider-reported usage, whether
the run met its verification condition, and the turn-by-turn transcript.

`runs-20261022T124255Z.json` is the run of record: 27 runs (three tasks, three
arms, three attempts), made on 2026-10-22 with the harness frozen at commit
`2aa9d7a`, through Claude Code on a subscription. All 27 met their verification
condition. What they show is in
[`docs/comparison.md`](../../docs/comparison.md#what-a-task-costs).

Smoke runs made before the harness was frozen are not kept here: they ran a
harness with known faults, and none of them is a result. What they found is in
`docs/progress.md`.
