"""FabricOps test suite.

Runs offline: no tenant, no secrets, no network. The Fabric CLI is replaced by the fake
`fab` in `tests/fakefab`, so the engine can be tested end-to-end in seconds.

    .venv/bin/python -m unittest discover -s automation/tests -t automation -v
"""

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
for _path in (_HERE.parents[0] / "src", _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
