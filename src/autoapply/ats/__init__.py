"""ATS adapters. Import concrete adapters here so they self-register.

Workstreams A-D add their imports below. Each concrete module calls
``ats.base.register(TheAdapter)`` at import time, so adding an adapter never
requires editing base.py.
"""

# Registration order is priority order (base.resolve_adapter): specific
# adapters first, generic fallback LAST.
# isort: off
from autoapply.ats import ashby as ashby  # noqa: F401 - registers adapter
from autoapply.ats import greenhouse as greenhouse  # noqa: F401 - registers adapter
from autoapply.ats import lever as lever  # noqa: F401 - registers adapter
from autoapply.ats import generic as generic  # noqa: F401 - fallback, keep last
# isort: on
