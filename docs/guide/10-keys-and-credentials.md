# 10 — Keys and credentials

Callimachus uses several kinds of credentials, but they do not have the same purpose or security boundary. The report-signing key is created locally and must be hidden from the process being constrained. Provider API keys are issued by external services and are normally available to the process that makes the request. CLI backends reuse an existing login and need no provider key in `.env`. The local `freetoken` backend is also credentialless and connects to an external server operated separately.

## Choose the key you actually need

| Need | Credential | How it is obtained | Where it belongs |
|---|---|---|---|
| Make report approval externally verifiable | HMAC signing key | Generate it locally once | A protected file exposed only to the trusted Stop hook or CI gate |
| Run Jury1/Jury2 through a credentialed HTTP LLM backend | LLM provider API key | Create it in that provider's official console | `.env`, a process secret, or the CI secret store used by the Verify process |
| Improve source resolution or retrieval | Resolver/content API key | Register with the relevant data service | `.env` or a process/CI secret; these are optional |
| Run through `claude_cli`, `codex_cli`, or `gemini_cli` | Existing CLI login | Sign in with the installed CLI | The CLI's own credential store; no API key is copied into `.env` |
| Run through a local `freetoken` server | None | Install and start FreeToken separately | Put only the server root and model ID in `.env`; there is no FreeToken API key |

Do not use an LLM API key as the signing key. Do not put the signing key in `.env`. Those mistakes collapse the intended trust boundary.

## Generate the report-signing key

The signing key is a 256-bit random value stored as 64 hexadecimal characters. Callimachus creates the file with user-only `0600` permissions and refuses to replace an existing key. Replacing it deliberately invalidates HMAC verification for reports sealed with the old key.

### Fresh setup with the configuration tool

The GUI exposes the signing-key path and `.env` destination:

```bash
python run.py configure
```

For a fresh headless setup, make the `.env` destination explicit:

```bash
python run.py configure --headless \
  --env-path .env \
  --mailto you@example.org \
  --accuracy standard \
  --gen-key
```

With no path after `--gen-key`, the key is created at the OS default: `%LOCALAPPDATA%\citation-verifier\signing.key` on Windows; `$XDG_CONFIG_HOME/citation-verifier/signing.key` (or `$HOME/.config/citation-verifier/signing.key`) on Linux and WSL. macOS uses the usable POSIX default below:

```text
$XDG_CONFIG_HOME/citation-verifier/signing.key
```

or, when `XDG_CONFIG_HOME` is unset:

```text
$HOME/.config/citation-verifier/signing.key
```

Use an explicit absolute path when the trusted hook or CI account has a different filesystem layout:

```bash
python run.py configure --headless \
  --env-path .env \
  --accuracy standard \
  --gen-key /absolute/protected/path/signing.key
```

The configuration command atomically patches only explicitly supplied `.env` values; when none are supplied to an existing file it is a no-op. It never puts the signing-key path in `.env`.

### Prepare Claude Code instructions from WSL for Windows-host hooks

When Claude Code runs on Windows but the repository and hook executor are in WSL, run this command **inside WSL** with an explicit absolute path to the Windows Claude settings as visible from WSL and a distribution name:

```bash
python run.py configure --headless --prepare-claude-hooks \
  --claude-hook-target windows-wsl --wsl-distro Ubuntu \
  --settings /mnt/c/Users/you/.claude/settings.json \
  --gen-key
```

This writes a manual patch beside `settings.json`; review and apply it yourself. The generated Windows-host command calls `wsl.exe --exec` directly, retains Linux paths, and passes the signing-key file only to the Stop hook. It cannot make a key protected if the Windows user, the WSL user, or the constrained agent can read that file; keep the key in a trusted Hook/CI boundary.

### Add a key without rewriting an existing `.env`

From the repository root, use the normal CLI; with no explicitly supplied `.env` values, an existing `.env` is byte-for-byte unchanged:

```bash
python run.py configure --headless --gen-key
```

For a custom key path:

```bash
python run.py configure --headless --gen-key /absolute/protected/path/signing.key
```

Neither command prints the key material. The setup log reports the key path and whether the file was created or retained; an existing key file remains unchanged.

### Stronger shared-machine deployment

On a machine where the constrained process runs as a different user, a root-owned key can provide a stronger boundary:

```bash
sudo python3 -c 'from core.app.commands.configure import generate_key; print(generate_key("/etc/citation-verifier/signing.key"))'
sudo chmod 600 /etc/citation-verifier/signing.key
```

