---
description: Local pytest environment gotchas on Windows
paths:
  - "tests/**"
  - "conftest.py"
---

# Running the tests locally

Validation depth is decided by `docs/PROPORTIONAL_VALIDATION.md`, not by this file.
This file only records how to run the suite without chasing false failures.

- Run through the project interpreter (`.venv/Scripts/python.exe -m pytest`), not a
  bare `pytest` that may resolve to an Anaconda install.
- The suite forces the `vmr_test` database. A failure that names the development
  database usually means the environment, not the test, is wrong.
- Windows-specific false failures to recognise before "fixing" product code:
  console encoding (`cp1252`) breaking on non-ASCII output, and the asyncio
  `ProactorEventLoop` producing teardown noise. Neither is a product defect.
- When CI fails, read the failed job and the exact failing tests first. Repair only
  candidate-caused failures; rerun proven infrastructure flakes rather than changing
  product code.
