"""Race several page conditions and react to whichever fires first.

Dynamic pages leave you waiting on more than one outcome at a point in a script:
a result list *or* an error banner *or* a captcha. :func:`race` arms a set of
conditions, returns the first :class:`Hit`, and tears the rest down; :func:`stream`
keeps them armed and yields hits as they arrive.

A condition is a :class:`Leaf`: something that arms itself against a tab, calls
``emit(label, data)`` when it fires, and disarms cleanly. The provided ones:

* :class:`Dom` — a selector appearing (optionally with some text). Driven by a
  page-side ``MutationObserver`` that reports over a CDP binding; scans once on
  arm, so an element already present fires at once, and fires once.
* :class:`Gone` — a selector no longer matching anything (a spinner removed).
  Same observer machinery; fires once.
* :class:`Response` — a network response whose URL contains a substring (and
  matches a status, if given). Fires on every match.
* :class:`Navigation` — the main frame navigating (to a URL substring, if given).
  Fires on every navigation.
* :class:`Console` — a ``console`` call matching a substring and/or level. Fires
  on every match.
* :class:`Dialog` — a JavaScript dialog opening. Fires on every match; note the
  warning on the class, as an open dialog blocks the page until it is handled.
* :class:`Predicate` — a JavaScript expression polled until truthy, for anything
  the others don't cover (``window`` state, computed style). Fires once.

Writing your own is just the two-method :class:`Leaf` protocol.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol, runtime_checkable

from pydoll.commands import RuntimeCommands
from pydoll.protocol.network.events import NetworkEvent
from pydoll.protocol.page.events import PageEvent
from pydoll.protocol.runtime.events import RuntimeEvent

logger = logging.getLogger(__name__)

Emit = Callable[[str, Any], None]


@dataclass(frozen=True)
class Hit:
    """One condition firing: the ``label`` it was armed under and its payload."""

    label: str
    data: Any


@runtime_checkable
class Leaf(Protocol):
    """A condition. ``arm`` sets it up and returns; it calls ``emit(label, data)``
    each time it fires; ``disarm`` tears it down. Both methods are awaited by the
    engine, so ``disarm`` runs as ordinary cleanup, never mid-cancellation."""

    async def arm(self, tab, label: str, emit: Emit) -> None: ...
    async def disarm(self) -> None: ...


@contextlib.asynccontextmanager
async def _armed(tab, leaves: dict[str, Leaf]) -> AsyncIterator[asyncio.Queue[Hit]]:
    queue: asyncio.Queue[Hit] = asyncio.Queue()

    def emit(label: str, data: Any) -> None:
        queue.put_nowait(Hit(label, data))

    armed: list[Leaf] = []
    try:
        for label, leaf in leaves.items():
            await leaf.arm(tab, label, emit)
            armed.append(leaf)
        yield queue
    finally:
        for leaf in reversed(armed):
            with contextlib.suppress(Exception):
                await leaf.disarm()


async def race(tab, leaves: dict[str, Leaf], *, timeout: float | None = None) -> Hit:
    """Arm every leaf, return the first :class:`Hit`, disarm the rest.

    ``leaves`` maps a label to a :class:`Leaf`; the returned hit carries the label
    that fired. Raises :class:`TimeoutError` if none fires within ``timeout``
    seconds (``None`` waits forever).
    """
    async with _armed(tab, leaves) as queue:
        if timeout is None:
            return await queue.get()
        try:
            return await asyncio.wait_for(queue.get(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"no condition fired within {timeout}s") from None


async def stream(tab, leaves: dict[str, Leaf]) -> AsyncIterator[Hit]:
    """Arm every leaf and yield hits as they arrive, until the caller stops
    iterating; the leaves are disarmed when the generator closes."""
    async with _armed(tab, leaves) as queue:
        while True:
            yield await queue.get()


# A MutationObserver that reports over window[BINDING]. probe() returns a JSON
# payload string when the condition holds, or null; it runs once so an
# already-satisfied condition fires at arm time, and a flag makes it fire once
# and then disconnect.
_OBSERVER_INSTALL = """
(function () {
  function probe() { %(probe)s }
  function fire(payload) {
    if (window[%(flag)s]) return;
    window[%(flag)s] = true;
    var obs = window[%(obs)s];
    if (obs) { obs.disconnect(); window[%(obs)s] = null; }
    window[%(binding)s](payload);
  }
  var hit = probe();
  if (hit !== null) { fire(hit); }
  else {
    var o = new MutationObserver(function () { var h = probe(); if (h !== null) fire(h); });
    window[%(obs)s] = o;
    o.observe(document.documentElement,
      { childList: true, subtree: true, attributes: true, characterData: true });
  }
})();
"""

_OBSERVER_DISCONNECT = "(function(){var o=window[%(obs)s];if(o){o.disconnect();window[%(obs)s]=null;}})();"


class _ObserverLeaf:
    """Base for conditions watching the DOM. Subclasses supply ``_probe_js`` (a JS
    statement block returning a JSON payload string when the condition holds, or
    ``null``). The observer reports the payload back over a CDP binding, and it is
    delivered as the hit's ``data`` (a parsed dict)."""

    def __init__(self):
        token = uuid.uuid4().hex
        self._binding = f"__pm_b_{token}"
        self._flag = f"__pm_f_{token}"
        self._obs = f"__pm_o_{token}"
        self._tab = None
        self._callback_id: int | None = None

    def _probe_js(self) -> str:
        raise NotImplementedError

    async def arm(self, tab, label: str, emit: Emit) -> None:
        self._tab = tab
        await tab.enable_runtime_events()
        await tab._execute_command(RuntimeCommands.add_binding(self._binding))

        async def on_binding(event):
            params = event["params"]
            if params["name"] == self._binding:
                emit(label, json.loads(params["payload"]))

        self._callback_id = await tab.on(RuntimeEvent.BINDING_CALLED, on_binding)
        await tab.execute_script(_OBSERVER_INSTALL % {
            "probe": self._probe_js(),
            "flag": json.dumps(self._flag), "obs": json.dumps(self._obs),
            "binding": json.dumps(self._binding)})

    async def disarm(self) -> None:
        if self._callback_id is not None:
            with contextlib.suppress(Exception):
                await self._tab.remove_callback(self._callback_id)
        if self._tab is not None:
            with contextlib.suppress(Exception):
                await self._tab.execute_script(_OBSERVER_DISCONNECT % {"obs": json.dumps(self._obs)})


