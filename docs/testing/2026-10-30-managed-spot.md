# Managed Azure Spot — release 0.6.19 verification

The existing Azure provider now forwards priority, eviction policy and maximum
bid to azure-vm-sdk 0.8.0. Regular remains the default. Spot requests verify
Azure's actual policy before adopting an existing VM and after creating one.
Invalid price/policy combinations fail during request validation.

Validation on 30 October 2026:

- Linux Python 3.12: engine 227 passed, coverage 96.17%; catalogue 798 passed,
  coverage 92.40%. Both full suites exceed the existing 90% gates.
- All repository pre-commit hooks passed, including both type checks, import
  contracts, lint/format and Bandit.
- Wheel and sdist built for both 0.6.19 distributions.
- NanoLab integration: 674 targeted configuration/provisioning/comparison tests
  passed; another VM/release selection passed 203 tests (6 skipped, 191 excluded
  NanoFaaS contracts). NanoLab type check and all five import contracts passed.
- Independent review found no correctness blocker. In particular, the lifecycle
  adapter preserves Spot fields and cleanup ownership is recorded before launch.

New tests exercised the real SDK workspace renderer with its FakeBackend;
NanoLab's test ran its existing provision_roles context through creation and
teardown of both Spot roles. External Azure/OpenTofu operations were simulated.
The initial new tests failed against the old implementation. A first full Linux
run caught an overly narrow error-message regex in one test; after correction
the full suites passed as reported above.

No live Azure VM or performance experiment was started. Publication to PyPI and
the corresponding NanoLab dependency/lock update remain release actions.
