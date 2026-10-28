# Changelog

## 0.4.0 - 2026-10-28

- Make `opencdr-deploy` the supported interface for management, catalog loading,
  local/deployed tests, coverage forwarding, cost reporting, and ingestion canaries.
- Retain generic script execution and `api core-cli` as deprecated compatibility paths.

## 0.3.0 - 2026-10-28

- Install, upgrade, and inspect the independently released `opencdr-mcp` package.

## 0.2.0 - 2026-10-27

- Install and retain exact tagged OpenCDR Core releases without a separate clone.
- Upgrade or roll back named installations by switching managed Core versions.
- Reconcile one or more installations from a strict TOML configuration file.
- Target separate AWS profiles and accounts with a fail-closed account-ID guard.
- Document declarative feature flags, multi-account behavior, and source management.

## 0.1.0 - 2026-10-27

- Initial deployment registry, immutable packaging, feature management, operational
  scripts, and complete OpenCDR REST API access.
