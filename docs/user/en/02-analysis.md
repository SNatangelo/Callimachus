# 02 — Analysis

The Analysis screen is where you choose the manuscript, set the evidence policy, and start or resume work.

## Choose a manuscript and mode

Drop a manuscript into the paper area or click it to browse. A run is for one manuscript; choose **New analysis** when you want to clear the current Analysis view and select a different one. Clearing the view does not remove the saved run from History.

Choose the verification mode that matches the evidence you want:

- **Standard** is the usual setting. It seeks full text and may use an abstract when full text is unavailable; the report preserves that limited scope.
- **Maximus** requires full text for semantic verification. If full text is missing, the citation remains unverified rather than being settled from an abstract.
- **Abstract** does not seek full text. It treats the title and authentic abstract as the final evidence scope.
- **Standard web** is a legacy name for Standard. General third-party web pages are not citation evidence.

Choose **Options** to open the Analysis options drawer. It contains the verification mode, Jury 2 severity, and the available workflow choices. The drawer can be closed without changing a running pipeline.

The **Low**, **Medium**, and **High** Jury 2 severity setting controls how the second judge handles a rejection or an uncertain response; stricter settings can leave more results uncertain. It does not override source-identity or evidence checks.

## Select the model lanes

Select one or more models for **Jury 1**. Jury 2 is optional, and its model assignments are independent of Jury 1. Selecting or saving a model for one lane does not assign it to the other. The chooser shows each lane's selected models and readiness. Configure providers and models in [Settings and models](03-settings.md).

If no model is selected, a new run stops after resolving the bibliography. It does not start Fetch, Guided Fetch, or Verify. This can be useful for an initial bibliography check, but it is not a full citation verification.

## Prepare institutional access

Before a new analysis, the optional access window can open several publisher pages at once. Select the publishers you need: their tabs share one saved browser session, so sign-ins to different sites can coexist. Choose your university and complete sign-in directly on the publishers' websites, then choose **Finished**. Opening a tab does not itself confirm access to its articles.

**Saved browser session** identifies the browser data that keeps these sign-ins. Normally leave it unchanged and use the same session across publishers. The window does not ask for an institution name or library address.

Fetch reuses that session on supported recovery paths, including publisher pages reached through DOI redirects, and still checks the identity and content of acquired documents. Access depends on your institution's subscriptions. You can continue without preparing access; a later sign-in request does not interrupt ordinary Fetch.

## Start, stop, or skip optional reviews

Press **Start analysis** to begin. The Analysis home screen checks only whether run files exist; selecting **Resume latest** performs the lookup for the most recently updated readable run. During an active run, the stop control requests a graceful stop. **New analysis** changes to **Stop and clear** while work is active; after the process exits, it clears the Analysis view while the saved run stays in History.

Two options can bypass human pauses:

- **Skip manual review of citations and bibliography** skips ambiguous citation/reference matching, footnote-source review, and reference-identity review after Parse.
- **Skip assisted Fetch and OCR** keeps automatic retrieval and verification, but waives pending manual Fetch and OCR work. A source needing an identity decision stays unverified; a PDF without extracted text cannot be assessed from its full text.

Leave these options off when you want to resolve the items yourself. See [Reviews and sources](04-reviews-and-sources.md) for the consequences and controls.
