"""
Sign-in flow, including the htmx path.

Covers what a user can do wrong and what an attacker can learn, not just the
happy path.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.users.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "pw-not-real-1234"
HTMX_HEADERS = {"hx-request": "true"}


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="a.hassan", password=PASSWORD, phone="07701234567")


@pytest.fixture
def login_url() -> str:
    return reverse("users:login")


class TestLoginPageRendering:
    def test_page_is_reachable_anonymously(self, client: Client, login_url: str) -> None:
        assert client.get(login_url).status_code == 200

    def test_page_renders_rtl_when_arabic_is_selected(self, client: Client, login_url: str) -> None:
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        body = client.get(login_url).content.decode()
        assert 'dir="rtl"' in body
        assert 'lang="ar"' in body

    def test_page_renders_ltr_when_english_is_selected(
        self, client: Client, login_url: str
    ) -> None:
        """The same template must serve both directions."""
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        body = client.get(login_url).content.decode()
        assert 'dir="ltr"' in body
        assert 'lang="en"' in body

    def test_browser_language_cannot_flip_the_layout(self, client: Client, login_url: str) -> None:
        """
        An English browser must not turn the Arabic interface left-to-right.
        Only an explicit choice changes direction.
        """
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ar"
        body = client.get(login_url, headers={"accept-language": "en-US,en;q=0.9"}).content.decode()
        assert 'dir="rtl"' in body

    def test_unsupported_language_choice_falls_back_to_the_default(
        self, client: Client, login_url: str
    ) -> None:
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
        response = client.get(login_url)
        assert response.status_code == 200
        assert response["Content-Language"] == settings.LANGUAGE_CODE

    def test_htmx_is_served_locally_not_from_a_cdn(self, client: Client, login_url: str) -> None:
        """A login page must not make a third-party request."""
        body = client.get(login_url).content.decode()
        assert "vendor/htmx.min.js" in body
        assert "unpkg.com" not in body
        assert "cdn." not in body

    def test_form_carries_a_csrf_token(self, client: Client, login_url: str) -> None:
        assert "csrfmiddlewaretoken" in client.get(login_url).content.decode()

    def test_password_field_is_masked(self, client: Client, login_url: str) -> None:
        assert 'type="password"' in client.get(login_url).content.decode()


class TestStandardLogin:
    def test_login_by_username_redirects_home(
        self, client: Client, login_url: str, user: User
    ) -> None:
        response = client.post(login_url, {"username": "a.hassan", "password": PASSWORD})
        assert response.status_code == 302
        assert response["Location"] == reverse("users:home")

    def test_login_by_phone(self, client: Client, login_url: str, user: User) -> None:
        response = client.post(login_url, {"username": "07701234567", "password": PASSWORD})
        assert response.status_code == 302

    def test_session_is_established(self, client: Client, login_url: str, user: User) -> None:
        client.post(login_url, {"username": "a.hassan", "password": PASSWORD})
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_bad_credentials_rerender_the_page(
        self, client: Client, login_url: str, user: User
    ) -> None:
        response = client.post(login_url, {"username": "a.hassan", "password": "wrong"})
        assert response.status_code == 200
        assert client.session.get("_auth_user_id") is None


class TestHtmxLogin:
    def test_success_returns_hx_redirect(self, client: Client, login_url: str, user: User) -> None:
        """
        htmx must navigate, not swap. Without HX-Redirect the home page would
        be injected into the form element.
        """
        response = client.post(
            login_url, {"username": "a.hassan", "password": PASSWORD}, headers=HTMX_HEADERS
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("users:home")

    def test_success_still_logs_the_user_in(
        self, client: Client, login_url: str, user: User
    ) -> None:
        client.post(login_url, {"username": "a.hassan", "password": PASSWORD}, headers=HTMX_HEADERS)
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_failure_returns_the_swappable_fragment(
        self, client: Client, login_url: str, user: User
    ) -> None:
        response = client.post(
            login_url, {"username": "a.hassan", "password": "wrong"}, headers=HTMX_HEADERS
        )
        # 200, not 4xx: htmx does not swap error responses by default.
        assert response.status_code == 200
        body = response.content.decode()
        assert 'id="login-form"' in body
        # A fragment, not a whole document.
        assert "<html" not in body

    def test_failure_shows_an_error_and_keeps_the_typed_identifier(
        self, client: Client, login_url: str, user: User
    ) -> None:
        response = client.post(
            login_url, {"username": "a.hassan", "password": "wrong"}, headers=HTMX_HEADERS
        )
        body = response.content.decode()
        assert 'class="alert"' in body
        assert "a.hassan" in body

    def test_failure_marks_the_field_invalid(self, client: Client, login_url: str) -> None:
        response = client.post(login_url, {"username": "", "password": ""}, headers=HTMX_HEADERS)
        assert "field--invalid" in response.content.decode()


class TestNoUserEnumerationFromTheWeb:
    def test_unknown_account_and_wrong_password_give_the_same_message(
        self, client: Client, login_url: str, user: User
    ) -> None:
        unknown = client.post(
            login_url, {"username": "nobody", "password": PASSWORD}, headers=HTMX_HEADERS
        ).content.decode()
        wrong = client.post(
            login_url, {"username": "a.hassan", "password": "wrong"}, headers=HTMX_HEADERS
        ).content.decode()

        marker = "بيانات الدخول غير صحيحة"
        assert marker in unknown
        assert marker in wrong

    def test_submitted_password_is_never_echoed_back(
        self, client: Client, login_url: str, user: User
    ) -> None:
        response = client.post(
            login_url,
            {"username": "a.hassan", "password": "wrong-but-secret"},
            headers=HTMX_HEADERS,
        )
        assert "wrong-but-secret" not in response.content.decode()


class TestAccessControl:
    def test_home_requires_authentication(self, client: Client) -> None:
        response = client.get(reverse("users:home"))
        assert response.status_code == 302
        assert reverse("users:login") in response["Location"]

    def test_home_renders_once_authenticated(self, client: Client, user: User) -> None:
        client.force_login(user)
        assert client.get(reverse("users:home")).status_code == 200

    def test_authenticated_user_is_bounced_off_the_login_page(
        self, client: Client, login_url: str, user: User
    ) -> None:
        client.force_login(user)
        assert client.get(login_url).status_code == 302

    def test_inactive_user_cannot_sign_in(self, client: Client, login_url: str, user: User) -> None:
        user.is_active = False
        user.save(update_fields=["is_active"])
        client.post(login_url, {"username": "a.hassan", "password": PASSWORD})
        assert client.session.get("_auth_user_id") is None


class TestLogout:
    def test_get_does_not_end_the_session(self, client: Client, user: User) -> None:
        """A GET logout is CSRF-able from an <img> tag. Django requires POST."""
        client.force_login(user)
        response = client.get(reverse("users:logout"))
        assert response.status_code == 405
        assert client.session.get("_auth_user_id") == str(user.pk)

    def test_post_ends_the_session(self, client: Client, user: User) -> None:
        client.force_login(user)
        response = client.post(reverse("users:logout"))
        assert response.status_code == 302
        assert client.session.get("_auth_user_id") is None
