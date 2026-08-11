#!/usr/bin/env python3
"""Mint one extension capture credential.

Prints two things that must go to two different places, and says which is which:

* the **credential**, which is pasted once into the Chrome extension and never
  written anywhere else;
* the **configuration entry**, which is a digest and is what
  ``EXTENSION_AUTH__CREDENTIALS`` carries in ``/etc/vmr/vmr.env``.

The server therefore never stores anything replayable — see
``app/core/auth/extension.py`` for why a plain SHA-256 is the right primitive
for a full-entropy secret. Nothing here touches the network, the database, or
any existing configuration file: it prints, and the operator places.

Usage:

    python scripts/mint_extension_credential.py --key-id beta-sahil-laptop
"""

from __future__ import annotations

import argparse
import secrets
import sys

from app.core.auth.extension import (
    CREDENTIAL_SCHEME,
    ExtensionAuthSettings,
    credential_digest,
)

# 32 bytes of `token_urlsafe` is ~43 characters, comfortably above the format's
# floor and well beyond guessing.
_SECRET_BYTES = 32


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-id",
        required=True,
        help=(
            "Short non-secret label for this credential, e.g. 'beta-sahil-laptop'. "
            "It is what revocation names and the only part that may appear in a log."
        ),
    )
    arguments = parser.parse_args(argv)

    key_id = arguments.key_id.strip().lower()
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    entry = f"{key_id}:{credential_digest(secret)}"

    # Validate through the real settings model rather than by eye, so a key id
    # this script accepts is one the application will also load.
    try:
        ExtensionAuthSettings(credentials=(entry,))
    except ValueError as exc:
        print(f"refusing to mint: {exc}", file=sys.stderr)
        return 2

    print("Credential - paste into the extension's Settings, then discard this text:")
    print(f"  {CREDENTIAL_SCHEME}.{key_id}.{secret}")
    print()
    print("Configuration entry - add to EXTENSION_AUTH__CREDENTIALS in /etc/vmr/vmr.env:")
    print(f"  {entry}")
    print()
    print(f'To revoke later, add "{key_id}" to EXTENSION_AUTH__REVOKED_KEY_IDS and restart.')
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
