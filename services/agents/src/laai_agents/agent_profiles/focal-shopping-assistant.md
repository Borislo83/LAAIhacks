# Focal Shopping Assistant

![tag:shopping](https://img.shields.io/badge/shopping-3D8BD3) ![tag:multi-agent](https://img.shields.io/badge/multi--agent-3D8BD3) ![tag:price-comparison](https://img.shields.io/badge/price--comparison-3D8BD3)

**Should I buy it?** Name a product and a budget. Focal's team of agents researches it, checks every price, adds your pick to your Amazon cart, drafts a message to a friend, and keeps watching the price for you, all inside one ASI:One conversation.

## Try it
- "Should I buy the Sony WH-1000XM5 under $350?"
- "Compare AirPods Pro 2 prices and find a cheaper alternative"
- "Is the Bose QuietComfort Ultra worth it?"

## How it works
1. **Memory agent** loads your shopping profile and past decisions.
2. If the budget is missing, a **form card** asks for budget, priority and use case.
3. The **price**, **reviews** and **alternatives** agents work in parallel. Each result is posted to the chat as it arrives.
4. The **verifier agent** audits every price. If nothing trustworthy is left, it sends the price agent back for a deeper search.
5. A **comparison card** shows your pick against the top rival.
6. The **buyer agent** finds the Amazon listing, and a **review card** asks you to approve the cart addition. Checkout is never automated.
7. The **messenger agent** drafts an iMessage for you to approve.
8. Last, a **price watch** paid through the Agent Payment Protocol (Stripe). The team re-checks the price and messages you when it drops.

## Protocols
Agent Chat Protocol with ASI:One Interactive Cards (form, custom, review) · Agent Payment Protocol (seller, Stripe) · typed agent-to-agent messages to 8 specialist agents.

## Safety
Nothing is added to a cart or sent without your approval on a card. Every price comes with its source, and nothing is made up when a source is missing.
