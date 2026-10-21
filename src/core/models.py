"""Data models for Flow2API"""

from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Union, Any, Literal
from datetime import datetime


class Token(BaseModel):
    """Token model for Flow2API"""

    id: Optional[int] = None

    # Authentication info (core)
    st: str  # Session compatibility identifier; the new Flow uses full Google/Flow Cookies
    at: Optional[str] = None  # Compatibility identifier after session validation
    at_expires: Optional[datetime] = None  # Next session validation time

    # Basic info
    email: str
    name: Optional[str] = ""
    remark: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    use_count: int = 0

    # VideoFX-specific fields
    credits: int = 0  # Remaining credits
    user_paygate_tier: Optional[str] = None  # PAYGATE_TIER_ONE

    # Project management
    current_project_id: Optional[str] = None  # UUID of the currently used project
    current_project_name: Optional[str] = None  # Project name

    # Feature toggles
    image_enabled: bool = True
    video_enabled: bool = True

    # Concurrency limits
    image_concurrency: int = -1  # -1 means unlimited
    video_concurrency: int = -1  # -1 means unlimited

    # Captcha solving proxy (token-level, can override the global browser captcha proxy)
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None

    # Google/Flow Cookie refresh; legacy field names kept for compatibility with existing data
    protocol_mode: str = "session"  # session/protocol
    google_cookies: str = ""
    login_account: str = ""
    login_password: str = ""
    proxy_url: str = ""
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = 30
    last_st_refresh_at: Optional[datetime] = None
    last_st_refresh_result: str = ""

    # 429 ban related
    ban_reason: Optional[str] = None  # Ban reason: "429_rate_limit" or None
    banned_at: Optional[datetime] = None  # Ban time


class Project(BaseModel):
    """Project model for VideoFX"""

    id: Optional[int] = None
    project_id: str  # VideoFX project UUID
    token_id: int  # Associated Token ID
    project_name: str  # Project name
    tool_name: str = "PINHOLE"  # Tool name, fixed to PINHOLE
    is_active: bool = True
    created_at: Optional[datetime] = None


class TokenStats(BaseModel):
    """Token statistics"""

    token_id: int
    image_count: int = 0
    video_count: int = 0
    success_count: int = 0
    error_count: int = 0  # Historical total errors (never reset)
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    # Today's statistics
    today_image_count: int = 0
    today_video_count: int = 0
    today_error_count: int = 0
    today_date: Optional[str] = None
    # Consecutive error count (used for the auto-disable check)
    consecutive_error_count: int = 0


class Task(BaseModel):
    """Generation task"""

    id: Optional[int] = None
    task_id: str  # operation name returned by the Flow API
    token_id: int
    model: str
    prompt: str
    status: str  # processing, completed, failed
    progress: int = 0  # 0-100
    result_urls: Optional[List[str]] = None
    error_message: Optional[str] = None
    scene_id: Optional[str] = None  # sceneId from the Flow API
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RequestLog(BaseModel):
    """API request log"""

    id: Optional[int] = None
    token_id: Optional[int] = None
    operation: str
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    status_code: int
    duration: float
    status_text: Optional[str] = None
    progress: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AdminConfig(BaseModel):
    """Admin configuration"""

    id: int = 1
    username: str
    password: str
    api_key: str
    error_ban_threshold: int = 3  # Auto-disable token after N consecutive errors


class ProxyConfig(BaseModel):
    """Proxy configuration"""

    id: int = 1
    enabled: bool = False  # Request proxy toggle
    proxy_url: Optional[str] = None  # Request proxy address
    media_proxy_enabled: bool = False  # Image upload/download proxy toggle
    media_proxy_url: Optional[str] = None  # Image upload/download proxy address


class GenerationConfig(BaseModel):
    """Generation timeout configuration"""

    id: int = 1
    image_timeout: int = 300  # seconds
    video_timeout: int = 1500  # seconds
    max_retries: int = 3  # Maximum number of request retries


class CallLogicConfig(BaseModel):
    """Token selection call logic configuration"""

    id: int = 1
    call_mode: str = "default"
    polling_mode_enabled: bool = False
    updated_at: Optional[datetime] = None


