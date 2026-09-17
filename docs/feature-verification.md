# Demo feature verification

## Scope and boundaries

Tests run against disposable SQLite data and mock billing. Settings email tests
use an in-memory backend: they test validation, attachment handling and feedback,
not real delivery. No production accounts, payments or external messages are used.
Passing tests establish the cases below, not a guarantee that every input,
browser, integration or accessibility requirement is covered.

This repository is intentionally a portfolio demo. Live payment, external OAuth,
SMTP delivery and paid AI are outside the supported reviewer flow rather than
features that a reviewer is expected to configure.

## Coverage map

| Feature family | Coverage |
| --- | --- |
| Demo entry/login | Portfolio-demo messaging; local demo sign-in; external provider buttons absent; mock login disabled when mock mode is explicitly overridden off |
| Settings theme | Profile/session persistence; invalid values; AJAX save; desktop form; mobile navigation toggle; appearance after reload/navigation |
| Settings assistant persona | Every available choice persists; invalid selection preserves current value; browser description and reload |
| Settings celebrations | Both directions persist; browser checkbox reload |
| Settings CSV/JSON exports | Download content and headers; ownership filtering; zero-grade preservation; export history |
| Backup/import | JSON round trip including completion and zero marks; invalid restore preserves original records; same-name modules at different levels survive full CSV import |
| Settings support forms | Required fields/attachment; bug attachment and feedback submission; visible email failure; in-memory mail only |
| Settings navigation | Help, methodology, release notes, privacy dashboard, subscription page; deletion confirmation and Cancel |
| Account deletion | Disposable owner's data removed; another account preserved; failed subscription cancellation blocks deletion; no live account deletion |
| Modules | Add/search/edit/reload/delete browser flow; numeric/name validation; duplicates; ownership |
| GCSE | Revision add/delete and invalid date; paper add/edit/clear/delete and invalid scores; checklist toggle; ownership |
| College | UCAS offer add/update/delete; statement progress; checklist toggle; ownership |
| Predictions | Existing calculation tests; What-If browser submit/results and JSON/ICS content; report/export backend tests |
| Dashboard planner | Existing goals, deadline edit/snooze/move/complete and AI-plan browser flows; backend dashboard tests |
| Free/Premium gating | Free user redirects from Premium-only report; mock monthly/yearly checkout changes the real application plan state; Premium-only report then opens; immediate Return to Free restores the gate |
| Mock billing safety | Checkout URL remains same-site; no Stripe URL is returned; invalid plan values fall back safely; success page confirms no card was charged |
| Demo assistant safety | Premium mentor path returns a deterministic local response in mock mode; external OpenAI client construction is blocked even if an API key is present |
| Premium/admin | Mock upgrade/cancel, free-user gating, assistant session and admin authorization tests |
| Student pages | Premium/free rendering sweep of 19 named student destinations |
| Visual consistency | Seven screens at desktop/phone widths in two themes; JS-error/overflow checks and review captures |

## Known limits / follow-up coverage

- Live Google/GitHub/Microsoft sign-in, Stripe checkout/webhooks/portal, SMTP
  delivery and paid AI responses are intentionally not part of the supported demo.
  They should not be enabled merely to review this repository.
- Malformed CSV imports, all AI widget interactions, voice output,
  complete UCAS scenario matrices and every admin action are not yet exhaustively tested.
- Backup History currently displays illustrative demo entries, not a durable historical backup store.
- Scheduled jobs have unit coverage; production scheduler/broker operation is not established or required for the portfolio demo.
- Screenshot captures are not pixel baselines, a contrast audit or a complete
  accessibility audit. Firefox, WebKit and physical-device behavior remain unverified.

Run `python manage.py test` and `npm run test:e2e`. GitHub Actions publishes
PNG-only review artifacts for seven days. Do not publish `.auth` storage states,
databases or credentials in artifacts.
