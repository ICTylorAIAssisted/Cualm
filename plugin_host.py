"""Plugin loader and hook dispatcher.

Scans a plugin directory and loads plugins in two forms:

1. **Flat files** — ``plugins/my_plugin.py`` is loaded directly.
2. **Directory plugins** — ``plugins/xmpp/plugin.py`` is loaded as the
   entry point.  The directory is added to ``sys.path`` so the plugin
   can import its own subpackages (e.g. ``from xmpp_tools import …``).

Only files/dirs at the top level of the plugins directory are
considered — the loader never recurses into subdirectories looking
for additional plugin entry points.

Hooks are plain functions whose names start with ``on_``.  A plugin
only needs to define the hooks it cares about — everything else is
ignored.  Hook functions receive a shared *ctx* dict as first arg
plus hook-specific keyword arguments.

Swappable core functions (llm_call, screenshot_fn, command_runner,
extract_command) are stored in ctx and can be replaced by any plugin
during ``on_startup``.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any


class PluginHost:
    """Load plugins from a directory and dispatch hooks."""

    def __init__(self) -> None:
        self._plugins: list[Any] = []  # loaded modules

    def load_directory(self, path: str) -> list[str]:
        """Discover and import plugins under *path*.

        Supports two layouts:
        - ``path/foo.py``         → loaded as plugin ``foo``
        - ``path/bar/plugin.py``  → loaded as plugin ``bar``
          (``path/bar/`` is added to sys.path so it can import
          its own packages)

        Returns the list of loaded plugin names.
        """
        root = Path(path)
        if not root.is_dir():
            return []

        loaded: list[str] = []

        for entry in sorted(root.iterdir()):
            # Skip hidden and dunder entries
            if entry.name.startswith((".", "_")):
                continue

            if entry.is_file() and entry.suffix == ".py":
                # Flat file plugin
                name = entry.stem
                mod = self._load_file(name, entry)
                if mod is not None:
                    loaded.append(name)

            elif entry.is_dir():
                plugin_py = entry / "plugin.py"
                if plugin_py.is_file():
                    # Directory plugin — add dir to sys.path for
                    # subpackage imports, then load plugin.py
                    dir_str = str(entry)
                    if dir_str not in sys.path:
                        sys.path.insert(0, dir_str)
                    name = entry.name
                    mod = self._load_file(name, plugin_py)
                    if mod is not None:
                        loaded.append(name)

        return loaded

    def _load_file(self, name: str, path: Path) -> Any:
        """Import a single Python file as a plugin module."""
        mod_name = f"cua_plugin.{name}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            self._plugins.append(mod)
            return mod
        except Exception as e:
            print(f"   ⚠ Failed to load plugin {name} ({path.name}): {e}")
            return None

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
