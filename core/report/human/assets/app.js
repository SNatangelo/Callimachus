// core/report/human/assets/app.js
// Copyright (C) 2026 Stefano Natangelo
// SPDX-License-Identifier: AGPL-3.0-only
(() => {
  "use strict";
  const state = JSON.parse(document.getElementById("cv-data").textContent);
  const app = document.getElementById("cv-app");
  const viewNames = ["overview", "claims", "sources", "pairs", "method", "diagnostics", "provenance"];
  const readRoute = () => {
    const fragment = location.hash.slice(1);
    const separator = fragment.indexOf("?");
    return {
      view: (separator >= 0 ? fragment.slice(0, separator) : fragment) || "overview",
      params: new URLSearchParams(separator >= 0 ? fragment.slice(separator + 1) : ""),
    };
  };
  let active = readRoute().view;
  let localeId = state.initial_locale;
  let themeId = state.initial_theme;
  let opener;

  const el = (tag, attrs = {}, text) => {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== false) node.setAttribute(key, String(value));
    });
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const add = (node, ...items) => {
    items.flat().filter(Boolean).forEach(item => node.append(item));
    return node;
  };
  const textNode = value => el("bdi", {dir: "auto"}, value || "");
  const loc = () => state.catalog.locales[localeId];
  const t = (key, values = {}) => {
    const value = loc().messages[key];
    if (typeof value !== "string") throw new Error("invalid localized message");
    return value.replace(/\{([a-z][a-z0-9_]*)\}/g, (_, name) => String(values[name] ?? ""));
  };
  const code = value => String(value || "").toLowerCase().replace(/[^a-z0-9_.-]+/g, "-");
  const status = value => {
    if (value === null || value === undefined || value === "") return t("value.not_recorded");
    const key = `status.${code(value)}`;
    return typeof loc().messages[key] === "string"
      ? t(key)
      : t("status.unknown", {value: String(value).replaceAll("_", " ")});
  };
  const classFor = value => `cv-${code(value || "neutral")}`;
  const badge = value => el("span", {class: `cv-badge ${classFor(value)}`}, status(value));
  const card = (...items) => add(el("section", {class: "cv-card"}), items);
  const section = (titleKey, ...items) => add(el("section", {class: "cv-section"}), el("h2", {class: "cv-section-title"}, t(titleKey)), items);
  const countedSection = (titleKey, count, ...items) => add(el("section", {class: "cv-section"}), add(el("div", {class: "cv-section-heading"}), el("h2", {class: "cv-section-title"}, t(titleKey)), el("span", {class: "cv-section-count"}, t("attention.items_count", {count}))), items);
  const quote = value => el("blockquote", {class: "cv-quote"}, value || t("value.not_recorded"));
  const query = () => readRoute().params;
  const routeHash = (view, params) => {
    const serialized = params.toString();
    return `#${view}${serialized ? `?${serialized}` : ""}`;
  };
  const setRoute = (view, params) => {
    const nextHash = routeHash(view, params);
    active = view;
    if (location.hash === nextHash) render(); else location.hash = nextHash;
  };
  const setQuery = (name, value) => {
    const next = query();
    if (value) next.set(name, value); else next.delete(name);
    setRoute(active, next);
  };
  const navigate = (view, params = {}) => {
    const next = new URLSearchParams();
    Object.entries(params).forEach(([name, value]) => {
      if (value !== null && value !== undefined && value !== "") next.set(name, String(value));
    });
    setRoute(view, next);
    requestAnimationFrame(() => window.scrollTo({top: 0}));
  };
  const outcome = pair => (pair.verification || {}).semantic_outcome || ((pair.verification || {}).result_class === "unresolved" ? "unresolved" : "no_outcome");
  const pairMap = key => {
    const map = new Map();
    (state.projection.pairs || []).forEach(pair => {
      if (!map.has(pair[key])) map.set(pair[key], []);
      map.get(pair[key]).push(pair);
    });
    return map;
  };
  const byClaim = pairMap("claim_id");
  const bySource = pairMap("ref_id");
  const claimById = new Map((state.projection.claims || []).map(item => [item.claim.id, item]));
  const sourceById = new Map((state.projection.sources || []).map(item => [item.reference.id, item]));
  const worst = pairs => {
    const ranks = {contradicts: 0, off_topic: 1, partial: 2, unresolved: 3, no_outcome: 4, related: 5, supports: 6};
    return pairs.reduce((best, pair) => !best || (ranks[outcome(pair)] ?? 4) < (ranks[best] ?? 4) ? outcome(pair) : best, "");
  };
  const modelText = step => step && [step.provider_id, step.model_id].filter(Boolean).join(" / ");
  const modelsFor = pairs => [...new Set(pairs.flatMap(pair => {
    const provenance = pair.verdict_provenance || {};
    return [provenance.proposal, ...(provenance.jury2_reviews || [])].map(modelText).filter(Boolean);
  }))].join("; ") || t("value.not_recorded");
  const rejectedJury2Count = pair => (pair.rejected_jury2_attempts || []).length;
  const detailList = entries => {
    const list = el("dl", {class: "cv-detail-list"});
    entries.filter(([, value]) => value !== null && value !== undefined && value !== "" && (!Array.isArray(value) || value.length)).forEach(([key, value]) => {
      const dd = el("dd");
      if (Array.isArray(value)) {
        const ul = el("ul", {class: "cv-detail-items"});
        value.filter(Boolean).forEach(item => ul.append(add(el("li"), item instanceof Node ? item : textNode(item))));
        dd.append(ul);
      } else dd.append(value instanceof Node ? value : textNode(value));
      list.append(el("dt", {}, t(key)), dd);
    });
    return list;
  };
  const detailCard = (titleKey, entries) => card(el("h3", {class: "cv-section-title"}, t(titleKey)), detailList(entries));
  const audit = record => {
    const details = el("details", {class: "cv-audit"});
    details.append(el("summary", {}, t("heading.audit")), el("pre", {}, JSON.stringify(record, null, 2)));
    return details;
  };
  const timeline = (entries, formatter, emptyText = t("value.not_recorded")) => {
    if (!entries.length) return textNode(emptyText);
    const list = el("div", {class: "cv-timeline"});
    entries.forEach(entry => {
      const content = formatter(entry);
      list.append(add(el("div", {class: "cv-timeline-item"}), el("strong", {}, content.title), el("span", {}, content.detail)));
    });
    return list;
  };
  const backdrop = el("div", {class: "cv-backdrop", hidden: ""});
  const drawer = el("aside", {class: "cv-drawer", hidden: "", role: "dialog", "aria-modal": "true", "aria-labelledby": "cv-drawer-title"});
  const close = () => {
    drawer.hidden = true; backdrop.hidden = true; drawer.replaceChildren();
    document.body.style.overflow = "";
    if (opener) opener.focus();
  };
  backdrop.addEventListener("click", close);
  const startDrawer = (title, source) => {
    opener = source; drawer.hidden = false; backdrop.hidden = false; drawer.replaceChildren();
    document.body.style.overflow = "hidden";
    const button = el("button", {type: "button", "aria-label": t("a11y.close_details")}, t("action.close"));
    button.addEventListener("click", close);
    drawer.append(add(el("div", {class: "cv-drawer-header"}), el("h2", {id: "cv-drawer-title"}, title), button));
    return button;
  };
  document.addEventListener("keydown", event => { if (event.key === "Escape" && !drawer.hidden) close(); });
  const excerpt = value => {
    const text = String(value || "").trim();
    return text.length > 160 ? `${text.slice(0, 157)}…` : text;
  };
  const claimText = item => item && (item.focus_text || (item.claim || {}).sentence) || t("value.not_recorded");
  const sourceCitation = item => item && (item.display_citation || (item.reference || {}).title || (item.reference || {}).raw_entry) || t("value.not_recorded");
  const sourceTitle = item => item && ((item.reference || {}).title || (item.reference || {}).raw_entry || item.display_citation) || t("value.not_recorded");
  const sourceLabel = item => item ? `${item.display_id || ""} ${sourceCitation(item)}`.trim() : t("value.not_recorded");
  const evidenceText = evidence => typeof evidence === "string" ? evidence : (evidence && [evidence.quote, evidence.text, evidence.snippet, evidence.excerpt, evidence.content].find(Boolean)) || "";
  const pairHeading = pair => `${excerpt(claimText(claimById.get(pair.claim_id)))} · ${sourceCitation(sourceById.get(pair.ref_id))}`;
  const explanationParts = value => {
    const text = String(value || "").trim();
    if (!text) return {};
    const matches = [...text.matchAll(/\b(Unsupported|Supported|Reason):\s*/gi)];
    if (!matches.length || text.slice(0, matches[0].index).trim()) return {explanation: text};
    const parts = {};
    matches.forEach((match, index) => {
      const key = match[1].toLowerCase();
      const next = matches[index + 1];
      const content = text.slice(match.index + match[0].length, next ? next.index : text.length).trim();
      if (content) parts[key] = parts[key] ? `${parts[key]} ${content}` : content;
    });
    return parts;
  };
  const reasonBlock = (labelKey, value, tone) => value ? add(
    el("section", {class: `cv-reason-block ${classFor(tone)}`}),
    el("h4", {}, t(labelKey)),
    el("p", {}, value),
  ) : null;
  const attributedVerdict = (value, attribution) => add(
    el("span", {class: "cv-verdict-attribution"}),
    badge(value),
    (typeof attribution === "string" ? attribution : modelText(attribution))
      ? el("span", {}, typeof attribution === "string" ? attribution : modelText(attribution))
      : null,
  );
  const evidenceCard = (pair, decision, verification) => {
    const result = outcome(pair);
    const parsed = explanationParts(decision.explanation);
    const supported = decision.supported_content || parsed.supported;
    const unsupported = decision.incompatible_proposition || parsed.unsupported;
    const reason = decision.reason || parsed.reason;
    const explanation = parsed.explanation;
    const providerConfidence = Number.isFinite(decision.provider_confidence)
      ? `${(decision.provider_confidence * 100).toFixed(1)}%`
      : null;
    const unsupportedTone = result === "contradicts" ? "contradicts" : result === "off_topic" ? "off_topic" : "partial";
    const blocks = [
      reasonBlock("field.supported_content", supported, "supports"),
      reasonBlock("field.incompatible_proposition", unsupported, unsupportedTone),
      reasonBlock("field.reason", reason, result),
      reasonBlock("field.explanation", explanation, result),
      reasonBlock("field.provider_confidence", providerConfidence, result),
    ].filter(Boolean);
    const decisionEvidence = Array.isArray(decision.evidence) ? decision.evidence : [];
    const verificationEvidence = Array.isArray(verification.evidence) ? verification.evidence : [];
    const excerpts = (decisionEvidence.length ? decisionEvidence : verificationEvidence).map(evidenceText).filter(Boolean);
    if (!blocks.length && !excerpts.length) return null;
    const heading = add(el("div", {class: "cv-evidence-heading"}), el("h3", {class: "cv-section-title"}, t("heading.evidence")), badge(result));
    const body = add(el("div", {class: "cv-reason-grid"}), blocks);
    const excerptSection = excerpts.length ? add(
      el("section", {class: `cv-evidence-excerpts ${classFor(result)}`}),
      el("h4", {}, t("field.evidence_excerpts")),
      add(el("div", {class: "cv-evidence-quote-list"}), excerpts.map(value => quote(value))),
    ) : null;
    return add(el("section", {class: `cv-card cv-verdict-card ${classFor(result)}`}), heading, body, excerptSection);
  };

  const showPair = (pair, source) => {
    const closeButton = startDrawer(pairHeading(pair), source);
    const verification = pair.verification || {}, decision = pair.decision || {}, provenance = pair.verdict_provenance || {};
    const finalizer = provenance.finalizer || {}, rejections = pair.jury1_rejections || [];
    const rejectedJury1Attempts = pair.rejected_jury1_attempts || [];
    const guardTerminated = rejectedJury1Attempts.length > 0 || rejections.length > 0 || (pair.pair_state || {}).terminal_cause === "jury1_guard" || finalizer.terminal_cause === "jury1_guard" || finalizer.cause === "jury1_guard";
    drawer.append(detailCard("heading.pair_result", [
      ["field.semantic_outcome", status(outcome(pair))], ["field.assurance", status(verification.assurance || verification.resolution)],
      ["field.result_class", status(verification.result_class)], ["field.evidence_scope", status(verification.scope || verification.evidence_scope)],
    ]));
    const rejectedJury2Attempts = pair.rejected_jury2_attempts || [];
    if (rejectedJury2Attempts.length) {
      const rejected = el("div", {class: "cv-jury2-rejected-list"});
      rejectedJury2Attempts.forEach(item => {
        const review = item.jury2_review || {};
        rejected.append(add(
          el("section", {class: `cv-reason-block ${classFor(item.jury1_outcome)}`}),
          el("h4", {}, `${t("field.model_cycle")} ${item.candidate_cycle ?? t("value.not_recorded")}`),
          detailList([
            ["field.jury1", attributedVerdict(item.jury1_outcome, item.proposal)],
            ["field.jury2", [status(review.event_type), modelText(review)].filter(Boolean).join(" · ")],
            ["field.reason", review.reason],
          ]),
        ));
      });
      drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.jury2_rejected_attempts")), rejected));
    }
    const evidence = evidenceCard(pair, decision, verification);
    if (evidence) drawer.append(evidence);
    const requests = new Map((pair.logical_requests || []).map(request => [request.logical_request_id, request]));
    const attemptsByRequest = new Map();
    (pair.dispatch_attempts || []).forEach(attempt => {
      if (!attemptsByRequest.has(attempt.logical_request_id)) attemptsByRequest.set(attempt.logical_request_id, []);
      attemptsByRequest.get(attempt.logical_request_id).push(attempt);
    });
    if (rejectedJury1Attempts.length) {
      const rejected = el("div", {class: "cv-jury1-rejected-list"});
      rejectedJury1Attempts.forEach(item => {
        const models = [...new Set((attemptsByRequest.get(item.logical_request_id) || []).map(modelText).filter(Boolean))].join("; ");
        const proposedOutcome = item.jury1_outcome
          ? attributedVerdict(item.jury1_outcome, models)
          : t("value.not_recorded");
        rejected.append(add(
          el("section", {class: `cv-reason-block ${classFor(item.jury1_outcome || "unresolved")}`}),
          el("h4", {}, `${t("field.model_cycle")} ${item.candidate_cycle ?? t("value.not_recorded")}`),
          detailList([
            ["field.jury1_unaccepted_outcome", proposedOutcome],
            ["field.model_stage", item.stage ? status(item.stage) : t("value.not_recorded")],
            ["field.rejection_cause", item.cause || item.state_cause || t("value.not_recorded")],
          ]),
        ));
      });
      drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.rejected_proposals")), el("p", {}, t("attention.jury1_guard")), rejected));
    }
    const stages = [];
    if (provenance.proposal) {
      stages.push({label: t("field.jury1"), attribution: provenance.proposal, request: requests.get(provenance.proposal.logical_request_id), result: decision.outcome || outcome(pair)});
    }
    (provenance.jury2_reviews || []).forEach(review => stages.push({label: t("field.jury2"), attribution: review, request: requests.get(review.logical_request_id), result: review.event_type || review.answer || review.technical_result}));
    drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.model_timeline")), timeline(stages, item => {
      const request = item.request || {};
      const details = [request.stage ? status(request.stage) : "", request.candidate_cycle ? `${t("field.model_cycle")}: ${request.candidate_cycle}` : "", item.result ? status(item.result) : ""].filter(Boolean);
      return {title: `${item.label} · ${modelText(item.attribution) || t("value.not_recorded")}`, detail: details.join(" · ")};
    }, guardTerminated ? "" : t("value.not_recorded")), detailList([
      ["field.finalization", status(finalizer.status || finalizer.resolution)],
      ["field.terminal_cause", status(finalizer.terminal_cause || finalizer.cause)],
      ["field.owner", t("value.deterministic_controller")],
    ])));
    const terminalByAttempt = new Map((pair.dispatch_events || []).filter(event => event.dispatch_attempt_id && ["completed", "failed"].includes(event.event_type)).map(event => [event.dispatch_attempt_id, event]));
    const attempts = (pair.dispatch_attempts || []).map(attempt => {
      const request = requests.get(attempt.logical_request_id) || {}, terminal = terminalByAttempt.get(attempt.dispatch_attempt_id) || {}, payload = terminal.payload || {};
      return {attempt, request, terminal, detail: [request.candidate_cycle ? `${t("field.model_cycle")}: ${request.candidate_cycle}` : "", status(terminal.event_type), status(payload.technical_result)].filter(Boolean).join(" · ")};
    });
    if (attempts.length) drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.attempt_history")), timeline(attempts, item => ({title: `${status(item.request.stage)} · ${modelText(item.attempt) || t("value.not_recorded")}`, detail: item.detail}))));
    drawer.append(audit({claim_id: pair.claim_id, ref_id: pair.ref_id, pair_state: pair.pair_state, verification, decision, provenance, rejected_jury1_attempts: pair.rejected_jury1_attempts, rejected_jury2_attempts: pair.rejected_jury2_attempts, logical_requests: pair.logical_requests, jury1_rejections: pair.jury1_rejections, dispatch_attempts: pair.dispatch_attempts, dispatch_events: pair.dispatch_events}));
    closeButton.focus();
  };
  const showClaim = (item, source) => {
    const closeButton = startDrawer(excerpt(claimText(item)), source);
    const pairs = byClaim.get(item.claim.id) || [];
    drawer.append(detailCard("heading.claim_considered", [["field.claim_text", quote(claimText(item))], ["field.claim_scope", status(item.claim.claim_scope)]]));
    const parserProvenance = [];
    if (item.claim.parser_sentence_index !== undefined && item.claim.parser_sentence_index !== null) parserProvenance.push(t("value.sentence_number", {number: item.claim.parser_sentence_index}));
    if (item.claim.marker_group_index !== undefined && item.claim.marker_group_index !== null && item.claim.marker_group_count) parserProvenance.push(t("value.marker_group", {number: Number(item.claim.marker_group_index) + 1, total: item.claim.marker_group_count}));
    drawer.append(detailCard("heading.extracted_text", [["field.original_sentence", quote(item.claim.sentence)], ["field.citation_marker", item.claim.marker_raw], ["field.parser_provenance", parserProvenance.length ? parserProvenance : t("value.not_recorded")]]));
    const verdicts = el("div", {class: "cv-structured"});
    pairs.forEach(pair => {
      const sourceItem = sourceById.get(pair.ref_id);
      const button = el("button", {type: "button", class: "cv-structured-item"});
      button.append(el("h3", {}, sourceLabel(sourceItem)), el("p", {}, `${status(outcome(pair))} · ${modelsFor([pair])}`));
      button.addEventListener("click", () => showPair(pair, button)); verdicts.append(button);
    });
    drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.pair_verdicts")), verdicts));
    drawer.append(audit({claim_id: item.claim.id, parser_sentence_index: item.claim.parser_sentence_index, marker_raw: item.claim.marker_raw}));
    closeButton.focus();
  };
  const resolveText = attempt => {
    const match = attempt.metadata_match || {};
    return [status(attempt.status || attempt.outcome || attempt.result), attempt.matched_title, match.matched_year ? t("value.year_inline", {year: match.matched_year}) : "", attempt.reason].filter(Boolean).join(" · ");
  };
  const fetchText = attempt => [status(attempt.outcome), attempt.status_code ? t("value.http_status", {code: attempt.status_code}) : "", attempt.paywalled ? t("value.paywall") : "", attempt.challenge_blocked ? t("value.challenge") : "", attempt.reason].filter(Boolean).join(" · ");
  const resolverCandidate = resolve => ((resolve.evidence_profile || {}).best_candidate || {});
  const resolverMetadata = resolve => resolve.metadata_match || (resolve.evidence_profile || {}).metadata_match || resolverCandidate(resolve).metadata_match || {};
  const isVerifiableManifestEntry = entry => ["fulltext", "full_text", "abstract", "abstract_only"].includes(entry.tier)
    || (entry.tier === "web" && entry.origin === "googlebooks");
  const preferredManifestEntry = item => {
    const entries = (item.manifest_entries || []).filter(isVerifiableManifestEntry), selected = entries.find(entry => entry.selected_for_verification);
    if (selected) return selected;
    const rank = {fulltext: 0, full_text: 0, abstract: 1, abstract_only: 1, web: 2};
    return entries.reduce((best, entry) => !best || (rank[entry.tier] ?? 99) < (rank[best.tier] ?? 99) ? entry : best, null);
  };
  const bestTier = item => (preferredManifestEntry(item) || {}).tier || "";
  const sourceTextAvailability = item => {
    const tier = bestTier(item);
    if (tier) return tier;
    const resolve = (item || {}).resolve || {};
    const adjudication = ((resolve.evidence_profile || {}).bibliographic_adjudication || {});
    const concern = bibliographicConcern(item);
    if (adjudication.outcome === "refuted" || (!Object.keys(adjudication).length && concern.level === "reference_refuted")) {
      return "cited_reference_contradicted_no_text";
    }
    const identityKnown = ["identified", "identified_with_errors"].includes(adjudication.identity_status)
      || resolve.status === "resolved";
    const accessBlocked = (item.fetch_attempts || []).some(attempt => attempt && (attempt.paywalled === true || attempt.challenge_blocked === true))
      || ["closed", "paywalled", "closed_access", "paywall"].includes(resolve.oa_status);
    if (accessBlocked) return identityKnown ? "access_blocked_no_text" : "retrieval_blocked_identity_uncertain";
    if (identityKnown) return "known_work_no_text";
    const reviewCodes = new Set(bibliographicReviewLabels(item).map(label => label.code));
    if ((adjudication.identity_status === "not_identified" && adjudication.check_status === "complete" && adjudication.outcome === "not_corroborated") || reviewCodes.has("not_found_after_completed_searches")) {
      return "not_found_in_completed_searches";
    }
    return "identity_uncertain_no_text";
  };
  const sourceTextAvailabilityDetail = item => {
    const availability = sourceTextAvailability(item);
    const key = `availability.${availability}.detail`;
    return typeof loc().messages[key] === "string" ? t(key) : "";
  };
  const sourceTextAvailabilityBadge = item => badge(sourceTextAvailability(item));
  const scoreText = value => typeof value === "number" ? new Intl.NumberFormat(localeId, {maximumFractionDigits: 3}).format(value) : value;
  const comparisonPanel = (titleKey, entries, extraClass = "") => {
    const details = detailList(entries);
    return add(
      el("section", {class: `cv-comparison-panel ${extraClass}`.trim()}),
      el("h4", {}, t(titleKey)),
      details.childElementCount ? details : el("p", {class: "cv-empty-value"}, t("value.not_recorded")),
    );
  };
  const bibliographicComparisonStatus = comparison => {
    if (comparison.status === "match") return "match";
    if (comparison.status === "mismatch") return "discrepancy";
    return comparison.matched_value === null || comparison.matched_value === undefined
      ? "missing from provider" : "unverified comparison";
  };
  const bibliographicField = (label, cited, observed, fieldStatus, source) => {
    const observedValue = observed === null || observed === undefined ? "missing from provider" : observed;
    const entries = [["Cited", cited], ["Observed", observedValue], ["Status", fieldStatus]];
    if (source) entries.push(["Source", source]);
    const list = el("dl", {class: "cv-detail-list"});
    entries.forEach(([key, value]) => {
      if (value === null || value === undefined || value === "") return;
      list.append(el("dt", {}, key), el("dd", {}, value));
    });
    return add(el("section", {class: "cv-comparison-panel"}), el("h4", {}, label), list);
  };
  const bibliographicFieldComparison = (reference, resolve, candidate, match, adjudication) => {
    if (!["identified", "identified_with_errors"].includes(adjudication.identity_status)) return null;
    const refutations = adjudication.refutations || [];
    const sourceFor = field => {
      const refutation = refutations.find(item => item && item.field === field && item.source);
      return refutation && refutation.source;
    };
    const fields = [];
    for (const comparison of match.coordinate_comparisons || []) {
      if (!comparison || comparison.cited_value === null || comparison.cited_value === undefined) continue;
      fields.push(bibliographicField(
        `Coordinate: ${String(comparison.kind || "unknown").replaceAll("_", " ")}`,
        comparison.cited_value,
        comparison.matched_value,
        bibliographicComparisonStatus(comparison),
        sourceFor(comparison.kind),
      ));
    }
    const citedYear = reference.year || reference.ay_year;
    if (citedYear !== null && citedYear !== undefined && match.matched_year !== null && match.matched_year !== undefined) {
      fields.push(bibliographicField(
        "Year", citedYear, match.matched_year,
        match.year_match === true ? "match" : match.year_match === false && !match.year_mismatch_plausible ? "discrepancy" : "unverified comparison",
        sourceFor("year"),
      ));
    }
    const citedAuthor = match.cited_first_author || reference.ay_surname;
    if (citedAuthor && match.matched_first_author) {
      fields.push(bibliographicField(
        "First author", citedAuthor, match.matched_first_author,
        match.author_match === true ? "match" : match.author_match === false ? "discrepancy" : "unverified comparison",
        sourceFor("author"),
      ));
    }
    const matchedTitle = resolve.matched_title || candidate.title;
    if (reference.title || matchedTitle || typeof match.title_overlap === "number") {
      fields.push(bibliographicField(
        "Title coverage", reference.title, matchedTitle,
        typeof match.title_overlap === "number" ? `title coverage: ${scoreText(match.title_overlap)}` : "title coverage",
        sourceFor("title"),
      ));
    }
    if (!fields.length) return null;
    return card(el("h3", {class: "cv-section-title"}, "Bibliographic field comparison"), add(el("div", {class: "cv-comparison-grid"}), fields));
  };
  const showSource = (item, source) => {
    const closeButton = startDrawer(sourceLabel(item), source);
    const reference = item.reference || {}, resolve = item.resolve || {};
    const adjudication = ((resolve.evidence_profile || {}).bibliographic_adjudication || {});
    const reviewLabels = item.bibliographic_review_labels || [];
    const refutationText = (adjudication.refutations || []).map(refutation =>
      [status(refutation.kind), refutation.field, refutation.cited_value, refutation.observed_value, refutation.source].filter(Boolean).join(" · ")
    );
    const attempts = [...(resolve.attempts || []), ...(resolve.resolver_attempts || [])];
    const candidate = resolverCandidate(resolve), match = resolverMetadata(resolve), acquired = preferredManifestEntry(item);
    const returnedAuthors = resolve.matched_authors || candidate.authors;
    const returnedAuthorList = Array.isArray(returnedAuthors) ? returnedAuthors.filter(Boolean) : [];
    const returnedAuthorText = typeof returnedAuthors === "string" ? returnedAuthors.trim() : "";
    const matchedAuthors = returnedAuthorList.length
      ? returnedAuthorList
      : returnedAuthorText || (match.matched_first_author ? t("value.first_author_only", {author: match.matched_first_author}) : null);
    const texts = (item.manifest_entries || []).filter(isVerifiableManifestEntry).map(entry => [status(entry.tier), entry.extraction_method, entry.char_count ? t("value.character_count", {count: entry.char_count}) : "", entry.identity_note].filter(Boolean).join(" · "));
    const comparison = add(
      el("div", {class: "cv-comparison-grid"}),
      comparisonPanel("heading.parsed_search_input", [
        ["field.bibliography", reference.raw_entry || reference.title],
        ["field.title", reference.title],
        ["field.parsed_first_author", reference.ay_surname],
        ["field.year", reference.year || reference.ay_year],
        ["field.doi", reference.doi],
        ["field.source_url", reference.url],
        ["field.source_type", reference.source_type ? status(reference.source_type) : null],
      ]),
      comparisonPanel("heading.resolver_output", [
        ["field.identity", add(badge(resolve.status), resolve.retracted === true ? badge("retracted") : null)],
        ["field.resolution_provider", (resolve.via || resolve.provider) ? status(resolve.via || resolve.provider) : null],
        ["field.matched_title", resolve.matched_title || candidate.title],
        ["field.matched_authors", matchedAuthors],
        ["field.matched_venue", match.matched_venue],
        ["field.matched_year", match.matched_year],
        ["field.match_score", scoreText(match.score)],
        ["field.title_overlap", scoreText(match.title_overlap)],
        ["field.resolution_basis", resolve.resolution_basis ? status(resolve.resolution_basis) : null],
        ["field.identity_reason", resolve.reason],
        ["field.bibliographic_concern", item.bibliographic_concern ? badge(item.bibliographic_concern.level) : null],
        ["field.bibliographic_concern_reason", item.bibliographic_concern ? bibliographicConcernText(item) : null],
        ["field.bibliographic_review_labels", reviewLabels.map(reviewLabelText)],
        ["field.fabrication_risk", status(resolve.fabrication_risk || resolve.reference_status_tag)],
        ["field.bibliographic_adjudication", adjudication.outcome ? status(adjudication.outcome) : null],
        ["field.check_status", adjudication.check_status ? status(adjudication.check_status) : null],
        ["field.correction_status", adjudication.correction_status ? status(adjudication.correction_status) : null],
        ["field.refutations", refutationText],
      ]),
      comparisonPanel("heading.acquired_text", acquired ? [
        ["field.evidence_scope", status(acquired.tier)],
        ["field.acquisition_origin", acquired.origin ? status(acquired.origin) : null],
        ["field.source_location", acquired.source_ref || acquired.source_url || acquired.url],
        ["field.extraction_method", acquired.extraction_method],
        ["field.character_count", acquired.char_count ? t("value.character_count", {count: acquired.char_count}) : null],
        ["field.identity", acquired.identity_status ? badge(acquired.identity_status) : null],
        ["field.identity_corroboration", acquired.identity_note],
      ] : [], "cv-comparison-wide"),
    );
    drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.source_comparison")), comparison));
    const fieldComparison = bibliographicFieldComparison(reference, resolve, candidate, match, adjudication);
    if (fieldComparison) drawer.append(fieldComparison);
    drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.resolution_search")), detailList([["field.query_display", resolve.query_display]]), timeline(attempts, attempt => ({title: status(attempt.via || attempt.provider || attempt.stage), detail: resolveText(attempt)}))));
    drawer.append(card(el("h3", {class: "cv-section-title"}, t("heading.source_acquisition")), detailList([["field.text_availability", sourceTextAvailabilityBadge(item)], ["field.availability_explanation", sourceTextAvailabilityDetail(item)], ["field.text_available", texts], ["field.used_by", (item.claim_ids || []).map(id => excerpt(claimText(claimById.get(id))))]]), timeline(item.fetch_attempts || [], attempt => ({title: status(attempt.method || attempt.origin), detail: fetchText(attempt)}))));
    drawer.append(audit({ref_id: item.reference.id, resolve, manifest_entries: item.manifest_entries, fetch_attempts: item.fetch_attempts}));
    closeButton.focus();
  };

  const table = (columns, rows, open) => {
    const head = el("tr"); columns.forEach(column => head.append(el("th", {scope: "col"}, t(column.label))));
    const body = el("tbody");
    rows.forEach(row => {
      const tr = el("tr", open ? {tabindex: "0"} : {});
      columns.forEach(column => { const td = el("td"); const value = column.value(row); td.append(value instanceof Node ? value : textNode(value)); tr.append(td); });
      if (open) { tr.addEventListener("click", () => open(row, tr)); tr.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(row, tr); } }); }
      body.append(tr);
    });
    return add(el("div", {class: "cv-table-wrap"}), add(el("table"), add(el("thead"), head), body));
  };
  const primary = (main, secondary) => add(el("div"), el("div", {class: "cv-table-primary"}, main), secondary ? el("div", {class: "cv-table-secondary"}, secondary) : null);
  const visible = item => {
    const claim = item.claim_id ? claimById.get(item.claim_id) : item.claim ? item : null;
    const source = item.ref_id ? sourceById.get(item.ref_id) : item.reference ? item : null;
    return [
      claim && claimText(claim), source && sourceTitle(source), source && sourceCitation(source),
      (item.reference || {}).raw_entry, item.source_display_citation,
    ].filter(Boolean).join(" ").toLocaleLowerCase(localeId);
  };
  const filters = (items, fields) => {
    const controls = el("div", {class: "cv-filters"});
    const filterField = (labelText, control) => add(el("div", {class: "cv-filter-field"}), el("label", {}, labelText), control);
    const search = el("input", {type: "search", value: query().get("q") || "", "aria-label": t("a11y.report_search")});
    search.addEventListener("change", () => setQuery("q", search.value));
    controls.append(filterField(t("heading.search"), search));
    fields.forEach(field => {
      const select = el("select", {"aria-label": t(field.label)}); select.append(el("option", {value: ""}, t("filter.all")));
      const choices = new Map();
      (field.options || []).forEach(option => choices.set(option.value, option));
      if (!field.fixed) {
        [...new Set(items.map(field.get).filter(Boolean).map(String))].sort().forEach(value => {
          if (!choices.has(value)) choices.set(value, {value});
        });
      }
      const selected = query().get(field.name);
      if (selected && !choices.has(selected)) choices.set(selected, {value: selected});
      choices.forEach(choice => select.append(el("option", {value: choice.value, selected: query().get(field.name) === choice.value ? "" : undefined}, choice.label ? t(choice.label) : field.status ? status(choice.value) : choice.value)));
      select.addEventListener("change", () => setQuery(field.name, select.value)); controls.append(filterField(t(field.label), select));
    });
    return controls;
  };
  const match = (items, fields) => items.filter(item => {
    const search = (query().get("q") || "").toLocaleLowerCase(localeId);
    return (!search || visible(item).includes(search)) && fields.every(field => {
      const selected = query().get(field.name);
      return !selected || (field.matches ? field.matches(item, selected) : String(field.get(item) || "") === selected);
    });
  });
  const metricCard = (value, key, stateCode = "neutral", destination) => {
    const node = el(destination ? "button" : "section", {class: `cv-card cv-metric ${classFor(stateCode)}${destination ? " cv-metric-link" : ""}`, type: destination ? "button" : undefined, "aria-label": destination ? t("action.open_metric", {value, label: t(key)}) : undefined});
    node.append(el("strong", {}, value), el("span", {}, t(key)));
    if (destination) node.addEventListener("click", () => navigate(destination.view, destination.params));
    return node;
  };
  const pairCounts = overview => {
    const counts = overview.pairs_by_semantic_outcome || {};
    return {...counts, unresolved: Number(counts.unresolved || 0) + Number(counts.no_outcome || 0) + Number(counts.non_decidable || 0)};
  };
  const outcomeSummaryKey = kind => `summary.outcome_${kind}`;
  const assessmentState = overview => ["green", "yellow", "red"].includes((overview.assessment || {}).state) ? overview.assessment.state : "yellow";
  const assessmentCopyKey = assessment => {
    const reasons = assessment.reason_codes || [];
    const hasBibliographicRefutation = reasons.some(code => [
      "hard.source.reference_refuted",
      "hard.source.high_fabrication_suspicion",
      "hard.source.suspected_fabricated",
    ].includes(code));
    return hasBibliographicRefutation
      ? "assessment.red.bibliographic_copy"
      : "assessment.red.copy";
  };
  const attentionText = item => {
    const pair = (state.projection.pairs || []).find(value => value.claim_id === item.claim_id && value.ref_id === item.ref_id) || {};
    if ((pair.pair_state || {}).terminal_cause === "jury1_guard") return t("attention.jury1_guard");
    const decision = pair.decision || {};
    return decision.reason || decision.incompatible_proposition || decision.explanation || decision.supported_content || status(item.semantic_outcome || item.result_class);
  };
  const hardSource = item => {
    const resolve = (item || {}).resolve || {};
    const concern = (item || {}).bibliographic_concern || {};
    return ["reference_refuted", "high_fabrication_suspicion"].includes(concern.level)
      || ["not_found", "identifier_mismatch", "suspected_fabricated"].includes(resolve.status)
      || resolve.reference_status_tag === "suspected_fabricated"
      || resolve.retracted === true;
  };
  const bibliographicConcern = item => (item || {}).bibliographic_concern || {};
  const bibliographicReviewLabels = item => Array.isArray((item || {}).bibliographic_review_labels) ? item.bibliographic_review_labels : [];
  const reviewLabelBadges = item => add(
    el("span", {class: "cv-review-label-badges"}),
    bibliographicReviewLabels(item).map(label => badge(label.code)),
  );
  const bibliographicAssessmentBadge = item => {
    const concern = bibliographicConcern(item);
    if (concern.level) return badge(concern.level);
    if (bibliographicReviewLabels(item).length) return badge("bibliographic_review_required");
    return status("none");
  };
  const reviewLabelText = label => {
    const providers = (label.providers || []).join(", ") || t("value.not_recorded");
    if (label.code === "incomplete_bibliographic_source") {
      return t("review_label.incomplete_bibliographic_source_copy", {
        label: status(label.code),
      });
    }
    if (label.code === "no_compatible_article_at_cited_coordinates") {
      return t("review_label.coordinate_copy", {
        label: status(label.code),
        resolver: status(((label.lookup || {}).resolver)),
        providers,
      });
    }
    if (label.code === "author_list_discrepancy") {
      return t("review_label.author_list_discrepancy_copy", {
        label: status(label.code),
        provider: status(label.provider),
        authors: (label.missing_cited_authors || []).join(", ") || t("value.not_recorded"),
      });
    }
    return t("review_label.completed_searches_copy", {
      label: status(label.code),
      providers,
    });
  };
  const bibliographicConcernText = item => {
    const concern = bibliographicConcern(item);
    if (concern.basis === "positive_refutation") {
      return t("attention.reference_refuted_copy", {
        resolver: status(concern.resolver),
        coordinates: concern.cited_coordinates || t("value.not_recorded"),
        work: concern.observed_work || t("value.not_recorded"),
      });
    }
    if (concern.basis === "complete_issue_absence_with_incomplete_identity_checks") {
      return t("attention.elevated_issue_absence_incomplete_checks_copy", {
        resolver: status(concern.resolver),
        coordinates: concern.cited_coordinates || t("value.not_recorded"),
      });
    }
    if (concern.refutation_kind === "absent_from_complete_issue") {
      return t("attention.refutation_complete_issue_copy", {
        resolver: status(concern.resolver),
        coordinates: concern.cited_coordinates || t("value.not_recorded"),
      });
    }
    if (concern.refutation_kind === "identifier_not_found") {
      return t("attention.refutation_identifier_not_found_copy", {
        resolver: status(concern.resolver),
        identifier: concern.cited_coordinates || t("value.not_recorded"),
      });
    }
    if (concern.refutation_kind === "identifier_targets_other_work") {
      return t("attention.refutation_identifier_other_work_copy", {
        resolver: status(concern.resolver),
        identifier: concern.cited_coordinates || t("value.not_recorded"),
        work: concern.observed_work || t("value.not_recorded"),
      });
    }
    if (concern.basis === "documented_refutation") {
      return t("attention.documented_refutation_copy", {
        resolver: status(concern.resolver),
        cited: concern.cited_coordinates || t("value.not_recorded"),
      });
    }
    if (concern.basis === "same_resolver_complete_absence") {
      const scope = concern.scope === "canonical journal/year/volume/first locator"
        ? t("value.canonical_article_coordinates")
        : concern.scope || t("value.not_recorded");
      return t("attention.high_suspicion_copy", {
        resolver: status(concern.resolver),
        journal: concern.journal || t("value.not_recorded"),
        scope,
      });
    }
    if (concern.basis === "completed_search_misses") {
      const providers = (concern.providers || []).map(status).join(", ") || t("value.not_recorded");
      return t("attention.elevated_suspicion_copy", {
        providers,
        journal: concern.journal || t("value.unconfirmed_journal"),
      });
    }
    return concern.conclusion || t("attention.high_suspicion_fallback");
  };
  const remediation = () => {
    const data = state.projection.remediation || {}, guidance = data.guidance || [], interventions = data.interventions || [], lineage = data.child_run;
    if (!guidance.length && !interventions.length && !lineage) return null;
    const body = el("div", {class: "cv-remediation"});
    if (guidance.length) {
      const list = el("div", {class: "cv-remediation-list"});
      guidance.forEach(item => {
        const routes = item.child_run_routes || [];
        const finding = [item.code, item.claim_id, item.ref_id, item.scope]
          .filter(value => value !== undefined && value !== null && value !== "").join(" · ");
        const row = add(el("article", {class: "cv-remediation-row"}), el("strong", {}, t(`remediation.action.${item.action}`)), el("p", {class: "cv-remediation-finding"}, finding), el("p", {}, t("remediation.automatic_finding")));
        if (item.child_run_command || routes.length) {
          row.append(el("p", {class: "cv-remediation-child"}, t("remediation.child_run_only")));
          if (item.child_run_command) row.append(el("p", {class: "cv-remediation-command"}, item.child_run_command));
          routes.forEach(route => {
            const commands = el("ul", {class: "cv-remediation-commands"});
            if (route.child_start_command) commands.append(el("li", {}, route.child_start_command));
            (route.commands || []).forEach(command => commands.append(el("li", {}, command)));
            row.append(el("p", {class: "cv-remediation-route"}, t("remediation.route", {route: route.route_id})), commands);
          });
        }
        list.append(row);
      });
      body.append(el("h3", {}, t("heading.remediation_guidance")), list);
    }
    if (interventions.length) {
      const list = el("ul", {class: "cv-remediation-interventions"});
      interventions.forEach(item => {
        const missing = t("value.not_recorded");
        list.append(add(el("li"),
          el("span", {}, t("remediation.intervention", {kind: t(`remediation.kind.${item.kind}`), effect: t(`remediation.effect.${item.effect}`), admission: item.admission_classification ? t(`remediation.admission.${item.admission_classification}`) : missing, action: item.action || missing, applied: item.applied_at || missing})),
          el("small", {class: "cv-remediation-binding"}, t("remediation.intervention_binding", {task: item.task_id, answer: item.answer_id, claim: item.claim_id || missing, reference: item.ref_id || missing, source: item.source_id || missing}))
        ));
      });
      body.append(el("h3", {}, t("heading.remediation_interventions")), el("p", {}, t("remediation.interventions_preserve")), list);
    }
    if (lineage) {
      const values = [["field.run_origin", lineage.run_origin], ["field.parent_run", lineage.parent_run_id]];
      if (lineage.provenance) values.push(["field.remediation_provenance", t("remediation.provenance_recorded")]);
      body.append(detailCard("heading.remediation_lineage", values));
    }
    return section("heading.remediation", body);
  };
  const overview = () => {
    const data = state.projection.overview || {}, counts = pairCounts(data), assessment = data.assessment || {}, assessmentCode = assessmentState(data);
    const hero = el("section", {class: "cv-hero", "data-assessment": assessmentCode});
    const left = add(el("div"), el("p", {class: "cv-eyebrow"}, t("eyebrow.run_result")), el("h2", {class: "cv-hero-title"}, t("heading.overall_result")), el("p", {class: "cv-hero-copy"}, t("summary.run_narrative", {claims: data.claims_total || 0, pairs: data.pairs_total || 0, references: data.references_total || 0})), el("p", {class: "cv-hero-copy"}, t("summary.hero_claims", {positive: data.claims_with_crediting_support || 0, total: data.claims_total || 0})));
    const bar = el("div", {class: "cv-pair-bar", "aria-label": t("heading.outcomes")});
    const legend = el("div", {class: "cv-legend"});
    ["supports", "partial", "unresolved", "contradicts", "related", "off_topic"].forEach(kind => { const count = Number(counts[kind] || 0); if (!count) return; bar.append(el("span", {class: classFor(kind), style: `flex:${count}`})); legend.append(add(el("span", {class: "cv-legend-item"}), el("i", {class: `cv-legend-dot ${classFor(kind)}`}), textNode(`${count} ${t(outcomeSummaryKey(kind))}`))); });
    left.append(bar, legend);
    const assessmentCounts = [
      [assessment.hard_findings, "assessment.hard_findings"],
      [assessment.review_findings, "assessment.review_findings"],
      [assessment.minor_findings, "assessment.minor_findings"],
      [assessment.technical_findings, "assessment.technical_findings"],
    ].filter(([value]) => Number(value || 0) > 0);
    const assessmentDetails = el("div", {class: "cv-assessment-counts"});
    assessmentCounts.forEach(([value, key]) => assessmentDetails.append(add(el("div"), el("strong", {}, value), el("span", {}, t(key)))));
    if (!assessmentCounts.length) assessmentDetails.append(el("p", {class: "cv-assessment-clear"}, t("assessment.no_material_findings")));
    const copyKey = assessmentCode === "red"
      ? assessmentCopyKey(assessment)
      : `assessment.${assessmentCode}.copy`;
    const right = add(el("div", {class: `cv-hero-assessment cv-assessment-${assessmentCode}`}), el("p", {class: "cv-assessment-label"}, t("summary.assessment_label")), el("h3", {class: "cv-assessment-title"}, t(`assessment.${assessmentCode}.title`)), el("p", {class: "cv-assessment-copy"}, t(copyKey)), assessmentDetails);
    hero.append(left, right);
    const findings = el("div", {class: "cv-metric-grid"});
    ["supports", "partial", "unresolved", "contradicts", "related", "off_topic"].forEach(kind => findings.append(metricCard(counts[kind] || 0, outcomeSummaryKey(kind), kind, {view: "pairs", params: {outcome: kind}})));
    const identities = add(
      el("div", {class: "cv-metric-grid"}),
      metricCard(data.sources_identity_confirmed || 0, "summary.sources_confirmed", "supports", {view: "sources", params: {identity: "resolved"}}),
      metricCard(data.sources_identity_not_automatically_confirmed || 0, "summary.sources_unconfirmed", "neutral", {view: "sources", params: {identity: "not_confirmed"}}),
      metricCard(data.sources_suspected_fabricated || 0, "summary.sources_fabricated", "contradicts", {view: "sources", params: {concern: "strong"}}),
      metricCard(data.sources_retracted || 0, "summary.sources_retracted", "contradicts", {view: "sources", params: {identity: "retracted"}}),
      metricCard(data.sources_elevated_bibliographic_suspicion || 0, "summary.sources_elevated_suspicion", "partial", {view: "sources", params: {concern: "elevated_bibliographic_suspicion"}}),
      metricCard(data.orphan_citations || 0, "summary.orphan_citations", "contradicts", {view: "claims", params: {issue: "orphan"}}),
    );
    const coverage = add(
      el("div", {class: "cv-metric-grid"}),
      metricCard(`${data.pairs_completed || 0}/${data.pairs_total || 0}`, "summary.pairs_completed", "neutral", {view: "pairs", params: {completion: "completed"}}),
      metricCard(`${data.references_cited || 0}/${data.references_total || 0}`, "summary.references_cited", "neutral", {view: "sources", params: {usage: "cited"}}),
      metricCard(data.sources_completed_search_review || 0, "summary.sources_completed_search_review", "neutral", {view: "sources", params: {review: "not_found_after_completed_searches"}}),
      metricCard(data.sources_coordinate_lookup_review || 0, "summary.sources_coordinate_lookup_review", "neutral", {view: "sources", params: {review: "no_compatible_article_at_cited_coordinates"}}),
      metricCard(data.sources_incomplete_bibliographic_source || 0, "summary.sources_incomplete_bibliographic_source", "unresolved", {view: "sources", params: {review: "incomplete_bibliographic_source"}}),
    );
    const review = el("div", {class: "cv-triage"});
    const attention = state.projection.attention || [];
    attention.filter(item => item.entity === "pair").forEach(item => {
      const claim = claimById.get(item.claim_id), pair = (byClaim.get(item.claim_id) || []).find(value => value.ref_id === item.ref_id), type = outcome(pair || {});
      const button = el("button", {type: "button", class: `cv-triage-row ${type === "unresolved" ? "cv-purple" : type === "contradicts" ? "cv-negative" : ""}`});
      button.append(el("span", {class: "cv-triage-title"}, t(type === "partial" ? "attention.partial_title" : "attention.unresolved_title")), el("span", {class: "cv-triage-copy"}, excerpt(claimText(claim))), el("span", {class: "cv-triage-source"}, t("attention.source_prefix", {source: item.source_display_citation || item.source_display_id || ""})), el("span", {class: "cv-triage-source"}, attentionText(item)));
      button.addEventListener("click", () => pair ? showPair(pair, button) : showClaim(claim, button)); review.append(button);
    });
    const orphans = attention.filter(item => item.entity === "citation");
    orphans.forEach(item => {
      const claim = claimById.get(item.claim_id), marker = (item.citation || {}).marker_raw || t("value.not_recorded");
      const button = el("button", {type: "button", class: "cv-triage-row cv-negative"});
      button.append(el("span", {class: "cv-triage-title"}, t("attention.orphan_title")), el("span", {class: "cv-triage-copy"}, excerpt(claimText(claim))), el("span", {class: "cv-triage-source"}, t("attention.orphan_copy", {marker})));
      button.addEventListener("click", () => showClaim(claim, button)); review.append(button);
    });
    const sourceAttention = attention.filter(item => item.entity === "source");
    const attentionSources = [...new Map(sourceAttention.map(item => {
      const sourceItem = sourceById.get(item.ref_id);
      return sourceItem ? [sourceItem.reference.id, sourceItem] : null;
    }).filter(Boolean)).values()];
    const seriousSources = attentionSources.filter(hardSource);
    seriousSources.forEach(item => {
      const button = el("button", {type: "button", class: "cv-triage-row cv-negative"}), resolve = item.resolve || {}, concern = bibliographicConcern(item);
      const identity = concern.level || (resolve.retracted ? "retracted" : resolve.reference_status_tag === "suspected_fabricated" ? "suspected_fabricated" : resolve.status);
      const titleKey = concern.level === "reference_refuted" ? "attention.reference_refuted_title" : concern.level === "high_fabrication_suspicion" ? "attention.high_suspicion_title" : "attention.serious_source_title";
      const copy = concern.level ? bibliographicConcernText(item) : t("attention.serious_source_copy", {status: status(identity)});
      button.append(el("span", {class: "cv-triage-title"}, t(titleKey, {source: sourceLabel(item)})), badge(identity), el("span", {class: "cv-triage-copy"}, copy));
      button.addEventListener("click", () => showSource(item, button)); review.append(button);
    });
    const elevatedSources = attentionSources.filter(item => bibliographicConcern(item).level === "elevated_bibliographic_suspicion");
    elevatedSources.forEach(item => {
      const button = el("button", {type: "button", class: "cv-triage-row cv-elevated"});
      button.append(el("span", {class: "cv-triage-title"}, t("attention.elevated_suspicion_title", {source: sourceLabel(item)})), badge("elevated_bibliographic_suspicion"));
      if (bibliographicReviewLabels(item).length) button.append(reviewLabelBadges(item));
      button.append(el("span", {class: "cv-triage-copy"}, [bibliographicConcernText(item), ...bibliographicReviewLabels(item).map(reviewLabelText)].join(" ")));
      button.addEventListener("click", () => showSource(item, button)); review.append(button);
    });
    const reviewSources = attentionSources.filter(item => !hardSource(item) && bibliographicConcern(item).level !== "elevated_bibliographic_suspicion" && bibliographicReviewLabels(item).length);
    reviewSources.forEach(item => {
      const button = el("button", {type: "button", class: "cv-triage-row cv-purple"});
      button.append(el("span", {class: "cv-triage-title"}, t("attention.bibliographic_review_title", {source: sourceLabel(item)})), reviewLabelBadges(item), el("span", {class: "cv-triage-copy"}, bibliographicReviewLabels(item).map(reviewLabelText).join(" ")));
      button.addEventListener("click", () => showSource(item, button)); review.append(button);
    });
    const sources = attentionSources.filter(item => {
      return !hardSource(item) && bibliographicConcern(item).level !== "elevated_bibliographic_suspicion" && !bibliographicReviewLabels(item).length;
    });
    if (sources.length) {
      const links = el("div", {class: "cv-triage-links"});
      sources.forEach(sourceItem => {
        const button = el("button", {type: "button"}, sourceLabel(sourceItem));
        button.addEventListener("click", () => showSource(sourceItem, button)); links.append(button);
      });
      review.append(add(
        el("div", {class: "cv-triage-row"}),
        el("span", {class: "cv-triage-title"}, t("attention.unconfirmed_title", {count: sources.length})),
        links,
        el("span", {class: "cv-triage-source"}, t("attention.unconfirmed_copy")),
      ));
    }
    const tables = state.projection.table_only_citations || [];
    if (tables.length) {
      const links = el("div", {class: "cv-triage-links"});
      tables.forEach(tableItem => {
        const item = sourceById.get(tableItem.ref_id), button = el("button", {type: "button"}, `${tableItem.source_display_id || ""} ${tableItem.source_display_citation || ""}`.trim());
        if (item) button.addEventListener("click", () => showSource(item, button)); links.append(button);
      });
      review.append(add(
        el("div", {class: "cv-triage-row"}),
        el("span", {class: "cv-triage-title"}, t("attention.table_only_title", {count: tables.length})),
        links,
        el("span", {class: "cv-triage-source"}, t("attention.table_only_copy")),
      ));
    }
    if (!review.childElementCount) review.append(card(el("p", {}, t("empty.no_attention"))));
    const reviewCount = attention.filter(item => item.entity === "pair").length + orphans.length + sourceAttention.length + tables.length;
    const remediationSection = remediation();
    const retracted = (state.projection.sources || []).filter(item => (item.resolve || {}).retracted === true);
    const retractedSection = retracted.length ? countedSection("heading.retracted_sources", retracted.length, retracted.map(item => {
      const button = el("button", {type: "button", class: "cv-triage-row cv-negative"});
      button.append(el("span", {class: "cv-triage-title"}, sourceLabel(item)), badge("retracted"), el("span", {class: "cv-triage-copy"}, t("summary.retracted_sources_copy")));
      button.addEventListener("click", () => showSource(item, button));
      return button;
    })) : null;
    return add(el("div"), hero, section("heading.manuscript_findings", findings, identities), section("heading.run_quality", coverage), retractedSection, countedSection("heading.needs_review", reviewCount, review), ...(remediationSection ? [remediationSection] : []));
  };
  const configuration = () => {
    const config = state.projection.configuration || {}, runtime = config.verify_runtime || {}, policy = config.verify_policy || {}, models = (state.projection.models || {}).by_provider_model_role || [];
    const used = models.map(item => `${[item.provider, item.model].filter(Boolean).join(" / ")} · ${status(item.role)}: ${item.attempts || 0}`);
    const configured = (policy.providers || policy.lanes || []).flatMap(item => typeof item === "string" ? [item] : (item.lanes || [item]).map(lane => {
      const name = [item.name || item.provider_id || item.provider, lane.model || lane.model_id].filter(Boolean).join(" / ");
      const roles = [lane.jury1_eligible ? status("jury1") : "", lane.jury2_eligible ? status("jury2") : ""].filter(Boolean);
      return [name, roles.join(" + ")].filter(Boolean).join(" · ");
    })).filter(Boolean);
    const usedModels = new Set(models.map(item => [item.provider, item.model].filter(Boolean).join("/")).filter(Boolean));
    const usedRoles = new Set(models.map(item => item.role).filter(Boolean));
    const jury2Level = policy.jury2_level || runtime.jury2_level;
    const judgeSeparation = jury2Level === "off" ? t("value.jury2_disabled") : usedModels.size === 1 && usedRoles.has("jury1") && usedRoles.has("jury2") ? t("value.same_underlying_model") : t("value.models_may_differ");
    const regime = (state.projection.manuscript || {}).accuracy || runtime.accuracy;
    const credentials = config.credentials || {recorded: false, rows: []};
    const present = credentials.rows.filter(item => item.present_at_start).map(item => item.env_name);
    const credentialOutcome = (channel, item) => `${t(`credential.channel.${channel}`)}: ${t("credential.calls", {success: item.successful_http_responses, calls: item.calls})}; 401 ${item.http_401}, 403 ${item.http_403}, 429 ${item.http_429}, ${t("credential.other", {count: item.other_http_errors})}, ${t("credential.network", {count: item.network_errors})}`;
    const calls = credentials.rows.filter(item => item.used).map(item => {
      const channels = ["resolve", "fetch", "search"].filter(channel => item.by_channel[channel].calls).map(channel => credentialOutcome(channel, item.by_channel[channel]));
      return `${item.provider} / ${item.env_name}: ${channels.join("; ")}${item.warning_code ? ` · ${t("credential.warning")}` : ""}`;
    });
    const credentialValues = credentials.recorded ? [["field.credentials_present", present.length ? present : t("credential.none_present")], ["field.credentials_used", calls.length ? calls : t("credential.none_used")]] : [["field.credentials_present", t("value.not_recorded")], ["field.credentials_used", t("value.not_recorded")]];
    return section("nav.method", add(el("div", {class: "cv-config-grid"}), detailCard("heading.evidence_regime", [["field.evidence_regime", status(regime)], ["field.evidence_regime_detail", regime && loc().messages[`regime.${regime}`] ? t(`regime.${regime}`) : t("value.not_recorded")], ["field.configuration_origin", runtime.accuracy_origin || t("value.not_recorded")]]), detailCard("heading.verification_policy", [["field.context", status(policy.context_mode || runtime.context_mode)], ["field.profile", status(policy.profile || runtime.profile)], ["field.jury2_policy", status(jury2Level)], ["field.reasoning", status(policy.reasoning || runtime.reasoning)]]), detailCard("heading.models_configured", [["field.configured_models", configured.length ? configured : t("value.not_recorded")], ["field.models_used", used.length ? used : t("value.not_recorded")], ["field.judge_separation", judgeSeparation]]), detailCard("heading.credentials", credentialValues)));
  };
  const claims = () => {
    const claimIssue = item => (item.citations || []).some(citation => citation.ref_id == null && !(citation.candidate_ref_ids || []).length) ? "orphan" : "";
    const fields = [
      {name: "outcome", label: "filter.outcome", get: item => worst(byClaim.get(item.claim.id) || []), status: true},
      {name: "sources", label: "filter.source_count", get: item => String(item.source_display_ids.length)},
      {name: "issue", label: "filter.issue", get: claimIssue, status: true, options: [{value: "orphan", label: "status.orphan"}]},
    ];
    const items = match(state.projection.claims || [], fields);
    return section("heading.claims", filters(state.projection.claims || [], fields), items.length ? table([{label: "column.claim_text", value: item => primary(excerpt(claimText(item)), item.claim.marker_raw)}, {label: "column.sources", value: item => (item.ref_ids || []).map(id => sourceLabel(sourceById.get(id))).join(" · ")}, {label: "column.outcome", value: item => badge(worst(byClaim.get(item.claim.id) || []))}, {label: "column.model", value: item => modelsFor(byClaim.get(item.claim.id) || [])}], items, showClaim) : card(el("p", {}, t("empty.no_results"))));
  };
  const sources = () => {
    const fields = [
      {
        name: "identity", label: "filter.identity", get: item => (item.resolve || {}).status, status: true,
        options: [{value: "not_confirmed", label: "filter.identity_not_confirmed"}],
        matches: (item, selected) => selected === "retracted" ? (item.resolve || {}).retracted === true : selected === "not_confirmed" ? (item.resolve || {}).status !== "resolved" : (item.resolve || {}).status === selected,
      },
      {
        name: "concern", label: "filter.bibliographic_concern", get: item => bibliographicConcern(item).level, status: true,
        options: [{value: "strong", label: "filter.strong_bibliographic_concern"}],
        matches: (item, selected) => selected === "strong" ? ["reference_refuted", "high_fabrication_suspicion"].includes(bibliographicConcern(item).level) : bibliographicConcern(item).level === selected,
      },
      {
        name: "review", label: "filter.bibliographic_review", get: () => "", fixed: true,
        options: [
          {value: "not_found_after_completed_searches", label: "status.not_found_after_completed_searches"},
          {value: "no_compatible_article_at_cited_coordinates", label: "status.no_compatible_article_at_cited_coordinates"},
          {value: "incomplete_bibliographic_source", label: "status.incomplete_bibliographic_source"},
          {value: "author_list_discrepancy", label: "status.author_list_discrepancy"},
        ],
        matches: (item, selected) => bibliographicReviewLabels(item).some(label => label.code === selected),
      },
      {name: "tier", label: "filter.evidence", get: bestTier, status: true},
      {name: "outcome", label: "filter.outcome", get: item => worst(bySource.get(item.reference.id) || []), status: true},
      {name: "usage", label: "filter.usage", get: item => (item.claim_ids || []).length ? "cited" : "uncited", status: true},
    ];
    const items = match(state.projection.sources || [], fields);
    return section("heading.sources", filters(state.projection.sources || [], fields), items.length ? table([{label: "column.bibliography", value: item => primary(sourceTitle(item), `${item.display_id || ""} · ${sourceCitation(item)}`)}, {label: "column.bibliographic_concern", value: bibliographicAssessmentBadge}, {label: "column.bibliographic_review", value: reviewLabelBadges}, {label: "column.identity", value: item => add(badge((item.resolve || {}).status), (item.resolve || {}).retracted === true ? badge("retracted") : null)}, {label: "column.text_availability", value: sourceTextAvailabilityBadge}, {label: "column.source_count", value: item => item.claim_ids.length}, {label: "column.outcome", value: item => badge(worst(bySource.get(item.reference.id) || []))}], items, showSource) : card(el("p", {}, t("empty.no_results"))));
  };
  const pairs = () => {
    const fields = [
      {
        name: "outcome", label: "filter.outcome", get: outcome, status: true,
        matches: (item, selected) => selected === "unresolved" ? ["unresolved", "no_outcome", "non_decidable"].includes(outcome(item)) : outcome(item) === selected,
      },
      {name: "assurance", label: "filter.assurance", get: item => (item.verification || {}).assurance || (item.verification || {}).resolution, status: true},
      {name: "model", label: "filter.model", get: item => modelsFor([item])},
      {name: "completion", label: "filter.completion", get: item => (item.verification || {}).operational_complete ? "completed" : "incomplete", status: true},
    ];
    const items = match(state.projection.pairs || [], fields);
    return section("heading.pairs", filters(state.projection.pairs || [], fields), items.length ? table([{label: "column.claim_text", value: item => primary(excerpt(claimText(claimById.get(item.claim_id))), sourceLabel(sourceById.get(item.ref_id)))}, {label: "column.outcome", value: item => badge(outcome(item))}, {label: "column.assurance", value: item => primary(status((item.verification || {}).assurance || (item.verification || {}).resolution), rejectedJury2Count(item) ? t("value.jury2_rejections", {count: rejectedJury2Count(item)}) : "")}, {label: "column.model", value: item => modelsFor([item])}], items, showPair) : card(el("p", {}, t("empty.no_results"))));
  };
  const structured = (titleKey, value) => {
    const entries = value && typeof value === "object" ? Object.entries(value) : [];
    const items = el("div", {class: "cv-structured"});
    entries.forEach(([key, item]) => items.append(add(el("article", {class: "cv-structured-item"}), el("h3", {}, key), el("p", {}, typeof item === "object" ? JSON.stringify(item) : String(item)))));
    return section(titleKey, items.childElementCount ? items : card(el("p", {}, t("empty.not_recorded"))));
  };
  const manuscript = () => { const data = state.projection.manuscript || {}; return data.display_title || data.title || data.filename || t("value.untitled_manuscript"); };
  const debugNotice = () => {
    const execution = state.projection.execution || {};
    if (!execution.debug_mode) return null;
    const notice = add(
      el("section", {class: "cv-debug-banner", role: "alert"}),
      el("span", {class: "cv-debug-badge"}, t("execution.debug_badge")),
      add(
        el("div"),
        el("strong", {class: "cv-debug-title"}, t("execution.debug_title")),
        el("p", {class: "cv-debug-copy"}, t("execution.debug_copy")),
      ),
    );
    if ((execution.debug_labels || []).length) notice.lastElementChild.append(el("p", {class: "cv-debug-labels"}, t("execution.debug_labels", {labels: execution.debug_labels.join(" · ")})));
    return notice;
  };
  const apply = () => {
    Object.entries(state.catalog.themes[themeId].tokens).forEach(([key, value]) => document.documentElement.style.setProperty(key, value));
    document.documentElement.lang = loc().language_tag; document.documentElement.dir = loc().direction;
    document.title = `${t("app.title")} · ${manuscript()}`;
    try { localStorage.setItem("callimachus-report-theme", themeId); } catch (_) {}
  };
  const render = () => {
    if (!viewNames.includes(active)) active = "overview";
    apply(); app.replaceChildren();
    const shell = el("div", {class: "cv-shell"});
    const menu = el("button", {type: "button", class: "cv-menu-button", "aria-label": t("action.open_navigation"), "aria-controls": "cv-sidebar", "aria-expanded": "false"}, "☰");
    const setNavigationOpen = open => {
      shell.classList.toggle("cv-nav-open", open);
      menu.textContent = open ? "×" : "☰";
      menu.setAttribute("aria-expanded", open ? "true" : "false");
      menu.setAttribute("aria-label", open ? t("a11y.close_navigation") : t("action.open_navigation"));
    };
    menu.addEventListener("click", () => setNavigationOpen(!shell.classList.contains("cv-nav-open")));
    const sidebar = el("aside", {class: "cv-sidebar", id: "cv-sidebar"});
    sidebar.append(add(el("div", {class: "cv-brand"}), el("img", {class: "cv-brand-logo", src: state.brand_logo, alt: t("app.brand")}), el("span", {}, t("app.title"))));
    const nav = el("nav", {class: "cv-nav", "aria-label": t("a11y.main_navigation")});
    const navCounts = {claims: (state.projection.claims || []).length, sources: (state.projection.sources || []).length, pairs: (state.projection.pairs || []).length};
    viewNames.forEach(name => {
      const button = el("button", {type: "button", "aria-current": name === active ? "page" : undefined});
      button.append(document.createTextNode(t(`nav.${name}`)));
      if (navCounts[name] !== undefined) button.append(el("span", {class: "cv-nav-count"}, navCounts[name]));
      button.addEventListener("click", () => { setNavigationOpen(false); navigate(name); }); nav.append(button);
    });
    sidebar.append(nav, el("p", {class: "cv-sidebar-footer"}, t("app.subtitle")));
    const locale = el("select", {"aria-label": t("a11y.locale_selector")}); Object.entries(state.catalog.locales).forEach(([id, value]) => locale.append(el("option", {value: id, selected: id === localeId ? "" : undefined}, value.native_name))); locale.addEventListener("change", () => { localeId = locale.value; render(); });
    const theme = el("select", {"aria-label": t("a11y.theme_selector")}); Object.entries(state.catalog.themes).forEach(([id, value]) => theme.append(el("option", {value: id, selected: id === themeId ? "" : undefined}, t(value.label_key)))); theme.addEventListener("change", () => { themeId = theme.value; render(); });
    const print = el("button", {type: "button"}, t("action.print")); print.addEventListener("click", () => window.print());
    const topbar = add(el("header", {class: "cv-topbar"}), el("div", {class: "cv-topbar-title"}, manuscript()), add(el("div", {class: "cv-controls"}), locale, theme, print));
    const main = el("main", {class: "cv-main"}); main.append(add(el("header", {class: "cv-page-header"}), el("h1", {class: "cv-title"}, manuscript()), el("p", {class: "cv-subtitle"}, t("app.subtitle"))));
    const debug = debugNotice();
    if (debug) main.append(debug);
    if (state.preview) main.append(card(el("p", {class: "cv-preview"}, t("preview.not_audit_ready"))));
    main.append(({overview, claims, sources, pairs, method: configuration, diagnostics: () => structured("heading.diagnostic_summary", state.projection.diagnostics), provenance: () => structured("heading.provenance_summary", state.render_metadata)})[active]());
    shell.append(menu, sidebar, topbar, main); app.append(shell, backdrop, drawer);
  };
  window.addEventListener("hashchange", () => { active = readRoute().view; render(); });
  render();
})();
