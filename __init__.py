"""Directory-install shim for Hermes.

`hermes plugins install deepinfra/deepinfra-hermes-sandbox` copies this tree into
`~/.hermes/plugins/deepinfra-sandbox/` and loads the directory beside `plugin.yaml`,
looking for `register()` there. The real code lives in the
`deepinfra_hermes_sandbox` package (the pip-installable layout), so put this
directory on `sys.path` and re-export from it. The name is prefixed, so it does
not collide with other plugins' modules.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from deepinfra_hermes_sandbox import DeepInfraProvider, register  # noqa: E402,F401

__all__ = ["DeepInfraProvider", "register"]
