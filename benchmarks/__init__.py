"""The token-reduction benchmark (specification 14, Phase 7).

This package exists to measure one claim: that an agent driving a page through
typed capabilities spends fewer tokens than the same agent driving it through
low-level browser tools. It is deliberately not an evaluation framework, a
provider abstraction, or an autonomous agent — specification 14 rules all three
out, and each of them would be a larger project than the thing being measured.

The shape follows from what a comparison has to survive to be believed:

**Both arms run the same loop.** :mod:`benchmarks.agent_loop` is shared. Only the
tool list differs, because a benchmark whose arms have separate loops is measuring
the loops.

**Success is decided by the fixture, not by the model.** A run counts only if
:mod:`benchmarks.tasks` can verify the intended effect happened. A model that says
it searched and did not is a failure, and a failure is never credited with the
tokens it saved by not doing the work.

**Token counts come from the provider.** Specification 14.3 forbids estimating
them, so every number is read from API usage metadata and no run without usage
metadata is reported.
"""
