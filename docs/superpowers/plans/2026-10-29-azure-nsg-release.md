# Azure NSG correction and release implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline, with one fresh final review.

**Goal:** fix actual Azure NSG discovery and publish Sonata 0.6.18.
**Architecture:** extend the existing Azure provider and JSON CLI helper, keep its public interface and security validation. Use the existing release workflow.
**Tech stack:** Python standard library, existing Azure CLI, pytest and uv.
**Spec:** docs/superpowers/specs/2026-10-29-azure-nsg-release.md.

## Global constraints

No new dependency, NanoLab imports, VM allocation or active-pilot mutation.
Release the matching engine/tasks versions together. Publication is authorized
by the author's explicit request on 29 October. Keep current bounded ingress.

## Review focus

- NIC-only, subnet-only and simultaneous NSGs, including another resource group.
- Missing/invalid associations fail closed, without inventing an NSG name.
- Empty successful CLI output versus invalid nonempty JSON.
- Empty/unbounded sources and invalid ports fail before writes.
- Azure read-back mismatch remains an error; managed VMs still work.

### Task 1: fix and verify release candidate

Files: vm/providers/azure.py, tests/vm/test_azure_provider.py, tests/test_artifacts.py under packages/sonata-tasks; both pyprojects, engine __init__, uv.lock.

- [ ] Add regression tests for actual NIC/subnet NSGs and null output, observe RED.
- [ ] Resolve associations with native Azure reads; use actual NSG resource group/name. Keep validation and read-back, add bounded CLI timeout and null handling.
- [ ] Align the existing errno.ELOOP test with catalogue lint; retain its safety assertion.
- [ ] Bump the four version sites to0.6.18 and lock; run focused tests, pre-commit, full Linux engine/catalogue coverage gates and build both distributions.
- [ ] Commit and obtain one fresh whole-branch review; fix any important findings with tests.

### Task 2: publish the verified candidate

- [ ] Confirm remote main unchanged, fast-forward main and push the tested commit, as explicitly requested.
- [ ] Require green CI, then tag v0.6.18 to trigger the existing release workflow.
- [ ] Verify both jobs and public package metadata; record release evidence. Keep active NanoLab untouched.
