"""Writing templates to a portable file (specification 7.13).

Only templates. Specification 7.13 states it outright — a session-bound
capability is never exported — and the reason is structural rather than
cautious: a :class:`~wcc.models.capability.BoundCapability` names a browser page
in a process that has since exited. Exporting one would produce a file whose
central field is a pointer to nothing, and whose reader would have to know to
ignore it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from wcc.models.capability import CapabilityTemplate
from wcc.models.common import SCHEMA_VERSION, WCCModel

#: The file's own format version, separate from the model schema version it
#: carries. The envelope may gain fields without the templates changing, and a
#: reader has to be able to tell those two events apart.
FILE_FORMAT = "wcc-capability-file/1"


class CapabilityFile(WCCModel):
    """A set of templates, and enough context to judge them on arrival."""

    format: str = FILE_FORMAT
    schema_version: str = SCHEMA_VERSION
    exported_at: datetime
    origin: str
    """The origin the templates were compiled against.

    Stated at the top so an importer can refuse the whole file at a glance, and
    so a person can tell what a file is for without reading its plans. Every
    template must agree with it — a file mixing origins is refused rather than
    split, since the reader would have to guess which origin the file is about.
    """

    templates: list[CapabilityTemplate] = Field(min_length=1)


def export_templates(templates: list[CapabilityTemplate]) -> CapabilityFile:
    """Build the file, refusing a set that cannot be described as one thing."""
    if not templates:
        raise ValueError("there is nothing to export")

    origins = {template.origin for template in templates}
    if len(origins) != 1:
        raise ValueError(
            f"templates span {sorted(origins)}; export one origin at a time so the "
            "file can be judged as a whole"
        )

    return CapabilityFile(
        exported_at=datetime.now(UTC),
        origin=origins.pop(),
        templates=list(templates),
    )


def write_file(path: Path, capability_file: CapabilityFile) -> None:
    """Write the file with sorted keys, so two exports of one set match.

    A file that reordered itself between runs would show a diff on every export
    and hide the one that mattered.
    """
    payload = capability_file.model_dump(mode="json")
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
