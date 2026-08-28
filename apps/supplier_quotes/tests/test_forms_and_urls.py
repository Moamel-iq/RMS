from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile
from django.test import SimpleTestCase
from django.urls import resolve, reverse
from django.utils.datastructures import MultiValueDict

from apps.supplier_quotes.forms import SupplierQuoteAttachmentForm


class SupplierQuoteAttachmentFormTests(SimpleTestCase):
    def test_rejects_an_unapproved_attachment_type(self) -> None:
        uploaded = SimpleUploadedFile(
            "quote.docx",
            b"not permitted",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        files: MultiValueDict[str, UploadedFile[Any]] = MultiValueDict({"file": [uploaded]})
        form = SupplierQuoteAttachmentForm(files=files)
        self.assertFalse(form.is_valid())
        self.assertIn("PDF", form.errors["file"][0])

    def test_accepts_a_pdf_attachment(self) -> None:
        uploaded = SimpleUploadedFile("quote.pdf", b"%PDF-1.4", content_type="application/pdf")
        files: MultiValueDict[str, UploadedFile[Any]] = MultiValueDict({"file": [uploaded]})
        form = SupplierQuoteAttachmentForm(files=files)
        self.assertTrue(form.is_valid())


class SupplierQuoteUrlTests(SimpleTestCase):
    def test_attachment_download_has_a_permissioned_route(self) -> None:
        url = reverse("supplier_quotes:attachment_download", args=[7, 3])
        self.assertEqual(resolve(url).url_name, "attachment_download")
