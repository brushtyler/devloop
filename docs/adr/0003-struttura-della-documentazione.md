# ADR 0003: audit della documentazione e struttura di destinazione

- **Stato:** proposto. Come previsto dall'ADR 0001, diventa approvato solo con l'approvazione e il merge dell'operatore (`operator_only_paths`).
- **Fase:** design (#205). È il binario 2 approvato dall'operatore il 2026-11-17 (diario #76): riallineare la documentazione esistente.
- **Data:** 2026-11-17.
- **Base verificata:** `main` al commit d6f3110.

## Contesto

- **Il README** (291 righe) presenta come prodotto solo "SSDLC Orchestrator", con installazione, primo progetto e comandi. devloop compare in una sola riga della sezione "Developing SSDLC" (README.md:285).
- **Il lavoro reale passa da devloop.** Gli ADR [0001](0001-prodotto-unico-fasi-su-tracker.md) e [0002](0002-ruoli-e-responsabilita.md) stabiliscono un prodotto unico, con le fasi su issue e PR, e i ruoli.
- **`docs/devloop.md`** è cresciuto per accumulo, una issue dopo l'altra: oggi ha 1848 righe.
- **Gli altri 17 documenti in `docs/`** (4073 righe in tutto, ADR compresi) sono stati scritti in momenti diversi. Prima di questo audit non si sapeva quali fossero ancora fedeli al codice.

Questo ADR contiene solo analisi e proposta: non modifica nessun documento esistente. Le scelte di presentazione del prodotto, i nomi pubblici e ciò che si elimina restano **proposte con alternative**, da decidere da parte dell'operatore (vedi [Decisioni riservate all'operatore](#decisioni-riservate-alloperatore)).

## Metodo

- **Perimetro.** Rientrano nell'inventario:
  - `README.md` e `AGENTS.md`;
  - tutti i file Markdown in `docs/`, compresi gli ADR;
  - la documentazione che si trova in `devloop/`, cioè i prompt in `devloop/prompts/` e i commenti di `devloop/config.toml`. Le docstring dei moduli sono escluse, come chiede l'issue, e gli schemi JSON sono contratti, non documenti.
  - Gli altri file Markdown del repository sono elencati a parte, in [Fuori perimetro](#fuori-perimetro).
- **Verifica.** Per ogni documento ho estratto le affermazioni controllabili e le ho confrontate con il codice di `main`, aprendo il file e la riga: comandi e opzioni CLI, valori di default, nomi di file, di stati e di codici di rifiuto, file di test citati, stato di implementazione dichiarato.
  - I documenti brevi o centrali sono stati letti per intero: README, AGENTS, ADR, `docs/devloop.md`, architettura, configurazione, supervisione, troubleshooting, task mode, bounce preview, candidate capture e i prompt.
  - Per i documenti di recupero più lunghi la verifica è **a campione**: comandi, default e identificatori.
- **Stati.**
  - **fedele**: nessuna discrepanza trovata nei controlli. Non è una prova di fedeltà completa;
  - **parzialmente obsoleto**: almeno un'affermazione verificata non corrisponde più al codice, o allo stato di `main`;
  - **obsoleto**: il documento nel suo insieme non descrive più il codice. Nessun documento è risultato obsoleto.
- **Direzione.** Le affermazioni in contrasto con gli ADR 0001 e 0002, ma non con il codice, sono segnalate a parte, come "direzione". Non bastano da sole a rendere un documento parzialmente obsoleto.
- **Pubblico.**
  - **U**: utente di ssdlc;
  - **O**: operatore o supervisore di devloop;
  - **S**: sviluppatore del repository, persona o agent;
  - **D**: decisioni.

## 1. Inventario

I riferimenti hanno la forma `file:riga` del codice di `main` al commit d6f3110.

| File | Righe | Pubblico | Stato | Verifica (documento ↔ codice) | Azione proposta |
|---|---|---|---|---|---|
| `README.md` | 291 | U, S | parzialmente obsoleto | (a) README.md:250: writer fencing "not yet used by real runs" ↔ orchestrator.py:4454-4456 scrive `writer_fencing: v1` nello stato di ogni nuova run, e orchestrator.py:1545-1548 avvia sotto l'holder i container di ruolo delle run ammesse (lo dice anche docs/writer-fencing.md:7-11). (b) README.md:275: `unittest discover -s tests` "what CI runs" ↔ .github/workflows/test.yml:40 esegue `scripts/run-tests.py`: stessi test, in parallelo, con il controllo del conteggio a test.yml:36-43; il README non cita mai lo script. (c) README.md:189-192: la tabella dei comandi non ha `usage-profile-*` ↔ orchestrator.py:6871-6873. Direzione: README.md:1-5 presenta solo l'orchestratore ↔ ADR 0001 decisione 1 (0001:17); README.md:261 "No automatic merge" ↔ devloop mergia nelle modalità `strict` e `auto` (devloop/config.py:47, devloop/github.py:333-337) | Riscrivere come presentazione del repository, secondo l'alternativa scelta in [README](#readme-cosa-dice-il-repository); spostare installazione, primo progetto e comandi nella guida dell'utente |
| `AGENTS.md` | 87 | S, O | parzialmente obsoleto (minore) | AGENTS.md:44: "le quattro app `brushtyler-devloop-*`" ↔ devloop/config.toml:271-273 configura una quinta app con lo stesso prefisso, `brushtyler-devloop-merger`, che non è una fonte fidata (docs/devloop.md:1036-1040; scripts/agent-token.py:36 la riserva). Il resto è fedele: permessi di default di `scripts/agent-token.py` (righe 16-18), app del dispatcher riservata (riga 36), comando `follow` | Aggiornare la frase sulle app (percorso riservato all'operatore) |
| `docs/adr/0001-prodotto-unico-fasi-su-tracker.md` | 183 | D | fedele | Registro di decisione approvato. 0001:61 "devloop oggi importa solo la stdlib e sé stesso" ↔ tests/test_devloop_boundary.py:25-31 lo impone ancora; il passo 1 (porte, 0001:88) ↔ devloop/ports.py:43, :90, :124 | Tenere invariato |
| `docs/adr/0002-ruoli-e-responsabilita.md` | 100 | D | parzialmente obsoleto | (a) 0002:3 "Stato: proposto" ↔ l'ADR è su `main` dal merge aa1e5ce (PR #189, 2026-11-17), che secondo l'ADR 0001 ne è l'approvazione. (b) 0002:11 "Oggi devloop non mergia" e 0002:88 "Fino a quelle implementazioni devloop si comporta come in modalità `off`" ↔ le modalità esistono (devloop/config.py:47) e il dispatcher mergia (devloop/dispatcher.py:3057, devloop/github.py:333-337) | Aggiornare solo la riga di stato, con un rimando alle implementazioni (#172, #182). Il resto resta registro storico. Decisione dell'operatore |
| `docs/devloop.md` | 1848 | O, S | parzialmente obsoleto | (a) docs/devloop.md:917-918: lo stato dell'ADR 0002 "is *proposed* until the operator approves and merges it" ↔ merge aa1e5ce, come sopra. (b) docs/devloop.md:1452-1453: il test di confine esiste "so the tool can later move to its own repository" ↔ il test c'è ancora (tests/test_devloop_boundary.py:1, :25), ma la ragione non vale più: l'ADR 0001 ha scartato i repository separati (0001:27) e prevede un pacchetto `core` (0001:165). Direzione: docs/devloop.md:5-6 "not part of the SSDLC orchestrator" ↔ ADR 0001:17. Fedeli nei controlli: porte (righe 1085-1087 ↔ devloop/ports.py:43-139), `sensitive_paths` (righe 326-327 ↔ devloop/policy.py:75), `operator_only_paths` (riga 566 ↔ devloop/config.py:48), limiti e default (righe 1464-1469 ↔ devloop/config.py:216-232). Duplicazioni interne: modalità di merge descritte due volte (righe 551-603 e 933-966); ruoli copiati dall'ADR 0002 (righe 912-931 ↔ 0002:27-35). Cinque eccezioni di `tests/test_doc_links.py` (righe 36-40) riguardano questo file | Dividere in guida, riferimento e protocollo (vedi [Divisione di docs/devloop.md](#divisione-di-docsdevloopmd)) |
| `docs/architecture.md` | 127 | U, S | fedele | `00-recon/controller-contract.json` ↔ ssdlc_controller.py; `interrupted-tail-<sha256>.bin` ↔ ssdlc_tasks.py; `execution-runtimes/` e `active.json` ↔ ssdlc_feedback_delegation.py:103, :394. tests/test_journal_integrity.py:208 legge la sezione "Task journal: guarantees and threat model". Descrive solo il motore locale: niente devloop, niente porte | Aggiornare: aggiungere i componenti del prodotto e le porte, spostando qui docs/devloop.md:1078-1098 |
| `docs/configuration.md` | 187 | U | fedele | Matrice dell'effort (righe 93-97) ↔ orchestrator.py:1163-1174; preferenze di routing (righe 55-66) ↔ orchestrator.py:1265-1361; default di container, timeout, diff e tentativi (righe 112-115 e 135-138) ↔ orchestrator.py:1568-1572, :2206, :5120-5124, :6299 e ssdlc_supervisor.py:1818; budget dello sviluppatore (riga 110) ↔ orchestrator.py:221-224. Nota: la mappa associa `schemas/` a questo file, che però descrive un solo schema, `quality-contract` (riga 151) | Tenere come riferimento di configurazione di ssdlc; rivedere la voce `schemas/` della mappa |
| `docs/supervision.md` | 103 | U | fedele | `run --detach` (riga 22) ↔ orchestrator.py:6619; uscita 14 su una run terminata (riga 25) ↔ ssdlc_supervisor.py:1299; `status`, `cancel` e `monitor` ↔ orchestrator.py:6651-6668. Direzione: docs/supervision.md:8 "the GitHub lifecycle (#4) [is a] separate issue" ↔ ADR 0001:171, che dichiara #4 assorbita | Tenere; aggiornare la nota su #4 |
| `docs/troubleshooting.md` | 58 | U | fedele | I codici di stop della tabella (righe 32-43) esistono in ssdlc_design.py, ssdlc_planning.py, ssdlc_tasks.py, ssdlc_recovery.py e ssdlc_telemetry.py | Tenere; aggiungere un rimando alle escalation di devloop nella guida dell'operatore |
| `docs/task-mode.md` | 374 | U | parzialmente obsoleto | (a) docs/task-mode.md:3-9: un esempio vuoto (solo un commento) e "Before you enable it" ↔ non c'è niente da abilitare, perché orchestrator.py:828-831 ignora `SSDLC_ENABLE_TASK_BREAKDOWN`. (b) docs/task-mode.md:367: "Preview's separate 0/10/20 outcomes are described above" ↔ sono descritti in docs/bounce-preview.md:57-60 (codice: orchestrator.py:6783-6785), non in questo file. Fedeli nei controlli: contesto del design di 80.000 e 50.000 caratteri (riga 49 ↔ orchestrator.py:4890, :4901), `recover_protocol_task` (riga 133 ↔ orchestrator.py:4257), contatori (righe 369-373 ↔ orchestrator.py:2778-2780, ssdlc_bootstrap.py:33) | Aggiornare l'introduzione e il rimando; la parte "Unified execution and greenfield bootstrap" (righe 234-374) si può spostare nel riferimento |
| `docs/bounce-preview.md` | 89 | U | fedele | Codici di uscita 0, 10 e 20 (righe 57-60) ↔ orchestrator.py:6783-6785; `preview_counters` (riga 61) ↔ orchestrator.py:2689-2691. Direzione: docs/bounce-preview.md:26-28 cita la consegna su GitHub (#4) come milestone futura ↔ ADR 0001:171 | Aggiornare: togliere il linguaggio di milestone; il titolo copre solo l'anteprima, mentre metà del testo descrive l'esecutore correttivo |
| `docs/candidate-capture.md` | 146 | U, S | fedele | `schemas/candidate-capture.schema.json` esiste; il comando della riga 104 trova `tests/replay/test_candidate_capture.py`; `docs/evidence/i0-offline-validation.json` esiste. È scritto come rapporto dell'incremento I0 (#23, #29) | Tenere; separare riferimento e cronologia (issue I8) |
| `docs/bounded-recovery.md` | 349 | U | fedele (a campione) | `--limits-profile` (riga 10) ↔ orchestrator.py:6716; `tests/replay/fixtures/i2-recovery.json` e `schemas/recovery-limits.schema.json` esistono. Rapporto dell'incremento I2 | Tenere; separare riferimento e cronologia |
| `docs/continuations.md` | 174 | U | fedele (a campione) | Comandi `continuation-*` ↔ ssdlc_continuations.py:547-548 e orchestrator.py:6883-6885; `docs/evidence/i1-offline-validation.json` esiste | Tenere; collegarlo dall'indice del recupero |
| `docs/historical-reconciliation.md` | 213 | U | fedele (a campione) | `continuation-limits-inspect` (riga 17) ↔ orchestrator.py:6880-6882 e ssdlc_reconciliation.py:487. Nota: `ssdlc_reconciliation.py` non ha una voce nella mappa | Tenere; aggiungere la voce nella mappa |
| `docs/scope-amendments.md` | 249 | U | fedele (a campione) | `continuation-scope-*` ↔ orchestrator.py:6877-6879 e ssdlc_scope_amendments.py:838; `docs/evidence/post-checkpoint-scope-validation.json` esiste | Tenere; collegarlo dall'indice del recupero |
| `docs/developer-feedback.md` | 929 | U, S | fedele (a campione) | `feedback-diagnose` e `feedback-status` ↔ ssdlc_feedback.py:693-694; `feedback-profile-*` ↔ ssdlc_feedback_admission.py:79-80; `feedback-repair-*` ↔ ssdlc_feedback_return.py:296-297; `ssdlc_impact.build_report` ↔ ssdlc_impact.py:294. Mescola decisioni di #39 (righe 21-51), stato degli incrementi F1-F3 (righe 7-19) e riferimento. Due eccezioni di `tests/test_doc_links.py` (righe 34-35) riguardano questo file | Dividere in riferimento e cronologia con le decisioni (issue I8) |
| `docs/native-gate-convergence.md` | 188 | U | fedele | Allowance di diagnosi pari a 2 (riga 50) ↔ ssdlc_gate_reports.py:966; log limitati a 1 MiB (riga 92) ↔ ssdlc_gate_reports.py:717; i cinque moduli di test delle righe 129-130 esistono | Tenere |
| `docs/cost-telemetry.md` | 165 | U | fedele (a campione) | Schema `ssdlc.cost-telemetry`, versione 1 (riga 4) ↔ ssdlc_telemetry.py:27-28; `telemetry_schema_version` (riga 59) ↔ orchestrator.py:1141; `SSDLC_UNMETERED_QUOTA` ↔ ssdlc_telemetry.py:426, :454 | Tenere |
| `docs/writer-fencing.md` | 439 | U, S | fedele | Marker `writer_fencing: v1` e chiave di rifiuto (righe 281-291) ↔ ssdlc_role_fencing.py:41-43, :61-63; codici `CONTAINER_PAYLOAD_*` e `ROLE_OUTSIDE_CONTAINER` ↔ ssdlc_role_fencing.py:44-46; avvio sotto l'holder ↔ orchestrator.py:1545-1548 | Tenere |
| `devloop/config.toml` (commenti) | 273 | O | fedele | Commenti di `[merge]` (righe 107-122) ↔ devloop/config.py:47, :451-482; `sensitive_paths` (riga 78) contiene i default di devloop/policy.py:75. La riga 3 rimanda a `docs/devloop.md` | Tenere; aggiornare i rimandi dopo la divisione |
| `devloop/prompts/implement.md` | 58 | S (agent) | fedele | Segnaposto e regole coerenti con il dispatcher. La riga 5 rimanda alla sezione "Developing SSDLC" del README | Tenere; aggiornare se la sezione cambia nome |
| `devloop/prompts/fix.md` | 62 | S (agent) | fedele | Coerente con implement.md | Tenere |
| `devloop/prompts/sync.md` | 45 | S (agent) | fedele | Divieti su `git merge` e `git commit` ↔ docs/devloop.md:47-56 | Tenere |
| `devloop/prompts/preflight.md` | 51 | S (agent) | fedele | Default di `operator_only_paths` ↔ devloop/config.py:48; decisioni riservate ↔ ADR 0002:19. La riga 6 rimanda alla sezione "Developing SSDLC" | Tenere; aggiornare se la sezione cambia nome |
| `devloop/prompts/review.md` | 81 | S (agent) | parzialmente obsoleto | devloop/prompts/review.md:3: "Your verdict decides whether the PR goes to a human for merge" ↔ con `[merge].mode` `strict` o `auto` mergia devloop (devloop/config.py:47, devloop/dispatcher.py:3057). La riga 10 rimanda alla sezione "Developing SSDLC" | Aggiornare la frase (percorso `devloop/`, livello 2) |

### Fuori perimetro

Questi file non rientrano nel perimetro dell'issue. Sono elencati perché la struttura di destinazione li tocca, ma il loro stato **non è stato valutato**.

| File | Righe | Nota |
|---|---|---|
| `CLAUDE.md` | 1 | Include solo `AGENTS.md` |
| `PLAN_AUTONOMY_WORKSTREAMS.md`, `PLAN_TASK_ROUTING_AND_SUBGOALS.md` | 350, 679 | Piani interni, linkati da README.md:254 e citati dai documenti di recupero. tests/doc_contracts.py:7-8 li esclude di proposito dalle pagine che valgono come documentazione. Candidati a un audit successivo |
| `standards/*.md` | 7 file | Standard copiati nei progetti di destinazione: contenuto per i target, non documentazione del repository |
| `tests/replay/README.md` | — | Documentazione delle fixture di replay |
| `docs/evidence/*.json` | 29 file | Evidenze, non documenti |

### Quadro d'insieme

- **Il codice di ssdlc è documentato con fedeltà.** Nei controlli nessun documento sul motore locale è risultato obsoleto. I problemi sono di struttura:
  - i documenti di recupero sono **rapporti d'incremento** (I0, I1, I2, F1-F3, #22), con riferimento, cronologia e decisioni mescolati;
  - manca un indice "quale stop → quale transizione → quale documento";
  - il README fa insieme da guida introduttiva e da indice.
- **Le discrepanze verificate sono poche e puntuali.** Sono concentrate nelle frasi di **stato** ("not yet", "proposed", "oggi non"), che il codice ha superato senza che qualcuno tornasse sul testo. Si correggono senza ristrutturare: vedi l'issue I1 del [Piano](#3-piano).
- **`docs/devloop.md` è accurato nei dettagli,** grazie alla mappa di #203 che lo lega a `devloop/`, ma è illeggibile come guida:
  - guida operativa, riferimento di configurazione e protocollo dei marker sono intrecciati;
  - due sezioni ripetono contenuti presenti altrove nel file o nell'ADR 0002.
- **Molta documentazione è vincolata da test.** Una ristrutturazione deve tenerne conto:
  - nove moduli `tests/test_devloop*.py` leggono `docs/devloop.md` per percorso o per titolo di sezione: `tests/test_devloop.py`, `tests/test_devloop_docs.py`, `tests/test_devloop_merge.py`, `tests/test_devloop_parked.py`, `tests/test_devloop_pin.py`, `tests/test_devloop_policy.py`, `tests/test_devloop_supervision.py`, `tests/test_devloop_supervisor_labels.py` e `tests/test_devloop_watch.py`;
  - tests/test_journal_integrity.py:208 legge una sezione di `docs/architecture.md`;
  - i contratti di `tests/doc_contracts.py` valgono solo per le pagine raggiungibili dal README attraverso link relativi;
  - `tests/test_doc_links.py` controlla link, ancore e percorsi citati, e ha un elenco di eccezioni da risolvere in questo audit: righe 28-41, discusse nell'issue I1.

## 2. Struttura di destinazione

La struttura segue gli ADR 0001 e 0002: un solo prodotto nello stesso repository, con due componenti che oggi convivono e convergeranno al passo 6 dell'ADR 0001. Le fasi sul tracker restano un percorso aggiuntivo finché la parità non è dimostrata.

### README: cosa dice il repository

Oggi il README è insieme presentazione, guida introduttiva di ssdlc e riferimento rapido. Le proposte qui sotto riguardano **cosa dice**. Il testo finale lo approva l'operatore.

**Alternativa A: prodotto unico in convergenza.** Il README presenta il repository come un solo prodotto, con due componenti e uno stato dichiarato.
- **Cos'è.** Un sistema per sviluppare software con agent AI sotto autorità esplicita dell'operatore, con fasi, review indipendenti ed evidenze.
- **Componenti.**
  - il **motore locale** (`orchestrator.py`, `ssdlc_*.py`): fasi, gate ed evidenze su un host, per un progetto di destinazione;
  - il **dispatcher** (`devloop/`): issue e PR su GitHub, implementer e reviewer indipendenti, merge secondo le direttive.
- **Stato oggi.**
  - devloop è il modo in cui si sviluppa questo repository ed è configurato per esso: repository, utenti fidati e app.
  - Il motore locale resta invariato nelle garanzie fino alla parità (ADR 0001, vincolo 1).
  - Le fasi di ssdlc sul tracker non esistono ancora (passo 3).
- **Direzione.** I passi dell'ADR 0001, con quelli già fatti: le porte del tracker in `devloop/ports.py`.
- **Indice per pubblico.** Vedi la [tabella delle guide](#come-si-separano-le-guide).

Pro:
- è coerente con la decisione 1 dell'ADR 0001 e con i ruoli dell'ADR 0002;
- mostra il flusso di lavoro reale;
- elimina la contraddizione "devloop is not part of the SSDLC orchestrator" (docs/devloop.md:5-6).

Contro:
- presenta come parte del prodotto un componente che oggi funziona solo su questo repository: chi arriva da fuori può aspettarsi di poterlo installare altrove;
- descrive una convergenza non ancora fatta, e va aggiornato a ogni passo dell'ADR 0001.

**Alternativa B: solo ssdlc, con devloop come strumento interno.** Il README resta la guida dell'utente di ssdlc. devloop compare in una sezione "Come si sviluppa questo repository", con il rimando alla guida dell'operatore.

Pro:
- è onesto su cosa un utente esterno può installare oggi;
- è il cambiamento minimo.

Contro:
- contraddice la decisione 1 dell'ADR 0001;
- lascia invisibile il flusso con cui si lavora davvero;
- andrà riscritto alla convergenza.

**Alternativa C: README come indice compatto.** Circa 80-120 righe, con cos'è, componenti, stato, direzione e link alle guide. Installazione e primo progetto passano interamente alla guida dell'utente. È compatibile con A e con B, che decidono **cosa** dice.

Pro:
- è breve e stabile, quindi più facile da tenere fedele;
- ogni pagina ha un solo pubblico.

Contro:
- chi arriva non trova un avvio rapido nella prima pagina;
- un clic in più.

**Raccomandazione: A nella forma compatta di C.** Lo stato va scritto in modo esplicito, così che il README non prometta più di ciò che c'è. A segue gli ADR già approvati. La forma compatta riduce il rischio che le frasi di stato invecchino, che è il difetto principale trovato dall'audit.

**Nomi pubblici: proposta, decisione dell'operatore.**
- (i) "SSDLC" come nome del prodotto e "devloop" come nome del componente dispatcher. CLI, percorsi e app restano invariati. È la proposta raccomandata: non rinomina niente.
- (ii) "SSDLC" con due componenti chiamati per funzione, per esempio "motore" e "dispatcher", mentre `python3 -m devloop` resta il nome tecnico.

### Come si separano le guide

| Pubblico | Documento | Contenuto | Da dove viene |
|---|---|---|---|
| Chi arriva | `README.md` | Cos'è, componenti, stato, direzione, indice | README attuale, riscritto |
| Utente di ssdlc | docs/getting-started.md (nuovo) | Requisiti, installazione, primo progetto, configurazione di base, comandi quotidiani, dove stanno i file | README.md:75-219 |
| Utente di ssdlc | `docs/task-mode.md`, `docs/supervision.md`, `docs/troubleshooting.md`, `docs/cost-telemetry.md` | Uso del motore, invariati salvo le correzioni | — |
| Utente di ssdlc | docs/recovery.md (nuovo indice) | Stop → transizione → documento, verso `docs/candidate-capture.md`, `docs/bounded-recovery.md`, `docs/continuations.md`, `docs/historical-reconciliation.md`, `docs/scope-amendments.md`, `docs/developer-feedback.md`, `docs/native-gate-convergence.md`, `docs/bounce-preview.md` e `docs/writer-fencing.md` | Tabella README.md:238-250 e tabella degli stop di `docs/troubleshooting.md` |
| Operatore e supervisore di devloop | docs/devloop/guide.md (nuovo) | Il ciclo, label e comandi `/devloop`, escalation, supervisione (`watch`, `follow`), **installazione con l'utente dedicato, pin, launcher, overlay, rollback**, app GitHub e app merger, limiti | `docs/devloop.md`, vedi sotto |
| Riferimento di configurazione | `docs/configuration.md` (ssdlc); docs/devloop/reference.md (nuovo, devloop) | Ogni variabile e chiave, con default e vincoli; livelli di review e politiche | `docs/configuration.md`; `docs/devloop.md` |
| Sviluppatore e agent | `AGENTS.md`; docs/devloop/protocol.md (nuovo); docs/development.md oppure la sezione "Developing SSDLC" | Regole per gli agent; marker e record di devloop; test, CI, mappa dei documenti, controller fissato | `AGENTS.md`; `docs/devloop.md`; README.md:270-285 |
| Architettura | `docs/architecture.md` | Componenti del prodotto, porte e adattatori, isolamento, layout, modello d'integrità | `docs/architecture.md`; docs/devloop.md:1078-1098 |
| Decisioni | `docs/adr/` | ADR, invariati salvo la riga di stato dell'ADR 0002 | — |

**Collocazione dei file: proposta.**
- (a) **Spostamenti minimi**, raccomandata. I file attuali restano dove sono, e nascono solo docs/devloop/ (tre pagine), docs/getting-started.md, docs/recovery.md e, se l'operatore lo sceglie, docs/development.md. I link già pubblicati nelle issue e nelle PR verso i documenti di ssdlc continuano a funzionare.
- (b) **Una directory per pubblico** (docs/ssdlc/, docs/devloop/, docs/dev/). È più ordinata, ma rompe i link esterni e richiede di aggiornare i test che leggono i documenti per percorso.

**Lingua.** I documenti sono in inglese, mentre ADR e `AGENTS.md` sono in italiano. La proposta è di non cambiare, ma resta una scelta dell'operatore.

### Divisione di docs/devloop.md

**Alternative.**
- (1) **Tre documenti per funzione** (guida, riferimento, protocollo), come chiede l'issue. È la proposta raccomandata: ogni lettore legge un documento solo, e la mappa di #203 può legare ogni modulo al documento giusto.
- (2) **Due documenti per pubblico**: la guida dell'operatore e gli interni per lo sviluppatore. È più semplice, ma riferimento e protocollo restano mescolati, e il secondo documento supererebbe le 1000 righe.
- (3) **Un solo file riordinato, con indice.** Cambia meno di tutte, ma resta un documento di 1800 righe che la mappa lega a qualunque modifica di `devloop/`.

**Ripartizione proposta per l'alternativa 1**, per intervalli di righe di `docs/devloop.md`:

| Destinazione | Sezioni attuali (righe) |
|---|---|
| docs/devloop/guide.md | introduzione e "The loop" (1-33); come riprendere dopo un'escalation (65-79); `/devloop fix`, `rereview` e ripresa (109-172); limiti delle PR aperte, ordine di avvio, label del supervisore (173-239); readiness check, parte operativa (240-286, 297-304); modalità di merge, chiusura dell'issue, hold, escalation al supervisore e comandi (551-563, 604-771); "Usage", "Watching a tick", "Waking the supervising agent" (1099-1246); "Stalled agents" (1297-1326); installazione con l'utente dedicato, pin, launcher, overlay, rollback, pin dopo un merge (1327-1453); "Limits" (1809-1848) |
| docs/devloop/reference.md | livello di review, livello 2, `main` che avanza, livello 0 (305-522); identità GitHub e app merger, creazione e configurazione (968-985, 997-1077); configurazione e sandbox dei reviewer (1455-1511); niente lavoro in background, azioni parallele, test locali, mappa dei documenti, permessi di agy, modelli di fallback e memoria (1512-1808) |
| docs/devloop/protocol.md | meccanica della sync (34-64); log, redazione dei segreti e conteggio dei round (80-108); marker della PR (166-172); attribuzione e hash del readiness check (262-296); ricevute (523-550); direttive e record del merge (564-602); traccia della supervisione (766-771); review strutturata, copertura dei criteri, finding e loro chiusura, marker precedenti a #110, lettura dei marker (772-900); attribuzione (901-911); record legati alle identità (986-996); evidenze e telemetria (1247-1296) |
| `docs/architecture.md` | porte e adattatori (1078-1098) |
| Rimando all'ADR 0002 | "Who does what" e la seconda "Merge modes" (912-966): oggi duplicano l'ADR 0002 e le righe 551-603. Si propone di sostituirle con un rimando; eliminarle è una decisione dell'operatore |

Le tre pagine stimate misurano circa 700, 650 e 450 righe.

**Cosa resta di `docs/devloop.md`: decisione dell'operatore.**
- (i) Un indice di poche righe verso le tre pagine. È la proposta raccomandata: le issue e le PR chiuse contengono link a `docs/devloop.md`.
- (ii) Eliminarlo.

Nei due casi le ancore attuali non funzioneranno più.

## 3. Piano

Ogni issue si apre dopo l'approvazione di questo ADR, una per area, con il perimetro che segue. Le issue che toccano `devloop/` richiedono il livello 2 di review (`sensitive_paths`). Quelle che toccano `AGENTS.md` o `docs/adr/` le mergia l'operatore (`operator_only_paths`).

| Issue | Titolo proposto | Perimetro | Dipendenze | Review |
|---|---|---|---|---|
| I1 | docs: correzioni di fedeltà senza ristrutturare | README.md:189-192, :250 e :275 (con `scripts/run-tests.py`); docs/task-mode.md:3-9 e :367; docs/devloop.md:917-918 e :1452-1453; le note su #4 in docs/supervision.md:8 e docs/bounce-preview.md:26-28. Le sette eccezioni di tests/test_doc_links.py:33-41: riformulare gli esempi, o usare `<!-- doc-links: ignore -->` dove non spezza il paragrafo, e svuotare l'elenco | Nessuna | Livello 1 |
| I2 | docs: correzioni nei percorsi riservati | AGENTS.md:44 (quinta app, `brushtyler-devloop-merger`); riga di stato dell'ADR 0002 (0002:3), con il rimando a #172 e #182 | Nessuna | Merge dell'operatore |
| I3 | devloop: prompt di review allineato alle modalità di merge | devloop/prompts/review.md:3 | Nessuna | Livello 2 |
| I4 | docs: divisione di docs/devloop.md in guida, riferimento e protocollo | Spostamento delle sezioni secondo la [ripartizione](#divisione-di-docsdevloopmd), **senza riscrivere il contenuto**; `docs/devloop.md` come indice o eliminato, secondo la decisione dell'operatore; link in `README.md` e in `docs/`; i nove test che leggono `docs/devloop.md` (vedi [Quadro d'insieme](#quadro-dinsieme)); voci `devloop/` di `[docs.map]` e commento della riga 3 di `devloop/config.toml`; eccezioni di `tests/test_doc_links.py` rimaste | I1 (stesse righe di `docs/devloop.md`) | Livello 2 (`devloop/config.toml`) |
| I5 | docs: guida dell'operatore e del supervisore senza duplicati | Riscrittura di docs/devloop/guide.md: rimando all'ADR 0002 al posto delle righe 912-966; una sola descrizione delle modalità di merge; percorso d'installazione dall'inizio alla fine (utente dedicato, chiavi, pin, overlay, avvio, rollback) | I4 | Livello 1 |
| I6 | docs: README secondo l'alternativa approvata e guida introduttiva | `README.md`; docs/getting-started.md (nuovo). Si conserva l'ancora `#recovery-and-continuation`, usata da docs/architecture.md:81, docs/task-mode.md:9 e docs/troubleshooting.md:26, oppure si aggiornano quei link. Le pagine con i contratti di `tests/doc_contracts.py` devono restare raggiungibili dal README. Se la sezione "Developing SSDLC" cambia nome o si sposta, nella stessa PR si aggiornano devloop/prompts/implement.md:5, devloop/prompts/review.md:10 e devloop/prompts/preflight.md:6 | Decisione sull'alternativa per il README; I4, per i link alla guida di devloop | Livello 1, oppure livello 2 se tocca i prompt |
| I7 | docs: architettura del prodotto | `docs/architecture.md`: componenti, relazione fra motore e dispatcher, porte e adattatori (da docs/devloop.md:1078-1098), confine di `tests/test_devloop_boundary.py` e futuro pacchetto `core` (ADR 0001:165). Il titolo letto da tests/test_journal_integrity.py:208 resta invariato | I4 | Livello 1 |
| I8 | docs: indice del recupero e separazione fra riferimento e cronologia | docs/recovery.md (nuovo); `docs/developer-feedback.md` diviso in riferimento e cronologia con le decisioni di #39; le parti d'incremento (I0, I1, I2, F1-F3, #22) di `docs/candidate-capture.md`, `docs/bounded-recovery.md`, `docs/continuations.md`, `docs/historical-reconciliation.md` e `docs/scope-amendments.md` spostate in sezioni "Cronologia", **senza eliminare contenuti**. Le frasi richieste da `tests/doc_contracts.py` restano raggiungibili | I6, per il link dal README | Livello 1 |
| I9 | docs: guida per chi sviluppa | La sezione "Developing SSDLC" ampliata, oppure docs/development.md: `scripts/run-tests.py`, singolo modulo, test della documentazione (`tests/test_doc_links.py`, `tests/doc_contracts.py`), CI di devloop (`.github/workflows/devloop.yml`), regola della mappa, controller fissato | I6 | Come I6 |
| I10 | devloop: estensione della mappa codice → documenti | Le voci della tabella qui sotto per i moduli oggi senza documento. Ogni issue da I4 a I9 aggiorna da sé le voci dei documenti che crea o rinomina; I10 copre il resto, dopo aver verificato modulo per modulo | I4, I6, I8, I9 | Livello 2 |

Ordine: I1, I2 e I3 sono indipendenti e possono partire subito. Poi I4, seguita in parallelo da I5 e I7. I6 dipende dalla decisione sul README, I8 e I9 vengono dopo I6, e I10 chiude il piano.

### Estensione della mappa codice → documenti (#203)

La mappa unisce le voci che coprono un file (devloop/config.py:108-120). Una voce generica, quindi, si somma alle voci specifiche invece di essere sostituita da esse. Proposta:

| Chiave | Documenti | Issue |
|---|---|---|
| `devloop/` | docs/devloop/guide.md, al posto di `docs/devloop.md` come voce generica: ogni nuovo modulo resta coperto | I4 |
| `devloop/config.py`, `devloop/config.toml`, `devloop/agents.py`, `devloop/unavailability.py`, `devloop/localtests.py`, `devloop/workspace.py`, `devloop/apps.py`, `devloop/prompts/` | docs/devloop/reference.md | I4 |
| `devloop/markdown.py`, `devloop/ledger.py`, `devloop/acceptance.py`, `devloop/review.py`, `devloop/policy.py`, `devloop/schemas/` | docs/devloop/protocol.md (per `devloop/policy.py` anche docs/devloop/reference.md) | I4 |
| `devloop/dispatcher.py` | docs/devloop/reference.md, docs/devloop/protocol.md, oltre alla guida tramite la voce generica | I4 |
| `devloop/ports.py`, `devloop/github.py` | `docs/architecture.md` | I4, I7 |
| `scripts/devloop-pinned` | docs/devloop/guide.md | I4 |
| `scripts/agent-token.py` | `AGENTS.md` | I10 |
| `orchestrator.py`, `ssdlc_tasks.py`, `ssdlc_controller.py` | docs/getting-started.md al posto di `README.md`, se il README diventa un indice; `docs/architecture.md` resta | I6 |
| `setup-ssdlc.sh` | docs/getting-started.md, `docs/configuration.md` | I6 |
| `scripts/run-tests.py` | docs/development.md, oppure `README.md` se la sezione resta lì | I9 |
| `ssdlc_continuations.py`, `ssdlc_historical.py`, `ssdlc_scope_amendments.py` | docs/recovery.md, in aggiunta alle voci attuali | I8 |
| `ssdlc_reconciliation.py` | `docs/historical-reconciliation.md` | I10 |
| `ssdlc_supervisor.py` | `docs/supervision.md` | I10 |
| `ssdlc_bounce.py`, `ssdlc_corrective.py`, `ssdlc_corrective_owners.py` | `docs/bounce-preview.md` | I10 |
| `ssdlc_candidates.py` | `docs/candidate-capture.md` | I10 |
| `ssdlc_bootstrap.py`, `ssdlc_preparations.py`, `ssdlc_design.py`, `ssdlc_planning.py`, `ssdlc_requirements.py` | `docs/task-mode.md` | I10 |
| `ssdlc_gate_reports.py`, `ssdlc_findings.py` | `docs/native-gate-convergence.md`, `docs/task-mode.md` | I10 |
| `ssdlc_usage_admission.py`, `ssdlc_replay_gate.py`, `ssdlc_grader.py` | `docs/bounded-recovery.md` (per `ssdlc_usage_admission.py` anche `docs/cost-telemetry.md`) | I10 |
| `ssdlc_owner_revalidation.py`, `ssdlc_impact.py` | `docs/developer-feedback.md` | I10 |
| `ssdlc_verification_repair.py`, `ssdlc_deadline_extensions.py`, `ssdlc_operator_recovery.py` | `docs/scope-amendments.md` | I10 |
| `ssdlc_locks.py` | `docs/writer-fencing.md` | I10 |
| `schemas/` | Da rivedere: oggi punta a `docs/configuration.md`, che descrive solo `quality-contract`. Proposta: una voce per ciascuno schema verso il documento che lo usa | I10 |

Le voci di I10 sono proposte da confermare leggendo ciascun modulo. Le chiavi non ammettono caratteri jolly, quindi serve una voce per file (docs/devloop.md:1615-1617).

## Decisioni riservate all'operatore

- L'approvazione di questo ADR, con il merge della sua PR.
- **La presentazione del prodotto nel README:** alternativa A, B o C, o una loro combinazione. La raccomandazione è A in forma compatta.
- **I nomi pubblici:** (i) o (ii). La raccomandazione è (i), senza rinominare niente.
- **Cosa si elimina:**
  - `docs/devloop.md` dopo la divisione: indice (raccomandato) oppure eliminazione;
  - le sezioni duplicate dell'ADR 0002 (docs/devloop.md:912-966): rimando (raccomandato) oppure tenerle;
  - le parti di cronologia nei documenti di recupero: spostate e non eliminate, come si raccomanda, oppure eliminate.
- La collocazione dei file, (a) o (b), e la lingua dei documenti.
- L'aggiornamento della riga di stato dell'ADR 0002 (issue I2).
- Restano valide tutte le decisioni riservate degli ADR 0001 e 0002.

## Precedenti

- [ADR 0001](0001-prodotto-unico-fasi-su-tracker.md): prodotto unico (decisione 1), piano in sei passi, decisioni riservate e approvazione degli ADR con il merge dell'operatore.
- [ADR 0002](0002-ruoli-e-responsabilita.md): ruoli, modalità di merge e `operator_only_paths`.
- #203: mappa codice → documenti in `[docs.map]` di `devloop/config.toml`.
- #204: controllo di link, ancore e percorsi (`tests/test_doc_links.py`), con le eccezioni da risolvere in questo audit.
- Diario #76, 2026-11-17: approvazione del binario 2.
