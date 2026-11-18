# ADR 0004: piano del passo 2 dell'ADR 0001 (budget, prenotazione e stato durevole)

- **Stato:** approvato dall'operatore con il merge di PR #343 (2026-11-18), accettando le raccomandazioni della sezione 5. Le decisioni riservate sono state prese il 2026-11-18 (vedi la [sezione 5](#5-decisioni-riservate-alloperatore)); quelle rimaste aperte sono indicate lì. Il resto del documento è il registro della proposta: le alternative e l'analisi restano come scritte prima dell'approvazione.
- **Fase:** design (#326). È il passo 2 del [piano dell'ADR 0001](0001-prodotto-unico-fasi-su-tracker.md#piano-ordine-rivisto-dal-conclave): "Budget, prenotazione e stato durevole (vincoli 2 e 3), sulla base di #133. Comprende l'identità di storia, fase e tentativo". Il passo 1, le porte del tracker (#138), è su `main`.
- **Data:** 2026-11-18.
- **Base verificata:** il commit 2002ee3 di `main`.

## Contesto

- I vincoli 2 e 3 dell'[ADR 0001](0001-prodotto-unico-fasi-su-tracker.md#vincoli) chiedono, prima di qualsiasi run reale con le fasi:
  - budget e prenotazione, con un intento durevole prima di ogni chiamata al modello, blocco sugli esiti incerti e budget cumulativi attraverso fasi, issue figlie, retry e riavvii;
  - consumi non dichiarati rifiutati;
  - stato durevole dei tentativi, identità stabili di storia, fase e tentativo, e pubblicazione riprendibile, con l'esecutore che salva ricevuta e output prima di pubblicare sul tracker.
- Oggi queste garanzie esistono **nel motore locale** (`LimitsStore`, journal, outbox), ma non in devloop, che tiene il suo stato su GitHub e non ha budget.
- #163 (budget per task e riallocazione) e #214 (telemetria B2) sono fermi su decisioni di progetto che questo ADR analizza.

Questo ADR contiene solo analisi e proposta: non modifica codice né altri documenti. Le scelte che spettano all'operatore restano **proposte con alternative** (vedi [Decisioni riservate all'operatore](#5-decisioni-riservate-alloperatore)). Le issue di implementazione nascono solo dopo l'approvazione, come per l'[ADR 0003](0003-struttura-della-documentazione.md#3-piano).

## Metodo

- Ogni affermazione sullo stato attuale cita il codice nella forma file:riga, al commit 2002ee3, oppure la issue o la PR che l'ha introdotta, ricavata dai riferimenti `Refs #N` dei commit.
- Lo stato delle issue è quello di `main`: una issue è "su `main`" se il merge della sua PR è nella storia del commit di base. Il testo delle issue non è stato riletto su GitHub: valgono il corpo di #326 e i messaggi dei commit.
- Vale il vincolo 1 dell'ADR 0001: le proposte non tolgono né sostituiscono garanzie del motore locale prima della parità.

## 1. Stato attuale

### devloop

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Stato su GitHub | Lo stato del dispatcher sta solo su GitHub, in label e marker nei commenti: un tick può fermarsi in qualsiasi punto e il successivo riprende da lì | devloop/dispatcher.py:3-4 |
| Ledger del dispatcher | Commenti firmati con l'identità del dispatcher, chiusi da un marker `ledger`: record `request`, `turn`, `verification` ed `event`. I turni di un round di review e il suo `ci_round` stanno in un unico registro di round (#325) | #133; devloop/ledger.py:1-31, :88-114; [protocollo](../devloop/protocol.md#evidence-and-telemetry) |
| Snapshot della richiesta | Alla presa in carico (`agent:ready` → `agent:working`): hash di titolo e corpo, chi ha messo la label e quando, commenti fidati passati all'implementer | devloop/ledger.py:57-60, :239-247; devloop/dispatch_actions.py:111 |
| Persistenza dei turni | Il record `turn` si scrive come `open` **prima** di avviare la CLI; se la scrittura fallisce il turno non parte. Prima ancora il numero dell'issue o della PR viene annotato in `<state_dir>/ledger-open-turns.json`, indipendente dalle label. Alla fine il record si chiude con esito e telemetria | devloop/ledger.py:48, :143-151, :249-285; devloop/dispatcher.py:1503-1525, :1785-1851 |
| Turni rimasti aperti | Il tick successivo li registra nel log e li marca `incomplete`. Il turno incompleto **non entra nella pianificazione**: il passo si ripianifica da label e marker. Fa eccezione l'implementazione interrotta senza PR, che va in escalation (`interrupted`) | devloop/dispatcher.py:1620-1657, :2158-2162; devloop/ledger.py:315-320 |
| Eventi | `ci_round` (i check visti da un round, o quelli riletti prima della promozione) e `sync` senza agent | #133; devloop/ledger.py:305-313; devloop/dispatcher.py:1579, :1616 |
| Telemetria dei turni | Quello che devloop legge dall'output della CLI: costo stimato e token di claude, token di codex. Per agy non legge ancora niente, anche se l'envelope JSON riporta i token (#358). Un valore non attestato è `null`, mai 0 | devloop/agents.py:285-286; devloop/ledger.py:28-30 |
| Ricevuta della PR | Alla promozione, costruita solo dai record del ledger e dai marker, legata alla head approvata, marcata `superseded` a ogni revoca | #135; devloop/receipt.py:1-24 |
| Limiti | Numerici e di tempo: `max_open_prs`, `max_review_rounds`, `max_actions_per_tick`, timeout dei turni e dei test. **Non esiste un budget di costo, una prenotazione o una dichiarazione dei consumi ignoti** | devloop/config.py:263-280 |
| Identità del lavoro | Numero dell'issue, numero e head della PR, numero del round, hash della richiesta. **Non esistono identità di storia, fase o tentativo**: un'issue ha al più una PR devloop aperta | devloop/ledger.py:57-60; devloop/planning.py:805 |
| Porta del tracker | `comment` restituisce l'id del commento, oppure `None` se la risposta non lo contiene; un errore di `gh` è un `GitHubError`. **Non c'è un esito incerto distinto** dal fallimento | #138; devloop/ports.py:98-105; devloop/github.py:444-462 |
| Confine | devloop importa solo la stdlib e sé stesso. Il pacchetto `core` previsto dall'ADR 0001 non esiste ancora | tests/test_devloop_boundary.py:22-31; [architettura](../architecture.md#future-core-package) |

### Motore locale

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| `LimitsStore` | Profilo dei limiti congelato in `limits/policy.json` e legato a ticket, base e controller; journal con hash concatenati come autorità di prenotazioni e consumi; marker `execution-mode.json` da `INTENT` ad `ACTIVE` | ssdlc_recovery.py:99-150; [Bounded recovery](../bounded-recovery.md#activation-and-limits) |
| Prenotazione prima della chiamata | Ogni chiamata esterna passa da `_supervised_dispatch`: ammissione della route (#165), prenotazione (`reserve`), intento del supervisore (`dispatch_started`), poi l'avvio. L'esito si riconcilia una volta sola: `returned`, `terminated`, `uncertain` o `not_dispatched` | orchestrator.py:430-517; ssdlc_recovery.py:392-426 |
| Esiti incerti | Una chiamata `reserved` o `uncertain` blocca ogni nuova chiamata con `OUTCOME_UNCERTAIN`. Un retry di trasporto è ammesso solo con la prova che la chiamata non è partita | ssdlc_recovery.py:245-251, :428-448 |
| Consumi ignoti (#43) | `live_unknown_usage` dichiara per motore `reserve_per_invocation` (riserva positiva e finita, prenotata prima della chiamata) oppure `unmetered_quota`. Senza regola, codex e agy sono rifiutati prima dell'avvio, e una chiamata tornata senza costo blocca la successiva | #43; ssdlc_recovery.py:16-35, :262-265, :379-389 |
| Controllo preventivo (#128) | La stessa validazione della prenotazione gira prima di salvare stato: prima dell'intento di feedback e prima che lo scheduler consumi un tentativo | #128; ssdlc_recovery.py:353-390; orchestrator.py:4494 |
| Ammissione di un profilo esistente | `live-usage-profile-admission-v1` aggiunge regole a un profilo attivo senza cambiare contatori, tetti o scadenza, con l'elenco esatto delle righe storiche non coperte | #43, #128; ssdlc_usage_admission.py:163-182 |
| Telemetria dei costi (#164) | Schema `ssdlc.cost-telemetry` v1: addebiti, stime, token e quote tenuti separati, `unknown` mai 0 | #164; ssdlc_telemetry.py:28-29; [Cost telemetry](../cost-telemetry.md) |
| Tetti separati e route (#165) | `SSDLC_BUDGET_CAP_USD` per la spesa misurata, `SSDLC_ESTIMATE_CAP_USD` per le stime; route ammessa prima della prenotazione; i CLI in abbonamento escono dai tetti in dollari solo con una dichiarazione esplicita | #165; orchestrator.py:257; [Cost telemetry](../cost-telemetry.md#separate-caps-and-route-admission) |
| Pubblicazione riprendibile | `ProjectionOutbox`: journal locale con `PREPARED`, `DISPATCH_RESERVED` e `SENT`; prima di un nuovo POST riconcilia sul commento esatto con lo stesso autore. L'assenza del commento non autorizza un altro POST | ssdlc_feedback_delivery.py:127-215 |
| Generazioni | Una generazione I2 selezionata (`generation.json`, `transition_id`) è la forma attuale di "tentativo" del motore locale, con limiti e contatori propri | ssdlc_recovery.py:230-236; [Manual limit extensions](../limit-extensions.md#eligible-runs) |

### Limiti a runtime e telemetria: stato delle issue

| Issue | Contenuto | Stato su `main` |
|---|---|---|
| #43 | `live_unknown_usage`: nessuna chiamata senza costo contata come zero | Su `main` (PR #108) |
| #128 | Controllo preventivo della chiamata imminente; ammissione con feedback | Su `main` (PR #150) |
| #164 | Schema condiviso della telemetria dei costi (parte A di #14) | Su `main` (PR #191) |
| #165 | Tetti separati per spesa e stime, ammissione per route (parte B1 di #14) | Su `main` (PR #225) |
| #162 | Contratto comune delle estensioni manuali di limiti e scadenza (`operator-limit-extension-v1`), solo con autorizzazione puntuale dell'operatore | Su `main` (PR #329); ssdlc_limit_extensions.py:41-44; [Manual limit extensions](../limit-extensions.md) |
| #163 | Budget per task e riallocazione | **Non avviata:** attende le decisioni della [sezione 3.5](#35-budget-per-task-e-trasferimenti-domande-di-163) |
| #166 | Letture delle quote in abbonamento e controlli espliciti (parte C di #14) | Su `main` (PR #330); [quote](../cost-telemetry.md#subscription-quota-snapshots-and-controls) |
| #167 | Riverifica dello stesso candidato dopo un timeout del verificatore | Su `main` (PR #332) |
| #168 | Scadenza esplicita della registrazione del feedback e autorizzazioni dipendenti | Su `main` (PR #333) |
| #214 | Telemetria B2: transizioni dei profili esistenti e tetti in crediti | **In decisione:** due domande riservate all'operatore, [decisione D6](#5-decisioni-riservate-alloperatore) |

#133 (ledger), #134 (mappa dei criteri di accettazione) e #135 (ricevuta della PR) sono su `main` (PR #154, #157 e #328).

### Misure disponibili per agent

Cosa si può misurare di un turno, per agent. Dove manca una misura non c'è un valore da usare: è `null`, mai 0.

| Agent | USD | % di quota | Token |
|---|---|---|---|
| claude | stima (`total_cost_usd`), non un addebito | non leggibile in modalità headless | sì |
| codex | no | solo delta dell'intero account, mai per chiamata | sì |
| agy | no | solo delta dell'intero account | sì, riportati dall'envelope JSON; devloop non li legge ancora (#358) |

Fonti:

- USD e token nei turni di devloop: devloop/agents.py:285-286 (campi della telemetria) e :298-309 (cosa legge oggi devloop da ciascuna CLI: claude costo stimato e token, codex solo token, agy niente).
- USD per route nel motore locale: #164 e #165, dove claude ha solo una stima e codex e agy nessun costo ([misure per route](../cost-telemetry.md#measures-required-per-route)).
- Quote: #166. Il delta è un'osservazione sull'intero account, dove possono lavorare altri client, e `call_consumption` è sempre `null`; claude non ha una quota leggibile ([lettura](../cost-telemetry.md#reading-a-quota), [delta](../cost-telemetry.md#deltas)).
- Token di agy: devloop chiama già agy in print mode con `--output-format json` (devloop/agents.py:373-374), e in quella modalità l'envelope riporta `duration_seconds`, `num_turns` e una `usage` con `input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens` e `total_tokens` (verificato con agy 1.5.11). L'adattatore del motore locale legge già questa `usage` ([adattatori](../cost-telemetry.md#adapters)); devloop oggi la ignora, e #358 la leggerà. I token di agy sono quindi **disponibili, ma oggi non letti** da devloop.
- Semantica dei token di agy: `input_tokens` **esclude** la cache, che sta a parte in `cache_read_tokens`, mentre l'`input_tokens` di codex la include (devloop/agents.py:306-307). La `usage` è cumulativa per conversazione, e devloop ne apre una nuova a ogni turno. È un motivo in più per non confrontare né sommare token di agent diversi.

Ne segue che le uniche misure valide per tutti gli agent sono quelle che devloop fa da sé, senza fidarsi della CLI: il **numero di turni** e il **tempo**. È la base della regola della [sezione 3.3](#33-modello-di-prenotazione-del-budget).

## 2. Lacune rispetto ai vincoli 2 e 3

| # | Lacuna | Vincolo dell'ADR 0001 | devloop oggi | Motore locale oggi | Cosa manca |
|---|---|---|---|---|---|
| L1 | Budget e prenotazione prima di qualsiasi run reale con le fasi | 2 | Nessun budget di costo; solo limiti di round, azioni e tempo (devloop/config.py:263-280) | Prenotazione per chiamata sotto lock, prima dell'avvio (ssdlc_recovery.py:392-401) | Un budget **per storia**, cumulativo attraverso fasi, issue figlie, tentativi, retry e riavvii, con prenotazione prima di ogni turno di devloop. Oggi un'issue figlia, un fallback di modello o un nuovo tick partono senza alcun conto |
| L2 | Consumi non dichiarati rifiutati | 2 | La telemetria si registra ma non decide niente: di agy devloop non legge ancora niente (i token ci sono, #358), di codex solo i token, di claude una stima (devloop/agents.py:285-286). Nessun turno viene rifiutato per consumo ignoto | Rifiutati senza `live_unknown_usage` o `SSDLC_UNMETERED_QUOTA` (ssdlc_recovery.py:262-265, :379-382) | Una dichiarazione per agent e per route in devloop, con lo stesso significato di `live_unknown_usage`, e il rifiuto prima dell'avvio di un turno non coperto |
| L3 | Stato durevole dei tentativi e degli intenti | 2 e 3 | Il record `turn` aperto prima della CLI è un intento, ma sta su GitHub. Un turno rimasto aperto diventa `incomplete` e **non blocca** la ripianificazione: review, fix e sync ripartono da label e marker (devloop/dispatcher.py:1620-1657). Nessun esito `uncertain` | Journal locale; `OUTCOME_UNCERTAIN` blocca ogni nuova chiamata finché l'esito non è riconciliato (ssdlc_recovery.py:250-251) | Un journal durevole degli intenti in devloop, con esiti distinti e blocco della ripetizione sullo stesso tentativo finché l'esito incerto non è riconciliato |
| L4 | Pubblicazione riprendibile, con ricevuta e output salvati prima di pubblicare | 3 | L'output dell'agent e i record si pubblicano direttamente; `comment` non distingue un esito incerto e può restituire `None` (devloop/github.py:444-452). Una ripetizione dopo un timeout può duplicare un commento | `ProjectionOutbox` per le proiezioni del feedback (ssdlc_feedback_delivery.py:127-215), non per gli altri output | Un outbox in devloop: record, output e ricevute scritti su disco prima della pubblicazione, riconciliati sul commento esatto prima di un nuovo POST. Nella porta, un esito incerto distinto (ADR 0001, "Porta del tracker") |
| L5 | Identità di storia, fase e tentativo | 3 | Solo numeri di issue e PR, round e head; un'issue ha al più una PR devloop aperta | Ticket e generazioni I2 (ssdlc_recovery.py:230-236), senza legame con il tracker | Identità stabili, durevoli e verificabili di storia, fase e tentativo, la chiave `(sha del design approvato, task-id)` per le unità generate da un piano, e il legame fra turni, tentativi e budget |

## 3. Struttura di destinazione

Ogni area ha alternative e una raccomandazione. Le raccomandazioni sono proposte. Diventano decisioni con l'approvazione di questo ADR, salvo le parti elencate nella [sezione 5](#5-decisioni-riservate-alloperatore), che restano dell'operatore.

### 3.1 Dove vive lo stato durevole

**Alternativa A: solo il ledger su GitHub.** Budget, intenti e tentativi diventano nuovi record `ledger`, come i turni di oggi.

- Pro: un solo posto, già firmato con l'identità del dispatcher e visibile all'operatore; nessuno stato nuovo sull'host.
- Contro:
  - un commento è modificabile e non ha un'operazione atomica di confronto e scrittura: due tick o due azioni parallele non possono prenotare in modo serializzato senza un lock locale, che oggi già serve per i registri di round (devloop/ledger.py:110-114);
  - una chiamata a GitHub fallita o incerta impedisce di registrare l'intento, o lo registra due volte;
  - viola la lettera del vincolo 3, che chiede di salvare su disco **prima** di pubblicare;
  - lega il nucleo a GitHub, mentre il passo 5 prevede un backend filesystem.

**Alternativa B: solo file locali.** Un journal locale con hash concatenati, come `limits/` del motore locale, è l'unica fonte. GitHub riceve solo testi leggibili.

- Pro: atomicità e lock locali; riuso del modello già provato da `LimitsStore` e `TaskJournal`.
- Contro:
  - l'operatore e i reviewer perdono la traccia sul tracker, contro il principio 2 dell'[ADR 0002](0002-ruoli-e-responsabilita.md#principi) ("tutto passa dal tracker");
  - un journal locale dà solo integrità, non provenienza (README, "Integrity checks are not authentication");
  - la perdita dell'host perde lo stato.

**Alternativa C: entrambi, con ruoli distinti.** Il journal locale in `state_dir` è l'**autorità** per intenti, prenotazioni, esiti e tentativi. Il ledger su GitHub ne è la **proiezione pubblicata** attraverso un outbox, con riconciliazione sul commento esatto, come `ProjectionOutbox`.

- Pro:
  - soddisfa il vincolo 3 alla lettera;
  - conserva la traccia firmata sul tracker e la ricevuta della PR (#135), che continuano a leggere il ledger;
  - si adatta al backend filesystem (passo 5), dove la proiezione diventa un altro adattatore.
- Contro:
  - due rappresentazioni da tenere coerenti, con una riconciliazione da progettare e testare;
  - lo stato sull'host diventa critico: va protetto da cancellazioni e rollback come `limits/` (ssdlc_recovery.py:137-138).

**Raccomandazione: C.**

- La divergenza fra le due rappresentazioni si risolve sempre a favore del journal locale. Una proiezione mancante si ripubblica; una proiezione che contraddice il journal è un errore d'integrità e va in escalation, come fa oggi la ricevuta con `receipt_conflict` (devloop/receipt.py:37).
- Il journal locale **sostituisce** `<state_dir>/ledger-open-turns.json` come traccia dei turni aperti.
- Le scritture di oggi su GitHub restano invariate nel contenuto. La regola "se il record del turno non si scrive, il turno non parte" (devloop/ledger.py:249-255) resta, applicata all'intento locale.
- Il journal sta nel `state_dir` dell'installazione fissata, fuori dai worktree degli agent.

### 3.2 Identità di storia, fase e tentativo

**Storia.** È l'unità di budget: "una nuova issue non azzera il budget della storia" (vincolo 2).

- (a) Il numero dell'issue radice. È semplice, ma vale solo su GitHub e non lega le issue figlie.
- (b) Un identificatore assegnato dal dispatcher alla presa in carico della radice, scritto nel journal e in un marker del dispatcher sulla radice e su ogni issue figlia.
- (c) L'hash della richiesta approvata (devloop/ledger.py:57-60). È verificabile, ma cambia con ogni revisione del testo.

**Raccomandazione: (b),** con il riferimento del tracker (repository e numero della radice) e l'hash della richiesta al momento dell'approvazione come attributi. Un'issue figlia eredita la storia solo dal marker scritto dal dispatcher, mai dal testo di un agent o di un autore non fidato.

**Fase.** È la tripla (storia, tipo di fase, revisione approvata a monte). La revisione è lo SHA del commit approvato o l'hash dell'artefatto, come chiede la catena fra fasi del vincolo 8. Al passo 2 esiste solo la fase "implementazione" del flusso attuale. Le altre fasi arrivano al passo 3, ma l'identità deve già prevederle.

**Tentativo.** È una sequenza monotona per fase, assegnata nel journal locale: (fase, n). I turni (implement, fix, sync, review) appartengono a un tentativo. Round e fix sono turni dello stesso tentativo, non tentativi nuovi. Le unità generate da un piano portano la chiave `(sha del design approvato, task-id)` del vincolo 3, e su quella chiave si riconcilia alla ripresa.

Come rappresentare più tentativi sullo stesso lavoro: la tabella "Cosa si perde e come si recupera" dell'ADR 0001 lo rimanda al passo 2.

- (i) **Un branch e una PR per tentativo**, con l'identità del tentativo nel nome del branch e nel marker della PR. Il primo tentativo mantiene il branch di oggi (`branch_prefix` più il numero dell'issue).
- (ii) **Tentativi come round della stessa PR.** Non cambia niente, ma non rappresenta varianti alternative: un nuovo tentativo cancella il precedente.
- (iii) **Branch senza PR fino alla scelta.** Le varianti restano locali e se ne pubblica una sola. Conserva il modello "due run separate" della perdita accettata (ADR 0001, decisione 4), ma lascia le varianti fuori dal tracker.

**Raccomandazione: (i),** con **un solo tentativo attivo per fase** finché non ci sono il contenimento e il fencing del passo 4 (vincolo 5). Un nuovo tentativo nasce solo dopo che il precedente è chiuso o abbandonato con un esito registrato, e consuma lo stesso budget della storia.

### 3.3 Modello di prenotazione del budget

**Alternativa A: solo tetti a consuntivo.** Si osserva il consumo e ci si ferma al raggiungimento del tetto. Oggi i tetti in dollari funzionano così per le chiamate con costo noto (docs/bounded-recovery.md, righe 60-68).

- Pro: semplice.
- Contro: non limita una chiamata dal costo ignoto, e con agy e codex in devloop il costo è sempre ignoto. Da solo non soddisfa il vincolo 2.

**Alternativa B: prenotazione per turno con riserva conservativa.** È il modello di `LimitsStore`: prima di avviare un turno si registra l'intento e si prenota. Per ogni agent e route vale una regola dichiarata, `reserve_per_invocation` oppure `unmetered_quota`. Restano sempre in vigore i tetti di numero (turni per storia, per fase e per ruolo) e di tempo.

- Pro:
  - è il modello già provato nel motore locale (ssdlc_recovery.py:353-401), con le stesse regole: una riserva resta addebitata e un costo osservato la può solo alzare;
  - senza regola il turno è rifiutato prima dell'avvio (L2);
  - i limiti di numero e di tempo restano il limite rigido, come nel motore locale.
- Contro:
  - le riserve sono stime dell'operatore, non costi reali, e un budget troppo stretto ferma il lavoro;
  - per un agent che non esporta costi (codex e agy, vedi [Misure disponibili per agent](#misure-disponibili-per-agent)) una `reserve_per_invocation` in USD è una **stima inventata** da chi la imposta: nessuna misura la conferma né la smentisce, e un costo osservato non arriva mai ad alzarla.

**Alternativa C: ammissione sulle quote dell'abbonamento.** Si legge la quota prima del turno, come i controlli di #166.

- Pro: misura qualcosa di reale per agy e codex.
- Contro: il delta di quota è un'osservazione sull'intero account, mai il consumo della chiamata (`call_consumption` è sempre `null`); claude non ha quota leggibile. Come modello di budget non basta.

**Raccomandazione: B, con C come controllo facoltativo.**

- Il budget è un **envelope per storia**. Le sue dimensioni seguono le [misure disponibili per agent](#misure-disponibili-per-agent):
  - le **sole dimensioni universali** sono quelle che devloop misura da sé, senza fidarsi della CLI: il **numero di turni**, totale, per ruolo e per tipo di fase, e il **tempo**. Valgono per tutti gli agent;
  - i **dollari** valgono solo dove sono attestati: la stima di claude e le API a consumo, con tetti separati per spesa misurata e stime, come in #165;
  - i **token** sono un tetto opzionale proprio di ciascun agent, **mai sommati né confrontati fra agent diversi**: modelli diversi hanno prezzi e quote diversi, e i token non hanno la stessa semantica (per agy `input_tokens` esclude la cache, per codex la include). Valgono per claude e codex, che devloop legge già, e per agy dopo #358;
  - per **agy** valgono turni e tempo, con la dichiarazione `unmetered_quota`, **e**, dopo #358, il tetto opzionale di token dell'agent, come per claude e codex.
- Per agy e codex la regola raccomandata è quindi `unmetered_quota` più i tetti di turni e, se l'operatore li fissa, di token dell'agent, non una `reserve_per_invocation` in USD, che sarebbe una stima inventata. Il valore dei tetti e la regola per ciascun agent restano decisioni dell'operatore (D2).
- Fasi, issue figlie, tentativi, retry, fallback di modello e riavvii consumano lo stesso envelope. Niente rimborsa una prenotazione.
- Un turno rimasto aperto o incompleto diventa un esito `uncertain` nel journal. Resta addebitato e blocca un nuovo turno **dello stesso tentativo** fino alla riconciliazione, che decide l'operatore o, quando ci sarà, la prova di terminazione del passo 4. È il comportamento di `OUTCOME_UNCERTAIN` (ssdlc_recovery.py:250-251) applicato a devloop. Si chiude così la lacuna L3: niente redispatch alla cieca.
- I controlli sulle quote di #166 si possono abilitare per route, ma solo come rifiuto aggiuntivo, mai come conteggio.
- Le estensioni dell'envelope restano dell'operatore, caso per caso, senza delega (ADR 0001, decisione 5). Il contratto `operator-limit-extension-v1` di #162 è il modello da seguire.
- I valori dell'envelope, le riserve, le dichiarazioni `unmetered_quota` e l'attivazione sono **decisioni riservate** (D2, D3 e D8).

### 3.4 Riuso del pacchetto `core` e confine di devloop

Vincolo 6 e decisione 1 dell'ADR 0001: devloop può riusare codice di ssdlc solo attraverso un pacchetto `core` dichiarato, aggiunto all'allowlist di `tests/test_devloop_boundary.py`. Valgono tre condizioni: solo validatori puri, prompt e schemi come dati, mai import di `ssdlc_*`. Il vincolo 1 tiene invariato il motore locale fino alla parità, e `tests/test_recovery_limits.py` è fissato dalla baseline del replay gate (`schemas/recovery-grader-baseline.json`).

**Alternativa A: devloop reimplementa tutto da sé.** Il confine resta com'è, ma la logica di budget esiste due volte e può divergere senza che un test se ne accorga.

**Alternativa B: un `core` nuovo, con funzioni pure scritte accanto al motore.** Il motore non cambia. Cosa va in `core`:

- la validazione del profilo dei limiti (`schemas/recovery-limits.schema.json` letto come dato) e delle regole sui consumi ignoti (oggi in ssdlc_recovery.py:23-35);
- il calcolo dell'addebito e della capienza su uno stato dato (oggi in ssdlc_recovery.py:268-302 e :357-390), senza I/O e senza lock;
- la classificazione della telemetria in spesa, stima e token.

Test di equivalenza, fuori dal confine, confrontano `core` e il motore sugli stessi casi. Journal, lock e outbox restano in devloop, che li implementa con la sola stdlib.

**Alternativa C: estrarre il codice dal motore in `core` e far importare `core` al motore.** Elimina il duplicato, ma cambia il motore, cioè il candidato sotto test del replay gate, prima della parità. Rischia di toccare file fissati dalla baseline, e il vincolo 1 dice che grader e baseline non si toccano nella stessa PR che cambia il motore.

**Raccomandazione: B.**

- `core` contiene solo funzioni pure, senza stato e senza I/O, e viene aggiunto all'allowlist con la decisione 1 già presa.
- La convergenza del motore su `core` è lavoro del passo 6.
- Il journal con hash concatenati **non** va in `core` in questo passo: è I/O con stato, anche se non è un ciclo. Se convenga ammetterlo come primitiva di `core` è una domanda per l'operatore (D7).

### 3.5 Budget per task e trasferimenti: domande di #163

Riguardano il motore locale (profili congelati e I2). Al passo 2 il motore resta invariato nelle garanzie (vincolo 1). Un trasferimento di budget, però, cambia come si consuma un limite già congelato, quindi serve una decisione di progetto prima di #163.

**1. Chi può autorizzare un trasferimento.**

- (a) **Solo l'operatore, con un'autorizzazione puntuale** legata all'hash esatto della proposta e con scadenza, come le estensioni di #162 (parte A).
- (b) **Una delega al supervisore con perimetro limitato:**
  - solo fra task della stessa run;
  - senza alzare il totale della run;
  - con un massimo per trasferimento e per run fissato dall'operatore nel profilo;
  - revocabile, e registrata come atto del supervisore.
- (c) **Regole automatiche nel profilo,** per esempio "il residuo di un task completato torna alla run".

La decisione 5 dell'ADR 0001 esclude la delega per le **estensioni**. Un trasferimento che non alza il totale non è un'estensione in senso stretto, ma sposta autorità di spesa fra task.

**Raccomandazione: (a)** finché l'operatore non decide altrimenti, riusando il contratto di #162 con una nuova operazione. (b) e (c) richiedono una decisione esplicita che precisi il perimetro della decisione 5.

**2. Quali dimensioni sono trasferibili, e con quale semantica.**

- `development_attempts` oggi è un **tetto per task**, non un plafond cumulativo della run (orchestrator.py:4513; ssdlc_feedback_roles.py:114).
- `wall_seconds` è una **scadenza assoluta** della run, che comprende preflight, esecuzione e attese dell'operatore (ssdlc_recovery.py:146-148, :238-249; [Bounded recovery](../bounded-recovery.md#live-calls-without-a-reported-cost-issue-43), paragrafo sulla scadenza).

Alternative:

- (a) **Solo le dimensioni cumulative:** `invocations`, `role_invocations`, `phase_invocations`, `metered_usd`, `cli_estimate_usd`.
  - `development_attempts` resta un tetto per task: alzarlo per un task è un'estensione, non un trasferimento.
  - `wall_seconds` non è trasferibile e si sposta solo con `extend_deadline` di #162.
- (b) **Tutte le dimensioni, trasformate in plafond.** `development_attempts` diventa un plafond della run e `wall_seconds` un tempo per task. È un cambio di semantica del profilo: serve una nuova versione dello schema, e i profili congelati non si possono reinterpretare.

Regole di consumo e impegno, valide in entrambe:

- un trasferimento sposta solo il residuo **né consumato né prenotato** del task sorgente;
- le prenotazioni aperte o incerte restano al sorgente;
- un consumo non torna mai disponibile, come oggi nessun cambio di provider, riavvio o riparazione fallita rimborsa una prenotazione (docs/bounded-recovery.md, righe 34-37).

**Raccomandazione: (a).**

**3. Allocazione sulle run esistenti.**

- (a) **Plafond cumulativi per task.** Ogni task ha un tetto sul proprio consumo cumulativo e la somma dei tetti non supera il totale della run, che resta comunque il limite.
- (b) **Quote della disponibilità residua.** Al momento dell'allocazione, il residuo della run si divide fra i task rimasti.

In entrambi i casi alcuni consumi non sono attribuibili a un task: design, piano, diagnosi, valutazioni dei gate e chiamate condivise della run. Due modi di trattarli:

- (i) un **conto condiviso della run**, sempre contato nel totale e mai trasferibile ai task senza l'operatore;
- (ii) una ripartizione proporzionale fra i task.

**Raccomandazione:**

- (a) per i profili nuovi, dichiarato nel profilo e incluso nel suo hash;
- (b) per le run esistenti, solo con una transizione esplicita sul modello di `live-usage-profile-admission-v1`. La transizione calcola le quote dal residuo, elenca il consumato per task e il consumo non attribuibile, e non riattribuisce niente al passato;
- (i) per i consumi non attribuibili: una ripartizione proporzionale inventerebbe un'attribuzione che la telemetria non sostiene.

### 3.6 Telemetria B2 (#214)

#214 è **in decisione**. Il suo controllo di prontezza ha sollevato due domande sul budget, che questo ADR riporta con le alternative e senza deciderle ([decisione D6](#5-decisioni-riservate-alloperatore)). Oggi un profilo congelato tiene i propri tetti `metered_usd` e `cli_estimate_usd`, e le transizioni dei profili esistenti e i tetti in crediti sono rimandati a B2 ([Cost telemetry](../cost-telemetry.md#measures-required-per-route)).

## 4. Piano delle issue

Ogni issue si apre dopo l'approvazione di questo ADR, una per area. Dopo l'approvazione le issue sono state aperte: P2-1 è #361, P2-2 #362, P2-3 #363, P2-4 #364, P2-5 #365, P2-6 #163 e P2-7 #214.

- Le issue che toccano `devloop/` o `tests/test_devloop_boundary.py` richiedono il livello 2 di review: `devloop/` è in `sensitive_paths`, e l'allowlist del confine è un'autorità.
- Quelle che toccano `docs/adr/`, `AGENTS.md` o `CLAUDE.md` le mergia l'operatore (`operator_only_paths`, devloop/config.toml:141).
- Ogni issue aggiorna i documenti che la mappa `[docs.map]` lega ai file che cambia.
- Nessuna issue modifica i file fissati dalla baseline del replay gate né il comportamento del motore locale.
- Le nuove funzioni restano spente finché l'operatore non le attiva (D8).

| Issue | Titolo proposto | Perimetro | Dipendenze | Review |
|---|---|---|---|---|
| P2-1 (#361) | feat: pacchetto `core` con i validatori puri del budget | Il pacchetto `core` (nome da confermare in D7) con la validazione del profilo e delle regole sui consumi ignoti, il calcolo di addebito e capienza su uno stato dato e la classificazione della telemetria. Allowlist di `tests/test_devloop_boundary.py` estesa a `core`. Test di equivalenza con ssdlc_recovery.py e ssdlc_telemetry.py sugli stessi casi. Motore invariato. `docs/architecture.md` aggiornato (sezioni "The devloop boundary" e "Future `core` package") | Approvazione dell'ADR | Livello 2 |
| P2-2 (#362) | feat(devloop): journal durevole degli intenti e degli esiti dei turni | Journal locale con hash concatenati in `state_dir`: intento prima di ogni turno, esiti `returned`, `failed`, `uncertain` e `not_dispatched`, gestione della coda incompleta e rifiuto di uno snapshot senza journal (vincolo 3). Sostituisce `ledger-open-turns.json`. Un turno incompleto diventa `uncertain` e blocca un nuovo turno dello stesso lavoro fino alla riconciliazione, con escalation. I record `ledger` su GitHub restano invariati | Approvazione dell'ADR | Livello 2 |
| P2-3 (#363) | feat(devloop): identità di storia, fase e tentativo | Entità e identità della [sezione 3.2](#32-identità-di-storia-fase-e-tentativo) nel journal di P2-2. Marker del dispatcher sulla radice e sulle issue figlie; identità del tentativo nel marker della PR e nei record dei turni; un solo tentativo attivo per fase; chiave `(sha del design approvato, task-id)` prevista nel formato. Rappresentazione dei tentativi secondo la scelta della sezione 3.2 | P2-2 | Livello 2 |
| P2-4 (#364) | feat(devloop): outbox per i record e le ricevute | Record del ledger, output degli agent e ricevute salvati nel journal prima della pubblicazione e pubblicati da un outbox riprendibile, sul modello di `ProjectionOutbox`: riconciliazione sul commento esatto dello stesso autore prima di ogni nuovo POST, nessun POST ripetuto su un esito incerto. Nella porta `Tracker`, un esito incerto distinto per `comment` ed `edit_comment` (ADR 0001, "Porta del tracker"), con fake e test di conformità aggiornati | P2-2 | Livello 2 |
| P2-5 (#365) | feat(devloop): envelope di budget per storia e prenotazione dei turni | Envelope per storia della [sezione 3.3](#33-modello-di-prenotazione-del-budget), con i calcoli di `core`: prenotazione prima di ogni turno, regole dichiarate per agent e route, rifiuto prima dell'avvio di un turno non coperto, consumo cumulativo attraverso fasi, issue figlie, tentativi, fallback e riavvii. Chiavi di configurazione con il budget spento per default; stato del budget in `status` e nella ricevuta della PR. Valori e attivazione secondo D2, D3 e D8 | P2-1, P2-2, P2-3 | Livello 2 |
| P2-6 (#163) | feat: budget per task e trasferimenti nel motore locale (#163) | Come deciso dall'operatore in D4: nuova operazione del contratto di #162, dimensioni trasferibili e regole di consumo, allocazione sui profili nuovi e transizione per le run esistenti, conto condiviso dei consumi non attribuibili. Non tocca i file fissati dalla baseline: se servisse, la revisione della baseline è un'issue separata autorizzata dall'operatore (D5) | Approvazione dell'ADR e D4 | Livello 2 (limiti e autorizzazioni) |
| P2-7 (#214) | feat: telemetria B2, transizioni dei profili e tetti in crediti (#214) | Come deciso dall'operatore in D6 | Approvazione dell'ADR e D6 | Livello 2 |

**Ordine.**

- P2-1 e P2-2 sono indipendenti e possono partire subito dopo l'approvazione.
- P2-3 e P2-4 seguono P2-2, anche in parallelo. P2-5 segue P2-1, P2-2 e P2-3, e può procedere in parallelo a P2-4.
- P2-4 e P2-5 chiudono insieme la parte di devloop: l'una non sostituisce l'altra.
- P2-6 e P2-7 riguardano il motore locale, non dipendono dalle altre e partono quando l'operatore ha preso le decisioni D4 e D6.
- Il passo 3 (fasi sul tracker) parte solo dopo **sia P2-4 sia P2-5** e dopo l'attivazione decisa in D8. Senza P2-4 mancherebbero il salvataggio degli output e delle ricevute prima della pubblicazione e la riconciliazione delle pubblicazioni incerte, che il vincolo 3 dell'ADR 0001 richiede prima delle run con le fasi; senza P2-5 mancherebbe la prenotazione del budget del vincolo 2.

**Esclusi da questo passo:**

- fasi come tipi di issue e PR (passo 3);
- contenimento, fencing e prova di terminazione dei writer (passo 4);
- backend filesystem (passo 5);
- porting del corpus A5 e convergenza del motore su `core` (passo 6);
- ogni cambio alla CI, alla baseline o all'oracolo A5.

## 5. Decisioni riservate all'operatore

Decise dall'operatore il 2026-11-18, accettando le raccomandazioni di questa sezione, con il commento sulla PR #343 e il merge:

- **D1.** ADR approvato.
- **D2.** Per agy e codex, `unmetered_quota` più i tetti di turni e, se fissati, di token dell'agent, invece di una riserva in USD. Le estensioni dell'envelope seguono il contratto di #162, caso per caso e senza delega. **I valori dell'envelope restano da fissare.**
- **D3.** Nessuna nuova credenziale e nessun nuovo permesso. **La lettura delle quote in abbonamento (#166) con le credenziali degli agent resta da decidere** quando servirà.
- **D4 (#163).** Trasferimenti autorizzati come in (a); dimensioni trasferibili come in (a); allocazione con plafond cumulativi per i profili nuovi e quote del residuo, con una transizione per le run esistenti, più un conto condiviso per i consumi non attribuibili.
- **D5.** Nessuna nuova baseline. Ogni modifica a file fissati è un'issue separata, da autorizzare caso per caso.
- **D6 (#214).** (a) transizione solo per i profili con tetti in crediti; (b) un tetto in crediti sostituisce l'obbligo USD di una route solo con una transizione autorizzata dall'operatore per quel profilo.
- **D7.** Il journal locale è l'autorità e il ledger su GitHub ne è la proiezione. La telemetria delle CLI può alzare un addebito, mai ridurlo sotto la riserva. Le issue figlie ereditano la storia solo dal marker del dispatcher. Il journal con hash concatenati resta in devloop.
- **D8.** Budget e blocco sugli esiti incerti diventano obbligatori prima del passo 3. Per il flusso attuale di devloop si passa prima da un periodo in sola registrazione.

Restano **aperti**, e chi li incontra li tratta come decisioni dell'operatore ancora da prendere:

- i valori dell'envelope per storia (D2);
- la lettura delle quote in abbonamento (#166) con le credenziali degli agent, e in quale ambiente (D3);
- il nome del pacchetto `core`, che P2-1 rimandava a D7: la decisione D7 ne fissa il contenuto (il journal resta in devloop), non il nome.

Restano valide tutte le decisioni riservate degli ADR 0001 e 0002.

### Domande come proposte

Registro storico: le domande con le alternative e le raccomandazioni, come le poneva la versione proposta di questo ADR, prima delle decisioni qui sopra.

- **D1. Approvazione di questo ADR,** con il merge della sua PR.
- **D2. Budget.**
  - I valori dell'envelope per storia: turni totali, per ruolo e per fase, tempo, dollari di spesa misurata e di stime dove sono attestati, ed eventuali tetti di token per singolo agent (claude e codex; agy dopo #358). Possono avere default per livello di rischio, come i tetti di #165, oppure essere solo espliciti, come i profili del motore locale. Nessun valore è proposto qui.
  - Le regole sui consumi ignoti per ogni agent e route di devloop, cioè la riserva per turno o `unmetered_quota`. Senza una regola, agy e codex vengono rifiutati. Per un agent che non esporta costi, come agy e codex, una riserva in USD è una stima inventata da chi la imposta ([sezione 3.3](#33-modello-di-prenotazione-del-budget)): per loro la raccomandazione è `unmetered_quota` più i tetti di turni e, se fissati, di token dell'agent.
  - Se le estensioni dell'envelope seguono il contratto di #162. La raccomandazione è sì: caso per caso, senza delega, come vuole la decisione 5 dell'ADR 0001.
- **D3. Credenziali.**
  - Se devloop può leggere le quote in abbonamento (#166) con le credenziali degli agent, e in quale ambiente.
  - Nessuna proposta di questo ADR aggiunge permessi alle app GitHub o nuove credenziali.
- **D4. Le tre domande di #163,** nella [sezione 3.5](#35-budget-per-task-e-trasferimenti-domande-di-163):
  - chi autorizza i trasferimenti: (a), (b) o (c). La raccomandazione è (a);
  - le dimensioni trasferibili: (a) o (b). La raccomandazione è (a);
  - l'allocazione: plafond cumulativi per i profili nuovi e quote del residuo con una transizione per le run esistenti, più un conto condiviso per i consumi non attribuibili. È la raccomandazione; le alternative sono nella sezione.
- **D5. Baseline del replay gate.**
  - Nessuna issue del piano richiede una nuova baseline o una nuova versione dell'oracolo A5.
  - Se un'implementazione dovesse toccare file fissati, per esempio `tests/test_recovery_limits.py`, la revisione è un'issue separata da autorizzare caso per caso.
- **D6. Le due domande di #214 (telemetria B2).**
  - **Quali profili esistenti sono incompatibili e richiedono la transizione.** Alternative:
    - (a) solo i profili con tetti in crediti;
    - (b) anche i profili in USD che coprono route in abbonamento senza dichiarazione;
    - (c) tutti i profili congelati prima di #165.
  - **Se un tetto in crediti può sostituire l'obbligo USD di una route.** Alternative:
    - (a) mai: i crediti si aggiungono e non sostituiscono;
    - (b) sì, solo con una transizione autorizzata dall'operatore per quel profilo;
    - (c) sì, con una dichiarazione nel profilo nuovo, senza transizione.
  - Il supervisore raccomanda: transizione solo per i profili in crediti, cioè (a) per la prima domanda; sostituzione dell'USD solo con una transizione autorizzata dall'operatore, cioè (b) per la seconda.
- **D7. Regole di fiducia e confine.**
  - **Autorità dello stato.** La raccomandazione C della [sezione 3.1](#31-dove-vive-lo-stato-durevole) rende il journal locale l'autorità, anche se dà solo integrità e non provenienza. Il ledger su GitHub ne diventa la proiezione.
  - **Telemetria non autenticata.** La telemetria dichiarata dalle CLI può alzare un addebito, ma mai ridurlo sotto la riserva.
  - **Marker di storia.** Le issue figlie ereditano la storia solo dal marker del dispatcher.
  - **Contenuto e nome di `core`.** Se il journal con hash concatenati può entrare in `core` come primitiva, oppure resta in devloop come si raccomanda.
- **D8. Attivazione.**
  - Quando il budget e il blocco sugli esiti incerti diventano obbligatori.
  - Se valgono anche per il flusso attuale di devloop, senza fasi, oppure solo per le run con le fasi, come chiede il vincolo 2.
  - Con quale passaggio di osservazione prima del rifiuto.
  - La raccomandazione è attivarli prima del passo 3 e, per il flusso attuale, dopo un periodo in sola registrazione. La scelta resta dell'operatore.

## Precedenti

- [ADR 0001](0001-prodotto-unico-fasi-su-tracker.md): vincoli 1, 2, 3 e 6, piano in sei passi, decisioni riservate 1 e 5.
- [ADR 0002](0002-ruoli-e-responsabilita.md): decisioni riservate all'operatore, fra cui budget e round, credenziali, regole di fiducia, baseline del replay gate e attivazione.
- [ADR 0003](0003-struttura-della-documentazione.md): formato del piano delle issue.
- #133, #134 e #135: ledger, mappa dei criteri e ricevuta della PR di devloop.
- #138: porte del tracker (passo 1).
- #43, #128, #164, #165, #166 e #162, #167, #168: limiti, telemetria e quote del motore locale.
- #163 e #214: decisioni aperte, analizzate nelle sezioni 3.5 e 3.6.
