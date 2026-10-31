# 02 — Analisi

Nella schermata Analisi scegli il manoscritto, imposti la politica delle prove e avvii o riprendi il lavoro.

## Scegli il manoscritto e la modalità

Trascina il manoscritto nell’area dedicata oppure fai clic per scegliere un file. Ogni analisi riguarda un solo manoscritto. Usa **Nuova analisi** per svuotare la schermata Analisi e selezionarne un altro; il run salvato resta in Cronologia.

Scegli la modalità di verifica in base alle prove che vuoi ammettere:

- **Standard** è l’impostazione abituale. Cerca il testo integrale e può usare un abstract se il testo integrale non è disponibile; il report conserva l’indicazione di questa limitazione.
- **Maximus** richiede il testo integrale per la verifica semantica. Se manca, la citazione resta non verificata anziché essere valutata usando un abstract.
- **Abstract** non cerca il testo integrale. Considera titolo e abstract autentico come ambito finale delle prove.
- **Standard web** è un vecchio nome per Standard. Le pagine web generiche di terzi non costituiscono prove della citazione.

Apri **Opzioni** per mostrare il pannello delle opzioni di Analisi. Contiene la modalità di verifica, la severità di Jury 2 e le opzioni del flusso di lavoro. Puoi chiudere il pannello senza modificare una pipeline in esecuzione.

I livelli di severità per Jury 2 — **Bassa**, **Media** e **Alta** — regolano il modo in cui il secondo giudice gestisce un rifiuto o una risposta incerta; con impostazioni più severe possono restare più risultati incerti. Non sostituiscono i controlli sull’identità della fonte o sulle prove.

## Seleziona i modelli

Puoi selezionare uno o più modelli per **Jury 1**. Jury 2 è facoltativa e le sue assegnazioni sono indipendenti da Jury 1. Selezionare o salvare un modello per una giuria non lo assegna all’altra. Il selettore mostra i modelli scelti per ciascuna giuria e il relativo stato. Configura provider e modelli in [Impostazioni e modelli](03-settings.md).

Senza un modello selezionato, una nuova analisi si ferma dopo la risoluzione della bibliografia: non avvia il recupero né la verifica. Può servire per un controllo iniziale della bibliografia, ma non è una verifica completa delle citazioni.

## Prepara l'accesso istituzionale

Prima di una nuova analisi, la finestra facoltativa di accesso può aprire più pagine di editori insieme. Seleziona gli editori che ti servono: le schede condividono una sessione browser salvata e gli accessi ai diversi siti possono coesistere. Scegli l'ateneo e completa il login direttamente nei siti degli editori, poi scegli **Finito**. Aprire una scheda non conferma da solo l'accesso agli articoli.

**Sessione browser salvata** identifica i dati del browser che conservano gli accessi. Normalmente puoi lasciarla invariata e usare la stessa sessione per tutti gli editori. La finestra non richiede il nome dell'ateneo né l'indirizzo della biblioteca.

Fetch riutilizza la sessione nei percorsi di recupero supportati, anche quando un DOI reindirizza alla pagina dell'editore, e controlla comunque identità e contenuto dei documenti acquisiti. L'accesso dipende dagli abbonamenti dell'ateneo. Puoi proseguire senza prepararlo; una successiva richiesta di login non interrompe il Fetch ordinario.

## Avvia, interrompi o salta le revisioni

Premi **Avvia analisi**. La schermata iniziale di Analisi controlla solo se esistono file di run; scegliendo **Riprendi ultima** viene cercata l’analisi leggibile aggiornata più di recente. Durante un’analisi attiva, il comando di interruzione richiede l’arresto graduale. **Nuova analisi** diventa **Interrompi e svuota**; al termine del processo svuota la schermata Analisi, mentre l’analisi salvata resta in Cronologia.

Due opzioni permettono di saltare le pause manuali:

- **Salta revisione manuale di citazioni e bibliografia** salta l’abbinamento delle citazioni ambigue, la revisione delle fonti nelle note e quella dell’identità bibliografica dopo Parse.
- **Salta Fetch assistito e OCR** mantiene attivi recupero e verifica automatici, ma rinuncia alle attività Fetch manuali e all’OCR in sospeso. Una fonte che richiede una decisione d’identità resta non verificata; senza testo OCR non si può valutare il contenuto integrale del PDF.

Lascia queste opzioni disattivate se vuoi risolvere personalmente gli elementi. Vedi [Revisioni e fonti](04-reviews-and-sources.md) per i dettagli.
