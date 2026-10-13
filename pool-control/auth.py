import asyncio
import uuid

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken


class EntraVerifier:
    def __init__(self, tenant, audience, roles, scopes, signing_key=None):
        self.tenant = str(uuid.UUID(tenant))
        self.issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        self.audience, self.roles, self.scopes = audience, set(roles), set(scopes)
        if not audience or not (self.roles or self.scopes):
            raise ValueError("Explicit audience and allowed role/scope required")
        self.signing_key = signing_key
        self.keys = PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys",
            timeout=10)

    async def verify_token(self, token):
        try:
            key = self.signing_key
            if key is None:
                key = (await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)).key
            c = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience,
                           issuer=self.issuer, options={
                               "require": ["exp", "nbf", "iat", "iss", "aud", "tid"]})
            if c["tid"] != self.tenant:
                return None
            roles = c.get("roles", [])
            if not isinstance(c.get("scp", ""), str):
                return None
            scopes = c.get("scp", "").split()
            if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
                return None
            if not (self.roles.intersection(roles) or self.scopes.intersection(scopes)):
                return None
            # oid is the tenant object identity for users and service principals.
            # App-id fallback is accepted only for role-bearing application tokens.
            if c.get("oid"):
                identity = "oid:" + str(uuid.UUID(c["oid"]))
            elif roles and not scopes and c.get("idtyp") == "app":
                identity = "app:" + str(uuid.UUID(c.get("azp") or c["appid"]))
            else:
                return None
            return AccessToken(token=token, client_id=c.get("azp", identity),
                               scopes=["pool.authorized"], expires_at=int(c["exp"]),
                               subject=self.tenant + ":" + identity)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None


def authenticated_owner():
    from mcp.server.auth.middleware.auth_context import get_access_token
    token = get_access_token()
    if token is None or not token.subject:
        raise PermissionError("Verified Entra identity required")
    return token.subject
