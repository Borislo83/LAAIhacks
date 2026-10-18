"""Visible browser executor limited to adding an explicitly selected Amazon ASIN.

Dedicated app-owned profile; never reads the user's existing browser profile.
Only the Add to Cart control is clicked. Success requires the ASIN in the active cart.
No checkout/payment/order controls are used.
"""

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .progress import report

_lock = asyncio.Lock()
_playwright = None
_browser = None
# One reused tab: new tabs per approval would leak, and closing it would hide the
# sign-in or verification the user is asked to finish there.
_page = None

CART_URL = "https://www.amazon.com/gp/cart/view.html"
NEEDS_SIGN_IN = {
    "status": "needs_sign_in",
    "message": "Sign in to your Amazon account in the opened Chrome window (only needed once), "
    "then press Add to Amazon cart again. Nothing was added.",
}


def guest_allowed() -> bool:
    """Opt-in demo mode: a signed-out window adds to Amazon's guest cart in that browser
    instead of stopping for sign-in. The item is not in anyone's Amazon account."""
    return os.environ.get("AMAZON_CART_GUEST", "").strip() == "1"


def _on_sign_in(page) -> bool:
    # Amazon's sign-in lives at /ap/signin or, in its newer flow, /ax/claim/...
    return bool(re.search(r"/(?:ap/signin|ax/claim)", urlsplit(page.url).path))


async def _signed_out(page) -> bool:
    """Amazon's header greets a signed-out visitor with "Hello, sign in"."""
    greeting = page.locator("#nav-link-accountList-nav-line-1").first
    if not await greeting.count():
        return False
    return "sign in" in (await greeting.inner_text()).lower()


# Amazon's add-on offer after Add to Cart: the side sheet or modal, and its decline control.
ADD_ON_SHEET = (
    "#attach-warranty-pane, #attach-desktop-sideSheet, #attachDisplayAddBaseAlert, "
    "[id^='attach-warranty'], #abb-intl-pop-table"
)
DECLINE = (
    "#attachSiNoCoverage, #attachSiNoCoverage input, input[aria-labelledby='attachSiNoCoverage-announce'], "
    "#siNoCoverage-announce, #abb-intl-decline"
)


async def _decline_add_ons(page, wait_ms: int = 6000) -> bool:
    """If Amazon offers protection or add-ons, press its "No thanks". True when declined."""
    from playwright.async_api import TimeoutError

    try:
        await page.wait_for_selector(f"{ADD_ON_SHEET}, {DECLINE}", timeout=wait_ms, state="visible")
    except TimeoutError:
        return False
    decline = page.locator(DECLINE).first
    if not await decline.count() or not await decline.is_visible():
        sheet = page.locator(ADD_ON_SHEET).first
        if not await sheet.count() or not hasattr(page, "get_by_role"):
            return False
        # Amazon renames its controls; inside the offer, "No thanks" is the decline.
        decline = sheet.get_by_role("button", name=re.compile(r"^\s*no thanks\s*$", re.IGNORECASE)).first
        if not await decline.count():
            decline = sheet.get_by_text(re.compile(r"^\s*no thanks\s*$", re.IGNORECASE)).first
        if not await decline.count():
            return False
    await decline.click(timeout=10000)
    report("declined_add_ons", "Declined Amazon's add-on offer; only the approved item is added")
    return True


def _cart_item(page, asin: str):
    return page.locator(
        f'#sc-active-cart [data-asin="{asin}"], #activeCartViewForm [data-asin="{asin}"]'
    ).first


async def add_to_cart(url: str, quantity: int, require_new: bool = False):
    """`require_new`: refuse when only Amazon's used buy box ("Buy Used") can add it.
    Once the item is in the cart, the cart window comes to the front on the cart page, so
    the guest cart (which lives only in this window's own profile) can be seen."""
    outcome = await _add_to_cart(url, quantity, require_new)
    if outcome.get("status") in ("cart_added", "needs_sign_in"):
        await _show_window()
    return outcome


# A bar along the bottom of every page and a title prefix: this is the glasses' cart
# window (its own profile and guest cart), not the user's Chrome. The bar lets clicks
# through, so it never gets in the way of the add.
MARKER_SCRIPT = """
(() => {
  const mark = () => {
    if (!document.title.startsWith('FOCAL CART')) document.title = 'FOCAL CART · ' + document.title;
    if (!document.body || document.getElementById('focal-cart-bar')) return;
    const bar = document.createElement('div');
    bar.id = 'focal-cart-bar';
    bar.textContent = 'FOCAL CART · the glasses demo cart (guest, separate from your Chrome) · checkout is never started';
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;pointer-events:none;' +
      'background:#ff6a00;color:#fff;font:600 13px/1.2 -apple-system,Helvetica,Arial,sans-serif;' +
      'padding:8px 14px;text-align:center;letter-spacing:.04em;box-shadow:0 -2px 8px rgba(0,0,0,.25)';
    document.body.appendChild(bar);
  };
  document.addEventListener('DOMContentLoaded', mark);
  setInterval(mark, 1000);
})();
"""


