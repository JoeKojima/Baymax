"""The ordered feature registry: one place that says what each feature hooks into, and in what order.

A feature is a `Plugin`. It may contribute:
    base_config(config) -> config     to the base session config (built once, at startup)
    config(config) -> config          to each session's config (every connect)
    input_gate(samples) -> bool       True withholds this mic audio from Gemini
    output_source(n_bytes, voice_active) -> bytes | None   extra audio mixed into the speaker
    frame_observer(frame)             every camera frame, on the camera thread; must be cheap
    session_task(session, shutdown_event)   an async task for each live session
    subscribe {topic: callback}       ember.bus events ("state", "transcript")
    load()                            imports or registers anything else the feature needs
Registry.install() applies them in list order into the hook lists the core
already reads (processing.input_gates / output_sources, fall_detection.
frame_observers / session_tasks), skipping anything already present, so
features that still register themselves on import keep working unchanged.
Order is explicit: gates run in this order, so a gate listed first sees the
audio first (e.g. a wake-word gate before the audiobook's).
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

HOOKS = ("input_gate", "output_source", "frame_observer", "session_task")


@dataclass
class Plugin:
    name: str
    about: str = ""
    enabled: Callable[[], bool] = lambda: True
    load: Optional[Callable[[], None]] = None
    base_config: Optional[Callable[[dict], dict]] = None
    config: Optional[Callable[[dict], dict]] = None
    input_gate: Optional[Callable] = None
    output_source: Optional[Callable] = None
    frame_observer: Optional[Callable] = None
    session_task: Optional[Callable] = None
    subscribe: dict = field(default_factory=dict)

    def hooks(self):
        """{hook name: callable} after load(); the callables may be resolved lazily (see `lazy`)."""
        return {name: _resolve(getattr(self, name)) for name in HOOKS if getattr(self, name) is not None}


class lazy:
    """A hook named by import path ("package.module:attr.attr"), resolved after load()."""

    def __init__(self, path):
        self.path = path

    def resolve(self):
        import importlib
        module, _, attrs = self.path.partition(":")
        value = importlib.import_module(module)
        for attr in attrs.split("."):
            value = getattr(value, attr)
        return value

    def __repr__(self):
        return f"lazy({self.path!r})"


def _resolve(value):
    return value.resolve() if isinstance(value, lazy) else value


def hook_lists():
    """The lists the core reads, by hook name."""
    from features.conversational_ability import processing
    from features.toolkits import fall_detection
    return {"input_gate": processing.input_gates, "output_source": processing.output_sources,
            "frame_observer": fall_detection.frame_observers, "session_task": fall_detection.session_tasks}


class Registry:
    def __init__(self, plugins, lists=None, bus=None):
        names = [plugin.name for plugin in plugins]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate plugin names: {names}")
        self.plugins = list(plugins)
        self._lists = lists
        self._bus = bus
        self.installed = False

    @property
    def lists(self):
        if self._lists is None:
            self._lists = hook_lists()
        return self._lists

    def active(self):
        return [plugin for plugin in self.plugins if plugin.enabled()]

    def install(self):
        """Load each enabled plugin and add its hooks in order (idempotent)."""
        if self.installed:
            return self
        bus = self._bus
        if bus is None:
            from ember.bus import bus
        for plugin in self.active():
            if plugin.load is not None:
                plugin.load()
            for hook, callback in plugin.hooks().items():
                target = self.lists[hook]
                if callback not in target:
                    target.append(callback)
            for topic, callback in plugin.subscribe.items():
                bus.subscribe(topic, _resolve(callback))
        self.installed = True
        return self

    def expected(self):
        """{hook: [callables in declared order]} for the enabled plugins."""
        order = {hook: [] for hook in HOOKS}
        for plugin in self.active():
            for hook, callback in plugin.hooks().items():
                order[hook].append(callback)
        return order

    def problems(self):
        """Differences between the declared order and the live hook lists: unknown or misordered hooks."""
        found = []
        for hook, expected in self.expected().items():
            actual = list(self.lists[hook])
            extra = [callback for callback in actual if callback not in expected]
            declared = [callback for callback in actual if callback in expected]
            if extra:
                found.append(f"{hook}: registered outside the registry: {[_name(c) for c in extra]}")
            if declared != [c for c in expected if c in declared]:
                found.append(f"{hook}: order {[_name(c) for c in declared]} != declared {[_name(c) for c in expected]}")
            missing = [c for c in expected if c not in actual]
            if missing:
                found.append(f"{hook}: declared but not registered: {[_name(c) for c in missing]}")
        return found

    def base_config(self, config):
        for plugin in self.active():
            if plugin.base_config is not None:
                config = plugin.base_config(config)
        return config

    def session_config(self, config):
        for plugin in self.active():
            if plugin.config is not None:
                config = plugin.config(config)
        return config

    def describe(self):
        rows = []
        for plugin in self.plugins:
            parts = [name for name in ("base_config", "config", *HOOKS) if getattr(plugin, name) is not None]
            parts += [f"bus:{topic}" for topic in plugin.subscribe]
            rows.append((plugin.name, "on" if plugin.enabled() else "off", ", ".join(parts) or "-", plugin.about))
        return rows


def _name(callback):
    owner = getattr(callback, "__self__", None)
    name = getattr(callback, "__qualname__", repr(callback))
    return f"{type(owner).__name__}.{callback.__name__}" if owner is not None else name
