"""watch.race / watch.stream tests. The browser ones drive real headless Chrome
on about:blank, injecting DOM and globals to trigger conditions."""
import asyncio
import functools
import http.server
import json
import logging
import shutil
import threading

import pytest
from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions

from pymanakin import Console, Dialog, Dom, Gone, Navigation, Predicate, Response, race, stream

needs_chrome = pytest.mark.skipif(not shutil.which("google-chrome"), reason="needs Chrome")


def _options():
    options = ChromiumOptions()
    options.headless = True
    return options


async def _eval(tab, script):
    result = await tab.execute_script(script, return_by_value=True)
    return result["result"]["result"].get("value")


async def _with_tab(body):
    async with Chrome(options=_options()) as browser:
        tab = await browser.start()
        await tab.go_to("about:blank")
        return await body(tab)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("watch_site")
    (root / "index.html").write_text("<!doctype html><title>one</title>")
    (root / "page2.html").write_text("<!doctype html><title>two</title>")
    (root / "data.json").write_text('{"ok": true}')
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


async def _with_site_tab(site, body):
    async with Chrome(options=_options()) as browser:
        tab = await browser.start()
        await tab.go_to(site)
        return await body(tab)


@needs_chrome
def test_race_returns_already_present_match():
    async def body(tab):
        await tab.execute_script("document.body.innerHTML = '<div id=present></div>'")
        return await race(tab, {"absent": Dom("#absent"), "present": Dom("#present")}, timeout=5)

    hit = asyncio.run(_with_tab(body))
    assert hit.label == "present"
    assert hit.data["selector"] == "#present"
    assert hit.data["tag"] == "div"


@needs_chrome
def test_race_observer_detects_later_match():
    async def body(tab):
        task = asyncio.create_task(race(tab, {"a": Dom("#a"), "b": Dom("#b")}, timeout=5))
        await asyncio.sleep(0.3)  # let both leaves arm before the element exists
        await tab.execute_script("document.body.insertAdjacentHTML('beforeend', '<div id=b></div>')")
        return await task

    hit = asyncio.run(_with_tab(body))
    assert hit.label == "b"


@needs_chrome
def test_race_text_filter_ignores_wrong_text():
    async def body(tab):
        await tab.execute_script(
            "document.body.innerHTML = '<div class=msg>nope</div>'")
        task = asyncio.create_task(
            race(tab, {"ok": Dom(".msg", text="ready")}, timeout=5))
        await asyncio.sleep(0.3)
        await tab.execute_script(
            "document.body.insertAdjacentHTML('beforeend', '<div class=msg>ready now</div>')")
        return await task

    hit = asyncio.run(_with_tab(body))
    assert hit.label == "ok"
    assert hit.data["text"] == "ready now"  # the matched element, not the wrong-text one


@needs_chrome
def test_race_timeout_raises():
    async def body(tab):
        with pytest.raises(TimeoutError):
            await race(tab, {"never": Dom("#never")}, timeout=1)
        return True

    assert asyncio.run(_with_tab(body)) is True


@needs_chrome
def test_predicate_wins_over_dom():
    async def body(tab):
        task = asyncio.create_task(race(
            tab, {"dom": Dom("#never"), "flag": Predicate("window.__ready === true")}, timeout=5))
        await asyncio.sleep(0.3)
        await tab.execute_script("window.__ready = true")
        return await task

    hit = asyncio.run(_with_tab(body))
    assert hit.label == "flag"
    assert hit.data is True


@needs_chrome
def test_stream_yields_each_match():
    async def body(tab):
        await tab.execute_script("document.body.innerHTML = '<div id=one></div>'")
        gen = stream(tab, {"one": Dom("#one"), "two": Dom("#two")})
        first = await gen.__anext__()  # #one is already present: initial scan
        await tab.execute_script("document.body.insertAdjacentHTML('beforeend', '<div id=two></div>')")
        second = await gen.__anext__()  # #two arrives later: observer
        await gen.aclose()
        return first, second

    first, second = asyncio.run(_with_tab(body))
    assert {first.label, second.label} == {"one", "two"}


