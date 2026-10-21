"""Token manager for Flow2API with AT auto-refresh"""

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List
from ..core.database import Database
from ..core.config import config
from ..core.models import Token, Project
from ..core.logger import debug_logger
from ..core.monitoring import record_token_refresh
from .flow_client import FlowClient
from .proxy_manager import ProxyManager


class TokenManager:
    """Token lifecycle manager with AT auto-refresh"""

    def __init__(self, db: Database, flow_client: FlowClient):
        self.db = db
        self.flow_client = flow_client
        self._refresh_lock_guard = asyncio.Lock()
        self._project_lock_guard = asyncio.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._project_locks: dict[int, asyncio.Lock] = {}
        self._refresh_futures: dict[int, asyncio.Task] = {}
        self._at_validation_cache: dict[int, float] = {}
        self._last_at_refresh_error: dict[int, str] = {}
        self._protocol_refresher_task: Optional[asyncio.Task] = None

    async def _get_token_lock(
        self,
        lock_map: dict[int, asyncio.Lock],
        guard: asyncio.Lock,
        token_id: int,
    ) -> asyncio.Lock:
        """Acquire a per-token lock to avoid serial blocking between different tokens."""
        async with guard:
            lock = lock_map.get(token_id)
            if lock is None:
                lock = asyncio.Lock()
                lock_map[token_id] = lock
            return lock

    def _get_project_pool_size(self) -> int:
        """Read the currently effective per-Token project pool size config."""
        try:
            return max(1, min(50, int(config.personal_project_pool_size or 4)))
        except Exception:
            return 4

    def _sort_projects(self, projects: List[Project]) -> List[Project]:
        """Sort projects in a stable order for round-robin selection."""
        return sorted(
            projects, key=lambda project: (project.id or 0, project.project_id)
        )

    def _normalize_project_name_base(self, project_name: Optional[str] = None) -> str:
        """Normalize a project base name for pooled creation."""
        raw_name = (project_name or "").strip()
        if raw_name:
            parts = raw_name.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].startswith("P") and parts[1][1:].isdigit():
                return parts[0]
            return raw_name
        return datetime.now().strftime("%b %d - %H:%M")

    def _build_project_name(
        self, pool_index: int, base_name: Optional[str] = None
    ) -> str:
        """Build a project name for the pool."""
        normalized_base = self._normalize_project_name_base(base_name)
        return f"{normalized_base} P{pool_index}"

    def _normalize_protocol_mode(self, value: Optional[str]) -> str:
        mode = (value or "session").strip().lower()
        return "protocol" if mode == "protocol" else "session"

    def _normalize_refresh_interval(self, value: Optional[int]) -> int:
        try:
            return max(1, int(value if value is not None else 30))
        except Exception:
            return 30

    def _is_frontend_session_at(self, token: Optional[Token]) -> bool:
        """Determine whether the AT is the static session identifier of the new Flow frontend.

        The new Flow's AT is a fixed value, and the real session is determined by the full
        Google/Flow Cookie, so its at_expires must not trigger a refresh; otherwise every
        request would re-validate the Cookie.
        """
        at = str(getattr(token, "at", "") or "").strip()
        if not at:
            return False
        frontend_at = str(
            getattr(self.flow_client, "FRONTEND_ACCESS_TOKEN", "") or ""
        ).strip()
        return bool(frontend_at) and at == frontend_at

    def _parse_at_expires(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _as_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if not value:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _normalize_token_proxy_url(self, proxy_url: Optional[str]) -> str:
        raw = (proxy_url or "").strip()
        if not raw:
            return ""
        proxy_manager = getattr(self.flow_client, "proxy_manager", None)
        if proxy_manager and hasattr(proxy_manager, "normalize_proxy_url"):
            normalized = proxy_manager.normalize_proxy_url(raw)
            return normalized or ""
        return raw

    def _clear_at_validation_cache(self, token_id: int) -> None:
        self._at_validation_cache.pop(int(token_id), None)

    def _mark_at_valid(self, token_id: int, ttl_seconds: int = 300) -> None:
        self._at_validation_cache[int(token_id)] = time.monotonic() + max(
            30, int(ttl_seconds or 300)
        )

    def _has_recent_at_validation(self, token_id: int) -> bool:
        expires_at = float(self._at_validation_cache.get(int(token_id), 0.0) or 0.0)
        if expires_at <= time.monotonic():
            self._clear_at_validation_cache(token_id)
            return False
        return True

    def _is_transient_network_failure(self, reason: Optional[str]) -> bool:
        """Determine whether the refresh failure reason is a transient network/proxy error."""
        text = str(reason or "").strip()
        if not text:
            return False
        lowered = text.lower()
        try:
            if self.flow_client._is_proxy_connection_error(Exception(text)):
                return True
        except Exception:
            pass
        try:
            if self.flow_client._is_retryable_network_error(lowered):
                return True
        except Exception:
            pass
        transient_keywords = (
            "timed out",
            "timeout",
            "connection",
            "curl: (",
            "proxy",
            "network",
            "ssl",
            "tls",
            "eof",
            "getaddrinfo",
            "name or service not known",
            "temporarily unavailable",
            "502",
            "503",
            "504",
        )
        return any(keyword in lowered for keyword in transient_keywords)

    def _all_refresh_failures_transient(self, reasons: List[str]) -> bool:
        """Return True only when all meaningful failure reasons are transient network errors."""
        meaningful: List[str] = []
        for reason in reasons:
            text = str(reason or "").strip()
            if not text or text.lower() == "success":
                continue
            meaningful.append(text)
        if not meaningful:
            return False
        return all(self._is_transient_network_failure(text) for text in meaningful)

    async def _flow_call_for_token(self, token: Token, call):
        previous_fingerprint = self.flow_client.get_request_fingerprint()
        proxy_url = (getattr(token, "proxy_url", "") or "").strip()
        if proxy_url:
            next_fingerprint = dict(previous_fingerprint or {})
            next_fingerprint["proxy_url"] = proxy_url
            self.flow_client._set_request_fingerprint(next_fingerprint)
        try:
            return await call()
        finally:
            self.flow_client._set_request_fingerprint(previous_fingerprint)

    async def _st_to_at_for_token(self, token: Token, st: str) -> Dict[str, Any]:
        return await self._flow_call_for_token(
            token,
            lambda: self.flow_client.st_to_at(
                st,
                google_cookies=token.google_cookies,
                token_id=token.id,
            ),
        )

    async def _get_credits_for_token(self, token: Token, at: str) -> Dict[str, Any]:
        return await self._flow_call_for_token(
            token,
            lambda: self.flow_client.get_credits(
                at,
                google_cookies=token.google_cookies,
                token_id=token.id,
            ),
        )

    async def get_personal_warmup_project_ids(
        self,
        tokens: Optional[List[Token]] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        """Return the list of project IDs recommended for warmup at personal-mode startup."""
        token_list = tokens if tokens is not None else await self.get_all_tokens()
        pool_size = self._get_project_pool_size()
        warmup_ids: List[str] = []
        seen_projects: set[str] = set()

        try:
            warmup_limit = None if limit is None else max(1, int(limit))
        except Exception:
            warmup_limit = None

        for token in token_list:
            if not token or not token.is_active:
                continue

            candidate_ids: List[str] = []
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                candidate_ids.append(current_project_id)

            projects = [
                project
                for project in await self.db.get_projects_by_token(token.id)
                if project.is_active
            ]
            for project in self._sort_projects(projects):
                project_id = str(project.project_id or "").strip()
                if project_id and project_id not in candidate_ids:
                    candidate_ids.append(project_id)

            for project_id in candidate_ids[:pool_size]:
                if project_id in seen_projects:
                    continue
                seen_projects.add(project_id)
                warmup_ids.append(project_id)
                if warmup_limit is not None and len(warmup_ids) >= warmup_limit:
                    return warmup_ids

        return warmup_ids

    async def _create_project_for_token(
        self, token: Token, pool_index: int, base_name: Optional[str] = None
    ) -> Project:
        """Create a new pooled project for a token and persist it."""
        project_name = self._build_project_name(pool_index, base_name)
        project_id = await self.flow_client.create_project(
            token.st,
            project_name,
            google_cookies=token.google_cookies,
        )
        debug_logger.log_info(
            f"[PROJECT] Created pooled project for token {token.id}: {project_name} ({project_id})"
        )
        project = Project(
            project_id=project_id,
            token_id=token.id,
            project_name=project_name,
        )
        project.id = await self.db.add_project(project)
        return project

    def _select_next_project(self, token: Token, projects: List[Project]) -> Project:
        """Select the next project from the pool in round-robin order."""
        ordered_projects = self._sort_projects(projects)
        if not ordered_projects:
            raise ValueError("No available projects for token")

        if len(ordered_projects) == 1:
            return ordered_projects[0]

        if token.current_project_id:
            for index, project in enumerate(ordered_projects):
                if project.project_id == token.current_project_id:
                    return ordered_projects[(index + 1) % len(ordered_projects)]

        return ordered_projects[0]

    # ========== Token CRUD ==========

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        return await self.db.get_all_tokens()

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        return await self.db.get_active_tokens()

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        return await self.db.get_token(token_id)

    async def delete_token(self, token_id: int):
        """Delete token"""
        token = await self.db.get_token(token_id)
        project_ids: List[str] = []
        if token:
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                project_ids.append(current_project_id)

        for project in await self.db.get_projects_by_token(token_id):
            project_id = str(project.project_id or "").strip()
            if project_id and project_id not in project_ids:
                project_ids.append(project_id)

        await self.db.delete_token(token_id)

        refresh_task = self._refresh_futures.pop(token_id, None)
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._refresh_locks.pop(token_id, None)
        self._project_locks.pop(token_id, None)

        if config.captcha_method == "personal" and project_ids:
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                for project_id in project_ids:
                    await service.stop_resident_mode(project_id)
            except Exception as e:
                debug_logger.log_warning(
                    f"[DELETE_TOKEN] Failed to clean up personal browser state: {e}"
                )

    async def enable_token(self, token_id: int):
        """Enable a token and reset error count"""
        # Enable the token
        await self.db.update_token(
            token_id, is_active=True, ban_reason=None, banned_at=None
        )
        # Reset error count when enabling (only reset total error_count, keep today_error_count)
        await self.db.reset_error_count(token_id)

    async def disable_token(self, token_id: int):
        """Disable a token"""
        await self.db.update_token(token_id, is_active=False)

    # ========== Token addition (supports Project creation) ==========

    async def add_token(
        self,
        st: str,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: bool = True,
        video_enabled: bool = True,
        image_concurrency: int = -1,
        video_concurrency: int = -1,
        captcha_proxy_url: Optional[str] = None,
        extension_route_key: Optional[str] = None,
        protocol_mode: str = "session",
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: bool = True,
        refresh_interval_minutes: int = 30,
    ) -> Token:
        """Add a new token and prepare its pooled projects."""
        if not st and google_cookies:
            cookie_hash = hashlib.sha256(google_cookies.encode("utf-8")).hexdigest()[
                :32
            ]
            st = f"frontend-{cookie_hash}"
        existing_token = await self.db.get_token_by_st(st) if st else None
        if existing_token:
            raise ValueError(f"Token already exists: {existing_token.email}")

        debug_logger.log_info(f"[ADD_TOKEN] Converting ST to AT...")
        try:
            result = await self.flow_client.st_to_at(
                st,
                google_cookies=google_cookies,
            )
            at = result["access_token"]
            expires = result.get("expires")
            user_info = result.get("user", {})
            email = user_info.get("email", "") or str(login_account or "").strip()
            if not email and google_cookies:
                # The new Flow's session endpoint does not return account info, so read the real login email directly from the Cookie.
                try:
                    identity = await self.detect_account_identity(
                        google_cookies, proxy_url=proxy_url
                    )
                    email = identity["email"]
                    google_cookies = identity["google_cookies"]
                    st = identity["st"]
                except Exception as e:
                    debug_logger.log_warning(
                        f"[ADD_TOKEN] Failed to auto-fetch the account email; using a placeholder email: {e}"
                    )
                    account_hash = hashlib.sha256(
                        google_cookies.encode("utf-8")
                    ).hexdigest()[:16]
                    email = f"account-{account_hash}@flow.local"
            login_account = login_account or email
            existing_identity = await self.db.get_token_by_email(email) if email else None
            if existing_identity:
                raise ValueError(f"Account already exists: {email}")
            name = user_info.get("name", email.split("@")[0] if email else "")
            at_expires = None
            if expires:
                try:
                    at_expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except Exception:
                    pass
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Google Cookie validation failed: {str(e)}")

        try:
            credits_result = await self.flow_client.get_credits(
                at,
                google_cookies=google_cookies,
            )
            credits = credits_result.get("credits", 0)
            user_paygate_tier = credits_result.get("userPaygateTier")
        except Exception:
            credits = 0
            user_paygate_tier = None

        base_project_name = self._normalize_project_name_base(project_name)
        project_pool_size = self._get_project_pool_size()
        pooled_projects: List[Project] = []

        if project_id:
            first_project_name = self._build_project_name(1, base_project_name)
            debug_logger.log_info(
                f"[ADD_TOKEN] Using provided project_id as pooled project #1: {project_id}"
            )
            pooled_projects.append(
                Project(
                project_id=project_id,
                token_id=0,
                project_name=first_project_name,
                    tool_name="PINHOLE",
                )
            )
        else:
            try:
                first_project_name = self._build_project_name(1, base_project_name)
                first_project_id = await self.flow_client.create_project(
                    st,
                    first_project_name,
                    google_cookies=google_cookies,
                )
                debug_logger.log_info(
                    f"[ADD_TOKEN] Created pooled project #1: {first_project_name} (ID: {first_project_id})"
                )
                pooled_projects.append(
                    Project(
                    project_id=first_project_id,
                    token_id=0,
                    project_name=first_project_name,
                        tool_name="PINHOLE",
                    )
                )
            except Exception as e:
                raise ValueError(f"Failed to create project: {str(e)}")

        token = Token(
            st=st,
            at=at,
            at_expires=at_expires,
            email=email,
            name=name,
            remark=remark,
            is_active=True,
            credits=credits,
            user_paygate_tier=user_paygate_tier,
            current_project_id=pooled_projects[0].project_id,
            current_project_name=pooled_projects[0].project_name,
            image_enabled=image_enabled,
            video_enabled=video_enabled,
            image_concurrency=image_concurrency,
            video_concurrency=video_concurrency,
            captcha_proxy_url=captcha_proxy_url,
            extension_route_key=extension_route_key,
            protocol_mode=self._normalize_protocol_mode(protocol_mode),
            google_cookies=(google_cookies or "").strip(),
            login_account=(login_account or "").strip(),
            login_password=login_password or "",
            proxy_url=self._normalize_token_proxy_url(proxy_url),
            auto_refresh_enabled=bool(auto_refresh_enabled),
            refresh_interval_minutes=self._normalize_refresh_interval(
                refresh_interval_minutes
            ),
        )

        token_id = await self.db.add_token(token)
        token.id = token_id
        await self.mark_cookie_validated(token_id)
        token.last_st_refresh_at = datetime.now(timezone.utc)
        token.last_st_refresh_result = "success"

        pooled_projects[0].token_id = token_id
        pooled_projects[0].id = await self.db.add_project(pooled_projects[0])

        while len(pooled_projects) < project_pool_size:
            new_project = await self._create_project_for_token(
                token, len(pooled_projects) + 1, base_project_name
            )
            pooled_projects.append(new_project)

        debug_logger.log_info(
            f"[ADD_TOKEN] Token added successfully (ID: {token_id}, Email: {email}, pooled_projects={len(pooled_projects)})"
        )
        return token

    async def update_token(
        self,
        token_id: int,
        st: Optional[str] = None,
        at: Optional[str] = None,
        at_expires: Optional[datetime] = None,
        email: Optional[str] = None,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: Optional[bool] = None,
        video_enabled: Optional[bool] = None,
        image_concurrency: Optional[int] = None,
        video_concurrency: Optional[int] = None,
        captcha_proxy_url: Optional[str] = None,
        extension_route_key: Optional[str] = None,
        protocol_mode: Optional[str] = None,
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
    ):
        """Update token (supports changing project_id and project_name)

        When the user edits and saves a token, if the token is not expired, the 429 ban state is cleared automatically.
        """
        update_fields = {}
        credential_updated = any(value is not None for value in (st, at, at_expires))

        if st is not None:
            update_fields["st"] = st
        if at is not None:
            update_fields["at"] = at
        if at_expires is not None:
            update_fields["at_expires"] = at_expires
        if email is not None:
            normalized_email = email.strip().lower()
            existing_identity = await self.db.get_token_by_email(normalized_email)
            if existing_identity and existing_identity.id != token_id:
                raise ValueError(f"Account already exists: {normalized_email}")
            update_fields["email"] = normalized_email
        if project_id is not None:
            update_fields["current_project_id"] = project_id
        if project_name is not None:
            update_fields["current_project_name"] = project_name
        if remark is not None:
            update_fields["remark"] = remark
        if image_enabled is not None:
            update_fields["image_enabled"] = image_enabled
        if video_enabled is not None:
            update_fields["video_enabled"] = video_enabled
        if image_concurrency is not None:
            update_fields["image_concurrency"] = image_concurrency
        if video_concurrency is not None:
            update_fields["video_concurrency"] = video_concurrency
        if captcha_proxy_url is not None:
            update_fields["captcha_proxy_url"] = captcha_proxy_url
        if extension_route_key is not None:
            update_fields["extension_route_key"] = extension_route_key
        if protocol_mode is not None:
            update_fields["protocol_mode"] = self._normalize_protocol_mode(
                protocol_mode
            )
        if google_cookies is not None:
            update_fields["google_cookies"] = google_cookies.strip()
        if login_account is not None:
            update_fields["login_account"] = login_account.strip()
        if login_password is not None:
            update_fields["login_password"] = login_password
        if proxy_url is not None:
            update_fields["proxy_url"] = self._normalize_token_proxy_url(proxy_url)
        if auto_refresh_enabled is not None:
            update_fields["auto_refresh_enabled"] = bool(auto_refresh_enabled)
        if refresh_interval_minutes is not None:
            update_fields["refresh_interval_minutes"] = (
                self._normalize_refresh_interval(refresh_interval_minutes)
            )

        # Check whether the token was banned due to 429; if so and it is not expired, clear the 429 state
        token = await self.db.get_token(token_id)
        if credential_updated and token and not token.is_active:
            debug_logger.log_info(
                f"[UPDATE_TOKEN] Token {token_id} credentials updated; automatically restoring to enabled state"
            )
            update_fields["is_active"] = True
            update_fields["ban_reason"] = None
            update_fields["banned_at"] = None

        if token and token.ban_reason == "429_rate_limit":
            # Check whether the token is expired
            is_expired = False
            if token.at_expires:
                now = datetime.now(timezone.utc)
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires
                is_expired = at_expires_aware <= now

            # If not expired, clear the 429 ban state
            if not is_expired:
                debug_logger.log_info(
                    f"[UPDATE_TOKEN] Token {token_id} edited and saved; clearing the 429 ban state"
                )
                update_fields["ban_reason"] = None
                update_fields["banned_at"] = None

        if update_fields:
            if credential_updated:
                self._clear_at_validation_cache(token_id)
            await self.db.update_token(token_id, **update_fields)

    # ========== AT auto-refresh logic (core) ==========

    def _should_refresh_at(self, token: Token) -> bool:
        """Determine whether the AT needs refreshing based on the current token snapshot."""
        if not token.at:
            debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT does not exist, needs refresh")
            return True

        if self._is_frontend_session_at(token):
            # Cookie-driven session: decide by the Cookie refresh interval, not by at_expires.
            if not str(token.google_cookies or "").strip():
                return True
            if not token.auto_refresh_enabled:
                return False
            last_refresh = self._as_utc(token.last_st_refresh_at)
            if not last_refresh:
                return True
            interval_minutes = self._normalize_refresh_interval(
                token.refresh_interval_minutes
            )
            due = last_refresh + timedelta(minutes=interval_minutes)
            if datetime.now(timezone.utc) < due:
                return False
            debug_logger.log_info(
                f"[AT_CHECK] Token {token.id}: Cookie refresh interval reached "
                f"(interval {interval_minutes} minutes), needs refresh"
            )
            return True

        if not token.at_expires:
            debug_logger.log_info(
                f"[AT_CHECK] Token {token.id}: AT expiry time unknown, attempting refresh"
            )
            return True

        now = datetime.now(timezone.utc)
        if token.at_expires.tzinfo is None:
            at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
        else:
            at_expires_aware = token.at_expires

        time_until_expiry = at_expires_aware - now
        if time_until_expiry.total_seconds() < 3600:
            debug_logger.log_info(
                f"[AT_CHECK] Token {token.id}: AT is about to expire "
                f"({time_until_expiry.total_seconds():.0f} seconds remaining), needs refresh"
            )
            return True

        return False

    def needs_at_refresh(self, token: Optional[Token]) -> bool:
        """Let the scheduling layer quickly judge whether the current token is likely to trigger an AT refresh."""
        if not token:
            return True
        return self._should_refresh_at(token)

    async def ensure_valid_token(self, token: Optional[Token]) -> Optional[Token]:
        """Ensure the token's AT is usable and, when necessary, return the latest refreshed object."""
        if not token:
            return None

        needs_refresh = self._should_refresh_at(token)
        if not needs_refresh and self._has_recent_at_validation(token.id):
            # Reuse a session that was just validated, to avoid hitting the upstream on every request.
            return token

        if not needs_refresh:
            try:
                credits_result = await self._get_credits_for_token(token, token.at)
                await self.db.update_token(
                    token.id,
                    credits=credits_result.get("credits", 0),
                    user_paygate_tier=credits_result.get("userPaygateTier"),
                )
                self._mark_at_valid(token.id)
                return await self.db.get_token(token.id) or token
            except Exception as e:
                debug_logger.log_warning(
                    f"[AT_CHECK] Token {token.id}: locally judged as not expired, but upstream validation failed; preparing to refresh AT/ST - {e}"
                )

        if not await self._refresh_at(token.id):
            return None

        return await self.db.get_token(token.id)

    async def is_at_valid(self, token_id: int, token: Optional[Token] = None) -> bool:
        """Check whether the AT is valid; automatically refresh it if invalid or about to expire

        Returns:
            True if AT is valid or refreshed successfully
            False if AT cannot be refreshed
        """
        token_obj = (
            token
            if token and token.id == token_id
            else await self.db.get_token(token_id)
        )
        if not token_obj:
            return False

        valid_token = await self.ensure_valid_token(token_obj)
        return valid_token is not None

    def _is_cookie_driven(self, token: Optional[Token]) -> bool:
        """The new Flow's session is determined by the full Cookie; prefer rotating the Cookie on renewal."""
        if not token or not self._is_frontend_session_at(token):
            return False
        return bool(str(token.google_cookies or "").strip())

    async def _refresh_at_inner(self, token_id: int) -> bool:
        """Perform exactly one real AT refresh attempt."""
        refresh_lock = await self._get_token_lock(
            self._refresh_locks,
            self._refresh_lock_guard,
            token_id,
        )
        async with refresh_lock:
            token = await self.db.get_token(token_id)
            if not token:
                return False

            cookie_driven = self._is_cookie_driven(token)
            if cookie_driven:
                # When the refresh interval is due, renew the Cookie first instead of only re-validating the static AT.
                new_st = await self._try_refresh_st(token_id, token)
                if new_st:
                    return True

            result = await self._do_refresh_at(token_id, token.st, token)
            if result:
                if cookie_driven:
                    # A successful static AT validation means the Cookie is still valid; avoid re-validating.
                    await self.mark_cookie_validated(token_id)
                return True

            debug_logger.log_info(
                f"[AT_REFRESH] Token {token_id}: first AT refresh failed, trying ST refresh..."
            )
            if not cookie_driven:
                new_st = await self._try_refresh_st(token_id, token)
                if new_st:
                    return True

            latest_token = await self.db.get_token(token_id)
            if latest_token and latest_token.google_cookies != token.google_cookies:
                return await self._do_refresh_at(token_id, latest_token.st, latest_token)

            failure_reasons: List[str] = [self._last_at_refresh_error.get(token_id, "")]
            try:
                latest_snapshot = await self.db.get_token(token_id)
                if latest_snapshot is not None:
                    failure_reasons.append(
                        str(latest_snapshot.last_st_refresh_result or "")
                    )
            except Exception:
                pass
            finally:
                self._last_at_refresh_error.pop(token_id, None)

            if self._all_refresh_failures_transient(failure_reasons):
                debug_logger.log_warning(
                    f"[AT_REFRESH] Token {token_id}: refresh failed due to transient "
                    f"network errors, keep token active - {failure_reasons}"
                )
                return False

            debug_logger.log_error(
                f"[AT_REFRESH] Token {token_id}: all refresh attempts failed, disabling token"
            )
            await self.disable_token(token_id)
            self._clear_at_validation_cache(token_id)
            return False

    async def _refresh_at(self, token_id: int) -> bool:
        """Coalesce concurrent AT refresh calls for the same token."""
        existing_task = self._refresh_futures.get(token_id)
        if existing_task:
            return await existing_task

        async def runner() -> bool:
            try:
                return await self._refresh_at_inner(token_id)
            finally:
                current = self._refresh_futures.get(token_id)
                if current is task:
                    self._refresh_futures.pop(token_id, None)

        task = asyncio.create_task(runner())
        self._refresh_futures[token_id] = task
        return await task

    async def _do_refresh_at(
        self, token_id: int, st: str, token: Optional[Token] = None
    ) -> bool:
        """Core logic for performing an AT refresh

        Args:
            token_id: Token ID
            st: Session Token

        Returns:
            True if refresh successful AND AT is valid, False otherwise
        """
        try:
            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: starting AT refresh...")

            # Convert ST to AT
            token = token or await self.db.get_token(token_id)
            result = await (
                self._st_to_at_for_token(token, st)
                if token is not None
                else self.flow_client.st_to_at(st)
            )
            new_at = result["access_token"]
            expires = result.get("expires")

            # Parse the expiry time
            new_at_expires = None
            if expires:
                try:
                    new_at_expires = datetime.fromisoformat(
                        expires.replace("Z", "+00:00")
                    )
                except:
                    pass

            # Update the database
            await self.db.update_token(token_id, at=new_at, at_expires=new_at_expires)

            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: AT refresh succeeded")
            debug_logger.log_info(f"  - New expiry time: {new_at_expires}")

            # Validate AT validity: test via get_credits
            try:
                credits_result = await (
                    self._get_credits_for_token(token, new_at)
                    if token is not None
                    else self.flow_client.get_credits(new_at)
                )
                await self.db.update_token(
                    token_id,
                    credits=credits_result.get("credits", 0),
                    user_paygate_tier=credits_result.get("userPaygateTier"),
                )
                self._mark_at_valid(token_id)
                debug_logger.log_info(
                    f"[AT_REFRESH] Token {token_id}: AT validation succeeded (credits: {credits_result.get('credits', 0)})"
                )
                self._last_at_refresh_error.pop(token_id, None)
                record_token_refresh("at", "success")
                return True
            except Exception as verify_err:
                # AT validation failed (may return 401), meaning the ST has expired
                error_msg = str(verify_err)
                if "401" in error_msg or "UNAUTHENTICATED" in error_msg:
                    debug_logger.log_warning(
                        f"[AT_REFRESH] Token {token_id}: AT validation failed (401); ST may have expired"
                    )
                    self._last_at_refresh_error[token_id] = (
                        f"AT validation failed (401 UNAUTHENTICATED): {error_msg}"
                    )
                    record_token_refresh("at", "failure")
                    return False
                else:
                    # Other errors (e.g. network issues) are still treated as success
                    debug_logger.log_warning(
                        f"[AT_REFRESH] Token {token_id}: a non-authentication error occurred during AT validation: {error_msg}"
                    )
                    self._last_at_refresh_error.pop(token_id, None)
                    record_token_refresh("at", "success")
                    return True

        except Exception as e:
            self._last_at_refresh_error[token_id] = f"{type(e).__name__}: {e}"
            debug_logger.log_error(
                f"[AT_REFRESH] Token {token_id}: AT refresh failed - {str(e)}"
            )
            record_token_refresh("at", "failure")
            return False

    def _is_placeholder_email(self, email: Optional[str]) -> bool:
        value = str(email or "").strip().lower()
        return not value or value.endswith("@flow.local")

    async def detect_account_identity(
        self,
        google_cookies: str,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, str]:
        """Read the Flow login email bound to a Google/Flow Cookie jar.

        Returns the refreshed Cookie storage and the derived session identifier so
        callers can persist the validated snapshot instead of the raw import.
        """
        from .protocol_login import protocol_loginer

        cookies = str(google_cookies or "").strip()
        if not cookies:
            raise ValueError("Google Cookies cannot be empty")
        normalized_proxy = self._normalize_token_proxy_url(proxy_url)
        if not normalized_proxy:
            proxy_manager = getattr(self.flow_client, "proxy_manager", None)
            if proxy_manager is not None:
                try:
                    normalized_proxy = self._normalize_token_proxy_url(
                        await proxy_manager.get_request_proxy_url()
                    )
                except Exception:
                    normalized_proxy = ""
        result = await protocol_loginer.login(
            cookies, proxy=normalized_proxy or None
        )
        if not result.get("success"):
            raise ValueError(
                str(result.get("error") or "Unable to obtain the Flow login email from the Cookie")
            )
        email = str(result.get("email") or "").strip()
        if not email:
            raise ValueError("Unable to obtain the Flow login email from the Cookie")
        refreshed_cookies = str(result.get("google_cookies") or cookies).strip()
        session_token = str(result.get("session_token") or "").strip()
        if not session_token:
            digest = hashlib.sha256(
                refreshed_cookies.encode("utf-8")
            ).hexdigest()[:32]
            session_token = f"frontend-{digest}"
        return {
            "email": email,
            "google_cookies": refreshed_cookies,
            "st": session_token,
        }

    async def mark_cookie_validated(
        self, token_id: int, email: Optional[str] = None
    ) -> None:
        """Record that the current Cookie snapshot was just validated upstream."""
        updates: Dict[str, Any] = {
            "last_st_refresh_at": datetime.now(timezone.utc),
            "last_st_refresh_result": "success",
        }
        normalized_email = str(email or "").strip().lower()
        if normalized_email:
            updates["email"] = normalized_email
            updates["login_account"] = normalized_email
        await self.db.update_token(token_id, **updates)
        self._mark_at_valid(token_id)

    async def _save_refreshed_cookies(
        self, token_id: int, token: Token, result: Dict[str, Any]
    ) -> str:
        """Validate the current account RPC before atomically saving its Cookie jar."""
        from .browser_cookie_utils import validate_flow_cookie_storage

        cookies = str(result.get("google_cookies") or "").strip()
        validate_flow_cookie_storage(cookies)
        expected_raw = (token.login_account or token.email or "").strip()
        expected_email = expected_raw.casefold()
        observed_raw = str(result.get("email") or "").strip()
        observed_email = observed_raw.casefold()
        if not observed_email:
            raise ValueError("The Flow page did not return a logged-in account; please re-sync the Google Cookies")
        # When the real account for the Cookie differs from the local record, auto-correct the email based on the Flow page.
        identity_changed = observed_email != expected_email
        if identity_changed:
            conflict = await self.db.get_token_by_email(observed_raw)
            if conflict is not None and conflict.id != token_id:
                raise ValueError(
                    f"The account {observed_raw} for this Cookie is already used by Token {conflict.id}; "
                    f"please resolve the duplicate account first"
                )
            debug_logger.log_warning(
                f"[COOKIE_REFRESH] Token {token_id}: automatically updating login email "
                f"{expected_raw or '<empty>'} -> {observed_raw}"
            )

        new_st = "frontend-" + hashlib.sha256(cookies.encode("utf-8")).hexdigest()[:32]
        candidate = token.model_copy(update={"google_cookies": cookies, "st": new_st})
        session = await self._st_to_at_for_token(candidate, new_st)
        new_at = str(session.get("access_token") or "").strip()
        if not new_at:
            raise RuntimeError("The Flow session validation response is missing access_token")
        updates = {
            "google_cookies": cookies,
            "st": new_st,
            "at": new_at,
            "at_expires": self._parse_at_expires(session.get("expires")),
            "last_st_refresh_at": datetime.now(timezone.utc),
            "last_st_refresh_result": "success",
        }
        if "credits" in session:
            updates["credits"] = session["credits"]
        if "userPaygateTier" in session:
            updates["user_paygate_tier"] = session["userPaygateTier"]
        if identity_changed:
            updates["email"] = observed_raw
            updates["login_account"] = observed_raw
            current_name = str(token.name or "").strip()
            old_local_part = expected_raw.split("@")[0] if expected_raw else ""
            if (
                not current_name
                or current_name == old_local_part
                or self._is_placeholder_email(current_name)
            ):
                updates["name"] = observed_raw.split("@")[0]
        saved = await self.db.update_token_if_cookie_matches(
            token_id, token.google_cookies, **updates
        )
        if not saved:
            raise RuntimeError("The Cookie was updated during refresh; please retry with the latest session")
        self._mark_at_valid(token_id)
        self._last_at_refresh_error.pop(token_id, None)
        record_token_refresh("st", "success")
        debug_logger.log_info(f"[COOKIE_REFRESH] Token {token_id}: Cookie refreshed and validated successfully")
        return new_st

    async def _record_cookie_refresh_failure(self, token: Token, error: str) -> None:
        await self.db.update_token_if_cookie_matches(
            token.id, token.google_cookies,
            last_st_refresh_at=datetime.now(timezone.utc),
            last_st_refresh_result=error,
        )
        record_token_refresh("st", "failure")

    async def _try_protocol_refresh_st(
        self, token_id: int, token: Token
    ) -> Optional[str]:
        if not (token.google_cookies or "").strip():
            return None
        try:
            from .protocol_login import protocol_loginer

            proxy_url = (token.proxy_url or "").strip()
            if not proxy_url:
                proxy_manager = getattr(self.flow_client, "proxy_manager", None)
                if proxy_manager is not None:
                    proxy_url = await proxy_manager.get_request_proxy_url()
            result = await protocol_loginer.login(
                token.google_cookies, proxy=proxy_url or None,
            )
            if not result.get("success"):
                raise RuntimeError(str(result.get("error") or "Flow Cookie refresh failed"))
            return await self._save_refreshed_cookies(token_id, token, result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            await self._record_cookie_refresh_failure(token, error)
            debug_logger.log_warning(f"[COOKIE_REFRESH] Token {token_id}: protocol refresh failed - {exc}")
            return None

    async def _try_refresh_st(self, token_id: int, token: Token) -> Optional[str]:
        """Refresh full current-Flow Cookies, with an account-bound browser fallback."""
        refreshed = await self._try_protocol_refresh_st(token_id, token)
        if refreshed:
            return refreshed
        latest = await self.db.get_token(token_id)
        if not latest or latest.google_cookies != token.google_cookies:
            # A plugin or another request has supplied a newer Cookie jar.
            return None
        if config.captcha_method not in {"personal", "browser"}:
            return None
        try:
            if config.captcha_method == "personal":
                from .browser_captcha_personal import BrowserCaptchaService
            else:
                from .browser_captcha import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(self.db)
            result = await asyncio.wait_for(
                service.refresh_google_cookies(token.current_project_id, token_id=token_id),
                timeout=45.0,
            )
            return await self._save_refreshed_cookies(token_id, token, result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            await self._record_cookie_refresh_failure(token, error)
            debug_logger.log_warning(f"[COOKIE_REFRESH] Token {token_id}: browser refresh failed - {error}")
            return None

    async def refresh_cookies(self, token_id: int) -> bool:
        """Explicitly renew current-Flow Cookies even when the AT marker is valid."""
        refresh_lock = await self._get_token_lock(
            self._refresh_locks, self._refresh_lock_guard, token_id
        )
        async with refresh_lock:
            token = await self.db.get_token(token_id)
            if not token:
                return False
            if not (token.google_cookies or "").strip():
                return await self._do_refresh_at(token_id, token.st, token)
            if await self._try_refresh_st(token_id, token):
                return True
            latest = await self.db.get_token(token_id)
            if latest and latest.google_cookies != token.google_cookies:
                return await self._do_refresh_at(token_id, latest.st, latest)
            return False

    async def _refresh_protocol_token(self, token: Token, now: datetime) -> None:
        token_id = int(token.id)
        refresh_lock = await self._get_token_lock(
            self._refresh_locks, self._refresh_lock_guard, token_id
        )
        async with refresh_lock:
            latest = await self.db.get_token(token_id)
            if not latest or not latest.is_active or not latest.auto_refresh_enabled:
                return
            if not (latest.google_cookies or "").strip():
                return
            # Cookie and AT metadata are committed together after current RPC validation.
            await self._try_refresh_st(token_id, latest)

    async def run_protocol_refresh_once(self) -> None:
        """Refresh current-Flow Cookie sessions whose refresh interval is due."""
        try:
            refresh_config = await self.db.get_token_refresh_config()
        except Exception as e:
            debug_logger.log_warning(f"[PROTOCOL_REFRESH] Failed to read the refresh config: {e}")
            return

        if not refresh_config or not refresh_config.enabled:
            return

        tokens = await self.db.get_active_tokens()
        now = datetime.now(timezone.utc)
        for token in tokens:
            try:
                if not token.auto_refresh_enabled:
                    continue
                if not (token.google_cookies or "").strip():
                    continue

                interval_minutes = (
                    token.refresh_interval_minutes
                    or refresh_config.refresh_interval_minutes
                    or 30
                )
                interval_minutes = self._normalize_refresh_interval(interval_minutes)
                last_refresh = self._as_utc(token.last_st_refresh_at)
                if last_refresh and now - last_refresh < timedelta(
                    minutes=interval_minutes
                ):
                    continue

                await self._refresh_protocol_token(token, now)
            except Exception as e:
                debug_logger.log_error(
                    f"[PROTOCOL_REFRESH] Token {getattr(token, 'id', '?')}: background refresh exception - {e}"
                )

    async def _protocol_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.run_protocol_refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug_logger.log_error(f"[PROTOCOL_REFRESH] background task exception - {e}")

    def start_protocol_refresher(self) -> None:
        if self._protocol_refresher_task and not self._protocol_refresher_task.done():
            return
        self._protocol_refresher_task = asyncio.create_task(
            self._protocol_refresh_loop()
        )

    async def stop_protocol_refresher(self) -> None:
        task = self._protocol_refresher_task
        self._protocol_refresher_task = None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            debug_logger.log_warning(f"[PROTOCOL_REFRESH] Error while stopping the background task: {e}")

    async def ensure_project_exists(self, token_id: int) -> str:
        """Ensure a token has a pooled set of projects and return one in round-robin order."""
        project_lock = await self._get_token_lock(
            self._project_locks,
            self._project_lock_guard,
            token_id,
        )
        async with project_lock:
            token = await self.db.get_token(token_id)
            if not token:
                raise ValueError("Token not found")

            projects = [
                project
                for project in await self.db.get_projects_by_token(token_id)
                if project.is_active
            ]
            projects = self._sort_projects(projects)

            try:
                project_pool_size = self._get_project_pool_size()
                while len(projects) < project_pool_size:
                    new_project = await self._create_project_for_token(
                        token, len(projects) + 1
                    )
                    projects.append(new_project)
                    projects = self._sort_projects(projects)

                selectable_projects = projects[:project_pool_size]
                selected_project = self._select_next_project(token, selectable_projects)
                await self.db.update_token(
                    token_id,
                    current_project_id=selected_project.project_id,
                    current_project_name=selected_project.project_name,
                )
                return selected_project.project_id
            except Exception as e:
                raise ValueError(f"Failed to prepare project pool: {str(e)}")

    async def record_usage(self, token_id: int, is_video: bool = False):
        """Record token usage"""
        await self.db.update_token(token_id, use_count=1, last_used_at=datetime.now())

        if is_video:
            await self.db.increment_token_stats(token_id, "video")
        else:
            await self.db.increment_token_stats(token_id, "image")

    async def record_error(self, token_id: int):
        """Record token error and auto-disable if threshold reached"""
        await self.db.increment_token_stats(token_id, "error")

        # Check if should auto-disable token (based on consecutive errors)
        stats = await self.db.get_token_stats(token_id)
        admin_config = await self.db.get_admin_config()

        if stats and stats.consecutive_error_count >= admin_config.error_ban_threshold:
            debug_logger.log_warning(
                f"[TOKEN_BAN] Token {token_id} consecutive error count ({stats.consecutive_error_count}) "
                f"reached threshold ({admin_config.error_ban_threshold}), auto-disabling"
            )
            await self.disable_token(token_id)

    async def record_success(self, token_id: int):
        """Record successful request (reset consecutive error count)

        This method resets error_count to 0, which is used for auto-disable threshold checking.
        Note: today_error_count and historical statistics are NOT reset.
        """
        await self.db.reset_error_count(token_id)

    async def ban_token_for_429(self, token_id: int):
        """Immediately disable a token due to a 429 error

        Args:
            token_id: Token ID
        """
        debug_logger.log_warning(
            f"[429_BAN] Disabling Token {token_id} (reason: 429 Rate Limit)"
        )
        await self.db.update_token(
            token_id,
            is_active=False,
            ban_reason="429_rate_limit",
            banned_at=datetime.now(timezone.utc),
        )

    async def auto_unban_429_tokens(self):
        """Automatically unban tokens that were disabled due to 429

        Rules:
        - Automatically unban 12 hours after the ban time
        - Only unban tokens that are not expired
        - Only unban tokens that were disabled due to 429
        """
        all_tokens = await self.db.get_all_tokens()
        now = datetime.now(timezone.utc)

        for token in all_tokens:
            # Skip tokens not banned due to 429
            if token.ban_reason != "429_rate_limit":
                continue

            # Skip tokens that are not disabled
            if token.is_active:
                continue

            # Skip tokens without a ban time
            if not token.banned_at:
                continue

            # Check whether the token is already expired
            if token.at_expires:
                # Ensure the timezone is consistent
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires

                # If already expired, skip
                if at_expires_aware <= now:
                    debug_logger.log_info(
                        f"[AUTO_UNBAN] Token {token.id} is expired; skipping unban"
                    )
                    continue

            # Ensure banned_at timezone is consistent
            if token.banned_at.tzinfo is None:
                banned_at_aware = token.banned_at.replace(tzinfo=timezone.utc)
            else:
                banned_at_aware = token.banned_at

            # Check whether 12 hours have elapsed
            time_since_ban = now - banned_at_aware
            if time_since_ban.total_seconds() >= 12 * 3600:  # 12 hours
                debug_logger.log_info(
                    f"[AUTO_UNBAN] Unbanning Token {token.id} (ban time: {banned_at_aware}, "
                    f"{time_since_ban.total_seconds() / 3600:.1f} hours elapsed)"
                )
                await self.db.update_token(
                    token.id, is_active=True, ban_reason=None, banned_at=None
                )
                # Reset the error count
                await self.db.reset_error_count(token.id)

    # ========== Credits refresh ==========

    async def refresh_credits(self, token_id: int) -> int:
        """Refresh the Token's credits

        Returns:
            credits
        """
        token = await self.db.get_token(token_id)
        if not token:
            return 0

        # Ensure the AT is valid
        token = await self.ensure_valid_token(token)
        if not token:
            return 0

        try:
            result = await self.flow_client.get_credits(
                token.at,
                google_cookies=token.google_cookies,
                token_id=token.id,
            )
            credits = result.get("credits", 0)
            user_paygate_tier = result.get("userPaygateTier")

            # Update the database
            await self.db.update_token(
                token_id,
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

            return credits
        except Exception as e:
            debug_logger.log_error(
                f"Failed to refresh credits for token {token_id}: {str(e)}"
            )
            return 0
