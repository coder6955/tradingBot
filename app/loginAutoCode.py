# ruff: noqa: N999
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.providers.token_store import (
    load_access_token,
    load_legacy_env_access_token,
    save_access_token,
    token_status,
)

logger = logging.getLogger(__name__)


class KiteLoginStepError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def ensure_access_token_for_today() -> dict[str, Any]:
    """Ensure the app has one broker-validated access token for today."""
    started = time.perf_counter()
    try:
        kite = _kite_client()
    except Exception as exc:  # noqa: BLE001 - optional dependency/client boundary
        return _result(False, "kite_client_unavailable", started, exc=exc)

    stored = load_access_token()
    if stored and _token_is_valid(kite, stored):
        return _result(True, "today_token_file", started, created=False)

    legacy = load_legacy_env_access_token()
    if legacy and _token_is_valid(kite, legacy):
        save_access_token(legacy, source="legacy_env_migration")
        return _result(True, "legacy_env_migrated", started, created=True)

    if not settings.kite_auto_login_enabled:
        return _result(False, "automatic_login_disabled", started)

    missing = [
        name
        for name, value in (
            ("KITE_API_KEY", settings.kite_api_key),
            ("KITE_API_SECRET", settings.kite_api_secret),
            ("KITE_USER_ID", settings.kite_user_id),
            ("KITE_PASSWORD", settings.kite_password),
            ("KITE_TOTP_SECRET", settings.kite_totp_secret),
        )
        if not str(value or "").strip()
    ]
    if missing:
        return _result(
            False,
            "automatic_login_credentials_missing",
            started,
            missing_configuration=missing,
        )

    try:
        request_token = _headless_request_token(kite)
        session = kite.generate_session(
            request_token, api_secret=str(settings.kite_api_secret)
        )
        access_token = str(session.get("access_token") or "").strip()
        if not access_token:
            return _result(False, "session_missing_access_token", started)
        if not _token_is_valid(kite, access_token):
            return _result(False, "generated_token_validation_failed", started)
        save_access_token(access_token, source="automatic_headless_login")
        return _result(True, "automatic_headless_login", started, created=True)
    except Exception as exc:  # noqa: BLE001 - browser and broker SDK boundary
        logger.warning("Kite automatic login failed: %s", type(exc).__name__)
        return _result(False, "automatic_login_failed", started, exc=exc)


def _kite_client() -> Any:
    if not settings.kite_api_key:
        raise RuntimeError("KITE_API_KEY is not configured")
    from kiteconnect import KiteConnect

    try:
        return KiteConnect(
            api_key=settings.kite_api_key,
            timeout=max(1, int(settings.kite_api_timeout_seconds)),
        )
    except TypeError:
        return KiteConnect(api_key=settings.kite_api_key)


def _token_is_valid(kite: Any, access_token: str) -> bool:
    try:
        kite.set_access_token(access_token)
        profile = kite.profile()
        return isinstance(profile, dict) and bool(profile)
    except Exception:  # noqa: BLE001 - broker SDK raises several exception types
        return False


def _headless_request_token(kite: Any) -> str:
    import pyotp
    from selenium import webdriver
    from selenium.common.exceptions import StaleElementReferenceException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    secret = re.sub(r"\s+", "", str(settings.kite_totp_secret or "")).upper()
    try:
        otp = pyotp.TOTP(secret).now()
    except Exception as exc:
        raise ValueError(
            "KITE_TOTP_SECRET must be the Base32 setup key, not a six-digit OTP"
        ) from exc

    options = webdriver.ChromeOptions()
    if settings.kite_auto_login_headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=options)
    timeout = max(10, int(settings.kite_auto_login_timeout_seconds))
    driver.set_page_load_timeout(timeout)
    wait = WebDriverWait(driver, timeout)
    try:
        driver.get(kite.login_url())
        try:
            _retry_stale_form_action(
                lambda: _submit_credentials(driver, wait, By),
                stale_exception=StaleElementReferenceException,
            )
        except Exception as exc:
            raise KiteLoginStepError(
                "credentials_form_failed",
                "Kite login could not submit the user ID/password form",
            ) from exc
        try:
            _retry_stale_form_action(
                lambda: _submit_totp(driver, wait, By, otp),
                stale_exception=StaleElementReferenceException,
            )
        except Exception as exc:
            failure_code = _classify_totp_page_failure(driver)
            raise KiteLoginStepError(
                failure_code,
                "Kite login did not reach or accept the TOTP form",
            ) from exc

        try:
            wait.until(
                lambda current: (
                    "request_token=" in str(current.current_url)
                    or "error=" in str(current.current_url)
                )
            )
        except Exception as exc:
            raise KiteLoginStepError(
                "broker_redirect_timeout",
                "Kite login did not return the API request token",
            ) from exc
        query = parse_qs(urlparse(str(driver.current_url)).query)
        if query.get("error"):
            raise RuntimeError("Kite login redirect reported an error")
        request_token = str((query.get("request_token") or [""])[0]).strip()
        if not request_token:
            raise RuntimeError("Kite redirect did not contain request_token")
        return request_token
    finally:
        driver.quit()


