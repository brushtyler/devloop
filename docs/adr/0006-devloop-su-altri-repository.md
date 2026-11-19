# ADR 0006: devloop su altri repository (parità con il motore locale)

- **Stato:** proposto. Le decisioni riservate all'operatore sono nella [sezione 6](#6-decisioni-riservate-alloperatore), con le alternative e una raccomandazione, senza prenderle. Le correzioni di analisi chieste dal supervisore con la review consultiva del 2026-11-19 alle 00:25 sono integrate, senza prendere decisioni: lock per repository (sezioni 1.8 e 3.8), coerenza con l'ADR 0005 e confine di fiducia del file di progetto (sezione 3.2), migrazione di ssdlc (sezioni 3.2 e 5), tracciabilità dell'overlay (sezioni 3.2 e 3.9), confine dell'installazione (sezione 3.5).
- **Fase:** design (#404). Richiesta dell'operatore del 2026-11-19 nel diario #76.
- **Data:** 2026-11-19.
- **Base verificata:** il commit fba0bb8 di `main`.

## Contesto

- Il motore locale **lavora su un progetto bersaglio separato.** Il README lo descrive così: "Runs a ticket for a target project on one host" (README.md:11). Per lo sviluppo di ssdlc stesso dice: "run a pinned controller checkout against a **separate** target checkout" (README.md:93).
- devloop invece **gestisce solo il repository da cui viene eseguito.** Copia fissata, configurazione, worktree, issue e PR vengono tutti dallo stesso clone (sezione 1).
- È un divario di **parità**. L'[ADR 0001](0001-prodotto-unico-fasi-su-tracker.md) vuole un prodotto unico, e la convergenza del motore locale (passo 6 del suo piano) è condizionata alla parità dimostrata. Un prodotto che sa lavorare solo su sé stesso non è alla pari con un motore che lavora su qualsiasi progetto.
- L'operatore vuole usare devloop su repository diversi da ssdlc, con una guida rapida e un pacchetto semplice da avviare, possibilmente un container.

Questo ADR **propone, con alternative e raccomandazioni, e non decide.** Non modifica codice né altri documenti, salvo l'indice degli ADR. Le issue di attuazione nascono solo dopo l'approvazione, come per l'[ADR 0004](0004-passo-2-budget-stato-durevole.md#4-piano-delle-issue).

## Metodo

- Ogni affermazione sullo stato attuale cita il codice nella forma file:riga, al commit fba0bb8.
- Il testo delle issue citate non è stato riletto su GitHub: valgono il corpo di #404, gli ADR su `main` e i messaggi dei commit.
- Valgono i vincoli dell'ADR 0001 e le decisioni degli ADR 0001, 0002, 0004 e 0005. In particolare:
  - ssdlc e devloop restano **un prodotto unico nello stesso repository** (ADR 0001, decisione 1; opzione B "Separazione dei repository" scartata);
  - il confine di devloop si allarga solo attraverso `core` (ADR 0001, vincolo 6 e decisione 1);
  - la policy di fiducia è riconosciuta dai reviewer (ADR 0005, regola 2 della sezione 3.1). È la regola dei reviewer di #399.
- Nessuna proposta toglie garanzie esistenti: pin verificato, smoke test, lock, overlay dell'operatore, policy letta dal dispatcher e mai dal branch di una PR.

## 1. Stato attuale

### 1.1 Installazione e repository gestito

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Radice | Tutto parte dal **checkout principale del repository che contiene la directory corrente**, ricavato da `git rev-parse --git-common-dir` | devloop/__main__.py:35-39 |
| Stato | `state_dir` è relativo a quella radice: `<clone>/.ssdlc/devloop` | devloop/config.toml:9-10; devloop/__main__.py:92-93 |
| Worktree | `Workspace` riceve la stessa radice e mette i worktree in `<state_dir>/worktrees`; ogni worktree nasce da `origin/<base_branch>` di quel clone | devloop/__main__.py:104-105; devloop/workspace.py:164-167, :241-247 |
| Sorgente del pin | Il pin risolve il ref sugli **stessi** `origin` e clone: fetch dei branch e dei tag di `origin`, commit raggiungibile da un ref di `origin` | devloop/pin.py:153-192 |
| Cosa estrae il pin | `devloop/`, `core/` se esiste, e i file di `review_policy.sensitive_path_lists`, letti dal `devloop/config.toml` **del commit fissato** | devloop/pin.py:195-225, :384-398 |
| Documenti | I documenti di `[docs.map]` non si estraggono: "they belong to the repository devloop works on", e il dispatcher li cerca nel worktree di ogni turno. È già una prima separazione fra installazione e repository gestito | devloop/pin.py:223-224; devloop/config.toml:278-285 |
| Pin automatico | Dopo un merge di devloop che tocca `devloop/`, `core/` o un file delle liste, il tick successivo fissa `origin/<base_branch>` | devloop/config.toml:184-193; devloop/config.py:120, :663 |
| Launcher | `scripts/devloop-pinned` cerca la copia e l'overlay sempre in `<clone>/.ssdlc/devloop`, ed esegue devloop con la directory di lavoro sul clone | scripts/devloop-pinned:23-24, :28-42, :54-56 |
| `state_dir` fisso | Per lo stesso motivo gli script di ops rifiutano uno `state_dir` diverso | ops/params.env.example:25-28 |
| Guida | "The configuration, schemas and prompts come from the pinned copy; GitHub state and worktrees come from the clone." | docs/devloop/guide.md:1487-1490 |

La **sorgente dell'installazione**, cioè da dove viene il codice di devloop, e il **repository gestito**, cioè quello di worktree, issue e PR, oggi coincidono per costruzione.

### 1.2 Configurazione

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| File unico | `devloop/config.toml` dell'installazione è il default. Lo legge `load_config` dalla copia in esecuzione | devloop/config.py:15, :1209-1221 |
| Chiavi del progetto | `repo`, `base_branch`, `branch_prefix`, `test_cmd`, `local_tests`, `ci_rerun_unrelated`, `language`, `trusted_users`, `blocked_labels`, `priority_labels`, `protected_paths`, `sensitive_paths`, `sensitive_path_lists`, `operator_only_paths`, `[review_policy.zero]` con i check obbligatori, `[docs.map]`, `[apps]` | devloop/config.toml:6-24, :56-59, :100-109, :123-136, :153-157, :178, :286, :477-501 |
| Default generali | Limiti, timeout, agent e modelli, budget, pin, gc, run, report, ci | devloop/config.toml:75-98, :184-276, :412-472 |
| Valori di ssdlc nel codice | Il default di `sensitive_path_lists` (`schemas/recovery-grader-baseline.json`), i percorsi esclusi dal livello 0 (`devloop/prompts/`, `standards/`, `schemas/`), i `sensitive_paths` di default e i percorsi del pin automatico | devloop/config.py:19, :120; devloop/policy.py:48-52, :75 |
| Liste lette dall'installazione | `sensitive_path_lists` si legge dalla radice dell'installazione, "never from a PR branch" | devloop/config.py:16-18; devloop/config.toml:134-136 |
| Overlay locale | `<state_dir>/config.local.toml` può impostare **le stesse chiavi** di `config.toml`; le tabelle si fondono chiave per chiave, un altro valore (anche un elenco) sostituisce quello sotto | devloop/config.py:1132-1198 |
| Policy dal dispatcher | La policy di review viene dal dispatcher in esecuzione, mai dal branch della PR, e il suo hash va in ogni ricevuta | devloop/config.toml:111-116 |
| Chi la modifica | `devloop/config.toml` non è in `operator_only_paths`. Le sue modifiche alla policy di fiducia vanno all'operatore per la regola dei reviewer dell'ADR 0005. Al commit di base il prompt della review non nomina ancora la policy | ADR 0005, regola 2 della sezione 3.1; devloop/config.toml:178; devloop/prompts/review.md:61 |

### 1.3 Prompt

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Modelli | I prompt sono file Markdown dell'installazione con segnaposto `string.Template` (`$repo`, `$base_branch`, `$test_cmd`, `$language`, `$docs`, …) | devloop/context.py:72-74; devloop/prompts/implement.md:1-4, :36, :52 |
| Istruzioni di ssdlc | "Read `AGENTS.md` and the README section 'Developing SSDLC'" è scritto nel testo | devloop/prompts/implement.md:5; devloop/prompts/review.md:24; devloop/prompts/preflight.md:6; devloop/prompts/advisory.md:15; devloop/prompts/conclave_proposal.md:9; devloop/prompts/conclave_vote.md:10; devloop/prompts/fix.md:9; devloop/prompts/sync.md:7 |
| Test di un modulo | `python3 -m unittest discover -s tests -p 'test_name.py'` e il riferimento a `tests/` sono scritti nel testo | devloop/prompts/implement.md:11-14; devloop/prompts/fix.md:16-18; devloop/prompts/sync.md:17-19 |
| ADR e percorsi riservati | Il preflight cita `docs/adr/`, `AGENTS.md`, `CLAUDE.md` e la formula "Decise dall'operatore" degli ADR di ssdlc | devloop/prompts/preflight.md:25-38 |
| Mappa dei documenti | Viene da `[docs.map]` attraverso `$docs`: è già un dato, non testo fisso | devloop/config.toml:278-285 |

### 1.4 Test locali e CI

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Comando | `test_cmd = "python3 scripts/run-tests.py"` | devloop/config.toml:11-13 |
| Selezione per modulo | `local_tests = "changed"` sceglie i `tests/test_*.py` pertinenti al diff: per nome di file, con la regola `ssdlc_<nome>` → `test_<nome>`, e tutti i `test_devloop*` per un file sotto `devloop/` | devloop/localtests.py:3-8, :24-25, :76-107; devloop/workspace.py:433-436; devloop/dispatcher.py:2240-2241 |
| Comando per modulo | `python3 -m unittest discover -s tests -p <modulo>`, scritto nel codice | devloop/localtests.py:110-114 |
| Lettura dei log della CI | `failed_modules` e `suite_summary` leggono il formato di `scripts/run-tests.py` (`FAILED MODULE`, "Ran N tests in S (… modules, … jobs)"); servono a `ci_rerun_unrelated` e al riepilogo della CI nei prompt | devloop/localtests.py:13-17, :26-35, :39-73; devloop/dispatch_actions.py:287-319 |
| Permessi degli agent | I comandi ammessi agli agent seguono la forma dei test di ssdlc: `Bash(python3 -m unittest *)` per claude, `python3 -m unittest` e `test_cmd` per agy | devloop/config.toml:420-429, :465-472; devloop/config.py:29-32 |
| Check obbligatori | Letti dai ruleset o dalla protezione del branch su GitHub; i check del livello 0 per nome e app | devloop/config.toml:226-234, :153-157 |

### 1.5 Confine e `core`

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Confine | devloop importa solo la stdlib, sé stesso e `core`; `core` importa solo `math`, `re` e sé stesso, senza I/O | tests/test_devloop_boundary.py:1-18, :41-50; docs/architecture.md:144-165 |
| Cosa vede il confine | Solo gli `import`: i comandi che devloop esegue nel worktree, come `test_cmd`, eseguono il codice del repository fuori dal confine | docs/architecture.md:159-162 |
| Pacchetti estratti | `devloop/` e `core/`; nessun file del motore | devloop/pin.py:51, :384-388 |

Il confine rende devloop **già separabile** dal motore: l'installazione non contiene codice `ssdlc_*`. Restano legati a ssdlc solo dati (configurazione, prompt, valori di default) e convenzioni (test, log della CI).

### 1.6 Identità

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| App | Una per agent, una per il dispatcher, una per i merge; le app di ruolo dell'ADR 0005 (`implementer`, `supervisor`) sono in arrivo | devloop/config.toml:474-501; ADR 0005, D6 |
| Token | Il token d'installazione vale **per il solo repository** di `repo`; l'installazione si cerca con `GET /repos/<repo>/installation` | devloop/apps.py:157-186, :224-229 |
| Chiavi | In `key_dir` (o `$DEVLOOP_APP_KEY_DIR`), per slug, mode 600 | devloop/apps.py:113-123; devloop/config.toml:474-478 |
| `scripts/agent-token.py` | Legge `devloop/config.toml` **del checkout in cui si trova** e ne usa `repo`; l'esempio cita `repos/brushtyler/ssdlc` | scripts/agent-token.py:9-10, :28-33, :71-77 |
| Manifest | `ops/github/apps/<ruolo>.json` con il segnaposto `@REPO@`; il nome è `<APP_PREFIX><ruolo>`; tutte le app sono private (`"public": false`) | ops/github/apps/claude-agent.json:4-10; ops/github/create-apps.sh:58-63; ops/params.env.example:12-14 |
| Separazione dagli agent | Gli agent non vedono le credenziali di `gh` né le chiavi tramite l'ambiente, ma girano con lo stesso utente: "Real isolation needs a dedicated system user or a container" | devloop/agents.py:51-67 |

### 1.7 Pacchetto e host

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Installazione | Script parametrici per un host: utente dedicato, clone del repository gestito, `state_dir`, overlay d'esempio, unità systemd. CLI, login, chiavi e primo pin restano manuali | ops/host/setup-devloop.sh:2-5, :54-56, :110-114, :135 |
| Servizio | Un'unità `devloop.service` con `WorkingDirectory` sul clone ed `ExecStart` su `scripts/devloop-pinned run` | ops/host/devloop.service:11-21 |
| Supervisore | Utente separato, con sudoers minimale: comandi di sola lettura del launcher, `pin *`, e start, stop, restart e status del servizio | ops/host/sudoers-supervisor:5-23 |
| Parametri | `REPO` è "il repository gestito, che contiene anche devloop" | ops/params.env.example:5-6 |
| Sandbox di codex | `codex exec -s workspace-write`; in review anche senza rete e senza radici scrivibili in più | devloop/agents.py:400-412 |

### 1.8 Più progetti sullo stesso host

Oggi non è previsto. `state_dir` è dentro il clone (sezione 1.1), il launcher e gli script ne assumono il percorso fisso (scripts/devloop-pinned:24; ops/params.env.example:25-28), l'unità ha un solo nome (ops/params.env.example:43-44) e il lock del dispatcher è `<state_dir>/dispatcher.lock` (devloop/__main__.py:490), cioè **per `state_dir`**, non per repository. Oggi le due cose coincidono solo perché `state_dir` è dentro il clone: due clone dello stesso repository, o due `state_dir` diversi, non si escludono. `tick --dry-run` non prende il lock (devloop/__main__.py:481-489). I login delle CLI stanno nella home dell'utente `devloop`, condivisa (ops/host/setup-devloop.sh:82, :110-114).

## 2. Lacune

| # | Lacuna | Dove | Effetto |
|---|---|---|---|
| G1 | Sorgente dell'installazione e repository gestito coincidono | sezione 1.1 | devloop non si può puntare su un altro repository senza copiarvi `devloop/` |
| G2 | Configurazione unica, con dati di ssdlc anche nel codice | sezione 1.2 | Un altro progetto deve riscrivere l'overlay quasi per intero, e alcuni valori (livello 0, liste sensibili, pin automatico) restano quelli di ssdlc |
| G3 | Prompt con istruzioni e comandi di ssdlc scritti nel testo | sezione 1.3 | Gli agent di un altro progetto cercano un README e una struttura dei test che non esistono |
| G4 | Test locali e lettura della CI legati a `unittest`, `tests/` e `scripts/run-tests.py` | sezione 1.4 | `local_tests = "changed"` e `ci_rerun_unrelated` non funzionano altrove |
| G5 | Confine non dichiarato fra ciò che resta in ssdlc e ciò che è installazione | sezione 1.5 | Non è scritto quali file sono del prodotto installabile e quali del progetto ssdlc |
| G6 | Identità e script legati al repository di ssdlc | sezione 1.6 | Per ogni progetto servono decisioni su app, chiavi e `scripts/agent-token.py` |
| G7 | Pacchetto solo come installazione sull'host del repository gestito | sezione 1.7 | Avviare devloop su un progetto nuovo richiede la procedura completa di ops |
| G8 | Un progetto per host | sezione 1.8 | Due progetti sullo stesso host si contendono percorsi, servizio e login. Con `state_dir` separati, due servizi sullo stesso repository non si escludono |

## 3. Proposte

Ogni area ha alternative e una raccomandazione. Le raccomandazioni sono proposte: diventano decisioni con l'approvazione di questo ADR, salvo le parti elencate nella [sezione 6](#6-decisioni-riservate-alloperatore), che restano dell'operatore.

### 3.1 Sorgente dell'installazione e repository gestito

**Alternativa A: due clone, sorgente e gestito.** Il pin lavora su un clone della **sorgente** (ssdlc), con le stesse verifiche di oggi: commit raggiungibile da un ref di `origin`, copia in sola lettura, smoke test, rollback. Worktree, fetch, issue e PR lavorano sul clone del **repository gestito**. Per ssdlc i due clone possono coincidere, come oggi.

- Pro: riusa per intero `devloop/pin.py` e le sue garanzie; nessun nuovo formato di distribuzione; il self-hosting di ssdlc resta un caso particolare (sorgente = gestito).
- Contro: un clone in più per host; la sorgente va aggiornata con un fetch di ssdlc.

**Alternativa B: release di ssdlc.** L'installazione viene da un archivio di una release (tag), scaricato e verificato con un digest pubblicato.

- Pro: non serve un clone di ssdlc.
- Contro: un formato e una verifica nuovi, da costruire accanto al pin; il digest pubblicato sulla stessa pagina dell'archivio non prova la provenienza; il rollback va riscritto.

**Alternativa C: solo l'immagine del container.** L'installazione è l'immagine (sezione 3.7).

- Pro: un solo artefatto.
- Contro: lega la separazione al container e lascia senza soluzione l'installazione sull'host, che resta quella di ssdlc e del supervisore.

**Raccomandazione: A, con i tag di ssdlc come ref da fissare per gli altri progetti.**

- La configurazione dell'host distingue `source` (clone, o URL da clonare, di ssdlc) e il repository gestito (clone e `repo`). Senza `source`, la sorgente è il repository gestito: ssdlc non cambia comportamento.
- `main_checkout()` (devloop/__main__.py:35-39) resta la radice del **gestito**. Il pin usa la radice della **sorgente**.
- Il pin automatico dopo un merge (devloop/config.toml:184-193) vale solo quando sorgente e gestito coincidono. Un merge in un altro progetto non tocca mai l'installazione.
- Per gli altri progetti si fissa un **tag** di ssdlc, non `main`: l'aggiornamento è un atto esplicito (`devloop pin <tag>`), con lo smoke test di oggi.
- `state_dir` diventa un percorso dell'host, assoluto o relativo al gestito, letto anche dal launcher (scripts/devloop-pinned:24), che oggi lo assume fisso.
- Se un tag debba essere **firmato** e verificato prima del pin è una scelta dell'operatore (D1).

### 3.2 File di progetto e precedenza

Oggi un solo file mescola tre tipi di chiavi. La proposta le separa per **chi le possiede**:

| Tipo | Chiavi (esempi) | Dove |
|---|---|---|
| Installazione: default generali | limiti e timeout, agent e modelli, forma del budget, pin, gc, run, report, ci | `devloop/config.toml` dell'installazione, ripulito dei valori di ssdlc |
| Progetto: fatti del repository e restrizioni aggiuntive | Fatti: `test_cmd`, selezione e formato dei test (sezione 3.4), `[docs.map]`, `language`, `priority_labels`, istruzioni per i prompt (sezione 3.3). Restrizioni, solo in aggiunta a quelle dell'installazione e dell'overlay: `sensitive_paths`, `sensitive_path_lists`, `protected_paths`, `operator_only_paths`, `blocked_labels`, check obbligatori del livello 0 | `.devloop/project.toml` nel repository gestito |
| Host e operatore | `repo`, `base_branch`, `state_dir`, `source`, `[apps]` e `key_dir`, `trusted_users`, modalità di merge, ruoli, livelli di review, allowlist del livello 0, permessi degli agent, valori del budget, attivazioni (preflight, conclave, deleghe) | overlay locale `<state_dir>/config.local.toml` |

**Precedenza:** default dell'installazione < file di progetto < overlay locale. L'overlay resta l'ultima parola dell'operatore, come oggi (devloop/config.py:1185-1198).

**Da dove si legge il file di progetto.** Dal commit di `origin/<base_branch>` letto all'inizio del tick, **mai** dal branch di una PR né dal worktree di un agent. È la stessa regola della policy di oggi (devloop/config.toml:111-116). Il suo hash entra nell'hash della policy e quindi nelle ricevute: una modifica annulla le approvazioni delle PR aperte, come oggi un cambio della configurazione. Per ssdlc, dove sorgente e gestito coincidono, si propone di leggerlo dal commit fissato (vedi "Migrazione di ssdlc", sotto).

#### Confine di fiducia: il file di progetto è input non fidato

Leggere da `origin/<base_branch>` esclude i branch delle PR, ma **non** chi scrive su `main` fuori da devloop. In un repository di terzi chiunque abbia la scrittura su `main` può modificare il file senza passare da una PR di devloop: collaboratori, altre automazioni, un push diretto. Lo fa quindi senza reviewer, senza la regola di P5-0 e senza `operator_only_paths`, che valgono solo per le PR che devloop rivede e mergia. Anche in ssdlc chi ha la scrittura su `main` lo può fare, ma lì è l'operatore.

Il file di progetto si tratta quindi come **input non fidato**, in ogni repository, ssdlc compreso. **Requisiti** (issue R2):

1. **Solo fatti e restrizioni.** Il file può contenere solo le chiavi della riga "Progetto" della tabella, con uno schema chiuso: una chiave sconosciuta o non ammessa è un errore, non si ignora. Le chiavi di fiducia (`trusted_users`, merge, ruoli, app, livelli di review, allowlist del livello 0, permessi degli agent, budget, attivazioni) sono rifiutate.
2. **Mai allentamenti.** Il **pavimento** è la configurazione dell'installazione con l'overlay. Il file di progetto può solo aggiungere sopra il pavimento: percorsi sensibili, protetti e riservati, label bloccanti, check obbligatori. Non può toglierne né indebolire un valore del pavimento. Un valore che allenta, per esempio `local_tests = "none"` quando il pavimento non lo è, è un errore.
3. **Rifiuto all'avvio e a ogni tick.** devloop **rifiuta all'avvio** un file di progetto che allenti l'installazione o l'overlay, o che non rispetti lo schema, con un messaggio che nomina la chiave. Se un file non valido compare su `origin/<base_branch>` durante `run`, il tick si ferma senza pianificare né agire e lo segnala come un pin fallito. Non applica il file, e non prosegue in silenzio con quello precedente.
4. **Le restrizioni del progetto non sono garantite.** Una restrizione scritta solo nel file di progetto può sparire con una modifica di `main`: il pavimento non scende mai, ma ciò che sta sopra sì. Una restrizione che l'operatore vuole garantita va nell'overlay. Per ssdlc, quindi, `devloop/`, `core/` e `schemas/` sensibili vanno nell'overlay, non solo nel file di progetto.
5. **I fatti eseguibili non allargano il confine.** `test_cmd` viene dal repository, ma esegue codice del repository nel suo worktree, cosa che devloop fa già fuori dal confine (docs/architecture.md:159-162). Un `test_cmd` cambiato su `main` vale quanto uno `scripts/run-tests.py` cambiato su `main`. I **permessi** degli agent invece non vengono mai dal file di progetto (sezione 3.4).
6. **Traccia.** Ogni cambio dell'hash del file di progetto lascia un record nel registro del dispatcher, con il commit da cui è stato letto, come per l'overlay (sezione 3.9).

#### Chi lo può modificare

Il file decide percorsi sensibili e riservati, check obbligatori e comandi che devloop esegue: è **policy di fiducia del progetto**. Alternative:

- (a) **Tutte le chiavi di progetto, comprese quelle di fiducia**, protette solo dalla regola dei reviewer di P5-0 (#399; ADR 0005, regola 2 della sezione 3.1), come `devloop/config.toml` oggi.
- (b) **Solo fatti e restrizioni, con la regola dei reviewer.** Le chiavi di fiducia stanno solo nell'overlay, e il file rispetta i requisiti del confine di fiducia. Le PR di devloop che toccano `.devloop/` ricadono nella regola dei reviewer di P5-0, come oggi `devloop/config.toml`. `.devloop/` è sensibile (livello 2) per default dell'installazione, ma **non** riservato.
- (b') **Come (b), più `.devloop/` riservato all'operatore per codice** (`operator_only_paths`). Il file non può togliere la propria protezione.
- (c) **Nessun file nel repository.** Tutto nell'overlay dell'host, per progetto.

**Coerenza con l'ADR 0005.** La decisione dell'operatore del 2026-11-18 alle 21:50 (ADR 0005, regola 2 della sezione 3.1 e sezione 4) ha tolto il file riservato per la policy di fiducia. Non si fa `operator_policy.toml`, `devloop/config.toml` non è in `operator_only_paths`, e resta solo la regola dei reviewer. Il motivo: in ssdlc il rischio reale è una regressione accidentale, o un agent che si sblocca allentando un controllo, e le PR passano già da due reviewer di fornitori diversi. (b') introdurrebbe un file riservato, in tensione con quella decisione. Bisogna quindi chiedersi se un repository gestito, specie di terzi, richieda una protezione più forte:

- **Il rischio in più esiste.** In un repository di terzi altri scrivono su `main` senza passare da devloop: un allentamento non nasce solo da un agent.
- **(b') non lo copre.** `operator_only_paths` e la regola dei reviewer agiscono solo sulle PR che devloop rivede e mergia, e un push diretto su `main` le scavalca entrambe. Contro quel rischio protegge solo la **validazione al caricamento** (requisiti 1-3), qualunque sia la strada da cui il file arriva su `main`.
- **Per le PR di devloop basta la regola dei reviewer**, come per `devloop/config.toml`. Con i requisiti 1 e 2 una PR può cambiare solo fatti e restrizioni sopra il pavimento, mai togliere una garanzia dell'operatore. Se una PR sfugge ai reviewer, il caso peggiore è un fatto sbagliato (una mappa dei documenti, un `test_cmd`) o una restrizione in meno sopra il pavimento: lo stesso rischio residuo che l'ADR 0005 ha accettato.

Valutazione:

- (a) estende la scelta dell'ADR 0005 a un contesto dove non regge. In un altro progetto chi scrive su `main` può cambiare insieme il codice e le regole che lo giudicano, comprese `trusted_users` e il merge.
- (b) è coerente con l'ADR 0005: nessun file riservato, regola dei reviewer. La protezione forte sta dove serve, nel codice che valida il file, non nei permessi di merge.
- (b') aggiunge un file riservato senza coprire il rischio che lo motiverebbe, e manderebbe all'operatore ogni modifica di un fatto del progetto.
- (c) è la più semplice, ma i fatti del progetto (mappa dei documenti, test) si allontanano dal repository e nessuno li rivede in una PR.

**Raccomandazione: (b).** Per `.devloop/` vale la regola dei reviewer di P5-0, e il file si valida come input non fidato, senza file riservato. Sono decisioni dell'operatore (D2):

- la precedenza;
- le chiavi ammesse e la regola "solo fatti e restrizioni";
- la protezione di `.devloop/`: (b), regola dei reviewer, oppure (b'), riservato per codice;
- il nome del file.

#### Migrazione di ssdlc

La migrazione (issue R3) sposta in `.devloop/project.toml` di ssdlc le chiavi di progetto oggi in `devloop/config.toml`, e i valori di ssdlc oggi scritti nel codice (devloop/config.py:19, :120; devloop/policy.py:48-52, :75; devloop/localtests.py:24-25). Le chiavi di fiducia di ssdlc (`repo`, `trusted_users`, `[apps]`, merge) vanno nell'overlay di ssdlc, con la tracciabilità della sezione 3.9.

**Il commit del pin e quello di base non coincidono.** Oggi tutta la configurazione, comprese le liste di `sensitive_path_lists` (devloop/config.py:16-18), si legge dalla copia fissata, cioè dal **commit del pin**. Il pin automatico è spento per default (`[pin] auto = false`, devloop/config.toml:193). La copia fissata resta quindi al commit dell'ultimo `devloop pin`, e rimane indietro rispetto a `main` finché l'operatore non fissa di nuovo. Il file di progetto letto da `origin/<base_branch>` viene invece dal **commit di base**, che avanza a ogni merge. I due commit coincidono solo subito dopo un pin di `origin/<base_branch>`, fino al merge successivo.

**Equivalenza della migrazione, con precisione.**

- *Equivalenza statica.* Per uno stesso commit C, la configurazione effettiva del codice nuovo fissato a C, con il file di progetto di C e l'overlay migrato, è identica a quella del codice di oggi fissato a C con l'overlay di oggi. È la prova di R3: un test calcola le due configurazioni effettive allo stesso commit e le confronta chiave per chiave, con lo stesso hash della policy.
- *Differenza di tempi.* Oggi una modifica delle chiavi di progetto mergiata su `main` ha effetto solo al pin successivo, che con `auto = false` è un atto dell'operatore. Se il file si legge dal commit di base, ha effetto al tick successivo, senza pin. Per ssdlc sarebbe un cambio di comportamento, non un'equivalenza.

Alternative per ssdlc, cioè quando sorgente e gestito coincidono:

- (i) **file di progetto dal commit fissato.** I tempi restano quelli di oggi: una modifica ha effetto solo con un pin. Il pin automatico, quando è attivo, scatta anche per un merge che tocca `.devloop/`, come oggi per `devloop/` e `core/` (devloop/config.toml:184-193);
- (ii) **file di progetto dal commit di base,** come per gli altri progetti. La regola è una sola, ma i fatti e le restrizioni del progetto cambiano senza pin.

**Raccomandazione: (i) per ssdlc,** e il commit di base per gli altri progetti, dove il repository gestito non è mai fissato. Con (i) la migrazione è equivalente anche nei tempi, e il pin resta il punto in cui l'operatore adotta una configurazione nuova. Le liste di `sensitive_path_lists` si leggono dallo stesso commit del file di progetto: quello fissato per ssdlc, quello di base per gli altri. La scelta è dell'operatore (D2).

### 3.3 Prompt come modelli

**Alternativa A: dati del progetto nei modelli dell'installazione.** I prompt restano dell'installazione. Il testo proprio di ssdlc diventa segnaposto riempiti dal file di progetto:

- le istruzioni da leggere (`instructions`, per esempio `["AGENTS.md", "README.md#developing-ssdlc"]`);
- il comando per un solo modulo di test (sezione 3.4);
- le regole del progetto sugli ADR, che il preflight cita (devloop/prompts/preflight.md:25-38).

Un segnaposto vuoto toglie la frase, non lascia un testo spezzato.

**Alternativa B: prompt del progetto.** Il repository può fornire prompt interi in `.devloop/prompts/`.

**Alternativa C: un'aggiunta del progetto in coda ai prompt.** Una sezione "Project notes" libera.

**Raccomandazione: A.**

- Le regole di sicurezza dei prompt sono policy di fiducia (ADR 0005, regola 2 della sezione 3.1: permessi, verdetti, cosa l'agent non fa). Con B un repository le potrebbe riscrivere, con C le potrebbe contraddire in coda.
- Con A il progetto fornisce solo dati, che passano dal file di progetto e dalle sue protezioni (sezione 3.2).
- Prova di equivalenza: con il file di progetto di ssdlc, i prompt resi restano **identici byte per byte** a quelli di oggi.

### 3.4 Test locali e CI

**Proposta.** Il file di progetto dichiara:

- `test_cmd`, il comando completo, obbligatorio;
- `local_tests`, come oggi (`full`, `changed`, `none`);
- una **selezione facoltativa** per `changed`:
  - (i) una strategia predefinita dell'installazione, con nome, per esempio `python-unittest-modules`, che è l'algoritmo di oggi (devloop/localtests.py:76-114) con le sue regole di nome (`ssdlc_` → `test_`, `devloop/` → `test_devloop*`) diventate parametri;
  - (ii) regole dichiarative "percorso → comando", senza codice del progetto nel dispatcher;
- un **formato dei log della CI** facoltativo. Senza formato, `ci_rerun_unrelated` è spento e il riepilogo della CI non c'è.

Senza selezione, `changed` si comporta come `full`: un progetto senza strategia non perde test, perde solo tempo.

Alternative:

- (a) solo `test_cmd`, sempre `full`;
- (b) `test_cmd` più la strategia predefinita (i);
- (c) (b) più le regole dichiarative (ii).

**Raccomandazione: (b) subito, (c) quando un secondo progetto lo chiede.**

- La strategia di oggi resta il caso di ssdlc, con gli stessi moduli selezionati: è la prova di equivalenza.
- I **comandi ammessi agli agent** (devloop/config.toml:420-429, :465-472) oggi seguono `test_cmd` e la forma dei test di ssdlc. Derivarli dal file di progetto vorrebbe dire che il repository decide che cosa gli agent possono eseguire, e i permessi degli agent sono policy di fiducia (ADR 0005). Con la raccomandazione (b) della sezione 3.2 i permessi restano nell'installazione e nell'overlay. Il file di progetto può proporre comandi, ma non li ammette: li ammette l'overlay dell'operatore (D2).

### 3.5 Confine e `core`

Restano in ssdlc, come **progetto**:

- il motore (`orchestrator.py`, `ssdlc_*`) e i suoi test;
- la baseline del replay gate e i file che fissa;
- `.devloop/project.toml` di ssdlc;
- la CI di ssdlc;
- la documentazione del motore.

Diventano **installazione**, estratti dal pin come oggi:

- `devloop/` (codice, prompt, schemi e default);
- `core/`;
- nient'altro: l'installazione è esattamente ciò che il pin estrae (devloop/pin.py:51, :384-388), salvo le liste di `sensitive_path_lists`, che diventano dati del repository gestito (sezione 3.2).

Oggi due strumenti stanno fuori da quel confine, in `scripts/` del clone:

- `scripts/devloop-pinned` è un **avviatore**: trova la copia fissata in `<clone>/.ssdlc/devloop/current` ed esegue devloop da lì, senza importare nulla dal clone (scripts/devloop-pinned:1-16, :28-42). Non può stare nella copia fissata, perché serve proprio a trovarla;
- `scripts/agent-token.py` importa il pacchetto `devloop` che trova accanto a sé, cioè quello del checkout, non quello fissato (scripts/agent-token.py:28-33).

Proposta coerente con il confine:

- **`agent-token` diventa un sottocomando dell'installazione** (`devloop token <actor> -- <comando>`), dentro `devloop/`, con le stesse regole di oggi su attori riservati e permessi. Si esegue attraverso l'avviatore, quindi dalla copia fissata e con la configurazione del progetto indicato (sezione 3.6). `scripts/agent-token.py` resta in ssdlc come involucro sottile che lo richiama, per non cambiare `AGENTS.md` e le abitudini delle sessioni (issue R6).
- **L'avviatore non è installazione: è il bootstrap dell'host o dell'immagine.** Non sta nella copia fissata e non dipende dal repository gestito. Riceve lo `state_dir` come parametro invece di ricavarlo dalla propria posizione (scripts/devloop-pinned:23-24), e continua a non importare nulla fuori dalla copia fissata.
  - Sull'host, gli script di `ops/host/` lo installano in un percorso dell'host, per esempio `/usr/local/lib/devloop/devloop-pinned`, copiandolo dalla sorgente al tag fissato. L'unità del servizio lo chiama con lo `state_dir` del progetto (issue R1 e R8).
  - Nel container è l'entrypoint dell'immagine (sezione 3.7).
  - Per ssdlc, dove sorgente e gestito coincidono, `scripts/devloop-pinned` resta dov'è e funziona come oggi senza parametro.

Alternative:

- (a) **Stesso repository, con la separazione dichiarata.** L'installazione è il sottoinsieme che il pin estrae. Un test verifica che l'installazione non legga file del repository fuori da `devloop/` e `core/`, salvo quelli del progetto gestito.
- (b) **Un repository separato per devloop.** È l'opzione B dell'ADR 0001, scartata dall'operatore.

**Raccomandazione: (a).** Il test di confine (tests/test_devloop_boundary.py:41-50) resta com'è. Si aggiunge un controllo che nell'installazione non ci siano percorsi o valori di ssdlc, cioè i valori della sezione 1.2 una volta spostati nel file di progetto. La regola sul confine non cambia: nessun import di `ssdlc_*`, solo `core`.

### 3.6 Identità

**Alternativa A: un insieme di app per repository.** Per ogni progetto, le app dai manifest (ops/github/apps/) con un `APP_PREFIX` proprio (ops/params.env.example:12-14).

- Pro: una chiave compromessa tocca un solo repository; permessi e installazioni separati; obbligatoria per un repository di un altro proprietario, perché le app sono private (`"public": false`, ops/github/apps/claude-agent.json:10) e si installano solo sull'account che le possiede.
- Contro: sette app e sette chiavi per progetto, contando le app di ruolo dell'ADR 0005.

**Alternativa B: le stesse app installate su più repository.**

- Pro:
  - il token è già limitato al solo repository di `repo` (devloop/apps.py:169-186), quindi un turno non scrive altrove;
  - i marker restano verificati per login su oggetti del repository;
  - nessuna app nuova.
- Contro:
  - una chiave compromessa tocca tutti i repository;
  - l'app `merger`, che ha `contents: write`, la avrebbero tutti i repository;
  - solo per repository dello stesso proprietario.

**Alternativa C: mista.** Le app degli agent e del dispatcher condivise fra i repository dello stesso proprietario, e `merger` per repository.

**Raccomandazione: B per i repository dello stesso proprietario e dello stesso operatore, A per gli altri.** In entrambi i casi:

- `merger` si installa **solo** sui repository dove l'operatore attiva il merge automatico;
- il sottocomando `devloop token` della sezione 3.5, richiamato anche da `scripts/agent-token.py`, legge `repo` e le app dalla configurazione del progetto indicato (lo `state_dir`, con il suo overlay), non da `devloop/config.toml` accanto a sé (scripts/agent-token.py:28-33, :71-77);
- le chiavi restano in `key_dir`, per slug (devloop/apps.py:113-123). Con A, gli slug distinti per prefisso convivono nella stessa `key_dir` senza conflitti;
- i manifest di #402 (PR #403) si riusano senza modifiche: `@REPO@` e `APP_PREFIX` bastano a creare un insieme per progetto. Con B si aggiunge solo l'installazione sul nuovo repository.

### 3.7 Pacchetto

**Alternativa A: installazione sull'host** con `ops/host/`, generalizzata (sezioni 3.1 e 3.8).

**Alternativa B: container.** Un'immagine con:

- devloop a una **versione fissata** (un tag di ssdlc), in sola lettura;
- `gh`, `git` e Python a versioni fissate;
- le CLI claude, codex e agy a versioni fissate, senza aggiornamento automatico (`DISABLE_AUTOUPDATER=1`, scripts/devloop-pinned:40-41).

Volumi:

- lo **`state_dir`**: journal, worktree, log e clone del repository gestito, persistente;
- le **chiavi delle app**, in sola lettura, come segreti;
- le **directory di login delle CLI** (`~/.claude`, `~/.codex`, `~/.gemini`), persistenti e scrivibili, perché le CLI rinnovano i token;
- l'**overlay** dell'operatore, in sola lettura.

**Alternativa C: entrambe.** Il container per gli altri progetti, l'host per ssdlc e per il supervisore.

Punti aperti del container, da chiudere nell'issue che lo costruisce:

- **Sandbox di codex.** codex isola i comandi con landlock e seccomp (devloop/agents.py:400-412, `-s workspace-write`). Dentro un container il profilo seccomp di default del runtime e i permessi del container possono impedirne l'attivazione. Serve una **prova** prima di adottare il container. Se la sandbox non si attiva, le strade sono due: un profilo seccomp dedicato che ammette le chiamate necessarie, senza `--privileged`, oppure lasciare codex sull'host. **Mai** codex senza sandbox (`--dangerously-bypass-approvals-and-sandbox` o simili): toglierebbe un contenimento esistente, ed è una decisione dell'operatore che questo ADR non raccomanda.
- **Login interattivi.** Si fanno con un comando una tantum nel container (`docker run -it … claude`, poi `/login`), che scrive nel volume di login. Un login scaduto ferma i turni di quell'agent come oggi: devloop lo segnala, e l'operatore rifà il login.
- **Aggiornamento della versione.** Una nuova immagine con un nuovo tag. All'avvio, l'entrypoint esegue lo smoke test di oggi (`tick --dry-run` e un giro di `watch`, devloop/pin.py:8-12) e solo poi entra in `run`. Il rollback è il tag precedente. Nel container il pin automatico è spento: sorgente e gestito sono distinti (sezione 3.1).
- **Contenimento.** Il container separa devloop dall'host, ma **non** separa gli agent dal dispatcher: girano nello stesso container, con lo stesso utente, e potrebbero leggere le chiavi montate (devloop/agents.py:64-66). Non sostituisce il contenimento degli esecutori del passo 4 dell'ADR 0001 (vincolo 5). Va dichiarato nella guida.
- **Supervisore fuori dal container.** Resta sull'host, con il suo utente e la sua app:
  - legge i log da una vista in sola lettura del volume dello `state_dir`;
  - avvia e ferma il servizio con un sudoers minimale sull'unità che avvia il container, come oggi (ops/host/sudoers-supervisor:19-23);
  - **non** entra nel gruppo `docker`, che equivale a root;
  - i comandi di sola lettura di oggi (`status`, `watch`, `report`, ops/host/sudoers-supervisor:11-17) diventano `docker exec` limitati a quei sottocomandi, oppure un servizio di sola lettura. La scelta va fatta nell'issue.

**Raccomandazione: C.**

- Il container è il pacchetto per gli altri progetti, subordinato alla prova della sandbox di codex.
- L'host resta per ssdlc, che si sviluppa da sé con il pin automatico, e per il supervisore.
- Senza la prova, il pacchetto degli altri progetti è A, con l'installazione sull'host generalizzata.

### 3.8 Più progetti sullo stesso host

**Proposta:**

- uno `state_dir` per progetto, **fuori** dal clone (per esempio `/var/lib/devloop/<progetto>`) o dentro il clone del progetto. Contiene lock, journal, worktree, overlay e copie fissate;
- un servizio per progetto: un'unità template `devloop@<progetto>.service` sull'host, oppure un container per progetto;
- il lock del dispatcher di oggi, `<state_dir>/dispatcher.lock` (devloop/__main__.py:490), resta, ma è **per `state_dir`**, non per repository (sezione 1.8). Con installazioni e `state_dir` separati, due servizi configurati sullo stesso `repo` **non** si escluderebbero: due dispatcher agirebbero sulle stesse issue e PR, con lavoro doppio e marker in conflitto. L'esclusione per repository va quindi aggiunta (vedi sotto);
- la memoria dei modelli non disponibili (`model-unavailability.json`) e il budget restano per progetto. Le **quote** degli account delle CLI sono condivise se il login è condiviso.

Alternative per i login delle CLI e per l'utente di sistema:

- (a) **un utente e un insieme di login per tutti i progetti.** È la più semplice, ma un agent di un progetto può leggere i worktree e lo stato degli altri (devloop/agents.py:64-66);
- (b) **un utente per progetto, con login propri.** Isola i file fra progetti, al costo di un login per CLI e per progetto;
- (c) **un container per progetto**, con i volumi propri: equivale a (b).

**Raccomandazione: (b) sull'host e (c) con il container.** Un repository di terzi non deve poter leggere lo stato di ssdlc.

**Esclusione per repository.** Alternative:

- (L1) **lock per repository sull'host.** Oltre al lock dello `state_dir`, il dispatcher prende, senza attendere, un lock con nome derivato dal `repo` normalizzato (proprietario e nome in minuscolo), in una directory comune dell'host, per esempio `/run/devloop/repos/<proprietario>--<nome>.lock`. Se il lock è occupato, devloop si ferma all'avvio con un messaggio che nomina il repository, come oggi con il lock dello `state_dir` (devloop/__main__.py:59-68).
  - Pro: stesso meccanismo di oggi (`flock`), nessuna dipendenza dal tracker, rilasciato dal kernel se il processo muore.
  - Contro: vale solo **sullo stesso host**. Con utenti per progetto la directory comune deve essere scrivibile da tutti gli utenti di devloop, per esempio con un gruppo dedicato, senza dare accesso agli `state_dir`. Con i container la directory va montata in tutti i container di quell'host.
- (L2) **lease sul tracker.** Un oggetto del repository gestito che dice quale dispatcher lo sta servendo, con scadenza e rinnovo a ogni tick, scritto dall'app del dispatcher.
  - Pro: vale anche fra host diversi.
  - Contro: serve un'operazione atomica sul tracker (per esempio la creazione di un ref, che fallisce se il ref esiste), con permessi in più per l'app del dispatcher; scadenze e orologi; un dispatcher che perde il lease deve fermare le azioni in corso. È il **fencing** degli esecutori del passo 4 dell'ADR 0001, di cui sarebbe un anticipo parziale.
- (L3) **solo procedura.** La guida dice "un servizio per repository" e lo controlla `check-host.sh`. Non è un'esclusione.

**Raccomandazione: (L1) in R8, come requisito**, con la regola "un repository, un host" scritta nella guida rapida. (L2) si valuta con il fencing del passo 4 dell'ADR 0001, se servirà gestire lo stesso repository da più host. `tick --dry-run` continua a non prendere nessun lock (devloop/__main__.py:481-489). La scelta è dell'operatore (D6).

### 3.9 Rischi

Rischi introdotti o resi più visibili da queste proposte, con la mitigazione proposta:

| # | Rischio | Mitigazione proposta |
|---|---|---|
| K1 | **Le chiavi di fiducia perdono storia e revisione.** Con la sezione 3.2, `repo`, `trusted_users`, `[apps]`, merge e permessi degli agent passano dal `devloop/config.toml` versionato, rivisto in PR e coperto dalla regola dei reviewer di P5-0, all'overlay non versionato. Una modifica dell'overlay non lascia traccia di chi l'ha fatta, quando e da quale valore | (T1) A ogni avvio, e a ogni rilettura della configurazione (devloop/config.py:1232-1255), il dispatcher scrive nel suo registro (il journal) un record con l'**hash dell'overlay**, l'hash del file di progetto con il suo commit, l'hash della configurazione effettiva e l'elenco delle chiavi di fiducia cambiate rispetto al record precedente, con il valore vecchio e quello nuovo. Il riepilogo di #316 mostra i cambi. (T2) In aggiunta, facoltativo: un **repository di configurazione dell'operatore**, privato, da cui l'overlay si copia sull'host. Dà storia e revisione vere, ma è infrastruttura in più e sta fuori da devloop. Raccomandazione: T1 sempre, nell'issue R2; T2 a scelta dell'operatore (D6) |
| K2 | **Il file di progetto cambia fuori da devloop** (push diretto su `main` di un repository di terzi) | Validazione come input non fidato e rifiuto all'avvio e al tick (sezione 3.2, requisiti 1-3); traccia del suo hash (requisito 6, T1); hash nella policy, quindi approvazioni annullate |
| K3 | **Restrizioni del progetto che spariscono** con una modifica di `main` | Il pavimento sta nell'installazione e nell'overlay; ciò che conta va nell'overlay (sezione 3.2, requisito 4) |
| K4 | **Due servizi sullo stesso repository** | Lock per repository sull'host (sezione 3.8, L1); un repository per host nella guida |
| K5 | **Lettura dello stato fra progetti** con un utente condiviso | Utente o container per progetto (sezione 3.8) |
| K6 | **Chiavi condivise fra repository** con le app condivise | `merger` solo dove serve; app per repository per i terzi (sezione 3.6) |
| K7 | **Il container non separa gli agent dal dispatcher** | Dichiararlo nella guida; il contenimento resta il passo 4 dell'ADR 0001 (sezione 3.7) |

## 4. Collocazione nel piano dell'ADR 0001

- I passi 3, 4 e 5 dell'[ADR 0001](0001-prodotto-unico-fasi-su-tracker.md#piano-ordine-rivisto-dal-conclave) sono le fasi sul tracker, il contenimento e il fencing degli esecutori, e il backend filesystem.
- Questo lavoro **non dipende** da quei passi, e quei passi non dipendono da lui. È invece un **prerequisito della parità** che condiziona il passo 6: il motore locale lavora su un progetto separato, e devloop deve saperlo fare prima della convergenza.
- Interazioni:
  - **passo 3 (fasi):** le fasi aggiungeranno prompt e chiavi di configurazione. Se i prompt sono già modelli con i dati del progetto (sezione 3.3), le fasi nascono nella forma giusta, invece di aggiungere altro testo proprio di ssdlc da migrare dopo;
  - **passo 4 (contenimento):** il container della sezione 3.7 non lo sostituisce. Il contenimento degli esecutori, quando arriverà, dovrà funzionare anche nel container;
  - **passo 5 (backend filesystem):** il file di progetto e lo `state_dir` per progetto valgono anche per un backend locale.
- Interazioni con l'ADR 0005:
  - il file di progetto è policy di fiducia: la regola dei reviewer (P5-0, #399) deve essere su `main` prima che nasca;
  - le app di ruolo (P5-3, P5-4) cambiano l'insieme di app da creare per progetto: è meglio fare l'issue sulle identità (R6) dopo di loro.

**Quando.** Alternative:

- (a) **subito dopo l'approvazione e dopo P5-0, prima del passo 3**;
- (b) dopo i passi 3–5, subito prima del passo 6;
- (c) intercalato, un'issue per volta, quando la coda lo permette.

**Raccomandazione: (a)** per R1–R5, che rendono i prompt e la configurazione indipendenti da ssdlc prima che le fasi li moltiplichino. Le altre issue (R6–R9) si fanno in (c), dopo le loro dipendenze. La scelta è dell'operatore (D5).

## 5. Piano delle issue

Le issue si aprono dopo l'approvazione di questo ADR, una per area, e citano la sezione di questo ADR su `main` che attuano, come chiede il [ciclo degli ADR](README.md#ciclo-di-un-adr). Il prefisso `R` (repository) le distingue dai passi del piano dell'ADR 0001.

- Le issue che toccano `devloop/` o `tests/test_devloop_boundary.py` richiedono il livello 2 di review: `devloop/` è in `sensitive_paths` (devloop/config.toml:123).
- Quasi tutte cambiano la policy di fiducia o il codice che la applica (file di progetto, permessi degli agent, identità): con la regola dei reviewer di #399 vanno all'operatore.
- Quelle che toccano `AGENTS.md`, `CLAUDE.md` o `docs/adr/` le mergia l'operatore (`operator_only_paths`, devloop/config.toml:178).
- Ogni issue aggiorna i documenti che la mappa `[docs.map]` lega ai file che cambia.
- Nessuna issue cambia il comportamento di devloop su ssdlc. La prova è l'equivalenza allo stesso commit fissato: stessa configurazione effettiva, stessi prompt resi, stessi moduli di test selezionati. Con la raccomandazione (i) della sezione 3.2, anche i tempi in cui una modifica della configurazione ha effetto restano quelli di oggi.
- Nessuna issue tocca il motore locale né i file fissati dalla baseline del replay gate.

| Issue | Titolo proposto | Perimetro | Dipendenze | Review |
|---|---|---|---|---|
| R1 | feat(devloop): sorgente dell'installazione separata dal repository gestito | `source` nella configurazione dell'host; il pin usa la radice della sorgente, worktree e GitHub quella del gestito; pin automatico solo con sorgente = gestito; `state_dir` dell'host passato all'avviatore come parametro e letto dagli script di ops; avviatore installato sull'host dalla sorgente al tag fissato, fuori dalla copia fissata e dal repository gestito (sezione 3.5); senza `source` il comportamento di oggi. Test con due repository git locali | Approvazione dell'ADR e D1 | Livello 2 |
| R2 | feat(devloop): file di progetto `.devloop/project.toml` | Schema chiuso e lettura dal commit di `origin/<base_branch>`, mai da un branch di PR (dal commit fissato per ssdlc, secondo D2); precedenza installazione < progetto < overlay; il file come input non fidato, con i requisiti 1-6 della sezione 3.2: solo fatti e restrizioni sopra il pavimento, rifiuto all'avvio di un file che allenti l'installazione o l'overlay, tick fermato se un file non valido compare su `main`; `.devloop/` sensibile per default, e riservato solo se D2 sceglie (b'); hash nella policy e nelle ricevute; record nel registro con gli hash dell'overlay, del file di progetto e della configurazione effettiva, e le chiavi di fiducia cambiate (sezione 3.9, T1). Senza file, la configurazione di oggi | R1, P5-0 dell'ADR 0005 e D2 | Livello 2, all'operatore (policy di fiducia) |
| R3 | refactor: configurazione di ssdlc nel suo file di progetto | Chiavi di progetto di ssdlc da `devloop/config.toml` a `.devloop/project.toml` di ssdlc; valori di ssdlc nel codice (devloop/config.py:19, :120; devloop/policy.py:48-52, :75; devloop/localtests.py:24-25) nel file; chiavi di fiducia di ssdlc nel suo overlay, con le restrizioni da garantire (`devloop/`, `core/`, `schemas/` sensibili); `sensitive_path_lists` dal repository gestito, allo stesso commit del file di progetto. Test di equivalenza statica: allo stesso commit fissato, stessa configurazione effettiva e stesso hash della policy prima e dopo la migrazione (sezione 3.2). Un controllo che l'installazione non contenga più valori di ssdlc (sezione 3.5) | R2 | Livello 2, all'operatore |
| R4 | feat(devloop): prompt come modelli con i dati del progetto | Segnaposto per le istruzioni da leggere, il comando di un solo modulo e le regole sugli ADR; frasi tolte quando il dato manca; valori di ssdlc nel suo file di progetto. Test: prompt resi identici byte per byte per ssdlc | R2 (R3 per i valori di ssdlc) | Livello 2, all'operatore (i prompt sono policy) |
| R5 | feat(devloop): test e CI dichiarati dal progetto | `test_cmd`, `local_tests`, strategia di selezione con nome (quella di oggi parametrizzata) e formato dei log della CI, facoltativi; senza selezione, `changed` vale `full`; senza formato, `ci_rerun_unrelated` spento. Comandi ammessi agli agent secondo D2. Test: stessi moduli selezionati per ssdlc | R2 | Livello 2, all'operatore |
| R6 | feat(devloop): identità per progetto | Sottocomando `devloop token` nell'installazione, con la configurazione del progetto indicato, e `scripts/agent-token.py` come involucro che lo richiama (sezione 3.5); `Identities` e `devloop apps` sul `repo` del progetto; documentazione delle due modalità (insieme per repository o app condivise) secondo D3; `AGENTS.md` aggiornato. I manifest di `ops/github/apps/` non cambiano | R1, P5-3 e P5-4 dell'ADR 0005, D3 | Livello 2, merge dell'operatore |
| R7 | feat(ops): immagine container di devloop | Prima, la **prova** della sandbox di codex nel container, con il risultato registrato nell'issue: se non si attiva senza `--privileged` né sandbox spenta, l'issue si ferma e torna all'operatore. Poi: immagine con versioni fissate, entrypoint con smoke test, volumi e segreti della sezione 3.7, login interattivi, aggiornamento e rollback per tag; supervisore fuori dal container con accesso in sola lettura | R1–R6 e D4 | Livello 2 |
| R8 | feat(ops): più progetti sullo stesso host | Unità template `devloop@.service`, parametri per progetto, `state_dir` e utente per progetto secondo D6, `check-host.sh` per progetto, sudoers del supervisore per unità. **Requisito: esclusione per repository** secondo D6, con la raccomandazione L1 della sezione 3.8: lock per `repo` normalizzato in una directory comune dell'host, preso all'avvio senza attendere, arresto con un messaggio se è occupato; directory comune accessibile a tutti gli utenti di devloop dell'host senza accesso agli `state_dir`, e montata in ogni container. Test: due dispatcher con `state_dir` diversi sullo stesso `repo` non partono insieme; su `repo` diversi sì | R1 e D6 | Livello 2: il lock tocca `devloop/` |
| R9 | docs: guida rapida per un nuovo progetto | Creare le app dai manifest (o installare quelle esistenti), scrivere `.devloop/project.toml`, scrivere l'overlay, avviare il container o il servizio, fare i login, verificare con `devloop apps` e `tick --dry-run`. Una prova su un repository di test | R1–R8 | Livello 1 |

**Ordine.**

- R1 parte subito dopo l'approvazione. R2 segue R1 e P5-0.
- R3, R4 e R5 seguono R2 e possono procedere in parallelo, salvo conflitti su `devloop/config.toml`. R4 e R5 usano i valori di ssdlc scritti da R3.
- R6 aspetta le app di ruolo dell'ADR 0005 (P5-3 e P5-4), per non toccare le identità due volte.
- R8 segue R1. R7 segue R1–R6, ed è fermato dalla prova della sandbox finché non passa.
- R9 è l'ultima.

**Esclusi:**

- un repository separato per devloop (ADR 0001, opzione B);
- prompt interi forniti dal progetto;
- contenimento degli esecutori e prova di terminazione (passo 4 dell'ADR 0001);
- backend diversi da GitHub (passo 5);
- codex senza sandbox;
- ogni cambio al motore locale, alla CI di ssdlc o alla baseline del replay gate.

## 6. Decisioni riservate all'operatore

Questo ADR le elenca, con le alternative e le raccomandazioni, ma **non le prende**. Restano valide tutte le decisioni riservate degli ADR 0001, 0002, 0004 e 0005.

- **D0. Approvazione di questo ADR,** con il merge della sua PR.
- **D1. Separazione e sorgente dell'installazione** ([sezione 3.1](#31-sorgente-dellinstallazione-e-repository-gestito)).
  - Alternative: A (due clone, con i tag di ssdlc), B (release con digest), C (solo l'immagine). La raccomandazione è A.
  - Se i tag fissati per gli altri progetti debbano essere firmati e verificati prima del pin. La raccomandazione è no per ora, perché il pin verifica già la raggiungibilità da `origin`, e sì prima di aprire devloop a operatori diversi.
- **D2. File di progetto e sua precedenza** ([sezione 3.2](#32-file-di-progetto-e-precedenza)).
  - Il nome (`.devloop/project.toml`) e la precedenza: installazione < progetto < overlay.
  - Quali chiavi può contenere: (a), (b), (b') o (c). La raccomandazione è (b): solo fatti del progetto e restrizioni sopra il pavimento di installazione e overlay, con le chiavi di fiducia nell'overlay.
  - Il file di progetto come **input non fidato**, con il rifiuto all'avvio di un file che allenti l'installazione o l'overlay e l'arresto del tick se un file non valido compare su `main` (requisiti 1-6 della sezione 3.2). La raccomandazione è sì, in ogni repository.
  - La protezione di `.devloop/`: (b), regola dei reviewer di P5-0, coerente con la decisione dell'ADR 0005 del 2026-11-18 alle 21:50, oppure (b'), riservato all'operatore per codice. La raccomandazione è (b): (b') non copre le scritture su `main` fuori da devloop, che sono il rischio in più dei repository di terzi, e quelle le copre la validazione.
  - Per ssdlc, da quale commit si legge il file di progetto: (i) dal commit fissato, con i tempi di oggi, oppure (ii) dal commit di base. La raccomandazione è (i).
  - Chi ammette i comandi degli agent derivati dai test del progetto ([sezione 3.4](#34-test-locali-e-ci)). La raccomandazione è l'overlay dell'operatore, mai il file di progetto da solo.
- **D3. App per repository** ([sezione 3.6](#36-identità)).
  - Alternative: A (un insieme per repository), B (app condivise), C (mista). La raccomandazione è B per i repository dello stesso proprietario e operatore, A per gli altri, e `merger` solo dove il merge automatico è attivo.
  - Creare e installare le app restano atti dell'operatore, perché sono credenziali.
- **D4. Pacchetto** ([sezione 3.7](#37-pacchetto)).
  - Alternative: A (host), B (container), C (entrambi). La raccomandazione è C, subordinata alla prova della sandbox di codex.
  - Se la prova fallisce: un profilo seccomp dedicato, oppure codex sull'host, oppure niente container. Codex senza sandbox **non** è raccomandato.
  - Come il supervisore legge lo stato del container: `docker exec` limitato o un servizio di sola lettura. Mai il gruppo `docker`.
- **D5. Collocazione nel piano** ([sezione 4](#4-collocazione-nel-piano-delladr-0001)).
  - Alternative: (a) prima del passo 3, (b) prima del passo 6, (c) intercalato. La raccomandazione è (a) per R1–R5 e (c) per le altre.
  - La parità con il motore locale per il passo 6 richiede, come minimo, R1–R5 su `main` e una prova su un repository diverso da ssdlc (R9).
- **D6. Regole di fiducia per progetto** (sezioni [3.2](#32-file-di-progetto-e-precedenza) e [3.8](#38-più-progetti-sullo-stesso-host)).
  - `trusted_users`, modalità di merge, supervisore e deleghe sono per progetto, nell'overlay. Se un altro progetto può avere utenti fidati diversi dall'operatore è una scelta caso per caso.
  - Un utente di sistema per progetto, (b), oppure uno condiviso, (a). La raccomandazione è (b) sull'host e un container per progetto.
  - Se un progetto di terzi può essere gestito sullo stesso host di ssdlc. La raccomandazione è sì, solo con utenti o container separati.
  - Come si garantisce l'esclusione per repository ([sezione 3.8](#38-più-progetti-sullo-stesso-host)): (L1) lock per repository sull'host, (L2) lease sul tracker, (L3) solo procedura. La raccomandazione è (L1) in R8, con un repository per host, e (L2) solo con il fencing del passo 4 dell'ADR 0001.
  - La tracciabilità delle chiavi di fiducia nell'overlay ([sezione 3.9](#39-rischi), K1): (T1) hash e cambi nel registro del dispatcher, (T2) repository di configurazione dell'operatore. La raccomandazione è T1 sempre e T2 a scelta dell'operatore.

## Precedenti

- [ADR 0001](0001-prodotto-unico-fasi-su-tracker.md): prodotto unico, vincoli 1, 5 e 6, piano in sei passi, decisione 1 sul confine.
- [ADR 0002](0002-ruoli-e-responsabilita.md): ruoli, supervisore e decisioni riservate.
- [ADR 0004](0004-passo-2-budget-stato-durevole.md): formato del piano delle issue (sezione 4).
- [ADR 0005](0005-deleghe-e-ruoli-degli-agent.md): policy di fiducia riconosciuta dai reviewer (regola 2 della sezione 3.1, #399), app di ruolo (D6).
- #174 e #260: installazione fissata e smoke test. #203 e #210: mappa dei documenti, non estratta dal pin.
- #169 e #200: test locali per modulo e nuova esecuzione dei fallimenti non pertinenti.
- #402 e PR #403: guida e script di ops, manifest delle app.
- Diario #76, 2026-11-19: richiesta dell'operatore.
