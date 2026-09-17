# PredictMyGrade Demo Review Checklist

Use this checklist for a portfolio walkthrough. It is designed to prove the demo behaves like a product without relying on live billing or paid integrations.

## Entry

- Open `/accounts/login/`.
- Confirm the page is labelled **Portfolio demo**.
- Confirm **Continue as Demo User** is the only sign-in action presented.
- Confirm Google, GitHub and Microsoft sign-in buttons are not shown.

## Free plan

- Open a Premium-only feature such as `/reports/ai/`.
- Confirm the Free account is redirected to the Upgrade screen.
- Confirm pricing/upgrade copy states that checkout is simulated and no payment method is collected.

## Premium plan

- Start the Demo Monthly or Demo Yearly plan.
- Confirm the success page says no card was charged.
- Re-open the Premium-only feature and confirm it loads.
- Open `/manage-subscription/` and confirm plan switching controls are available.

## Return to Free

- Choose **Return to Free**.
- Confirm the plan screen shows **Free Demo Plan**.
- Re-open the Premium-only feature and confirm the Upgrade gate is restored.

## Demo safety

- The checkout response must stay on the same application host.
- No real payment processor page should appear.
- Demo assistant responses should state that they are simulated.
- Supplying an OpenAI key must not enable external AI while mock billing is on.
- Use throwaway/sample academic data only.

## Automated coverage

The backend tests cover local checkout URLs, Premium activation, immediate demo downgrade, feature-gate restoration, and external-AI blocking. `e2e/demo-mode.spec.js` covers the public demo login surface and the browser-level Free -> Premium -> Free walkthrough.