class Dom(_ObserverLeaf):
    """Fires when ``selector`` matches an element (optionally one containing
    ``text``). Scans once on arm, so an element already on the page fires at once,
    then fires a single time. ``data`` is ``{"selector", "tag", "text"}`` describing
    the matched element (its text trimmed and capped)."""

    def __init__(self, selector: str, *, text: str | None = None):
        super().__init__()
        self._selector = selector
        self._text = text

    def _probe_js(self) -> str:
        sel, txt = json.dumps(self._selector), json.dumps(self._text)
        return (f"var els = document.querySelectorAll({sel}), txt = {txt};"
                f"for (var i = 0; i < els.length; i++) {{ var el = els[i];"
                f"  if (txt === null || (el.textContent || '').indexOf(txt) !== -1)"
                f"    return JSON.stringify({{ selector: {sel}, tag: el.tagName.toLowerCase(),"
                f"      text: (el.textContent || '').trim().slice(0, 200) }}); }}"
                f"return null;")


class Gone(_ObserverLeaf):
    """Fires when ``selector`` matches nothing — e.g. a loading spinner removed.
    If it already matches nothing on arm, fires at once; fires once. ``data`` is
    ``{"selector"}`` (there is no element left to describe)."""

    def __init__(self, selector: str):
        super().__init__()
        self._selector = selector

    def _probe_js(self) -> str:
        sel = json.dumps(self._selector)
        return f"if (document.querySelectorAll({sel}).length === 0) return JSON.stringify({{ selector: {sel} }}); return null;"


class _EventLeaf:
    """Base for conditions riding a CDP event. Subclasses set ``_event`` (the CDP
    event name) and ``_enable`` (the tab coroutine that turns its domain on), and
    implement ``_match(params)``, returning the payload to emit or ``None`` to
    ignore the event."""

    _event: str = ""
    _enable: str = ""

    def __init__(self):
        self._tab = None
        self._callback_id: int | None = None

    def _match(self, params: dict) -> Any | None:
        raise NotImplementedError

    async def arm(self, tab, label: str, emit: Emit) -> None:
        self._tab = tab
        await getattr(tab, self._enable)()

        async def handler(event):
            data = self._match(event["params"])
            if data is not None:
                emit(label, data)

        self._callback_id = await tab.on(self._event, handler)

    async def disarm(self) -> None:
        if self._callback_id is not None:
            with contextlib.suppress(Exception):
                await self._tab.remove_callback(self._callback_id)


