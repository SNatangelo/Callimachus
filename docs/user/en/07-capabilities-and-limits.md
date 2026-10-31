# 07 — Capabilities and limits

Callimachus helps trace citations to bibliographic records and source text, then assess how that text relates to cited claims. It preserves the material and decisions used for each run so you can inspect how the result was reached.

## What the app can check

The app can identify and retrieve scholarly sources, extract text from supported files, use OCR on scanned PDFs, and check source passages against cited claims. It can also flag citation-style issues and sources that may be missing, mismatched, or retracted. Parse and identity reviews let you resolve some ambiguity while keeping your decision in the run record.

The outcome depends on what source material was available. Full text, an authentic abstract, and a book preview are distinct evidence scopes. Abstract-only or preview-based findings do not mean full-text verification. A citation may remain unresolved or not decidable when the evidence is incomplete.

## What the app cannot promise

A completed run does not prove that every citation is correct, that every claim is supported, or that generated explanations are error-free. The app checks selected sources under the chosen policy; it cannot infer what is in a missing or unreadable document. OCR can recover visible text but not layout or meaning lost in a scan. General third-party web pages are not treated as citation evidence.

Callimachus does not bypass paywalls, account sign-in, CAPTCHAs, or other access controls. Guided Fetch can open a page for you to inspect or accept a lawful copy you provide. It cannot grant access that your account or institution does not have.

## Online services, local models, and cost

Manuscript text extraction and bundled OCR run on your computer. Bibliographic searches and source retrieval can still contact online registries, repositories, or publishers. When Verify uses a remote model, claims and source passages may be sent to that provider. A local model keeps prompts on your computer only when its inference service also runs locally and does not forward requests.

The app therefore is not guaranteed to work offline, even with a local model. Provider charges, rate limits, account requirements, privacy, and retention depend on the services you configure. Review [Privacy and AI](../../legal/en/privacy.md) before sending sensitive material.
