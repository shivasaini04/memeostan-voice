# v0.1.0 release checks

Prepared 2026-10-31 on Linux/Python 3.14.

- 16 offline tests: fail-closed dialing, number allowlist/suppression, credential isolation, authentication, CSRF/Origin rejection, disabled signup/legacy routes, secure-cookie flag, request/frame bounds, session timeout, tenant isolation, queue idempotency, recording/email defaults, booking uniqueness, safe recording paths, Google SDK config construction, synthetic Exotel media framing/resampling, and browser-session cleanup.
- External provider networking is blocked in tests. Carrier/model handlers use fakes where needed.
- Fresh source copy + runtime-only environment: operator provisioning, actual Uvicorn startup/health, Chrome login, company settings save, dashboard, and browser demo page all checked. The initial fresh-browser check found a Referrer-Policy/Origin interaction; using `same-origin` corrected it and the flow passed.
- `manage.py purge-call-data` checked against the temporary smoke database.
- Runtime dependency audit: no known vulnerabilities reported at preparation time. Dependency resolution is pinned; audits are time-specific.
- Source secret scan: no findings after excluding generated caches. Published tree excludes .env, databases, recordings, logs and tunnel binaries.
- Syntax compilation and Git whitespace checks passed.

GitHub Actions repeats tests, release-source scanning, syntax compilation, and the dependency audit on Linux/Python 3.13 and 3.14.

Not verified: paid Gemini/Sarvam/OpenRouter inference, real Exotel origination/PSTN audio, carrier-specific account/flow provisioning, production echo behavior, large-scale traffic, Windows/macOS. The screenshot uses an empty synthetic account. The test runner emits a Starlette deprecation warning about its httpx test transport; it does not affect the runtime or test result.
