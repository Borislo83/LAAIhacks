# Progress and Amazon cart handoff

- Dashboard: `http://localhost:5173/dashboard.html`; agents `:8000`, orchestrator `:8001`.
- Chat/Direct show actual agent stages over authenticated SSE, partial results, and an activity log.
- Amazon buyer accepts a product name or a full HTTPS Amazon US product URL. Select a listing, then approve **Add to real Amazon cart**.
- Executor opens a dedicated local Chrome profile, checks the ASIN, adds the item, verifies it in the active cart, and stops. It cannot click checkout, Buy Now, payment, or order submission.
- Signed-out carts work in that dedicated browser session. Amazon verification/sign-in may require manual attention. The normal laptop browser does not automatically share this cart.
- The same private `AMAZON_CART_EXECUTOR_TOKEN` must be in agents/orchestrator `.env` files. Local files are configured and ignored by Git. Never put this token in the frontend.
- `AMAZON_BROWSER_PROFILE` defaults to ignored `.runtime/amazon-profile`. Chrome must be installed; Python dependencies include Playwright.
- Live verified: quantity 1, Sony WH-1000XM5 ASIN `B09XS7JWHH`, fresh signed-out profile. Screenshot showed empty cart before, one item afterward, and “Hello, sign in.” No order or checkout.
- Live test exposed repeated hidden ASIN fields and `add-to-cart-button-ubb`; the executor now handles both. Regression tests cover these, wrong variants, uncertain outcomes, and duplicate prevention.
- Local screenshots/result: `/tmp/laai-amazon-last-result.json` contains the successful run's artifact paths. These are local evidence, not checked-in account/browser data.
- Validation before handoff: 32 agent tests and 30 orchestrator tests pass; browser fixture confirms progress arrives before final answer and cart review/confirmation render correctly.
- Deliberate limitations: no cloud browser, no CAPTCHA bypass, no automatic retry of uncertain cart changes, and no purchase/order implementation.

Entry points: `services/agents/src/laai_agents/amazon_cart.py`, `buyer.py`, `progress.py`; orchestrator `progress.py` and streaming routes; `apps/dashboard/prototype/dashboard.html`.
