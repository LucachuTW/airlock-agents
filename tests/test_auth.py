import uuid

import pytest
from fastapi import HTTPException

from app.auth import decode_token, hash_password, issue_token, verify_password
from app.models import User


def test_password_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert h != hash_password("s3cret")  # salted


def test_token_roundtrip():
    user = User(id=uuid.uuid4(), email="a@b.c", password_hash="x", role="analyst")
    payload = decode_token(issue_token(user))
    assert payload["sub"] == str(user.id)
    assert payload["role"] == "analyst"


def test_invalid_token_rejected():
    with pytest.raises(HTTPException):
        decode_token("not-a-token")
