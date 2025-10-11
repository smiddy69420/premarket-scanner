# Project Brief & Open Questions

## Current Understanding
- The project powers a monetized Discord community centered on actionable premarket trading insights.
- Two Render services exist:
  - **premarket-scanner** exposes interactive Discord slash commands like `/scan_ticker`, `/earnings_watch`, and `/analyze`.
  - **premarket-cron** pushes scheduled call/put option alerts aimed at short-term trading opportunities.
- Alerts in the `#scans` Discord channel must deliver consistent value to attract and retain paying members.
- The current infrastructure budget is capped at roughly $20/month, with $7 already committed to Render's starter plan, leaving about $13/month for additional services.

## Ideas to Explore
- Expand data coverage (e.g., futures, commodities, major ETFs) to widen appeal.
- Offer tiered alerting with configurable risk profiles (conservative vs. aggressive plays).
- Add educational context (e.g., why an alert triggered, glossary links) to build user trust.
- Track alert outcomes to create performance dashboards for marketing and transparency.

## Stakeholder Responses
- **Target audience clarity** → Primarily day traders, with some trades extending 2–3 days depending on option expirations.
- **Data providers & limits** → Polygon and Finnhub for equities/options with a 5 req/sec ceiling and no option Greeks at launch.
- **Alert volume tolerance** → Deliver 10 curated alerts every trading day at 9:15 a.m. EST.
- **Success metrics** → Emphasize week-one retention, conversion from free to paid, and 7/30-day hit rates.
- **Monetization model** → Free read-only tier; $29/mo paid tier with aggressive alerts, dashboards, and archives; 7-day trial; referrals earn a free week.
- **Compliance considerations** → Pin a disclaimer, append automatic alert footers, log all messages, and avoid DM-based advice.
- **DevOps workflow** → GitHub Actions deploys to Render staging per PR; approvals promote to production; feature flags govern new detectors.
- **Discord community tools** → Add moderation/support bots where they add value.
- **User feedback loop** → Capture 👍/👎 reactions, auto-post weekly polls, and log `/feedback <text>` submissions with NPS.
- **Long-term roadmap** →
  - *Phase 1 (Weeks 1–3):* Ship MVP slash commands, cron alerts, logging, disclaimers, feedback loop, and staging/prod pipelines.
  - *Phase 2 (Weeks 4–8):* Launch monetization, tiering, risk preferences, performance summaries, glossary, and referral program.
  - *Phase 3 (Week 9+):* Expand data coverage, add backtesting, personalized watchlists, AI scoring, admin KPIs, and optional portfolio tracking.
  - *Phase 4 (Optional):* Automate marketing, introduce multi-bot architecture, partner webhooks, and scalable cron/logging.

## Follow-up Questions
1. **Discord role mapping**: Which specific roles should unlock the paid-tier features, and how should trials/referrals adjust those roles automatically?
   - **Answer:** Roles map to Discord as follows — `@Free` (read-only sample channels), `@Trial` (temporary full access for 7 days), `@Paid-Conservative` (conservative alerts + dashboard), `@Paid-Aggressive` (all alerts plus analytics), and `@Staff` (admins/reviewers/moderators). Automation comes from a single Stripe webhook worker: checkout events assign the paid role, trial starts assign `@Trial` with a scheduled removal after 7 days, referral redemptions extend access by 7 days or credit the next invoice, and cancellation events immediately remove the paid role.
2. **Alert curation workflow**: Should the 10 daily alerts be fully automated, or is there a human-in-the-loop review step before publishing?
   - **Answer:** A hybrid flow is preferred. The bot generates 20 candidate trades pre-market, a `@Staff` reviewer approves or vetoes the top 10, and if no action happens by 8:55 a.m. ET the system auto-publishes the highest scoring 10. Outcome tracking runs automatically afterward.
3. **Historical data retention**: How long do we need to retain alert logs and feedback for compliance and performance dashboards?
   - **Answer:** Retain alerts, outcomes, and feedback for five years. Keep the first two years in “hot” storage (Postgres + S3) and archive older data to cheaper cold storage to balance compliance with cost.
4. **Infrastructure observability**: Are there preferred monitoring/alerting tools (e.g., Grafana, Sentry) we should integrate with Render for uptime and latency tracking?
   - **Answer:** Adopt Sentry for error tracking (free tier), Healthchecks.io for cron verification (free tier), Render’s native metrics/logs for uptime, and optionally Grafana Cloud’s free tier for dashboards once time permits.
5. **Budget prioritization**: With only ~$13/month remaining, which third-party services (e.g., Sentry, Healthchecks, paid data add-ons) deliver the highest early value, and which can wait until revenue ramps?
   - **Answer:** Prioritize free tiers: Sentry and Healthchecks.io are must-haves at $0, Grafana Cloud’s free dashboards are optional, Stripe fees are unavoidable (≈2.9% + $0.30), and market data APIs should stay within free limits by caching.
6. **Cost ceilings**: Are there hard limits on per-user costs (e.g., Discord premium features, Stripe fees, optional data upgrades) that we should use when evaluating new functionality?
   - **Answer:** Target ≤$1 monthly infrastructure cost per active paid user, keep total API spend under $25/month until monthly recurring revenue surpasses $250, accept default Stripe processing fees, and defer Discord boosts/premium features until revenue exceeds $500/month.
7. **Free alternatives**: Should we prioritize open-source or free-tier tooling (self-hosted monitoring, open data sources) even if it requires more engineering effort, or is a small spend acceptable for faster time-to-value?
   - **Answer:** Use free and open-source options now—Render’s free staging tier, Grafana Cloud free dashboards, Sentry free (10k events/month), free market data APIs (Polygon, Finnhub, Yahoo), and cron plus Redis queues instead of paid schedulers. Paid upgrades (Sentry Team, Datadog APM, premium data feeds) become viable after profitability.

Documenting these answers keeps implementation aligned with the monetization strategy while surfacing the next set of priorities.
