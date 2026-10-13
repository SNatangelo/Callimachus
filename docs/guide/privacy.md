# Privacy and AI in Callimachus

Updated: 3 October 2026. Maintainer: Stefano Natangelo.

## What happens when you use the app

Callimachus processes documents you choose to check citations. Extraction and
bundled OCR run on your computer. Starting or resuming an analysis authorises
the configured online operations described below. Console commands can also
perform online operations; read this information before running them.

- Resolution and retrieval send bibliographic queries, titles, author names,
  identifiers and request metadata to scholarly registries, repositories and
  publishers. They receive your IP address; a configured contact email may be
  sent for API identification. Configured service credentials authenticate
  requests to their respective services. Key checks can contact those services
  when an analysis starts, including a references-only analysis.
- Semantic Verify sends claims and source passages in prompts to the models and
  backends you select. These can include personal information present in your
  manuscript or sources. The backend can be a remote API, an authenticated
  external CLI, or a server you configure. Local LLMs are supported: when the
  selected model and inference server run entirely on your computer without
  forwarding requests, semantic prompts are not sent to an external model service.
  A server on your computer may itself forward requests. A credentialless backend
  is not a promise of local processing. Bibliographic searches and retrieval can
  still contact online services when semantic inference is local.
- Guided Fetch opens external websites in a browser you provide. Those sites
  have their own privacy policies, cookies and authentication. Browser capture
  retains the source material you choose to supply.

Provider retention, international transfers, model training, costs and account
requirements depend on the service and your agreement with it. Review its
privacy policy and endpoint before enabling it. Do not submit confidential,
personal or sensitive material without authority and the necessary consent.
Callimachus does not sell your documents or automatically send them to the
maintainer. No usage analytics or advertising service is built into the app.

## Local storage and security

Your configuration, acquired sources, OCR text, prompts, model responses,
decisions, reports and Console logs can remain in your local data directories.
Native installations normally use `%LOCALAPPDATA%/Callimachus` on Windows and
`~/.local/share/Callimachus` on Linux. MSIX may virtualise Windows application
data; custom directories can override these paths. Resolver caches and invalid
credential fingerprints may also be stored separately. See Chapters 07, 10 and 12.

The `.env` file stores configured API keys in plain text. Masking them in the GUI
does not encrypt them on disk. Run artifacts and exports are not encrypted by
Callimachus. Protect your account, files and backups with operating-system
access controls and disk encryption. Ordinary service requests use HTTPS;
custom endpoints and external tools follow their own configuration. Do not
configure an untrusted or unencrypted remote endpoint for sensitive data.

## Your controls

Choose providers and credentials in Settings and select the juries in Analysis.
To avoid semantic-model transmissions, select no LLM: the new run stops after
Resolve. This still uses online bibliographic services. To withdraw permission
for an active analysis, press Stop; requests already sent cannot be recalled.
Do not start or resume another online operation until you wish to authorise it.
Remove credentials or change providers in Settings before future operations.
An existing paused run may retain its earlier configuration; check before resuming.

Inspect History, the run directory and Console to access your stored information.
Delete unwanted runs and reusable cache entries through the app; delete remaining
local configuration, logs, external caches and backups yourself when no longer
needed. There is no automatic retention deadline. Ask each external provider
directly about deletion of material it received. Uninstall behavior depends on
the package and does not erase provider records or exported files.

Public export removes unnecessary source text, but retains claims, evidence,
decisions and bibliographic information. Review the resulting HTML for personal
information and publication rights before sharing it. Export does not publish it.

## Generated content and reporting

LLM verdicts and explanations are generated dynamically and may be mistaken or
inappropriate. Deterministic grounding checks constrain evidence; they do not
guarantee that generated explanations are correct or safe. This is a research
tool, not medical, legal or other professional advice.

Use **Info → Report AI content** or
[contact the maintainer through GitHub](https://github.com/SNatangelo/Callimachus/issues/new)
to report inappropriate generated content, privacy concerns or application issues.
This opens a form in your browser; nothing is submitted automatically. GitHub
issues are public: include only a minimal, sanitised description you choose to
share, never API keys, private manuscripts or complete run archives. GitHub's
privacy policy applies to submissions. The maintainer reviews reported concerns
and can correct or disable affected generation behavior; existing local reports
are not remotely changed. A GitHub account is needed to submit an issue.
