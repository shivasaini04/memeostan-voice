# Security policy

This experimental starter supports one trusted operator and one worker. It is not a hostile-user multi-tenant service. Keep it local until you configure HTTPS, an exact PUBLIC_ORIGIN, secure cookies, incoming Exotel Basic authentication, filesystem protection, and provider spending limits.

Report vulnerabilities using this repository's private security advisory reporting, or email hello@axoncrew.com. Do not put credentials, phone numbers, recordings, or transcripts in a public issue. Rotate any exposed keys immediately.

The app restricts voice sessions to authenticated operators or the configured Exotel stream Basic credentials. Browser mutations and browser WebSockets require the configured Origin. The trigger API requires the operator's API key. Signup and unsupported telephony/legacy turn endpoints are disabled. Rate and audio-session limits are process local; run one worker. Reverse-proxy limits and provider budgets are still needed for public network exposure.

SQLite contains plaintext provider credentials and personal call data. The application does not implement encryption at rest, automated legal consent verification, regulatory DND lookup, or automated retention guarantees. Use encrypted storage, restrict local access, and manage backups. Detailed debugging and audio recording are opt-in and sensitive.

Keep dependencies updated. No warranty is provided; see LICENSE.
