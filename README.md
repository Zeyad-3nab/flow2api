# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**A full-featured, OpenAI-compatible API service that provides a unified interface for Flow**

</div>

## ❤️ Sponsors

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://useapi.net/docs/articles/google-flow-api-zh?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">
    <img src="https://useapi.net/assets/images/sponsor/useapi-banner.png" alt="useapi.net" width="150">
  </a>
</td>
<td valign="top">
  Thanks to <strong>useapi.net</strong> for sponsoring this project! <strong>One subscription, every API</strong>: useapi.net runs hosted APIs on your own Google account: <strong>Google Flow</strong> (Veo 3.1, audio-native Omni 1.1 Flash, Nano Banana Pro), <strong>Google Vids</strong> (Gemini Omni 1.1 Flash with voiced avatars, <strong>no captcha</strong>, an allowance separate from Flow, so using both on one account roughly doubles your Omni capacity), <strong>Google Flow Music</strong> (full songs with Lyria 3.5) and <strong>Gemini Notebook</strong> (NotebookLM). New: <strong>Meta Muse</strong> (muse.ai): images and about 10-second videos with spoken dialogue on your own Muse account, free plan included. Nothing to self-host. The same flat $15/month also covers Dreamina, Kling, PixVerse, MiniMax, Mureka, Runway and more. <a href="https://useapi.net/docs/api-google-flow-v1?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Flow docs</a> · <a href="https://useapi.net/docs/api-google-vids-v1?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Vids docs</a> · <a href="https://useapi.net/docs/api-muse-v1?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Muse docs</a> · <a href="https://buy.stripe.com/8x2aEX4Bd8Vh9PMg4qeUU03?client_reference_id=flow2api">Subscribe</a>
</td>
</tr>
</table>

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://proxylane.dev/?utm_source=flow2api&utm_medium=partnership&utm_campaign=flow2api_sponsor_202610&utm_content=github_readme">
    <img src="static/sponsors/proxylane-flow2api.png" alt="ProxyLane" width="150">
  </a>
</td>
<td valign="top">
  Thanks to <strong>ProxyLane</strong> for sponsoring this project! ProxyLane is a residential proxy service in 195 countries, with 95%+ of IPs rated low risk by Scamalytics and HTTP / SOCKS5 with username and password. Put the proxy into a Token's protocol or captcha proxy, or into the global request proxy, with a city and a session name in the username: each Token keeps one residential IP for up to 72 hours, and a replacement IP stays in the same city. New customers get 30% off the first purchase with code <strong>FLOW2API30</strong>. <a href="https://proxylane.dev/?utm_source=flow2api&utm_medium=partnership&utm_campaign=flow2api_sponsor_202610&utm_content=github_readme_en">Sign up here</a> to get started.
</td>
</tr>
</table>

## ✨ Core Features

- 🎨 **Text-to-Image** / **Image-to-Image**
- 🎬 **Text-to-Video** / **Image-to-Video**
- 🎞️ **Start/End Frame Video**
- 🔄 **Automatic Cookie Refresh** - periodically refreshes the complete Google/Flow Cookie and verifies the login state, with browser/personal browser fallback; after a login expires, the Cookie must be re-synced
- 📊 **Balance Display** - queries and shows VideoFX Credits in real time
- 🚀 **Load Balancing** - multi-Token round-robin and concurrency control
- 🌐 **Proxy Support** - supports HTTP/SOCKS5 proxies
- 📱 **Web Admin Panel** - intuitive Token and config management
- 🎨 **Multi-turn Image Generation**
- 🧩 **Official Gemini Request Body Compatibility** - supports `generateContent` / `streamGenerateContent`, `systemInstruction`, `contents.parts.text/inlineData/fileData`
- ✅ **Gemini Official Format Verified for Image Output** - verified with a real Token that `/models/{model}:generateContent` correctly returns the official `candidates[].content.parts[].inlineData`

## 🚀 Quick Start

### Prerequisites

- Docker and Docker Compose (recommended)
- Or Python 3.8+

