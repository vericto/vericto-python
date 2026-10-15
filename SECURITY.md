# Security Policy

Vericto is a security product. We take vulnerabilities seriously and
appreciate responsible disclosure.

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report it privately, by either channel:

- Email **security@vericto.com**.
- Open a private report on GitHub:
  [Report a vulnerability](https://github.com/vericto/vericto-python/security/advisories/new)
  (Security tab → "Report a vulnerability"). Only the maintainers can see it.

Include:

- A description of the vulnerability and its impact.
- Steps to reproduce.
- The affected version (`python -c "import vericto; print(vericto.__version__)"`)
  and the driver involved.
- Any suggested remediation.

You will receive an acknowledgement within **48 hours** and a status update
within **5 business days**.

## Scope

High-priority issues for this package include:

- **Credential exposure**: the API key written, logged or sent anywhere other
  than the configured Vericto API over TLS.
- **Guard bypass**: a path through a wrapped connection that executes SQL Vericto
  did not evaluate, or blocked, outside an explicit `fail_mode="open"`; or a masked
  statement that runs the original SQL instead of `rewritten_query`.
- **Data leakage to Vericto**: parameter values sent to the API.

Issues in the SQL evaluation itself belong to
[vericto-engine](https://github.com/vericto/vericto-engine/security).

## Disclosure process

1. Report received and acknowledged (within 48h).
2. We confirm and assess severity.
3. We develop and test a fix.
4. We release the fix and credit you (unless you prefer to remain anonymous).
5. Public disclosure after users have had reasonable time to update.

## Supported versions

Security fixes are applied to the latest released version. Older versions are
patched at the maintainers' discretion.

Thank you for helping keep Vericto and its users safe.
