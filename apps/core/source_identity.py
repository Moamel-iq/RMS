"""
One canonical form for a source identity, for every module that records one.

A source identity names the upstream economic event behind a posting:

    organization + document type + document id + event

It is the thing that stops a retried purchase invoice becoming two journals
and two stock receipts. That guarantee is a **uniqueness index**, and a
uniqueness index compares bytes. `"145"` and `"145 "` are different bytes, so
without a single canonical form the guarantee simply does not apply to the
retry it exists to catch — the second posting lands beside the first and both
look correct.

This module lives in `apps.core` rather than in accounting because three
places already carry these fields — `JournalEntry`, `AuditEvent`, and now the
stock ledger — and a second normalizer in any of them would be a second answer
to "is this the same event". Inventory does not get its own.

## The rules, and why they are not symmetric

    document_type -> strip().upper()
    document_id   -> strip()          <- NOT upper()
    event         -> strip().upper()

`document_type` and `event` are **our** vocabulary. `PURCHASE_INVOICE` is a
name this system chose, and folding its case loses nothing.

`document_id` is **someone else's**. It may be a supplier's invoice number, a
delivery-app order reference, or a bank transaction id, and those systems are
frequently case-sensitive: `AB-1042` and `ab-1042` can be two different
invoices from the same supplier. Folding the case here would merge two real
documents into one and silently suppress the second posting — a far worse
failure than the duplicate we are trying to prevent.

So whitespace is removed everywhere (it is always transport damage), and case
is normalised only where we own the vocabulary.

## Whitespace-only values are refused, not swallowed

`"   "` is not an absent identity. It is a caller sending a broken value, and
quietly reading it as "no source document" would move the posting outside the
uniqueness guarantee while leaving it looking exactly like a manual entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class SourceIdentity:
    """A canonicalised source triple. Frozen: it is an identity, not a buffer."""

    document_type: str
    document_id: str
    event: str

    @property
    def is_present(self) -> bool:
        """Whether this names an upstream document at all."""
        return bool(self.document_type or self.document_id or self.event)

    @property
    def is_complete(self) -> bool:
        return bool(self.document_type and self.document_id and self.event)

    def as_tuple(self) -> tuple[str, str, str]:
        """For fingerprints and lookups — always the canonical form."""
        return (self.document_type, self.document_id, self.event)


def _canonical(raw: str, *, field: str, fold_case: bool) -> str:
    """
    Strip, optionally upper-case, and refuse a value that was only whitespace.

    `raw or ""` because callers legitimately pass `None` for an absent
    identity, and turning that into the string `"None"` would be worse than
    anything this module prevents.
    """
    text = str(raw or "")
    canonical = text.strip()
    if fold_case:
        canonical = canonical.upper()

    if text and not canonical:
        raise ValidationError(
            _("%(field)s is blank. Send a real value or send none of the three."),
            code="blank_source_identity",
            params={"field": field},
        )
    return canonical


def canonical_source_identity(
    *,
    source_document_type: str = "",
    source_document_id: str = "",
    source_event: str = "",
) -> SourceIdentity:
    """
    The one form a source identity is validated, compared, and stored in.

    Call this **before** anything else touches the three values: before
    validation, before an idempotency lookup, before the request fingerprint,
    and before persistence. A fingerprint computed over the raw values and a
    row written from the canonical ones would disagree on the retry, which is
    precisely the case both exist to handle.
    """
    return SourceIdentity(
        document_type=_canonical(
            source_document_type, field="source_document_type", fold_case=True
        ),
        # Deliberately not case-folded — see the module docstring.
        document_id=_canonical(source_document_id, field="source_document_id", fold_case=False),
        event=_canonical(source_event, field="source_event", fold_case=True),
    )