- Because Flow has added an extra captcha, you can choose between browser-based captcha solving or a third-party captcha service:
  - Register for [YesCaptcha](https://yescaptcha.com/i/13Xd8K), get an API Key, and enter it in the `YesCaptcha API Key` field on the system config page.
  - Register for [CapSolver](https://dashboard.capsolver.com/passport/register?inviteCode=z95yVf1HvixI), get an API Key, and enter it in the `CapSolver API Key` field on the system config page.
  - Register for [Captcha.run](https://captcha.run/sso?inviter=36179602-dbd1-4227-8ec8-98f8c0170c59), get an API Key, and enter it in the `Captcha.run API Key` field on the system config page.
  - Register for [2Captcha](https://2captcha.com/auth/register/?from=24953866), get an API Key, and enter it in the `2Captcha API Key` field on the system config page.
- YesCaptcha lets you switch `type` in the admin panel: `RecaptchaV3TaskProxyless`, `RecaptchaV3TaskProxylessM1`, `RecaptchaV3TaskProxylessM1S7`, `RecaptchaV3TaskProxylessM1S9`; the current default recommendation is `M1S9`, and S7/S9 force a submitted `minScore` of 0.7/0.9.
- The default `docker-compose.yml` is recommended with a third-party captcha service (yescaptcha/captcharun/twocaptcha/capmonster/ezcaptcha/capsolver).
If you need headed captcha solving inside Docker (browser/personal), use the `docker-compose.headed.yml` below.

- Browser extension that automatically pushes the latest Flow login state: [Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater).

### Option 1: Docker Deployment (recommended)

#### Standard Mode (no proxy)

```bash
# 克隆项目
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

> Note: Compose already mounts `./tmp:/app/tmp` by default. Setting the cache timeout to `0` means "never auto-expire and delete"; if you want the cache files to survive a container rebuild, keep this `tmp` mount as well.

#### WARP Mode (with proxy)

```bash
# 使用 WARP 代理启动
docker-compose -f docker-compose.proxy.yml up -d

# 查看日志
docker-compose -f docker-compose.proxy.yml logs -f
```

#### Docker Headed Captcha Mode (browser / personal)

> Suitable when you have a virtualized-desktop requirement and want to enable headed browser captcha solving inside the container.  
> This mode starts `Xvfb + Fluxbox` by default to provide in-container visualization, and sets `ALLOW_DOCKER_HEADED_CAPTCHA=true`.  
> Only the application port is exposed; no remote desktop connection port is provided.
> The `personal` built-in browser now starts in headed mode by default; to temporarily switch back to headless, additionally set the environment variable `PERSONAL_BROWSER_HEADLESS=true`.

```bash
# 启动有头模式（首次建议带 --build）
docker compose -f docker-compose.headed.yml up -d --build

# 查看日志
docker compose -f docker-compose.headed.yml logs -f
```

- API port: `8000`
- After entering the admin panel, set the captcha method to `browser` or `personal`

### Option 2: Local Deployment

```bash
# 克隆项目
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

### First Access

After the service starts, open the admin panel at **http://localhost:8000**. Change the password immediately after your first login!

- **Username**: `admin`
- **Password**: `admin`

## 📈 Monitoring Endpoints

- `GET /health`: public health check that returns a summary of whether the service is alive, the active Token count, the soon-to-expire Token count, the expired Token count, the 429-disabled count, and so on
- `GET /metrics`: Prometheus metrics endpoint
- `GET /api/tokens`: admin endpoint that returns Token status such as `at_expires`, `at_expired`, `at_expiring_within_1h`, `ban_reason`, and `consecutive_error_count`

Prometheus can scrape `/metrics` directly. If you deploy to Kubernetes, it is recommended to scrape only from within the cluster and to separately restrict external access to `/metrics` at the Ingress/Gateway layer.

### Model Test Page

Visit **http://localhost:8000/test** to open the built-in model test page, which supports:

- Browsing all available models by category (image generation, text/image-to-video, multi-image video, video upscaling, etc.)
- One-click testing from a prompt, with streaming display of the generation progress
- Image upload for image-to-image / image-to-video scenarios
- Previewing the image or video directly once generation completes

## 📋 Supported Models

### Image Generation

The current `/v1/models` and Gemini `/models` expose two image series, Nano Banana 2.1 and Nano Banana 2 Lite, totaling 30 aspect-ratio and resolution variants. The existing `gemini-3.1-flash-image-*` names are retained and call the new Nano Banana 2.1 (`BELUGA`). The old Pro names `gemini-3.0-pro-image-*` still call Nano Banana Pro (`GEM_PIX_2`) but do not appear in the model list. The old Imagen models are no longer supported; when a model does not exist, it will not fall back to legacy upstream fields.

| Current Public Name | Resolution | Aspect Ratio |
|---|---:|---:|
| `gemini-3.1-flash-image-landscape` | Default | Landscape |
| `gemini-3.1-flash-image-portrait` | Default | Portrait |
| `gemini-3.1-flash-image-square` | Default | Square |
| `gemini-3.1-flash-image-four-three` | Default | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four` | Default | Portrait 3:4 |
| `gemini-3.1-flash-image-landscape-2k` | 2K | Landscape |
| `gemini-3.1-flash-image-portrait-2k` | 2K | Portrait |
| `gemini-3.1-flash-image-square-2k` | 2K | Square |
| `gemini-3.1-flash-image-four-three-2k` | 2K | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four-2k` | 2K | Portrait 3:4 |
| `gemini-3.1-flash-image-landscape-4k` | 4K | Landscape |
| `gemini-3.1-flash-image-portrait-4k` | 4K | Portrait |
| `gemini-3.1-flash-image-square-4k` | 4K | Square |
| `gemini-3.1-flash-image-four-three-4k` | 4K | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four-4k` | 4K | Portrait 3:4 |

Nano Banana 2 Lite uses `gemini-3.1-flash-lite-image-*`, with aspect-ratio and resolution suffixes identical to the table above, for example `gemini-3.1-flash-lite-image-landscape`, `gemini-3.1-flash-lite-image-portrait-2k`, `gemini-3.1-flash-lite-image-square-4k`. It also supports the simplified name `gemini-3.1-flash-lite-image` together with the `aspectRatio` / `imageSize` parameters.

The 2K/4K variants are produced by high-definition upscaling after generation and do not mean Lite natively outputs those resolutions. The main program explicitly submits the model, aspect ratio, and reference images via the image RPC; both browser captcha solving and third-party captcha solving provide only the captcha. The `browser` / `personal` modes do not require a third-party captcha API key, and image, upload, upscale, and video requests obtain a captcha according to the required action. Lite does not automatically fall back to other image models.

### Video Generation

The current `/v1/models` and Gemini `/models` expose only Flow's current model families. The legacy underscore model names remain compatible but no longer appear in the model list.

| Current Public Name | Upstream Model Family | Capability |
|---|---|---|
| `veo-3.1-lite` / `veo-3.1-fast` / `veo-3.1-quality` / `omni-1.1-flash` | Current default model | 8-second landscape text-to-video |
| `veo-3.1-lite-{4,6,8}s-{landscape,portrait}` | Veo 3.1 - Lite | Text-to-video |
| `veo-3.1-fast-{4,6,8}s-{landscape,portrait}` | Veo 3.1 - Fast | Text-to-video |
| `veo-3.1-quality-{4,6,8}s-{landscape,portrait}` | Veo 3.1 - Quality | Text-to-video |
| `omni-1.1-flash-{4,6,8,10}s-{landscape,portrait}` | Omni 1.1 Flash (`abra`) | Text-to-video / reference-image video |
| `veo-3.1-{lite,fast,quality}-i2v-{4,6}s-{landscape,portrait}` | Veo 3.1 | Start-frame / start-end-frame video |
| `veo-3.1-{lite,fast}-r2v-8s-{landscape,portrait}` | Veo 3.1 | Multi-reference-image video |
| `veo-3.1-{lite,fast,quality}-extend-8s-{landscape,portrait}` | Veo 3.1 | Video extension |

The braces in the table mean you pick one of the values. For example, `veo-3.1-fast-{4,6,8}s-{landscape,portrait}` covers the combinations of 4/6/8 seconds with landscape/portrait.

#### Omni 1.1 Flash

`Omni 1.1 Flash` supports both text-to-video and reference-image video. Callers use the same set of model names, and the service automatically selects the generation mode based on whether the request contains images:

- `omni-1.1-flash` is the default alias for `omni-1.1-flash-8s-landscape`.
- The full model name is `omni-1.1-flash-{4,6,8,10}s-{landscape,portrait}`, letting you choose 4, 6, 8, or 10 seconds and landscape or portrait.
- Text-only input uses text-to-video mode; providing 1–3 `image_url` images uses reference-image video mode.
- Duration and orientation are specified by the model name; there is no need to pass the equivalent parameters separately.

Example reference-image video request:

```json
{
  "model": "omni-1.1-flash-8s-landscape",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "让画面中的人物自然转身并看向镜头" },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/jpeg;base64,<参考图base64>"
          }
        }
      ]
    }
  ],
  "stream": true
}
```

#### Legacy names: Text-to-Video (T2V - Text to Video)
⚠️ **Image upload not supported**

| Model Name | Description | Dimensions |
|---------|---------|--------|
| `veo_3_1_t2v_fast_portrait` | Text-to-video | Portrait |
| `veo_3_1_t2v_fast_landscape` | Text-to-video | Landscape |
| `veo_3_1_t2v_fast_portrait_ultra` | Text-to-video | Portrait |
| `veo_3_1_t2v_fast_ultra` | Text-to-video | Landscape |
| `veo_3_1_t2v_fast_portrait_ultra_relaxed` | Text-to-video | Portrait |
| `veo_3_1_t2v_fast_ultra_relaxed` | Text-to-video | Landscape |
| `veo_3_1_t2v_portrait` | Text-to-video | Portrait |
| `veo_3_1_t2v_landscape` | Text-to-video | Landscape |
| `veo_3_1_t2v_landscape_4s` | Text-to-video 4s | Landscape |
| `veo_3_1_t2v_portrait_4s` | Text-to-video 4s | Portrait |
| `veo_3_1_t2v_landscape_6s` | Text-to-video 6s | Landscape |
| `veo_3_1_t2v_portrait_6s` | Text-to-video 6s | Portrait |
| `veo_3_1_t2v_fast_landscape_4s` | Text-to-video Fast 4s | Landscape |
| `veo_3_1_t2v_fast_portrait_4s` | Text-to-video Fast 4s | Portrait |
| `veo_3_1_t2v_fast_landscape_6s` | Text-to-video Fast 6s | Landscape |
| `veo_3_1_t2v_fast_portrait_6s` | Text-to-video Fast 6s | Portrait |
| `veo_3_1_t2v_lite_portrait` | Text-to-video Lite | Portrait |
| `veo_3_1_t2v_lite_landscape` | Text-to-video Lite | Landscape |
| `veo_3_1_t2v_lite_4s_portrait` | Text-to-video Lite 4s | Portrait |
| `veo_3_1_t2v_lite_4s_landscape` | Text-to-video Lite 4s | Landscape |
| `veo_3_1_t2v_lite_6s_portrait` | Text-to-video Lite 6s | Portrait |
| `veo_3_1_t2v_lite_6s_landscape` | Text-to-video Lite 6s | Landscape |

#### Legacy names: Start/End Frame models (I2V - Image to Video)
📸 **Supports 1-2 images: 1 as the start frame, 2 as the start and end frames**

> 💡 **Automatic adaptation**: the system selects the matching model_key based on the number of images
> - **Single-frame mode** (1 image): generates the video from the start frame
> - **Dual-frame mode** (2 images): generates a transition video from the start + end frames
> - `veo_3_1_i2v_lite_*` supports only **1** start-frame image
> - `veo_3_1_interpolation_lite_*` supports only **2** start/end-frame images

| Model Name | Description | Dimensions |
|---------|---------|--------|
| `veo_3_1_i2v_s_fast_portrait_fl` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_fast_fl` | Image-to-video | Landscape |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_fast_ultra_fl` | Image-to-video | Landscape |
| `veo_3_1_i2v_s_fast_portrait_ultra_relaxed` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_fast_ultra_relaxed` | Image-to-video | Landscape |
| `veo_3_1_i2v_s_portrait` | Image-to-video | Portrait |
| `veo_3_1_i2v_s_landscape` | Image-to-video | Landscape |
| `veo_3_1_i2v_s_landscape_4s` | Image-to-video 4s | Landscape |
| `veo_3_1_i2v_s_portrait_4s` | Image-to-video 4s | Portrait |
| `veo_3_1_i2v_s_landscape_6s` | Image-to-video 6s | Landscape |
| `veo_3_1_i2v_s_portrait_6s` | Image-to-video 6s | Portrait |
| `veo_3_1_i2v_s_fast_landscape_4s_fl` | Image-to-video Fast 4s | Landscape |
| `veo_3_1_i2v_s_fast_portrait_4s_fl` | Image-to-video Fast 4s | Portrait |
| `veo_3_1_i2v_s_fast_landscape_6s_fl` | Image-to-video Fast 6s | Landscape |
| `veo_3_1_i2v_s_fast_portrait_6s_fl` | Image-to-video Fast 6s | Portrait |
| `veo_3_1_i2v_lite_portrait` | Image-to-video Lite (start frame only) | Portrait |
| `veo_3_1_i2v_lite_landscape` | Image-to-video Lite (start frame only) | Landscape |
| `veo_3_1_i2v_lite_4s_portrait` | Image-to-video Lite 4s (start frame only) | Portrait |
| `veo_3_1_i2v_lite_4s_landscape` | Image-to-video Lite 4s (start frame only) | Landscape |
| `veo_3_1_i2v_lite_6s_portrait` | Image-to-video Lite 6s (start frame only) | Portrait |
| `veo_3_1_i2v_lite_6s_landscape` | Image-to-video Lite 6s (start frame only) | Landscape |
| `veo_3_1_interpolation_lite_portrait` | Image-to-video Lite (start/end-frame transition) | Portrait |
| `veo_3_1_interpolation_lite_landscape` | Image-to-video Lite (start/end-frame transition) | Landscape |
| `veo_3_1_interpolation_lite_4s_portrait` | Image-to-video Lite 4s (start/end-frame transition) | Portrait |
| `veo_3_1_interpolation_lite_4s_landscape` | Image-to-video Lite 4s (start/end-frame transition) | Landscape |
| `veo_3_1_interpolation_lite_6s_portrait` | Image-to-video Lite 6s (start/end-frame transition) | Portrait |
| `veo_3_1_interpolation_lite_6s_landscape` | Image-to-video Lite 6s (start/end-frame transition) | Landscape |

