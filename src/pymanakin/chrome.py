"""pydoll Chrome seeded with a copy of the user's real browser profile.

Each source of session state goes in by the route that survives Chromium's
encryption. localStorage and IndexedDB aren't encrypted with the OS key, so they
are copied into the temp profile before launch. Cookies are, so they are
decrypted by browserprofile and replayed over CDP before the first navigation,
which works whichever key the new browser ends up using.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import browserprofile as bp
from pydoll.browser.chromium import Chrome
from pydoll.exceptions import CommandFailed

if TYPE_CHECKING:
    from pydoll.browser.options import ChromiumOptions
    from pydoll.browser.tab import Tab
    from pydoll.protocol.network.types import CookieParam

logger = logging.getLogger(__name__)


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
