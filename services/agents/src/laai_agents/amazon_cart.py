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


async def add_to_cart(url: str, quantity: int):
    from playwright.async_api import Error, TimeoutError, async_playwright

    from .buyer import amazon_listing

    canonical = amazon_listing(url)
    if not canonical or not 1 <= quantity <= 20:
        return {"status": "blocked", "message": "Invalid Amazon listing or quantity"}
    asin = canonical.rsplit("/", 1)[1]
    async with _lock:
        global _playwright, _browser
        try:
            if _playwright is None:
                _playwright = await async_playwright().start()
            if _browser is None or not _browser.pages:
                profile = Path(
                    os.getenv("AMAZON_BROWSER_PROFILE", ".runtime/amazon-profile")
                ).resolve()
                profile.mkdir(parents=True, exist_ok=True, mode=0o700)
                report("opening_browser", "Opening the dedicated Amazon cart browser")
                _browser = await _playwright.chromium.launch_persistent_context(
                    str(profile),
                    channel="chrome",
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                )
                _browser.on("close", _reset_browser)
            page = await _browser.new_page()
            cart_url = "https://www.amazon.com/gp/cart/view.html"
            report(
                "checking_cart",
                "Checking whether the selected item is already in the cart",
            )
            await page.goto(cart_url, wait_until="domcontentloaded", timeout=45000)
            # Amazon's sign-in lives at /ap/signin or, in its newer flow, /ax/claim/...
            if re.search(r"/(?:ap/signin|ax/claim)", urlsplit(page.url).path):
                return {
                    "status": "needs_sign_in",
                    "message": "Sign in in the opened Amazon browser, then prepare and approve again.",
                }
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
            existing = page.locator(
                f'#sc-active-cart [data-asin="{asin}"], #activeCartViewForm [data-asin="{asin}"]'
            ).first
            if await existing.count() and await existing.is_visible():
                return {
                    "status": "cart_added",
                    "cart_url": cart_url,
                    "message": "The selected item is already in your Amazon cart. No duplicate was added. Review quantity on Amazon; stopped before checkout.",
                }
            report("opening_listing", "Opening the exact approved Amazon listing")
            await page.goto(canonical, wait_until="domcontentloaded", timeout=45000)
            # Amazon's sign-in lives at /ap/signin or, in its newer flow, /ax/claim/...
            if re.search(r"/(?:ap/signin|ax/claim)", urlsplit(page.url).path):
                return {
                    "status": "needs_sign_in",
                    "message": "Sign in in the opened Amazon browser, then prepare and approve again.",
                }
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
            item = page.locator(
                f'#sc-active-cart [data-asin="{asin}"], '
                f'#activeCartViewForm [data-asin="{asin}"]'
            ).first
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
                "message": "Item verified in your Amazon cart. Stopped before checkout. No order was placed.",
            }
        except (Error, OSError):
            return {
                "status": "cart_unverified",
                "message": "Browser automation could not finish. Inspect the Amazon cart before retrying; no order was submitted.",
            }


def _reset_browser(*_):
    global _browser
    _browser = None
