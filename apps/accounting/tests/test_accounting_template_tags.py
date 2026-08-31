"""Tests for accounting presentation helpers."""

from apps.accounting.templatetags.accounting_tags import simple_account_code


def test_simple_account_code_matches_imported_chart_numbering() -> None:
    assert simple_account_code("1-01-01-001") == "1111"
    assert simple_account_code("1-01-02-001") == "1121"
    assert simple_account_code("1-02-01-009") == "1219"
    assert simple_account_code("8-01-03-001") == "8131"


def test_simple_account_code_preserves_non_structural_values() -> None:
    assert simple_account_code("12121") == "12121"
    assert simple_account_code("ASSET-001") == "ASSET-001"
    assert simple_account_code(None) == ""
