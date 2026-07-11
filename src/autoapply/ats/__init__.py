"""ATS adapters. Import concrete adapters here so they self-register.

Workstreams A-D add their imports below. Each concrete module calls
``ats.base.register(TheAdapter)`` at import time, so adding an adapter never
requires editing base.py.
"""

from autoapply.ats import greenhouse as greenhouse  # noqa: F401 - registers adapter