The helper retains an existing key rather than rotating it. This example assumes the trusted gate can read a root-owned file; use ownership for the dedicated trusted service account when it runs as another user.

### Check the key without disclosing it

Do not use `cat`, paste the key into an issue, or include it in diagnostics. Check only the path, size, and permissions:

```bash
test -s "$HOME/.config/citation-verifier/signing.key"
ls -l "$HOME/.config/citation-verifier/signing.key"
```

To confirm that the trusted process can load it without printing it:

```bash
CITATION_VERIFIER_SIGNING_KEY_FILE="$HOME/.config/citation-verifier/signing.key" \
  python -c 'from core.infra.integrity.signing import key_present; raise SystemExit(0 if key_present() else 1)'
```

## Expose the signing key only to the trusted side

Set the path in the Stop hook or CI gate environment:

```bash
export CITATION_VERIFIER_SIGNING_KEY_FILE=/absolute/protected/path/signing.key
```

The preferred arrangement is:

```text
constrained run process     -> cannot read the key
trusted Stop hook / CI gate -> can read the key and verify or seal the report
```

Never place `CITATION_VERIFIER_SIGNING_KEY_FILE` or the inline `CITATION_VERIFIER_SIGNING_KEY` in:

- the project `.env` when the constrained agent can read it;
- a repository-level hook configuration the constrained agent can edit;
- a shell startup file inherited by the constrained process;
- logs, task answers, reports, or committed files.

The file path itself is not the cryptographic secret; the file contents are. The boundary still fails if the constrained process can read that file.

Callimachus resolves a signing key in this order:

1. the file named by `CITATION_VERIFIER_SIGNING_KEY_FILE`;
2. the inline value in `CITATION_VERIFIER_SIGNING_KEY`.

The file form is preferred. Without either key, Callimachus emits a plain SHA-256 content seal. That detects accidental modification, but it does not prove approval by an external authority and cannot satisfy a gate that requires HMAC.

For the full hook and CI threat model, see [`DEPLOYMENT.md`](../../DEPLOYMENT.md), [Agent guide](../deployment/agent-guide.md), and [Administrator guide](../deployment/administrator-guide.md).

## Create an LLM provider API key

The general workflow is the same for each credentialed HTTP backend:

1. Open the provider's official console and sign in.
2. Create a project or workspace if the provider requires one.
3. Add billing or quota limits when required by that service.
4. Create a narrowly scoped API key and copy it once.
5. Put it in the matching variable in the local `.env` or a secret store.
6. Select the matching backend and a model identifier.

Provider consoles and commercial terms can change. The links below point to official provider surfaces rather than third-party tutorials.

