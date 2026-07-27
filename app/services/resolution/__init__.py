"""Automatic company-domain resolution (DAT-017A).

Four modules, split by what each is allowed to know:

* :mod:`.policy` — the versioned decision rules. Pure functions over a plain
  evidence record: no database, no provider, no clock. That is what makes every
  branch of the policy testable without a fixture and reproducible from a stored
  decision.
* :mod:`.store` — reading and writing decision rows. Knows the models and
  nothing about how a decision is reached.
* :mod:`.service` — orchestration: gather evidence, decide whether a provider
  call is even needed, apply the decision to the permanent Company and Contact.
* :mod:`.gates` — what a decision authorizes downstream. Separate on purpose:
  the difference between ``provisional`` and ``confirmed`` is only real if
  something enforces it, and that something must not live in the module that
  produces the states.
"""
