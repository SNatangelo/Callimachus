# Microsoft Store preparation

This contract concerns the separate Windows MSIX channel. It does not certify
the app. NSIS and Linux remain separate channels. Do not compile until authorised.

The preserved native recipes and their build plan are in
[the Store build plan](../../packaging/store/native-recipes/build-plan.json).
Build an MSIX with [`packaging/build_msix.py`](../../packaging/build_msix.py)
after the required package identity and tested Windows distribution are ready.

## Before submission

- Reserve Callimachus in Partner Center and obtain the real package identity,
  publisher distinguished name, display name and four-part package version.
  Never submit an example identity.
- Clarify publisher account eligibility with Microsoft. Effective policy 7.19,
  section 10.8.3 includes API secret keys in financial information and requires
  a Company account when primary functionality requires them. Callimachus also
  supports external CLI and credentialless backends; account eligibility has
  not been confirmed for this listing.
- Publish reviewed [English privacy information](../guide/privacy.md) and its
  [Italian version](../guide/privacy-it.md) at stable public HTTPS URLs. Verify
  access without signing in and enter the live privacy URL in Partner Center.
  Bundled local documents alone do not provide a public submission URL.
- Review credential storage and endpoint security for this distribution.
  `.env` keys are currently plaintext. Do not advertise encrypted credentials
  or unrestricted suitability for confidential manuscripts. Resolve any
  certification requirement for storage security before submission.
- Provide AGPL-3.0-only terms, complete corresponding sources for the shipped
  version and third-party notices. Store terms must not restrict those licences.
  Complete the age/content questionnaire and declare live generative AI in
  Partner Center. Review reports of inappropriate generated content and correct
  or disable affected generation behavior as needed.

## Package contract

Use `packaging/build_msix.py` on a tested Windows x64 onedir distribution.
`--help` lists the mandatory inputs. It does not invoke PyInstaller or NSIS.
It requires the real Store identity and a selected x64
`Microsoft.VCLibs.140.00.UWPDesktop` framework package. The builder checks the
framework's runtime DLL versions against the build's externalised DLLs and uses
the framework's actual manifest version for the dependency. Framework package
version and runtime DLL version are different version spaces.

Store deploys the runtime framework. The MSIX must not include the NSIS
PowerShell prerequisite downloader or request administrator elevation. It uses
the full-trust desktop capability needed by the Win32 app. Code, documentation
and models are immutable installed resources; data and caches belong elsewhere.

MakeAppx from the Windows SDK is needed when compilation is authorised. Store
signs accepted MSIX submissions. A local installation trial needs a signed test
package and a trusted test certificate; never ship development signing keys.
Keep build metadata, notices and source revision aligned with the payload.
No new GitHub workflow is needed to use this builder.

## Listing draft — English

**External services and AI:** Internet is needed for online citation searches and
retrieval. Semantic verification requires a backend you configure. Provider
accounts, API keys, subscriptions or usage charges may be required and are not
included. AI verdicts and explanations are generated dynamically: inspect
evidence and audit records before relying on them.

Callimachus checks source identities, citation style and support for manuscript
claims. It includes local PDF extraction and OCR, assisted source retrieval,
manual review, audit records and HTML reports with a public export option.
Completing a run does not certify every citation as correct. Reports can contain
personal information and copyrighted passages: review them before publication.

Python, the GUI and OCR models are included. Browser capture in Guided Fetch
requires Google Chrome installed separately. Optional Claude, Codex and Gemini
CLI backends require their respective external installation and login; the app
does not install them. A bibliography-only workflow works without selecting a
semantic LLM but still uses online services.

Privacy information and AI reporting are available in Info. Reporting opens a
public GitHub form and needs a GitHub account; no run data is attached automatically.
This research tool does not provide professional advice.

## Listing draft — Italiano

**Servizi esterni e IA:** serve Internet per le ricerche bibliografiche e il recupero
online. La verifica semantica richiede un backend configurato dall'utente. Il
provider può richiedere account, chiavi API, abbonamenti o costi a consumo,
non inclusi. Verdetti e spiegazioni sono generati dinamicamente dall'IA:
controlla prove e registri prima di usarli.

Callimachus verifica identità delle fonti, stile delle citazioni e supporto alle
affermazioni. Include estrazione PDF e OCR locali, recupero assistito, revisione
manuale, tracciabilità e report HTML esportabili per la pubblicazione.
Completare una run non certifica la correttezza delle citazioni. I report possono
contenere dati personali e passaggi protetti: controllali prima di pubblicarli.

Python, GUI e modelli OCR sono inclusi. La cattura nel fetch assistito richiede
Google Chrome installato separatamente. I backend CLI opzionali di Claude,
Codex e Gemini richiedono la relativa installazione esterna e il login;
Callimachus non li installa. La modalità bibliografica funziona senza LLM
selezionate ma continua a utilizzare servizi online.

In Info sono disponibili privacy e segnalazioni IA. Si apre un modulo pubblico
su GitHub e serve un account; nessun dato della run viene allegato automaticamente.
È uno strumento di ricerca, non di consulenza professionale.

## Private certification instructions and test gates

The following installed-package tests remain pending until an MSIX build is
authorised. Provide a consented synthetic manuscript, a usable test endpoint /
account and sufficient provider allowance for Verify in private certification
notes. Never include credentials in the package or public listing. A reviewer
must have a way to exercise the full functionality.

- Clean Windows x64 machine, standard user, no Python, Git or VC runtime:
  install the signed package and its framework. Launch from Start; check splash,
  English/Italian, light/dark themes and readable controls.
- Cold Resolve: persistent user caches, no writes inside WindowsApps. Restart
  and check Settings, History and cached source reuse.
- Actual image-only PDF OCR with networking disabled after installation.
  Checking that ONNX files exist does not prove inference works.
- New analysis, manual review, deferred review, assisted fetch, file supply,
  identity attestation, OCR queue, Stop and Resume.
- Verify with the supplied backend, inspect verdicts/provenance, generate and
  export HTML, and open it in a separate browser. MSIX AppData virtualisation
  can affect external access to report files.
- Console paging/export, file dialogs, Guided Fetch capture, missing Chrome /
  CLI messages, and optional external CLI login with packaged child execution.
- Update using the same identity/publisher and a higher version; check retained
  configuration/runs and uninstall behavior. Run Windows App Certification Kit
  and resolve its findings before submission.
- Live public privacy, reporting and corresponding-source links.

## Primary references

- [Effective Store policy 7.19](https://learn.microsoft.com/en-us/windows/apps/publish/store-policy-archive/store-policy-7-19), checked 2 October 2026.
- [Desktop C++ frameworks](https://learn.microsoft.com/en-us/troubleshoot/developer/visualstudio/cpp/libraries/c-runtime-packages-desktop-bridge).
- [Packaged desktop runtime](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes).
- [Capabilities](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/app-capability-declarations).
- [Win32 Store distribution](https://learn.microsoft.com/en-us/windows/apps/distribute-through-store/how-to-distribute-your-win32-app-through-microsoft-store).

Recheck policy at submission: version 7.20 takes effect on 22 October 2026.
Preparation is not a guarantee of certification or compliance with all privacy
laws in every jurisdiction.