#### Legacy names: Multi-image Generation (R2V - Reference Images to Video)
🖼️ **Supports multiple images**

> **2026-03-06 Update**
>
> - Synced with the upstream's new `R2V` video request body
> - `textInput` has been switched to `structuredPrompt.parts`
> - Added `mediaGenerationContext.batchId` at the top level
> - Added `useV2ModelConfig: true` at the top level
> - Landscape / portrait `R2V` models share the same new request body
> - The upstream `videoModelKey` for landscape `R2V` has been switched to the `*_landscape` form
> - Per the current upstream protocol, `referenceImages` currently accepts at most **3** images

| Model Name | Description | Dimensions |
|---------|---------|--------|
| `veo_3_1_r2v_fast_portrait` | Image-to-video | Portrait |
| `veo_3_1_r2v_fast_landscape` | Image-to-video | Landscape |
| `veo_3_1_r2v_fast_portrait_ultra` | Image-to-video | Portrait |
| `veo_3_1_r2v_fast_landscape_ultra` | Image-to-video | Landscape |
| `veo_3_1_r2v_fast_portrait_ultra_relaxed` | Image-to-video | Portrait |
| `veo_3_1_r2v_fast_landscape_ultra_relaxed` | Image-to-video | Landscape |

#### Legacy names: Video Upscaling models (Upsample)

