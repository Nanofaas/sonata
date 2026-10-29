# Azure NSG correction and release

The author requested fixing Sonata and publishing it while the separately
approved Azure pilot runs. Resolve actual NIC/subnet NSG associations instead of
assuming <VM>-nsg; preserve bounded CIDRs, port validation and read-back checks.
Azure CLI may return empty stdout for a successful null query. Keep the existing
public API and zero engine runtime dependencies. No new package dependency.

Publish sonata-engine/sonata-tasks 0.6.18 together through the existing tag/OIDC
workflow after local Linux suites, package builds, static checks and fresh review.
Fix the existing PT011 test lint failure that blocks CI, preserving errno.ELOOP.
Do not change the active NanoLab pilot checkout/environment. Its temporary
provider override is removed only after the pilot and published-version update.
