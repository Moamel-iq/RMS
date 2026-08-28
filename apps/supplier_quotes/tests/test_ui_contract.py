from pathlib import Path

from django.template.loader import get_template
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_ROOT = ROOT / "templates" / "supplier_quotes"
TEMPLATE_NAMES = ("list.html", "form.html", "detail.html", "line_form.html")
LEGACY_CLASS_TOKENS = (
    "pagehead",
    "formcard",
    'class="btn',
    'class="table',
    "responsive-table",
    "data-table-shell",
)


class SupplierQuoteUiContractTests(SimpleTestCase):
    def test_every_screen_compiles_and_uses_the_partial_aware_shell(self) -> None:
        for name in TEMPLATE_NAMES:
            get_template(f"supplier_quotes/{name}")
            source = (TEMPLATE_ROOT / name).read_text(encoding="utf-8")
            self.assertIn('{% extends shell_base_template|default:"shell.html" %}', source)

    def test_the_supplier_offer_screens_do_not_depend_on_legacy_presentational_classes(
        self,
    ) -> None:
        source = "\n".join(
            (TEMPLATE_ROOT / name).read_text(encoding="utf-8") for name in TEMPLATE_NAMES
        )
        for token in LEGACY_CLASS_TOKENS:
            self.assertNotIn(token, source)
        self.assertIn('class="ui-page ui-quotes-page"', source)
        self.assertIn('class="ui-table ui-table--responsive"', source)
        self.assertIn('class="ui-form ui-form-card', source)

    def test_destructive_actions_require_confirmation(self) -> None:
        detail = (TEMPLATE_ROOT / "detail.html").read_text(encoding="utf-8")
        self.assertEqual(detail.count('onsubmit="return confirm('), 3)
