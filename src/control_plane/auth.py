"""GitHub login and platform sessions.

GitHub proves who someone is, and the platform decides whether they may enter. The list of numeric
GitHub ids matters because a session here can move production. GitHub's own token is used once to
read the user and is not kept: the first version does nothing else on their behalf.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx

from control_plane.clock import Clock
from control_plane.domain.models import OAuthRequest, Session, User
from control_plane.errors import ApiError
from control_plane.store.repository import Repository

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"
OAUTH_REQUEST_MINUTES = 5


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_return_path(path: str | None) -> str:
    """Only paths inside the console: anything else would make login an open redirect."""
    if path and path.startswith("/") and not path.startswith(("//", "/\\")):
        return path
    return "/"


@dataclass(frozen=True)
class LoginStart:
    redirect_url: str
    browser_binding: str


@dataclass(frozen=True)
class IssuedSession:
    token: str
    session: Session


class AuthService:
    def __init__(
        self,
        repo: Repository,
        clock: Clock,
        http: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        callback_url: str,
        allowed_github_ids: list[int],
        session_hours: int,
    ) -> None:
        self.repo = repo
        self.clock = clock
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self.allowed = frozenset(allowed_github_ids)
        self.session_hours = session_hours

    async def start_login(self, return_to: str | None) -> LoginStart:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        browser_binding = secrets.token_urlsafe(32)
        await self.repo.save_oauth_request(
            state,
            OAuthRequest(
                code_verifier=verifier,
                browser_binding=_hash(browser_binding),
                return_to=safe_return_path(return_to),
                expires_at=self.clock.now() + timedelta(minutes=OAUTH_REQUEST_MINUTES),
            ),
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.callback_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return LoginStart(f"{GITHUB_AUTHORIZE}?{query}", browser_binding)

    async def finish_login(
        self, *, code: str, state: str, browser_binding: str | None
    ) -> tuple[IssuedSession, str]:
        request = await self.repo.consume_oauth_request(state)
        if request is None or request.expires_at <= self.clock.now():
            raise ApiError(400, "LOGIN_EXPIRED", "this login attempt is no longer valid")
        # The state must come back to the browser that started it, or a link could log a victim
        # in to an attacker's account.
        if not browser_binding or _hash(browser_binding) != request.browser_binding:
            raise ApiError(400, "LOGIN_BROWSER_MISMATCH", "login started in another browser")
        user = await self._github_user(code, request.code_verifier)
        if user.github_id not in self.allowed:
            raise ApiError(403, "NOT_ALLOWED", "this GitHub account has no access")
        return await self._issue(user), request.return_to

    async def _github_user(self, code: str, verifier: str) -> User:
        try:
            token_response = await self.http.post(
                GITHUB_TOKEN,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": self.callback_url,
                    "code_verifier": verifier,
                },
            )
            token = token_response.json().get("access_token")
            if not token:
                raise ApiError(400, "LOGIN_REJECTED", "GitHub did not accept the login code")
            user_response = await self.http.get(
                GITHUB_USER,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            user_response.raise_for_status()
        except (httpx.HTTPError, ValueError) as error:
            raise ApiError(502, "GITHUB_UNAVAILABLE", "could not reach GitHub") from error
        body = user_response.json()
        return User(
            id=f"github:{body['id']}",
            github_id=int(body["id"]),
            login=str(body["login"]),
            avatar_url=str(body.get("avatar_url") or ""),
        )

    async def _issue(self, user: User) -> IssuedSession:
        token = secrets.token_urlsafe(48)
        now = self.clock.now()
        session = Session(
            user=user,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + timedelta(hours=self.session_hours),
            created_at=now,
        )
        # Only the hash is stored: reading the table must not let anyone log in.
        await self.repo.save_session(_hash(token), session)
        return IssuedSession(token, session)

    async def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = await self.repo.session(_hash(token))
        if session is None or session.expires_at <= self.clock.now():
            return None
        return session

    async def logout(self, token: str) -> None:
        await self.repo.delete_session(_hash(token))


LOCAL_USER = User(id="local:developer", github_id=0, login="local-developer")
