"""User accounts: the directory an administrator manages and both login paths read.

Split in two on purpose.

``tokens``
    Minting, storing and consuming the one-time password-setup and
    password-reset links. Isolated because it is the part where a mistake is
    silent — a token stored in the clear, or a consumed token that still works,
    looks exactly like a working feature.
``service``
    Everything an administrator does (create, disable, reactivate, re-role,
    issue a link) and everything a sign-in does (resolve by address, resolve by
    Google subject, verify a password, stamp a login).

Both write audit events through ``app.services.audit`` and neither ever puts a
password, a password hash or a raw token into one.
"""
