"""The last check before anything reaches the disk (specification 7.2, 18).

Two barriers already stand between a password field and a recorded value: the page
script never reads one, and :class:`~wcc.models.element.NormalizedElement` refuses
to hold one. This is the third, and it exists because the first two guard the same
path — an element as it is extracted — while persistence writes whole documents,
including nested shapes that arrive from elsewhere.

The check is structural rather than a search for known-bad strings. Looking for a
sentinel only finds the value a test planted; looking for "a control whose type may
never carry a value, carrying one" finds the class of mistake.
"""

from __future__ import annotations

from typing import Any

#: Mirrors :data:`wcc.models.common.NEVER_CAPTURED` as plain strings. Imported as
#: values rather than as the enum so this walks decoded JSON, which is what is
#: actually about to be written.
_NEVER_CAPTURED = frozenset({"password", "file", "hidden"})

#: Keys whose presence marks a mapping as describing a control.
_CONTROL_MARKERS = ("control_type", "value")


class SensitiveValueError(Exception):
    """A payload carried a value specification 7.2 says must never be captured."""


def _offences(node: Any, path: str) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if all(marker in node for marker in _CONTROL_MARKERS):
            control_type = str(node.get("control_type", ""))
            value = node.get("value")
            if control_type in _NEVER_CAPTURED and value is not None:
                found.append(f"{path}: a {control_type} control carries a value")
            if node.get("value_omitted_for_safety") and value is not None:
                found.append(f"{path}: the value is marked omitted for safety and is still present")
        for key, child in node.items():
            found.extend(_offences(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found.extend(_offences(child, f"{path}[{index}]"))
    return found


def assert_no_sensitive_values(payload: dict[str, Any], *, described_as: str) -> None:
    """Raise unless ``payload`` is safe to persist.

    Refuses the write rather than stripping the value. Silently removing it would
    persist a record that looks complete and is not, and would hide the bug that
    produced it — and the bug is the interesting part, because whatever put a
    password in a document will do it again somewhere else.
    """
    offences = _offences(payload, "")
    if offences:
        raise SensitiveValueError(
            f"refusing to persist {described_as}: " + "; ".join(sorted(offences))
        )
