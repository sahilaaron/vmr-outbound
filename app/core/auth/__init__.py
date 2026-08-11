"""Hosted-operator authentication for the internal VMR Beta.

The package is deliberately small and has one job: decide whether the caller is
an approved internal VMR operator, and refuse everything else. It is *not* a user
system. There is no signup, no password, no tenancy and no role model — those
belong to a product that does not exist yet and would be dead weight here.

Layout
------
``config``      the configuration block and the approved-operator policy.
``session``     the signed stateless session cookie and CSRF token derivation.
``csrf``        the request-scoped CSRF seam and the enforcement dependency.
``templating``  the Jinja extension that puts the token in every POST form.
``identity``    the provider seam and the claim rules applied to an assertion.
``jwks``        RS256 signature verification against the provider's key set.
``google``      the live Google OAuth 2.0 / OpenID Connect client.
``policy``      the anonymous route allow-list and path normalisation.
``middleware``  the boundary that applies all of the above to every request.
``startup``     the contract that makes unsafe hosted states unstartable.

Nothing here knows about Gmail. VMR application identity and Gmail mailbox
authorization are separate concerns with separate credentials, and this package
requests identity scopes only.

This module deliberately re-exports nothing: ``app.core.config`` imports
``app.core.auth.config``, so any import here that reached back into
``app.core.config`` would form an import cycle. Import the submodule you need.
"""

from __future__ import annotations
