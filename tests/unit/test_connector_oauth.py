import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agentbox.api.errors import APIError
from agentbox.config import Settings
from agentbox.connectors import oauth


@pytest.mark.parametrize("bad_claim", [None, "nonce", "aud", "iss", "exp"])
async def test_google_pkce_and_verified_identity(bad_claim):
    settings = Settings(_env_file=None, google_client_id="google-app", google_client_secret="secret")
    params = parse_qs(urlsplit(oauth.authorize_url(settings, "gmail", "http://localhost/callback", "state", "verifier", "nonce")).query)
    assert params["code_challenge_method"] == ["S256"] and params["nonce"] == ["nonce"]
    assert set(params["scope"][0].split()) == {"openid", "email", *oauth.GMAIL_SCOPES}
    assert params["include_granted_scopes"] == ["false"] and "select_account" in params["prompt"][0]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "test-key"}
    claims = {"iss": "https://accounts.google.com", "aud": "google-app", "sub": "subject", "nonce": "nonce",
              "exp": int(time.time()) + 300, "iat": int(time.time()), "email": "user@gmail.com", "email_verified": True}
    if bad_claim:
        claims[bad_claim] = 1 if bad_claim == "exp" else "wrong"
    encoded = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    def handler(request):
        if request.method == "POST":
            assert b"code_verifier=verifier" in request.content
            return httpx.Response(200, json={"id_token": encoded, "access_token": "access", "refresh_token": "refresh"})
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        if bad_claim:
            with pytest.raises(APIError) as exc:
                await oauth.exchange_code(http, settings, "gmail", "code", "http://localhost/callback", "verifier", "nonce")
            assert exc.value.code == "oauth_identity_invalid"
        else:
            result = await oauth.exchange_code(http, settings, "gmail", "code", "http://localhost/callback", "verifier", "nonce")
            assert result["email"] == "user@gmail.com" and result["auth"] == "oauth"


@pytest.mark.parametrize("invalid", [None, "issuer", "tenant", "nonce", "offline"])
async def test_microsoft_signed_identity_and_tenant_pinning(invalid):
    tenant = "12345678-1234-1234-1234-123456789012"
    settings = Settings(_env_file=None, m365_client_id="microsoft-app", m365_client_secret="secret", m365_tenant=tenant)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "microsoft-key"}
    claims = {"iss": f"https://login.microsoftonline.com/{tenant}/v2.0", "tid": tenant,
              "aud": "microsoft-app", "sub": "subject", "nonce": "nonce", "exp": int(time.time()) + 300,
              "iat": int(time.time()), "preferred_username": "person@example.com"}
    if invalid == "issuer":
        claims["iss"] = "https://attacker.example/v2.0"
    elif invalid == "tenant":
        claims["tid"] = "87654321-1234-1234-1234-123456789012"
        claims["iss"] = f"https://login.microsoftonline.com/{claims['tid']}/v2.0"
    elif invalid == "nonce":
        claims["nonce"] = "wrong"
    tokens = {"id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "microsoft-key"}),
              "access_token": "access", "refresh_token": "refresh"}
    if invalid == "offline":
        tokens.pop("refresh_token")

    def handler(request):
        if request.method == "POST":
            assert f"/{tenant}/oauth2/v2.0/token" in str(request.url)
            assert b"code_verifier=verifier" in request.content
            return httpx.Response(200, json=tokens)
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        if invalid:
            with pytest.raises(APIError, match="Could not verify"):
                await oauth.exchange_code(http, settings, "m365", "code", "http://localhost/callback", "verifier", "nonce")
        else:
            result = await oauth.exchange_code(http, settings, "m365", "code", "http://localhost/callback", "verifier", "nonce")
            assert result["email"] == "person@example.com" and result["oauth_provider"] == "m365"
