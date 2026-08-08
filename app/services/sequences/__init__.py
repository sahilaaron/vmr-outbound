"""Persistence, review and read models for the seven-message sequence (SEQ-001).

Split by responsibility rather than by table:

``persistence``
    Turning a validated :class:`~app.services.personalization.sequence.GeneratedSequence`
    into durable rows, atomically, and answering "has this exact generation
    already happened" before anything is spent.
``review``
    Per-message decisions against exact immutable versions, editing, and the
    derived sequence aggregate.
``read``
    Bounded read models for the Review queue, the Contact page and Admin
    diagnosis. No page loads seven bodies per contact.
"""
