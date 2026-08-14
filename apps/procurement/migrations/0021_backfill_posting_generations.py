"""
Give every already-posted invoice its generation 1.

Task 2.12 made `SupplierInvoicePosting` the record of an invoice reaching the
ledger. Invoices posted before it existed have a journal and no generation, and
that gap is not cosmetic: it would leave two shapes of posted invoice forever —
one whose reversal is driven by the posting table and one whose reversal falls
back to the invoice's own columns — and every later reader would have to know
which was which.

It also shows up immediately in verification. A database migrated from zero and
seeded produces a generation for every posted demo invoice; a development
database seeded before this task does not, so the two stop reproducing each
other's counts, and "fresh reproduces dev exactly" is the check this project
uses to certify a step.

So the backfill is written rather than the special case.

**What a backfilled row says, and what it deliberately does not.** These
invoices posted only direct charges — an inventory line could not post before
Task 2.12 — so the goods figures are zero and there is no match to name. The
payable is the invoice's own `posted_amount`, which for a direct-only invoice
was already the whole document. `allocation_fingerprint` is empty because there
were no allocations to hash.

**The journals keep their original source identity.** They name the invoice's
`public_id`, not the new row's, and they are not rewritten: a posted journal's
source identity is immutable at the database and rewriting history to look
tidier is the opposite of what an audit trail is for. `verify_supplier_payables`
accepts both, and the reconciliation joins through the stored FK rather than by
matching identifiers.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    SupplierInvoice = apps.get_model("procurement", "SupplierInvoice")
    SupplierInvoicePosting = apps.get_model("procurement", "SupplierInvoicePosting")

    zero = 0
    for invoice in SupplierInvoice.objects.filter(
        status__in=("POSTED", "REVERSED"), journal_entry__isnull=False
    ).exclude(postings__isnull=False):
        SupplierInvoicePosting.objects.create(
            organization_id=invoice.organization_id,
            supplier_invoice=invoice,
            purchase_match=None,
            generation=1,
            status="REVERSED" if invoice.status == "REVERSED" else "LIVE",
            journal_entry_id=invoice.journal_entry_id,
            reversal_journal_entry_id=invoice.reversal_journal_entry_id,
            allocation_fingerprint="",
            goods_cleared_value=zero,
            invoice_matched_value=zero,
            price_variance=zero,
            direct_charge_value=invoice.posted_amount,
            payable_value=invoice.posted_amount,
            posted_by_id=invoice.posted_by_id,
            posted_at=invoice.posted_at,
            reversed_by_id=invoice.reversed_by_id,
            reversed_at=invoice.reversed_at,
            reversal_reason=invoice.reversal_reason,
        )


def unbackfill(apps, schema_editor):
    """
    Remove only the rows this migration created: generation 1, no match.

    A row with a match was written by the posting service and is not this
    migration's to delete. The immutability trigger refuses a delete either
    way, so this is reachable only with the trigger dropped — which is what
    reversing `0018` does first.
    """
    SupplierInvoicePosting = apps.get_model("procurement", "SupplierInvoicePosting")
    SupplierInvoicePosting.objects.filter(generation=1, purchase_match__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("procurement", "0020_posting_match_optional"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
