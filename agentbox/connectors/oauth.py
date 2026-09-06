"""Bring-your-own OAuth applications; code flow with PKCE, nonce and verified ID tokens."""
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode
from uuid import UUID

import jwt

from agentbox.api.errors import APIError

GMAIL_SCOPES = {"https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"}


def gmail_scopes(scope):
    granted = set(scope.split())
    mail_scopes = {s for s in granted if "/auth/gmail." in s or s == "https://mail.google.com/"}
    if mail_scopes != GMAIL_SCOPES:
        raise APIError(403, "gmail_permissions_invalid", "Allow reading and sending email. If you previously granted full Gmail access, revoke AgentBox in your Google account and connect again.")
    return sorted(granted)


PROVIDERS = {
    "gmail": {"authorize": "https://accounts.google.com/o/oauth2/v2/auth",
              "token": "https://oauth2.googleapis.com/token", "keys": "https://www.googleapis.com/oauth2/v3/certs",
              "scope": "openid email " + " ".join(sorted(GMAIL_SCOPES)),
              "extra": {"access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "false"}},
    "m365": {"authorize": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
             "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
             "keys": "https://login.microsoftonline.com/common/discovery/v2.0/keys",
             "scope": "offline_access openid email https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send",
             "extra": {"prompt": "select_account"}},
}


def credentials(settings, provider):
    prefix = "google" if provider == "gmail" else "m365"
    return getattr(settings, prefix + "_client_id"), getattr(settings, prefix + "_client_secret")


def configured(settings, provider):
    return provider in PROVIDERS and all(credentials(settings, provider))


def authorize_url(settings, provider, redirect_uri, state, verifier, nonce):
    if not configured(settings, provider):
        raise APIError(409, "oauth_not_configured", "The operator must configure an OAuth client ID and secret first.")
    cid, _ = credentials(settings, provider)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": cid, "response_type": "code", "redirect_uri": redirect_uri, "state": state,
              "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
              "scope": PROVIDERS[provider]["scope"], **PROVIDERS[provider]["extra"]}
    return PROVIDERS[provider]["authorize"].format(tenant=settings.m365_tenant) + "?" + urlencode(params)


async def token_request(http, settings, provider, data):
    cid, secret = credentials(settings, provider)
    url = PROVIDERS[provider]["token"].format(tenant=settings.m365_tenant)
    response = await http.post(url, data={"client_id": cid, "client_secret": secret, **data}, timeout=30)
    if response.status_code != 200:
        # Token endpoint bodies can contain credentials; never echo them to the browser or agent.
        raise APIError(502, "oauth_error", f"OAuth token exchange failed ({response.status_code}); reconnect or check the app configuration.")
    return response.json()


async def exchange_code(http, settings, provider, code, redirect_uri, verifier, nonce):
    tokens = await token_request(http, settings, provider, {"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": redirect_uri, "code_verifier": verifier})
    try:
        encoded = tokens["id_token"]
        header = jwt.get_unverified_header(encoded)
        response = await http.get(PROVIDERS[provider]["keys"], timeout=15)
        response.raise_for_status()
        jwk = next(k for k in response.json()["keys"] if k["kid"] == header["kid"])
        cid, _ = credentials(settings, provider)
        claims = jwt.decode(encoded, jwt.PyJWK.from_dict(jwk).key, algorithms=["RS256"], audience=cid,
                            options={"require": ["exp", "iat", "iss", "sub", "nonce"], "verify_iss": False})
        if not secrets.compare_digest(claims["nonce"], nonce):
            raise ValueError("nonce mismatch")
        if provider == "gmail":
            if claims["iss"] not in ("accounts.google.com", "https://accounts.google.com") or not claims.get("email_verified"):
                raise ValueError("invalid Google identity")
            address = claims["email"]
        else:
            tenant = str(UUID(claims["tid"]))
            if claims["iss"] != f"https://login.microsoftonline.com/{tenant}/v2.0":
                raise ValueError("issuer mismatch")
            if settings.m365_tenant not in ("common", "organizations", "consumers"):
                # Pin a tenant when the operator supplies its UUID. Tenant domains should use UUID configuration.
                if tenant != str(UUID(settings.m365_tenant)):
                    raise ValueError("tenant mismatch")
            address = claims.get("email") or claims["preferred_username"]
        if not tokens.get("refresh_token"):
            raise ValueError("offline access not granted")
    except (jwt.PyJWTError, KeyError, ValueError, StopIteration, TypeError) as exc:
        raise APIError(502, "oauth_identity_invalid", "Could not verify the mailbox identity or offline consent. Reconnect.") from exc
    config = {"auth": "oauth", "oauth_provider": provider, "email": address.lower(), "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"], "expires_at": time.time() + int(tokens.get("expires_in", 3600))}
    if provider == "gmail":
        config.update(transport="gmail_api", scopes=gmail_scopes(tokens.get("scope", PROVIDERS[provider]["scope"])))
    return config


async def refresh_if_needed(http, settings, cfg):
    if cfg.get("auth") != "oauth" or float(cfg.get("expires_at", 0)) > time.time() + 120:
        return False
    if http is None:
        raise APIError(503, "oauth_refresh_unavailable", "OAuth refresh requires the runtime HTTP client.")
    tokens = await token_request(http, settings, cfg["oauth_provider"],
                                  {"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]})
    if cfg["oauth_provider"] == "gmail" and tokens.get("scope"):
        cfg["scopes"] = gmail_scopes(tokens["scope"])
    cfg.update(access_token=tokens["access_token"], expires_at=time.time() + int(tokens.get("expires_in", 3600)))
    if tokens.get("refresh_token"):
        cfg["refresh_token"] = tokens["refresh_token"]
    return True
