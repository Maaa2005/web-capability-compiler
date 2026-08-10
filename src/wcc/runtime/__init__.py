"""The execution runtime: argument validation, stepwise execution, verification.

Specification 4.2 puts the coordinator, the verifier, and the risk gate outside the
driver, and this package is where they live. Nothing here imports Playwright; the
driver is reached through the narrow protocols declared beside each user, so the
whole runtime is testable against a fake page.
"""