async def _side_of_screen() -> tuple[int, int, int, int]:
    """The right ~42% of the main screen (x, y, width, height), for the cart window."""
    width, height = 1512, 982
    try:
        probe = await asyncio.create_subprocess_exec(
            "osascript", "-l", "JavaScript", "-e",
            'ObjC.import("AppKit"); const f = $.NSScreen.mainScreen.visibleFrame; '
            "`${f.size.width},${f.size.height}`",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out = (await asyncio.wait_for(probe.communicate(), 5))[0].decode().strip()
        width, height = (int(float(v)) for v in out.split(","))
    except Exception:  # noqa: BLE001 -- a default size is fine
        pass
    side = max(560, int(width * 0.42))
    return width - side, 25, side, height - 25


async def _show_window():
    """Best effort: the cart tab in front, and its Chrome window above the user's Chrome
    (the same app, so it is raised by process id). Never fails the add."""
    try:
        if _page is not None and not _page.is_closed():
            await _page.bring_to_front()
        profile = str(Path(os.getenv("AMAZON_BROWSER_PROFILE", ".runtime/amazon-profile")).resolve())
        find = await asyncio.create_subprocess_exec(
            "ps", "-axo", "pid=,command=", stdout=asyncio.subprocess.PIPE
        )
        lines = (await find.communicate())[0].decode().splitlines()
        # The browser's own process: its helpers carry --type=renderer, gpu-process, ...
        pid = next((ln.split()[0] for ln in lines
                    if f"user-data-dir={profile}" in ln and "--type=" not in ln), "")
        if pid.isdigit():
            raise_it = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(raise_it.wait(), 5)
    except Exception:  # noqa: BLE001 -- showing the window is a convenience
        pass


async def _add_to_cart(url: str, quantity: int, require_new: bool = False):
    from playwright.async_api import Error, TimeoutError, async_playwright

    from .buyer import amazon_listing

    canonical = amazon_listing(url)
    if not canonical or not 1 <= quantity <= 20:
        return {"status": "blocked", "message": "Invalid Amazon listing or quantity"}
    asin = canonical.rsplit("/", 1)[1]
    async with _lock:
        global _playwright, _browser, _page
        try:
            if _playwright is None:
                _playwright = await async_playwright().start()
            # The profile stays locked while its context is open, so a live context with
            # no tabs gets a new tab below rather than a second launch.
            if _browser is None:
                profile = Path(
                    os.getenv("AMAZON_BROWSER_PROFILE", ".runtime/amazon-profile")
                ).resolve()
                profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                report("opening_browser", "Opening the dedicated Amazon cart browser")
                x, y, width, height = await _side_of_screen()
                _browser = await _playwright.chromium.launch_persistent_context(
                    str(profile),
                    channel="chrome",
                    headless=False,
                    # Sized to the window, which sits on the right of the screen.
                    no_viewport=True,
                    args=[f"--window-position={x},{y}", f"--window-size={width},{height}",
                          "--hide-crash-restore-bubble"],
                )
                # Marked so it is never mistaken for the user's own Chrome.
                await _browser.add_init_script(MARKER_SCRIPT)
                _browser.on("close", _reset_browser)
            if _page is None or _page.is_closed():
                # The window opens with one blank tab: use it rather than adding a second.
                blank = [p for p in _browser.pages if p.url == "about:blank"]
                _page = blank[0] if blank else await _browser.new_page()
            page, cart_url = _page, CART_URL
            report(
                "checking_cart",
                "Checking whether the selected item is already in the cart",
            )
            await page.goto(cart_url, wait_until="domcontentloaded", timeout=45000)
            if _on_sign_in(page):
                return dict(NEEDS_SIGN_IN)
            signed_out = await _signed_out(page)
            guest = signed_out and guest_allowed()
            if signed_out and not guest:
                # A signed-out add lands in a guest cart, not the user's account: open
                # Amazon's sign-in in this window and stop.
                link = page.locator("#nav-link-accountList").first
                if await link.count():
                    await link.click(timeout=10000)
                return dict(NEEDS_SIGN_IN)
            try:
                await page.wait_for_selector(
                    "#sc-active-cart, #activeCartViewForm, #sc-empty-cart, .sc-empty-cart",
                    timeout=10000,
                )
            except TimeoutError:
                return {
                    "status": "needs_attention",
                    "message": "Amazon's cart could not be read. Complete any sign-in or verification in the opened browser; no add was attempted.",
                }
            existing = _cart_item(page, asin)
            if await existing.count() and await existing.is_visible():
                return {
                    "status": "cart_added",
                    "cart_url": cart_url,
                    "message": "The selected item is already in your Amazon cart. No duplicate was added. Review quantity on Amazon; stopped before checkout.",
                }
            report("opening_listing", "Opening the exact approved Amazon listing")
            await page.goto(canonical, wait_until="domcontentloaded", timeout=45000)
            if _on_sign_in(page):
                return dict(NEEDS_SIGN_IN)
            if await page.locator(
                '#captchacharacters, form[action*="validateCaptcha"]'
            ).count():
                return {
                    "status": "needs_attention",
                    "message": "Complete Amazon's verification in the opened browser. No cart addition was attempted.",
                }
            if urlsplit(page.url).path.startswith("/checkpoint"):
                # Amazon's security checkpoint; a person must complete it.
                return {
                    "status": "needs_attention",
                    "message": "Complete Amazon's security checkpoint in the opened browser, then prepare and approve again. No cart addition was attempted.",
                }
            if amazon_listing(page.url) != canonical:
                # Host and path only: the query string can carry session data.
                landed = urlsplit(page.url)
                return {
                    "status": "blocked",
                    "message": "Amazon redirected away from the approved product "
                    f"(to {landed.hostname}{landed.path[:80]}). No cart change was attempted.",
                }
            report(
                "verifying_listing",
                "Checking ASIN and requested quantity before adding",
            )
            asin_field = page.locator("#ASIN")
            # Amazon repeats the same hidden ASIN for its multiple buy-box forms.
            asin_values = {
                (await asin_field.nth(i).input_value()).upper()
                for i in range(await asin_field.count())
            }
            if asin_values != {asin}:
                return {
                    "status": "blocked",
                    "message": "Amazon did not show the approved ASIN. Select the exact product variant and try again.",
                }
            selector = page.locator("select#quantity:visible").first
            if await selector.count():
                await selector.select_option(str(quantity), timeout=5000)
            elif quantity != 1:
                return {
                    "status": "blocked",
                    "message": "Amazon's quantity selector could not be verified. No item was added.",
                }
            button = page.locator("#add-to-cart-button:visible").first
            if not await button.count():
                if require_new:
                    return {
                        "status": "blocked",
                        "message": "Amazon now offers this listing only used. "
                        "The approved item was new, so nothing was added.",
                    }
                button = page.locator("#add-to-cart-button-ubb:visible").first
            if (
                await button.count() != 1
                or not await button.is_visible()
                or not await button.is_enabled()
            ):
                return {
                    "status": "needs_attention",
                    "message": "Add to Cart is unavailable. Review the listing in the opened Amazon browser.",
                }
            report("adding_to_cart", "Adding the approved item to the real Amazon cart")
            await button.click(timeout=15000)
            # Amazon may first offer protection or add-ons ("Add to your order":
            # AppleCare+, warranties). Only the approved item is added: always decline.
            await _decline_add_ons(page)
            # Wait for Amazon's add-to-cart response before navigating to the cart.
            try:
                await page.wait_for_selector(
                    "#huc-v2-order-row-confirm-text, #NATC_SMART_WAGON_CONF_MSG_SUCCESS, "
                    "#sw-atc-confirmation, #attach-added-to-cart-message",
                    timeout=15000,
                )
            except TimeoutError:
                # The active cart is the authority if Amazon changes its success banner.
                pass
            report("verifying_cart", "Checking the real cart for the approved ASIN")
            await page.goto(cart_url, wait_until="domcontentloaded", timeout=45000)
            item = _cart_item(page, asin)
            try:
                await item.wait_for(state="visible", timeout=10000)
            except TimeoutError:
                return {
                    "status": "cart_unverified",
                    "message": "The add was attempted, but the item could not be verified in the active cart. Inspect the opened browser before retrying.",
                }
            report(
                "cart_added",
                "Verified the selected item in the real Amazon cart; stopping here",
            )
            return {
                "status": "cart_added",
                "cart_url": cart_url,
                "guest": guest,
                "message": (
                    "Item verified in the guest cart of the laptop's cart window (not signed in "
                    "to an Amazon account). Stopped before checkout. No order was placed."
                    if guest
                    else "Item verified in your Amazon cart. Stopped before checkout. No order was placed."
                ),
            }
        except (Error, OSError):
            return {
                "status": "cart_unverified",
                "message": "Browser automation could not finish. Inspect the Amazon cart before retrying; no order was submitted.",
            }


def _reset_browser(*_):
    global _browser, _page
    _browser = _page = None
