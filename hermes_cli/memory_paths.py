"""Compatibility alias for local memory operations.

Implementation lives in hermes_cli.local_memory_ops.paths.  This module aliases itself to the implementation
module so existing imports and monkeypatches keep targeting the real globals.
"""

from importlib import import_module as _import_module
import sys as _sys

_impl = _import_module("hermes_cli.local_memory_ops.paths")
_sys.modules[__name__] = _impl
