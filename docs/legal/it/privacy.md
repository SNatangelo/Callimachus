# Privacy e IA in Callimachus

Aggiornamento: 24 ottobre 2026. Responsabile del progetto: Stefano Natangelo.

## Uso dei dati

Callimachus elabora i documenti che scegli per verificare le citazioni.
Estrazione e OCR incluso avvengono sul computer. Avviare o riprendere un'analisi
autorizza le operazioni online configurate descritte qui. Anche i comandi della
Console possono utilizzare servizi esterni: leggi queste informazioni prima di usarli.

- Risoluzione e recupero inviano query bibliografiche, titoli, autori,
  identificatori e metadati delle richieste a registri scientifici, archivi ed
  editori. I servizi ricevono l'indirizzo IP e possono ricevere l'email di contatto
  configurata per identificare le richieste. Le credenziali autenticano le
  richieste ai rispettivi servizi. Il controllo delle chiavi può contattarli
  all'avvio dell'analisi, anche nella modalità di sole referenze.
- Verify invia affermazioni e passaggi delle fonti nei prompt ai modelli e ai
  backend selezionati. Possono contenere dati personali presenti nei documenti.
  I backend possono essere API remote, CLI esterne autenticate o server
  configurati dall'utente. Sono supportate anche LLM locali: se modello e server
  di inferenza operano interamente sul computer senza inoltrare richieste, i
  prompt della verifica semantica non vengono inviati a un servizio di modelli
  esterno. Un server locale può comunque inoltrare le richieste. Un backend senza
  chiave non garantisce un'elaborazione locale. Le ricerche bibliografiche e il
  recupero possono contattare servizi online anche con inferenza locale.
- Il fetch assistito apre siti esterni nel browser fornito dall'utente. Quei siti
  gestiscono cookie, autenticazione e privacy secondo le proprie regole.
  La cattura conserva i materiali che scegli di fornire.

Conservazione, trasferimenti internazionali, addestramento dei modelli, costi e
requisiti degli account dipendono dal servizio e dagli accordi con esso. Consulta
la sua informativa e l'endpoint prima di abilitarlo. Non inviare materiale
riservato, personale o sensibile senza autorizzazione e i consensi necessari.
Callimachus non vende i documenti né li invia automaticamente al responsabile
del progetto. L'app non include servizi di pubblicità o analisi dell'utilizzo.

## Archiviazione locale e sicurezza

Configurazione, fonti, testo OCR, prompt, risposte, decisioni, report e log della
Console possono restare nelle cartelle locali. Le installazioni native usano
normalmente `%LOCALAPPDATA%/Callimachus` su Windows,
`~/Library/Application Support/Callimachus` su macOS e
`~/.local/share/Callimachus` su Linux. MSIX può virtualizzare i dati di Windows;
percorsi personalizzati possono sostituire quelli predefiniti. Le cache dei
resolver e le impronte delle credenziali non valide possono essere separate.
Nella guida desktop consulta i capitoli Impostazioni e modelli, Risultati e
cronologia e Avvio rapido.

Le app desktop compilate 2.0 salvano chiavi API, token, password e la chiave
locale di firma generata nell’app nel vault nativo dell’utente: Gestione
credenziali di Windows, Portachiavi di macOS o Secret Service su Linux. Se il
vault non è disponibile o è bloccato, l’accesso alle credenziali fallisce senza
ripiegare su file in chiaro. Le impostazioni non segrete, inclusi endpoint,
modelli ed email di contatto, usano `settings.conf`; l’inventario predefinito
incluso nell’app è `settings.defaults`, in sola lettura. Un valore non segreto
può comunque essere personale. Restano validi gli override dell’ambiente di
processo e i riferimenti espliciti a file di firma già configurati; client CLI
esterni e browser gestiscono autonomamente accessi e sessioni.

L’esecuzione dai sorgenti continua a usare `.env` o `.env.local`, dove le chiavi
API configurate sono in chiaro. Le app compilate ignorano i vecchi `.env`, senza
migrarli o eliminarli automaticamente: le credenziali vanno configurate
esplicitamente. La mascheratura è solo un controllo di visualizzazione. I backup
delle impostazioni possono includere credenziali e sono cifrati solo scegliendo
la cifratura con password all’esportazione. Callimachus non cifra gli artefatti
delle run o gli export HTML. Il vault non protegge ogni file locale né isola i
segreti dall’applicazione in esecuzione. Vedi
[Guida ai pacchetti desktop (in inglese)](https://github.com/SNatangelo/Callimachus/blob/main/docs/user/en/09-desktop-packages.md#settings-and-credentials-in-20).
Proteggi account, file e backup con i controlli del sistema operativo e la
cifratura del disco. I servizi ordinari usano HTTPS; endpoint personalizzati e strumenti
esterni seguono la propria configurazione. Non usare endpoint remoti non
affidabili o non cifrati per dati sensibili.

## Controlli disponibili

Scegli provider e credenziali in Settings e seleziona le giurie in Analysis.
Senza LLM selezionate una nuova analisi si ferma dopo Resolve e non invia prompt
ai modelli; continua però a usare i servizi bibliografici online. Per revocare
il permesso durante un'analisi premi Stop. Non è possibile richiamare richieste
già inviate. Non avviare o riprendere altre operazioni online finché non desideri
autorizzarle. Rimuovi le credenziali o cambia provider prima delle operazioni
successive. Una run in pausa può conservare la configurazione precedente:
controllala prima di riprenderla.

Consulta History, la cartella della run e la Console per accedere ai dati.
Puoi eliminare run e voci della cache riutilizzabile nell'app; elimina a parte
configurazione, log, cache esterne e backup non più necessari. Non esiste una
scadenza automatica di conservazione. Richiedi direttamente ai provider la
cancellazione dei dati ricevuti. La disinstallazione dipende dal pacchetto e non
elimina registrazioni dei provider o file esportati.

L'export pubblico rimuove il testo non necessario delle fonti, ma conserva
affermazioni, prove, decisioni e informazioni bibliografiche. Controlla dati
personali e diritti di pubblicazione nell'HTML prima di condividerlo.
L'esportazione non pubblica il file.

## Contenuti generati e segnalazioni

Verdetti e spiegazioni delle LLM sono generati dinamicamente e possono essere
sbagliati o inappropriati. I controlli deterministici limitano le prove, ma non
garantiscono correttezza o sicurezza delle spiegazioni. È uno strumento di ricerca,
non una fonte di consulenza medica, legale o professionale.

Usa **Info → Segnala contenuti IA** o
[contatta il responsabile tramite GitHub](https://github.com/SNatangelo/Callimachus/issues/new)
per segnalazioni su contenuti, privacy o problemi dell'app. Si apre un modulo
nel browser: nulla viene inviato automaticamente. Le issue di GitHub sono
pubbliche; condividi solo una descrizione minima anonimizzata, mai chiavi API,
manoscritti riservati o archivi completi delle run. Alle segnalazioni si applica
la privacy di GitHub e serve un account per inviarle. Il responsabile esamina le
segnalazioni e può correggere o disabilitare il comportamento interessato;
i report locali esistenti non vengono modificati da remoto.

