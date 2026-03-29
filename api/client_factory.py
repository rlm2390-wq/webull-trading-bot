"""
api/client_factory.py
Creates and returns an authenticated Webull client.
"""

import os
from webull import webull
from utils.logger import get_logger

logger = get_logger(__name__)


def get_client() -> webull:
    """
    Create and return an authenticated Webull client using environment variables.

    Expected environment variables:
        WEBULL_USERNAME
        WEBULL_PASSWORD
        WEBULL_DEVICE_ID
        WEBULL_TRADE_PIN
        WEBULL_MFA_CODE (optional)
    """

    username  = os.getenv("WEBULL_USERNAME")
    password  = os.getenv("WEBULL_PASSWORD")
    device_id = os.getenv("WEBULL_DEVICE_ID")
    trade_pin = os.getenv("WEBULL_TRADE_PIN")
    mfa_code  = os.getenv("WEBULL_MFA_CODE")  # optional

    # Validate required credentials
    if not all([username, password, device_id, trade_pin]):
        logger.critical("Missing one or more required Webull environment variables")
        raise RuntimeError("Webull credentials not fully configured")

    # Initialize Webull client
    wb = webull()
    logger.info("Logging into Webull…")

    # Step 1 — Login
    login_response = wb.login(
        username=username,
        password=password,
        device_id=device_id,
        mfa=mfa_code
    )

    if "accessToken" not in login_response:
        logger.critical("Webull login failed: %s", login_response)
        raise RuntimeError("Webull login failed")

    logger.info("Webull login successful")

    # Step 2 — Trade token (PIN)
    token_response = wb.get_trade_token(trade_pin)

    if token_response is False:
        logger.critical("Failed to obtain Webull trade token")
        raise RuntimeError("Trade token failed")

    logger.info("Trade token obtained successfully")

    return wb
