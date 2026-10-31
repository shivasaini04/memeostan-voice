# Contributing

Use Python 3.13 or 3.14 on Linux. Install requirements-dev.txt and run `python -m pytest -q`. Tests must not use live credentials, send email, or place a carrier call. Use synthetic numbers/audio and temporary databases. Include tests for changes to authentication, tenancy, dialing controls, data deletion, and audio conversion.

Keep the first release scoped to an experimental Exotel starter. Explain behavior changes and how you checked them in your PR. Never attach real calls, transcripts, credentials, or private deployment details. Preserve upstream notices. Contributions are licensed under the repository's MIT license.
