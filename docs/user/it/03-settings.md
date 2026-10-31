# 03 — Impostazioni e modelli

Apri **Impostazioni** per cambiare le preferenze dell’app e consultare le categorie curate: **Generali**, **Servizi bibliografici**, **Acquisizione** e **Integrità del report**. Le credenziali dei provider e la scelta dei modelli si configurano nel selettore delle giurie. I servizi utilizzabili dipendono dall’app e dal tuo account.

Per cambiare lingua, apri **Impostazioni → Generali → Lingua dell'applicazione** e seleziona una lingua. La scelta viene salvata automaticamente; riavvia Callimachus per applicarla al successivo avvio.

## Configura un provider di modelli

Apri il selettore delle giurie da Analisi e scegli un provider. Il relativo modulo mostra le impostazioni del provider. Inserisci i dati che già possiedi e premi **Salva configurazione**. Le impostazioni del provider sono condivise da Jury 1 e Jury 2: salvare le credenziali non assegna i modelli a nessuna delle due giurie. I campi riservati sono mascherati nell’interfaccia; la conservazione dipende da come viene avviato Callimachus, come descritto sotto.

Un provider remoto può richiedere un account, una chiave API, un piano a pagamento o autorizzazioni specifiche. Callimachus non crea account o chiavi: richiedili al provider e controllane condizioni e costi. Il comando di controllo verifica il provider configurato e può contattarlo.

La ricerca dei modelli locali è separata. Usa il comando di controllo o rilevamento del provider dopo averlo installato o avviato. Se lo stato indica che non hai eseguito l’accesso, Callimachus non offre un controllo di accesso per quel provider: completa l’accesso nel client supportato dal provider, poi torna nell’app e controlla di nuovo. Un provider rilevato ma indicato come non supportato per le giurie non può essere selezionato. Usa **Altri provider** per consultare gli altri servizi supportati.

## Archiviazione e backup delle impostazioni

Le app desktop compilate 2.0 salvano chiavi API, token e password nel vault nativo:
Gestione credenziali di Windows, Portachiavi di macOS o Secret Service su Linux.
Le impostazioni non segrete, come endpoint, modelli ed email di contatto, usano
`settings.conf` nella cartella dati dell’utente; `settings.defaults` è l’inventario
incluso nell’app in sola lettura. Altre preferenze dell’interfaccia possono usare
file separati. Se l’accesso al vault fallisce, l’app segnala un errore senza
ripiegare su file di credenziali in chiaro.

L’avvio dai sorgenti (`python run.py app`) mantiene il file locale `.env`, incluse
le chiavi API in chiaro. Le app compilate ignorano i vecchi `.env` e non li
migrano o eliminano automaticamente. Reinserisci le credenziali necessarie nel
modulo del provider o in Impostazioni, poi controlla il provider. Gli override
dell’ambiente di processo hanno priorità: rimuovili prima di modificare quel
valore nell’app.

Export e import delle impostazioni sono espliciti. Un export può contenere
credenziali; la cifratura con password è facoltativa. Copiare solo `settings.conf`
non copia le voci del vault. L’import sostituisce le impostazioni supportate e
nell’app compilata salva i segreti importati nel vault. Accessi dei client CLI
esterni e sessioni browser rimangono gestiti dai rispettivi client. Vedi
[Guida ai pacchetti desktop (in inglese)](https://github.com/SNatangelo/Callimachus/blob/main/docs/user/en/09-desktop-packages.md#settings-and-credentials-in-20)
per percorsi dei dati e comportamento delle configurazioni precedenti.

## Assegna i modelli alle giurie

Il selettore mostra il conteggio dei modelli scelti per provider e un riepilogo delle assegnazioni correnti. Assegna i modelli in modo indipendente: scegli uno o più modelli per **Jury 1** e, facoltativamente, un insieme diverso per **Jury 2**. Modificare una giuria non cambia l’altra. La severità di Jury 2 si trova nelle **Opzioni** di Analisi.

Il controllo riuscito conferma solo che il provider ha risposto; non garantisce che ogni modello rilevato possa eseguire una giuria. Controlla lo stato prima di iniziare. La verifica di idoneità controlla le giurie selezionate prima dell’avvio della verifica. Senza modelli selezionati, una nuova analisi si ferma dopo Resolve e non avvia Fetch o Verify.

La sezione **Avanzate** del selettore contiene impostazioni come contesto e ragionamento. Queste impostazioni sono condivise tra le analisi, non separate per modello o giuria.

Se l’app richiede una chiave locale di firma, apri **Impostazioni → Integrità del report** e scegli **Genera chiave di firma**. La chiave locale sigilla i report e non viene mostrata nell’interfaccia. Le app compilate la creano o conservano nel vault del sistema operativo; l’avvio dai sorgenti usa un file di firma. I riferimenti espliciti a file già configurati conservano il loro comportamento. Il sigillo locale non fornisce l’autorità esterna separata necessaria per una run audit-ready.


### macOS: recuperare l’accesso dopo un aggiornamento

Callimachus si apre senza chiedere password del portachiavi durante lo splash. Se
le credenziali API salvate richiedono un’autorizzazione, la finestra principale
propone **Recupera credenziali salvate**. Premi **Conferma** per avviare le richieste
native di macOS, oppure **Annulla** per continuare senza quelle credenziali. In ogni
richiesta nativa, **Consenti sempre** (**Always Allow**) ricorda l’accesso per questa
build; un aggiornamento con firma locale può richiederlo di nuovo. Negare l’accesso
interrompe il recupero e non provoca richieste ripetute in background. Puoi riprovare
dalle impostazioni Generali.

Le API key di uno stesso profilo condividono una sola voce protetta del portachiavi,
insieme alla chiave segreta di firma dei report. Il primo recupero di una vecchia
installazione può richiedere un consenso per ciascuna vecchia voce; gli aggiornamenti
successivi accedono alla voce unica. Scegli **Consenti sempre** per ricordare il
consenso: una nuova build con firma locale può comunque richiederne uno nuovo.
Callimachus elimina soltanto le singole vecchie voci recuperate dopo aver verificato
la copia; conserva quelle negate, modificate o dubbie. Le impostazioni ordinarie
restano fuori dal portachiavi.
