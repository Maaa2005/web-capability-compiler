"""Agent Page IR: building it, and deciding when two readings mean the same."""

from wcc.ir.builder import build_page_ir
from wcc.ir.canonical import canonical_projection, semantically_equivalent

__all__ = ["build_page_ir", "canonical_projection", "semantically_equivalent"]
