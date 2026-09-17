# Demo Mode Architecture

PredictMyGrade-Demo deliberately separates portfolio product behaviour from live-service integrations.

## Billing

`BILLING_MOCK_MODE` is enabled in the demo configuration. Checkout creates a same-site URL that leads to the local payment-success view. Completing that route changes `UserProfile.is_premium` and `plan_type`; cancellation changes them back to the Free state. The same fields are used by the Premium decorators and template context, so the walkthrough exercises real feature-gating code without using a payment provider.

## Assistant

While mock billing is enabled, `core.tasks.fetch_chat_completion` returns a deterministic local `OpenAIResponse`. `OpenAIClient` also refuses to initialise in that mode. This provides two safeguards against accidental paid API traffic while keeping the Premium mentor flow demonstrable.

## Authentication

The reviewer-facing login template exposes only the local demo account flow. OAuth plumbing remains in the historical application architecture, but provider actions are intentionally not presented as part of the portfolio experience.

## Data

The demo account uses the normal Django database and normal application models. Changes therefore persist for the session/account and exercise the same CRUD, import/export and settings logic as other users. Reviewers should only enter throwaway data.

## Automated verification

- `core/tests/test_auth.py`: demo-only sign-in surface and feature gating
- `core/tests/test_billing.py`: local mock checkout, Premium activation, plan controls and downgrade back to Free
- `core/tests/test_demo_ai.py`: deterministic assistant response and external-AI guard
- `e2e/demo-mode.spec.js`: browser-level login and Free -> Premium -> Free walkthrough
