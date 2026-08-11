# Raw benchmark runs

Every run this benchmark has made, including the failures, as written by
`benchmarks.metrics.write_runs`. Tracked in git on purpose: specification 14.4
publishes raw logs because a token-reduction figure that cannot be checked against
the runs behind it is a claim rather than a measurement.

Each file holds one invocation. Each record carries the model, the timestamps, the
task, the input value, the tool-call count, the provider-reported usage, whether
the run met its verification condition, and the turn-by-turn transcript.

**Nothing here yet.** The harness is complete and tested; the measuring run has
not been made.
