"""OAuth resource verifier and optional rotated service bearer credentials. No secret logging."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os

import jwt
from mcp.server.auth.provider import AccessToken

from market_signal.research.lab.common import strict_json

SCOPE = "context:submit"


class Verifier:
    def __init__(
        self,
        *,
        issuer: str | None = None,
        audience: str | None = None,
        jwks_url: str | None = None,
        principals: dict | None = None,
        tokens: dict | None = None,
    ):
        self.issuer, self.audience = issuer, audience
        self.principals = principals or {}
        self.tokens = tokens or {}
        if any(len(t) < 32 for t in self.tokens):
            raise ValueError("service tokens must contain at least 32 characters")
        if any(
            not isinstance(v, str) or not v or len(v) > 128
            for v in [*self.tokens.values(), *self.principals.values()]
        ):
            raise ValueError("invalid configured integration identity")
        if jwks_url and (
            not jwks_url.startswith("https://")
            or not issuer
            or not issuer.startswith("https://")
            or not audience
            or not audience.startswith("https://")
        ):
            raise ValueError("OAuth requires HTTPS issuer, resource audience and JWKS URL")
        self.jwks = jwt.PyJWKClient(jwks_url, timeout=5, lifespan=300) if jwks_url else None

    @classmethod
    def environment(cls):
        return cls(
            issuer=os.environ.get("PRISM_CONTEXT_OAUTH_ISSUER"),
            audience=os.environ.get("PRISM_CONTEXT_GATEWAY_URL", "").rstrip("/") + "/mcp",
            jwks_url=os.environ.get("PRISM_CONTEXT_OAUTH_JWKS"),
            principals=strict_json(os.environ.get("PRISM_CONTEXT_OAUTH_PRINCIPALS", "{}")),
            tokens=strict_json(os.environ.get("PRISM_CONTEXT_GATEWAY_TOKENS", "{}")),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # Hash before compare so credential length is not a comparison timing oracle.
        digest = hashlib.sha256(token.encode()).digest()
        identity = None
        for secret, candidate in self.tokens.items():
            if hmac.compare_digest(digest, hashlib.sha256(secret.encode()).digest()):
                identity = candidate
        if identity:
            return AccessToken(
                token=token,
                client_id=identity,
                scopes=[SCOPE],
                resource=self.audience,
                claims={"integration_id": identity, "service": True},
            )
        if self.jwks is None:
            return None
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            client = claims.get("azp") or claims.get("client_id")
            identity = self.principals.get(f"{claims['sub']}|{client}")
            scopes = claims.get("scope", "").split()
            if not identity or SCOPE not in scopes:
                return None
            return AccessToken(
                token=token,
                client_id=client,
                subject=claims["sub"],
                scopes=scopes,
                expires_at=claims["exp"],
                resource=self.audience,
                claims={"integration_id": identity, "iss": self.issuer},
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None
