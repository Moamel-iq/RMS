"""
Shared abstract models.

Abstract only — this app owns no tables. The full audit foundation (actor
tracking, correlation IDs, history integration) is Task 0.5; this is the
timestamp base that models needed before then.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    """Records when a row was created and last changed.

    This is not the audit trail. It answers "when", never "who" or "why", and
    it says nothing about posted ledger entries, which are immutable by
    construction rather than by timestamp.
    """

    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True
