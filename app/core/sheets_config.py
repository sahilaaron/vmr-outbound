"""Configuration for the Google Sheets add-on seam (env prefix ``SHEETS__``).

A fourth, separate authority, and separate for the same reason the other three
are: ``auth`` is the human operator's browser session, ``extension_auth`` is the
Chrome capture extension's own credential, ``gmail`` is permission to write into
a mailbox, and this is permission for one Apps Script add-on to submit rows and
read results on behalf of the account running it. None may stand in for another.

Everything here is a *ceiling*, not a capability. Turning the feature on does not
authorise a Campaign, a provider call or a stage — those remain decided by the
account's Campaign access, the Campaign's own execution switches and the Agent
controls. What this block decides is which add-on may present a credential at
all, and how much work one request may ask for.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SheetsIntegrationSettings(BaseModel):
    """Google Sheets add-on settings (env prefix ``SHEETS__``)."""

    model_config = {"frozen": True}

    #: The OAuth client ids whose ID tokens this deployment will accept.
    #:
    #: This is the confused-deputy check. A Google ID token is a perfectly valid
    #: Google token whoever it was minted for, so "signed by Google" is not an
    #: authorization — "minted for *our* add-on" is. Empty means the integration
    #: accepts nobody, which is the correct reading of an unconfigured
    #: deployment and is enforced in ``app/core/auth/sheets_assertion.py``.
    #:
    #: Written in the environment as a JSON list, the same shape
    #: ``AUTH__ALLOWED_OPERATOR_EMAILS`` uses:
    #: ``SHEETS__ALLOWED_AUDIENCES=["1234-abc.apps.googleusercontent.com"]``.
    #:
    #: An operator reads the value to put here off the add-on's own setup screen,
    #: which decodes and displays the ``aud`` claim of the token Apps Script
    #: actually mints. Guessing it from the Cloud console is how a deployment
    #: ends up configured for a client that never calls.
    allowed_audiences: tuple[str, ...] = Field(
        default=(),
        description="OAuth client ids whose Google ID tokens the add-on may present.",
    )

    #: The largest number of rows one submit request may carry, and the whole of
    #: the supported Google Sheets ingestion maximum: nothing else in the path
    #: bounds a cohort, so this number *is* the customer-facing contract.
    #:
    #: A ceiling rather than a queue: an oversized batch is refused whole, with
    #: the limit stated, so the add-on can chunk deliberately instead of the
    #: server silently processing a prefix.
    #:
    #: Five hundred is the size of a real prospect sheet. Fifty was set when
    #: intake could still spend a logo.dev lookup per new company name, so the
    #: request bound doubled as a spend bound; ``companies.py`` since removed
    #: every provider call from intake, leaving one submit as bounded,
    #: deterministic database work that costs nothing per row. The remaining
    #: reason to bound it at all is that one request should stay one Apps Script
    #: execution, which five hundred rows of pure database work is.
    max_batch_rows: int = Field(
        default=500,
        gt=0,
        le=500,
        description="Maximum prospect rows accepted in one submit request.",
    )

    #: The largest number of submissions one results request may ask about.
    #: Reads are cheap but not free, and an unbounded id list is an unbounded
    #: query. The add-on pages through its own rows against this number.
    max_result_ids: int = Field(
        default=200,
        gt=0,
        le=1000,
        description="Maximum submission identifiers accepted in one results request.",
    )

    #: The longest free-text prospect context one row may carry into
    #: personalization. Bounded because it is operator prose that reaches a model
    #: prompt, and because a cell can hold far more than a useful observation.
    max_context_chars: int = Field(
        default=1000,
        gt=0,
        le=5000,
        description="Maximum characters accepted in one row's operator context field.",
    )


__all__ = ["SheetsIntegrationSettings"]
