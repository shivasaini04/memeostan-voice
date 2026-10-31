# Release preparation

The draft audit has been resolved for an **experimental, single-operator starter** release:

- Public signup, unauthenticated browser audio, legacy turn APIs, unsupported Twilio ingress, website scraping, and arbitrary outcome webhooks were removed/disabled.
- Sessions are authenticated; browser Origin checks, secure-cookie configuration, rate limits, body/frame limits, and bounded audio duration/concurrency were added.
- Incoming Exotel streams require separate Basic credentials; shared carrier-account fallback was removed.
- Dialing is disabled by default and requires an explicit owned-number allowlist; mock DND claims were replaced with an honest local suppression gate.
- Provider prewarming was removed. Audio recording, email, detailed logs, and paid calling default off. Fixed pricing/performance and unimplemented removal promises were removed from prompts.
- Local data deletion, operator provisioning, pinned dependencies, Python 3.13+ audio compatibility, automated tests, CI, and licensing notices were added.

The release deliberately retains experimental limitations documented in README and SECURITY: no regulatory consent/DND service, no production multi-tenant assurance, plaintext credentials in a protected local database, no automatic retention policy, and no monetary spending cap. Provider-backed phone calls are not covered by offline tests.
