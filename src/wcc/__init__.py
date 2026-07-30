"""Web Capability Compiler.

Compile existing websites into reusable, typed capabilities for MCP agents.
The implementation source of truth is docs/spec/WCC_AGENT_IMPLEMENTATION_SPEC_v0.2.1.md.
"""

from wcc.models.common import SCHEMA_VERSION

__version__ = "0.1.0.dev0"

__all__ = ["SCHEMA_VERSION", "__version__"]
