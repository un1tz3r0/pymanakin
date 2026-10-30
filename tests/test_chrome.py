"""ProfiledChrome tests. The browser ones drive real headless Chrome, using a
disposable source profile: nothing here reads a real profile."""
import asyncio
import datetime as dt
import functools
import http.server
import shutil
import threading
from pathlib import Path

import browserprofile as bp
import pytest
from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions

import pymanakin
from pymanakin import ProfiledChrome, cookie_param

needs_chrome = pytest.mark.skipif(not shutil.which("google-chrome"), reason="needs Chrome")

WRITE_JS = """
(async () => {
  localStorage.setItem('ls_key', 'ls_value');
  document.cookie = 'visible=js; Max-Age=86400; Path=/';
  const db = await new Promise((ok, no) => {
    const r = indexedDB.open('test', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('s');
    r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error);
  });
  await new Promise((ok, no) => {
    const t = db.transaction('s', 'readwrite');
    t.objectStore('s').put('idb_value', 'k');
    t.oncomplete = ok; t.onerror = () => no(t.error);
  });
  db.close();
})()
"""

READ_JS = """
(async () => {
  const db = await new Promise((ok, no) => {
    const r = indexedDB.open('test');
    r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error);
  });
  const idb = await new Promise((ok, no) => {
    const q = db.transaction('s').objectStore('s').get('k');
    q.onsuccess = () => ok(q.result); q.onerror = () => no(q.error);
  });
  db.close();
  return JSON.stringify({ls: localStorage.getItem('ls_key'), cookie: document.cookie, idb});
})()
"""


def _options(*arguments):
    options = ChromiumOptions()
    options.headless = True
    for argument in arguments:
        options.add_argument(argument)
    return options


async def _evaluate(tab, script):
    result = await tab.execute_script(script, await_promise=True, return_by_value=True)
    return result["result"]["result"].get("value")


class _Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Set-Cookie", "hidden=server; Max-Age=86400; Path=/; HttpOnly; SameSite=Strict")
        super().end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "index.html").write_text("<!doctype html><title>test</title>")
    handler = functools.partial(_Handler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture(scope="module")
def source_profile(site, tmp_path_factory):
    """A real Chrome profile with login-like state, keyed for ``BASICTEXT``."""
    root = tmp_path_factory.mktemp("source")

    async def build():
        options = _options(f"--user-data-dir={root}", "--password-store=basic")
        async with Chrome(options=options) as browser:
            tab = await browser.start()
            await tab.go_to(site)
            await _evaluate(tab, WRITE_JS)
            await asyncio.sleep(3)  # let the LevelDB commit land

    asyncio.run(build())
    return bp.Profile("chrome", "chromium", "Default", root / "Default", root=root, keyring_name="Chrome")


def _cookie(**overrides):
    fields = dict(
        host=".example.com", name="sid", value="abc", path="/", secure=False, http_only=False,
        same_site=None, expires=None, browser="chrome", profile="Default")
    return bp.Cookie(**{**fields, **overrides})


def test_cookie_param_full():
    expires = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    cookie = _cookie(host="host.example.com", secure=True, http_only=True, same_site="Lax", expires=expires)
    assert cookie_param(cookie) == {
        "name": "sid", "value": "abc", "domain": "host.example.com", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": expires.timestamp()}


def test_cookie_param_leaves_session_cookie_unset():
    param = cookie_param(_cookie())
    assert "expires" not in param
    assert "sameSite" not in param


@needs_chrome
def test_session_state_reaches_new_browser(site, source_profile):
    async def run():
        async with ProfiledChrome(source_profile, options=_options(), keyring="BASICTEXT") as browser:
            tab = await browser.start()
            cookies = await browser.get_cookies()
            await tab.go_to(site)
            return cookies, await _evaluate(tab, READ_JS)

    cookies, seen = asyncio.run(run())

    hidden = next(c for c in cookies if c["name"] == "hidden")
    assert hidden["httpOnly"] is True
    assert hidden["sameSite"] == "Strict"
    assert '"ls":"ls_value"' in seen
    assert '"idb":"idb_value"' in seen
    assert "visible=js" in seen


@needs_chrome
def test_one_bad_cookie_does_not_lose_the_rest(source_profile, monkeypatch):
    real_extract = bp.extract_cookies

    def with_bad_cookie(profile, keyring=None):
        return [*real_extract(profile, keyring=keyring), _cookie(name="bad", path="not-absolute")]

    monkeypatch.setattr(pymanakin.chrome.bp, "extract_cookies", with_bad_cookie)

    async def run():
        async with ProfiledChrome(source_profile, options=_options(), keyring="BASICTEXT") as browser:
            await browser.start()
            return sorted(c["name"] for c in await browser.get_cookies())

    assert asyncio.run(run()) == ["hidden", "visible"]


@needs_chrome
def test_session_never_touches_source_and_temp_profile_is_removed(site, source_profile):
    async def run():
        async with ProfiledChrome(source_profile, options=_options(), keyring="BASICTEXT") as browser:
            tab = await browser.start()
            await tab.go_to(site)
            await _evaluate(tab, "localStorage.setItem('ls_key', 'changed')")
            temp_profile = next(
                arg.split("=", 1)[1] for arg in browser.options.arguments if arg.startswith("--user-data-dir="))
            assert Path(temp_profile, "Default", "Local Storage").is_dir()
            return Path(temp_profile)

    temp_profile = asyncio.run(run())

    assert not temp_profile.exists()

    async def reread():
        async with ProfiledChrome(source_profile, options=_options(), keyring="BASICTEXT") as browser:
            tab = await browser.start()
            await tab.go_to(site)
            return await _evaluate(tab, "localStorage.getItem('ls_key')")

    assert asyncio.run(reread()) == "ls_value"


@needs_chrome
def test_failed_storage_copy_leaves_no_temp_profile(source_profile, monkeypatch):
    created = []

    def failing_copy(profile, dest):
        created.append(Path(dest).parent)
        raise bp.BrowserProfileError("boom")

    monkeypatch.setattr(pymanakin.chrome.bp, "copy_site_storage", failing_copy)
    browser = ProfiledChrome(source_profile, options=_options(), keyring="BASICTEXT")

    with pytest.raises(bp.BrowserProfileError):
        asyncio.run(browser.start())
    assert not created[0].exists()
