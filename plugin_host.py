"""Plugin loader and hook dispatcher.

Scans a directory for Python files, imports each, and dispatches
hook calls to any module that defines a matching function.

Hooks are plain functions whose names start with ``on_``.  A plugin
only needs to define the hooks it cares about — everything else is
ignored.  Hook functions receive a shared *ctx* dict as first arg
plus hook-specific keyword arguments.

Swappable core functions (llm_call, screenshot_fn, command_runner,
extract_command) are stored in ctx and can be replaced by any plugin
during ``on_startup``.
"""

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any


class PluginHost:
    """Load plugins from a directory tree and dispatch hooks."""

    def __init__(self) -> None:
        self._plugins: list[Any] = []  # loaded modules

    def load_directory(self, path: str) -> list[str]:
        """Import all .py files under *path* (recursive).

        Returns the list of loaded module names.
        """
        root = Path(path)
        if not root.is_dir():
            return []

        loaded: list[str] = []
        for py_file in sorted(root.rglob("*.py")):
            if py_file.name.startswith("_"):
                continue
            mod_name = f"cua_plugin.{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, py_file)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                self._plugins.append(mod)
                loaded.append(py_file.stem)
            except Exception as e:
                print(f"   ⚠ Failed to load plugin {py_file.name}: {e}")
        return loaded

    def emit(self, hook: str, ctx: dict, **kwargs: Any) -> None:
        """Call *hook* on every plugin that defines it.

        Each plugin receives ``ctx`` plus any extra keyword args.
        Exceptions are caught and printed — a broken plugin never
        crashes the agent.
        """
        for mod in self._plugins:
            fn = getattr(mod, hook, None)
            if fn is None:
                continue
            try:
                fn(ctx, **kwargs)
            except Exception as e:
                name = getattr(mod, "__name__", "?")
                print(f"   ⚠ Plugin {name}.{hook}() failed: {e}")
