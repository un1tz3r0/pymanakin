# pymanakin

Automate a browser that's already logged in. pymanakin combines
[browserprofile](https://github.com/un1tz3r0/browserprofile), which reads your
desktop browser profile, with [pydoll](https://github.com/autoscrape-labs/pydoll),
which drives Chrome, so an automated session behaves the way your normal browser
would.

```python
import asyncio

import browserprofile as bp
from pymanakin import ProfiledChrome


async def main():
    profile = bp.list_profiles("chrome")[0]
    async with ProfiledChrome(profile) as browser:
        tab = await browser.start()
        await tab.go_to("https://github.com/")


asyncio.run(main())
```

`ProfiledChrome` is a pydoll `Chrome`, so everything else works as usual. Pass
`options=` for pydoll's `ChromiumOptions` and `keyring=` to force a Linux keyring
backend (see the browserprofile README).

## What gets carried over

| Data | How |
|---|---|
| Cookies, including HttpOnly | Decrypted, then set over CDP before the first navigation |
| localStorage, IndexedDB | Copied into the temporary profile before launch |

Your source browser can stay open, and nothing done in a session reaches your
real profile: the copy is deleted when the browser stops.

## Limitations

- **Saved passwords aren't carried over.** Sites that keep you logged in through
  cookies or storage work; sites that rely on the browser autofilling a saved
  password don't.
- **Headless mode can be blocked.** Cloudflare challenged headless Chrome on
  codepen.io even with a valid session, while pydoll's default visible window
  got straight through.
- **Partitioned cookies lose their partition.** browserprofile doesn't read the
  partition key, so they are replayed as ordinary cookies.
- **Chromium-based profiles only.** Firefox storage would have to be parsed, not
  copied.
- **All sites, every start.** Storage is copied whole, because Chromium keeps
  every site's localStorage in one database.
- **Your sessions are exposed while it runs.** pydoll's debugging port has no
  authentication, so any local process can use your logged-in sessions for as
  long as the browser is open.