class CacheConfig(BaseModel):
    """Cache configuration"""

    id: int = 1
    cache_enabled: bool = False
    cache_timeout: int = 7200  # seconds (2 hours), 0 means never expire
    cache_base_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DebugConfig(BaseModel):
    """Debug configuration"""

    id: int = 1
    enabled: bool = False
    log_requests: bool = True
    log_responses: bool = True
    mask_token: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CaptchaConfig(BaseModel):
    """Captcha configuration"""

    id: int = 1
    captcha_method: str = (
        "yescaptcha"  # yescaptcha/flow2api_team/captcharun/twocaptcha/capsolver/ezcaptcha/browser/personal/remote_browser
    )
    yescaptcha_api_key: str = ""
    yescaptcha_base_url: str = "https://api.yescaptcha.com"
    yescaptcha_task_type: str = "RecaptchaV3TaskProxylessM1S9"
    capmonster_api_key: str = ""
    capmonster_base_url: str = "https://api.capmonster.cloud"
    ezcaptcha_api_key: str = ""
    ezcaptcha_base_url: str = "https://api.ez-captcha.com"
    capsolver_api_key: str = ""
    capsolver_base_url: str = "https://api.capsolver.com"
    captcharun_api_key: str = ""
    captcharun_base_url: str = "https://api.captcha-run.com"
    twocaptcha_api_key: str = ""
    twocaptcha_base_url: str = "https://api.2captcha.com"
    remote_browser_base_url: str = ""
    remote_browser_api_key: str = ""
    remote_browser_timeout: int = 60
    website_key: str = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
    page_action: str = "IMAGE_GENERATION"
    browser_proxy_enabled: bool = False  # Whether browser captcha solving uses a proxy
    browser_proxy_url: Optional[str] = None  # Browser captcha solving proxy URL
    browser_count: int = 1  # Number of browser captcha-solving instances
    personal_project_pool_size: int = (
        4  # Default number of projects maintained per Token (only affects project rotation)
    )
    personal_max_resident_tabs: int = 5  # Upper limit of shared captcha-solving tabs per built-in browser instance
    browser_personal_fresh_restart_every_n_solves: int = (
        10  # After how many successful solves the browser is cleaned up and restarted; 0 disables it
    )
    personal_idle_tab_ttl_seconds: int = 600  # Idle timeout (seconds) for built-in browser tabs
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PluginConfig(BaseModel):
    """Plugin connection configuration"""

    id: int = 1
    connection_token: str = ""  # Plugin connection token
    auto_enable_on_update: bool = True  # Automatically enable the token on update (on by default)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TokenRefreshConfig(BaseModel):
    """Protocol ST refresh configuration"""

    id: int = 1
    enabled: bool = True
    refresh_interval_minutes: int = 30
    updated_at: Optional[datetime] = None


# OpenAI Compatible Request Models
class ChatMessage(BaseModel):
    """Chat message"""

    role: str
    content: Union[str, List[dict]]  # string or multimodal array


class ImageConfig(BaseModel):
    """Gemini imageConfig parameters"""

    aspectRatio: Optional[str] = None  # "16:9", "9:16", "1:1", "4:3", "3:4"
    imageSize: Optional[str] = None  # "2k", "4k"

    # Compatible with size/quality or snake_case fields that upstreams like OpenAI/NewAPI may pass through
    model_config = ConfigDict(extra="allow")


class GenerationConfigParam(BaseModel):
    """Gemini generationConfig parameters (for model name resolution)"""

    responseModalities: Optional[List[str]] = None  # ["IMAGE", "TEXT"]
    imageConfig: Optional[ImageConfig] = None

    model_config = ConfigDict(extra="allow")


class GeminiInlineData(BaseModel):
    """Gemini inline binary data."""

    mimeType: str
    data: str


class GeminiFileData(BaseModel):
    """Gemini file reference."""

    fileUri: str
    mimeType: Optional[str] = None


class GeminiPart(BaseModel):
    """Gemini content part."""

    text: Optional[str] = None
    inlineData: Optional[GeminiInlineData] = None
    fileData: Optional[GeminiFileData] = None

    model_config = ConfigDict(extra="allow")


class GeminiContent(BaseModel):
    """Gemini content block."""

    role: Optional[Literal["user", "model"]] = None
    parts: List[GeminiPart]


class GeminiGenerateContentRequest(BaseModel):
    """Gemini official generateContent request."""

    contents: List[GeminiContent]
    generationConfig: Optional[GenerationConfigParam] = None
    systemInstruction: Optional[GeminiContent] = None

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    """Chat completion request (OpenAI compatible + Gemini extension)"""

    model: str
    messages: Optional[List[ChatMessage]] = None
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Flow2API specific parameters
    image: Optional[str] = None  # Base64 encoded image (deprecated, use messages)
    video: Optional[str] = None  # Base64 encoded video (deprecated)
    # Gemini extension parameters (from extra_body or top-level)
    generationConfig: Optional[GenerationConfigParam] = None
    contents: Optional[List[Any]] = None  # Gemini native contents

    model_config = ConfigDict(
        extra="allow"
    )  # Allow extra fields like extra_body passthrough
