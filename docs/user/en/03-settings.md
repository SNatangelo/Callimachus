# 03 — Settings and models

Open **Settings** to change application-wide preferences and inspect the curated categories: **General**, **Bibliographic services**, **Acquisition**, and **Report integrity**. Provider credentials and model choices are configured in the Jury chooser. The available services depend on the app and your account.

To change the app language, open **Settings → General → Application language** and choose a language. The choice is saved automatically; restart Callimachus to apply it on the next launch.

## Configure a model provider

Open the Jury chooser from Analysis and select a provider. Its configuration form shows the settings for that provider. Enter values you already have, then select **Save configuration**. Provider settings are shared across Jury 1 and Jury 2: saving credentials does not assign the provider's models to either lane. Secret fields are masked in the window; storage depends on how Callimachus is launched, as described below.

A remote model provider may require an account, API key, paid plan, or permission. Callimachus cannot create those accounts or keys for you; obtain credentials from the provider and check its terms and costs before using it. The app’s **Check** button tests the configured provider and may contact it.

Local model discovery is separate. Use the provider's check or discovery control to look for available models after installing or starting it. If the status says you are not signed in, Callimachus has no sign-in control for that provider. Complete sign-in in the provider’s own supported client, then return and check again. A provider that is detected but marked unsupported for jury use cannot be selected as a verification model. Use **Other providers** to browse additional supported services.

## Settings storage and backups

Compiled desktop 2.0 saves API keys, tokens and passwords in the native OS vault:
Windows Credential Manager, macOS Keychain or Linux Secret Service. Non-secret
settings such as endpoints, model choices and contact email use `settings.conf`
in the user-data directory; `settings.defaults` is the bundled read-only inventory.
Other UI preferences may use separate files. If vault access fails, the app
reports an error and does not fall back to plaintext credential storage.

Source launches (`python run.py app`) retain local `.env` storage, including
plaintext API keys. Compiled apps ignore old `.env` files and do not migrate or
delete them automatically. Re-enter required credentials through the provider
form or Settings, then check the provider. Genuine process overrides take priority;
remove an override before editing that setting in the app.

Settings export/import is explicit. An export can include credentials; password
encryption is optional. Copying `settings.conf` alone does not copy vault entries.
Import replaces supported settings and sends imported secrets to the vault in a
compiled app. External CLI logins and browser sessions stay with their own clients.
See the [desktop package guide (English)](https://github.com/SNatangelo/Callimachus/blob/main/docs/user/en/09-desktop-packages.md#settings-and-credentials-in-20)
for data paths and legacy configuration behavior.

## Assign models to juries

The chooser displays selected-model counts by provider and a summary of the current Jury selections. Assign models independently: choose one or more models for **Jury 1**, and optionally choose a different set for **Jury 2**. Changing one lane does not change the other. Jury 2 severity is available in Analysis **Options**.

A successful provider check only confirms that the provider responded; it does not guarantee that every detected model can run a jury. Review the lane status before starting. The run readiness check verifies selected jury lanes before verification starts. With no selected model, a new run stops after Resolve and does not Fetch or Verify.

The chooser's **Advanced** section contains settings such as context and reasoning configuration. These settings are shared by analysis runs and are not scoped to an individual model or Jury lane.

If the app requests a local signing key, open **Settings → Report integrity** and choose **Generate signing key**. This local key seals reports and is not shown in the interface. Compiled apps create or retain it in the OS vault; source launches use a signing-key file. Existing explicit file references keep their file-based behavior. This local seal does not provide the separate external integrity authority required for an audit-ready run.


### macOS: recovering access after an update

Callimachus opens without requesting Keychain passwords during its splash. If saved
API credentials need permission, the main window offers **Recover saved credentials**.
Choose **Confirm** to start native macOS requests, or **Cancel** to continue without
those credentials. In each native request, **Always Allow** remembers access for
this build; a self-signed update may need permission again. Denial stops recovery
and does not trigger repeated background requests. You can retry from General settings.

API keys for one profile share a single protected Keychain item, including the
secret report-signing key. When importing an older installation, macOS may ask once
for each old item; subsequent builds access the single combined item. Choose
**Always Allow** to remember authorization. A self-signed update may still require
one new grant. Only after verifying the copy does Callimachus remove the exact old
items it recovered; denied, changed or uncertain entries are preserved. Build
versions do not create separate copies. Ordinary settings and model selections
remain outside Keychain.
