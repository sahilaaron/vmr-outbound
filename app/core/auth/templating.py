"""Automatic CSRF token emission for every server-rendered form.

There are 111 state-changing forms across 47 templates and four independent
Jinja environments. Hand-editing each one would make the security of the write
boundary depend on nobody ever forgetting a hidden input — including in
templates written months from now, by which time the reason would be folklore.

So the token is not written by hand at all. This Jinja extension rewrites the
raw template source at *compile* time, inserting ``{{ csrf_field() }}``
immediately after every opening ``<form>`` tag whose method is POST. A template
added tomorrow is covered the moment it is compiled, and the resulting page is
identical to one where the field had been typed in by hand.

Why compile-time source rewriting and not response rewriting: a compiled
template is cached once, so there is no per-request cost, no HTML re-parsing on
the response path, no ``Content-Length`` surgery, and nothing that can corrupt a
streamed or non-HTML response. The transformation is also visible in
``environment.preprocess`` output, so it can be asserted on directly — which the
conformance test does.

When authentication is disabled, ``csrf_field()`` renders empty and the emitted
markup is byte-identical to today's.
"""

from __future__ import annotations

import re

from jinja2 import Environment
from jinja2.ext import Extension

# Matches one opening <form ...> tag, tolerating newlines inside the tag and any
# attribute order. `[^>]*` cannot run past the tag because a `>` inside an
# attribute value would already break the surrounding HTML.
_FORM_TAG = re.compile(r"<form\b[^>]*>", re.IGNORECASE | re.DOTALL)
_POST_METHOD = re.compile(r"""\bmethod\s*=\s*["']?post\b""", re.IGNORECASE)

CSRF_CALL = "{{ csrf_field() }}"


def inject_csrf_fields(source: str) -> str:
    """Insert ``{{ csrf_field() }}`` after every opening POST ``<form>`` tag.

    Idempotent: a tag that is already followed by the call is left alone, so a
    template that spells the field out by hand does not end up with two.
    """

    pieces: list[str] = []
    cursor = 0
    for match in _FORM_TAG.finditer(source):
        tag = match.group(0)
        pieces.append(source[cursor : match.end()])
        cursor = match.end()
        if not _POST_METHOD.search(tag):
            continue
        if source[cursor:].lstrip()[: len(CSRF_CALL)] == CSRF_CALL:
            continue
        pieces.append(CSRF_CALL)
    pieces.append(source[cursor:])
    return "".join(pieces)


def post_form_tags(source: str) -> list[str]:
    """Every opening POST ``<form>`` tag in ``source`` — used by the tests."""

    return [tag for tag in _FORM_TAG.findall(source) if _POST_METHOD.search(tag)]


class CsrfFormExtension(Extension):
    """Jinja extension that guarantees every POST form carries its token."""

    def preprocess(self, source: str, name: str | None, filename: str | None = None) -> str:
        return inject_csrf_fields(source)


def install_csrf_form_extension(environment: Environment) -> None:
    """Add the extension to one environment, idempotently."""

    if not environment.extensions.get(f"{CsrfFormExtension.__module__}.CsrfFormExtension"):
        environment.add_extension(CsrfFormExtension)
