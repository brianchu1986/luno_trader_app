# Security Policy

This repository contains software that may connect to the Luno exchange and interact with real funds. Please read this document carefully before using or reporting security issues.

## Supported Versions

This project is currently under active development. Only the latest commit on the default branch is supported for security fixes.

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately.

**Preferred method**

-   Open a GitHub issue **without sensitive details** and write: “Security report – please contact me”
-   Then send details privately to the maintainer

**What to include**

-   A clear description of the issue and impact (what can be accessed/modified)
-   Steps to reproduce (proof-of-concept is helpful)
-   Affected files/modules if known
-   Any suggested fix or mitigation

**What NOT to include**

-   Do not post API keys, secrets, wallet addresses, or private account information in public issues
-   Do not upload screenshots/logs that contain secrets

## Disclosure Process

After receiving a private report, the maintainer will:

1. Confirm receipt and begin triage
2. Reproduce and assess severity
3. Prepare a fix and release notes
4. Credit the reporter if requested (or keep anonymous)

## Security Best Practices (Users)

### 1) Protect your API keys

-   Store secrets in environment variables (e.g., `.env`) and **never commit them**
-   Add `.env` to `.gitignore`
-   Rotate keys immediately if you suspect exposure

### 2) Use least-privilege API permissions

Only enable the minimum permissions needed:

-   Prefer read-only where possible
-   Do not grant withdrawal/send permissions unless you fully understand the risk

### 3) Start small and use dry-run/testing

-   Test with minimal balances
-   Use dry-run / paper-trading modes if available
-   Monitor logs and order placement carefully before running unattended

### 4) Operational security

-   Run on a secured machine/server
-   Keep your OS and Python dependencies updated
-   Avoid running as root
-   Restrict access to logs and databases (may contain balances/positions)

### 5) Assume you can lose funds

Trading systems can fail due to:

-   Exchange downtime, latency, rate limits
-   Partial fills and slippage
-   Bugs, misconfiguration, or unexpected market behavior

You are responsible for your own usage and any financial outcome.

## Scope

This policy covers:

-   Secret leakage (API keys, tokens)
-   Unauthorized order placement or account actions
-   Remote code execution / injection
-   Dependency supply-chain issues
-   Unsafe defaults that can cause unintended live trading

## Out of Scope

The following are generally out of scope:

-   Feature requests or strategy profitability
-   Issues caused by user misconfiguration without a security impact
-   Social engineering attempts unrelated to this repository

## Security Notes for Contributors

-   Never log secrets (API keys, signatures, full auth headers)
-   Avoid printing full request/response payloads from exchange APIs in production logs
-   Validate and sanitize external inputs (CLI args, config files)
-   Prefer explicit allowlists for symbols/pairs, order types, and max order size
-   Keep risk checks close to execution (final gate before live orders)
