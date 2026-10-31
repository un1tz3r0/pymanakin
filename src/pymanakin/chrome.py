"""pydoll Chrome seeded with a copy of the user's real browser profile.

Each source of session state goes in by the route that survives Chromium's
encryption. localStorage and IndexedDB aren't encrypted with the OS key, so they
are copied into the temp profile before launch. Cookies are, so they are
decrypted by browserprofile and replayed over CDP before the first navigation,
which works whichever key the new browser ends up using.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import browserprofile as bp
from pydoll.browser.chromium import Chrome
from pydoll.exceptions import CommandFailed

if TYPE_CHECKING:
    from pydoll.browser.options import ChromiumOptions
    from pydoll.browser.tab import Tab
    from pydoll.protocol.network.types import Cookie as CdpCookie, CookieParam

logger = logging.getLogger(__name__)

# username field, in order of preference; the password field is input[type=password]
_USERNAME_SELECTORS = ("input[autocomplete='username']", "input[type='email']", "input[type='text']")


def cookie_param(cookie: bp.Cookie) -> CookieParam:
    """Map a browserprofile cookie to CDP. A host without a leading dot stays host-only."""
    param: CookieParam = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.host,
        "path": cookie.path,
        "secure": cookie.secure,
        "httpOnly": cookie.http_only,
    }
    if cookie.same_site:
        param["sameSite"] = cookie.same_site
    if cookie.expires:
        param["expires"] = cookie.expires.timestamp()
    return param


def _cookie_from_cdp(cookie: CdpCookie) -> bp.Cookie:
    """Map a live CDP cookie back to a browserprofile record, so a live snapshot
    shares the checkpoint format with an on-disk one. ``expires <= 0`` is a
    session cookie."""
    expires = cookie.get("expires")
    return bp.Cookie(
        host=cookie["domain"], name=cookie["name"], value=cookie["value"],
        path=cookie.get("path", "/"), secure=bool(cookie.get("secure")),
        http_only=bool(cookie.get("httpOnly")), same_site=cookie.get("sameSite"),
        expires=dt.datetime.fromtimestamp(expires, dt.timezone.utc) if expires and expires > 0 else None,
        browser="pydoll", profile="session")


def _host_matches(login_host: str, page_host: str) -> bool:
    """True when a login's host covers the page host (exact or registrable-suffix)."""
    return (login_host == page_host
            or page_host.endswith("." + login_host)
            or login_host.endswith("." + page_host))


class ProfiledChrome(Chrome):
    """A :class:`Chrome` that starts out logged in wherever ``profile`` is.

    The copy lives in a temporary profile that pydoll deletes on stop, so nothing
    done in this session reaches the real profile.
    """

    def __init__(
        self,
        profile: bp.Profile,
        options: ChromiumOptions | None = None,
        connection_port: int | None = None,
        keyring: str | None = None,
    ):
        super().__init__(options, connection_port)
        self._profile = profile
        self._keyring = keyring
        self._logins: list[bp.Login] | None = None  # extracted lazily on first autofill_login

    async def start(self) -> Tab:
        cookies = bp.extract_cookies(self._profile, keyring=self._keyring)

        # Created through pydoll's manager so its stop and failed-start paths clean it up
        temp_dir = self._temp_directory_manager.create_temp_dir()
        try:
            bp.copy_site_storage(self._profile, Path(temp_dir.name) / "Default")
        except Exception:
            self._temp_directory_manager.cleanup()
            raise
        self.options.add_argument(f"--user-data-dir={temp_dir.name}")

        tab = await super().start()
        await self._inject_cookies(cookies)
        return tab

    async def _inject_cookies(self, cookies: list[bp.Cookie]) -> None:
        params = [cookie_param(cookie) for cookie in cookies]
        try:
            await self.set_cookies(params)
            return
        except CommandFailed:
            logger.debug("cookie batch rejected, retrying one at a time")

        # setCookies is all-or-nothing, so one bad cookie would drop every login
        skipped = 0
        for param in params:
            try:
                await self.set_cookies([param])
            except CommandFailed:
                skipped += 1
        logger.warning("skipped %d of %d cookies the browser rejected", skipped, len(params))

    async def checkpoint(self, secrets: str = "hash") -> dict:
        """Snapshot the live session's cookies, in browserprofile's checkpoint
        format. Take one before and one after some browsing and pass both to
        ``browserprofile.diff_checkpoints`` to see what the sites changed."""
        cookies = [_cookie_from_cdp(c) for c in await self.get_cookies()]
        return bp.checkpoint_from_records(
            {"cookies": cookies}, secrets=secrets, sources=["pydoll-session"])

    async def autofill_login(self, tab: Tab, *, submit: bool = False) -> bool:
        """Fill the current page's login form from a saved login for its host.

        pymanakin acts as the autofiller itself (Chrome won't autofill an
        injected password), so this works where seeding ``Login Data`` doesn't.
        Returns whether a form was filled. With the first matching login's
        credentials; an ambiguous multi-account page isn't resolved.
        """
        if self._logins is None:
            self._logins = bp.extract_logins(self._profile, keyring=self._keyring)

        page_host = urlsplit(await tab.current_url()).hostname or ""
        login = next((l for l in self._logins
                      if l.password and _host_matches(urlsplit(l.url).hostname or "", page_host)), None)
        if login is None:
            return False

        password_field = await tab.query("input[type=password]", raise_exc=False)
        if password_field is None or not await password_field.is_visible():
            return False

        if login.username:
            for selector in _USERNAME_SELECTORS:
                field = await tab.query(selector, raise_exc=False)
                if field is not None and await field.is_visible():
                    await field.click()
                    await field.type_text(login.username)
                    break

        await password_field.click()
        await password_field.type_text(login.password)
        if submit:
            await password_field.type_text("\n")
        return True
