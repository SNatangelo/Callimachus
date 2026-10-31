# macOS Keychain update qualification

This procedure tests continuity between two signed application builds using only synthetic credentials. Run native phases in an interactive macOS session; source-level simulations do not establish system consent or persistent access. A persistent local signature is not Apple Developer ID signing or notarization.

## Synthetic A/B check


Use the existing private package self-test described in [Desktop packaging](desktop-build.md#persistent-macos-signing-and-keychain-access), with a **new UUID** for each test namespace. Set `CALLIMACHUS_PACKAGE_KEYCHAIN_ID` to that UUID and `CALLIMACHUS_PACKAGE_KEYCHAIN_PHASE` to `create`, `check`, `recover` or `delete`, then launch the selected app executable with `--package-self-test`. Run from an interactive macOS login session; a controller connected through SSH must attach the test to the logged-in Aqua session for native consent. The controller must never click consent or type a password on the operator's behalf.

1. Verify both app signatures, exact revisions and full designated requirements; require distinct executable/CDHash values. Create no fixture if an attribute-only query finds a pre-existing item at the fresh namespace.
2. Create with A, then cold-check with A. Repeat in a second fresh UUID namespace as a canary. Require one aggregate and no legacy items per namespace.
3. Cold-check with B without interactive recovery. If macOS already permits B, record that outcome rather than forcing a permission change. Do not force a permission change merely to produce this scenario.
4. Initiate recovery once and ask the operator to deny it. Require refusal and another quiet refusal. An isolated ordinary package self-test can then check the frozen GUI shell/settings without personal vault callbacks; blank secret environment settings as the normal smoke runner does.
5. Initiate recovery again and ask for **Always Allow** on the synthetic item. Check the process result; a controller's `STARTED` status is not completion. Then require two successful checks from distinct new B processes, after the recovery process has exited.
6. Check a separately signed, owned control app quietly, if available; it must remain unauthorized. Do not alter system trust or grant access to all applications.
7. Delete the primary fixture using an authorized build. Verify the canary still reads, then delete it. Check fresh processes fail on the now-missing items, confirm zero rows with attribute-only queries, and unload only the controller's own test jobs. Preserve evidence on failure and before any retry.

Success phases exit 0 and print `macOS Keychain update probe passed: <phase>`. A denied or missing fixture check exits 1; invalid IDs/phases exit2. Deletion alone is insufficient proof because deleting an already absent fixture may succeed: keep creation, reading, canary and final-count evidence together.


## Desktop recovery dialog

Use `CALLIMACHUS_PACKAGE_KEYCHAIN_PHASE=gui` with the same fresh fixture UUID to exercise the real desktop recovery dialog and Settings retry button. Preferences and loaders are isolated from personal configuration.

Repeat the existing signature, fresh-namespace, primary/canary creation and
quiet B-refusal prerequisites before this sequence. Use an interactive Aqua
session and a visible Cocoa window; remove any offscreen Qt override. Keep both
UUIDs and all executable hashes in the operator's evidence record.

1. Launch B's executable with `--package-self-test`, the **primary** fixture UUID
   and phase `gui`. Choose **Confirm** in Callimachus, then personally choose
   **Deny** in the native synthetic-item consent dialog. Confirm that navigation
   remains usable. Change pages and hide/show the window: no further native or
   app recovery request should occur spontaneously. Do not automate consent or
   enter a personal password through a controller.
2. Open Settings and click its credential-recovery button. Choose **Confirm**
   again, then personally choose **Always Allow** for the synthetic item. Require
   one native request for each explicit recovery attempt, with no chain after a
   refusal. Close the desktop window and require process exit 0; closing while
   access is still denied must exit 1. No fixture value should be changed by
   recovery.
3. After that process exits, launch and close **two distinct new B processes**
   with the same primary UUID and phase `gui`. Require usable desktop windows,
   no app recovery dialog, no native consent and exit 0 from each. Record PIDs,
   outcomes and the operator's observations; a launched process is not a pass.
   Retain the existing quiet `check` and differently signed-control checks too.
4. Confirm the canary still reads with its original authorized build. Clean up
   only through the existing `delete` phase, first with the exact primary UUID,
   then with the exact canary UUID after its preservation check. That phase
   validates the fixture's synthetic values and addresses only its aggregate and
   three exact legacy accounts. Do not enumerate/delete other profiles, search
   for user secrets or change Keychain ACLs to force the scenario. Keep the
   existing exact-namespace, attribute-only final-count evidence and preserve
   failure evidence before retrying.


## Evidence and limits

Record the exact revisions, executable hashes, complete designated requirements, native OS/architecture, process exits, operator observations and cleanup counts for the build pair. Keep evidence outside public releases and never record secret values. Cold-process checks do not substitute for an OS reboot. A passing pair does not guarantee that a future self-signed update will avoid native authorization.
