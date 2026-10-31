# 08 — Troubleshooting

## Analysis stops after Resolve

A new run with no selected LLM stops after resolving the bibliography. Open Settings and configure a provider, then select at least one model for Jury 1 in Analysis. Save settings and check the model-lane status before starting again.

## A provider or local model is unavailable

For a remote provider, confirm that its account, key, endpoint, and permissions are current, then save the changes and use **Check**. The provider’s own service may be down or may require access not included in your account.

For a local model, start its provider and select **Recheck**. If Callimachus says you are not signed in, Callimachus has no sign-in control for that provider. Complete sign-in in the provider’s own supported client, then select Recheck. A detected provider marked unsupported for jury use cannot be selected in Callimachus.

## Credentials disappeared after an update, or the vault is unavailable

Compiled 2.0 ignores legacy `.env` files and does not migrate their credentials.
Re-enter the required keys in the Jury chooser or Settings and check the provider.
If the OS vault is unavailable or locked, restore access to Windows Credential
Manager, macOS Keychain or Linux Secret Service and retry. Putting keys in
`settings.conf` does not bypass vault access. A process-environment override can
also hide saved changes; remove it before editing that setting in the app. See
[Settings storage and backups](03-settings.md#settings-storage-and-backups).

## The run is waiting for a decision

If Parse review is open, match only the items you can identify and leave the rest unresolved or ambiguous. Use **Resume analysis** to save decisions and continue. **Review later** leaves the run paused so you can return to it from Analysis or History.

If Guided Fetch is open, check the selected reference and the source identity before confirming. Provide a lawful exact copy if you have one. If a PDF requires OCR, use **Run OCR**; skipping OCR means its full text is unavailable for verification. **Proceed / skip remaining** records skipped retrievals and unresolved identities rather than inventing source material.

## The report is incomplete or an outcome is uncertain

Check the History row’s coverage and completion fields, then inspect the report’s source scope and review notes. Abstract evidence, missing full text, unresolved identity, or a non-decidable claim can limit the result even when the run is complete. Correct the input or provide stronger source material before continuing in a new run.

## Contact or support

The **Console** section displays run output for optional troubleshooting; ordinary use does not require entering commands. Review and sanitize any log before sharing it, since it may contain document or source details. For an application issue or inappropriate generated text, **Info → Report AI content** opens a public issue form; nothing is sent automatically. Share only a short, sanitized description and never include credentials or private manuscripts.

