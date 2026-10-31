# Provider and publisher asset provenance

Collected 2026-10-20. These are source-provided brand marks/icons; no logo geometry was reconstructed. Owners retain their respective rights. Verify current brand terms before redistribution. SVG assets were parsed as XML and checked for scripts, event-handler attributes, and external references.

| Asset | Brand | Original source | Notes |
| --- | --- | --- | --- |
| provider-openai.svg | OpenAI; Codex row | https://cdn.openai.com/brand/OpenAI-Logos-2025.zip (OpenAI-logos(new)/SVGs/OpenAI-black-monoblossom.svg) | Official OpenAI brand package; used for Codex CLI because no separate CLI mark was located. |
| provider-ollama.svg | Ollama | https://github.com/ollama/ollama/blob/main/docs/ollama-logo.svg | Official repository. |
| provider-deepseek.svg | DeepSeek | https://github.com/deepseek-ai/DeepSeek-LLM/blob/main/images/logo.svg | Official repository. |
| provider-groq.svg | Groq | https://groq.com/favicon.svg | Official homepage favicon, downloaded 2026-10-20. Original bytes retained; parsed as SVG with no scripts or external references. |
| provider-xai.svg | Grok / xAI | https://grok.com/images/favicon.svg | Official Grok symbol-only favicon, downloaded 2026-10-21; original bytes retained, SHA256 `c3db0dfaf760b702b8490c6cbefe07fd8bfe00db43cae6a0acccf768f44d6179`. Parsed as SVG with no scripts, event handlers or external references. The original black tile and white symbol are rendered without an additional badge. Brand terms: https://x.ai/legal/brand-guidelines. |
| provider-mistral.svg | Mistral | https://github.com/mistralai/mistral-common/blob/main/docs/assets/logo.svg | Official Mistral repository. The source SVG contains the original orange-red pixel M beside the wordmark; the icon renderer uses an in-memory viewBox crop to show that original M. |
| provider-openrouter.svg | OpenRouter | https://openrouter.ai/brand/logos/transparent/glyph/svg/glyph-ink.svg | Official OpenRouter brand-assets page; original ink glyph SVG. Parsed as XML and checked for scripts, event-handler attributes, and external references. |
| provider-opencode.png | OpenCode | https://opencode.ai/favicon-96x96-v3.png | Official OpenCode homepage favicon (96px). |
| provider-glm.svg | GLM / Z.ai | https://z-cdn.chatglm.cn/z-ai/static/logo.svg | Logo URL used by the official Z.ai homepage icon metadata. Parsed as XML and checked for scripts, event-handler attributes, and external references. |
| provider-antigravity.svg | Google Antigravity | https://antigravity.google/assets/image/brand/antigravity-icon__full-color.svg | Official Google Antigravity Press Assets page SVG. Parsed as XML and checked for scripts, event-handler attributes, and external references. |
| provider-typesafe.png | TypeSafe AI | https://framerusercontent.com/images/aNFzSFxM4fjICmnibw7npfZjcQ.png | Official TypeSafe AI homepage favicon image. |
| provider-freetoken.png | FreeToken | https://avatars.githubusercontent.com/u/286493426?s=60&v=4 | Official GitHub account avatar URL requested for the provider row (60×60); original pixels are retained and drawn over a rounded white contrast tile. |
| provider-claude.png | Anthropic Claude | https://claude.ai/favicon.ico | Official 48px favicon frame retained as PNG. |
| provider-gemini.png | Google Gemini | https://www.gstatic.com/lamda/images/gemini_sparkle_4g_512_lt_f94943af3be039176192d.png | Linked from Gemini official homepage icon metadata. |
| provider-google.png | Google Search | https://www.google.com/favicon.ico | Official favicon endpoint; PNG is the runtime icon asset. |
| provider-bing.png | Bing Search | https://www.bing.com/sa/simg/favicon-trans-bg-blue-mg-png.png | Official Bing homepage rel=icon. |
| provider-duckduckgo.png | DuckDuckGo Search | https://duckduckgo.com/static-assets/favicons/favicon.ico | Official page icon; the PNG is a visual-check export. DDG-icon_256x256.png from the same official page is also saved as provider-duckduckgo.png. |
| publisher-sciencedirect.svg | ScienceDirect / Elsevier | https://upload.wikimedia.org/wikipedia/commons/6/6a/ScienceDirect_logo.svg | Original Elsevier mark, identified by Commons as public-domain simple text/geometric logo; Commons cites Elsevier branding source http://info.sciencedirect.com/branding. ScienceDirect official page returned HTTP 403 during retrieval. |
| publisher-nature.png | Nature | https://www.nature.com/static/images/favicons/nature/apple-touch-icon-f39cb19454.png | Official Nature touch icon URL, verified in page metadata. |
| publisher-wiley.png | Wiley | https://cdn.prod.website-files.com/69aab977ac635f7d8bab71fc/6a3dcb24736b91575cba6694_Variant%3DLogo%20Mark.png | Official Wiley homepage apple-touch-icon metadata. |
| publisher-springer.png | SpringerLink | https://link.springer.com/oscar-static/images/favicons/springerlink/favicon-16x16-8bd8c1c945.png | Official 16×16 SpringerLink favicon; the original navy/orange horse mark is retained and shown over a rounded white contrast tile. The previously collected 247×19 Springer Nature Link platform wordmark is kept only in `.local-review/publisher-springer-platform-wordmark.png`, not used as an icon. |

## Rendering treatment

Provider OpenAI, Ollama, and OpenRouter marks use the theme text color by replacing only their source's known monochrome fill values at render time; they remain on transparent backgrounds without a white badge. The Mistral pixel M is selected from the existing official SVG using a viewBox crop, preserving its original colors and paths. The FreeToken avatar and Wiley/Springer publisher raster marks preserve their source pixels and receive a rounded white contrast tile. Qt renders raster marks at the requested device-pixel ratio.

## Unresolved marks

The earlier Commons filename candidate did not resolve, and no higher-resolution Springer horse source was verified. The official SpringerLink 16×16 favicon does provide a square original navy/orange horse mark, which is used here with a contrast tile and DPR-aware rendering. The original 247×19 Springer Nature Link platform wordmark is retained in workspace review staging for reference only. Existing assets and icons41 sprites were left untouched.

## API-key portals checked

| Provider | Portal |
| --- | --- |
| OpenAI | https://platform.openai.com/api-keys |
| Anthropic | https://console.anthropic.com/settings/keys (redirects to https://platform.claude.com/settings/keys) |
| Gemini API | https://aistudio.google.com/apikey |
| OpenRouter | https://openrouter.ai/keys |
| DeepSeek | https://platform.deepseek.com/api_keys |
| Groq | https://console.groq.com/keys |
| Grok / xAI | https://console.x.ai/ |
| Mistral | https://console.mistral.ai/api-keys |
| OpenCode Zen | https://opencode.ai/zen |
| GLM / ZhipuAI | https://open.bigmodel.cn/ |
| FreeToken | https://console.freetoken.ai/users/sign_in |

Codex CLI, Claude Code, Gemini CLI, Antigravity CLI, Ollama, Google/Bing/DuckDuckGo Search, and host integration are CLI/local/search/host-managed rather than provider API-key entries. TypeSafe AI's distinct key workflow was not verified; OpenCode Zen materials describe its Jev model as available through the Zen key.
