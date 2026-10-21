"""Flow API Client for VideoFX (Veo)"""

import asyncio
import json
import contextvars
import time
import uuid
import random
import base64
import gzip
import ssl
import re
from typing import Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote, urljoin, urlparse
import urllib.error
import urllib.request
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger
from ..core.config import (
    config,
    get_yescaptcha_min_score,
    FLOW2API_TEAM_CAPTCHA_BASE_URL,
    FLOW2API_TEAM_CAPTCHA_CLIENT_KEY,
    FLOW2API_TEAM_CAPTCHA_METHOD,
    FLOW2API_TEAM_CAPTCHA_TASK_TYPE,
    is_yes_style_captcha_method,
)
from .flow_current import CurrentFlowClientMixin
from .flow_frontend import FlowFrontendMixin

try:
    import httpx
except ImportError:
    httpx = None


class _FlowClientBase(FlowFrontendMixin):
    """VideoFX API client"""

    FLOW_PUBLIC_API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
    FLOW_BROWSER_CHANNEL_HEADER = "stable"
    FLOW_BROWSER_COPYRIGHT_HEADER = "Copyright 2026 Google LLC. All Rights Reserved."
    FLOW_BROWSER_VALIDATION_HEADER = "MRCPrt/rS3JY47x2Yiz9h3ag4U8="
    FLOW_BROWSER_YEAR_HEADER = "2026"
    FLOW_FRONTEND_EXPERIMENT_IDS = (
        "106184493,106256669,105798603,106281924,106259075,106262194,"
        "105993823,106104244,105484652,1714252,105928947,106238955,"
        "106225453,106131447,1706538,119157484,106243706,105746691,"
        "106151974,106125249,106001691,106077941"
    )
    YESCAPTCHA_SLOT_BACKOFF_SECONDS = (3, 5, 8, 13, 20)

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url
        self.api_base_url = config.flow_api_base_url
        self.timeout = config.flow_timeout
        # Cache the User-Agent for each account
        self._user_agent_cache = {}
        # Browser fingerprint bound to the current request chain (based on contextvar to avoid concurrency cross-talk)
        self._request_fingerprint_ctx: contextvars.ContextVar[
            Optional[Dict[str, Any]]
        ] = contextvars.ContextVar("flow_request_fingerprint", default=None)
        self._remote_browser_prefill_last_sent: Dict[str, float] = {}

        # Keep only the minimal browser-style headers that still appear stably on the current upstream; the specific UA / Accept-Language / UA-CH
        # are unified based on the runtime fingerprint bound to the current request chain, no longer compatible with the legacy random-platform strategy.
        self._default_client_headers = {
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        # Dispatch strategy changed to "send as soon as the request arrives":
        # Do not batch-shape or queue submissions locally in flow2api, to avoid staggering requests of the same batch.

    def _generate_user_agent(self, account_id: str = None) -> str:
        """Generate a fixed User-Agent based on the account ID
        
        Args:
            account_id: account identifier (such as email or token_id); the same account returns the same UA
            
        Returns:
            User-Agent string
        """
        if not hasattr(self, "_user_agent_cache"):
            self._user_agent_cache = {}

        # If no account ID is provided, generate a random UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # If already cached, return it directly
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
        
        # Use the account ID as the random seed to ensure the same account generates the same UA
        import hashlib

        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        
        # fallback only applies when there is no runtime fingerprint, aligned directly with the current upstream Windows Chrome style
        chrome_versions = ["136.0.0.0"]
        ch_version = rng.choice(chrome_versions)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ch_version} Safari/537.36"
        )
        
        # Cache the result
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    def _set_request_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        """Set the browser fingerprint context for the current request chain."""
        if not hasattr(self, "_request_fingerprint_ctx"):
            self._request_fingerprint_ctx = contextvars.ContextVar(
                "flow_request_fingerprint",
                default=None,
            )
        self._request_fingerprint_ctx.set(dict(fingerprint) if fingerprint else None)

    def get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """Get a snapshot of the browser fingerprint bound to the current request chain."""
        if not hasattr(self, "_request_fingerprint_ctx"):
            return None
        fingerprint = self._request_fingerprint_ctx.get()
        if not isinstance(fingerprint, dict) or not fingerprint:
            return None
        return dict(fingerprint)

    def clear_request_fingerprint(self):
        """Clear the browser fingerprint bound to the request chain."""
        self._set_request_fingerprint(None)

    def _set_captcha_request_context(self, context: Optional[Dict[str, Any]]):
        if not hasattr(self, "_captcha_request_ctx"):
            self._captcha_request_ctx = contextvars.ContextVar("flow_captcha_request", default=None)
        self._captcha_request_ctx.set(dict(context) if context else None)

    def _get_captcha_request_context(self) -> Dict[str, Any]:
        context = self._captcha_request_ctx.get() if hasattr(self, "_captcha_request_ctx") else None
        return dict(context) if context else {}

    def _current_browser_ref(self, token_id: Optional[int]):
        context = self._get_captcha_request_context()
        if context.get("method") == "browser" and context.get("token_id") == token_id:
            return context.get("browser_ref")
        return None

    def _get_primary_accept_language(self, fallback: str = "zh-CN,zh;q=0.9") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                return self._normalize_accept_language_header(
                    accept_language, fallback=fallback
                )
        return self._normalize_accept_language_header(fallback, fallback=fallback)

    def _get_primary_locale_code(self, fallback: str = "en-US") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            language = str(fingerprint.get("language") or "").strip()
            if language:
                return language
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                primary = accept_language.split(",", 1)[0].strip()
                if primary:
                    return primary
        return fallback

    def _infer_sec_ch_ua_from_user_agent(self, user_agent: Optional[str]) -> str:
        ua = str(user_agent or "").strip()
        if not ua:
            return ""
        import re

        match = re.search(r"(?:Chrome|Chromium)/(\d+)", ua, re.IGNORECASE)
        major = match.group(1) if match else "124"
        return (
            f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"'
        )

    def _normalize_sec_ch_ua_header(
        self,
        sec_ch_ua: Optional[str],
        *,
        user_agent: Optional[str] = None,
    ) -> str:
        raw = str(sec_ch_ua or "").strip()
        inferred = self._infer_sec_ch_ua_from_user_agent(user_agent)
        if not raw:
            return inferred
        ua_text = str(user_agent or "").lower()
        if "chrome/" in ua_text and "google chrome" not in raw.lower():
            return inferred
        return raw or inferred

    def _build_fingerprint_from_user_agent(
        self,
        user_agent: Optional[str],
        *,
        accept_language: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        ua = str(user_agent or "").strip()
        if not ua:
            return {}
        ua_lower = ua.lower()
        platform = '"Windows"'
        mobile = "?0"
        if "android" in ua_lower:
            platform = '"Android"'
            mobile = "?1"
        elif "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
            platform = '"iOS"'
            mobile = "?1"
        elif "mac" in ua_lower:
            platform = '"macOS"'
        elif "linux" in ua_lower or "x11" in ua_lower:
            platform = '"Linux"'
        fingerprint: Dict[str, Any] = {
            "user_agent": ua,
            "sec_ch_ua": self._infer_sec_ch_ua_from_user_agent(ua),
            "sec_ch_ua_mobile": mobile,
            "sec_ch_ua_platform": platform,
        }
        normalized_accept_language = self._normalize_accept_language_header(
            accept_language
        )
        if normalized_accept_language:
            fingerprint["accept_language"] = normalized_accept_language
        if proxy_url:
            fingerprint["proxy_url"] = proxy_url
        return fingerprint

    def _normalize_accept_language_header(
        self,
        accept_language: Optional[str],
        fallback: str = "zh-CN,zh;q=0.9",
    ) -> str:
        raw = str(accept_language or "").strip()
        if not raw:
            return fallback
        if "," in raw:
            normalized_parts: list[str] = []
            for index, item in enumerate(raw.split(",")):
                candidate = str(item or "").strip()
                if not candidate:
                    continue
                language = candidate.split(";", 1)[0].strip()
                if not language:
                    continue
                if index == 0:
                    normalized_parts.append(language)
                    continue
                q_match = re.search(r";\s*q=([0-9.]+)", candidate, re.IGNORECASE)
                q_value = (
                    q_match.group(1)
                    if q_match
                    else f"{max(0.1, 1 - (index * 0.1)):.1f}"
                )
                normalized_parts.append(f"{language};q={q_value}")
            if normalized_parts:
                return ",".join(normalized_parts)
            return fallback
        if "-" in raw:
            primary = raw.split("-", 1)[0].strip()
            if len(primary) == 2 and primary.isalpha():
                return f"{raw},{primary};q=0.9"
        return raw

    def _get_effective_request_user_agent(
        self, account_id: Optional[str] = None
    ) -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            user_agent = str(fingerprint.get("user_agent") or "").strip()
            if user_agent:
                return user_agent
        return self._generate_user_agent(account_id)

    @staticmethod
    def _should_attach_runtime_session_cookies(url: str) -> bool:
        host = str(urlparse(str(url or "")).hostname or "").lower()
        if not host:
            return False
        return any(
            host == candidate or host.endswith(f".{candidate}")
            for candidate in (
                "google.com",
                "googleapis.com",
                "recaptcha.net",
            )
        )

    @staticmethod
    def _merge_cookie_header(
        existing_cookie_header: Optional[str],
        extra_cookies: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        cookie_items: Dict[str, str] = {}
        raw_existing = str(existing_cookie_header or "").strip()
        if raw_existing:
            for part in raw_existing.split(";"):
                item = str(part or "").strip()
                if not item or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                key = str(key or "").strip()
                value = str(value or "").strip()
                if key:
                    cookie_items[key] = value

        if isinstance(extra_cookies, dict):
            for key, value in extra_cookies.items():
                normalized_key = str(key or "").strip()
                normalized_value = str(value or "").strip()
                if (
                    normalized_key
                    and normalized_value
                    and normalized_key not in cookie_items
                ):
                    cookie_items[normalized_key] = normalized_value

        if not cookie_items:
            return raw_existing or None
        return "; ".join(f"{key}={value}" for key, value in cookie_items.items())

    def _build_flow_project_page_url(self, project_id: str) -> str:
        return f"https://flow.google.com/project/{project_id}"

    def _build_current_flow_media_headers(
        self,
        *,
        content_type: str = "application/json",
    ) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": self._get_primary_accept_language(
                fallback="zh-CN,zh;q=0.9"
            ),
            "Content-Type": content_type,
            "Origin": "https://flow.google.com",
            "Priority": "u=1, i",
            "Referer": "https://flow.google.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "sec-fetch-storage-access": "active",
        }
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    def _build_labs_request_context_headers(
        self, project_id: Optional[str]
    ) -> Dict[str, str]:
        return self._build_current_flow_media_headers()

    def _compact_json_dumps(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _encode_trpc_input(self, payload: Dict[str, Any]) -> str:
        return quote(self._compact_json_dumps(payload), safe="")

    @staticmethod
    def _extract_project_id_from_request_payload(
        payload: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        client_context = payload.get("clientContext")
        if isinstance(client_context, dict):
            project_id = str(client_context.get("projectId") or "").strip()
            if project_id:
                return project_id
        requests = payload.get("requests")
        if isinstance(requests, list):
            for item in requests:
                if not isinstance(item, dict):
                    continue
                item_client_context = item.get("clientContext")
                if not isinstance(item_client_context, dict):
                    continue
                project_id = str(item_client_context.get("projectId") or "").strip()
                if project_id:
                    return project_id
        return None

    async def _get_token_st_by_id(self, token_id: Optional[int]) -> Optional[str]:
        if not token_id or self.db is None or not hasattr(self.db, "get_token"):
            return None
        try:
            token = await self.db.get_token(int(token_id))
            st_value = str(getattr(token, "st", "") or "").strip() if token else ""
            return st_value or None
        except Exception as e:
            debug_logger.log_warning(
                f"[VIDEO WARMUP] Failed to read ST for Token-{token_id}: {e}"
            )
            return None

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        use_media_proxy: bool = False,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        allow_urllib_fallback: bool = True,
        apply_default_client_headers: bool = True,
        impersonate: str = "chrome",
    ) -> Dict[str, Any]:
        """Unified HTTP request handling"""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if use_media_proxy and hasattr(
                    self.proxy_manager, "get_media_proxy_url"
                ):
                    proxy_url = await self.proxy_manager.get_media_proxy_url()
                elif hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if (
                respect_fingerprint_proxy
                and isinstance(fingerprint, dict)
                and "proxy_url" in fingerprint
            ):
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None
        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        effective_user_agent = str(
            fingerprint_user_agent or self._generate_user_agent(account_id)
        ).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault(
            "Accept-Language",
            self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
        )

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get(
            "sec_ch_ua_platform"
        ):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get(
            "sec_ch_ua_mobile"
        ):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        if isinstance(
            fingerprint, dict
        ) and self._should_attach_runtime_session_cookies(url):
            origin = (
                str(fingerprint.get("origin") or "").strip()
                or "https://flow.google.com"
            )
            referer = str(fingerprint.get("referer") or "").strip()
            if not referer:
                fingerprint_project_id = str(
                    fingerprint.get("project_id") or ""
                ).strip()
                if fingerprint_project_id:
                    referer = self._build_flow_project_page_url(fingerprint_project_id)
            if origin:
                headers.setdefault("Origin", origin)
            if referer:
                headers.setdefault("Referer", referer)
            merged_cookie_header = self._merge_cookie_header(
                headers.get("Cookie"),
                fingerprint.get("session_cookies"),
            )
            if merged_cookie_header:
                headers["Cookie"] = merged_cookie_header

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(
                json_data
            )
            headers.setdefault("Origin", "https://flow.google.com")
            if derived_project_id:
                headers.setdefault(
                    "Referer", self._build_flow_project_page_url(derived_project_id)
                )

        request_body_for_log = raw_body if raw_body is not None else json_data
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] Submitting request with captcha browser fingerprint: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=request_body_for_log,
                proxy=proxy_url,
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                duration_ms = (time.time() - start_time) * 1000

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms,
                    )

                if response.status_code >= 400:
                    error_reason = f"HTTP Error {response.status_code}"
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        error_reason = (
                            f"HTTP Error {response.status_code}: {response.text[:200]}"
                        )

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(
                        f"[API FAILED] Request Body: {request_body_for_log}"
                    )
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")
                    raise Exception(error_reason)

                return response.json()

        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(
                x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]
            ):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(
                    f"[API FAILED] Request Body: {request_body_for_log}"
                )
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            if allow_urllib_fallback and self._should_fallback_to_urllib(error_msg):
                debug_logger.log_warning(
                    f"[HTTP FALLBACK] curl_cffi request failed, falling back to urllib: {method.upper()} {url}"
                )
                try:
                    return await asyncio.to_thread(
                        self._sync_json_request_via_urllib,
                        method.upper(),
                        url,
                        headers,
                        json_data,
                        proxy_url,
                        request_timeout,
                    )
                except Exception as fallback_error:
                    debug_logger.log_error(
                        f"[HTTP FALLBACK] urllib fallback also failed: {fallback_error}"
                    )
                    raise Exception(
                        f"Flow API request failed: curl={error_msg}; urllib={fallback_error}"
                    )

            raise Exception(f"Flow API request failed: {error_msg}")

    async def _make_text_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        apply_default_client_headers: bool = True,
        return_error_response: bool = False,
        redact_sensitive_logs: bool = False,
        impersonate: str = "chrome",
    ) -> str:
        """Perform a raw text request (such as SSE) and return the response text."""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if (
                respect_fingerprint_proxy
                and isinstance(fingerprint, dict)
                and "proxy_url" in fingerprint
            ):
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None

        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        effective_user_agent = str(
            fingerprint_user_agent or self._generate_user_agent(account_id)
        ).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault(
            "Accept-Language",
            self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
        )

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]
            if self._should_attach_runtime_session_cookies(url):
                origin = (
                    str(fingerprint.get("origin") or "").strip()
                    or "https://flow.google.com"
                )
                referer = str(fingerprint.get("referer") or "").strip()
                if not referer:
                    fingerprint_project_id = str(
                        fingerprint.get("project_id") or ""
                    ).strip()
                    if fingerprint_project_id:
                        referer = self._build_flow_project_page_url(
                            fingerprint_project_id
                        )
                if origin:
                    headers.setdefault("Origin", origin)
                if referer:
                    headers.setdefault("Referer", referer)
            if self._should_attach_runtime_session_cookies(url):
                merged_cookie_header = self._merge_cookie_header(
                    headers.get("Cookie"),
                    fingerprint.get("session_cookies"),
                )
                if merged_cookie_header:
                    headers["Cookie"] = merged_cookie_header

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(
                json_data
            )
            headers.setdefault("Origin", "https://flow.google.com")
            if derived_project_id:
                headers.setdefault(
                    "Referer", self._build_flow_project_page_url(derived_project_id)
                )

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get(
            "sec_ch_ua_platform"
        ):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get(
            "sec_ch_ua_mobile"
        ):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        request_body_for_log = (
            "<redacted>"
            if redact_sensitive_logs
            else (raw_body if raw_body is not None else json_data)
        )
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] Submitting text request with captcha browser fingerprint: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers={} if redact_sensitive_logs else headers,
                body=request_body_for_log,
                proxy=proxy_url,
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                duration_ms = (time.time() - start_time) * 1000
                response_text = response.text

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers={} if redact_sensitive_logs else dict(response.headers),
                        body="<redacted>" if redact_sensitive_logs else response_text,
                        duration_ms=duration_ms,
                    )

                if response.status_code >= 400 and return_error_response:
                    return response_text

                if response.status_code >= 400:
                    error_reason = (
                        f"HTTP Error {response.status_code}: {response_text[:500]}"
                    )
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        pass

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(
                        f"[API FAILED] Request Body: {request_body_for_log}"
                    )
                    debug_logger.log_error(
                        "[API FAILED] Response: <redacted>"
                        if redact_sensitive_logs
                        else f"[API FAILED] Response: {response_text}"
                    )
                    raise Exception(error_reason)

                return response_text

        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(
                x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]
            ):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(
                    f"[API FAILED] Request Body: {request_body_for_log}"
                )
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")
            raise Exception(f"Flow API text request failed: {error_msg}")

    def _should_fallback_to_urllib(self, error_message: str) -> bool:
        """Decide whether to fall back from curl_cffi to urllib."""
        error_lower = (error_message or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
                "curl: (6)",
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (52)",
                "curl: (56)",
                "connection timed out",
                "could not connect",
                "failed to connect",
                "ssl connect error",
                "tls connect error",
                "network is unreachable",
            ]
        )

    def _sync_json_request_via_urllib(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        proxy_url: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """Perform a JSON request using urllib as a network fallback for curl_cffi."""
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        request_headers["Accept-Encoding"] = "identity"

        data = None
        if method.upper() != "GET" and json_data is not None:
            data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )

        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            url=url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            with opener.open(
                request,
                timeout=timeout,
            ) as response:
                payload = response.read()
                status_code = int(response.getcode() or 0)
                content_encoding = str(
                    response.headers.get("Content-Encoding") or ""
                ).lower()
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status_code = int(getattr(exc, "code", 500) or 500)
            content_encoding = str(
                getattr(exc, "headers", {}).get("Content-Encoding") or ""
            ).lower()
            if content_encoding == "gzip" and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass
            body_text = payload.decode("utf-8", errors="replace")
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}") from exc
        except Exception as exc:
            raise Exception(str(exc)) from exc

        if content_encoding == "gzip" and payload:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        body_text = payload.decode("utf-8", errors="replace")
        if status_code >= 400:
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}")

        try:
            return json.loads(body_text) if body_text else {}
        except Exception as exc:
            raise Exception(f"Invalid JSON response: {body_text[:200]}") from exc

    def _is_timeout_error(self, error: Exception) -> bool:
        """Decide whether this is a network timeout, to allow fast-fail retries."""
        error_lower = str(error).lower()
        return any(
            keyword in error_lower
            for keyword in [
            "timed out",
            "timeout",
            "curl: (28)",
            "connection timed out",
            "operation timed out",
            ]
        )

    def _is_proxy_connection_error(self, error: Exception) -> bool:
        """Identify connection failures caused by an unavailable local/upstream proxy."""
        error_lower = str(error).lower()
        return any(
            keyword in error_lower
            for keyword in [
            "failed to connect to 127.0.0.1 port",
            "failed to connect to localhost port",
            "proxyerror",
            "proxy error",
            "failed to connect to proxy",
            "couldn't connect to server",
            "curl: (7)",
            ]
        )

    def _is_retryable_network_error(self, error_str: str) -> bool:
        """Identify retryable TLS/connection network errors."""
        error_lower = (error_str or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "ssl_error_syscall",
            "tls connect error",
            "ssl connect error",
            "connection reset",
            "connection aborted",
            "connection was reset",
            "connection timed out",
            "curl: (28)",
            "timed out",
            "timeout",
            "unexpected eof",
            "empty reply from server",
            "recv failure",
            "send failure",
            "connection refused",
            "network is unreachable",
            "remote host closed connection",
            ]
        )

    def _get_control_plane_timeout(self) -> int:
        """Cap the timeout for lightweight control-plane requests, to avoid auth/project endpoints hanging for a long time."""
        return max(5, min(int(self.timeout or 0) or 120, 10))

    def _get_video_submit_timeout(self) -> int:
        """The video submit endpoint should return the operation quickly, to avoid a single network hang stalling the whole chain."""
        return max(30, min(int(self.timeout or 0) or 120, 75))

    def _get_video_poll_timeout(self) -> int:
        """Video status queries are lightweight polling; the request timeout should not exceed the next polling round by too much."""
        return max(10, min(int(self.timeout or 0) or 120, 45))

    def _resolve_generation_retry_budget(
        self, base_max_retries: int, error: Optional[Union[Exception, str]] = None
    ) -> int:
        """Compute the total number of retries allowed for the current generation chain."""
        effective_max_retries = max(1, int(base_max_retries or 1))
        if error is None:
            return effective_max_retries

        error_str = str(error)
        error_lower = error_str.lower()
        if (
            "recaptcha evaluation failed" in error_lower
            or "recaptcha 验证失败" in error_str
        ):
            return max(
                effective_max_retries,
                int(config.browser_captcha_generation_retries or 6),
            )
        return effective_max_retries

    def _build_realistic_video_submit_headers(self) -> Dict[str, str]:
        """Build video submit headers matching the current upstream real captured-traffic style."""
        return self._build_current_flow_media_headers(
            content_type="text/plain;charset=UTF-8"
        )

    def _resolve_runtime_impersonate(self, fallback: str = "chrome") -> str:
        resolved = self._resolve_impersonate_from_fingerprint(fallback=fallback)
        return resolved or fallback

    def _resolve_impersonate_from_fingerprint(self, fallback: str = "chrome") -> str:
        """Select the closest curl_cffi impersonate based on the browser fingerprint bound to the current request chain."""
        fingerprint = self.get_request_fingerprint()
        if not isinstance(fingerprint, dict):
            return fallback

        ua = str(fingerprint.get("user_agent") or "").strip()
        ua_lower = ua.lower()
        if not ua_lower:
            return fallback

        if "android" in ua_lower:
            return "chrome_android"
        if "edg/" in ua_lower or " edge/" in ua_lower:
            return "edge"
        if (
            "safari/" in ua_lower
            and "chrome/" not in ua_lower
            and "chromium/" not in ua_lower
        ):
            if "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
                return "safari_ios"
            return "safari"

        if "chrome/" not in ua_lower and "chromium/" not in ua_lower:
            return fallback

        return "chrome"

    async def _make_video_api_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        timeout: int,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        action: str = "VIDEO_GENERATION",
    ) -> Dict[str, Any]:
        """Apply a hard deadline to the video API, to avoid an occasional low-level curl_cffi stall leaving the whole request hanging."""
        raw_body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"))
        headers = self._build_realistic_video_submit_headers()
        headers.update(self._build_labs_request_context_headers(project_id))
        try:
            return await asyncio.wait_for(
                self._make_request(
                    method="POST",
                    url=url,
                    headers=headers,
                    json_data=json_data,
                    raw_body=raw_body,
                    use_at=True,
                    at_token=at,
                    timeout=timeout,
                    allow_urllib_fallback=False,
                    apply_default_client_headers=False,
                    impersonate=self._resolve_runtime_impersonate(),
                ),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError as exc:
            raise Exception(
                f"Flow video API request timed out after {timeout}s"
            ) from exc

    async def _acquire_image_launch_gate(
        self,
        token_id: Optional[int],
        token_image_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """Image requests no longer do local dispatch queuing; they go straight to acquiring a token and submitting upstream."""
        return True, 0, 0

    async def _release_image_launch_gate(self, token_id: Optional[int]):
        """Keep the interface shape; currently no local dispatch state needs to be released."""
        return

    async def _acquire_video_launch_gate(
        self,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """Video requests no longer do local dispatch queuing; they go straight to acquiring a token and submitting upstream."""
        return True, 0, 0

    async def _release_video_launch_gate(self, token_id: Optional[int]):
        """Keep the interface shape; currently no local dispatch state needs to be released."""
        return

    async def _make_image_generation_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        attempt_trace: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Image generation requests use a shorter timeout and retry quickly on network timeout."""
        request_timeout = config.flow_image_request_timeout
        total_attempts = max(1, config.flow_image_timeout_retry_count + 1)
        retry_delay = config.flow_image_timeout_retry_delay

        # For the browser/remote-browser captcha chain, prefer keeping the same egress as during captcha solving.
        # Otherwise, switching to the media proxy on the first hop easily triggers reCAPTCHA validation failures and amplifies the long tail.
        fingerprint = self._request_fingerprint_ctx.get()
        has_fingerprint_context = bool(isinstance(fingerprint, dict) and fingerprint)

        has_media_proxy = False
        if self.proxy_manager and config.flow_image_timeout_use_media_proxy_fallback:
            try:
                has_media_proxy = bool(await self.proxy_manager.get_media_proxy_url())
            except Exception:
                has_media_proxy = False
        prefer_media_first = bool(
            has_media_proxy and config.flow_image_prefer_media_proxy
        )

        if has_fingerprint_context and prefer_media_first:
            prefer_media_first = False
            debug_logger.log_info(
                "[IMAGE] Detected captcha browser fingerprint context; the first hop is fixed to the captcha chain;"
                "the media proxy is used only as a fallback on network timeout."
            )

        last_error: Optional[Exception] = None

        for attempt_index in range(total_attempts):
            if has_media_proxy:
                # On two retries, use a "primary chain + backup chain" strategy to avoid getting stuck on the wrong chain each time.
                if attempt_index == 0:
                    prefer_media_proxy = prefer_media_first
                elif attempt_index == 1:
                    prefer_media_proxy = not prefer_media_first
                else:
                    prefer_media_proxy = prefer_media_first
            else:
                prefer_media_proxy = False
            route_label = "media proxy chain" if prefer_media_proxy else "captcha chain"
            http_attempt_started_at = time.time()
            http_attempt_info: Optional[Dict[str, Any]] = None
            if isinstance(attempt_trace, dict):
                http_attempt_info = {
                    "attempt": attempt_index + 1,
                    "route": route_label,
                    "timeout_seconds": request_timeout,
                    "used_media_proxy": bool(prefer_media_proxy),
                }
            try:
                if config.captcha_method == "browser" and project_id:
                    from .browser_captcha import BrowserCaptchaService

                    service = await BrowserCaptchaService.get_instance(self.db)
                    (
                        response_payload,
                        _browser_ref,
                        fingerprint,
                    ) = await service.submit_flow_request(
                        project_id=project_id,
                        action="IMAGE_GENERATION",
                        token_id=token_id,
                        url=url,
                        at_token=at,
                        json_data=json_data,
                        timeout=request_timeout,
                        browser_ref=self._current_browser_ref(token_id),
                    )
                    self._set_request_fingerprint(fingerprint if fingerprint else None)

                    status_code = int(response_payload.get("status") or 0)
                    response_text = response_payload.get("text") or ""
                    if status_code >= 400:
                        error_reason = f"HTTP Error {status_code}"
                        parsed_body = None
                        try:
                            parsed_body = (
                                json.loads(response_text) if response_text else None
                            )
                        except Exception:
                            parsed_body = None
                        if isinstance(parsed_body, dict) and "error" in parsed_body:
                            error_info = parsed_body["error"] or {}
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details or []:
                                if isinstance(detail, dict) and detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                        elif response_text:
                            error_reason = (
                                f"HTTP Error {status_code}: {response_text[:200]}"
                            )
                        error = RuntimeError(error_reason)
                        error.browser_ref = _browser_ref
                        raise error

                    result = json.loads(response_text) if response_text else {}
                else:
                    result = await self._make_request(
                        method="POST",
                        url=url,
                        headers=self._build_labs_request_context_headers(project_id),
                        json_data=json_data,
                        use_at=True,
                        at_token=at,
                        timeout=request_timeout,
                        use_media_proxy=prefer_media_proxy,
                        respect_fingerprint_proxy=not prefer_media_proxy,
                    )
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int(
                        (time.time() - http_attempt_started_at) * 1000
                    )
                    http_attempt_info["success"] = True
                    attempt_trace.setdefault("http_attempts", []).append(
                        http_attempt_info
                    )
                return result
            except Exception as e:
                last_error = e
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int(
                        (time.time() - http_attempt_started_at) * 1000
                    )
                    http_attempt_info["success"] = False
                    http_attempt_info["timeout_error"] = bool(self._is_timeout_error(e))
                    http_attempt_info["error"] = str(e)[:240]
                    attempt_trace.setdefault("http_attempts", []).append(
                        http_attempt_info
                    )
                if getattr(e, "submission_uncertain", False) or not self._is_timeout_error(e) or attempt_index >= total_attempts - 1:
                    raise

                if has_media_proxy and total_attempts > 1:
                    next_prefer_media_proxy = (
                        not prefer_media_proxy
                        if attempt_index == 0
                        else prefer_media_proxy
                    )
                else:
                    next_prefer_media_proxy = prefer_media_proxy
                next_route_label = (
                    "media proxy chain" if next_prefer_media_proxy else "captcha chain"
                )
                debug_logger.log_warning(
                    f"[IMAGE] Image generation request network timeout, preparing to retry quickly "
                    f"({attempt_index + 2}/{total_attempts}), current chain={route_label}, "
                    f"next chain={next_route_label}, timeout={request_timeout}s"
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Image generation request failed")

    # ========== Authentication (uses ST) ==========

    async def st_to_at(self, st: str) -> dict:
        """Convert ST to AT

        Args:
            st: Session Token

        Returns:
            {
                "access_token": "AT",
                "expires": "2025-11-15T04:46:04.000Z",
                "user": {...}
            }
        """
        url = f"{self.labs_base_url}/auth/session"
        try:
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
            )
        except Exception as e:
            if not self._is_proxy_connection_error(e):
                raise

            debug_logger.log_warning(
                f"[AUTH] ST->AT failed via configured proxy, retrying direct connection: {e}"
            )
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
                force_no_proxy=True,
            )

    # ========== Project management (uses ST) ==========

    async def create_project(
        self,
        st: str,
        title: str,
        google_cookies: Optional[str] = None,
    ) -> str:
        """Create a project and return project_id

        Args:
            st: Session Token
            title: project title

        Returns:
            project_id (UUID)
        """
        cookie_storage = str(google_cookies or "").strip()
        if cookie_storage:
            try:
                payload = await self._call_flow_frontend_rpc(
                    rpc_id="jHPbke",
                    argument=["projects/*", [None, [title]], [None, 22]],
                    cookie_header="",
                    timeout=max(
                        self._get_control_plane_timeout(), min(self.timeout, 20)
                    ),
                    cookie_storage=cookie_storage,
                )
                project_id = self._extract_flow_project_id(payload)
                if not project_id:
                    raise RuntimeError(
                        "Flow frontend createProject response missing projectId"
                    )
                return project_id
            except Exception as frontend_error:
                debug_logger.log_warning(
                    "[PROJECT] Flow frontend createProject failed; "
                    f"falling back to the legacy endpoint: {frontend_error}"
                )

        # Legacy Session Token accounts remain usable until their Google cookies are synced.
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {"json": {"projectTitle": title, "toolName": "PINHOLE"}}
        max_retries = config.flow_max_retries
        request_timeout = max(self._get_control_plane_timeout(), min(self.timeout, 15))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_st=True,
                    st_token=st,
                    timeout=request_timeout,
                )
                project_result = (
                    result.get("result", {})
                    .get("data", {})
                    .get("json", {})
                    .get("result", {})
                )
                project_id = project_result.get("projectId")
                if not project_id:
                    raise Exception(
                        "Invalid project.createProject response: missing projectId"
                    )
                return project_id
            except Exception as e:
                last_error = e
                retry_reason = (
                    "Network timeout"
                    if self._is_timeout_error(e)
                    else self._get_retry_reason(str(e))
                )
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[PROJECT] Failed to create project, preparing to retry ({retry_attempt + 2}/{max_retries}) "
                        f"title={title!r}, reason={retry_reason}: {e}"
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to create project")

    async def delete_project(self, st: str, project_id: str):
        """Delete a project

        Args:
            st: Session Token
            project_id: project ID
        """
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {"json": {"projectToDeleteId": project_id}}

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
        )

    # ========== Media retrieval (uses AT) ==========

    async def get_media(self, at: str, media_name: str) -> dict:
        """Get media content (videos return base64-encoded data)

        Google's batchCheckAsyncVideoGenerationStatus endpoint, after a video is generated successfully,
        does not return a download URL. The video content must be fetched via GET /v1/media/{name}.

        Args:
            at: Access Token
            media_name: media name (UUID format)

        Returns:
            {
                "name": "uuid",
                "video": {
                    "encodedVideo": "base64...",
                    "seed": 74602,
                    "prompt": "...",
                    "model": "veo_3_1_t2v_fast",
                    "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
                }
            }
        """
        url = f"{self.api_base_url}/media/{media_name}"
        return await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=max(60, int(self.timeout or 120)),
        )

    # ========== Balance query (uses AT) ==========

    async def get_credits(self, at: str) -> dict:
        """Query balance

        Args:
            at: Access Token

        Returns:
            {
                "credits": 920,
                "userPaygateTier": "PAYGATE_TIER_ONE"
            }
        """
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=self._get_control_plane_timeout(),
        )
        return result

    # ========== Image upload (uses AT) ==========

    def _detect_image_mime_type(self, image_bytes: bytes) -> str:
        """Detect the image MIME type via the file-header magic bytes

        Args:
            image_bytes: image byte data

        Returns:
            MIME type string; defaults to image/jpeg
        """
        if len(image_bytes) < 12:
            return "image/jpeg"

        # WebP: RIFF....WEBP
        if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        # PNG: 89 50 4E 47
        if image_bytes[:4] == b"\x89PNG":
            return "image/png"
        # JPEG: FF D8 FF
        if image_bytes[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        # GIF: GIF87a or GIF89a
        if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        # BMP: BM
        if image_bytes[:2] == b"BM":
            return "image/bmp"
        # JPEG 2000: 00 00 00 0C 6A 50
        if image_bytes[:6] == b"\x00\x00\x00\x0cjP":
            return "image/jp2"

        return "image/jpeg"

    def _convert_to_jpeg(self, image_bytes: bytes) -> bytes:
        """Convert the image to JPEG format

        Args:
            image_bytes: raw image byte data

        Returns:
            image byte data in JPEG format
        """
        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        # If there is an alpha channel, convert to RGB
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        
        output = BytesIO()
        img.save(output, format="JPEG", quality=95)
        return output.getvalue()

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id: Optional[str] = None,
    ) -> str:
        """Upload an image and return mediaId

        Args:
            at: Access Token
            image_bytes: image byte data
            aspect_ratio: image or video aspect ratio (automatically converted to the image format)
            project_id: project ID (usable by the new upload endpoint)

        Returns:
            mediaId
        """
        # Convert the video aspect_ratio to the image aspect_ratio
        # VIDEO_ASPECT_RATIO_LANDSCAPE -> IMAGE_ASPECT_RATIO_LANDSCAPE
        # VIDEO_ASPECT_RATIO_PORTRAIT -> IMAGE_ASPECT_RATIO_PORTRAIT
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # Automatically detect the image MIME type
        mime_type = self._detect_image_mime_type(image_bytes)

        # Encode as base64 (strip the prefix)
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        # Try the new upload endpoint first: /v1/flow/uploadImage
        # On failure, automatically fall back to the legacy endpoint to ensure compatibility
        ext = "png" if "png" in mime_type else "jpg"
        upload_file_name = f"flow2api_upload_{int(time.time() * 1000)}.{ext}"
        new_url = f"{self.api_base_url}/flow/uploadImage"
        normalized_project_id = str(project_id or "").strip()
        new_client_context = {
            "sessionId": self._generate_session_id(),
            "tool": "PINHOLE",
        }
        if normalized_project_id:
            new_client_context["projectId"] = normalized_project_id

        new_json_data = {
            "clientContext": new_client_context,
            "fileName": upload_file_name,
            "imageBytes": image_base64,
            "isHidden": False,
            "isUserUploaded": True,
            "mimeType": mime_type,
        }

        # Compatibility fallback: legacy endpoint :uploadUserImage
        legacy_url = f"{self.api_base_url}:uploadUserImage"
        legacy_json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio,
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER",
            },
        }
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        captcha_method = getattr(config, "captcha_method", "personal")
        if captcha_method == "personal":
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                fingerprint = service.get_last_fingerprint()
                if not fingerprint:
                    await service.get_token(project_id, "uploadUserImage")
                    fingerprint = service.get_last_fingerprint()
                self._set_request_fingerprint(fingerprint)
            except Exception as e:
                debug_logger.log_error(f"[UPLOAD] Failed to pre-fetch fingerprint: {e}")

        for retry_attempt in range(max_retries):
            try:
                new_result = await self._make_request(
                    method="POST",
                    url=new_url,
                    json_data=new_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True,
                )
                media_id = self._extract_media_name(
                    new_result.get("media")
                ) or new_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                if media_id:
                    return media_id
                raise Exception(
                    f"Invalid upload response: missing media id, keys={list(new_result.keys())}"
                )
            except Exception as new_upload_error:
                last_error = new_upload_error
                retry_reason = (
                    "Network timeout"
                    if self._is_timeout_error(new_upload_error)
                    else self._get_retry_reason(str(new_upload_error))
                )

                # The legacy endpoint does not carry projectId; once a project-context upload falls back, it may attach the image to the wrong project.
                if normalized_project_id:
                    if retry_reason and retry_attempt < max_retries - 1:
                        debug_logger.log_warning(
                            f"[UPLOAD] Project-scoped upload hit {retry_reason}, preparing to retry the new endpoint "
                            f"({retry_attempt + 2}/{max_retries}, project_id={normalized_project_id})..."
                        )
                        await asyncio.sleep(1)
                        continue
                    raise RuntimeError(
                        "Project-scoped image upload failed via /flow/uploadImage; "
                        "legacy :uploadUserImage fallback is disabled because it may attach media "
                        f"to a different project (project_id={normalized_project_id})."
                    ) from new_upload_error

                debug_logger.log_warning(
                    f"[UPLOAD] New upload API failed, fallback to legacy endpoint: {new_upload_error}"
                )

            try:
                legacy_result = await self._make_request(
                    method="POST",
                    url=legacy_url,
                    json_data=legacy_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True,
                )

                media_id = legacy_result.get("mediaGenerationId", {}).get(
                    "mediaGenerationId"
                ) or legacy_result.get("media", {}).get("name")
                if media_id:
                    return media_id
                raise Exception(
                    f"Legacy upload response missing media id: keys={list(legacy_result.keys())}"
                )
            except Exception as legacy_upload_error:
                last_error = legacy_upload_error
                retry_reason = self._get_retry_reason(str(legacy_upload_error))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[UPLOAD] Upload hit {retry_reason}, preparing to retry ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to upload image")

    # ========== Image generation (uses AT) - synchronous return ==========

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        token_id: Optional[int] = None,
        token_image_concurrency: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int], Awaitable[None]]] = None,
        google_cookies: Optional[str] = None,
    ) -> tuple[dict, str, Dict[str, Any]]:
        """Generate an image (synchronous return)

        Args:
            at: Access Token
            project_id: project ID
            prompt: the prompt
            model_name: NARWHAL / GEM_PIX / GEM_PIX_2 / IMAGEN_3_5
            aspect_ratio: image aspect ratio
            image_inputs: list of reference images (used for image-to-image)

        Returns:
            (result, session_id, perf_trace)
            result: the generation result returned by upstream
            session_id: the sessionId used by this successful image generation request
            perf_trace: trace of generation retries and chain latency
        """
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"
        frontend_cookie_storage = ""
        if not image_inputs and (google_cookies or token_id is not None):
            try:
                frontend_cookie_storage = (
                    await self._resolve_flow_frontend_cookie_storage(
                        google_cookies=google_cookies,
                        token_id=token_id,
                    )
                )
            except RuntimeError:
                frontend_cookie_storage = ""

        # The frontend attempt is extra; legacy keeps its configured retry budget.
        max_retries = config.flow_max_retries + (1 if frontend_cookie_storage else 0)
        last_error = None
        perf_trace: Dict[str, Any] = {
            "max_retries": max_retries,
            "generation_attempts": [],
        }
        
        for retry_attempt in range(max_retries):
            attempt_trace: Dict[str, Any] = {
                "attempt": retry_attempt + 1,
                "recaptcha_ok": False,
            }
            attempt_started_at = time.time()
            # Re-acquire the reCAPTCHA token on every retry
            recaptcha_started_at = time.time()
            if progress_callback is not None:
                await progress_callback("solving_image_captcha", 38)
            launch_gate_acquired = False
            (
                launch_ok,
                launch_queue_ms,
                launch_stagger_ms,
            ) = await self._acquire_image_launch_gate(
                token_id=token_id,
                token_image_concurrency=token_image_concurrency,
            )
            attempt_trace["launch_queue_ms"] = launch_queue_ms
            attempt_trace["launch_stagger_ms"] = launch_stagger_ms
            if not launch_ok:
                last_error = Exception("Image launch queue wait timeout")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="IMAGE_GENERATION",
                    token_id=token_id,
                    website_url=(
                        self._build_flow_frontend_project_page_url(project_id)
                        if frontend_cookie_storage
                        else None
                    ),
                )
            finally:
                if launch_gate_acquired:
                    await self._release_image_launch_gate(token_id)
            attempt_trace["recaptcha_ms"] = int(
                (time.time() - recaptcha_started_at) * 1000
            )
            attempt_trace["recaptcha_ok"] = bool(recaptcha_token)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] generation",
                )
                if should_retry:
                    continue
                raise last_error
            if progress_callback is not None:
                await progress_callback("submitting_image", 48)
            session_id = self._generate_session_id()

            # Build the request - the new endpoint carries clientContext both at the outer level and inside requests
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
            }

            # The new image endpoint uses a structured prompt + the new-media switch
            request_data = {
                "clientContext": client_context,
                "seed": random.randint(1, 999999),
                "imageModelName": model_name,
                "imageAspectRatio": aspect_ratio,
                "structuredPrompt": {"parts": [{"text": prompt}]},
                "imageInputs": image_inputs or [],
            }

            json_data = {
                "clientContext": client_context,
                "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
                "useNewMedia": True,
                "requests": [request_data],
            }

            try:
                if frontend_cookie_storage:
                    frontend_payload = await self._call_flow_frontend_rpc(
                        rpc_id="ogiZ0b",
                        argument=self._build_frontend_image_generation_argument(
                            project_id=project_id,
                            prompt=prompt,
                            model_name=model_name,
                            aspect_ratio=aspect_ratio,
                            recaptcha_token=recaptcha_token,
                            session_id=str(uuid.uuid4()).upper(),
                        ),
                        cookie_header="",
                        timeout=max(config.flow_image_request_timeout, 90),
                        project_id=project_id,
                        cookie_storage=frontend_cookie_storage,
                    )
                    result = self._normalize_frontend_image_generation_response(
                        frontend_payload
                    )
                else:
                    result = await self._make_image_generation_request(
                        url=url,
                        json_data=json_data,
                        at=at,
                        attempt_trace=attempt_trace,
                        project_id=project_id,
                        token_id=token_id,
                    )
                attempt_trace["success"] = True
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                perf_trace["final_success_attempt"] = retry_attempt + 1
                return result, session_id, perf_trace
            except Exception as e:
                last_error = e
                attempt_trace["success"] = False
                attempt_trace["error"] = str(e)[:240]
                attempt_trace["duration_ms"] = int(
                    (time.time() - attempt_started_at) * 1000
                )
                perf_trace["generation_attempts"].append(attempt_trace)
                if frontend_cookie_storage:
                    debug_logger.log_warning(
                        "[IMAGE] Flow frontend RPC failed; falling back to the legacy "
                        f"generation endpoint: {e}"
                    )
                    frontend_cookie_storage = ""
                    continue
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] generation",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # All retries failed
        perf_trace["final_success_attempt"] = None
        raise last_error

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        user_paygate_tier: str = "PAYGATE_TIER_NOT_PAID",
        session_id: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> str:
        """Upscale the image to 2K/4K

        Args:
            at: Access Token
            project_id: project ID
            media_id: the image's mediaId (media[0]["name"] returned by batchGenerateImages)
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K or UPSAMPLE_IMAGE_RESOLUTION_4K
            user_paygate_tier: user tier (such as PAYGATE_TIER_NOT_PAID / PAYGATE_TIER_ONE)
            session_id: optional; reuses the sessionId of the image generation request

        Returns:
            base64-encoded image data
        """
        url = f"{self.api_base_url}/flow/upsampleImage"

        # 403/reCAPTCHA/500 retry logic - uses the configured maximum retry count
        max_retries = config.flow_max_retries
        last_error = None

        for retry_attempt in range(max_retries):
            # Acquire the reCAPTCHA token - uses the IMAGE_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(
                project_id, action="IMAGE_GENERATION", token_id=token_id
            )
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] upscale",
                )
                if should_retry:
                    continue
                raise last_error
            upsample_session_id = session_id or self._generate_session_id()

            json_data = {
                "mediaId": media_id,
                "targetResolution": target_resolution,
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": upsample_session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
            }

            # 4K/2K upscaling uses a dedicated timeout because the returned base64 payload is very large
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=config.upsample_timeout,
                )

                # Return the base64-encoded image
                return result.get("encodedImage", "")
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] upscale",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        raise last_error

    # ========== Video generation (uses AT) - asynchronous return ==========

    def _extract_media_name(self, media: Any) -> Optional[str]:
        """Extract the media id from the new media object or array."""
        if isinstance(media, list):
            for item in media:
                media_name = self._extract_media_name(item)
                if media_name:
                    return media_name
            return None
        if isinstance(media, dict):
            name = media.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _build_video_text_input(
        self, prompt: str, use_v2_model_config: bool = False
    ) -> Dict[str, Any]:
        # The current Flow upstream video chain uniformly uses structuredPrompt and no longer supports the legacy prompt field.
        return {"structuredPrompt": {"parts": [{"text": prompt}]}}

    def _build_video_media_generation_context(
        self, batch_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return {
            "batchId": batch_id or str(uuid.uuid4()),
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        }

    def _find_nested_string(self, value: Any, keys: tuple[str, ...]) -> Optional[str]:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for candidate in value.values():
                found = self._find_nested_string(candidate, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_nested_string(item, keys)
                if found:
                    return found
        return None

    def _truncate_large_debug_value(self, data: Any, max_length: int = 240) -> Any:
        """Truncate overly long fields in debug output to avoid polluting the console."""
        if isinstance(data, dict):
            return {
                key: self._truncate_large_debug_value(value, max_length=max_length)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [
                self._truncate_large_debug_value(item, max_length=max_length)
                for item in data
            ]
        if isinstance(data, str) and len(data) > max_length:
            return f"{data[:max_length]}... (truncated, total {len(data)} chars)"
        return data

    def _extract_video_status_from_media(
        self, media: Dict[str, Any]
    ) -> tuple[Optional[str], Dict[str, Any]]:
        status_block = (
            media.get("mediaMetadata", {}).get("mediaStatus", {})
            or media.get("mediaStatus", {})
            or {}
        )
        status = (
            status_block.get("mediaGenerationStatus")
            or status_block.get("status")
            or media.get("status")
        )
        return status, status_block if isinstance(status_block, dict) else {}

    def _extract_video_url_from_media(self, media: Dict[str, Any]) -> Optional[str]:
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        candidates = [
            self._find_nested_string(
                video, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")
            ),
            self._find_nested_string(
                media, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")
            ),
            self._find_nested_string(video, ("uri", "url")),
        ]
        for candidate in candidates:
            if candidate and (
                candidate.startswith("http://")
                or candidate.startswith("https://")
                or candidate.startswith("/")
            ):
                return candidate
        return None

    def _extract_video_metadata_from_media(
        self,
        media: Dict[str, Any],
    ) -> Dict[str, Any]:
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        generated_video = (
            video.get("generatedVideo")
            if isinstance(video.get("generatedVideo"), dict)
            else {}
        )
        request_data = (
            media.get("mediaMetadata", {})
            .get("requestData", {})
            .get("videoGenerationRequestData", {})
            if isinstance(media.get("mediaMetadata"), dict)
            else {}
        )
        control_input = (
            request_data.get("videoModelControlInput", {})
            if isinstance(request_data, dict)
            else {}
        )
        dimensions = (
            video.get("dimensions") if isinstance(video.get("dimensions"), dict) else {}
        )

        media_name = self._extract_media_name(media)
        aspect_ratio = (
            self._find_nested_string(generated_video, ("aspectRatio",))
            or self._find_nested_string(video, ("aspectRatio", "videoAspectRatio"))
            or self._find_nested_string(
                control_input, ("videoAspectRatio", "aspectRatio")
        )
            or self._find_nested_string(
                media.get("mediaMetadata", {}), ("videoAspectRatio", "aspectRatio")
        )
        )
        model_name = self._find_nested_string(
            generated_video, ("model",)
        ) or self._find_nested_string(
            control_input, ("videoModelName", "videoModelKey")
        )
        duration = self._find_nested_string(
            dimensions, ("length", "duration")
        ) or self._find_nested_string(generated_video, ("length", "duration"))

        video_metadata: Dict[str, Any] = {}
        if media_name:
            video_metadata["mediaName"] = media_name
            video_metadata["mediaGenerationId"] = media_name
        if aspect_ratio:
            video_metadata["aspectRatio"] = aspect_ratio
        if model_name:
            video_metadata["model"] = model_name
        if duration:
            video_metadata["duration"] = duration

        embedded_url = self._extract_video_url_from_media(media)
        if embedded_url:
            video_metadata["embeddedUrl"] = embedded_url
            video_metadata["fifeUrl"] = embedded_url

        return video_metadata

    def _media_to_video_operation(
        self,
        media: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(media, dict):
            return None

        media_name = self._extract_media_name(media)
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        video_operation = (
            video.get("operation") if isinstance(video.get("operation"), dict) else {}
        )
        operation_name = (
            video_operation.get("name")
            or self._find_nested_string(video_operation, ("name",))
            or media_name
        )
        if not operation_name:
            return None

        project_id = media.get("projectId") or fallback_project_id
        status, status_block = self._extract_video_status_from_media(media)
        operation: Dict[str, Any] = {
            "operation": {
                "name": operation_name,
            },
            "status": status or "MEDIA_GENERATION_STATUS_PENDING",
        }
        if media_name:
            operation["mediaName"] = media_name
        if project_id:
            operation["projectId"] = project_id

        scene_id = (
            media.get("sceneId")
            or media.get("workflowStepId")
            or video_operation.get("sceneId")
        )
        if scene_id:
            operation["sceneId"] = scene_id

        video_metadata = self._extract_video_metadata_from_media(media)
        if video_metadata:
            operation["operation"]["metadata"] = {"video": video_metadata}

        error = status_block.get("error") if isinstance(status_block, dict) else None
        if isinstance(error, dict):
            operation["operation"]["error"] = error

        return operation

    def _merge_video_operations_with_media(
        self,
        operations: List[Dict[str, Any]],
        media_operations: List[Dict[str, Any]],
        fallback_project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        media_by_name: Dict[str, Dict[str, Any]] = {}
        for item in media_operations:
            media_name = item.get("mediaName") or (item.get("operation") or {}).get(
                "name"
            )
            if media_name:
                media_by_name[media_name] = item

        merged: List[Dict[str, Any]] = []
        for raw_operation in operations:
            operation = dict(raw_operation) if isinstance(raw_operation, dict) else {}
            operation_body = dict(operation.get("operation") or {})
            operation["operation"] = operation_body
            name = operation_body.get("name") or operation.get("mediaName")
            media_operation = media_by_name.get(name) if name else None
            if media_operation:
                operation.setdefault("mediaName", media_operation.get("mediaName"))
                operation.setdefault("projectId", media_operation.get("projectId"))
                operation.setdefault("status", media_operation.get("status"))
                operation.setdefault("sceneId", media_operation.get("sceneId"))
                if "metadata" not in operation_body and (
                    media_operation.get("operation") or {}
                ).get("metadata"):
                    operation_body["metadata"] = (
                        media_operation.get("operation") or {}
                    ).get("metadata")
                if "error" not in operation_body and (
                    media_operation.get("operation") or {}
                ).get("error"):
                    operation_body["error"] = (
                        media_operation.get("operation") or {}
                    ).get("error")
            elif fallback_project_id:
                operation.setdefault("projectId", fallback_project_id)
            merged.append(operation)

        return merged

    def _normalize_video_generation_response(
        self,
        result: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return result

        normalized = dict(result)
        media_items = normalized.get("media")
        media_operations: List[Dict[str, Any]] = []
        if isinstance(media_items, list):
            for media in media_items:
                operation = self._media_to_video_operation(
                    media, fallback_project_id=fallback_project_id
                )
                if operation:
                    media_operations.append(operation)

        operations = normalized.get("operations")
        if isinstance(operations, list) and operations:
            normalized["operations"] = self._merge_video_operations_with_media(
                operations,
                media_operations,
                fallback_project_id=fallback_project_id,
            )
        elif media_operations:
            normalized["operations"] = media_operations

        return normalized

    def _build_video_media_generation_context(self, batch_id: str) -> Dict[str, Any]:
        return {
            "batchId": batch_id,
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        }

    def _operations_to_media_refs(
        self,
        operations: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        media_refs: List[Dict[str, str]] = []
        for operation in operations or []:
            if not isinstance(operation, dict):
                continue
            operation_body = operation.get("operation") or {}
            media_name = (
                operation.get("mediaName")
                or operation.get("name")
                or operation_body.get("name")
            )
            project_id = (
                operation.get("projectId")
                or operation.get("project_id")
                or operation_body.get("projectId")
            )
            if (
                isinstance(media_name, str)
                and media_name.strip()
                and isinstance(project_id, str)
                and project_id.strip()
            ):
                media_refs.append(
                    {
                    "name": media_name.strip(),
                    "projectId": project_id.strip(),
                    }
                )
        return media_refs

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        """Text-to-video; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            prompt: the prompt
            model_key: veo_3_1_t2v_fast, etc.
            aspect_ratio: video aspect ratio
            user_paygate_tier: user tier

        Returns:
            {
                "operations": [{
                    "operation": {"name": "task_id"},
                    "sceneId": "uuid",
                    "status": "MEDIA_GENERATION_STATUS_PENDING"
                }],
                "remainingCredits": 900
            }
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"
        frontend_cookie_storage = ""
        if google_cookies or token_id is not None:
            try:
                frontend_cookie_storage = (
                    await self._resolve_flow_frontend_cookie_storage(
                        google_cookies=google_cookies,
                        token_id=token_id,
                    )
                )
            except RuntimeError:
                frontend_cookie_storage = ""

        # 403/reCAPTCHA retry logic - allows a higher retry limit when reCAPTCHA evaluation failed
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        if frontend_cookie_storage:
            max_retries += 1
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Re-acquire the reCAPTCHA token on every retry - videos use the VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                    website_url=(
                        self._build_flow_frontend_project_page_url(project_id)
                        if frontend_cookie_storage
                        else None
                    ),
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] generation",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not frontend_cookie_storage and not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(
                    prompt, use_v2_model_config=True
                ),
                "videoModelKey": model_key,
                "metadata": {},
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                if frontend_cookie_storage:
                    frontend_payload = await self._call_flow_frontend_rpc(
                        rpc_id="YhhmEf",
                        argument=self._build_frontend_video_generation_argument(
                            project_id=project_id,
                            prompt=prompt,
                            model_key=model_key,
                            recaptcha_token=recaptcha_token,
                            session_id=str(uuid.uuid4()).upper(),
                        ),
                        cookie_header="",
                        timeout=max(self._get_video_submit_timeout(), 90),
                        project_id=project_id,
                        cookie_storage=frontend_cookie_storage,
                    )
                    return self._normalize_frontend_video_submission(
                        frontend_payload,
                        project_id=project_id,
                        aspect_ratio=aspect_ratio,
                        model_key=model_key,
                    )
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result,
                    fallback_project_id=project_id,
                )
            except Exception as e:
                last_error = e
                if frontend_cookie_storage:
                    debug_logger.log_warning(
                        "[VIDEO T2V] Flow frontend RPC failed; falling back to the "
                        f"legacy generation endpoint: {e}"
                    )
                    frontend_cookie_storage = ""
                    retry_attempt += 1
                    continue
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] generation",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    def _build_browser_style_control_headers(
        self,
        referer: str,
        origin: Optional[str] = None,
        account_id: Optional[str] = None,
        content_type: Optional[str] = None,
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": referer,
            "User-Agent": self._get_effective_request_user_agent(account_id),
            "Accept-Language": accept_language
            or self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
            "Priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        if origin:
            headers["Origin"] = origin
            if origin == "https://flow.google.com":
                headers.setdefault("sec-fetch-storage-access", "active")
        if content_type:
            headers["Content-Type"] = content_type
        if api_key:
            headers["x-goog-api-key"] = api_key
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    async def _labs_trpc_get_with_st(
        self,
        path_with_query: str,
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        page_url = self._build_flow_project_page_url(project_id)
        return await self._make_request(
            method="GET",
            url=f"{self.labs_base_url}/trpc/{path_with_query}",
            headers=self._build_browser_style_control_headers(
                referer=page_url,
                account_id=st[:16],
                content_type="application/json",
            ),
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _labs_trpc_post_with_st(
        self,
        trpc_path: str,
        payload: Dict[str, Any],
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        page_url = self._build_flow_project_page_url(project_id)
        return await self._make_request(
            method="POST",
            url=f"{self.labs_base_url}/trpc/{trpc_path}",
            headers=self._build_browser_style_control_headers(
                referer=page_url,
                origin="https://flow.google.com",
                account_id=st[:16],
                content_type="application/json",
            ),
            json_data=payload,
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _aisandbox_request(
        self,
        method: str,
        path: str,
        at: Optional[str],
        *,
        json_data: Optional[Dict[str, Any]] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        content_type: Optional[str] = "text/plain;charset=UTF-8",
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = self._build_browser_style_control_headers(
            referer="https://flow.google.com/",
            origin="https://flow.google.com",
            account_id=account_id,
            content_type=content_type,
            accept_language=accept_language,
            api_key=api_key,
        )
        return await self._make_request(
            method=method,
            url=f"{self.api_base_url}{path}",
            headers=headers,
            json_data=json_data,
            raw_body=raw_body,
            use_at=bool(at),
            at_token=at,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _warmup_flow_video_frontend_context(
        self,
        *,
        at: str,
        project_id: str,
        token_id: Optional[int],
        session_id: str,
        user_paygate_tier: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
    ) -> None:
        """Fill in the minimal first-party initialization/telemetry chain before video submission, following the current upstream real page order."""
        account_id = at[:16] if at else None
        page_url = self._build_flow_project_page_url(project_id)
        session_create_time = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        st = await self._get_token_st_by_id(token_id)
        if st:
            null_meta_input = self._encode_trpc_input(
                {
                "json": None,
                "meta": {"values": ["undefined"]},
                }
            )
            labs_get_paths = [
                f"general.fetchUserPreferences?input={null_meta_input}",
                f"videoFx.getFlowAppConfig?input={null_meta_input}",
                f"videoFx.getUserSettings?input={null_meta_input}",
            ]
            for path_with_query in labs_get_paths:
                try:
                    await self._labs_trpc_get_with_st(
                        path_with_query, st=st, project_id=project_id
                    )
                except Exception as e:
                    debug_logger.log_warning(
                        f"[VIDEO WARMUP] Labs GET failed ({path_with_query}): {e}"
                    )

            labs_batch_log_payload = {
                "json": {
                    "appEvents": [
                        {
                        "event": "PAGE_VIEW",
                        "eventProperties": [
                            {"key": "URL", "stringValue": page_url},
                                {
                                    "key": "USER_AGENT",
                                    "stringValue": self._get_effective_request_user_agent(
                                        st[:16]
                                    ),
                                },
                            {"key": "IS_DESKTOP"},
                        ],
                        "activeExperiments": [],
                        "eventMetadata": {"sessionId": session_id},
                        "eventTime": session_create_time,
                        }
                    ]
                }
            }
            try:
                await self._labs_trpc_post_with_st(
                    "general.submitBatchLog",
                    payload=labs_batch_log_payload,
                    st=st,
                    project_id=project_id,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[VIDEO WARMUP] Labs submitBatchLog failed: {e}"
                )

        try:
            await self._aisandbox_request(
                "POST",
                ":checkAppAvailability",
                at=None,
                raw_body=self._compact_json_dumps(
                    {"clientContext": {"tool": "PINHOLE"}}
                ),
                api_key=self.FLOW_PUBLIC_API_KEY,
                account_id=account_id,
            )
        except Exception as e:
            debug_logger.log_warning(f"[VIDEO WARMUP] checkAppAvailability failed: {e}")

        debug_logger.log_info(
            f"[VIDEO WARMUP] Current minimal video initialization chain completed: project_id={project_id}, "
            f"session_id={session_id}, model_key={model_key}, aspect_ratio={aspect_ratio}, "
            f"user_paygate_tier={user_paygate_tier}, prompt_len={len(prompt or '')}"
        )

    def _video_aspect_ratio_to_agent_aspect_ratio(self, aspect_ratio: str) -> str:
        mapping = {
            "VIDEO_ASPECT_RATIO_LANDSCAPE": "16:9",
            "VIDEO_ASPECT_RATIO_PORTRAIT": "9:16",
            "VIDEO_ASPECT_RATIO_SQUARE": "1:1",
        }
        return mapping.get(str(aspect_ratio or "").strip(), "16:9")

    def _parse_sse_json_events(self, raw_text: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if not raw_text:
            return events

        for block in raw_text.split("\n\n"):
            block = block.strip()
            if not block:
                continue

            data_lines = [
                line[5:].strip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue

            payload_text = "\n".join(data_lines).strip()
            if not payload_text or payload_text == "[DONE]":
                continue

            try:
                payload = json.loads(payload_text)
            except Exception:
                continue

            if isinstance(payload, dict):
                events.append(payload)

        return events

    def _extract_agent_session_id(
        self, sessions_payload: Dict[str, Any]
    ) -> Optional[str]:
        if not isinstance(sessions_payload, dict):
            return None

        sessions = sessions_payload.get("sessions")
        if isinstance(sessions, list):
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                session_id = str(session.get("agentSessionId") or "").strip()
                if session_id:
                    return session_id

        session_info = sessions_payload.get("sessionInfo")
        if isinstance(session_info, dict):
            session_id = str(session_info.get("agentSessionId") or "").strip()
            if session_id:
                return session_id

        return None

    def _extract_flow_entity_id(self, entity_payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(entity_payload, dict):
            return None

        candidates = [entity_payload]
        result = entity_payload.get("result")
        if isinstance(result, dict):
            candidates.append(result)
            data = result.get("data")
            if isinstance(data, dict):
                candidates.append(data)
                json_node = data.get("json")
                if isinstance(json_node, dict):
                    candidates.append(json_node)
                    nested_result = json_node.get("result")
                    if isinstance(nested_result, dict):
                        candidates.append(nested_result)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for key in ("entityId", "id", "parentEntityId"):
                entity_id = str(candidate.get(key) or "").strip()
                if entity_id:
                    return entity_id

        return None

    def _extract_turn_count(self, session_detail: Dict[str, Any]) -> int:
        if not isinstance(session_detail, dict):
            return 0
        turns = session_detail.get("turns")
        if isinstance(turns, list):
            return len(turns)
        return 0

    def _extract_generate_video_with_references_result(
        self,
        events: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for event in events:
            if not isinstance(event, dict):
                continue
            agent_message = event.get("agentMessage")
            if not isinstance(agent_message, dict):
                continue
            agent_events = agent_message.get("agentEvents")
            if not isinstance(agent_events, list):
                continue
            for agent_event in agent_events:
                if not isinstance(agent_event, dict):
                    continue
                tool_result_wrapper = agent_event.get("toolResult")
                if not isinstance(tool_result_wrapper, dict):
                    continue
                if (
                    tool_result_wrapper.get("toolName")
                    != "generate_video_with_references"
                ):
                    continue
                tool_result = tool_result_wrapper.get("toolResult")
                if isinstance(tool_result, dict):
                    return tool_result
        return None

    async def get_flow_creation_agent_session(
        self,
        at: str,
        project_id: str,
        *,
        account_id: Optional[str] = None,
        allow_global_fallback: bool = True,
    ) -> Optional[str]:
        sessions_result = await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions?projectId={quote(project_id, safe='')}",
            at=at,
            content_type=None,
            account_id=account_id,
        )
        session_id = self._extract_agent_session_id(sessions_result)
        if session_id or not allow_global_fallback:
            return session_id

        global_sessions_result = await self._aisandbox_request(
            "GET",
            "/flowCreationAgent/sessions",
            at=at,
            content_type=None,
            account_id=account_id,
        )
        return self._extract_agent_session_id(global_sessions_result)

    async def get_flow_creation_agent_session_detail(
        self,
        at: str,
        agent_session_id: str,
        *,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions/{quote(agent_session_id, safe='')}",
            at=at,
            content_type=None,
            account_id=account_id,
        )

    async def create_flow_entity(
        self,
        st: str,
        project_id: str,
    ) -> str:
        payload = {"json": {"projectId": project_id}}
        result = await self._labs_trpc_post_with_st(
            "flow.createEntity",
            payload=payload,
            st=st,
            project_id=project_id,
        )
        entity_id = self._extract_flow_entity_id(result)
        if not entity_id:
            raise RuntimeError(
                f"flow.createEntity response missing entityId: keys={list(result.keys())}"
            )
        return entity_id

    async def copy_project_media_to_character_slot(
        self,
        at: str,
        *,
        project_id: str,
        media_id: str,
        entity_id: str,
        image_reference_index: int,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "mediaId": media_id,
            "destinationProjectId": project_id,
            "destinationMediaContext": {
                "entityContext": {
                    "entityId": entity_id,
                    "characterSlot": {
                        "imageReferenceIndex": int(image_reference_index),
                    },
                }
            },
        }
        return await self._aisandbox_request(
            "POST",
            "/flow:copyProjectMedia",
            at=at,
            raw_body=self._compact_json_dumps(payload),
            account_id=account_id,
        )

    async def stream_flow_creation_agent(
        self,
        at: str,
        payload: Dict[str, Any],
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        action: str = "VIDEO_GENERATION",
        account_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        url = f"{self.api_base_url}/flowCreationAgent:streamChat?alt=sse"
        headers = self._build_browser_style_control_headers(
            referer="https://flow.google.com/",
            origin="https://flow.google.com",
            account_id=account_id,
            content_type="application/json",
            accept_language=self._get_primary_accept_language(),
        )
        headers["Accept"] = "text/event-stream, text/event-stream"

        if config.captcha_method == "browser" and project_id:
            from .browser_captcha import BrowserCaptchaService

            service = await BrowserCaptchaService.get_instance(self.db)
            (
                response_payload,
                _browser_ref,
                fingerprint,
            ) = await service.submit_flow_request(
                project_id=project_id,
                action=action,
                token_id=token_id,
                url=url,
                at_token=at,
                json_data=payload,
                timeout=self._get_video_submit_timeout(),
                browser_ref=self._current_browser_ref(token_id),
            )
            self._set_request_fingerprint(fingerprint if fingerprint else None)

            status_code = int(response_payload.get("status") or 0)
            raw_text = response_payload.get("text") or ""
            if status_code >= 400:
                error_reason = f"HTTP Error {status_code}"
                parsed_body = None
                try:
                    parsed_body = json.loads(raw_text) if raw_text else None
                except Exception:
                    parsed_body = None
                if isinstance(parsed_body, dict) and "error" in parsed_body:
                    error_info = parsed_body["error"] or {}
                    error_message = error_info.get("message", "")
                    details = error_info.get("details", [])
                    for detail in details or []:
                        if isinstance(detail, dict) and detail.get("reason"):
                            error_reason = detail.get("reason")
                            break
                    if error_message:
                        error_reason = f"{error_reason}: {error_message}"
                elif raw_text:
                    error_reason = f"HTTP Error {status_code}: {raw_text[:200]}"
                error = RuntimeError(error_reason)
                error.browser_ref = _browser_ref
                raise error
        else:
            raw_text = await self._make_text_request(
                method="POST",
                url=url,
                headers=headers,
                json_data=payload,
                use_at=True,
                at_token=at,
                timeout=self._get_video_submit_timeout(),
                apply_default_client_headers=False,
                impersonate=self._resolve_runtime_impersonate(),
            )
        return self._parse_sse_json_events(raw_text)

    async def generate_omni_reference_video(
        self,
        at: str,
        st: str,
        project_id: str,
        prompt: str,
        aspect_ratio: str,
        reference_media_ids: List[str],
        model_usage_key: str = "abra_r2v_10s",
        model_display_name: str = "Omni Flash",
        duration: int = 10,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not st:
            raise RuntimeError("Omni reference-image video requires an ST token, but the current account did not provide an ST")
        if not reference_media_ids:
            raise RuntimeError("Omni reference-image video requires at least 1 reference image")

        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        entity_handle = "entity-0"
        video_context_warmed = False
        account_id = at[:16] if at else None
        entity_id: Optional[str] = None
        agent_aspect_ratio = self._video_aspect_ratio_to_agent_aspect_ratio(
            aspect_ratio
        )
        agent_prompt = (
            f"{prompt}\n\nUse a {agent_aspect_ratio} aspect ratio."
            if prompt
            else f"Use a {agent_aspect_ratio} aspect ratio."
        )

        while retry_attempt < max_retries:
            launch_gate_acquired = False
            approve_browser_id = None
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            browser_id = None
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)

            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO OMNI-R2V] generation",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error

            try:
                if not entity_id:
                    entity_id = await self.create_flow_entity(
                        st=st, project_id=project_id
                    )
                    for index, media_id in enumerate(reference_media_ids):
                        await self.copy_project_media_to_character_slot(
                            at=at,
                            project_id=project_id,
                            media_id=media_id,
                            entity_id=entity_id,
                            image_reference_index=index,
                            account_id=account_id,
                        )
                    debug_logger.log_info(
                        f"[VIDEO OMNI-R2V] Created character entity and attached reference images: project_id={project_id}, entity_id={entity_id}, refs={len(reference_media_ids)}"
                    )

                if not video_context_warmed:
                    await self._warmup_flow_video_frontend_context(
                        at=at,
                        project_id=project_id,
                        token_id=token_id,
                        session_id=session_id,
                        user_paygate_tier=user_paygate_tier,
                        prompt=agent_prompt,
                        model_key=model_usage_key,
                        aspect_ratio=aspect_ratio,
                    )
                    video_context_warmed = True

                agent_session_id = await self.get_flow_creation_agent_session(
                    at=at,
                    project_id=project_id,
                    account_id=account_id,
                    allow_global_fallback=True,
                )
                if not agent_session_id:
                    raise RuntimeError(
                        f"Flow Creation Agent Session not found: project_id={project_id}"
                    )

                session_detail = await self.get_flow_creation_agent_session_detail(
                    at=at,
                    agent_session_id=agent_session_id,
                    account_id=account_id,
                )
                current_turn_count = self._extract_turn_count(session_detail)
                next_turn_number = current_turn_count + 1

                prompt_payload = {
                    "agentSessionId": agent_session_id,
                    "agentClientContext": {
                        "projectId": f"projects/{project_id}",
                        "clientSessionId": session_id,
                        "recaptchaContext": {
                            "token": recaptcha_token,
                            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                        },
                        "turnNumber": next_turn_number,
                    },
                    "userMessage": {
                        "userPrompt": {
                            "parts": [{"text": agent_prompt}],
                        },
                        "entityReferences": [
                            {
                                "entityId": entity_id,
                                "handle": entity_handle,
                            }
                        ],
                    },
                }

                prompt_events = await self.stream_flow_creation_agent(
                    at=at,
                    payload=prompt_payload,
                    project_id=project_id,
                    token_id=token_id,
                    account_id=account_id,
                )
                tool_result = self._extract_generate_video_with_references_result(
                    prompt_events
                )

                if not tool_result:
                    (
                        approve_recaptcha_token,
                        approve_browser_id,
                    ) = await self._get_recaptcha_token(
                        project_id,
                        action="VIDEO_GENERATION",
                        token_id=token_id,
                    )
                    if not approve_recaptcha_token:
                        raise RuntimeError(
                            "Failed to acquire the reCAPTCHA token during the Omni reference-image video approval phase"
                        )
                    approve_payload = {
                        "agentSessionId": agent_session_id,
                        "agentClientContext": {
                            "projectId": f"projects/{project_id}",
                            "clientSessionId": session_id,
                            "recaptchaContext": {
                                "token": approve_recaptcha_token,
                                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                            },
                            "turnNumber": next_turn_number + 1,
                        },
                        "userMessage": {
                            "userPrompt": {
                                "parts": [{"text": "Approve"}],
                            }
                        },
                    }
                    approve_events = await self.stream_flow_creation_agent(
                        at=at,
                        payload=approve_payload,
                        project_id=project_id,
                        token_id=token_id,
                        account_id=account_id,
                    )
                    tool_result = self._extract_generate_video_with_references_result(
                        approve_events
                    )
                    await self._notify_browser_captcha_request_finished(
                        approve_browser_id
                    )
                    approve_browser_id = None

                if not tool_result:
                    raise RuntimeError(
                        "The Omni reference-image video chain did not return a generate_video_with_references toolResult"
                    )

                media_id = str(tool_result.get("media_id") or "").strip()
                resolved_project_id = (
                    str(tool_result.get("project_id") or project_id).strip()
                    or project_id
                )
                if not media_id:
                    raise RuntimeError(
                        f"Omni reference-image video toolResult missing media_id: {tool_result}"
                    )

                operation = {
                    "operation": {"name": media_id},
                    "name": media_id,
                    "mediaName": media_id,
                    "projectId": resolved_project_id,
                    "workflowId": tool_result.get("workflow_id"),
                    "batchId": tool_result.get("batch_id"),
                    "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                }
                return {
                    "operations": [operation],
                    "agentToolResult": tool_result,
                    "projectId": resolved_project_id,
                    "entityId": entity_id,
                    "entityHandle": entity_handle,
                    "modelUsageKey": model_usage_key,
                    "aspectRatio": tool_result.get("aspect_ratio") or aspect_ratio,
                    "duration": duration,
                    "modelDisplayName": model_display_name,
                }
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO OMNI-R2V] generation",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
                if approve_browser_id:
                    await self._notify_browser_captcha_request_finished(
                        approve_browser_id
                    )

            retry_attempt += 1

        raise last_error

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Image-to-video; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            prompt: the prompt
            model_key: veo_3_1_r2v_fast_landscape
            aspect_ratio: video aspect ratio
            reference_images: list of reference images [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "..."}]
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoReferenceImages"

        # 403/reCAPTCHA retry logic - allows a higher retry limit when reCAPTCHA evaluation failed
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Re-acquire the reCAPTCHA token on every retry - videos use the VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] generation",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            request_seed = random.randint(1, 99999)
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "requests": [
                    {
                    "aspectRatio": aspect_ratio,
                    "seed": request_seed,
                        "textInput": self._build_video_text_input(
                            prompt, use_v2_model_config=True
                        ),
                    "videoModelKey": model_key,
                    "referenceImages": reference_images,
                        "metadata": {},
                    }
                ],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] generation",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Generate a video from the start/end frames; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            prompt: the prompt
            model_key: veo_3_1_i2v_s_fast_fl
            aspect_ratio: video aspect ratio
            start_media_id: start-frame mediaId
            end_media_id: end-frame mediaId
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartAndEndImage"

        # 403/reCAPTCHA retry logic - allows a higher retry limit when reCAPTCHA evaluation failed
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Re-acquire the reCAPTCHA token on every retry - videos use the VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] start/end frame generation",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(
                    prompt, use_v2_model_config=True
                ),
                "videoModelKey": model_key,
                "startImage": {"mediaId": start_media_id},
                "endImage": {"mediaId": end_media_id},
                "metadata": {},
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] start/end frame generation",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Generate a video from the first frame only; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            prompt: the prompt
            model_key: veo_3_1_i2v_s_fast_fl, etc.
            aspect_ratio: video aspect ratio
            start_media_id: start-frame mediaId
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartImage"

        # 403/reCAPTCHA retry logic - allows a higher retry limit when reCAPTCHA evaluation failed
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Re-acquire the reCAPTCHA token on every retry - videos use the VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] first frame generation",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier,
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(
                    prompt, use_v2_model_config=True
                ),
                "videoModelKey": model_key,
                "startImage": {"mediaId": start_media_id},
                # Note: no endImage field; only the first frame is used
                "metadata": {},
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] first frame generation",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    # ========== Video extension (Video Extend) ==========

    async def generate_video_extend(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        video_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Video extension; extends an already-generated video by 7 seconds

        Args:
            at: Access Token
            project_id: project ID
            prompt: the extension prompt
            model_key: veo_3_1_extend_portrait / veo_3_1_extend, etc.
            aspect_ratio: video aspect ratio
            video_media_id: the source video's mediaGenerationId
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text (operations list)
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoExtendVideo"

        # 403/reCAPTCHA retry logic - allows a higher retry limit when reCAPTCHA evaluation failed
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        workflow_id = str(uuid.uuid4())
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] extension",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True

            request_seed = random.randint(1, 99999)
            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "requests": [
                    {
                    "aspectRatio": aspect_ratio,
                    "seed": request_seed,
                    "textInput": {
                            "structuredPrompt": {"parts": [{"text": prompt}]}
                    },
                        "videoInput": {"mediaId": video_media_id},
                    "videoModelKey": model_key,
                        "metadata": {"workflowId": workflow_id},
                    }
                ],
                "useV2ModelConfig": True,
            }

            # Debug: print the request body for debugging
            import json as _json

            debug_logger.log_info(f"[VIDEO EXTEND] Request URL: {url}")
            debug_logger.log_info(
                f"[VIDEO EXTEND] Request JSON: {_json.dumps(json_data, indent=2, ensure_ascii=False)[:2000]}"
            )

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] extension",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1

        # All retries failed
        raise last_error

    # ========== Video concatenation (Video Concatenation) ==========

    async def run_concatenation(
        self,
        at: str,
        original_media_id: str,
        extend_media_id: str,
    ) -> dict:
        """
        Call the Google runVideoFxConcatenation API to concatenate videos
        
        Args:
            at: authentication token
            original_media_id: the original video's mediaGenerationId (UUID)
            extend_media_id: the extension video's mediaGenerationId (UUID)
        
        Returns:
            a dict containing the operation name
        """
        url = f"{self.api_base_url}:runVideoFxConcatenation"
        
        json_data = {
            "inputVideos": [
                {
                    "mediaGenerationId": original_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "0s",
                    "endTimeOffset": "8s",
                },
                {
                    "mediaGenerationId": extend_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "1s",
                    "endTimeOffset": "8s",
                },
            ]
        }
        
        debug_logger.log_info(
            f"[CONCAT] Submitting concatenation task: original={original_media_id[:12]}..., extend={extend_media_id[:12]}..."
        )
        
        result = await self._make_request(
            method="POST", url=url, json_data=json_data, use_at=True, at_token=at
        )
        debug_logger.log_info(
            f"[CONCAT] Concatenation task submitted: {json.dumps(result, ensure_ascii=False)[:300]}"
        )
        return result

    async def poll_concatenation_status(
        self,
        at: str,
        operation_name: str,
        timeout: int = 300,
        poll_interval: int = 3,
    ) -> dict:
        """
        Poll the concatenation task status until it completes or times out
        
        Args:
            at: authentication token
            operation_name: the concatenation task's operation name
            timeout: timeout in seconds
            poll_interval: polling interval in seconds
        
        Returns:
            a dict containing outputUri and mediaGenerationId
        """
        url = f"{self.api_base_url}:runVideoFxCheckConcatenationStatus"
        json_data = {"operation": {"operation": {"name": operation_name}}}
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            result = await self._make_request(
                method="POST",
                url=url,
                json_data=json_data,
                use_at=True,
                at_token=at,
                timeout=300,  # concat API returns base64 video (~14MB), needs longer timeout
            )
            
            status = result.get("status", "")
            output_uri = result.get("outputUri", "")
            encoded_video = result.get("encodedVideo", "")
            
            ev_len = len(encoded_video) if encoded_video else 0
            elapsed = int(time.time() - start_time)
            all_keys = list(result.keys())
            debug_logger.log_info(
                f"[CONCAT] Status: {status}, outputUri={'yes' if output_uri else 'no'}, "
                f"encodedVideo={ev_len} chars, elapsed={elapsed}s, keys={all_keys}"
            )
            
            # Check outputUri first
            if output_uri:
                debug_logger.log_info(
                    f"[CONCAT] Concatenation complete (outputUri): {output_uri[:120]}"
                )
                return result
            
            # The Google API returns encodedVideo (base64-encoded MP4) instead of outputUri
            if encoded_video and "SUCCESSFUL" in status:
                try:
                    import os

                    video_bytes = base64.b64decode(encoded_video)
                    video_filename = f"concat_{uuid.uuid4().hex[:12]}.mp4"
                    
                    # Save to the tmp/ directory (FastAPI mounts it as the /tmp static files)
                    save_dir = "tmp"
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, video_filename)
                    
                    with open(save_path, "wb") as f:
                        f.write(video_bytes)
                    
                    # Build the URL: FastAPI mounts /tmp -> /app/tmp/
                    serve_url = f"/tmp/{video_filename}"
                    debug_logger.log_info(
                        f"[CONCAT] Concatenation complete (encodedVideo): saved {len(video_bytes)} bytes -> {serve_url}"
                    )
                    
                    result["outputUri"] = serve_url
                    result["local_file"] = save_path
                    return result
                except Exception as e:
                    debug_logger.log_error(f"[CONCAT] Failed to decode encodedVideo: {e}")
                    raise Exception(f"Failed to decode the concatenated video: {e}")
            
            # SUCCESSFUL but neither outputUri nor encodedVideo
            if "SUCCESSFUL" in status:
                debug_logger.log_warning(
                    f"[CONCAT] SUCCESSFUL but no outputUri/encodedVideo: {json.dumps(result, ensure_ascii=False)[:300]}"
                )

            if "FAILED" in status or "ERROR" in status:
                debug_logger.log_error(
                    f"[CONCAT] Failed: {status}, response: {json.dumps(result, ensure_ascii=False)[:300]}"
                )
                raise Exception(f"Video concatenation failed: {status}")
            
            await asyncio.sleep(poll_interval)
        
        debug_logger.log_error(f"[CONCAT] Timed out ({timeout}s); giving up on concatenation")
        raise Exception(f"Video concatenation timed out ({timeout}s)")

    # ========== Video upscaling (Video Upsampler) ==========

    async def upsample_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Upscale the video to 4K/1080P; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            video_media_id: the video's mediaId
            aspect_ratio: video aspect ratio VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
            resolution: VIDEO_RESOLUTION_4K or VIDEO_RESOLUTION_1080P
            model_key: veo_3_1_upsampler_4k or veo_3_1_upsampler_1080p

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoUpsampleVideo"

        # 403/reCAPTCHA retry logic - allows a higher retry limit when reCAPTCHA evaluation failed
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        scene_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id, action="VIDEO_GENERATION", token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] upscale",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt="",
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True

            request_seed = random.randint(1, 99999)
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(
                    batch_id
                ),
                "requests": [
                    {
                    "aspectRatio": aspect_ratio,
                    "resolution": resolution,
                    "seed": request_seed,
                        "videoInput": {"mediaId": video_media_id},
                    "videoModelKey": model_key,
                        "metadata": {"sceneId": scene_id},
                    }
                ],
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(
                    result, fallback_project_id=project_id
                )
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] upscale",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        raise last_error

    # ========== Task polling (uses AT) ==========

    async def check_video_status(
        self,
        at: str,
        operations: List[Dict],
        token_id: Optional[int] = None,
        google_cookies: Optional[str] = None,
    ) -> dict:
        """Query the video generation status

        Args:
            at: Access Token
            operations: internal operations list; the current upstream status query uses only the media references extracted from it

        Returns:
            {
                "operations": [{
                    "operation": {
                        "name": "task_id",
                        "metadata": {...}  # contains video info when complete
                    },
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                }]
            }
        """
        use_frontend_rpc = any(
            isinstance(operation, dict)
            and operation.get("frontendRpc") in {"YhhmEf", "jwpduf"}
            for operation in operations or []
        )
        if use_frontend_rpc:
            try:
                operation_ids: List[str] = []
                for operation in operations or []:
                    operation_id = str(
                        (operation or {}).get("name")
                        or ((operation or {}).get("operation") or {}).get("name")
                        or ""
                    ).strip()
                    if operation_id and operation_id not in operation_ids:
                        operation_ids.append(operation_id)
                if not operation_ids:
                    raise ValueError("Video status query is missing the Flow frontend operation ID")
                cookie_storage = await self._resolve_flow_frontend_cookie_storage(
                    google_cookies=google_cookies,
                    token_id=token_id,
                )
                first_operation = operations[0] if operations else {}
                payload = await self._call_flow_frontend_rpc(
                    rpc_id="jwpduf",
                    argument=[
                        None,
                        None,
                        [[operation_id] for operation_id in operation_ids],
                    ],
                    cookie_header="",
                    timeout=max(self._get_video_poll_timeout(), 30),
                    project_id=(first_operation or {}).get("projectId"),
                    cookie_storage=cookie_storage,
                )
                return self._normalize_frontend_video_status(payload, operations)
            except Exception as frontend_error:
                debug_logger.log_warning(
                    "[VIDEO POLL] Flow frontend RPC failed; falling back to the "
                    f"legacy status endpoint: {frontend_error}"
                )

        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        media_refs = self._operations_to_media_refs(operations)
        if not media_refs:
            raise ValueError("Video status query is missing the media reference; cannot issue a query per the current upstream structure")

        json_data = {"media": media_refs}
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_poll_timeout(),
                )
                try:
                    media_preview = (
                        result.get("media") if isinstance(result, dict) else None
                    )
                    operations_preview = (
                        result.get("operations") if isinstance(result, dict) else None
                    )
                    preview_payload = {
                        "top_keys": list(result.keys())
                        if isinstance(result, dict)
                        else [],
                        "media_count": len(media_preview)
                        if isinstance(media_preview, list)
                        else 0,
                        "operations_count": len(operations_preview)
                        if isinstance(operations_preview, list)
                        else 0,
                        "first_media": media_preview[0]
                        if isinstance(media_preview, list) and media_preview
                        else None,
                        "first_operation": operations_preview[0]
                        if isinstance(operations_preview, list) and operations_preview
                        else None,
                    }
                    print(
                        "[VIDEO POLL RAW] "
                        + json.dumps(
                            self._truncate_large_debug_value(preview_payload),
                            ensure_ascii=False,
                        )[:4000]
                    )
                except Exception:
                    pass
                return self._normalize_video_generation_response(result)
            except Exception as e:
                last_error = e
                retry_reason = self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[VIDEO POLL] Status query hit {retry_reason}, preparing to retry ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Video status query failed")

    # ========== Media deletion (uses ST) ==========

    async def delete_media(self, st: str, media_names: List[str]):
        """Delete media

        Args:
            st: Session Token
            media_names: list of media IDs
        """
        url = f"{self.labs_base_url}/trpc/media.deleteMedia"
        json_data = {"json": {"names": media_names}}

        await self._make_request(
            method="POST", url=url, json_data=json_data, use_st=True, st_token=st
        )

    async def get_media_url_redirect(
        self,
        st: str,
        media_name: str,
        media_url_type: str = "MEDIA_URL_TYPE_FULL_MEDIA",
    ) -> Optional[str]:
        """Fetch the actual media access URL via trpc media.getMediaUrlRedirect."""
        normalized_media_name = str(media_name or "").strip()
        if not normalized_media_name:
            return None

        url = (
            f"{self.labs_base_url}/trpc/media.getMediaUrlRedirect"
            f"?name={quote(normalized_media_name, safe='')}"
            f"&mediaUrlType={quote(str(media_url_type or 'MEDIA_URL_TYPE_FULL_MEDIA'), safe='')}"
        )

        proxy_url = None
        if self.proxy_manager:
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                proxy_url = await self.proxy_manager.get_request_proxy_url()
            else:
                proxy_url = await self.proxy_manager.get_proxy_url()

        headers = {
            "Cookie": f"__Secure-next-auth.session-token={st}",
            "User-Agent": self._generate_user_agent(st[:16] if st else None),
            "Accept": "*/*",
        }
        for key, value in self._default_client_headers.items():
            headers.setdefault(key, value)

        request_timeout = self._get_control_plane_timeout()
        start_time = time.time()
        try:
            async with AsyncSession(trust_env=False) as session:
                response = await session.get(
                    url,
                    headers=headers,
                    proxy=proxy_url,
                    timeout=request_timeout,
                    impersonate="chrome",
                    allow_redirects=False,
                )

            duration_ms = (time.time() - start_time) * 1000
            if config.debug_enabled:
                debug_logger.log_response(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=response.text,
                    duration_ms=duration_ms,
                )

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location") or response.headers.get(
                    "location"
                )
                if location:
                    return urljoin(url, location)

            final_url = str(getattr(response, "url", "") or "").strip()
            if response.status_code == 200 and final_url and final_url != url:
                return final_url

            raise RuntimeError(
                f"media.getMediaUrlRedirect returned unexpected status={response.status_code} "
                f"for media={normalized_media_name}"
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to get media redirect URL: media={normalized_media_name}, type={media_url_type}, error={e}"
            ) from e

    # ========== Helper methods ==========

    async def _handle_retryable_generation_error(
        self,
        error: Exception,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
        defer_browser_error_notification: bool = False,
    ) -> bool:
        """Unified handling of retry decisions and captcha self-healing notifications for the generation chain."""
        error_str = str(error)
        retry_reason = self._get_retry_reason(error_str)
        retry_delay = self._get_retry_delay_seconds(error_str, retry_attempt)

        effective_max_retries = self._resolve_generation_retry_budget(
            max_retries, error_str
        )
        is_terminal_attempt = retry_attempt >= effective_max_retries - 1
        should_notify_browser = (
            not defer_browser_error_notification
            or not retry_reason
            or is_terminal_attempt
            or (config.captcha_method == "browser" and "recaptcha" in error_str.lower())
        )

        if should_notify_browser:
            notify_reason = retry_reason or error_str[:120] or type(error).__name__
            await self._notify_browser_captcha_error(
                browser_id=getattr(error, "browser_ref", None) or browser_id,
                project_id=project_id,
                error_reason=notify_reason,
                error_message=error_str,
            )

        if not retry_reason or getattr(error, "submission_uncertain", False):
            return False

        if is_terminal_attempt:
            debug_logger.log_warning(
                f"{log_prefix}hit {retry_reason}; reached the maximum retry count ({effective_max_retries}); this request failed and the close/recycle step was executed."
            )
            return False

        debug_logger.log_warning(
            f"{log_prefix}hit {retry_reason}; will re-acquire the captcha and retry after {retry_delay} seconds ({retry_attempt + 2}/{effective_max_retries})..."
        )
        await asyncio.sleep(retry_delay)
        return True

    async def _handle_missing_recaptcha_token(
        self,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        token_error = Exception("Failed to obtain reCAPTCHA token")
        return await self._handle_retryable_generation_error(
            error=token_error,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            browser_id=browser_id,
            project_id=project_id,
            log_prefix=log_prefix,
        )

    async def _build_team_captcha_cookie_map(
        self, token_id: Optional[int]
    ) -> Dict[str, str]:
        """Cookie key-value pairs issued by the team captcha service (selected per the flow.google.com request convention)."""
        try:
            cookie_storage = await self._resolve_flow_frontend_cookie_storage(
                token_id=token_id
            )
        except Exception as e:
            debug_logger.log_warning(
                f"[reCAPTCHA {FLOW2API_TEAM_CAPTCHA_METHOD}] Failed to read account Cookie: {e}"
            )
            return {}
        if not str(cookie_storage or "").strip():
            return {}
        from .browser_cookie_utils import cookie_pairs_for_url

        cookie_map: Dict[str, str] = {}
        for name, value in cookie_pairs_for_url(cookie_storage):
            if name:
                cookie_map[name] = value
        return cookie_map

    def _resolve_third_party_captcha_method(self) -> Optional[str]:
        """Return the first configured third-party captcha method with a key (yescaptcha takes priority).

        batchexecute RPCs such as SPrCad only accept third-party captcha tokens;
        tokens harvested by browser/personal are rejected with UNUSUAL_ACTIVITY.
        """
        if config.captcha_method == FLOW2API_TEAM_CAPTCHA_METHOD:
            # The built-in team captcha is always available (address/key hardcoded) and is preferred as the third-party token source.
            return FLOW2API_TEAM_CAPTCHA_METHOD
        for method, attr in (
            ("yescaptcha", "yescaptcha_api_key"),
            ("capmonster", "capmonster_api_key"),
            ("ezcaptcha", "ezcaptcha_api_key"),
            ("capsolver", "capsolver_api_key"),
            ("captcharun", "captcharun_api_key"),
            ("twocaptcha", "twocaptcha_api_key"),
        ):
            if str(getattr(config, attr, "") or "").strip():
                return method
        return None

    @staticmethod
    def _extract_browser_captcha_session_id(
        browser_ref: Optional[Union[int, str]],
    ) -> Optional[str]:
        """Extract the Flow session id corresponding to this harvest from the browser request handle."""
        if not isinstance(browser_ref, str):
            return None
        raw = browser_ref.strip()
        if raw.startswith("personal:"):
            return raw.partition("personal:")[2] or None
        browser_id, separator, request_ref = raw.partition(":")
        if not separator or not browser_id.isdigit() or not request_ref:
            return None
        if request_ref.startswith("lease-"):
            return request_ref.partition("/")[2] or None
        return request_ref

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """Decide whether a retry is needed and return the log hint text"""
        error_lower = error_str.lower()
        if "browser captcha queue wait timeout" in error_lower:
            return None
        if "error_no_slot_available_block" in error_lower:
            return "打码服务资源阻塞"
        if "error_no_slot_available" in error_lower:
            return "打码服务资源不足"
        if "model_access_denied" in error_lower:
            # The account lacks permission for the corresponding model; retrying will not change the result
            return None
        if "403" in error_lower:
            return "403 error"
        if "429" in error_lower or "too many requests" in error_lower:
            return "429 rate limited"
        if self._is_retryable_network_error(error_str):
            return "network/TLS error"
        if "recaptcha evaluation failed" in error_lower:
            return "reCAPTCHA 验证失败"
        if "recaptcha" in error_lower:
            return "reCAPTCHA 错误"
        if any(
            keyword in error_lower
            for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
                '"reason":"internal"',
            "server error",
            "upstream error",
            ]
        ):
            return "500/internal error"
        return None

    def _get_retry_delay_seconds(self, error_str: str, retry_attempt: int) -> int:
        error_lower = str(error_str or "").lower()
        if "error_no_slot_available_block" in error_lower:
            return 20
        if "error_no_slot_available" in error_lower:
            index = max(
                0, min(retry_attempt, len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS) - 1)
            )
            return self.YESCAPTCHA_SLOT_BACKOFF_SECONDS[index]
        if "recaptcha evaluation failed" in error_lower:
            return 2
        if "recaptcha" in error_lower:
            return 2
        return 1

    def _resolve_recaptcha_runtime_settings(
        self,
        method: str,
        action: str,
    ) -> Dict[str, Any]:
        normalized_action = (
            str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        )
        website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

        if method == "flow2api_team":
            page_action = normalized_action
            task_type = FLOW2API_TEAM_CAPTCHA_TASK_TYPE
            min_score = get_yescaptcha_min_score(task_type)
        elif method == "yescaptcha":
            page_action = normalized_action
            task_type = config.yescaptcha_task_type
            min_score = get_yescaptcha_min_score(task_type)
        elif method == "capmonster":
            page_action = normalized_action
            task_type = "RecaptchaV3TaskProxyless"
            min_score = None
        elif method == "ezcaptcha":
            page_action = normalized_action
            task_type = "ReCaptchaV3TaskProxylessS9"
            min_score = None
        elif method == "capsolver":
            page_action = normalized_action
            task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
            min_score = None
        elif method == "captcharun":
            page_action = normalized_action
            task_type = "ReCaptchaV3"
            min_score = None
        elif method == "twocaptcha":
            page_action = normalized_action
            task_type = "RecaptchaV3TaskProxyless"
            min_score = 0.9
        else:
            page_action = normalized_action
            task_type = None
            min_score = None

        return {
            "website_key": website_key,
            "page_action": page_action,
            "task_type": task_type,
            "min_score": min_score,
        }

    def _merge_request_fingerprint(self, patch: Optional[Dict[str, Any]]):
        if not isinstance(patch, dict) or not patch:
            return
        current = self.get_request_fingerprint() or {}
        merged = dict(current)
        for key, value in patch.items():
            if value is None:
                continue
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    continue
                merged[key] = normalized
            else:
                merged[key] = value
        self._set_request_fingerprint(merged if merged else None)

    async def _notify_browser_captcha_error(
        self,
        browser_id: Optional[Union[int, str]] = None,
        project_id: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """Notify the browser captcha service of an execution failure for self-healing.
        
        Args:
            browser_id: the browser ID used in browser mode
            project_id: the project_id used in personal mode
            error_reason: the categorized error reason
            error_message: the raw error text
        """
        context = self._get_captcha_request_context()
        if context.get("browser_ref") != browser_id or context.get("project_id") != project_id:
            context = {}
        method = context.get("method") or config.captcha_method
        if method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_error(
                    browser_id,
                    error_reason=error_message or error_reason or "upstream_error",
                )
            except Exception:
                pass
        elif method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService

                service = await ExtensionCaptchaService.get_instance()
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif method == "personal" and project_id:
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                    token_id=context.get("token_id"),
                    slot_id=context.get("slot_id"),
                    issued_at=context.get("issued_at"),
                )
            except Exception:
                pass
        elif method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/error",
                    json_data={
                        "error_reason": error_reason
                        or error_message
                        or "upstream_error"
                    },
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[reCAPTCHA RemoteBrowser] Failed to report error: {e}"
                )

    async def _notify_browser_captcha_request_finished(
        self, browser_id: Optional[Union[int, str]] = None, *, success: Optional[bool] = None,
    ):
        """Notify the headed browser that the upstream image/video request has finished and the corresponding captcha browser can be closed."""
        context = self._get_captcha_request_context()
        method = context.get("method") if context.get("browser_ref") == browser_id else None
        method = method or config.captcha_method
        if method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                if success is None:
                    await service.report_request_finished(browser_id)
                else:
                    await service.report_request_finished(browser_id, success=success)
            except Exception:
                pass
        elif method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/finish",
                    json_data={"status": "success"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[reCAPTCHA RemoteBrowser] Failed to report finish: {e}"
                )
        context = self._get_captcha_request_context()
        if context.get("browser_ref") == browser_id:
            self._set_captcha_request_context(None)

    def _generate_session_id(self) -> str:
        """Generate sessionId: ;timestamp"""
        return f";{int(time.time() * 1000)}"

    def _generate_scene_id(self) -> str:
        """Generate sceneId: UUID"""
        return str(uuid.uuid4())

    def _get_remote_browser_service_config(self) -> tuple[str, str, int]:
        base_url = (config.remote_browser_base_url or "").strip().rstrip("/")
        api_key = (config.remote_browser_api_key or "").strip()
        timeout = max(5, int(config.remote_browser_timeout or 60))

        if not base_url:
            raise RuntimeError("remote_browser service address is not configured")
        if not api_key:
            raise RuntimeError("remote_browser API Key is not configured")

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError("remote_browser service address format is invalid")

        return base_url, api_key, timeout

    @staticmethod
    def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
        read_value = max(3.0, float(read_timeout))
        write_value = min(10.0, max(3.0, read_value))
        if httpx is None:
            return read_value
        return httpx.Timeout(
            connect=2.5,
            read=read_value,
            write=write_value,
            pool=2.5,
        )

    @staticmethod
    def _parse_json_response_text(text: str) -> Optional[Any]:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    async def _stdlib_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_data: Optional[bytes] = None

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_data = json.dumps(payload).encode("utf-8")

        def do_request() -> tuple[int, str]:
            request = urllib.request.Request(
                url=url,
                data=request_data,
                headers=req_headers,
                method=request_method,
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                    status_code = int(
                        getattr(response, "status", 0) or response.getcode() or 0
                    )
                    body = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return status_code, body.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                body = exc.read()
                charset = exc.headers.get_content_charset() if exc.headers else None
                return int(getattr(exc, "code", 0) or 0), body.decode(
                    charset or "utf-8", errors="replace"
                )

        try:
            status_code, text = await asyncio.to_thread(do_request)
        except Exception as e:
            raise RuntimeError(f"remote_browser request failed: {e}") from e

        return status_code, FlowClient._parse_json_response_text(text), text

    @staticmethod
    async def _sync_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_kwargs: Dict[str, Any] = {
            "headers": req_headers,
            "timeout": FlowClient._build_remote_browser_http_timeout(timeout),
        }

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_kwargs["json"] = payload

        if httpx is None:
            return await FlowClient._stdlib_json_http_request(
                method=method,
                url=url,
                headers=req_headers,
                payload=payload,
                timeout=timeout,
            )

        try:
            # The remote_browser control plane only needs stable JSON transport and does not need browser fingerprint spoofing.
            # Using httpx avoids curl_cffi swallowing the POST body in the current environment.
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False
            ) as session:
                response = await session.request(
                    method=request_method,
                    url=url,
                    **request_kwargs,
                )
        except Exception as e:
            raise RuntimeError(f"remote_browser request failed: {e}") from e

        status_code = int(getattr(response, "status_code", 0) or 0)
        text = response.text or ""
        parsed = FlowClient._parse_json_response_text(text)

        return status_code, parsed, text

    async def _call_remote_browser_service(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        timeout_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        base_url, api_key, timeout = self._get_remote_browser_service_config()
        url = f"{base_url}{path}"
        effective_timeout = max(5, int(timeout_override or timeout))

        status_code, payload, response_text = await self._sync_json_http_request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=json_data,
            timeout=effective_timeout,
        )

        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or str(payload)
            if not detail:
                detail = (response_text or "").strip() or f"HTTP {status_code}"
            raise RuntimeError(f"remote_browser request failed: {detail}")

        if not isinstance(payload, dict):
            raise RuntimeError("remote_browser returned an invalid format")

        return payload

    async def prefill_remote_browser_pool(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        cooldown_seconds: float = 8.0,
    ) -> bool:
        """Let the local remote_browser service start replenishing the pool early, moving the token-acquisition wait as early as possible."""
        if config.captcha_method != "remote_browser":
            return False

        normalized_project = str(project_id or "").strip()
        normalized_action = (
            str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        )
        if not normalized_project:
            return False

        cache_key = f"{normalized_project}|{normalized_action}|{int(token_id or 0)}"
        now_value = time.monotonic()
        last_sent = float(
            self._remote_browser_prefill_last_sent.get(cache_key, 0.0) or 0.0
        )
        if (now_value - last_sent) < max(0.5, float(cooldown_seconds)):
            return False

        try:
            await self._call_remote_browser_service(
                method="POST",
                path="/api/v1/prefill",
                json_data={
                    "project_id": normalized_project,
                    "action": normalized_action,
                    "token_id": token_id,
                },
                timeout_override=3,
            )
            self._remote_browser_prefill_last_sent[cache_key] = now_value
            return True
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] prefill failed: {e}")
            return False

    async def prefill_remote_browser_for_tokens(
        self, tokens: List[Any], action: str = "IMAGE_GENERATION"
    ) -> int:
        if config.captcha_method != "remote_browser":
            return 0

        unique_projects: List[str] = []
        seen_projects = set()
        for token in tokens or []:
            project_id = str(getattr(token, "current_project_id", "") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            unique_projects.append(project_id)

        warmed = 0
        for project_id in unique_projects:
            if await self.prefill_remote_browser_pool(project_id, action=action):
                warmed += 1
        return warmed

    def _resolve_remote_browser_solve_timeout(self, action: str) -> int:
        base_timeout = max(5, int(config.remote_browser_timeout or 60))
        action_name = str(action or "").strip().upper()

        # This only fetches the reCAPTCHA token and should not share the hundreds-of-seconds timeout of the whole generation chain.
        target_timeout = 45 if action_name == "VIDEO_GENERATION" else 35
        return max(12, min(base_timeout, target_timeout))

    async def _get_recaptcha_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        website_url: Optional[str] = None,
        method_override: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[Union[int, str]]]:
        """Acquire the reCAPTCHA token - supports multiple captcha methods
        
        Args:
            project_id: project ID
            action: reCAPTCHA action type
                - IMAGE_GENERATION: image generation and 2K/4K image upscaling (default)
                - VIDEO_GENERATION: video generation and video upscaling
            token_id: the current business token id (used in browser mode to read the token-level captcha proxy)
            method_override: force a specific captcha method (such as "yescaptcha").
                Used when a harvested token is not accepted by the target RPC (such as SPrCad upscaling).
        
        Returns:
            (token, browser_id) tuple.
            - browser mode: browser_id is the local browser ID
            - remote_browser mode: browser_id is the remote session_id
            - other modes: browser_id is None
        """
        captcha_method = method_override or config.captcha_method
        previous = self._get_captcha_request_context()
        if previous.get("method") == "browser" and previous.get("owner") is asyncio.current_task():
            await self._notify_browser_captcha_request_finished(previous.get("browser_ref"))
        self._set_captcha_request_context({
            "method": captcha_method, "token_id": token_id, "project_id": project_id,
            "owner": asyncio.current_task(),
        })
        debug_logger.log_info(
            f"[reCAPTCHA] Start acquiring token: method={captcha_method}, project_id={project_id}, action={action}"
        )

        if captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService

                service = await ExtensionCaptchaService.get_instance(self.db)
                extension_timeout = 45 if action == "VIDEO_GENERATION" else 25
                token = await service.get_token(
                    project_id, action, timeout=extension_timeout, token_id=token_id
                )
                self._set_request_fingerprint(None)
                return token, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Extension] error: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None

        # Built-in browser captcha (nodriver)
        if captcha_method == "personal":
            debug_logger.log_info(f"[reCAPTCHA] using personal mode")
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                debug_logger.log_info(f"[reCAPTCHA] Imported BrowserCaptchaService successfully")
                service = await BrowserCaptchaService.get_instance(self.db)
                debug_logger.log_info(
                    f"[reCAPTCHA] Got the service instance successfully; about to call get_token"
                )
                solve_bundle = None
                get_token_bundle = getattr(service, "get_token_bundle", None)
                if callable(get_token_bundle):
                    solve_bundle = await get_token_bundle(
                        project_id,
                        action,
                        token_id=token_id,
                    )
                    token = str((solve_bundle or {}).get("token") or "").strip() or None
                else:
                    get_token_with_metadata = getattr(
                        service, "get_token_with_metadata", None
                    )
                    if callable(get_token_with_metadata):
                        (
                            token,
                            _slot_id,
                            _cookie_source_token_id,
                        ) = await get_token_with_metadata(
                            project_id,
                            action,
                            token_id=token_id,
                        )
                    else:
                        token = await service.get_token(
                            project_id, action, token_id=token_id
                        )
                    solve_bundle = (
                        {
                        "token": token,
                            "fingerprint": service.get_last_fingerprint()
                            if token
                            else None,
                        }
                        if token
                        else None
                    )
                debug_logger.log_info(f"[reCAPTCHA] Captcha solving done: success={bool(token)}")
                fingerprint = (
                    solve_bundle.get("fingerprint")
                    if isinstance(solve_bundle, dict)
                    and isinstance(solve_bundle.get("fingerprint"), dict)
                    else None
                )
                harvest_session_id = (
                    str((solve_bundle or {}).get("session_id") or "").strip() or None
                )
                if isinstance(solve_bundle, dict) and token:
                    session_cookies = solve_bundle.get("session_cookies")
                    proxy_url = str(solve_bundle.get("proxy_url") or "").strip()
                    next_fingerprint = dict(fingerprint or {})
                    if isinstance(session_cookies, dict) and session_cookies:
                        next_fingerprint["session_cookies"] = dict(session_cookies)
                    if (
                        proxy_url
                        and not str(next_fingerprint.get("proxy_url") or "").strip()
                    ):
                        next_fingerprint["proxy_url"] = proxy_url
                    next_fingerprint["project_id"] = project_id
                    next_fingerprint.setdefault("origin", "https://flow.google.com")
                    next_fingerprint.setdefault(
                        "referer", self._build_flow_project_page_url(project_id)
                    )
                    fingerprint = next_fingerprint or None
                if token:
                    effective_ua = str(
                        (fingerprint or {}).get("user_agent") or ""
                    ).strip()
                    effective_lang = str(
                        (fingerprint or {}).get("accept_language") or ""
                    ).strip()
                    if not effective_ua:
                        debug_logger.log_warning(
                            "[reCAPTCHA Personal] Got the token, but no browser fingerprint UA was extracted;"
                            "To avoid a mismatch between the protocol submission and the captcha environment, discard this token and trigger a retry"
                        )
                        self._set_request_fingerprint(None)
                        return None, None
                    debug_logger.log_info(
                        "[reCAPTCHA Personal] Using browser fingerprint: "
                        f"UA={effective_ua[:120]}, Accept-Language={effective_lang or '<empty>'}"
                    )
                self._set_request_fingerprint(fingerprint if token else None)
                browser_ref = (
                    f"personal:{harvest_session_id}"
                    if token and harvest_session_id
                    else None
                )
                if token:
                    self._set_captcha_request_context({
                        "method": "personal", "browser_ref": browser_ref,
                        "token_id": token_id, "project_id": project_id,
                        "slot_id": (solve_bundle or {}).get("slot_id"),
                        "issued_at": (solve_bundle or {}).get("issued_at"),
                        "owner": asyncio.current_task(),
                    })
                return token, browser_ref
            except RuntimeError as e:
                # Catch explicit errors from the Docker environment or missing dependencies
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Personal] {error_msg}")
                print(f"[reCAPTCHA] ❌ Built-in browser captcha failed: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] import failed: {str(e)}")
                print(f"[reCAPTCHA] ❌ nodriver is not installed; please run: pip install nodriver")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] error: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # Headed browser captcha (playwright)
        elif captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                token, browser_id, fingerprint = await service.get_token(
                    project_id, action, token_id=token_id
                )
                if token and action == "CHAT_GENERATION" and not self._extract_browser_captcha_session_id(
                    browser_id
                ):
                    debug_logger.log_warning(
                        "[reCAPTCHA Browser] harvest did not return a session id; the generation request may be rejected"
                    )
                self._set_request_fingerprint(fingerprint if token else None)
                if token:
                    self._set_captcha_request_context({
                        "method": "browser", "browser_ref": browser_id,
                        "token_id": token_id, "project_id": project_id,
                        "owner": asyncio.current_task(),
                    })
                return token, browser_id
            except RuntimeError as e:
                from .browser_captcha import BrowserCaptchaQueueTimeout
                if isinstance(e, BrowserCaptchaQueueTimeout):
                    raise
                # Catch explicit errors from the Docker environment or missing dependencies
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Browser] {error_msg}")
                print(f"[reCAPTCHA] ❌ Headed browser captcha failed: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] import failed: {str(e)}")
                print(
                    f"[reCAPTCHA] ❌ playwright is not installed; please run: pip install playwright && python -m playwright install chromium"
                )
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] error: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        elif captcha_method == "remote_browser":
            try:
                solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                payload = await self._call_remote_browser_service(
                    method="POST",
                    path="/api/v1/solve",
                    json_data={
                        "project_id": project_id,
                        "action": action,
                        "token_id": token_id,
                    },
                    timeout_override=solve_timeout,
                )
                token = payload.get("token")
                session_id = payload.get("session_id")
                fingerprint = (
                    payload.get("fingerprint")
                    if isinstance(payload.get("fingerprint"), dict)
                    else None
                )
                self._set_request_fingerprint(fingerprint if token else None)
                if not token or not session_id:
                    raise RuntimeError(
                        f"remote_browser response is missing token/session_id: {payload}"
                    )
                return token, str(session_id)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA RemoteBrowser] error: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # API captcha service
        elif captcha_method in [
            "yescaptcha",
            "flow2api_team",
            "captcharun",
            "twocaptcha",
            "capmonster",
            "ezcaptcha",
            "capsolver",
        ]:
            existing_fingerprint = self.get_request_fingerprint() or {}
            proxy_url = str(existing_fingerprint.get("proxy_url") or "").strip()
            if not proxy_url and self.proxy_manager:
                try:
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                except Exception as e:
                    debug_logger.log_warning(
                        f"[reCAPTCHA] Failed to get proxy for API captcha: {e}"
                    )

            api_captcha_ua = str(
                existing_fingerprint.get("user_agent") or ""
            ).strip() or self._generate_user_agent(
                str(token_id or project_id or captcha_method)
            )
            next_fingerprint = dict(existing_fingerprint)
            next_fingerprint.update(
                self._build_fingerprint_from_user_agent(
                    api_captcha_ua,
                    accept_language=self._get_primary_accept_language(),
                    proxy_url=proxy_url,
                )
            )
            self._set_request_fingerprint(next_fingerprint)
            captcha_website_url = str(
                website_url or ""
            ).strip() or self._build_flow_project_page_url(project_id)
            parsed_captcha_url = urlparse(captcha_website_url)
            captcha_origin = (
                f"{parsed_captcha_url.scheme}://{parsed_captcha_url.netloc}"
                if parsed_captcha_url.scheme and parsed_captcha_url.netloc
                else "https://flow.google.com"
            )
            self._merge_request_fingerprint(
                {
                    "project_id": project_id,
                    "origin": captcha_origin,
                    "referer": captcha_website_url,
                }
            )

            api_result = await self._get_api_captcha_token(
                captcha_method,
                project_id,
                action,
                proxy_url=proxy_url,
                user_agent=api_captcha_ua,
                website_url=captcha_website_url,
                token_id=token_id,
            )
            if api_result is None:
                return None, None
            token, captcha_user_agent = api_result
            # Merge the userAgent returned by the captcha service into the fingerprint so that Flow API submissions reuse the same UA.
            # Otherwise Google reCAPTCHA V3 evaluation will flag UNUSUAL_ACTIVITY due to UA mismatch and return
            # "reCAPTCHA evaluation failed". Other Client Hints (sec-ch-ua-platform, etc.)
            # are inferred automatically by _make_request from the UA (Windows -> "Windows", etc.).
            if captcha_user_agent:
                existing_fp = self._request_fingerprint_ctx.get()
                merged_fp = dict(existing_fp) if isinstance(existing_fp, dict) else {}
                merged_fp["user_agent"] = captcha_user_agent
                self._set_request_fingerprint(merged_fp)
                debug_logger.log_info(
                    f"[reCAPTCHA {captcha_method}] Injected the captcha UA into the request fingerprint: "
                    f"{captcha_user_agent[:80]}"
                )
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] Unknown captcha method: {captcha_method}")
            self._set_request_fingerprint(None)
            return None, None

    async def _get_api_captcha_token(
        self,
        method: str,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        website_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Optional[tuple[str, str]]:
        """Generic API captcha service

        Args:
            method: captcha service type
            project_id: project ID
            action: reCAPTCHA action type (IMAGE_GENERATION or VIDEO_GENERATION)
            proxy_url: the pre-resolved proxy address, ensuring captcha solving and subsequent submission use the same egress
            user_agent: the UA passed explicitly to the captcha service, to avoid solve/submit environment mismatch

        Returns:
            (gRecaptchaResponse, userAgent) tuple, or None (on failure).
            userAgent is returned so that subsequent Flow API requests reuse the UA used during captcha solving,
            to avoid reCAPTCHA V3 evaluation flagging UNUSUAL_ACTIVITY due to UA mismatch.
        """
        # Get the config
        if method == "flow2api_team":
            # Built-in team captcha: the address and key are hardcoded and do not accept config overrides.
            client_key = FLOW2API_TEAM_CAPTCHA_CLIENT_KEY
            base_url = FLOW2API_TEAM_CAPTCHA_BASE_URL
        elif method == "yescaptcha":
            client_key = config.yescaptcha_api_key
            base_url = config.yescaptcha_base_url
        elif method == "capmonster":
            client_key = config.capmonster_api_key
            base_url = config.capmonster_base_url
        elif method == "ezcaptcha":
            client_key = config.ezcaptcha_api_key
            base_url = config.ezcaptcha_base_url
        elif method == "capsolver":
            client_key = config.capsolver_api_key
            base_url = config.capsolver_base_url
        elif method == "captcharun":
            client_key = config.captcharun_api_key
            base_url = config.captcharun_base_url
        elif method == "twocaptcha":
            client_key = config.twocaptcha_api_key
            base_url = config.twocaptcha_base_url
        else:
            debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
            return None

        if not client_key:
            debug_logger.log_info(
                f"[reCAPTCHA] {method} API key not configured, skipping"
            )
            return None

        runtime_settings = self._resolve_recaptcha_runtime_settings(method, action)
        task_type = runtime_settings["task_type"]
        min_score = runtime_settings["min_score"]
        website_key = runtime_settings["website_key"]
        website_url = str(
            website_url or ""
        ).strip() or self._build_flow_project_page_url(project_id)
        page_action = runtime_settings["page_action"]

        try:
            # Get the proxy config so that captcha API requests also go through the proxy
            # Note: curl_cffi uses the proxy parameter for SOCKS5 and the proxies parameter for HTTP proxies
            proxy = None
            proxies = None
            resolved_proxy_url = str(proxy_url or "").strip()
            if not resolved_proxy_url and self.proxy_manager:
                try:
                    resolved_proxy_url = str(
                        await self.proxy_manager.get_request_proxy_url() or ""
                    ).strip()
                    if resolved_proxy_url:
                        if resolved_proxy_url.startswith("socks5://"):
                            # curl_cffi uses the proxy parameter for SOCKS5
                            proxy = resolved_proxy_url
                        else:
                            # HTTP/HTTPS proxies use the proxies dict
                            proxies = {
                                "http": resolved_proxy_url,
                                "https": resolved_proxy_url,
                            }
                except Exception as e:
                    debug_logger.log_warning(
                        f"[reCAPTCHA {method}] Failed to get proxy: {e}"
                    )
            elif resolved_proxy_url:
                if resolved_proxy_url.startswith("socks5://"):
                    proxy = resolved_proxy_url
                else:
                    proxies = {"http": resolved_proxy_url, "https": resolved_proxy_url}

            effective_user_agent = str(
                user_agent
                or (self.get_request_fingerprint() or {}).get("user_agent")
                or self._generate_user_agent(str(project_id or method))
            ).strip()
            if method == "twocaptcha":
                from .twocaptcha import solve_recaptcha_v3

                solution = await solve_recaptcha_v3(
                    client_key, website_url, website_key, page_action,
                    enterprise=True, base_url=base_url,
                    proxy_url=resolved_proxy_url or None,
                )
                return solution["gRecaptchaResponse"], solution["userAgent"]
            if method == "captcharun":
                headers = {
                    "Authorization": f"Bearer {client_key}",
                    "Content-Type": "application/json",
                }
                task = {
                    "captchaType": "ReCaptchaV3",
                    "siteKey": website_key,
                    "siteReferer": website_url,
                    "siteAction": page_action,
                }
                async with AsyncSession() as session:
                    request_options = {
                        "headers": headers,
                        "impersonate": "chrome",
                        "timeout": 30,
                    }
                    if proxy:
                        request_options["proxy"] = proxy
                    elif proxies:
                        request_options["proxies"] = proxies
                    result = await session.post(
                        f"{base_url.rstrip('/')}/v2/tasks",
                        json=task,
                        **request_options,
                    )
                    payload = result.json()
                    task_id = str(payload.get("taskId") or payload.get("id") or "")
                    if result.status_code >= 400 or not task_id:
                        debug_logger.log_error(
                            f"[reCAPTCHA captcharun] create task failed: {payload}"
                        )
                        return None
                    for _ in range(60):
                        await asyncio.sleep(3)
                        result = await session.get(
                            f"{base_url.rstrip('/')}/v2/tasks/{task_id}",
                            **request_options,
                        )
                        payload = result.json()
                        status = str(payload.get("status") or "").lower()
                        if status == "success":
                            solution = payload.get("response") or {}
                            token = solution.get("gRecaptchaResponse")
                            if token:
                                return str(token), effective_user_agent
                            return None
                        if status in {"failed", "failure", "error", "cancelled"}:
                            debug_logger.log_error(
                                f"[reCAPTCHA captcharun] task failed: {payload}"
                            )
                            return None
                return None

            async with AsyncSession() as session:
                create_url = f"{base_url}/createTask"
                create_data = {
                    "clientKey": client_key,
                    "task": {
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                        "type": task_type,
                        "pageAction": page_action,
                    },
                }
                if is_yes_style_captcha_method(method):
                    create_data["softID"] = "33424"
                if effective_user_agent:
                    create_data["task"]["userAgent"] = effective_user_agent
                if min_score is not None:
                    create_data["task"]["minScore"] = min_score
                if method == FLOW2API_TEAM_CAPTCHA_METHOD:
                    # The team captcha additionally supports issuing the account Cookie together with the egress proxy,
                    # so the captcha side fetches the token with the same login state + the same egress IP, giving a more stable score.
                    team_cookie_map = await self._build_team_captcha_cookie_map(
                        token_id
                    )
                    if team_cookie_map:
                        create_data["task"]["cookie"] = team_cookie_map
                    if resolved_proxy_url:
                        create_data["task"]["proxy"] = resolved_proxy_url

                task_id = None
                last_create_error = None
                max_create_attempts = (
                    len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS)
                    if is_yes_style_captcha_method(method)
                    else 1
                )
                for create_attempt in range(max_create_attempts):
                    if proxy:
                        result = await session.post(
                            create_url,
                            json=create_data,
                            impersonate="chrome",
                            proxy=proxy,
                        )
                    else:
                        result = await session.post(
                            create_url,
                            json=create_data,
                            impersonate="chrome",
                            proxies=proxies,
                        )

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] createTask response status: {result.status_code}"
                    )
                    result_json = result.json()
                    task_id = result_json.get("taskId")

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] created task_id: {task_id}, response: {result_json}"
                    )

                    if task_id:
                        break

                    error_code = str(result_json.get("errorCode") or "").strip()
                    error_desc = result_json.get("errorDescription", "Unknown error")
                    last_create_error = result_json
                    if is_yes_style_captcha_method(method) and error_code in {
                        "ERROR_NO_SLOT_AVAILABLE",
                        "ERROR_NO_SLOT_AVAILABLE_BLOCK",
                    }:
                        delay = self._get_retry_delay_seconds(
                            error_code, create_attempt
                        )
                        if create_attempt < max_create_attempts - 1:
                            debug_logger.log_warning(
                                f"[reCAPTCHA {method}] createTask resource shortage ({error_code}); will retry after {delay} seconds "
                                f"({create_attempt + 2}/{max_create_attempts})"
                            )
                            await asyncio.sleep(delay)
                            continue
                    debug_logger.log_error(
                        f"[reCAPTCHA {method}] Failed to create task: {error_desc}"
                    )
                    return None

                if not task_id:
                    error_desc = (last_create_error or {}).get(
                        "errorDescription", "Unknown error"
                    )
                    debug_logger.log_error(
                        f"[reCAPTCHA {method}] Failed to create task: {error_desc}"
                    )
                    return None

                get_url = f"{base_url}/getTaskResult"
                for i in range(40):
                    get_data = {"clientKey": client_key, "taskId": task_id}
                    # Use different parameters depending on the proxy type
                    if proxy:
                        result = await session.post(
                            get_url, json=get_data, impersonate="chrome", proxy=proxy
                        )
                    else:
                        result = await session.post(
                            get_url,
                            json=get_data,
                            impersonate="chrome",
                            proxies=proxies,
                        )
                    result_json = result.json()

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] polling #{i + 1}: {result_json}"
                    )

                    status = result_json.get("status")
                    if status == "ready":
                        solution = result_json.get("solution", {})
                        response = solution.get("gRecaptchaResponse")
                        if response:
                            self._merge_request_fingerprint(
                                self._build_fingerprint_from_user_agent(
                                    solution.get("userAgent"),
                                    accept_language=self._get_primary_accept_language(),
                                    proxy_url=resolved_proxy_url,
                                )
                            )
                            debug_logger.log_info(f"[reCAPTCHA {method}] Token acquired successfully")
                            # also return userAgent; the caller injects it into the fingerprint context,
                            # to ensure the Flow API submits requests with the UA consistent with captcha solving.
                            user_agent = solution.get("userAgent")
                            return response, user_agent

                    await asyncio.sleep(3)

                debug_logger.log_error(
                    f"[reCAPTCHA {method}] Timeout waiting for token"
                )
                return None

        except Exception as e:
            debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
            return None


class FlowClient(CurrentFlowClientMixin, _FlowClientBase):
    """Flow client backed exclusively by the current flow.google.com RPC layer."""

    @staticmethod
    def _get_runtime_config():
        return config
