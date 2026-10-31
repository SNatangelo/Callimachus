# 08 — Risoluzione dei problemi

## L’analisi si ferma dopo Resolve

Una nuova analisi senza un LLM selezionato si ferma dopo la risoluzione della bibliografia. Apri **Impostazioni** e configura un provider, quindi seleziona almeno un modello per **Jury 1** in **Analisi**. Salva le impostazioni e controlla lo stato delle assegnazioni dei modelli prima di riprovare.

## Un provider o modello locale non è disponibile

Per un provider remoto, verifica che account, chiave, endpoint e autorizzazioni siano ancora validi, salva le modifiche e usa **Controlla**. Il servizio del provider potrebbe non essere disponibile o richiedere un accesso non incluso nel tuo account.

Per un modello locale, avvia il relativo provider e seleziona **Rileva**. Se Callimachus indica che non hai eseguito l’accesso, l’app non offre un controllo di accesso per quel provider. Completa l’accesso nel client supportato dal provider, poi seleziona di nuovo **Rileva**. Un provider rilevato ma non supportato per le giurie non può essere selezionato in Callimachus.

## Credenziali mancanti dopo un aggiornamento o vault non disponibile

L’app compilata 2.0 ignora i vecchi `.env` e non ne migra le credenziali.
Reinserisci le chiavi necessarie nel selettore delle giurie o in Impostazioni e
controlla il provider. Se il vault è bloccato o non disponibile, ripristina
l’accesso a Gestione credenziali di Windows, Portachiavi di macOS o Secret Service
su Linux e riprova. Inserire chiavi in `settings.conf` non aggira l’accesso al
vault. Anche un override dell’ambiente di processo può nascondere le modifiche
salvate: rimuovilo prima di modificare quel valore nell’app. Vedi
[Archiviazione e backup delle impostazioni](03-settings.md#archiviazione-e-backup-delle-impostazioni).

## L’analisi attende una decisione

Se è aperta la revisione Parse, abbina solo gli elementi che riesci a identificare e lascia gli altri irrisolti o ambigui. Usa **Riprendi analisi** per salvare le decisioni e continuare. **Rivedi più tardi** chiude la finestra e lascia l’analisi in pausa, così puoi tornarci da Analisi o Cronologia.

Se è aperto Fetch guidato, controlla il riferimento selezionato e l’identità della fonte prima di confermare. Se ne hai una copia lecita, fornisci il file esatto. Se un PDF richiede OCR, usa **Esegui OCR**; saltarlo rende il testo integrale non disponibile per la verifica. **Procedi / salta Fetch e OCR** registra i recuperi saltati e le identità irrisolte, senza inventare materiale bibliografico.

## Il report è incompleto o l’esito è incerto

Controlla i campi di copertura e completamento nella riga della Cronologia, poi esamina nel report l’ambito delle fonti e le note di revisione. Prove basate su abstract, testo integrale mancante, identità irrisolta o un’affermazione non decidibile possono limitare il risultato anche se l’analisi è completa. Correggi l’input o fornisci materiale bibliografico più solido prima di continuare con una nuova analisi.

## Contatti e assistenza

La sezione **Console** mostra l’output dell’analisi per la diagnostica facoltativa; nell’uso normale non serve inserire comandi. Esamina e anonimizza i log prima di condividerli, perché possono contenere dettagli del documento o delle fonti. Per un problema dell’app o contenuti generati inappropriati, **Info → Segnala contenuti IA** apre un modulo pubblico di segnalazione; non invia nulla automaticamente. Condividi solo una breve descrizione anonimizzata e non includere credenziali o manoscritti privati.

