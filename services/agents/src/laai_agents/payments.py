"""Agent Payment Protocol (seller role) with Stripe embedded Checkout in test mode.

The price watch is the paid feature (Focal Pro). With sk_test_/pk_test_ keys no real
money moves: pay with Stripe's test card 4242 4242 4242 4242. Live keys are refused
unless FOCAL_ALLOW_LIVE_PAYMENTS=1. The seller verifies the Checkout Session is paid with
Stripe before it sends CompletePayment.
"""

import os
import time

import aiohttp
from uagents_core.contrib.protocols.payment import Funds, RequestPayment

from . import net

STRIPE_API = "https://api.stripe.com/v1"


def amount_cents() -> int:
    return int(os.getenv("FOCAL_WATCH_PRICE_CENTS", "99"))


def configured() -> bool:
    secret = os.getenv("STRIPE_SECRET_KEY", "")
    public = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    if not (secret and public):
        return False
    live = secret.startswith("sk_live") or public.startswith("pk_live")
    return not live or os.getenv("FOCAL_ALLOW_LIVE_PAYMENTS") == "1"


def test_mode() -> bool:
    return os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_test")


def _auth() -> aiohttp.BasicAuth:
    return aiohttp.BasicAuth(os.getenv("STRIPE_SECRET_KEY", ""), "")


async def create_checkout(description: str, reference: str) -> dict:
    return_url = os.getenv("STRIPE_RETURN_URL", "https://asi1.ai/") + "?session_id={CHECKOUT_SESSION_ID}"
    form = {
        "ui_mode": "embedded",
        "redirect_on_completion": "if_required",
        "mode": "payment",
        "payment_method_types[0]": "card",
        "return_url": return_url,
        "expires_at": str(int(time.time()) + 1800),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(amount_cents()),
        "line_items[0][price_data][product_data][name]": "Focal Pro price watch",
        "line_items[0][price_data][product_data][description]": description[:300],
        "metadata[reference]": reference,
        "metadata[service]": "focal_price_watch",
    }
    async with net.session().post(
        f"{STRIPE_API}/checkout/sessions",
        data=form,
        auth=_auth(),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as response:
        body = await response.json()
        if response.status != 200:
            raise RuntimeError(body.get("error", {}).get("message", "Stripe error"))
    return {
        "client_secret": body["client_secret"],
        "id": body["id"],
        "checkout_session_id": body["id"],
        "publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
        "currency": "usd",
        # RequestPayment metadata values must be strings.
        "amount_cents": str(amount_cents()),
        "ui_mode": "embedded",
    }


async def is_paid(checkout_session_id: str) -> bool:
    if not checkout_session_id.startswith("cs_"):
        return False
    async with net.session().get(
        f"{STRIPE_API}/checkout/sessions/{checkout_session_id}",
        auth=_auth(),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as response:
        if response.status != 200:
            return False
        body = await response.json()
    return body.get("payment_status") == "paid"


def payment_request(recipient: str, reference: str, description: str, checkout: dict) -> RequestPayment:
    return RequestPayment(
        accepted_funds=[
            Funds(currency="USD", amount=f"{amount_cents() / 100:.2f}", payment_method="stripe")
        ],
        recipient=recipient,
        deadline_seconds=900,
        reference=reference,
        description=description,
        metadata={"stripe": checkout, "service": "focal_price_watch"},
    )