These models do not call the upstream upsampler key directly; instead, they first generate the video with the corresponding standard Veo 3.1 model, then submit a 1080P/4K upscaling request.

| Model Name | Description | Output |
|---------|---------|--------|
| `veo_3_1_t2v_landscape_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_portrait_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_landscape_1080p` | Text-to-video upscale | 1080P |
| `veo_3_1_t2v_portrait_1080p` | Text-to-video upscale | 1080P |
| `veo_3_1_t2v_landscape_4s_4k` | Text-to-video 4s upscale | 4K |
| `veo_3_1_t2v_portrait_4s_4k` | Text-to-video 4s upscale | 4K |
| `veo_3_1_t2v_landscape_4s_1080p` | Text-to-video 4s upscale | 1080P |
| `veo_3_1_t2v_portrait_4s_1080p` | Text-to-video 4s upscale | 1080P |
| `veo_3_1_t2v_landscape_6s_4k` | Text-to-video 6s upscale | 4K |
| `veo_3_1_t2v_portrait_6s_4k` | Text-to-video 6s upscale | 4K |
| `veo_3_1_t2v_landscape_6s_1080p` | Text-to-video 6s upscale | 1080P |
| `veo_3_1_t2v_portrait_6s_1080p` | Text-to-video 6s upscale | 1080P |
| `veo_3_1_t2v_fast_portrait_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_portrait_ultra_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_ultra_4k` | Text-to-video upscale | 4K |
| `veo_3_1_t2v_fast_portrait_1080p` | Text-to-video upscale | 1080P |
| `veo_3_1_t2v_fast_1080p` | Text-to-video upscale | 1080P |
| `veo_3_1_t2v_fast_portrait_ultra_1080p` | Text-to-video upscale | 1080P |
| `veo_3_1_t2v_fast_ultra_1080p` | Text-to-video upscale | 1080P |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_fast_ultra_fl_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p` | Image-to-video upscale | 1080P |
| `veo_3_1_i2v_s_fast_ultra_fl_1080p` | Image-to-video upscale | 1080P |
| `veo_3_1_i2v_s_landscape_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_portrait_4k` | Image-to-video upscale | 4K |
| `veo_3_1_i2v_s_landscape_1080p` | Image-to-video upscale | 1080P |
| `veo_3_1_i2v_s_portrait_1080p` | Image-to-video upscale | 1080P |
| `veo_3_1_i2v_s_landscape_4s_4k` | Image-to-video 4s upscale | 4K |
| `veo_3_1_i2v_s_portrait_4s_4k` | Image-to-video 4s upscale | 4K |
| `veo_3_1_i2v_s_landscape_4s_1080p` | Image-to-video 4s upscale | 1080P |
| `veo_3_1_i2v_s_portrait_4s_1080p` | Image-to-video 4s upscale | 1080P |
| `veo_3_1_i2v_s_landscape_6s_4k` | Image-to-video 6s upscale | 4K |
| `veo_3_1_i2v_s_portrait_6s_4k` | Image-to-video 6s upscale | 4K |
| `veo_3_1_i2v_s_landscape_6s_1080p` | Image-to-video 6s upscale | 1080P |
| `veo_3_1_i2v_s_portrait_6s_1080p` | Image-to-video 6s upscale | 1080P |
| `veo_3_1_r2v_fast_portrait_ultra_4k` | Multi-image video upscale | 4K |
| `veo_3_1_r2v_fast_landscape_ultra_4k` | Multi-image video upscale | 4K |
| `veo_3_1_r2v_fast_portrait_ultra_1080p` | Multi-image video upscale | 1080P |
| `veo_3_1_r2v_fast_landscape_ultra_1080p` | Multi-image video upscale | 1080P |

## 📡 API Usage Examples (streaming required)

> Besides the `OpenAI-compatible` examples below, the service also supports the official Gemini format:
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> The official Gemini format supports the following authentication methods:
> - `Authorization: Bearer <api_key>`
> - `x-goog-api-key: <api_key>`
> - `?key=<api_key>`
>
> The official Gemini image request body already supports:
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Gemini Official generateContent (Text-to-Image)

> Verified with a real Token.
> For a streaming response, replace the path with `:streamGenerateContent?alt=sse`.

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {
      "parts": [
        {
          "text": "Return an image only."
        }
      ]
    },
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "一颗放在木桌上的红苹果，棚拍光线，极简背景"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }'
```

