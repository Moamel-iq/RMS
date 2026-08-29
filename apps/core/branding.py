"""
The product's name, in the five forms it is allowed to take.

The name is not one string. A sidebar has room for a word and a person reads
it forty times a day; a printed invoice is a legal-ish document that leaves the
building and has to say what produced it; a browser tab is read out of context,
sometimes by somebody searching a row of tabs. Those want different lengths of
the same name, and picking one length for all of them makes the short places
shouty and the formal places anonymous.

So the forms are named by **where they appear**, not by how long they are:

    SHORT       جدوى                                    rail, sidebar, home
    SIGN_IN     جدوى لإدارة المطاعم                      the login page
    DOCUMENT    Jadwa RMS | جدوى لإدارة المطاعم          tab titles, print, reports
    FULL        Jadwa Restaurant Management System      footer, about
    VENDOR      Jadwa                                   who makes it

Every one of them lives here and nowhere else. Before this module the name was
a literal repeated in 139 templates — 137 of them the same tab-title line —
which is why renaming it was a sweep rather than an edit, and why a handful of
screens would inevitably have been missed.

## What this is not

It is not the *organization's* name. `جدوى` is the software; the organization
rows in the database are the businesses that run it, and one of them is called
`خان مندي`. A print letterhead names both, and they must never be conflated:
`print_organization` comes from the tenant's own record, and only the wordmark
beside it comes from here.

## Translation

The Arabic-only forms are message ids like every other string in this project,
so an English locale can render `Jadwa` where Arabic renders `جدوى`. The two
forms that are *deliberately bilingual* — `DOCUMENT` and `FULL` — are not
translated: they are typography, fixed by the owner, and a locale that
rearranged them would be changing the brand rather than translating it.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

#: The everyday name. Short enough for a collapsed rail.
SHORT = _("جدوى")

#: The sign-in page, where the reader may not yet know what they are opening.
SIGN_IN = _("جدوى لإدارة المطاعم")

#: Tab titles, print headers and report sheets — anywhere the name is read
#: away from the running application. Bilingual and fixed.
DOCUMENT = "Jadwa RMS | جدوى لإدارة المطاعم"

#: The formal product name, for the footer and the about screen.
FULL = "Jadwa Restaurant Management System"

#: Who makes it.
VENDOR = "Jadwa"


def branding(request: HttpRequest) -> dict[str, Any]:
    """
    The name forms, on every render.

    Separate from `shell` deliberately: that processor returns an empty dict
    for an anonymous request, and the login page — the one screen whose whole
    job is to say what this is — renders anonymously. A brand that disappeared
    exactly where it matters most would be a quiet, permanent bug.
    """
    return {
        "brand_short": SHORT,
        "brand_sign_in": SIGN_IN,
        "brand_document": DOCUMENT,
        "brand_full": FULL,
        "brand_vendor": VENDOR,
    }