@needs_chrome
def test_gone_fires_when_already_absent():
    async def body(tab):
        return await race(tab, {"absent": Gone("#nope")}, timeout=5)

    assert asyncio.run(_with_tab(body)).label == "absent"


@needs_chrome
def test_gone_fires_when_selector_removed():
    async def body(tab):
        await tab.execute_script("document.body.innerHTML = '<div id=spinner></div>'")
        task = asyncio.create_task(race(tab, {"loaded": Gone("#spinner")}, timeout=5))
        await asyncio.sleep(0.3)  # arm the observer while the spinner is still present
        await tab.execute_script("document.getElementById('spinner').remove()")
        return await task

    assert asyncio.run(_with_tab(body)).label == "loaded"


@needs_chrome
def test_response_fires_on_matching_request(site):
    async def body(tab):
        task = asyncio.create_task(race(tab, {"api": Response("data.json")}, timeout=5))
        await asyncio.sleep(0.3)  # arm the network listener before the request
        await tab.execute_script("fetch('data.json')")
        return await task

    hit = asyncio.run(_with_site_tab(site, body))
    assert hit.label == "api"
    assert hit.data.endswith("data.json")


@needs_chrome
def test_navigation_fires_on_main_frame(site):
    async def body(tab):
        task = asyncio.create_task(race(tab, {"nav": Navigation("page2")}, timeout=5))
        await asyncio.sleep(0.3)
        await tab.go_to(f"{site}page2.html")
        return await task

    hit = asyncio.run(_with_site_tab(site, body))
    assert hit.label == "nav"
    assert hit.data.endswith("page2.html")


@needs_chrome
def test_console_fires_on_matching_level():
    async def body(tab):
        task = asyncio.create_task(race(tab, {"err": Console(level="error")}, timeout=5))
        await asyncio.sleep(0.3)  # arm before the message
        await tab.execute_script("console.log('ignored'); console.error('boom', 42)")
        return await task

    hit = asyncio.run(_with_tab(body))
    assert hit.label == "err"
    assert "boom" in hit.data and "42" in hit.data  # args joined, log ignored


class _FakeTab:
    """Captures the handler a leaf registers, so an event can be replayed without
    a browser. Used for the Dialog leaf, since a real dialog blocks the page."""

    def __init__(self):
        self.handler = None

    async def enable_page_events(self):
        pass

    async def on(self, event_name, handler):
        self.handler = handler
        return 1

    async def remove_callback(self, callback_id):
        pass


def test_dialog_matches_and_warns(caplog):
    leaf = Dialog()
    tab = _FakeTab()
    hits = []
    opening = {"params": {"url": "x", "frameId": "f", "message": "Are you sure?",
                          "type": "confirm", "hasBrowserHandler": False}}

    async def run():
        with caplog.at_level(logging.WARNING):
            await leaf.arm(tab, "dlg", lambda label, data: hits.append((label, data)))
        await tab.handler(opening)

    asyncio.run(run())
    assert hits == [("dlg", "Are you sure?")]
    assert any("blocks the page" in record.message for record in caplog.records)


def test_dialog_type_filter():
    leaf = Dialog(type="prompt")
    assert leaf._match({"type": "alert", "message": "hi"}) is None
    assert leaf._match({"type": "prompt", "message": "name?"}) == "name?"


@needs_chrome
def test_losers_are_disarmed():
    async def body(tab):
        loser = Dom("#loser")
        predicate = Predicate("window.__go === true")
        task = asyncio.create_task(race(tab, {"loser": loser, "go": predicate}, timeout=5))
        await asyncio.sleep(0.3)
        await tab.execute_script("window.__go = true")
        hit = await task
        disconnected = await _eval(tab, f"return window[{json.dumps(loser._obs)}] == null")
        return hit, disconnected, predicate._task.done()

    hit, disconnected, poll_done = asyncio.run(_with_tab(body))
    assert hit.label == "go"
    assert disconnected is True  # the losing observer was disconnected and cleared
    assert poll_done is True  # the losing predicate's poll task was cancelled
