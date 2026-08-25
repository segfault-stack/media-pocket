# Security Policy

## Supported versions

Security fixes are applied to the latest `0.1.x` release and to `main`. Older revisions and operator-modified deployments are not maintained by this repository.

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/segfault-stack/media-pocket/security/advisories/new) for vulnerabilities in Media Pocket. Do not disclose a vulnerability, credential, cookie, private URL, user data, or raw production log in a public issue.

Include only the information needed to reproduce and assess the problem:

- the affected revision or release;
- the relevant component and deployment assumptions;
- minimal reproduction steps;
- the expected and observed security impact;
- a redacted log or proof of concept when useful.

Remove bot tokens, API credentials, passwords, provider cookies, Spotify sessions, invitation codes, chat identifiers, downloaded media, and personal data before submitting a report.

This project does not promise a response or remediation deadline. Reports are assessed according to impact, reproducibility, and maintainer availability.

## Operational incidents

Compromised deployment credentials, lost provider sessions, exposed databases, host intrusion, and misuse of an operator's bot instance are deployment incidents, not public vulnerability reports. Rotate affected credentials, isolate the deployment, preserve only safely redacted evidence, and follow the operator's own recovery process.
