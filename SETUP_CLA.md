# Archivio automatico delle firme CLA

## Stato di questa PR

Il workflow è predisposto per **SNatangelo/Callimachus**. Il repository di destinazione delle firme è **SNatangelo/callimachus-legal**, che deve essere **privato**, con ramo `main` inizializzato.

Il collegamento usato per preparare questa PR non permette di creare repository o configurare secret/regole. Queste impostazioni non sono state create automaticamente. La PR non pubblica Callimachus e non importa codice o cronologia da CitationVerifier.

## 1. Crea il repository privato

Su GitHub, crea un repository con:

- Owner: `SNatangelo`
- Nome: `callimachus-legal`
- Visibilità: **Private**
- **Add a README file**: attivo, così esiste il ramo `main`.

Non creare JSON vuoti per le firme: il workflow creerà autonomamente cartelle e file. Non usare questo repository per il codice pubblico e non cambiarne la visibilità. Il workflow verifica la visibilità prima di scrivere, ma non può impedire una successiva pubblicazione manuale dell'archivio.

## 2. Crea il token e aggiungi il secret

GitHub → Settings del tuo account → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.

Imposta owner `SNatangelo`, **Only select repositories → callimachus-legal**, e **Repository permissions → Contents: Read and write**. Metadata è disponibile in lettura. Non servono permessi di amministrazione, Actions o accesso a CitationVerifier. Scegli una scadenza e sostituisci il secret quando rinnovi il token.

In **Callimachus → Settings → Secrets and variables → Actions → New repository secret**:

```text
Name: CLA_ARCHIVE_TOKEN
Secret: il token appena generato
```

Non inserire il token nel codice, nelle PR o nella chat. Il `GITHUB_TOKEN` per commenti, lettura degli autori e stato delle PR viene fornito automaticamente da GitHub Actions.

## 3. Unisci la PR e verifica il controllo

Il workflow deve essere su `main` per gestire i commenti. Apri una PR di prova da un contributore non ancora firmatario: il solo account del mantenitore è esente, quindi non prova la raccolta di una nuova firma.

Il controllo **`cla/signatures`** deve risultare non superato prima dell'accettazione. Il bot mostra il testo da commentare. Dopo quel commento deve risultare superato e nel repository privato devono comparire:

```text
agreements/v1.1/CLA.md
agreements/v1.1/manifest.json
signatures/v1.1/github-<ID>.json
checks/v1.1/pr-<NUMERO>/<COMMIT>.json
```

La firma contiene l'identità GitHub, il testo effettivo del commento e i suoi timestamp, URL/ID del commento, PR, commit osservato, versione e SHA-256 del CLA, commit del documento e URL del workflow. Il contenuto del CLA è preservato esattamente. Non vengono raccolte automaticamente email o nomi anagrafici.

Se manca il token, l'archivio non è privato/accessibile, un autore non è identificabile o il CLA è stato alterato, il controllo fallisce. Dopo aver risolto il problema, commenta `recheck`. Una firma archiviata non viene cancellata automaticamente se il commento originale viene modificato o eliminato.

## 4. Rendi obbligatorio il controllo

In **Callimachus → Settings → Rules → Rulesets**, applica al ramo principale una regola attiva che richieda PR e il controllo **`cla/signatures`**, scegliendo GitHub Actions come origine se disponibile. Non richiedere il vecchio `license/cla`. Non rimuovere gli altri controlli del progetto. Nessun bypass per contributi esterni; le approvazioni di un secondo revisore possono restare a zero se lavori da solo.

La presenza del workflow non crea questa regola. I ruleset sui repository privati dipendono dal piano; verifica l'effettivo blocco del merge. Non impostare controlli inesistenti prima del test.

Se avevi già collegato il servizio hosted cla-assistant.io, scollegalo da questo repository e conserva privatamente gli eventuali vecchi export. Questo processo non importa automaticamente firme v1.0 né utilizza il Gist/metadata del vecchio setup.

## Accordi e casi non automatici

La versione **1.1 del 23 settembre 2026** aggiorna modalità di accettazione e informativa privacy per questo processo. Le concessioni di diritti e l'impegno di disponibilità pubblica della sezione 2.3 non cambiano rispetto al documento v1.0. Conserva separatamente vecchi accordi e firme già raccolte.

Per contributi aziendali occorrono identificazione e autorità del titolare effettivo, con accettazione scritta verificata privatamente. Il verde del bot non prova questi elementi. Non alterare un JSON fingendo che sia una firma per commento; non usare il commento per diritti che il firmatario non possiede. Questa automazione non risolve la titolarità di codice precedente, dipendenze o materiale di terzi.

Per una futura v1.2 crea un nuovo documento immutabile, aggiorna versione, hash, frase di accettazione e percorsi del workflow; non sovrascrivere v1.1. Le firme non sono trasferite automaticamente fra versioni. Il registro Git conserva la cronologia ma non è una marcatura temporale qualificata o un archivio inalterabile contro un amministratore: resta opportuno un backup privato indipendente dell'archivio.

## Importazione del progetto per il lancio

Quando trasferisci il codice da CitationVerifier, conserva questo workflow, `.github/cla/`, `CLA.md` v1.1 e `CONTRIBUTING.md`. Porta anche `LICENSE` AGPL completo e `LICENSING.md`; aggiorna l'eventuale richiamo a v1.0 o al servizio hosted. Non sovrascrivere questi file con il pacchetto precedente. Non serve trasferire la cronologia privata di CitationVerifier.

## Manutenzione e sicurezza

Il precedente `contributor-assistant/github-action` è archiviato e non più mantenuto. Questo setup usa invece uno script Python standard-library versionato nel progetto. Non usa l'Action archiviata, non esegue codice delle PR e non installa dipendenze.

`actions/checkout` è fissato a un commit verificato. Il checkout privilegia esclusivamente `github.workflow_sha`, cioè il commit del workflow fidato per `pull_request_target`/`issue_comment`, mai `head.sha` della PR. Le credenziali non restano nel checkout. I permessi del token pubblico sono limitati a lettura contenuti, gestione PR e stati; il PAT è limitato all'archivio.

Tutte le letture della PR/autori/commenti sono dati, non comandi. Le richieste API hanno host fisso. Il registro usa file distinti per account/versione e scritture che rifiutano sovrascritture divergenti. Autori e commenti sono paginati. Il controllo finale viene pubblicato sul commit della PR, non sul commit predefinito del workflow.

Test locali con API simulate:

```sh
python3 -m unittest discover -s .github/cla/tests -v
```

Il test end-to-end richiede archivio, secret e PR reale di un altro account e non è stato eseguito durante la preparazione. Se GitHub blocca l'evento `pull_request_target` nelle policy Actions del repository, serve verificare e autorizzare specificamente questo workflow fidato; non eseguire codice dei fork con i secret. La documentazione GitHub annuncia un cambiamento della policy predefinita per i repository pubblici dal 2 novembre 2026.

## Riferimenti

- https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository
- https://docs.github.com/en/actions/concepts/about-actions-policies
- https://github.com/contributor-assistant/github-action (stato archiviato verificato il 23 settembre 2026)