### Text-to-Image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只可爱的猫咪在花园里玩耍"
      }
    ],
    "stream": true
  }'
```

### Image-to-Image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "将这张图片变成水彩画风格"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_encoded_image>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Text-to-Video

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast-8s-landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只小猫在草地上追逐蝴蝶"
      }
    ],
    "stream": true
  }'
```

### Start/End Frame Video Generation

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast-i2v-4s-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "从第一张图过渡到第二张图"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<首帧base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<尾帧base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Multi-image Video Generation

> The server automatically assembles the new video request body for `R2V`; callers still just use OpenAI-compatible input.
> The server automatically maps landscape `R2V` to the latest `*_landscape` upstream model key.
> At most **3** reference images are currently allowed.

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast-r2v-8s-portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "以三张参考图的人物和场景为基础，生成一段镜头平滑推进的竖屏视频"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图1base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图2base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图3base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgements

- The YesCaptcha captcha-solving solution provided by [PearNoDec](https://github.com/PearNoDec)
- The headless captcha-solving solution provided by [raomaiping](https://github.com/raomaiping)

Thanks to all contributors and users for their support!

---

## 📞 Contact

- Submit an issue: [GitHub Issues](https://github.com/TheSmallHanCat/flow2api/issues)

---

**⭐ If this project helps you, please give it a Star!**

## Star History

[![Star History Chart](https://star-history.dera.page/svg?repos=TheSmallHanCat/flow2api&type=date&legend=top-left)](https://star-history.dera.page/#TheSmallHanCat/flow2api&type=date&legend=top-left)