class Response(_EventLeaf):
    """Fires when a network response's URL contains ``url_substring`` (and matches
    ``status``, if given). ``data`` is the response URL. Fires on every match, and
    enables network events on the tab."""

    _event = NetworkEvent.RESPONSE_RECEIVED
    _enable = "enable_network_events"

    def __init__(self, url_substring: str, *, status: int | None = None):
        super().__init__()
        self._url = url_substring
        self._status = status

    def _match(self, params):
        response = params["response"]
        if self._url in response["url"] and (self._status is None or response["status"] == self._status):
            return response["url"]
        return None


class Navigation(_EventLeaf):
    """Fires when the main frame navigates (to a URL containing ``url_substring``,
    if given). ``data`` is the new URL. Fires on every navigation, and enables
    page events on the tab."""

    _event = PageEvent.FRAME_NAVIGATED
    _enable = "enable_page_events"

    def __init__(self, url_substring: str | None = None):
        super().__init__()
        self._url = url_substring

    def _match(self, params):
        frame = params["frame"]
        if "parentId" in frame:  # a sub-frame, not the page
            return None
        if self._url is None or self._url in frame["url"]:
            return frame["url"]
        return None


def _console_arg(arg: dict) -> str:
    """One console argument as text: its value, else Chrome's description, else
    its type (an object logged by reference carries no value)."""
    if "value" in arg:
        return str(arg["value"])
    return str(arg.get("description", arg["type"]))


class Console(_EventLeaf):
    """Fires on a ``console`` call whose joined text contains ``text`` (if given)
    and whose level matches ``level`` (``"error"``, ``"warning"``, …, if given).
    ``data`` is the joined message. Fires on every match, and enables runtime
    events on the tab."""

    _event = RuntimeEvent.CONSOLE_API_CALLED
    _enable = "enable_runtime_events"

    def __init__(self, text: str | None = None, *, level: str | None = None):
        super().__init__()
        self._text = text
        self._level = level

    def _match(self, params):
        if self._level is not None and params["type"] != self._level:
            return None
        message = " ".join(_console_arg(arg) for arg in params["args"])
        if self._text is None or self._text in message:
            return message
        return None


class Dialog(_EventLeaf):
    """Fires when a JavaScript dialog opens (``alert`` / ``confirm`` / ``prompt`` /
    ``beforeunload``, or only ``type`` if given). ``data`` is the dialog message.

    Warning: an open dialog blocks the page *and* CDP until it is handled.
    Detecting it here does not dismiss it — handle it (``Page.handleJavaScriptDialog``)
    or the session will hang. ``arm`` logs this at WARNING level."""

    _event = PageEvent.JAVASCRIPT_DIALOG_OPENING
    _enable = "enable_page_events"

    def __init__(self, type: str | None = None):
        super().__init__()
        self._type = type

    async def arm(self, tab, label: str, emit: Emit) -> None:
        logger.warning(
            "Dialog leaf armed (%s): an open JS dialog blocks the page and CDP until handled; "
            "detecting it does not dismiss it, so handle it or the session may hang.", label)
        await super().arm(tab, label, emit)

    def _match(self, params):
        if self._type is None or params["type"] == self._type:
            return params["message"]
        return None


class Predicate:
    """Fires when a JavaScript ``expression`` evaluates truthy, polled every
    ``interval`` seconds. Fires once; ``data`` is the truthy value."""

    def __init__(self, expression: str, *, interval: float = 0.1):
        self._expression = expression
        self._interval = interval
        self._task: asyncio.Task | None = None

    async def arm(self, tab, label: str, emit: Emit) -> None:
        self._task = asyncio.create_task(self._poll(tab, label, emit))

    async def _poll(self, tab, label: str, emit: Emit) -> None:
        script = f"return ({self._expression});"
        while True:
            response = await tab.execute_script(script, return_by_value=True)
            value = response["result"]["result"].get("value")
            if value:
                emit(label, value)
                return
            await asyncio.sleep(self._interval)

    async def disarm(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
