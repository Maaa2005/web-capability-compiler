"""The version of the compiler's output (specification 17).

A saved template is reused for as long as it still binds, so a fix to the compiler
reaches only pages that have never been compiled. This number says which compiler
made a stored template: raise it in the same commit as any change that can make the
compiler produce a different template for the same page, and compiled templates
from an earlier revision are ignored and compiled again.

An integer raised by hand, not a hash of the code: a hash would discard every
template on a comment change, while a person who forgets to raise it has made a
correctness bug, so the rule is written next to the number. Distinct from
``SCHEMA_VERSION`` (the shape of the stored models) and ``SCHEMA_REVISION`` (the
shape of the database); none of the three implies the others.
"""

COMPILER_REVISION = 1
