# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Example plug-in: sign every Demo Bank beneficiary request, the way many payment APIs require.

No AI can guess a shared secret, so this is where a power user steps in - six lines of Python.
Restora loads every .py file in the plug-ins folder (restora.toml -> [plugins] folder).
"""
import hashlib
import hmac

from restora import plugin

SIGNING_KEY = b"demo-signing-key"          # in real life: read it from an environment variable


@plugin.before_request
def sign_beneficiary_requests(request, row):
    if "/beneficiaries" in request.url:
        body = (request.body or "").encode("utf-8")
        request.headers["X-Signature"] = hmac.new(SIGNING_KEY, body, hashlib.sha256).hexdigest()


@plugin.generator("nickname", "beneficiaryNickname")
def friendly_nickname(field, hint):
    return "Rent - Landlord"
