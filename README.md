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

### Saved passwords

Chrome won't autofill a password injected from outside, so `ProfiledChrome` does
the autofilling itself: on a login page, it types a saved login for that host.

```python
await tab.go_to("https://example.com/login")
await browser.autofill_login(tab)            # submit=True to press Enter after
```

It fills the first saved login matching the page's host, and isn't meant to
resolve a page with several accounts.

### Checkpoint / diff a session

See what sites changed over a stretch of browsing:

```python
import browserprofile as bp

before = await browser.checkpoint()          # live cookies, hashed by default
await tab.go_to("https://example.com/login")
await browser.autofill_login(tab, submit=True)
after = await browser.checkpoint()
delta = bp.diff_checkpoints(before, after)   # which cookies were added/changed
```

`checkpoint(secrets=...)` takes the same `hash` / `exclude` / `decrypted` modes as
browserprofile's CLI.

### Racing page conditions

On a dynamic page, a point in a script often has several possible outcomes. `race`
arms a set of conditions and returns the first that fires, tearing the rest down:

```python
from pymanakin import race, Dom, Gone, Response, Navigation, Predicate

hit = await race(tab, {
    "results": Dom("#results li"),
    "empty": Dom(".no-results", text="Nothing found"),
    "loaded": Gone(".spinner"),
    "api": Response("/search?", status=200),
    "redirected": Navigation("/login"),
    "captcha": Predicate("!!document.querySelector('iframe[src*=captcha]')"),
}, timeout=10)

if hit.label == "captcha":
    ...
```

The leaves:

| Leaf | Fires when | `data` |
|---|---|---|
| `Dom(selector, text=…)` | the selector matches (with that text) | the selector |
| `Gone(selector)` | the selector matches nothing | the selector |
| `Response(url_substring, status=…)` | a network response matches | the URL |
| `Navigation(url_substring=…)` | the main frame navigates | the new URL |
| `Predicate(js, interval=…)` | a JS expression is truthy | the value |

`Dom` and `Gone` use a `MutationObserver` with a scan on arm (an element already
present, or already absent, fires at once) and fire once. `Response` and
`Navigation` ride CDP events and fire on every match. `Predicate` polls and fires
once. `stream` keeps the conditions armed and yields hits as they arrive:

```python
async for hit in stream(tab, {"row": Dom("tr.item"), "done": Dom("#complete")}):
    if hit.label == "done":
        break
```

A condition is any object with `arm` / `disarm` (the `Leaf` protocol), so your
own is a small class away.

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
