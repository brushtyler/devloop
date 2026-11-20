# ADR 0005: deleghe al supervisore, implementer generico e pool dei reviewer

- **Stato:** approvato dall'operatore con il merge di PR #373 (2026-11-18), accettando le raccomandazioni della sezione 4. Le decisioni riservate sono state prese il 2026-11-18 (vedi la [sezione 4](#4-decisioni-riservate-alloperatore)); quelle rimaste aperte sono indicate lì. Il resto del documento è il registro della proposta: le alternative e l'analisi restano come scritte prima dell'approvazione. Fanno eccezione le due integrazioni chieste dall'operatore il 2026-11-18 alle 15:20, prima del merge: l'analisi delle cause per ogni estensione di limiti e le chiavi degli agent in configurazione. Sono scritte nelle sezioni 3.1, 3.2, 3.3, 4 e 5. Lo stesso vale per le correzioni chieste dall'operatore il 2026-11-18 dalle 16:40 alle 16:55, sempre prima del merge: la policy di fiducia in un file riservato all'operatore e le chiavi con agent e ruolo per esteso, che sostituiscono lo schema con il suffisso `-impl`. Sono scritte nelle sezioni 1, 2, 3.1, 3.2, 3.3, 4 e 5. La decisione dell'operatore del 2026-11-18 alle 21:50, anch'essa prima del merge, **sostituisce** quella sulla policy di fiducia delle 16:40: niente file riservato né misura temporanea sull'overlay, ma una regola per i reviewer, e i tetti delle deleghe come costanti del codice. È scritta nelle sezioni 1, 2, 3.1, 4 e 5. Dopo l'approvazione, la decisione dell'operatore del 2026-11-19 su #461 precisa quando un `needs_operator` per la policy di fiducia tiene il merge all'operatore: è scritta come precisazione di D10 nella sezione 4. Lo stesso vale per la decisione dell'operatore del 2026-11-19 su #472, che lascia all'operatore solo la regola stessa e sposta la decisione sulla policy di fiducia dalla PR alla issue, e per la decisione dell'operatore del 2026-11-19 alle 01:30 sul reviewer di soli finding (#416), scritta come integrazione di D8 nella sezione 4 e come P5-9 nella sezione 5.
- **Fase:** design (#370).
- **Data:** 2026-11-18.
- **Base verificata:** il commit 7b336c9 di `main`.

## Contesto

- L'operatore vuole intervenire durante la giornata **solo su decisioni critiche, architetturali e di sicurezza** (diario #76, 2026-11-18).
- Il 2026-11-18 è intervenuto sette volte, e solo una era davvero sua: l'approvazione dell'[ADR 0004](0004-passo-2-budget-stato-durevole.md). Le altre sono state:
  - due concessioni di round su #351, perché il limite dei round è sempre critico;
  - un `/devloop retry` su #357, dopo un errore tecnico di agy. È arrivato all'operatore perché il supervisore aveva implementato la PR;
  - la ripetizione delle decisioni dell'ADR 0004 su #214 e su #163. Questa la risolve #367, e non è trattata qui.
- Le cause sono tre:
  - le deleghe al supervisore sono più strette del necessario;
  - l'implementer e il supervisore sono lo stesso agent con la stessa identità;
  - il pool dei reviewer non ha riserve.

Questo ADR è nato come analisi e proposta: non modifica codice né altri documenti, nemmeno gli ADR che rivede. Per le scelte che spettavano all'operatore ha proposto alternative e raccomandazioni; le decisioni prese sono nella sezione [Decisioni riservate all'operatore](#4-decisioni-riservate-alloperatore). Le issue di implementazione nascono solo dopo l'approvazione, come per l'[ADR 0004](0004-passo-2-budget-stato-durevole.md#4-piano-delle-issue).

Questo ADR segue il [ciclo degli ADR](README.md#ciclo-di-un-adr) di `docs/adr/README.md`.

## Metodo

- Ogni affermazione sullo stato attuale cita il codice nella forma file:riga, al commit 7b336c9, oppure il documento e la riga in cui è scritta una decisione già presa.
- Le issue citate senza un riferimento al codice (#316, #365) non sono su `main`: vale quello che ne dice il corpo di #370.
- #367 (PR #377) e #369 (PR #378) sono arrivate su `main` dopo il commit di base. Quando le cito, il riferimento è al commit 92e3e07 di `main`.
- Restano valide tutte le decisioni riservate degli ADR 0001, 0002 e 0004, salvo le revisioni limitate proposte qui e decise dall'operatore.

## 1. Stato attuale

### Decisioni in vigore

| Decisione | Testo | Riferimento |
|---|---|---|
| ADR 0001, decisione 5 | "Estensioni di budget e di round. Restano dell'operatore, **caso per caso**, senza delega" | docs/adr/0001-prodotto-unico-fasi-su-tracker.md:172 |
| ADR 0002, principio 3 | Budget e round, credenziali e permessi, regole di fiducia, baseline, modalità di merge, attivazioni e ADR sono riservati all'operatore | docs/adr/0002-ruoli-e-responsabilita.md:19 |
| ADR 0002, principio 4 | Il supervisore non implementa e non fa review, e non decide escalation sulle PR di cui è anche implementer | docs/adr/0002-ruoli-e-responsabilita.md:21-23 |
| ADR 0002, non critico | Ritentare un passo fallito per un problema di strumento, senza contare un round e con al massimo 2 tentativi | docs/adr/0002-ruoli-e-responsabilita.md:75 |
| ADR 0002, supervisore | Non cambia configurazione, policy, budget o credenziali | docs/adr/0002-ruoli-e-responsabilita.md:31 |
| ADR 0004, budget | Le estensioni dell'envelope restano dell'operatore, caso per caso, senza delega | docs/adr/0004-passo-2-budget-stato-durevole.md:201, :328 |
| ADR 0002, coordinatore | Ruolo facoltativo, "oggi non assegnato": piano e priorità del backlog, bozze di ADR, **verifica del lavoro del supervisore**. L'assegnazione dei ruoli, coordinatore compreso, è riservata all'operatore | docs/adr/0002-ruoli-e-responsabilita.md:30, :93 |

**Il coordinatore oggi.** Nei fatti il ruolo lo tiene l'**operatore**, che verifica il lavoro del supervisore (indicazione dell'operatore del 2026-11-18). Nel codice il ruolo non esiste: nessun attore, app, chiave di configurazione o comando lo nomina (nessuna occorrenza in `devloop/` né in `scripts/`). Per questo ADR, quindi, chi verifica le deleghe del supervisore è l'operatore.

### Policy di fiducia e configurazione

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Dove sta la policy | `devloop/config.toml` contiene `trusted_users`, i livelli di review con `sensitive_paths`, la modalità di merge con i livelli e `operator_only_paths`, e i ruoli | devloop/config.toml:28-29, :81-93, :122-145, :300-314 |
| Percorsi riservati | `operator_only_paths = ["docs/adr/", "AGENTS.md", "CLAUDE.md"]`, uguale al default del codice: `devloop/config.toml` non c'è | devloop/config.toml:141; devloop/config.py:49 |
| Una PR che cambia la configurazione | `devloop/` è in `sensitive_paths`, quindi la PR è di livello 2. In modalità `auto` devloop mergia anche il livello 2, salvo un override `off` in `[merge.levels]`, che nel file è commentato | devloop/config.toml:93, :122-145 |
| Da dove viene la policy | Dal dispatcher in esecuzione, mai dal branch della PR. Un merge che tocca `devloop/` entra in vigore con il pin successivo, automatico con `[pin] auto` | devloop/config.toml:84-86, :147-155 |
| Overlay locale | `<state_dir>/config.local.toml` può impostare le stesse chiavi di `config.toml`, `trusted_users` e `operator_only_paths` compresi. Un elenco dell'overlay sostituisce quello sotto | devloop/__main__.py:152; devloop/pin.py:39; devloop/config.py:839-868, :886-899 |
| Tetti delle deleghe | Le deleghe non esistono ancora. L'unico tetto di oggi è la costante `MAX_RETRIES` | devloop/supervision.py:61 |
| Review e policy di fiducia | Il verdetto `needs_operator` esiste già per "a product or authority decision", ma il prompt della review non nomina la policy di fiducia | devloop/schemas/review.json:7; devloop/prompts/review.md:58-59 |
| Review consultiva (#369) | Gira solo sulle PR già in `agent:needs-operator` e non ha un verdetto: scrive rischi, `operator_checks` e un'opinione non vincolante | devloop/prompts/advisory.md:5-8, :20-25 e devloop/schemas/advisory.json:4-12, al commit 92e3e07 |
| Conclave | Su un `needs_operator` può sbloccare il lavoro con una decisione unanime, salvo che un membro segnali il perimetro riservato all'operatore (`RESERVED`). Oggi è spento | devloop/conclave.py:3-20; devloop/config.toml:43 |

### Round, retry e comandi del supervisore

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Limite dei round | `max_review_rounds = 3`, globale per tutte le PR. I round contati sono tutti, CI fallite comprese, salvo quelli esentati (resync, revoca per la base, round interrotti) | devloop/config.toml:51; devloop/planning.py:778-784, :872-875 |
| Motivo di ciascun round | devloop sa già perché un round conta: CI fallita, review dopo una correzione, review dopo una sync con conflitti, prima review | devloop/planning.py:878-920 |
| Escalation al limite | Classe `round_limit`, sempre critica: la PR va in `agent:needs-operator` | devloop/dispatcher.py:2103-2108; devloop/supervision.py:33-34 |
| Come si concede un round oggi | Il messaggio di escalation dice all'operatore di **alzare `max_review_rounds`**. È una chiave che `devloop run` rilegge a ogni tick: vale per **tutte** le PR, non per quella in escalation | devloop/dispatcher.py:449-450; devloop/config.py:927 |
| Finding e sicurezza | I finding della review hanno file, riga, titolo, dettaglio e correzione suggerita. **Non esiste un campo che dica se un finding riguarda la sicurezza** | devloop/schemas/review.json:9-37 |
| Classi non critiche | `tool_failure`, `preflight_default`, `sync_failed`, `merged_issue`. Ogni altra classe, anche sconosciuta, è critica | devloop/supervision.py:25-30, :73-76 |
| Comandi del supervisore | `/devloop retry`, `accept-defaults`, `decide <testo>` ed `escalate <motivo>`, solo in modalità di merge `auto` e solo sulle escalation in `agent:needs-supervisor` | devloop/supervision.py:7-17, :65-66, :120-140 |
| Comandi su un'escalation all'operatore | Un comando dell'identità del supervisore su un elemento in `agent:needs-operator` riceve solo la risposta "non applicabile" | devloop/dispatch_supervision.py:245-251 |
| Retry | Al massimo `MAX_RETRIES = 2` per passo e input, contando insieme i retry del supervisore e dell'operatore; poi `retry_limit`, critica | devloop/supervision.py:61; devloop/dispatch_supervision.py:494-517 |
| Retry sulle PR dello stesso agent | Un'escalation su una PR il cui `implementer` coincide con `roles.supervisor` va all'operatore, **anche se è un `tool_failure`**. È il caso di #357. Il confronto è fra **chiavi** di agent, non fra kind | devloop/dispatch_supervision.py:106-107 |
| Fallimenti tecnici | Un `AgentError` (crash, timeout, stallo, modello non disponibile, permesso negato) è `tool_failure` | devloop/dispatcher.py:1449-1465 |
| Traccia dei comandi | Ogni comando lascia un record `supervision` firmato dal dispatcher, scritto prima del cambio di label | devloop/dispatch_supervision.py:327-331, :388-395 |
| Ledger | Record `request`, `turn`, `verification` ed `event` | devloop/ledger.py:1-18 |
| Riepilogo giornaliero | `devloop report` (#256) conta PR, merge, round, turni con fallback ed escalation. **Non legge i record `supervision`** | devloop/report.py:1-15, :225-227 |
| Capacità | `max_open_prs`, `max_parked_prs`, `max_actions_per_tick`, `max_parallel_actions`. Solo le prime due e `max_review_rounds` si applicano senza riavvio. Si cambiano solo modificando la configurazione o l'overlay locale | devloop/config.toml:45-56; devloop/config.py:263-271, :846-853, :925-927 |
| Budget | devloop non ha ancora un budget: l'envelope per storia è P2-5 dell'ADR 0004 (#365) | devloop/config.py:263-282; docs/adr/0004-passo-2-budget-stato-durevole.md:300 |

### Implementer e identità

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Un solo implementer | `roles.implementer` è un'unica chiave di agent, oggi `claude`, per tutte le issue | devloop/config.toml:308; devloop/config.py:744-746; devloop/dispatch_actions.py:102-104 |
| Supervisore | `roles.supervisor` è una chiave di agent: nell'installazione dell'operatore è `claude`, lo stesso agent dell'implementer | devloop/config.toml:307; devloop/config.py:748-752; docs/adr/0002-ruoli-e-responsabilita.md:31, :34 |
| Identità del commit e della PR | Il commit, il push e la PR usano l'app dell'agent implementer | devloop/dispatch_actions.py:154-156, :180-181; devloop/dispatcher.py:565-570 |
| Marker della PR | Registra la chiave dell'implementer e il suo profilo (identità, kind, modello, effort, argomenti) | devloop/dispatch_actions.py:170-172; devloop/policy.py:266-277 |
| Chi corregge | I fix e le sync con conflitti li esegue l'agent registrato come implementer della PR, non quello configurato oggi | devloop/dispatch_actions.py:681-686 |
| Record accettati | I record di un attore valgono solo se li ha scritti la sua app | devloop/dispatcher.py:575-579 |
| Attribuzione | Il corpo della PR inizia con l'intestazione dell'agent e del modello che hanno davvero lavorato, fallback compresi | devloop/dispatch_actions.py:177-179 |
| Identità del supervisore | I comandi, le label e le richieste di fix del supervisore valgono se li scrive l'app dell'agent configurato come `roles.supervisor`: oggi `claude-agent`, la stessa app dell'implementer | devloop/dispatcher.py:679-693 |
| App | Un'app per agent, più `dispatcher` e `merger`, che non sono agent; ogni attore ha la sua app | devloop/config.py:205-207, :766-780; devloop/config.toml:381-405 |
| Chiave dell'agent | La chiave `[agents.<chiave>]` sceglie anche l'app: un agent firma con `[apps.<chiave>]` (`[apps.claude]` è `brushtyler-devloop-claude-agent`), e la stessa app non può servire due attori. La chiave compare anche nel marker della PR, nei record `turn` del ledger, nella memoria dei modelli non disponibili e nella ricevuta della policy | devloop/config.toml:388-398; devloop/config.py:771-780, :824; devloop/dispatcher.py:571-579; devloop/dispatch_actions.py:170; devloop/ledger.py:314; devloop/unavailability.py:3-9 |
| Token per le sessioni | scripts/agent-token.py espone `claude`, `codex` e `agy`; `dispatcher` e `merger` sono riservati | scripts/agent-token.py:12-14, :35-36 |
| Preflight | Deve essere un agent diverso dall'implementer, controllato una volta sola all'avvio | devloop/config.py:754-759 |

### Reviewer e fallback

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Pool | `roles.reviewers = ["codex", "agy"]`; nessuna chiave del pool può essere quella dell'implementer | devloop/config.toml:309-314; devloop/config.py:671-692 |
| Livello 1 | Il reviewer registrato nella PR, altrimenti il primo del pool, purché la **chiave** sia diversa da quella dell'implementer. Non si controlla il modello | devloop/dispatch_merge.py:157-162 |
| Livello 2 | La prima coppia del pool, in ordine di configurazione, distinta e indipendente da tutti gli autori | devloop/dispatch_merge.py:163-177 |
| Indipendenza | Un reviewer è indipendente da un autore se differiscono **sia** l'identità GitHub **sia** la tupla `(kind, model, effort)`. Due reviewer sono distinti se differiscono l'identità e la tupla con gli argomenti di review | devloop/policy.py:29-34, :287-301 |
| Autori | Implementer, ogni fixer, ogni sync con conflitti, e l'agent configurato sotto la chiave dell'implementer con **tutti i suoi modelli di fallback** | devloop/dispatcher.py:1992-2004 |
| Fallback dei reviewer | Al livello 2 un modello di fallback deve restare indipendente dagli autori e distinto dagli altri reviewer, **per tupla**. Al livello 1 nessun controllo | devloop/dispatch_actions.py:470-493 |
| Fornitore | Non esiste un concetto di fornitore: `kind` è la CLI (`claude`, `codex`, `agy`), non chi produce il modello. La validazione dei fallback controlla l'effort e i duplicati, non il fornitore | devloop/config.py:19, :327-357 |
| Indisponibilità | Un modello non disponibile si ricorda per agent, e i turni successivi partono dal primo modello disponibile della catena. Quando la catena è esaurita, il turno fallisce come `tool_failure`. La scelta dei reviewer non ne tiene conto | devloop/unavailability.py:1-14; devloop/dispatcher.py:1743-1756; devloop/dispatch_merge.py:145-177 |
| Ricevuta della policy | L'hash della policy copre app, kind, modello, effort, argomenti e fallback di ogni reviewer del pool: cambiare il pool annulla le approvazioni delle PR aperte | devloop/config.py:822-827; devloop/policy.py:59-63 |

### Ritmo e coda

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Escalation all'operatore | Immediata: label `agent:needs-operator` e commento del dispatcher, a qualsiasi ora | devloop/supervision.py:7-10; devloop/dispatcher.py:2103-2108 |
| Ordine della coda | Le issue pronte partono per priorità, poi per data di `agent:ready`. Un'issue bloccata da un'altra aperta aspetta. Con `max_parked_prs` raggiunto partono solo le `priority:high` | devloop/config.toml:45-50, :70-75; devloop/planning.py:398-414, :630-637 |
| Finestre di decisione | Non esistono nel codice: il diario #76 è un'issue scritta a mano | — |

## 2. Lacune

| # | Lacuna | Conseguenza del 2026-11-18 |
|---|---|---|
| G1 | Un round in più si concede solo alzando una chiave globale, e solo l'operatore può farlo | Due interventi su #351 |
| G2 | Il supervisore non può ritentare un `tool_failure` sulle PR implementate dal suo stesso agent, anche se il retry non giudica il contenuto | Un intervento su #357 |
| G3 | Implementer e supervisore sono lo stesso agent con la stessa app. Un solo agent implementa tutte le issue | G2 e il punto 3 qui sotto ne dipendono |
| G4 | Il pool ha due reviewer, entrambi necessari al livello 2, senza riserva | Il 2026-11-18 agy era senza quota su Argon e Hydrogen ha restituito un 404 |
| G5 | Un fallback può passare a un modello dello stesso fornitore dell'implementer senza che devloop se ne accorga: agy offre anche modelli Claude | Nessuna, finora |
| G6 | Le decisioni arrivano all'operatore una per una, in qualsiasi momento | Sette interruzioni |
| G7 | La policy di fiducia (`trusted_users`, `operator_only_paths`, regole di review e di merge) sta in `devloop/config.toml`, che non è un percorso riservato. Una PR di livello 2 mergiata in `auto` potrebbe togliere un percorso riservato o aggiungere un utente fidato senza che l'operatore la veda | Nessuna, finora. La decisione del 2026-11-18 alle 21:50 la affida ai reviewer (regola 2 della sezione 3.1) |

## 3. Proposte

Ogni area ha alternative e una raccomandazione. Le raccomandazioni sono proposte. Diventano decisioni con l'approvazione di questo ADR, salvo le parti elencate nella [sezione 4](#4-decisioni-riservate-alloperatore), che restano dell'operatore.

### 3.1 Deleghe al supervisore

#### Revisione limitata della decisione 5 dell'ADR 0001

La decisione 5 dell'ADR 0001 si rivede **solo** per le deleghe elencate in questa sezione, entro i tetti scritti nel codice (regola 2). Fuori da questi casi resta com'è. Rivedono di conseguenza, nella stessa misura:

- il principio 3 dell'ADR 0002 ("budget e round");
- il principio 4 dell'ADR 0002, solo per i retry dei fallimenti tecnici;
- la sezione 3.3 e la decisione D2 dell'ADR 0004, solo per la tolleranza dell'envelope.

| Area | Il supervisore decide da solo | Resta dell'operatore |
|---|---|---|
| Round di review | Fino a +2 round per PR, se il round in più nasce da una CI fallita o da finding in calo, e nessun finding aperto è di sicurezza | Oltre +2, o con finding di sicurezza aperti |
| Budget (dopo #365) | Estensione dell'envelope fino alla tolleranza X%, una volta per storia, solo in turni e tempo | USD oltre il tetto, una seconda estensione, i valori dell'envelope |
| Fallimenti tecnici | Retry dei passi falliti per timeout o modelli non disponibili, anche sulle PR implementate dallo stesso agent del supervisore, entro il limite esistente di 2 | — |
| Capacità | Slot, parallelismo, limiti temporanei, entro i tetti dell'operatore | I tetti |
| Default non critici | Già oggi, con `/devloop decide` (devloop/supervision.py:15-16) | — |
| Sempre dell'operatore | — | ADR e architettura, `AGENTS.md` e `CLAUDE.md`, regole di fiducia, credenziali e permessi, sicurezza (threat model, finding non corretti), baseline del replay gate, modalità di merge, attivazioni (D8 dell'ADR 0004) |

#### Regole comuni a tutte le deleghe

1. **Solo con un comando.** La delega si esercita con un comando del supervisore sul tracker, come quelli di oggi (devloop/supervision.py:120-140). devloop lo accetta solo entro i tetti scritti nel codice (regola 2), verificati dal dispatcher al momento del comando. Il supervisore **non modifica mai a mano** la configurazione né l'overlay locale.
2. **Tetti nel codice e policy di fiducia riconosciuta dai reviewer** (decisione dell'operatore del 2026-11-18 alle 21:50, che sostituisce quella delle 16:40 sul file riservato). Oggi `trusted_users`, `operator_only_paths` e le regole di review e di merge stanno in `devloop/config.toml`, che non è riservato (G7). Il rischio reale non è un attacco ma una regressione accidentale, o un agent che si sblocca allentando un controllo. Le PR su `devloop/` passano già da due reviewer indipendenti di fornitori diversi (livello 2, devloop/config.toml:93) e dalla CI.
   - **Tetti nel codice.** I tetti massimi delle deleghe sono **costanti del codice**, come `MAX_RETRIES` (devloop/supervision.py:61): +2 round per PR, 3 concessioni al giorno, 24 ore per un limite temporaneo, tolleranza di budget del 25%, tetti di capacità per chiave (D3 e D4). La configurazione e l'overlay locale possono solo **abbassarli** o spegnere le deleghe: il dispatcher rifiuta all'avvio un valore che li alzi.
   - **Regola per i reviewer.** Il prompt della review chiede esplicitamente di riconoscere le modifiche alla **policy di fiducia** e di dare in quel caso il verdetto `needs_operator`, con il motivo in `human_reason`. Il verdetto esiste già (devloop/schemas/review.json:7), ma oggi il prompt non nomina la policy (devloop/prompts/review.md:58-59). Solo queste PR arrivano all'operatore. La policy comprende:
     - `trusted_users`;
     - `operator_only_paths`;
     - modalità e regole di merge;
     - livelli di review e pool dei reviewer;
     - limiti dei round;
     - tetti delle deleghe;
     - permessi degli agent (`write_args`, `review_args`, sandbox);
     - app e identità;
     - **il codice che la applica**.
   - **Review consultiva.** Anche il suo prompt chiede di riconoscere le modifiche alla policy di fiducia. Non ha un verdetto e gira solo su PR già all'operatore (sezione 1): le segnala in modo esplicito fra gli `operator_checks`, con il motivo.
   - **Conclave.** Se l'operatore lo attiva, una modifica alla policy di fiducia rientra nel perimetro riservato all'operatore (`RESERVED`): il conclave non può sbloccare un `needs_operator` dato per questo motivo (devloop/conclave.py:3-16).
   - Alzare un tetto è una modifica del codice che applica la policy, quindi ricade nella regola. Lo stesso vale per una PR che cambia la regola stessa.
   - **Nessun file riservato.** Il file `operator_policy.toml`, i test di invarianti e CODEOWNERS non si fanno: per questo rischio sarebbero un'impalcatura sproporzionata. `devloop/config.toml` non è in `operator_only_paths`, nemmeno attraverso l'overlay: rendere riservata tutta la configurazione mandava all'operatore quasi ogni PR.
   - **Rischi residui accettati** (D4 e sezione 4): la regola dipende dall'attenzione dei reviewer, che sono due e di fornitori diversi ma possono non accorgersene; una modifica deliberatamente mascherata non è coperta; l'overlay locale dell'operatore può indebolire la policy.
3. **Tetto giornaliero.** Un numero massimo di concessioni del supervisore per giorno UTC, tutte le deleghe insieme, retry esclusi (hanno già il loro limite). Il valore lo sceglie l'operatore (D4); la proposta è 3.
4. **Traccia.** Ogni uso lascia il record `supervision` di oggi (devloop/dispatch_supervision.py:388-395) e un record `event` del ledger con delega, tetto, valore concesso e residuo. Il riepilogo di #316 elenca le concessioni del giorno; il riepilogo attuale non legge i record `supervision` (devloop/report.py:225-227) e va esteso.
5. **Verifica.** Le concessioni le verifica il **coordinatore**, che secondo l'ADR 0002 verifica il lavoro del supervisore (docs/adr/0002-ruoli-e-responsabilita.md:30). Oggi il coordinatore è l'operatore (sezione 1): legge le concessioni nel riepilogo di #316 e, se una non lo convince, revoca la delega (regola 6) o riporta la PR a sé. Se un giorno il ruolo passasse a un agent, avrebbe un'**identità propria, distinta da quella del supervisore**, perché ne verifica il lavoro: assegnarlo e crearne l'app sono decisioni dell'operatore (docs/adr/0002-ruoli-e-responsabilita.md:93).
6. **Revoca.** Ogni delega ha una chiave di configurazione dell'operatore, per esempio `[delegations] rounds = false`. Spenta, il comando riceve la risposta "non applicabile" e l'escalation resta all'operatore, come oggi (devloop/dispatch_supervision.py:245-251). Tutte le deleghe nuove sono **spente per default**: attivarle è una decisione dell'operatore (D10).
7. **Il supervisore non approva mai.** Concede round, budget o capacità, ma il giudizio sul codice resta ai reviewer. Nessuna delega cambia le direttive di merge (docs/adr/0002-ruoli-e-responsabilita.md:54-63).
8. **Fallisce chiuso.** Se il dispatcher non può verificare una condizione (record illeggibile, finding senza classificazione, escalation di un altro tipo), il comando è rifiutato e l'escalation resta o passa all'operatore.
9. **Analisi delle cause** (decisione dell'operatore del 2026-11-18). Vale per ogni estensione di limiti: round concessi, tolleranza di budget e capacità.
   - Il supervisore non concede un'estensione senza un'analisi scritta del perché serve. L'analisi sta nel motivo del comando oppure nel diario #76, e in quel caso il motivo la cita.
   - L'analisi nomina la causa: finding nuovi o in calo, sync, CI, PR troppo grande, interazione con altre PR.
   - Se la causa è correggibile, per esempio un difetto di devloop o un'issue troppo larga, il supervisore apre l'issue che la corregge e la cita nell'analisi.
   - devloop rifiuta un comando di estensione senza motivo. Il contenuto dell'analisi lo verifica il coordinatore (regola 5), che legge le concessioni nel riepilogo di #316.
10. **Problemi ricorrenti** (decisione dell'operatore del 2026-11-18). Un problema che si ripete su più PR non si risolve con estensioni caso per caso: per esempio, più PR che arrivano al limite dei round per la stessa causa.
    - Il supervisore lo valuta e propone all'operatore, nella finestra delle decisioni (sezione 3.4), un aumento del limite in configurazione oppure la revisione o la riscrittura delle issue: divisione, perimetro più stretto, criteri più precisi.
    - Il valore del limite resta dell'operatore. Il supervisore lo propone, ma non lo cambia, e non copre il problema con altre estensioni in attesa della decisione.

#### Round di review

Oggi `round_limit` è critica e porta la PR all'operatore (devloop/dispatcher.py:2103-2108). Proposta:

- quando la PR raggiunge il limite, il dispatcher valuta se la concessione è delegabile. Se lo è, l'escalation ha una nuova classe non critica, per esempio `round_grantable`, e va in `agent:needs-supervisor`. Altrimenti resta `round_limit` e va all'operatore;
- il supervisore risponde con `/devloop grant-round <motivo>`, dove il motivo riporta o cita l'analisi delle cause (regola 9). Il dispatcher rilegge le condizioni, concede **un** round a quella PR e registra la concessione. Il limite della PR diventa `max_review_rounds` + round esentati + round concessi: è il calcolo di oggi (devloop/dispatch_actions.py:353-356) con un termine in più. `max_review_rounds` non cambia;
- il tetto è +2 round per PR, una costante del codice (regola 2). Un terzo round torna all'operatore come `round_limit`.

Condizioni di delegabilità, tutte verificate dal dispatcher:

- l'ultimo round contato è una CI fallita, come lo classifica già `round_reasons` (devloop/planning.py:905-906, :919-920), **oppure** i finding bloccanti aperti sono meno di quelli del round precedente;
- nessun finding aperto è di sicurezza (vedi sotto);
- la PR è in modalità di merge `auto`, non tocca `operator_only_paths` e non ha `phase:design`, come per i retry (devloop/dispatch_supervision.py:94-125);
- il tetto per PR e quello giornaliero non sono esauriti.

**Come riconoscere un finding di sicurezza.** Oggi nessun campo lo dice (devloop/schemas/review.json:9-37).

- (a) **Un campo `security` per finding**, obbligatorio nello schema della review, messo dal reviewer. Un finding senza il campo, per esempio di un record precedente, conta come di sicurezza.
- (b) **Un'approssimazione per percorso:** la delega è esclusa se la PR tocca un `sensitive_paths`. È semplice, ma esclude quasi tutto il lavoro su devloop, che è in `sensitive_paths` (devloop/config.toml:93).
- (c) **Entrambe.**

**Raccomandazione: (a),** con la regola che fallisce chiuso. Il campo è una dichiarazione del reviewer, non del supervisore, quindi la delega non tocca il giudizio.

#### Budget, dopo #365

- Comando `/devloop grant-budget <motivo>` sulla storia in escalation per envelope esaurito, con l'analisi delle cause nel motivo (regola 9).
- Estende solo le dimensioni **turni** e **tempo** dell'envelope (ADR 0004, sezione 3.3, docs/adr/0004-passo-2-budget-stato-durevole.md:192-196), di una percentuale fissa del valore iniziale, **una volta per storia**.
- Mai i dollari, i token o una seconda estensione: restano dell'operatore con il contratto di #162 (docs/adr/0004-passo-2-budget-stato-durevole.md:201).
- L'estensione segue le regole di consumo dell'ADR 0004: niente rimborsa una prenotazione.

**Alternative per la tolleranza:**

| Tolleranza | Pro | Contro |
|---|---|---|
| 10% | Il rischio è minimo | Con envelope piccoli è meno di un turno: in pratica non sblocca niente |
| **25%** | Su un envelope tipico vale uno o due turni: abbastanza per chiudere un round o un fix | — |
| 50% | Sblocca quasi sempre | Una storia può costare una volta e mezza il previsto senza che l'operatore lo veda prima |

**Raccomandazione: 25%.** Il valore lo sceglie l'operatore (D3), e va scritto come tetto, una costante del codice (regola 2).

#### Fallimenti tecnici sulle PR dello stesso agent

Oggi il controllo di devloop/dispatch_supervision.py:106-107 manda all'operatore ogni escalation su una PR implementata dall'agent del supervisore. Il principio 4 dell'ADR 0002 serve a evitare che il supervisore giudichi il proprio lavoro, ma un retry di un `tool_failure` non giudica niente: ripete lo stesso passo sullo stesso input.

- (a) **Solo `tool_failure`** diventa delegabile anche su queste PR. Le altre classi non critiche (`sync_failed`, `preflight_default`, `merged_issue`) restano all'operatore.
- (b) **Tutte le classi non critiche.** Ma una sync con conflitti scrive codice, e un `/devloop decide` è un giudizio.
- (c) **Nessun cambio.**

**Raccomandazione: (a),** con il limite esistente di 2 retry (devloop/supervision.py:61), che resta condiviso con i retry dell'operatore. Con l'implementer generico (sezione 3.2) il controllo continua a confrontare l'**agent** effettivo dell'implementer, non l'app: la regola serve quando l'agent del supervisore è anche quello dell'implementer. Con le chiavi per ruolo della sezione 3.2, `claude-supervisor` e `claude-implementer` sono sempre chiavi diverse, anche quando l'agent è lo stesso: il confronto fra chiavi di oggi (devloop/dispatch_supervision.py:106-107) non riconoscerebbe più il caso. Il controllo confronta quindi l'agent effettivo e il fornitore (D8), mai le chiavi.

#### Capacità

- Comando `/devloop capacity <chiave> <valore> <durata> <motivo>` per `max_open_prs`, `max_parked_prs` e `max_parallel_actions`, sulla issue del riepilogo (`[report] issue`, devloop/config.toml:164-170), l'unica che il dispatcher già tiene come propria. Il motivo riporta l'analisi delle cause (regola 9).
- Il valore deve stare entro un tetto per ciascuna chiave, che l'operatore fissa e che sta nel codice come costante (regola 2), come la durata massima di 24 ore (D4). Alla scadenza torna il valore della configurazione.
- `max_parallel_actions` oggi richiede un riavvio (devloop/config.py:925-927): la delega vale solo per le chiavi che si applicano a caldo, oppure l'implementazione la estende.

**Alternative:** (a) il comando sul tracker, come sopra; (b) un overlay separato che il supervisore può scrivere. **Raccomandazione: (a).** (b) contraddice la regola "mai modificando a mano l'overlay", e un file locale non lascia traccia firmata sul tracker (principio 2 dell'ADR 0002).

### 3.2 App per ruolo, implementer generico e agent selezionabile

#### Due app nuove, una per ruolo

**Proposta** (indicazione dell'operatore del 2026-11-18, che resta da confermare con D6): due app nuove, ciascuna dedicata a un **ruolo** e non a un agent, tutte e due **uniche**. Il principio 1 dell'ADR 0002 assegna i ruoli ad attori, ciascuno con la propria identità (docs/adr/0002-ruoli-e-responsabilita.md:17): per questi due ruoli l'identità segue il ruolo, non l'agent che lo esegue.

| App | Ruolo | Cosa firma |
|---|---|---|
| `brushtyler-devloop-implementer` | Implementer, qualunque agent faccia il lavoro | Commit, push, PR, record dei fix e delle sync con conflitti |
| `brushtyler-devloop-supervisor` | Supervisore, qualunque agent lo esegua | Comandi sulle escalation, label (`agent:ready`, priorità), issue, diario, richieste di fix e di hold |

Le altre app non cambiano:

- `claude-agent`, `codex-agent` e `agy-agent` restano ai **reviewer**, e non firmano più né il lavoro dell'implementer né gli atti del supervisore;
- `dispatcher` e `merger` restano come sono (devloop/config.py:205-207).

**Perché un'app per ruolo basta.** Implementer e supervisore sono unici: c'è un solo implementer per PR (i fix li esegue l'agent registrato nel marker, devloop/dispatch_actions.py:681-686) e un solo supervisore (devloop/config.py:748-752). Non c'è quindi il rischio che più implementer o più supervisori si alternino sulla stessa PR, e un'app per ruolo basta a distinguere chi ha fatto cosa. L'agent e il modello effettivi restano:

- nell'intestazione di attribuzione del corpo della PR e dei commenti (devloop/dispatch_actions.py:177-179);
- nella chiave `implementer` e nel profilo del marker della PR (devloop/dispatch_actions.py:170-172), con `identity` uguale al login dell'app implementer;
- nel trailer del commit dell'agent.

**Alternativa scartata: tre app di implementer,** una per agent. Lascerebbe com'è la regola sull'identità (devloop/policy.py:297-301), ma aggiunge tre credenziali da creare, ruotare e proteggere per un'informazione che il profilo ha già. Con un implementer unico per PR non serve, e non è un'alternativa raccomandata.

Conseguenze nel codice, da fare nelle issue P5-3 (app implementer) e P5-4 (app del supervisore):

- l'identità dei record di fix si verifica sull'app implementer, non su quella dell'agent (devloop/dispatcher.py:575-579);
- l'identità del supervisore diventa l'app del supervisore, non più l'app dell'agent `roles.supervisor` (devloop/dispatcher.py:679-693). `roles.supervisor` diventa la tabella `[roles.supervisor]` dell'attore `claude-supervisor`, con `kind`, `provider` e modello: servono al controllo sulle PR implementate dallo stesso agent, che confronta agent effettivo e fornitore (devloop/dispatch_supervision.py:106-107, sezione 3.1);
- scripts/agent-token.py **non** espone **né** l'app implementer **né** l'app del supervisore, come oggi non espone `dispatcher` e `merger` (scripts/agent-token.py:35-36). Nessuna sessione di review o interattiva può scrivere un record come implementer o un comando come supervisore. Il token dell'implementer lo usa solo il dispatcher; il supervisore ottiene il suo con un percorso dedicato, da definire in P5-4, che lo script generico non offre;
- l'app di un agent resta solo per le review.

**Conseguenza sulle review.** Con le due app di ruolo, `claude-agent` non firma più né il codice né i comandi del supervisore, e l'identità di un reviewer è sempre diversa da quelle degli autori. claude può quindi fare la review del codice implementato da claude, purché rispetti la regola sul fornitore e sul modello scelta in D8 (sezione 3.3).

Credenziali e permessi delle due app sono decisioni dell'operatore (D6). La proposta è:

- app implementer: `contents: write` per il push, `pull_requests: write`, `issues: write`;
- app del supervisore: `issues: write` e `pull_requests: write` per commenti e label, senza `contents: write`, perché il supervisore non implementa e non mergia (docs/adr/0002-ruoli-e-responsabilita.md:31).

#### Chiavi degli agent e app dichiarata

Decisione dell'operatore del 2026-11-18, corretta dall'operatore lo stesso giorno, dalle 16:40 alle 16:55: sostituisce lo schema con il suffisso `-impl`. Oggi la chiave `[agents.<chiave>]` sceglie anche l'app, e la stessa chiave compare nel marker, nei record dei turni, nella memoria dei modelli non disponibili e nella ricevuta (sezione 1, riga "Chiave dell'agent").

**Principio: una chiave nomina agent e ruolo per esteso, e serve un solo ruolo.** Nessuna chiave è senza ruolo, quindi `claude`, `codex` e `agy` scompaiono come chiavi.

- **Reviewer.** `claude-reviewer`, `codex-reviewer` e `agy-reviewer`, che firmano con le app `*-agent` di oggi: `claude-agent`, `codex-agent` e `agy-agent` (devloop/config.toml:388-398). Il terzo reviewer claude è `claude-reviewer` (sezione 3.3).
- **Implementer.** `claude-implementer`, e in seguito `codex-implementer` e `agy-implementer`. Il nome dell'agent nella chiave serve: con l'implementer selezionabile (D7) ogni agent implementer è una voce distinta, con modello, argomenti di scrittura e permessi propri. Tutte pubblicano con l'unica app `brushtyler-devloop-implementer`, dichiarata con `app = "implementer"`.
- **Supervisore.** `claude-supervisor`. **Non** sta in `[agents]`, perché devloop non lo esegue: è il nome dell'attore supervisore, descritto in `[roles.supervisor]` con `kind`, `provider` e modello, e firma con l'app di ruolo `brushtyler-devloop-supervisor`. Oggi `roles.supervisor` è invece una chiave di `[agents]` (devloop/config.py:748-752).
- **App dichiarata.** La chiave non coincide più con il nome dell'app, quindi l'agent dichiara la sua app invece di dedurla dalla chiave: gli implementer `app = "implementer"`, i reviewer la tabella `[apps.*]` della loro app di oggi.
- **App di ruolo.** `[apps.implementer]` e `[apps.supervisor]` sono app di ruolo, come `dispatcher` e `merger` (devloop/config.py:207), e non agent. L'app del supervisore è legata a `[roles.supervisor]`: nessun agent la dichiara.
- **Vincoli della configurazione.** Oggi ogni app appartiene a un solo attore (devloop/config.py:779-780). Il vincolo resta per le tabelle `[apps.*]`, ma più implementer possono dichiarare la stessa app `implementer`. Solo una chiave `*-implementer` può dichiarare `app = "implementer"`. Nessun reviewer può dichiarare un'app di ruolo: la sua review sarebbe firmata come implementer o come supervisore. Una chiave usata in un ruolo diverso da quello che nomina, per esempio `claude-reviewer` come `roles.implementer`, ferma devloop all'avvio.
- **Controlli di conflitto.** Il controllo sulle PR implementate dallo stesso agent del supervisore e l'indipendenza dei reviewer confrontano **agent effettivo e fornitore** (D8), mai le chiavi. Oggi tre controlli confrontano chiavi: il pool in configurazione (devloop/config.py:687-688), il reviewer di livello 1 (devloop/dispatch_merge.py:157-162) e le PR dello stesso agent del supervisore (devloop/dispatch_supervision.py:106-107, sezione 3.1, [Fallimenti tecnici sulle PR dello stesso agent](#fallimenti-tecnici-sulle-pr-dello-stesso-agent)).
- **Record.** La chiave resta quella registrata nel marker, nei record `turn`, nella memoria dei modelli non disponibili e nella ricevuta: `claude-implementer` e `claude-reviewer` vi compaiono come agent distinti.
- **Altri ruoli.** Anche `preflight` e `conclave`, oggi spenti (devloop/config.toml:35, :43), nominano chiavi di agent: seguono lo stesso principio quando l'operatore li attiva.

**Migrazione.** Questo ADR fissa il principio e il requisito minimo di sicurezza; i dettagli stanno nelle issue P5-3 e P5-4.

- Il cambio si attiva **con la coda vuota**: nessuna PR aperta né azione in corso (D10). I fix e le sync di una PR aperta li esegue l'agent registrato nel suo marker (devloop/dispatch_actions.py:681-686), e un marker con `implementer = "claude"` citerebbe una chiave che non c'è più.
- All'avvio il dispatcher **si ferma** se trova PR aperte con marker che citano chiavi assenti dalla configurazione o con un ruolo diverso da quello del marker.
- I lettori dei record storici (marker, record `turn` del ledger, ricevute) mappano le vecchie chiavi `claude`, `codex` e `agy` senza riscriverle.
- La memoria dei modelli non disponibili (devloop/unavailability.py:3-9) si migra alle chiavi nuove.
- La chiave dell'advisory reviewer di #369, `advisory_reviewer` (devloop/config.toml:119-125 al commit 92e3e07), diventa `claude-reviewer`.

#### Selezione dell'agent

Oggi c'è un solo implementer per tutte le issue (devloop/config.py:744-746).

- (a) **Label `impl:<agent>`** sull'issue, messa da un utente fidato o dal supervisore, con le regole delle label di priorità di #178 (devloop/config.toml:68-72): vale solo su issue di autori fidati o delle app di devloop.
- (b) **Mappa per area in configurazione**, per esempio `[roles.implementers] "area:docs" = "claude-implementer"`, `"area:ui" = "agy-implementer"`, `"phase:design" = "codex-implementer"`, letta dalle label di area dell'issue.
- (c) **Entrambe:** la label `impl:<agent>` prevale, poi la mappa, poi il default `roles.implementer`.

**Raccomandazione: (c).** In ogni caso:

- la label nomina l'agent e sceglie la sua chiave di implementer: `impl:claude` sceglie `claude-implementer` (sezione [Chiavi degli agent e app dichiarata](#chiavi-degli-agent-e-app-dichiarata));
- l'agent si sceglie **alla presa in carico** e si registra nel record `request` del ledger e nel marker della PR. Una label cambiata dopo non cambia l'implementer di una PR aperta;
- un'issue con due label `impl:` diverse, o con aree che portano ad agent diversi, va all'operatore come `configuration`;
- l'agent scelto deve essere diverso dal preflight. Oggi il controllo è solo all'avvio (devloop/config.py:754-759) e diventa per issue;
- la chiave di un reviewer non è più esclusa dal pool in configurazione (devloop/config.py:687-688): l'esclusione diventa per PR, come già fa la selezione (devloop/dispatch_merge.py:158-168).

### 3.3 Pool dei reviewer: esclusione per agent effettivo

#### Regola di esclusione

Con l'app implementer, la condizione sull'identità di `independent` (devloop/policy.py:297-301) è sempre vera fra un reviewer e un autore: l'esclusione dipende solo dalla tupla.

- Un reviewer si esclude se la sua tupla `(kind, model, effort)` **effettiva** coincide con quella dell'implementer, di un fixer o di una sync con conflitti, compresi i modelli di fallback della catena dell'implementer, come oggi (devloop/dispatcher.py:1992-2004). Se implementa agy, claude può fare la review.
- La regola vale **anche al livello 1**, dove oggi si confronta solo la chiave (devloop/dispatch_merge.py:157-162).
- Fra reviewer resta la regola di oggi: identità diversa e tupla diversa (devloop/policy.py:291-295).

#### Fornitore

Oggi `kind` è la CLI, non il fornitore del modello (devloop/config.py:19). Serve un attributo nuovo.

- (a) Dedurlo dal prefisso del nome del modello (`claude-`, `gpt-`, `gemini-`). È fragile: i nomi li sceglie chi offre la CLI.
- (b) **Una chiave `provider` esplicita** per ogni agent e per ogni modello di fallback, obbligatoria, registrata nel profilo e coperta dall'hash della policy.
- (c) (b) più una **tabella di prefissi noti nel codice**, usata solo come controllo: un modello `claude-*` dichiarato con un altro fornitore ferma devloop all'avvio.

**Raccomandazione: (c).** Il controllo serve proprio per agy, che offre anche modelli Claude.

#### Quorum e fornitori

Al livello 2 tutti i reviewer richiesti devono approvare (docs/adr/0002-ruoli-e-responsabilita.md:55), quindi un reviewer in più non indebolisce il quorum: aggiunge solo una riserva. Resta il rischio che due modelli dello stesso fornitore condividano gli stessi punti ciechi.

- (i) **Solo la tupla:** basta che il modello differisca. Sonnet 5.5 può fare la review di Opus 5.5.
- (ii) **Almeno un reviewer di un fornitore diverso da tutti gli autori.** Al livello 1 l'unico reviewer deve essere di un altro fornitore; al livello 2 uno dei due può essere dello stesso.
- (iii) **Tutti i reviewer di un fornitore diverso.**

**Raccomandazione: (ii).** Claude resta utile come riserva quando implementa codex o agy, e al livello 2 quando implementa claude, in coppia con un reviewer di un altro fornitore.

#### Claude come terzo reviewer

- Il terzo reviewer è la **chiave `claude-reviewer`** (decisione dell'operatore del 2026-11-18, corretta dalle 16:40 alle 16:55), kind `claude`, con un modello diverso da quello dell'implementer `claude-implementer`: Sonnet 5.5 contro Opus 5.5. Oggi la configurazione rifiuta nel pool la chiave dell'implementer (devloop/config.py:687-688): claude entra nel pool con la migrazione delle chiavi (sezione [Chiavi degli agent e app dichiarata](#chiavi-degli-agent-e-app-dichiarata)).
- Pool: `["codex-reviewer", "agy-reviewer", "claude-reviewer"]`, in quest'ordine: claude entra solo quando uno dei primi due manca o non è indipendente.
- Firma le review con `claude-agent`, cioè `[apps.claude]` (devloop/config.toml:388-390), che con le due app di ruolo resta solo ai reviewer. Cambia il nome della chiave, non l'app.
- Prerequisiti: le app implementer e del supervisore (sezione 3.2, D6) e la migrazione delle chiavi. Prima, `claude-agent` firma anche il codice e i comandi del supervisore, e una sessione del supervisore potrebbe scrivere un record di review che devloop accetta.
- Con le app di ruolo, claude può fare la review anche delle PR implementate da claude, purché rispetti la regola sul fornitore e sul modello di D8.
- Cambiare il pool cambia l'hash della policy (devloop/config.py:822-827) e annulla le approvazioni delle PR aperte: va attivato quando nessuna PR è vicina al merge.

#### Riserva quando un reviewer non è disponibile

Oggi la coppia è la prima valida del pool, senza guardare la disponibilità (devloop/dispatch_merge.py:168-173), e la memoria dei modelli non disponibili riguarda solo la catena di un agent (devloop/unavailability.py:1-9).

**Proposta:** alla scelta dei reviewer di un **nuovo round**, un reviewer la cui catena è tutta ricordata come non disponibile si salta e si prende il successivo valido del pool. Un reviewer non si sostituisce mai a metà round, dopo che la ricevuta del round lo ha registrato (devloop/policy.py:59-63): il round resta in attesa o va in `tool_failure`, come oggi.

#### Fallback dello stesso fornitore

Oggi un fallback di un reviewer al livello 2 deve solo restare indipendente per tupla (devloop/dispatch_actions.py:478-493); al livello 1 non c'è controllo. Un fallback di agy su un modello Claude passerebbe, perché il `kind` resta `agy`.

**Proposta, scritta nel codice.** È un vincolo **separato dalla regola di quorum** (sezione precedente) e vale qualunque alternativa scelga l'operatore in D8: la regola di quorum decide quali reviewer formano la coppia, questo vincolo decide quali modelli di fallback un reviewer può usare.

- a runtime, a entrambi i livelli, un modello di fallback di un reviewer **non è mai ammesso** se il suo fornitore coincide con quello di un autore effettivo della PR: l'implementer, ogni fixer e ogni sync con conflitti, con i modelli di fallback che hanno davvero usato. Il fatto che un altro reviewer della coppia sia già di un fornitore diverso non lo rende ammissibile: con la regola (ii), su una PR implementata da claude e revisionata da codex e agy, agy non può ripiegare su un modello Claude anche se codex garantisce già un fornitore diverso. Se nessun modello rimasto nella catena è ammesso, il turno fallisce come `tool_failure`, senza review;
- il modello primario di un reviewer resta soggetto solo alla regola di quorum: il reviewer `claude-reviewer` può fare la review di una PR di claude se la regola scelta lo permette, ma i suoi fallback, tutti Claude, non sono ammessi su quella PR;
- simmetricamente, un fallback dell'implementer o di un fixer entra negli autori con il suo fornitore, come oggi entra con la sua tupla;
- all'avvio, come controllo anticipato, la configurazione si rifiuta se un reviewer del pool ha nella catena un fallback di un fornitore diverso da quello del suo modello primario e uguale al fornitore di un agent che può fare l'implementer (il default `roles.implementer`, gli agent della mappa per area e quelli selezionabili con `impl:<agent>`). È il caso di agy con un fallback Claude. Il controllo a runtime resta comunque quello che garantisce il vincolo, perché l'implementer effettivo si conosce solo per PR.

### 3.4 Ritmo delle decisioni

Non serve codice: è una prassi del supervisore, che l'operatore conferma o cambia (D9).

- **Due finestre giornaliere**, alle 9 e alle 18 UTC. Il supervisore raccoglie le decisioni in sospeso nella sezione "Dove siamo" del diario #76, ciascuna con quello che blocca e con la raccomandazione.
- **Fuori finestra** si interrompe l'operatore solo per:
  - sicurezza: un finding di sicurezza su codice già mergiato, una credenziale esposta, un contenuto non fidato che ha raggiunto un agent;
  - fermo totale: `devloop run` fermo (devloop/config.toml:158-162), oppure nessuna azione possibile perché tutte le PR aspettano l'operatore e non c'è un'issue pronta.
- **Coda profonda:** il supervisore tiene sempre almeno due issue pronte (`agent:ready`) che non dipendono da decisioni in sospeso, usando le priorità e le dipendenze che devloop già rispetta (devloop/planning.py:398-414).

**Alternative:** (a) due finestre fisse; (b) una sola finestra; (c) nessuna finestra, decisioni appena servono, come oggi. **Raccomandazione: (a).** Se l'operatore vuole renderla vincolante per tutti gli agent, va scritta in `AGENTS.md`, che mergia l'operatore.

## 4. Decisioni riservate all'operatore

Decise dall'operatore il 2026-11-18, accettando le raccomandazioni di questa sezione, con il commento sulla PR #373 e il merge. Includono le integrazioni delle 15:20, le correzioni della revisione dalle 16:40 alle 16:55 e la decisione delle 21:50, che sostituisce quella delle 16:40 sulla policy di fiducia:

- **D1.** ADR approvato.
- **D2.** La decisione 5 dell'ADR 0001, i principi 3 e 4 dell'ADR 0002, la sezione 3.3 e la D2 dell'ADR 0004 sono rivisti in modo limitato, **solo** per le deleghe della [sezione 3.1](#31-deleghe-al-supervisore). Le deleghe sono tutte approvate, e ciascuna resta spenta finché l'operatore non la attiva (D10). Integrazione del 2026-11-18 alle 15:20:
  - ogni estensione di limiti (round concessi, tolleranza di budget, capacità) richiede un'analisi scritta delle cause, nel comando o nel diario #76. Se la causa è correggibile, il supervisore apre l'issue che la corregge (regola 9 della [sezione 3.1](#regole-comuni-a-tutte-le-deleghe));
  - un problema che si ripete su più PR non si risolve con estensioni caso per caso: il supervisore propone all'operatore un aumento del limite in configurazione oppure la revisione o la riscrittura delle issue (regola 10).
- **D3.** Tolleranza di budget del **25%**, scritta come tetto, una costante del codice (regola 2 della [sezione 3.1](#regole-comuni-a-tutte-le-deleghe)).
- **D4.**
  - **+2 round per PR**;
  - **3 concessioni al giorno**;
  - durata massima di un limite temporaneo di capacità: **24 ore**;
  - questi tetti, la tolleranza di D3 e i tetti di capacità per chiave sono **costanti del codice**, come `MAX_RETRIES` (devloop/supervision.py:61), non chiavi di `devloop/config.toml` (decisione delle 21:50). La configurazione e l'overlay possono solo abbassarli o spegnere le deleghe, e il dispatcher rifiuta all'avvio un valore che li alzi. Una modifica dei tetti ricade nella regola per i reviewer;
  - le concessioni si esercitano con il comando sul tracker, opzione (a) della sezione [Capacità](#capacità), mai modificando a mano l'overlay;
  - nessuna concessione senza l'analisi delle cause (D2). Il valore dei limiti in configurazione resta dell'operatore, anche quando il supervisore ne propone l'aumento per un problema ricorrente.
- **Policy di fiducia riconosciuta dai reviewer** (decisione del 2026-11-18 alle 21:50, che **sostituisce** quella delle 16:40 sul file riservato, opzione B, e sulla misura temporanea, opzione A; regola 2 della [sezione 3.1](#regole-comuni-a-tutte-le-deleghe)):
  - il prompt della review chiede di riconoscere le modifiche alla policy di fiducia e di dare in quel caso il verdetto `needs_operator`, con il motivo. Il prompt della review consultiva chiede di riconoscerle e di segnalarle all'operatore. Solo quelle PR arrivano all'operatore;
  - la policy comprende `trusted_users`, `operator_only_paths`, modalità e regole di merge, livelli di review e pool dei reviewer, limiti dei round, tetti delle deleghe, permessi degli agent (`write_args`, `review_args`, sandbox), app e identità, e **il codice che la applica**;
  - non si fanno il file `operator_policy.toml`, i test di invarianti né CODEOWNERS;
  - l'opzione A è tolta: `devloop/config.toml` non è più in `operator_only_paths`, nemmeno attraverso l'overlay locale;
  - la regola è l'issue P5-0, da mergiare per prima;
  - **rischi residui accettati:**
    - la regola dipende dall'attenzione dei reviewer: sono due, di fornitori diversi, ma possono non accorgersene;
    - una modifica deliberatamente mascherata non è coperta;
    - l'overlay locale dell'operatore può indebolire la policy;
  - CODEOWNERS, con la code owner review del ruleset, resta una possibile aggiunta futura, solo se emergesse un bisogno reale;
  - **raccomandazioni non bloccanti** all'operatore, sulle impostazioni di GitHub:
    - nel ruleset di `main`, la riapprovazione dopo un push (`dismiss_stale_reviews_on_push` o `require_last_push_approval`);
    - per la sessione del supervisore, un token non admin: oggi usa il login dell'operatore, che ha il bypass del ruleset.
- **D5.** Campo `security` per finding, opzione (a), con la regola che fallisce chiuso.
- **Retry sulle PR dello stesso agent del supervisore.** Solo `tool_failure`, opzione (a) della sezione [Fallimenti tecnici sulle PR dello stesso agent](#fallimenti-tecnici-sulle-pr-dello-stesso-agent), con il limite esistente di 2 retry, condiviso con l'operatore.
- **D6.**
  - due app nuove, una per ruolo: `brushtyler-devloop-implementer` e `brushtyler-devloop-supervisor`;
  - scripts/agent-token.py non le espone;
  - le app `claude-agent`, `codex-agent` e `agy-agent` restano ai reviewer;
  - il coordinatore oggi è l'operatore;
  - chiavi degli agent (integrazione del 2026-11-18 alle 15:20, corretta dalle 16:40 alle 16:55, sezione [Chiavi degli agent e app dichiarata](#chiavi-degli-agent-e-app-dichiarata)): ogni chiave nomina agent e ruolo per esteso e serve un solo ruolo; lo schema con il suffisso `-impl` è sostituito. Reviewer: `claude-reviewer`, `codex-reviewer` e `agy-reviewer`, con le app `*-agent` di oggi. Implementer: `claude-implementer`, poi `codex-implementer` e `agy-implementer`, tutte con l'unica app implementer, dichiarata con `app = "implementer"`. Supervisore: `claude-supervisor`, che non sta in `[agents]` perché devloop non lo esegue: è descritto in `[roles.supervisor]` con `kind`, `provider` e modello, e firma con l'app del supervisore. Le chiavi `claude`, `codex` e `agy` scompaiono. I controlli di conflitto confrontano agent effettivo e fornitore (D8), mai le chiavi;
  - migrazione delle chiavi: con la coda vuota (D10); il dispatcher si ferma all'avvio se trova PR aperte con marker che citano chiavi assenti o con un ruolo diverso; i lettori dei record storici mappano le vecchie chiavi senza riscriverle; la memoria dei modelli non disponibili si migra; la chiave dell'advisory reviewer di #369 diventa `claude-reviewer`. I dettagli stanno in P5-3 e P5-4.
- **D7.** Opzione (c): la label `impl:<agent>` prevale, poi la mappa per area, poi il default. La label la possono mettere gli utenti fidati e il supervisore, con le regole di #178. La label e la mappa scelgono la chiave `<agent>-implementer`. Il default diventa `roles.implementer = "claude-implementer"`.
- **D8.**
  - regola sul fornitore (ii);
  - dichiarazione del fornitore (c);
  - claude come terzo reviewer, con un modello diverso da quello dell'implementer claude (Sonnet 5.5) e in **ultima posizione** del pool, come riserva. Il terzo reviewer è la chiave `claude-reviewer`: entra nel pool con la migrazione delle chiavi;
  - divieto dei fallback sul fornitore degli autori.
  - **Integrazione del 2026-11-19** (decisione dell'operatore delle 01:30, diario #76, opzione B, [#416](https://github.com/brushtyler/ssdlc/issues/416), presa dopo l'approvazione di questo ADR): quando l'implementer firma con l'app implementer (P5-3), un reviewer dello stesso agent dell'implementer, per esempio claude quando implementa `claude-implementer`, fa una review **aggiuntiva e solo per i finding**:
    - i suoi `request_changes` producono finding che entrano nel ciclo di correzione come quelli degli altri reviewer;
    - la sua approvazione **non conta per il quorum**: la decisione di chiudere i suoi finding e di approvare resta ai reviewer del quorum, che così ne validano le richieste;
    - **non sostituisce un reviewer mancante**: quel ruolo resta a `claude-reviewer` nel pool, con un modello diverso;
    - ha un modello diverso da quello dell'implementer (per esempio Sonnet 5.5 contro Opus 5.5), è attivo solo al livello 2, la sua review è cieca sull'autore (#389), il suo verdetto non entra nel quorum né nel limite dei round come round a sé (i suoi finding si aggiungono al round in corso), ed è registrato nel ledger e nella ricevuta della PR come "finding-only";
    - è spento per default (D10);
    - la review consultiva (#369) resta per le PR che arrivano all'operatore.

    **Motivo:** con lo stesso modello e gli stessi punti ciechi, la sua approvazione aggiunge poca evidenza indipendente; i suoi finding invece sono punti concreti e verificabili. Il resto di D8 non cambia.
- **D9.** Due finestre fisse, alle **9 e alle 18 UTC**, opzione (a). Fuori finestra si interrompe l'operatore solo per sicurezza o per un fermo totale.
- **D10.** Tutto spento per default. Si attiva una cosa per volta, nell'ordine raccomandato: retry, round, app implementer e app del supervisore, terzo reviewer, capacità, budget. Il cambio delle chiavi degli agent si attiva con la coda vuota: nessuna PR aperta né azione in corso.
  - **Precisazione del 2026-11-19** (decisione dell'operatore, opzione A, nel [commento su #461](https://github.com/brushtyler/ssdlc/issues/461#issuecomment-7012437974), presa dopo l'approvazione di questo ADR): il vincolo di merge di un `needs_operator` per la policy di fiducia (`operator_reason` `trust_policy`, #434) cade solo se lo stesso reviewer che l'ha dato approva l'head finale, ha chiuso lui stesso i suoi finding e su quell'head nessun reviewer del quorum dichiara toccata la policy nei casi di #447; altrimenti la PR va all'operatore come prima. È il caso di una PR che al primo round sbaglia una regola e al round approvato attua fedelmente una decisione registrata (#457): il gate segue l'ultimo round, non un round precedente. Il resto di D10 non cambia.
  - **Precisazione del 2026-11-19, su #472** (decisione dell'operatore nel [commento su #472](https://github.com/brushtyler/ssdlc/issues/472), presa dopo l'approvazione di questo ADR): le PR che attuano fedelmente una decisione già approvata dall'operatore, con le correzioni delle review e senza finding aperti, non vanno all'operatore per il merge, perché rispettano già la sua volontà. Il punto 3 della regola dei reviewer (#447) resta solo per la regola stessa: la sezione "Trust policy" dei prompt di review e di advisory e le condizioni che decidono quando serve l'operatore. Una modifica di `NON_CRITICAL`, di un comando `/devloop` o di una condizione di merge che una decisione registrata prescrive esplicitamente, nominando la classe, il comando o la condizione, è un'attuazione fedele e segue il punto 1; un'aggiunta che la decisione non nomina resta all'operatore. La decisione si prende sulla issue, non sulla PR: il controllo di prontezza dichiara se la issue tocca la policy di fiducia e, se nessuna decisione la copre, la manda all'operatore con la classe `operator_decision` e la domanda "approvi la modifica della policy di fiducia descritta qui?", prima dell'implementazione. Conta come decisione anche un commento di approvazione dell'operatore sulla issue, scritto da un utente di `trusted_users` (non dal supervisore né da un agent) dopo l'ultima modifica del testo e prima della presa in carico; una modifica del testo successiva lo rende non valido. Con quella decisione una PR fedele, senza finding aperti e con la CI verde, si mergia da sola, salvo i percorsi di `operator_only_paths`. Restano all'operatore le modifiche non coperte da una decisione, quelle che vanno oltre o contro, gli indebolimenti non autorizzati, la regola stessa e `operator_only_paths`. Il resto di D10 non cambia.

Restano **aperti**, e chi li incontra li tratta come decisioni dell'operatore ancora da prendere:

- i tetti per chiave di `max_open_prs`, `max_parked_prs` e `max_parallel_actions`, che l'operatore fissa e che P5-8 scrive come costanti del codice (D4);
- la creazione delle due app e i loro permessi, che sono credenziali dell'operatore (D6);
- l'eventuale scrittura delle finestre in `AGENTS.md` (D9).

Restano valide tutte le decisioni riservate degli ADR 0001, 0002 e 0004, salvo le revisioni limitate di D2.

### Domande come proposte

Registro storico: le domande con le alternative e le raccomandazioni, come le poneva la versione proposta di questo ADR, prima delle decisioni qui sopra.

Questo ADR le elenca, con le alternative e le raccomandazioni, ma **non le prende**. Restano valide tutte le decisioni riservate degli ADR 0001, 0002 e 0004, salvo le revisioni che l'operatore approva qui.

- **D1. Approvazione di questo ADR,** con il merge della sua PR.
- **D2. Revisione limitata della decisione 5 dell'ADR 0001,** e di conseguenza dei principi 3 e 4 dell'ADR 0002 e della sezione 3.3 e della D2 dell'ADR 0004, solo per le deleghe della [sezione 3.1](#31-deleghe-al-supervisore). L'operatore può approvarle tutte, una parte o nessuna.
- **D3. Tolleranza di budget:** 10%, 25% o 50%. La raccomandazione è 25%.
- **D4. Tetti delle deleghe:**
  - +2 round per PR, proposto;
  - il tetto giornaliero di concessioni, proposto 3;
  - i tetti di capacità per `max_open_prs`, `max_parked_prs` e `max_parallel_actions`, e la durata massima di un limite temporaneo, proposta 24 ore.
- **D5. Finding di sicurezza:** campo nello schema della review, approssimazione per percorso, o entrambi. La raccomandazione è il campo, con la regola che fallisce chiuso.
- **D6. Credenziali, permessi e identità dei ruoli.** L'operatore ha indicato il 2026-11-18 la proposta qui sotto, che diventa decisione con l'approvazione di questo ADR:
  - due app nuove, una per ruolo, tutte e due uniche: `brushtyler-devloop-implementer` per l'implementer, qualunque agent faccia il lavoro, e `brushtyler-devloop-supervisor` per il supervisore (comandi, label, issue, diario). La creazione e i permessi di ciascuna, proposti nella [sezione 3.2](#32-app-per-ruolo-implementer-generico-e-agent-selezionabile);
  - `claude-agent`, `codex-agent` e `agy-agent` restano ai reviewer; `dispatcher` e `merger` non cambiano;
  - scripts/agent-token.py non espone né l'app implementer né l'app del supervisore;
  - le tre app di implementer, una per agent, non sono un'alternativa raccomandata;
  - **coordinatore:** oggi è l'operatore, che verifica il lavoro del supervisore e le sue deleghe (sezione 1 e regola 5 della [sezione 3.1](#regole-comuni-a-tutte-le-deleghe)). Se il ruolo passasse a un agent, avrebbe un'app propria, distinta da quella del supervisore: l'assegnazione e l'app sono decisioni dell'operatore (docs/adr/0002-ruoli-e-responsabilita.md:93).
- **D7. Selezione dell'implementer:** label `impl:<agent>`, mappa per area, o entrambe con un default. La raccomandazione è entrambe. Chi può mettere la label è una regola di fiducia: la proposta è utenti fidati e supervisore, come per le priorità (#178).
- **D8. Reviewer:**
  - la regola sul fornitore: (i), (ii) o (iii); la raccomandazione è (ii);
  - claude come terzo reviewer, con quale modello e in quale posizione del pool. Con le app di ruolo di D6, claude può fare la review anche del codice implementato da claude, se rispetta la regola sul fornitore e sul modello scelta qui;
  - la dichiarazione del fornitore: (a), (b) o (c); la raccomandazione è (c).

  Il divieto dei fallback sul fornitore degli autori ([Fallback dello stesso fornitore](#fallback-dello-stesso-fornitore)) non dipende dalla regola scelta qui e non è un'alternativa: è richiesto dall'issue #370.
- **D9. Ritmo:** le finestre, i loro orari e i casi che giustificano un'interruzione fuori finestra.
- **D10. Attivazione.** Ogni delega e ogni cambio di ruolo è spento per default e lo accende l'operatore, uno per volta, in quest'ordine raccomandato: retry dei fallimenti tecnici, round, app implementer e app del supervisore, terzo reviewer, capacità, budget.

## 5. Piano delle issue

Ogni issue si apre dopo l'approvazione di questo ADR, una per area. Le issue nascono dopo il merge della PR #373 e citano la sezione di questo ADR su `main` che attuano, come chiede il [ciclo degli ADR](README.md#ciclo-di-un-adr).

- Tutte toccano `devloop/` e richiedono il livello 2 di review: `devloop/` è in `sensitive_paths` (devloop/config.toml:93).
- Quelle che toccano `AGENTS.md` o `CLAUDE.md` le mergia l'operatore (`operator_only_paths`, devloop/config.toml:141). `devloop/config.toml` non è riservato.
- Dopo P5-0, una PR che cambia la policy di fiducia, compreso il codice che la applica, va all'operatore con il verdetto `needs_operator` dei reviewer (regola 2 della [sezione 3.1](#regole-comuni-a-tutte-le-deleghe)). Ne fanno parte quasi tutte le issue di questo piano: tetti delle deleghe, app e identità, pool dei reviewer.
- Ogni issue aggiorna i documenti che la mappa `[docs.map]` lega ai file che cambia.
- Nessuna issue modifica i file fissati dalla baseline del replay gate né il comportamento del motore locale.
- Le nuove funzioni restano spente finché l'operatore non le attiva (D10).

| Issue | Titolo proposto | Perimetro | Dipendenze | Review |
|---|---|---|---|---|
| P5-0 | feat(devloop): i reviewer riconoscono le modifiche alla policy di fiducia | Il prompt della review (devloop/prompts/review.md) chiede di riconoscere le modifiche alla policy di fiducia, con l'elenco della regola 2 della [sezione 3.1](#regole-comuni-a-tutte-le-deleghe) compreso il codice che la applica, e di dare in quel caso il verdetto `needs_operator` con il motivo. Il prompt della review consultiva (devloop/prompts/advisory.md) chiede di riconoscerle e di segnalarle negli `operator_checks`. Il prompt della proposta del conclave le tratta come perimetro riservato (`RESERVED`). I tetti delle deleghe non nascono qui: sono costanti del codice nelle issue che introducono ciascuna delega | Approvazione dell'ADR | Livello 2. Cambia la regola stessa, quindi va all'operatore |
| P5-1 | feat(devloop): comando di concessione dei round con tetti e registro | `/devloop grant-round <motivo>`; classe non critica `round_grantable` quando le condizioni della [sezione 3.1](#round-di-review) sono verificate; limite per PR = `max_review_rounds` + esentati + concessi; tetto +2 per PR e tetto giornaliero comune come costanti del codice, che configurazione e overlay possono solo abbassare; record `supervision` ed `event` del ledger; chiave di revoca; comando di estensione rifiutato senza il motivo che porta l'analisi delle cause (regola 9). Campo `security` dei finding nello schema e nel prompt della review, secondo D5. Le regole comuni delle deleghe nascono qui e le riusano P5-2, P5-7 e P5-8 | P5-0, approvazione dell'ADR, D2, D4 e D5 | Livello 2 |
| P5-2 | feat(devloop): retry dei fallimenti tecnici sulle PR dello stesso agent del supervisore | Il controllo di devloop/dispatch_supervision.py:106-107 non vale più per `tool_failure`; resta per le altre classi. Limite di 2 retry invariato e condiviso con l'operatore. Chiave di revoca | P5-0, approvazione dell'ADR e D2 | Livello 2 |
| P5-3 | feat(devloop): app implementer, chiavi per ruolo e selezione dell'agent | Attore riservato `implementer` con la sua app per commit, push, PR e record dei fix; scripts/agent-token.py non lo espone; `identity` del profilo uguale all'app implementer. Chiavi con agent e ruolo per esteso (`claude-implementer`, `codex-reviewer`, …) e app dichiarata dall'agent (`app = "implementer"` per gli implementer), con i vincoli della sezione [Chiavi degli agent e app dichiarata](#chiavi-degli-agent-e-app-dichiarata); controlli di conflitto per agent effettivo e fornitore, mai per chiave. Migrazione con la coda vuota: arresto all'avvio con PR aperte i cui marker citano chiavi assenti o con un ruolo diverso, lettura dei record storici con le vecchie chiavi `claude`, `codex` e `agy` senza riscriverli, migrazione della memoria dei modelli non disponibili, `advisory_reviewer` di #369 a `claude-reviewer`. Default `roles.implementer = "claude-implementer"`. Selezione dell'agent secondo D7, registrata nel record `request` e nel marker della PR; controllo del preflight per issue; esclusione del pool per PR. `AGENTS.md`, tabella delle identità, aggiornato: la mergia l'operatore | P5-0, approvazione dell'ADR, D6 (app implementer creata dall'operatore) e D7 | Livello 2, merge dell'operatore |
| P5-4 | feat(devloop): app del supervisore | Attore riservato `supervisor` con la sua app di ruolo `[apps.supervisor]` per comandi, label, issue e diario, che nessun agent dichiara: `_may_hold`, gli ammessi alle label e i comandi sulle escalation la riconoscono al posto dell'app dell'agent `roles.supervisor` (devloop/dispatcher.py:679-693). `[roles.supervisor]` descrive l'attore `claude-supervisor`, fuori da `[agents]`, con `kind`, `provider` e modello, per il controllo sulle PR dello stesso agent. scripts/agent-token.py non la espone; percorso dedicato con cui il supervisore ottiene il token. Parte della migrazione delle chiavi che riguarda il supervisore, con lo stesso requisito minimo di P5-3. `AGENTS.md`, tabella delle identità, aggiornato: la mergia l'operatore | P5-0, approvazione dell'ADR e D6 (app del supervisore creata dall'operatore). Indipendente da P5-3 | Livello 2, merge dell'operatore |
| P5-5 | feat(devloop): esclusione dei reviewer per agent effettivo e terzo reviewer | Chiave `provider` per agent e fallback, tabella di prefissi come controllo; regola di quorum sul fornitore secondo D8, a entrambi i livelli; indipendenza per tupla anche al livello 1; salto del reviewer non disponibile alla scelta di un nuovo round; claude come terzo reviewer nel pool con la chiave `claude-reviewer` e l'app `claude-agent` (`[apps.claude]`), in ultima posizione: `["codex-reviewer", "agy-reviewer", "claude-reviewer"]` | P5-3 (con la migrazione delle chiavi), P5-4 e D8 | Livello 2 |
| P5-6 | feat(devloop): nessun fallback sullo stesso fornitore dell'implementer | Divieto a runtime, a entrambi i livelli e indipendente dalla regola di quorum di D8, di ogni fallback di un reviewer sul fornitore di un autore effettivo della PR, con `tool_failure` se la catena non ha modelli ammessi; fallback dell'implementer e dei fixer registrati con il fornitore; rifiuto all'avvio dei reviewer con un fallback sul fornitore di un possibile implementer diverso dal fornitore del loro modello primario | P5-5 | Livello 2 |
| P5-7 | feat(devloop): tolleranza dell'envelope di budget delegata al supervisore | `/devloop grant-budget <motivo>`: una volta per storia, solo turni e tempo, del X% deciso in D3, mai USD né token; registro, revoca e analisi delle cause come P5-1 | #365, P5-1 e D3 | Livello 2 |
| P5-8 | feat(devloop): limiti di capacità temporanei del supervisore | `/devloop capacity` sulla issue del riepilogo, entro i tetti di D4 e con scadenza; ritorno automatico al valore configurato; analisi delle cause come P5-1 | P5-1 e D4 | Livello 2 |
| P5-9 ([#416](https://github.com/brushtyler/ssdlc/issues/416)) | devloop: reviewer di soli finding dello stesso agent dell'implementer | Chiave `review_policy.findings_reviewer`, spenta per default (D10), fuori dal pool dei reviewer; solo al livello 2, sulle PR il cui implementer firma con l'app implementer ed è lo stesso agent, con un modello diverso e indipendente per tupla da ogni autore; review cieca sull'autore; i suoi finding si aggiungono al round in corso, vanno in correzione e li chiudono i reviewer del quorum; la sua approvazione non conta per il quorum e non sostituisce un reviewer mancante; registrato come "finding-only" nel ledger e nella ricevuta della PR (integrazione di D8 del 2026-11-19) | P5-3 e P5-5 | Livello 2, merge dell'operatore (tocca `docs/adr/`) |

**Ordine.**

- P5-0 si mergia per prima: tutte le altre la aspettano, direttamente o attraverso le loro dipendenze, perché cambiano il codice che applica la policy di fiducia e la regola per i reviewer le deve già coprire.
- P5-1 e P5-2 sono indipendenti fra loro e partono dopo P5-0. P5-2 è la più piccola e toglie da sola il caso di #357.
- P5-3 e P5-4 partono dopo P5-0, quando l'operatore ha creato la rispettiva app (D6), e sono indipendenti fra loro. P5-5 le aspetta tutte e due: finché `claude-agent` firma il codice dell'implementer o i comandi del supervisore, claude non può entrare nel pool dei reviewer. P5-5 aspetta anche la migrazione delle chiavi di P5-3: `claude-reviewer` entra nel pool solo dopo che l'implementer è passato a `claude-implementer`. P5-6 segue P5-5.
- Le regole 9 e 10 della sezione 3.1 non hanno un'issue a parte: P5-1 rifiuta i comandi senza motivo, e il resto è una prassi del supervisore che verifica il coordinatore.
- P5-7 aspetta #365, cioè l'envelope dell'ADR 0004 (P2-5).
- P5-8 segue P5-1 e non blocca niente.
- P5-9 segue P5-3 e P5-5: serve un implementer che firma con l'app implementer e il pool dei reviewer con le chiavi per ruolo.
- Il ritmo delle decisioni (sezione 3.4) non ha un'issue: parte con D9.

**Esclusi:**

- approvazioni da parte del supervisore, in qualsiasi forma;
- estensioni in USD o in token, e seconde estensioni dello stesso envelope;
- ogni cambio alle direttive di merge, ai livelli di review o a `operator_only_paths`;
- un file riservato per la policy di fiducia, i test di invarianti e CODEOWNERS (decisione del 2026-11-18 alle 21:50);
- più implementer sulla stessa PR.

## Precedenti

- [ADR 0001](0001-prodotto-unico-fasi-su-tracker.md#decisioni-riservate-alloperatore): decisione 5, qui rivista in modo limitato.
- [ADR 0002](0002-ruoli-e-responsabilita.md#critico-e-non-critico): ruoli, principi 3 e 4, classi critiche e non critiche.
- [ADR 0004](0004-passo-2-budget-stato-durevole.md#33-modello-di-prenotazione-del-budget): envelope per storia e regola sulle estensioni; formato del piano delle issue.
- #182, #258 e #321: comandi del supervisore e dell'operatore sulle escalation.
- #111 e #121: reviewer indipendenti e modelli di fallback.
- #351, #357: gli interventi dell'operatore del 2026-11-18. #367: la ripetizione delle decisioni degli ADR.
- Diario #76, 2026-11-18: richiesta dell'operatore.