| Backend | Key variable | Official key surface | Model variable |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | [OpenAI API keys](https://platform.openai.com/api-keys) | `CITATION_VERIFIER_MODEL` |
| `anthropic` | `ANTHROPIC_API_KEY` | [Claude Console API keys](https://platform.claude.com/settings/keys) | `CITATION_VERIFIER_MODEL` |
| `gemini` | `GEMINI_API_KEY` | [Google AI Studio API keys](https://aistudio.google.com/app/apikey) | `GEMINI_MODEL`, then `CITATION_VERIFIER_MODEL` |
| `openrouter` | `OPENROUTER_API_KEY` | [OpenRouter keys](https://openrouter.ai/settings/keys) | `CITATION_VERIFIER_MODEL` |
| `mistral` | `MISTRAL_API_KEY` | [Mistral API keys](https://console.mistral.ai/api-keys) | `MISTRAL_MODEL`, then `CITATION_VERIFIER_MODEL` |
| `glm` | `ZHIPUAI_API_KEY` | [Z.AI API keys](https://z.ai/manage-apikey/apikey-list) | `ZHIPUAI_MODEL`, then `CITATION_VERIFIER_MODEL` |
| `opencode` | `OPENCODE_API_KEY` | [OpenCode authentication](https://opencode.ai/auth) | `OPENCODE_MODEL`, then `CITATION_VERIFIER_MODEL` |
| `ollama` cloud | `OLLAMA_API_KEY` | [Ollama keys](https://ollama.com/settings/keys) | `CITATION_VERIFIER_MODEL` |
| `openai_compatible` | `OPENAI_API_KEY` | The console operated by the selected compatible provider | `OPENAI_MODEL`, then `CITATION_VERIFIER_MODEL` |

### Minimal HTTP-backend example

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=openai
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
CITATION_VERIFIER_MODEL=<provider-model-id>
OPENAI_API_KEY=<secret>
```

Only enable providers whose credentials and models are configured. Backend and credential lists are ordered, unique CSV values; a single value is the simplest configuration.

### OpenAI-compatible endpoints

`openai_compatible` reuses these variables:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=openai_compatible
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
OPENAI_BASE_URL=https://provider.example/v1
OPENAI_API_KEY=<key-issued-by-that-provider>
OPENAI_MODEL=<provider-model-id>
```

The key comes from the operator of `OPENAI_BASE_URL`; it is not necessarily an OpenAI key. An official OpenAI request should use the `openai` backend instead.

### Anthropic token fallback limitation

The Anthropic HTTP transport can fall back from `ANTHROPIC_API_KEY` to `ANTHROPIC_AUTH_TOKEN`. The current declarative Verify policy, however, identifies Anthropic credentials through `ANTHROPIC_API_KEY` and rejects a token-only configuration before dispatch. For ordinary Verify runs, configure `ANTHROPIC_API_KEY`; treat `ANTHROPIC_AUTH_TOKEN` as a transport or compatible-endpoint fallback, not a standalone replacement.

### Local Ollama limitation

The Ollama transport can address a local unauthenticated server through `OLLAMA_HOST`. The current declarative Verify policy still requires a non-empty `OLLAMA_API_KEY` lane whenever `ollama` is selected. Therefore, setting only `OLLAMA_HOST` is not sufficient for a Verify run. For a local server that ignores authentication, use a dedicated local-only placeholder rather than reusing a real cloud key:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=ollama
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=off
CITATION_VERIFIER_MODEL=<local-model-id>
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_API_KEY=local-only
```

This is a current policy constraint, not evidence that the local Ollama server requires authentication.

### Run a local FreeToken server without an API key

FreeToken is external software, not a Callimachus Python dependency. Follow the
official [FreeToken installation guide](https://github.com/FlashML-org/FreeToken/blob/main/docs/install.md),
then start its API server with a local model path or Hugging Face repository ID:

```bash
ft serve --model <path-or-hf-id>
```

The default server root is `http://127.0.0.1:1919`. Once the server reports it
is ready, inspect the model ID that it exposes:

```bash
curl http://127.0.0.1:1919/v1/models
```

Use that returned ID in the Callimachus configuration:

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=freetoken
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
FREETOKEN_HOST=http://127.0.0.1:1919
FREETOKEN_MODEL=<id-returned-by-v1-models>
```

`FREETOKEN_HOST` is the server root, without `/v1`; leaving it empty selects
the default above. `FREETOKEN_MODEL` falls back to `CITATION_VERIFIER_MODEL`,
but one of them must name a model explicitly. Callimachus does not install or
start FreeToken, does not query `/v1/models` to choose a model, and does not
send an API-key authorization header. If the server, endpoint, or configured
model is unavailable, Verify records the failure and does not fall back to an
unselected backend. See FreeToken's official [quick start](https://github.com/FlashML-org/FreeToken/blob/main/docs/quickstart.md)
and [`ft serve` CLI reference](https://github.com/FlashML-org/FreeToken/blob/main/docs/cli.md)
for server options.

## Use an existing CLI login instead of creating an API key

The credentialless CLI backends are `claude_cli`, `codex_cli`, and
`gemini_cli`. Install the corresponding executable, complete its normal login
flow, and ensure it is available on `PATH` to the same account that runs
Callimachus.

```dotenv
CITATION_VERIFIER_VERIFY_BACKENDS=codex_cli
CITATION_VERIFIER_VERIFY_JURY2_LEVEL=medium
CITATION_VERIFIER_MODEL=<model-id-accepted-by-the-installed-cli>
```

No provider key belongs in `.env` for these backends. The current Verify policy still requires a model value, even though the underlying CLI transport can sometimes choose its own default.

## Create optional resolver and content keys

These credentials improve a specific resolver or retrieval path. They do not replace the required Verify backend configuration.

| Variable | How to obtain it | What it enables |
|---|---|---|
| `GOOGLE_BOOKS_API_KEY` | Enable the Books API in a Google Cloud project, then use **APIs & Services → Credentials → Create credentials → API key**. See the official [Google Books API key instructions](https://developers.google.com/books/docs/v1/using#acquiring_and_using_an_api_key). | Identified Google Books requests and the key-gated preview-snippet tier. |
| `CORE_API_KEY` | Register an email address on the official [CORE API page](https://core.ac.uk/services/api). | CORE open-access discovery and API full-text retrieval. |
| `ELSEVIER_API_KEY` | Register and request a key in the [Elsevier Developer Portal](https://dev.elsevier.com/apikey/create). | Elsevier Article API retrieval, subject to account and institutional entitlements. |
| `NCBI_API_KEY` | Sign in to an [NCBI account](https://www.ncbi.nlm.nih.gov/account/), then create or view the key under account settings. `ENTREZ_API_KEY` remains a supported alias. | Adds the key only to NCBI E-utilities requests and raises the configured shared rate from 3/s to 10/s. |
| `OPENALEX_API_KEY` | Create a key in the OpenAlex account/API settings used by your deployment. | Authenticated OpenAlex Resolve and Fetch requests. |
| `SEMANTIC_SCHOLAR_API_KEY` | Submit the official [Semantic Scholar API-key request](https://www.semanticscholar.org/product/api#api-key-form); the key is delivered by email. | Authenticated Semantic Scholar requests and service-specific rate limits. |
| `LENS_API_KEY` | Sign in to Lens, request Scholarly API access, and manage the token from the [Lens API subscription page](https://www.lens.org/lens/user/subscriptions). | Lens scholarly title-search fallback. |
| `TDM_API_TOKEN` | Use a Wiley Online Library account, review the official [Wiley TDM resources and token request](https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining), and store the UUID token it provides. IP-based entitlement still applies. | DOI-targeted Wiley TDM PDF retrieval. The token is added as a request header only at send time; the PDF still passes the normal extraction, identity, materiality, and document-relation gates. No extra package is required. |
| `SPRINGER_NATURE_META_API_KEY` | Request the Meta API key through the official [Springer Nature developer portal](https://dev.springernature.com/). | DOI-exact Meta API enrichment for metadata and abstracts. It cannot replace the selected Resolve identity. No extra package is required. |
| `SPRINGER_NATURE_OPEN_ACCESS_API_KEY` | Request the Open Access API key through the official [Springer Nature developer portal](https://dev.springernature.com/). | DOI-exact Open Access JATS retrieval. Only a matching record with a substantive article body enters the normal full-text gates. No extra package is required. |
| `COURTLISTENER_API_TOKEN` | Create a CourtListener account and copy or reset the token on [your API-token profile](https://www.courtlistener.com/profile/api-token/). | CourtListener US case-law resolution. |
| `TAVILY_API_KEY` | Create a key in the Tavily account/API settings used by your deployment. | Tavily Search requests when that deterministic web-search backend is selected. |
| `MOJEEK_API_KEY` | Create a key in the Mojeek Search API account used by your deployment. | Mojeek Search requests when that deterministic web-search backend is selected. |

Absence is explicit: a key-gated provider is skipped or degraded according to its adapter. Callimachus must not fabricate a successful resolution merely because a credential is missing.

For new current-schema runs, the HTML report records only whether each
recognized variable name was present at startup and aggregates outcomes for
requests to which that credential was actually attached. Values, request
headers, and credential-bearing query strings are never persisted in this
telemetry. A 401/403-only warning is diagnostic: rotate or verify the key, but
also check provider authorization and content entitlement before concluding
that the credential has expired.

Publisher selection is deterministic and data-driven. DOI prefixes and official host suffixes are maintained in `core/resolve/providers/publisher_routes.json`; no adapter performs a title-only publisher search. A catalog match only chooses which API may be called. The response must independently satisfy the adapter's DOI/body contract and the normal downstream validation.

## Store provider keys safely

For a local single-user checkout:

```bash
cp .env.example .env
chmod 600 .env
```

Edit only `.env`, which is git-ignored. Never put a real secret in `.env.example`, documentation, a command transcript, or a committed fixture. Process-environment values take precedence over dotenv values, so remove or rotate a stale exported key when changing `.env` appears to have no effect.

In CI, use the platform's encrypted secret store and inject only the credentials required by that job. Restrict keys by API, project, spending limit, IP, or referrer where the provider supports those controls. Rotate a key immediately if it appears in Git history, logs, screenshots, task answers, or a report.

Provider keys and the signing key have different exposure rules:

- an HTTP provider key must be readable by the process making the LLM or resolver request;
- the signing key must not be readable by the constrained process whose report is being approved.

For every configuration label associated with these keys, see the [Environment reference](11-environment-reference.md).

---

Previous: [Troubleshooting](09-troubleshooting.md) · Next: [Environment reference](11-environment-reference.md).