def _submit_credentials(driver: Any, wait: Any, by: Any) -> None:
    user_input = wait.until(
        lambda current: _first_element(
            current,
            (
                (by.ID, "userid"),
                (by.CSS_SELECTOR, 'input[autocomplete="username"]'),
                (by.CSS_SELECTOR, 'input[type="text"]'),
            ),
        )
    )
    password_input = wait.until(
        lambda current: _first_element(
            current,
            (
                (by.ID, "password"),
                (by.CSS_SELECTOR, 'input[autocomplete="current-password"]'),
                (by.CSS_SELECTOR, 'input[type="password"]'),
            ),
        )
    )
    user_input.clear()
    user_input.send_keys(str(settings.kite_user_id))
    password_input.clear()
    password_input.send_keys(str(settings.kite_password))
    _submit(driver, by)


def _submit_totp(driver: Any, wait: Any, by: Any, otp: str) -> None:
    otp_input = wait.until(
        lambda current: _first_element(
            current,
            (
                (by.ID, "pin"),
                (by.ID, "totp"),
                (by.CSS_SELECTOR, 'input[name="totp"]'),
                (by.CSS_SELECTOR, 'input[autocomplete="one-time-code"]'),
                (by.CSS_SELECTOR, 'input[inputmode="numeric"]'),
                (by.CSS_SELECTOR, 'input[placeholder*="TOTP" i]'),
                (by.CSS_SELECTOR, 'input[aria-label*="TOTP" i]'),
                (by.CSS_SELECTOR, 'input[type="number"]'),
                (by.CSS_SELECTOR, 'input[type="tel"]'),
            ),
        )
    )
    otp_input.clear()
    otp_input.send_keys(otp)
    _submit(driver, by)


def _classify_totp_page_failure(driver: Any) -> str:
    """Return a secret-free failure class from the broker page shown after login."""
    try:
        page_text = str(driver.find_element("tag name", "body").text or "").lower()
    except Exception:  # noqa: BLE001 - diagnostic only
        return "totp_form_failed"
    if any(
        marker in page_text
        for marker in (
            "invalid user id or password",
            "invalid username or password",
            "incorrect user id or password",
            "incorrect password",
            "invalid credentials",
        )
    ):
        return "credentials_rejected"
    if "captcha" in page_text:
        return "interactive_challenge_required"
    if "too many" in page_text and ("attempt" in page_text or "request" in page_text):
        return "broker_rate_limited"
    if "totp" in page_text or "two-factor" in page_text:
        return "totp_selector_unrecognized"
    return "totp_form_failed"


def _retry_stale_form_action(
    action: Any,
    *,
    stale_exception: type[Exception],
    attempts: int = 4,
) -> None:
    for attempt in range(attempts):
        try:
            action()
            return
        except stale_exception:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.25)


def _first_element(driver: Any, locators: tuple[tuple[str, str], ...]) -> Any | None:
    for by, selector in locators:
        elements = driver.find_elements(by, selector)
        if elements:
            return elements[0]
    return None


def _submit(driver: Any, by: Any) -> None:
    button = _first_element(
        driver,
        (
            (by.CSS_SELECTOR, 'button[type="submit"]'),
            (by.CSS_SELECTOR, 'input[type="submit"]'),
        ),
    )
    if button is None:
        raise RuntimeError("Kite login submit button was not found")
    button.click()


def _result(
    ready: bool,
    source: str,
    started: float,
    *,
    created: bool = False,
    exc: Exception | None = None,
    missing_configuration: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ready": ready,
        "source": source,
        "created": created,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "error_type": type(exc).__name__ if exc is not None else None,
        "error_code": getattr(exc, "code", None) if exc is not None else None,
        "missing_configuration": missing_configuration or [],
        "token_file": token_status(),
    }


def main() -> int:
    result = ensure_access_token_for_today()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    sys.exit(main())
