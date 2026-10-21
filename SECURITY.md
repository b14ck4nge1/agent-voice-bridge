# Security policy

Agent Voice Bridge authorises actions on your computer, so security reports are
welcome and taken seriously.

* **Reporting:** use GitHub's private vulnerability reporting ("Report a
  vulnerability" on the Security tab). Please don't open public issues for
  vulnerabilities.
* **Scope:** anything that lets an operation be approved without the user's
  explicit, confirmed decision on that exact operation; token or secret leaks;
  bypassing the second factor; replaying decisions; denial of the fail-closed
  behaviour.
* **Supported versions:** only the latest commit on `main` during pre-alpha.

The threat model and known gaps are in [docs/security.md](docs/security.md).
