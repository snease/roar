"""ROAR's Python API. Importing this package does not start a GUI.

Use Design for headless input/serialization, Technologies for LUT discovery,
and Session for live Qt plots. Equations are trusted input, not a sandbox.
"""

import sys as _sys

if _sys.version_info < (3, 10):
    raise ImportError("ROAR requires Python 3.10 or newer; this is Python {}.{}.{} ({}).\n"
                      "Activate your ROAR environment, check python3 --version, and retry."
                      .format(*_sys.version_info[:3], _sys.executable))

from pathlib import Path as _Path

# Source checkouts keep floorplan in src/; wheels place it inside roar_api.
# Resolve the checkout directly so branch switches do not require reinstalling
# just to refresh setuptools' editable-package mappings.
_checkout = _Path(__file__).resolve().parent.parent
if (_checkout / "pyproject.toml").is_file() and (_checkout / "src/floorplan/__init__.py").is_file():
    __path__.append(str(_checkout / "src"))

from .design import Design, Graph
from .evaluation import EvaluationResult, Technologies
from .session import LiveGraph, Session

__version__ = "1.0.2"
__all__ = ["Design", "Graph", "Session", "LiveGraph", "Technologies", "EvaluationResult", "launch"]


def launch(design=None, *, home=None, run=False):
    """Show a design (or saved-state path); optionally run a standalone Qt loop.

    In notebooks enable Qt integration first; leave run=False and retain the
    returned Session. Standalone scripts can use Session(...).run() instead.
    """
    if design is not None and not isinstance(design, Design):
        session = Session.from_file(design, home=home, show=True)
    else:
        session = Session(design, home=home, show=True)
    if run:
        session.run()
    return session
