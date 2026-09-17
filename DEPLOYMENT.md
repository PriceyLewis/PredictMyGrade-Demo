# Demo Hosting Notes

PredictMyGrade-Demo is a portfolio project, not a production-ready service. This file is retained only to explain how the demo may be run in a private review environment; it is **not** a launch runbook.

## Demo Boundary

The current repository deliberately keeps the product experience mock-first:

- billing is local and simulated only
- checkout never sends a reviewer to a real payment processor
- Premium state can be enabled and removed locally to demonstrate feature gating
- assistant replies use deterministic demo responses while mock billing is enabled
- external AI client construction is blocked in demo mode even if an API key is accidentally supplied
- third-party OAuth buttons are intentionally removed from the reviewer login experience
- legal/privacy/cookie pages are portfolio UI, not production policy or compliance controls

## Running The Demo Privately

For local or private portfolio review:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

The example environment uses SQLite and mock billing. Keep real payment, OAuth, SMTP and AI-provider credentials out of the demo environment.

## What A Real Product Would Require

Turning this repository into a live service should be treated as a separate engineering project rather than a configuration change. At minimum that work would need independent design and review for:

- hosting, networking and infrastructure
- secrets and key management
- production database operations and backups
- payment provider integration, webhooks, reconciliation and refunds
- identity/account recovery and verified authentication
- email delivery and domain ownership
- logging, monitoring, alerting and incident response
- security review and penetration testing
- privacy, data retention, age-related requirements and consent
- legal terms and prediction disclaimers
- customer support, moderation and abuse handling
- accessibility and cross-browser/device validation

## Portfolio Recommendation

Keep this repository as the safe demonstration build. If PredictMyGrade is ever developed into a real service, create a separate production workstream/repository with its own threat model, infrastructure, integrations, policies and launch criteria instead of enabling live services here.
