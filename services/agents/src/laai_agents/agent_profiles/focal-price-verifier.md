# Focal Price Verifier

![tag:shopping](https://img.shields.io/badge/shopping-3D8BD3) ![tag:verification](https://img.shields.io/badge/verification-3D8BD3)

Audits price offers before anyone acts on them. It keeps high- and medium-trust offers, drops low-trust leads and statistical outliers (more than 35% from the median), and flags single-store prices, search-listing-only prices, and anything over budget. If nothing trustworthy is left, it asks the price agent for a deeper store search.

It never invents a price; it only filters and explains.

Input: a `PriceCompareResponse` and an optional budget. Output: kept and dropped offers with reasons, flags, the best verified offer, and a verdict (`verified`, `caution` or `rejected`).

Part of the Focal team; the coordinator is `FOCAL`.
