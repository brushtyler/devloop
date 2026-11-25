# ADR 0008: pool di esecuzione condiviso fra progetti (worker, budget comune, isolamento per job)

- **Stato:** proposto.
- **Fase:** design (#586).
- **Data:** 2026-11-25.
- **Base verificata:** il commit 0eb5a11 di `main`.

## Contesto

- Oggi ogni progetto servito da devloop porta con sé **la propria infrastruttura di esecuzione**: utente di sistema degli agent, login delle CLI, avviatore, toolchain e budget ([ADR 0006](0006-devloop-su-altri-repository.md), D6 e sezione 3.8; [ADR 0004](0004-passo-2-budget-stato-durevole.md), sezione 3.1).
- La prova della guida rapida su un repository di test (#515) ha mostrato che **quasi tutto il costo di un progetto nuovo sta dal lato dell'esecuzione**, non del progetto:
  - l'utente degli agent e la sua sigillatura;
  - i login di tre CLI, con le loro eccezioni: il demone di codex, i `trustedWorkspaces` di agy (#572);
  - l'avviatore e la copia fissata (#573).
- Con molti progetti questo lavoro si ripete identico per ciascuno.
- **Le risorse scarse sono degli account, non dei progetti.** Le quote dei provider sono per account, mentre il budget si conta per storia, dentro un progetto (ADR 0004). Con più progetti il conto per progetto non vede la risorsa reale né la sua contesa.
- **Lo stesso host tiene le chiavi delle app ed esegue il codice scritto dagli agent e i loro test** (ADR 0006, rischio K7). La separazione è solo fra utenti dello stesso kernel.

Terminologia: in questo ADR "**pool di esecuzione**", o solo "pool", è un insieme di worker che eseguono turni. **Non** è il pool dei reviewer dell'[ADR 0005](0005-deleghe-e-ruoli-degli-agent.md), sezione 3.3, che resta com'è. Un "**job**" è un'unità di lavoro che il pool esegue: un turno di un agent, oppure una verifica (sezione 3.4).

Questo ADR **propone, con alternative e raccomandazioni, e non decide.** Non modifica codice né altri documenti, salvo l'indice degli ADR. Le issue di attuazione nascono solo dopo l'approvazione, come per gli ADR [0004](0004-passo-2-budget-stato-durevole.md#4-piano-delle-issue) e [0006](0006-devloop-su-altri-repository.md#5-piano-delle-issue).

## Metodo

- Ogni affermazione sullo stato attuale cita il codice nella forma file:riga, al commit 0eb5a11.
- Il testo delle issue citate non è stato riletto su GitHub: valgono il corpo di #586, gli ADR su `main` e i messaggi dei commit.
- Valgono i vincoli e le decisioni degli ADR 0001, 0002, 0004, 0005 e 0006. In particolare:
  - il vincolo 5 dell'ADR 0001: nessun redispatch senza la prova che i writer sono terminati, e **un timeout non è una prova**;
  - il journal locale come autorità degli intenti e degli esiti (ADR 0004, D7);
  - codex senza sandbox mai (ADR 0006, D4), e niente container finché la sandbox di codex non si attiva senza `--privileged` né `CAP_SYS_ADMIN` (precisazione di D4, #426);
  - "un repository, un dispatcher" (ADR 0006, sezione 3.8);
  - nessun canale fra i ruoli di progetti diversi ([precisazione dell'ADR 0002 del 2026-11-24](0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-24-ruoli-per-progetto-629), #629).
- Nessuna proposta toglie garanzie esistenti: intento durevole prima della CLI, blocco sugli esiti incerti, verifica legata all'albero del commit, nessuna credenziale di GitHub agli agent, policy letta dal dispatcher e mai dal repository.
- Le citazioni non riportano valori di un'installazione reale: i percorsi sono quelli del repository o i nomi dei parametri di `ops/params.env.example`.

## 1. Stato attuale

### 1.1 Il turno come unità

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Chi esegue un turno | Il dispatcher chiama `AgentRunner.run` nel proprio processo, sul worktree dell'azione, con il prompt e lo schema del turno | devloop/dispatcher.py:3499; devloop/agents.py:854-869 |
| Comando della CLI | `build_command` costruisce l'argv: modello, effort, schema dell'output, argomenti di scrittura o di review dell'agent. Per codex `-s workspace-write`, e in review senza rete e senza radici scrivibili in più | devloop/agents.py:453-486, :463-477 |
| Ingresso | Il prompt composto dal dispatcher, lo schema dell'output (`devloop/schemas/`), il worktree, il timeout del runner | devloop/agents.py:980-1016; devloop/config.toml:96 |
| Uscita | L'output validato sullo schema, le modifiche lasciate nel worktree, la telemetria letta dall'output della CLI | devloop/agents.py:667-691, :379-399 |
| Fallback e memoria dei modelli | La catena dei modelli è una decisione del dispatcher, turno per turno: un modello non disponibile esce dalla catena e si ricorda in `model-unavailability.json` dello `state_dir` | devloop/dispatcher.py:3429-3470, :3397-3420; devloop/unavailability.py:1-15, :27 |
| Intento prima della CLI | Il record `turn` si scrive `open` prima dell'avvio, e l'intento nel journal prima ancora; se la scrittura fallisce il turno non parte | devloop/ledger.py:11-16; devloop/journal.py:7-9, :676; devloop/dispatcher.py:2760-2805 |
| Esiti | `returned`, `failed`, `not_dispatched`, `uncertain`; nessuna ripetizione automatica per un turno incerto | devloop/journal.py:10-14, :702, :732 |
| Turni rimasti aperti | Il tick successivo li marca `incomplete` nel ledger e `uncertain` nel journal; con `block_uncertain_turns` bloccano il lavoro fino a `/devloop reconcile` | devloop/dispatcher.py:3227-3245; devloop/config.toml:37 |
| Identità | Turni `turn-<n>-<hex>`; con `story_identity` e `attempt_identity`, storie e tentativi (ADR 0004, 3.2), spenti per default | devloop/journal.py:162-166; devloop/config.toml:43, :56 |
| Lavoro di un turno scaduto | Le modifiche non committate di un implementer o di un fixer scaduto si salvano come patch, mai committate (#374) | devloop/dispatcher.py:3676-3690 |

Il turno ha quindi già input definiti, output strutturato e un ciclo di vita registrato: è l'unità naturale di un job. **Manca la separazione fra chi decide e chi esegue**: il turno gira nel processo del dispatcher, sul suo worktree, e non c'è una porta dell'esecuzione. Le porte di oggi sono solo tracker, code review e CI (devloop/ports.py:1-12).

### 1.2 Utente degli agent, login e sigillatura

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Utente degli agent | Con `[agent_user] name` le CLI e i test girano come un altro utente, con `sudo -n -E -u`, e non leggono `key_dir`, `state_dir` e overlay | devloop/agentuser.py:1-30, :371-373; devloop/config.toml:245-247 |
| Sudoers | L'utente di devloop avvia come utente degli agent solo le tre CLI, `bash` e `kill` | ops/host/sudoers-agent:11-18 |
| Un utente per tutti i turni del progetto | Implementer e reviewer dello stesso progetto usano lo stesso utente: ciò che un turno scrive nella home non deve arrivare ai turni dopo. Le directory delle CLI sono di root e sigillate, controllate all'avvio e prima di ogni turno | devloop/agentuser.py:44-60; devloop/agents.py:759-770, :871-897 |
| Login | I login delle CLI stanno nella home dell'utente degli agent, che li legge e li rinnova: un turno li può leggere | devloop/agentuser.py:375-377; ops/host/setup-agent-user.sh:18-22 |
| Eccezioni delle CLI | Il login di codex lascia acceso il demone `codex app-server`, da fermare a loop fermo (#572); agy chiede di fidarsi della directory corrente | ops/host/setup-agent-user.sh:273, :301-309 |
| Turni di agy | Uno alla volta per runner, perché agy tiene memorie nella home; claude e codex insieme | devloop/agents.py:765-770 |
| Per progetto | Con `PROJECT` ogni progetto ha i suoi utenti, quindi la sua home degli agent e i suoi login (D6) | ops/params.env.example:25-36, :55-62; ops/README.md:1166-1177 |
| Separazione dalle chiavi | Senza utente degli agent l'ambiente isolato evita solo l'uso accidentale delle credenziali; l'isolamento vero viene dall'utente | devloop/agents.py:70-96, :84-86 |

### 1.3 Verifica

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Ciclo turno-test | Dopo un turno di scrittura il dispatcher esegue i test locali; se falliscono, un nuovo turno nello stesso worktree riceve la coda dell'output, fino a `implement_test_retries` | devloop/dispatcher.py:3640-3670; devloop/config.toml:95 |
| Selezione | `local_tests` sceglie il comando dal diff del worktree | devloop/dispatcher.py:3088-3100; devloop/localtests.py:182, :216 |
| Chi esegue i test | `Workspace.run_tests`, **come utente degli agent**, nel worktree in cui ha scritto l'agent, con una directory temporanea propria | devloop/workspace.py:952-990 |
| Alberi | Il dispatcher calcola l'albero prima e dopo il test (`git write-tree` su una copia dell'indice) e registra i file che il test ha cambiato | devloop/dispatcher.py:3102-3128; devloop/workspace.py:877-903 |
| Legame con il commit | Il record `verification` si lega al commit solo se l'albero prima del test, dopo il test e nel commit sono lo stesso | devloop/ledger.py:17-20, :483-516; devloop/dispatcher.py:3150-3160 |
| Commit e push | Li fa il dispatcher, con l'identità dell'implementer e il token in un solo comando; fra commit e push controlla i percorsi protetti | devloop/dispatch_actions.py:386-398; devloop/workspace.py:905-939 |

Il valore del record `verification` **non** viene dal fatto che il codice dei test sia fidato: è codice del repository, eseguito come utente degli agent. Viene da tre cose: il **comando** lo sceglie il dispatcher dalla configurazione dell'host; gli **alberi** li misura il dispatcher, non l'agent; il **commit** lo crea il dispatcher, e il legame vale solo se gli alberi coincidono.

### 1.4 Risorse, budget e parallelismo

| Elemento | Cosa fa oggi | Riferimenti |
|---|---|---|
| Budget | Envelope per storia nel journal del progetto: turni, tempo, dollari dove attestati; spento per default | devloop/budget.py:1-25; devloop/config.toml:301-302 |
| Consumi | Cumulativi, nessun rimborso; un turno senza esito confermato blocca la storia | devloop/budget.py:32-40 |
| Telemetria | Ciò che la CLI scrive, non autenticato; `null` dove la CLI non riporta | devloop/agents.py:379-399 |
| Parallelismo | `max_parallel_actions` per progetto; modalità continua con slot propri | devloop/config.toml:90-94; devloop/dispatcher.py:111-124 |
| Indisponibilità dei modelli | Memoria per `state_dir`: un modello esaurito su un account, scoperto da un progetto, non è noto agli altri | devloop/unavailability.py:1-15, :27 |
| Lock | Un lock per `state_dir` e un lock per repository sull'host; nessun limite globale fra progetti | devloop/__main__.py:347-356, :359-401; ops/host/tmpfiles-devloop-repos.conf:6-12 |
| Osservazione | Lo stallo legge `/proc` e i transcript delle sessioni nella home dell'utente degli agent; `devloop follow` legge gli stessi transcript | devloop/stall.py:1-17; devloop/follow.py:1-8; devloop/agents.py:1001-1009 |
| Misure | `devloop report` ricava i turni e le loro durate dai record firmati del ledger, per giorno UTC | devloop/report.py:1-12 |

### 1.5 Indipendenza dei reviewer

Un reviewer è indipendente da un autore se l'identità e la tupla `(kind, model, effort)` sono diverse (devloop/policy.py:371-375). Il dispatcher salta un modello non indipendente prima di avviarlo (devloop/dispatcher.py:3485-3490). Il modello che gira è quindi parte della garanzia: chi esegue il turno non lo può cambiare.

## 2. Lacune

| # | Lacuna | Dove | Effetto |
|---|---|---|---|
| E1 | Nessuna porta dell'esecuzione | sezione 1.1 | Il turno gira solo nel processo del dispatcher: non si può spostare altrove senza riscrivere i passi |
| E2 | Utente, login e sigillatura per progetto | sezione 1.2 | Ogni progetto ripete login, eccezioni delle CLI e manutenzione (#515, #572, #573) |
| E3 | Login leggibili dai turni | sezione 1.2 | Un turno può leggere i login degli account; se più progetti usano gli stessi account, l'esposizione è già dell'account, non del progetto |
| E4 | Risorse contate per progetto | sezione 1.4 | Nessun tetto globale per account; la memoria dei modelli esauriti non si condivide; con N progetti il parallelismo è la somma degli slot, senza coordinamento |
| E5 | Codice degli agent e chiavi sullo stesso kernel | sezioni 1.2 e 1.3 | K7 dell'ADR 0006: un'evasione dall'utente degli agent arriva alle chiavi |
| E6 | Verifica legata al dispatcher | sezione 1.3 | L'host del dispatcher deve avere la toolchain di ogni progetto ed eseguire il codice del repository accanto alle chiavi |
| E7 | Osservazione legata all'host | sezione 1.4 | Stallo e `follow` leggono `/proc` e la home dell'utente degli agent: con un'esecuzione altrove non vedono niente |
| E8 | Nessuna misura del tempo perso | sezione 1.4 | Non si sa se il collo di bottiglia sono i turni, la coda, la CI o le attese dell'operatore |

## 3. Proposte

Ogni area ha alternative e una raccomandazione. Le raccomandazioni sono proposte: diventano decisioni con l'approvazione di questo ADR, salvo le parti elencate nella [sezione 6](#6-decisioni-riservate-alloperatore), che restano dell'operatore.

### 3.1 Confine fra progetto e pool

| Resta al progetto | Va al pool |
|---|---|
| Politica di fiducia: `trusted_users`, percorsi protetti, approvazioni, merge | Credenziali e login delle CLI |
| Identità su GitHub: l'insieme di app, eventualmente condiviso come da #570 | Isolamento per job |
| File di progetto, istruzioni, `test_cmd` | Contabilità delle risorse: tetti per account, pesi per progetto, ammissione e coda |
| L'ambiente di build come immagine o profilo dichiarato | Scheduling, con le priorità e l'equità fra progetti |
| Il dispatcher: decisioni, journal locale come autorità (ADR 0004, 3.1), scritture su GitHub | Telemetria aggregata |
| Il budget della storia, chiesto al pool come prenotazione | Esecuzione dei test di verifica, come job separato (sezione 3.4) |
| La catena dei modelli e la scelta del modello di ogni turno | Memoria dei modelli non disponibili per account |

Il dispatcher del progetto diventa un **cliente leggero** del pool: decide, chiede un job con un budget massimo, riceve l'esito, lo controlla e pubblica. Non cambia niente di ciò che il dispatcher scrive su GitHub, né di chi lo scrive.

Due regole derivano dalle decisioni in vigore:

- **Il pool non decide niente che sia policy del progetto.** Comandi ammessi agli agent, argomenti delle CLI, `test_cmd`, modello ed effort arrivano nel job dal dispatcher, che li prende dalla configurazione dell'host (ADR 0006, D2). Il worker li applica e non ne aggiunge. Un job che chiede un modello non disponibile torna `unavailable`, mai eseguito su un altro modello: il fallback resta del dispatcher (sezione 1.1), e così l'indipendenza dei reviewer (sezione 1.5).
- **Il pool non scrive su GitHub.** Non ha chiavi delle app né token: riceve il codice dal dispatcher e restituisce una patch, mai un push.

### 3.2 Il job

**Ingresso.**

- Progetto, identità del turno, sha di base e codice. Il codice arriva come **bundle git** inviato dal dispatcher, senza token per il worker, anche per un repository privato.
- Prompt composto dal dispatcher, schema dell'output, timeout, budget massimo del job (turni e tempo, ADR 0004, 3.3).
- Profilo dell'agent: CLI, modello, effort, argomenti di scrittura o di review, comandi ammessi.
- Profilo dell'ambiente (sezione 3.5) e versione del contratto.

**Uscita.**

- Il **risultato come albero git**, non come diff testuale: un bundle con un commit il cui albero è ciò che `git add -A` metterebbe nel commit, lo stesso albero che oggi `Workspace.tree` calcola (devloop/workspace.py:877-892). Il dispatcher lo importa in quarantena, controlla che contenga solo gli oggetti raggiungibili dal commit dichiarato ed entro un limite di dimensione, e crea **il proprio** commit con lo stesso albero, con messaggio e autore di oggi. Rispetto a una patch testuale si conservano esattamente modi, link e file binari, e l'albero si confronta per hash. Il salvataggio del lavoro di un turno scaduto (#374) è già una patch di questo tipo (devloop/dispatcher.py:3676-3690).
- L'output strutturato, che il dispatcher valida di nuovo sullo schema: il worker non è fidato sulla forma.
- La telemetria **dichiarata** e i log, puliti dai segreti dal worker e di nuovo dal dispatcher prima di pubblicarli.

**Identità e idempotenza.** Il job porta l'identità del turno del journal del progetto (devloop/journal.py:162) e, con `attempt_identity`, quella di storia, fase e tentativo (ADR 0004, 3.2). Una consegna ripetuta con la stessa identità non avvia un secondo turno: il pool restituisce lo stato o l'esito del primo.

**Intento, ammissione e lease.**

- Oggi l'intento si scrive prima dell'avvio della CLI (sezione 1.1). Con il pool fra le due cose c'è una coda. Alternative:
  - (i) **intento alla consegna.** Il dispatcher prenota e scrive l'intento quando consegna il job. Un job che il pool rifiuta o lascia in coda oltre la scadenza resta `not_dispatched`, e con le regole di oggi resta contato (devloop/budget.py:32-36): la coda consuma l'envelope;
  - (ii) **intento all'ammissione.** Alla consegna il dispatcher esegue solo il controllo (`check_only`, devloop/dispatcher.py:2802-2826). Quando il pool ammette il job, chiede al dispatcher il via libera; il dispatcher prenota e scrive l'intento, e solo allora risponde. La CLI parte solo dopo la risposta.
- **Raccomandazione: (ii).** Conserva alla lettera la regola "intento durevole prima della CLI" e non fa pagare la coda alla storia. Costa un giro in più fra pool e dispatcher, trascurabile rispetto a un turno.
- **Lease e heartbeat.** Il worker rinnova il lease del job a intervalli; con il lease il pool inoltra al dispatcher i segnali di attività che oggi lo stallo legge sull'host (devloop/stall.py:1-17) e il transcript che `devloop follow` mostra (devloop/follow.py:1-8).
- **Un lease scaduto non è una prova di terminazione** (ADR 0001, vincolo 5). Il turno diventa `uncertain` nel journal e `incomplete` nel ledger, come oggi un turno rimasto aperto (devloop/dispatcher.py:3227-3245), e non si ripete da solo.
- **Fencing.** Ogni lease ha un numero monotono. Il dispatcher accetta un esito solo con il numero del lease corrente; un esito tardivo di un lease scaduto si registra e si scarta. Poiché il worker non scrive su GitHub e non fa push, un worker che sopravvive al proprio lease non può scrivere niente: il suo unico danno è il consumo dell'account. È la parte più semplice del fencing del passo 4 dell'ADR 0001, resa possibile dal fatto che l'uscita del job è un dato e non un'azione.
- **Prova di terminazione.** Se l'isolamento per job lo permette (sezione 3.5), il pool può distruggere l'ambiente del job e attestarlo. Se quella attestazione basti a riconciliare da sola un turno incerto è una decisione del passo 4 dell'ADR 0001, non di questo ADR: fino ad allora la riconciliazione resta `/devloop reconcile` di un utente fidato.

**Ciclo turno-test.** Il ciclo di oggi alterna turno e test nello stesso worktree (sezione 1.3). Nel pool diventa: job dell'agent, poi job di verifica sull'albero restituito; se la verifica fallisce, un nuovo job dell'agent parte dall'albero del tentativo precedente, con la coda dell'output nel prompt. Più consegne, ma ogni passo ha un ingresso preciso, come chiede l'idempotenza. L'agent può continuare a eseguire i test da sé dentro il proprio job, come oggi: servono a lui, non sono la verifica.

**Versioni.** Gli altri progetti fissano un tag di devloop (ADR 0006, D1), quindi progetti diversi possono avere versioni diverse del dispatcher. Il contratto del job ha una **versione propria**; il pool dichiara quelle che accetta e rifiuta le altre con un errore che le nomina, mai con un'interpretazione.

### 3.3 Risorse e budget

**La risorsa è lo slot di account**: provider, account, modelli disponibili, quota residua quando è leggibile. Aggiungere worker con lo stesso account aumenta il parallelismo, non la capacità.

**La quota residua spesso non si legge.** Claude non ha una quota leggibile in modalità headless; codex e agy hanno solo il delta dell'intero account (ADR 0004, "Misure disponibili per agent"). Il pool conosce quindi lo slot da tre fonti: i **tetti configurati** dall'operatore, le misure che fa da sé (turni e tempo per account) e i **segnali di esaurimento** delle CLI, con l'ora di ripristino quando la CLI la annuncia (devloop/unavailability.py:1-10). La memoria dei modelli non disponibili passa dal progetto al pool e diventa per account: un esaurimento scoperto da un progetto vale per tutti, e il pool tiene in coda i job di quel modello fino al ripristino, invece di farli fallire.

**Due livelli di budget.**

- **Progetto** (ADR 0004, invariato): l'envelope della storia, nel journal del progetto, che resta l'autorità. La prenotazione del turno diventa la richiesta al pool (sezione 3.2, (ii)).
- **Pool** (nuovo): i tetti per account e la ripartizione fra progetti, nel registro del pool, che ne è l'autorità. Il pool non vede le storie dei progetti e non le giudica.

Alternative per il livello del pool:

- (a) **Tetti per account, con pesi per progetto.** Ogni account ha tetti di turni e di tempo per finestra; i progetti ottengono gli slot in proporzione ai pesi, con un minimo garantito, e la capacità non usata da un progetto passa agli altri.
  - Pro: usa la risorsa reale; nessuno spreco quando un progetto è fermo; un progetto non può affamare gli altri.
  - Contro: il comportamento di un progetto dipende dal carico degli altri, e va spiegato nei log e in `status`.
- (b) **Quote fisse per progetto.** Ogni progetto ha i suoi turni per finestra, indipendenti dagli altri.
  - Pro: prevedibile, facile da spiegare.
  - Contro: spreca la capacità dei progetti fermi; la somma delle quote può superare l'account, e allora il conflitto torna.
- (c) **Sola priorità.** Una coda unica ordinata per priorità.
  - Pro: semplice.
  - Contro: un progetto con molto lavoro ad alta priorità affama gli altri; nessuna equità.

**Raccomandazione: (a)**, con l'ordine dei job **dentro** un progetto deciso dal dispatcher del progetto (la sua pianificazione e `priority_labels`) e l'ordine **fra** progetti deciso dal pool con i pesi. Il valore dei tetti e dei pesi è dell'operatore.

**Tempo in coda.** `wall_seconds` della storia è una scadenza assoluta (devloop/budget.py:9-15): con un pool condiviso, l'attesa in coda consuma la scadenza. L'esito del job distingue il tempo in coda dal tempo di esecuzione, e `status` li mostra separati, perché l'operatore capisca se una storia scade per lavoro o per attesa.

**Parallelismo.** `max_parallel_actions` resta per progetto: limita le azioni del dispatcher, non i job in esecuzione. Il limite reale di esecuzione diventa quello del pool, per account e per host.

**Telemetria dichiarata.** I token e i costi che la CLI riporta restano **dichiarati**, come oggi (devloop/agents.py:382-383), ora riferiti da un worker invece che letti dal dispatcher: si registrano come tali, possono alzare un addebito e mai abbassarlo sotto la riserva (ADR 0004, D7). Le misure che il pool fa da sé, cioè tempo in coda, tempo di esecuzione e numero di turni per account, sono attestate dal pool: valgono quanto la fiducia nel pool, e si registrano con l'identità del pool.

### 3.4 Verifica

Oggi la garanzia della verifica viene da tre cose (sezione 1.3): comando scelto dal dispatcher, alberi misurati dal dispatcher, commit creato dal dispatcher. Nel pool:

- il **comando** resta del dispatcher, nel job;
- il **commit** resta del dispatcher, con lo stesso albero del bundle (sezione 3.2);
- l'**albero prima del test** lo calcola il dispatcher dal bundle che invia, e il job lo dichiara come ingresso;
- l'**albero dopo il test** e l'**esito** li misura chi esegue il test.

Il terzo punto è l'unico che cambia natura: oggi è una misura del dispatcher, nel pool è una **dichiarazione** di chi esegue il test. Un worker compromesso potrebbe dichiarare "passato, nessun file cambiato" su un albero che non ha mai provato. Nessun meccanismo di questo ADR lo impedisce del tutto; lo limitano tre regole.

1. **Job fidato e separato.** La verifica è un job di tipo `verification`, distinto da quello dell'agent: un ambiente nuovo, mai lo stesso del turno che ha scritto l'albero, su un worker della classe di fiducia del progetto (sezione 3.5).
2. **Legame per hash.** Il job riceve l'albero come bundle e ne dichiara l'hash; il worker lo ricalcola dopo l'estrazione e rifiuta il job se non coincide. Al ritorno dichiara l'albero dopo il test e, se è cambiato, restituisce i file cambiati. Il dispatcher lega il commit solo se l'albero inviato, l'albero dichiarato prima e dopo il test e l'albero del commit coincidono: la regola di `bind_commit` di oggi (devloop/ledger.py:508-516), con un termine in più.
3. **Esecutore nel record.** Il record `verification` registra l'identità del worker e del pool che l'ha eseguito. Una verifica del dispatcher e una del pool si distinguono nel ledger e nella ricevuta.

Alternative per **dove girano i test di verifica** (P5):

- (a) **Job fidato separato nel pool**, con le tre regole.
  - Pro: l'host del dispatcher non esegue più codice del repository (E6) e non ha bisogno della toolchain di ogni progetto; il codice dei test gira lontano dalle chiavi (K7 dell'ADR 0006).
  - Contro: la misura degli alberi diventa una dichiarazione di un worker; un giro in più fra dispatcher e pool per ogni tentativo.
- (b) **Il dispatcher, come oggi**, anche quando i turni girano nel pool.
  - Pro: la garanzia resta una misura del dispatcher.
  - Contro: il dispatcher deve importare l'albero ed eseguire i test con un utente degli agent proprio, quindi l'host del dispatcher resta un ambiente di esecuzione con la sigillatura e la toolchain del progetto: si perde gran parte del beneficio del pool, e K7 resta.

**Raccomandazione: (a), dal passo 3** del piano, con le regole 1–3. Fino ad allora vale (b). Il cambio di natura della misura va scritto nella guida e nella ricevuta della PR. La garanzia che i reviewer vedono il codice esatto del commit non cambia: viene dall'head della PR, non dalla verifica.

### 3.5 Sicurezza e isolamento

**Il worker non scrive su GitHub e non ha chiavi delle app**, come l'utente degli agent di oggi (devloop/agentuser.py:1-8).

**Isolamento per job.** Ogni job gira in un ambiente usa e getta:

- creato per il job, distrutto alla fine, con il worktree;
- nessuno stato fra job di progetti diversi: niente home, cache delle CLI, cache dei pacchetti o directory temporanee condivise;
- le memorie delle CLI (sezione 1.2) non sopravvivono al job, e la serializzazione dei turni di agy (devloop/agents.py:765-770) non serve più;
- la sigillatura delle directory delle CLI di oggi (#438, #517) diventa una proprietà dell'immagine dell'ambiente, non un controllo prima di ogni turno.

Alternative (P4), tutte **subordinate a una prova** come quella di #426 (sandbox di codex attiva senza `--privileged` né `CAP_SYS_ADMIN`, verificata con la stessa prova):

- (a) **microVM con kernel proprio.**
  - Pro: il confine è un kernel, non un utente: un'evasione dalla sandbox dell'agent non arriva all'host né agli altri job; il kernel della microVM è sotto il controllo del pool, quindi la sandbox di codex ha le funzioni che chiede senza privilegi sull'host; distruggerla è una prova di terminazione naturale (sezione 3.2).
  - Contro: un'infrastruttura nuova da installare e mantenere; tempo di avvio per job; serve la virtualizzazione dell'host.
- (b) **Container con un runtime in cui la sandbox di codex funziona.**
  - Pro: strumenti noti, immagini dichiarate (per esempio un devcontainer del progetto).
  - Contro: con il runtime di #426 la prova è fallita; serve un altro runtime, e la prova va ripetuta; il kernel resta quello dell'host.
- (c) **Utente per job sull'host.** Un insieme di utenti di sistema preparati in anticipo; ogni job ne prende uno, con la home ricreata, e lo restituisce.
  - Pro: la sandbox di codex funziona già così (è il modello di oggi, con un utente per progetto); nessuna infrastruttura nuova.
  - Contro: kernel condiviso, come oggi; ricreare la home a ogni job significa ricopiare i login, o usare il proxy delle credenziali; il confine fra job resta quello fra utenti.

**Raccomandazione:** provare prima (a), perché è l'unica che chiude anche E5; se la prova fallisce, (c), che ripete il modello di oggi per job invece che per progetto. (b) solo se un runtime supera la prova. La scelta è dell'operatore dopo la prova (P4).

**Credenziali fuori dalla sandbox.** Oggi un turno può leggere i login degli account dalla home dell'utente degli agent (sezione 1.2). In un pool condiviso, un'esfiltrazione da un job di un progetto colpisce tutti i progetti che usano quell'account. Va però detto che, se i progetti usano gli stessi account anche oggi, con utenti degli agent diversi, l'esposizione è già dell'account: il pool non la allarga, la concentra in un punto solo. Alternative:

- (a) **Login copiati nel job**, in sola lettura. Come oggi, con un raggio più largo se gli account sono per progetto.
- (b) **Proxy delle credenziali.** La CLI nel job ha un segnaposto; il traffico verso il provider passa da un proxy del worker, che aggiunge il segreto e rinnova i token. Il job non ha mai il segreto, e l'uscita di rete del job si può limitare al proxy.
  - Da provare per ciascuna CLI: se ammette un indirizzo del provider diverso, come gestisce il rinnovo del login in abbonamento, se si rompe con un aggiornamento.
- (c) **Credenziali di breve durata per job**, dove il provider le offre.

**Raccomandazione: (b) come obiettivo**, con una prova per CLI. Una CLI che non supera la prova resta con (a), e i suoi job girano solo nei pool che ammettono quel rischio (P1).

**Integrità dei risultati.** Un worker compromesso non ruba solo segreti: può **alterare l'uscita** dei job che esegue, per esempio aggiungendo codice alla patch di un altro progetto. Le difese di oggi restano a valle (controllo dei percorsi protetti, review di due fornitori, merge), ma un pool condiviso è un punto da cui si raggiungono tutti i progetti.

**Pool separati per livello di fiducia.** Il repository di devloop è il motore che tutti i progetti fissano: un'alterazione delle sue patch arriverebbe a ogni installazione. È la stessa logica per cui devloop tiene un insieme di app suo, non condiviso con i progetti che serve (precisazione di D3 dell'ADR 0006, #570). Alternative (P2):

- (a) **Pool proprio** per devloop: worker, login e ammissione suoi, anche sullo stesso host.
- (b) **Worker dedicati** nello stesso pool: solo job di devloop, ma ammissione, registro e credenziali in comune.
- (c) **Nessun isolamento**: devloop come gli altri progetti.

**Raccomandazione: (a).** Per devloop non è un costo nuovo: ha già la sua infrastruttura di esecuzione, che resta. Il pool condiviso serve gli altri progetti.

**Ambiente di build.** Il progetto dichiara il profilo dell'ambiente (toolchain, versioni) nel file di progetto sull'host, mai nel repository (ADR 0006, D2, requisito 4): con (a) o (b) è un'immagine, con (c) un profilo di pacchetti installati sull'host del pool. Il pool non costruisce immagini dal repository.

### 3.6 Trasporto

I worker **chiedono** il lavoro al pool (pull), senza porte aperte verso di loro, su un canale autenticato con un'**identità per worker**, revocabile.

**GitHub non fa da coda di job**: il repository è pubblico, ci sono rate limit e latenza, il tracker si riempirebbe di rumore, e il bundle del codice non ci passa.

Alternative per il canale:

- (a) **Directory di scambio sull'host**, con i permessi del filesystem, come la casella fra i ruoli di oggi (`$COORDINATION_DIR`).
- (b) **Socket Unix** con l'identità del processo dal kernel.
- (c) **HTTPS con TLS reciproco**, un certificato per worker.

**Raccomandazione:** (a) o (b) al passo 2, sullo stesso host, senza rete; (c) al passo 4, per i worker remoti. Il dispatcher di un progetto parla con il pool, mai con un worker: il pool è l'unico che conosce i worker.

### 3.7 Osservazione

- Lo **stallo** e `devloop follow` (sezione 1.4) leggono dall'heartbeat e dal flusso del transcript del job, inoltrati dal pool (sezione 3.2), non più da `/proc` e dalla home degli agent.
- **Supervisore e coordinatore osservano anche il pool, in sola lettura** (ADR 0002 invariato). Per la precisazione del 2026-11-24 (#629) non c'è nessun canale fra i ruoli di progetti diversi: la vista del pool per un ruolo di progetto mostra **solo i job del suo progetto** e gli aggregati senza i dettagli degli altri (per esempio l'occupazione di un account). La vista completa è dell'operatore.

### 3.8 Rapporto con le decisioni in vigore

| Decisione | Esito | Come |
|---|---|---|
| ADR 0006, D6 (un utente di sistema e i login per progetto) | **Superata per l'esecuzione**, con l'approvazione di P1 | Il progetto tiene il suo utente per il dispatcher, il suo `state_dir` e la sua `key_dir`. L'utente degli agent e i login passano al pool. Con P1 (c), D6 vale ancora fra pool di fiducia diversa |
| ADR 0006, D4 e la precisazione di #426 (niente container per ora) | **Conservata** | Il passo 2 dipende dalla prova dell'isolamento per job, con lo stesso criterio di #426. Un container torna solo con un runtime che supera quella prova |
| ADR 0006, sezione 3.8 ("un repository, un dispatcher"; lock per repository) | **Conservata** | Il pool serve più dispatcher, uno per repository. I worker remoti del passo 4 non sono dispatcher |
| ADR 0006, D3 e la precisazione di #570 (insiemi di app) | **Conservata** | Il pool non ha chiavi delle app. Il pool di devloop separato (P2) segue la stessa logica dell'insieme di app di devloop |
| ADR 0004 (budget per storia, journal locale come autorità) | **Conservata, con un livello in più** | L'envelope e il journal restano del progetto. Il pool aggiunge tetti per account e ammissione, con un registro suo (sezione 3.3) |
| ADR 0004, D7 (telemetria che alza e mai abbassa) | **Conservata** | La telemetria del worker è dichiarata; le misure del pool si registrano con la sua identità |
| ADR 0001, vincolo 5 (contenimento e fencing) | **Precisata** | Lease, fencing con numero monotono e ambienti usa e getta ne sono una parte. La prova di terminazione resta del passo 4 dell'ADR 0001 |
| ADR 0005 (pool dei reviewer, diversità delle chiavi) | **Invariata** | Il modello del job lo decide il dispatcher; il pool non sostituisce modelli. Lo scheduling rispetta le regole sulla chiave dell'implementer |
| ADR 0002 (ruoli) | **Invariata** | Il pool non è un ruolo che decide. Supervisore e coordinatore lo osservano in sola lettura, per progetto (sezione 3.7) |

### 3.9 Rischi

| # | Rischio | Mitigazione proposta |
|---|---|---|
| Q1 | **Concentrazione.** Un pool compromesso raggiunge tutti i progetti che serve, in segreti e in integrità dei risultati | Pool separati per fiducia (P1, P2); isolamento per job (P4); proxy delle credenziali; controlli a valle invariati (percorsi protetti, review, merge) |
| Q2 | **Verifica dichiarata.** L'albero dopo il test e l'esito diventano dichiarazioni di un worker | Job di verifica separato e fidato, legame per hash, esecutore nel record (sezione 3.4); la ricevuta della PR dice chi ha verificato |
| Q3 | **Lease scaduto preso per terminazione** | Un lease scaduto è `uncertain`, mai ripetuto da solo; fencing sugli esiti (sezione 3.2) |
| Q4 | **Coda che consuma la scadenza delle storie** | Intento all'ammissione; tempo in coda e di esecuzione separati in `status` (sezione 3.3) |
| Q5 | **Punto unico di guasto.** Un pool fermo ferma tutti i progetti | Il dispatcher tratta un pool irraggiungibile come oggi una CLI che non parte: nessun intento, nessun turno, escalation dopo una soglia. Più pool (passo 5) |
| Q6 | **Versioni diverse** fra i dispatcher dei progetti e il pool | Versione del contratto, dichiarata dal pool e rifiutata con un errore (sezione 3.2) |
| Q7 | **Residui fra job** (cache dei pacchetti, memorie delle CLI) | Ambiente usa e getta, nessuna cache condivisa fra progetti (sezione 3.5) |
| Q8 | **Fuga di dati fra progetti** attraverso le viste del pool | Vista per progetto per i ruoli; vista completa solo all'operatore (sezione 3.7) |
| Q9 | **La prova dell'isolamento fallisce** | Il passo 2 si ferma e torna all'operatore, come R7 dell'ADR 0006; resta l'alternativa (c) della sezione 3.5 |
| Q10 | **Bundle ostile** in uscita da un worker | Importazione in quarantena, solo gli oggetti raggiungibili dal commit dichiarato, limite di dimensione, commit ricreato dal dispatcher (sezione 3.2) |

## 4. Collocazione nel piano dell'ADR 0001

- Il passo 4 dell'ADR 0001 è il contenimento e il fencing degli esecutori. Il pool **ne anticipa una parte**: isolamento per job, lease, fencing sugli esiti. Non lo sostituisce: la prova di terminazione e la sua forza come riconciliazione restano di quel passo.
- Il passo 5 dell'ADR 0001, il backend filesystem, non dipende dal pool: il pool non usa il tracker.
- L'alternativa L2 dell'ADR 0006, sezione 3.8 (lease sul tracker), resta un'altra cosa: riguarda i dispatcher, il pool riguarda i worker.

## 5. Piano delle issue

Le issue si aprono dopo l'approvazione di questo ADR, una per passo, e citano la sezione di questo ADR su `main` che attuano, come chiede il [ciclo degli ADR](README.md#ciclo-di-un-adr). Il prefisso `X` (esecuzione) le distingue dai piani degli altri ADR.

- Le issue che toccano `devloop/` richiedono il livello 2 di review (`sensitive_paths`).
- Quelle che cambiano chi esegue i turni e i test, o come si lega la verifica, toccano la policy di fiducia: con la regola dei reviewer dell'ADR 0005 vanno all'operatore.
- Quelle che toccano `docs/adr/`, `AGENTS.md` o `CLAUDE.md` le mergia l'operatore (`operator_only_paths`).
- Ogni issue aggiorna i documenti che la mappa `[docs.map]` lega ai file che cambia, e dichiara il passo sull'host quando ne serve uno (`ops/host/migrate.md`).
- Le funzioni nuove restano spente finché l'operatore non le attiva; senza pool, devloop si comporta come oggi.

| Issue | Titolo proposto | Perimetro | Dipendenze | Review |
|---|---|---|---|---|
| X0 | feat(devloop): misura del tempo per fase del lavoro | Dai record del ledger e dal journal: tempo dei turni, attese fra un'azione e la successiva, round della CI, attese dell'operatore (`agent:needs-operator`), per issue e per giorno, in `devloop report`. Nessun cambio al comportamento. Serve a ordinare i passi seguenti | Approvazione dell'ADR | Livello 2 |
| X1 | refactor(devloop): porta di esecuzione | Il contratto job/esito della sezione 3.2 nel codice, accanto alle porte di `devloop/ports.py`, con un **esecutore locale** che chiama `AgentRunner` e `Workspace.run_tests` come oggi. Il dispatcher passa solo dalla porta. Solo refactoring: stessi prompt, stessi record, stessi alberi; prova di equivalenza sui test esistenti | X0 non necessario | Livello 2 |
| X2 | feat: pool locale | Un pool sullo stesso host che serve tutti i progetti dell'host della stessa classe di fiducia (P1): una sola installazione delle CLI, tetti per account e pesi per progetto (P3), ammissione con intento all'ammissione, lease e fencing, memoria dei modelli per account, isolamento per job (P4), proxy delle credenziali dove la prova per CLI passa. **Prima, la prova dell'isolamento**, con il risultato nell'issue: se fallisce, l'issue si ferma e torna all'operatore. Procedura sull'host in `ops/host/` | X1, P1, P3, P4 | Livello 2, all'operatore |
| X3 | feat: verifica come job separato | Il job `verification` della sezione 3.4, con le regole 1–3; l'esecutore nel record `verification` e nella ricevuta; guida aggiornata sul cambio di natura della misura | X2, P5 | Livello 2, all'operatore |
| X4 | feat: worker remoti | Identità per worker revocabile, canale HTTPS con TLS reciproco, lease e fencing fra host, osservazione per progetto (sezione 3.7) | X2, X3, P6 | Livello 2, all'operatore |
| X5 | feat: più pool | Più pool, separati per fiducia o per risorse; un progetto sceglie il suo pool nell'overlay | X2, P1, P2 | Livello 2, all'operatore |

**Ordine.**

- X0 e X1 sono indipendenti e possono partire subito dopo l'approvazione. X0 viene prima nelle priorità: se il tempo si perde in coda o in attesa dell'operatore, e non nei turni, il pool non è il primo intervento.
- X2 segue X1 e le decisioni P1, P3 e P4, ed è fermata dalla prova dell'isolamento finché non passa. Qui arriva gran parte del beneficio, senza rete.
- X3 segue X2. X4 segue X3. X5 può seguire X2 in parallelo a X3.
- Il pool di devloop (P2) non richiede un'issue: con (a) devloop resta com'è.

**Esclusi:**

- più dispatcher sullo stesso repository: resta "un repository, un dispatcher" (ADR 0006, 3.8);
- worker o pool di terzi: un eventuale ADR futuro;
- codex senza sandbox;
- GitHub come coda dei job;
- ogni cambio al pool dei reviewer dell'ADR 0005.

## 6. Decisioni riservate all'operatore

Le presenta questo ADR con le alternative e una raccomandazione, senza prenderle.

- **P0. Approvazione di questo ADR,** con il merge della sua PR.
- **P1. Pool condiviso fra progetti** (sezioni [3.1](#31-confine-fra-progetto-e-pool) e [3.8](#38-rapporto-con-le-decisioni-in-vigore)).
  - Alternative: (a) sì, fra tutti i progetti dell'host; (b) no, resta D6 dell'ADR 0006; (c) solo fra progetti dello stesso proprietario e dello stesso livello di fiducia.
  - Raccomandazione: **(c)**. È lo stesso perimetro dell'insieme di app condiviso della precisazione di D3 (#570): stesso proprietario, stesso host. Un progetto di un altro livello di fiducia ha un pool suo, o resta con D6.
- **P2. Isolamento di devloop** ([sezione 3.5](#35-sicurezza-e-isolamento)).
  - Alternative: (a) pool proprio; (b) worker dedicati nello stesso pool; (c) nessuno.
  - Raccomandazione: **(a)**. devloop è il motore che tutti i progetti fissano, e ha già la sua infrastruttura di esecuzione.
- **P3. Modello di budget del pool** ([sezione 3.3](#33-risorse-e-budget)).
  - Alternative: (a) tetti per account con pesi per progetto; (b) quote fisse per progetto; (c) sola priorità.
  - Raccomandazione: **(a)**, con l'envelope della storia dell'ADR 0004 invariato e l'intento scritto all'ammissione. I valori dei tetti e dei pesi restano da fissare.
- **P4. Isolamento per job** ([sezione 3.5](#35-sicurezza-e-isolamento)).
  - Alternative: (a) microVM; (b) container con un runtime in cui la sandbox di codex funziona; (c) utente per job sull'host.
  - Raccomandazione: **provare prima (a)**, poi (c) se la prova fallisce; (b) solo con un runtime che supera la prova di #426. La scelta dopo la prova, come per #426. Codex senza sandbox resta escluso.
- **P5. Dove girano i test di verifica** ([sezione 3.4](#34-verifica)).
  - Alternative: (a) job fidato separato nel pool; (b) il dispatcher, come oggi.
  - Raccomandazione: **(a) dal passo 3**, con le regole 1–3 della sezione 3.4 e il cambio di natura della misura dichiarato; (b) fino ad allora.
- **P6. Worker remoti e progetti di altri proprietari** ([sezione 5](#5-piano-delle-issue)).
  - Alternative: (a) fuori perimetro per ora; (b) ammessi, a condizioni da definire.
  - Raccomandazione: **(a)**. I worker remoti si valutano con X4, dopo la prova del pool locale; i progetti di altri proprietari restano con D6 o con un pool proprio.

Restano valide tutte le decisioni riservate degli ADR 0001, 0002, 0004, 0005 e 0006, salvo D6 dell'ADR 0006 per l'esecuzione, se l'operatore approva P1.

## Precedenti

- [ADR 0001](0001-prodotto-unico-fasi-su-tracker.md): vincolo 5 (contenimento e fencing), passo 4 del piano.
- [ADR 0002](0002-ruoli-e-responsabilita.md): ruoli; precisazione del 2026-11-24 sui ruoli per progetto (#629).
- [ADR 0004](0004-passo-2-budget-stato-durevole.md): budget per storia, journal locale come autorità, identità di storia, fase e tentativo, misure disponibili per agent.
- [ADR 0005](0005-deleghe-e-ruoli-degli-agent.md): pool dei reviewer ed esclusione per agent effettivo.
- [ADR 0006](0006-devloop-su-altri-repository.md): D3 e la precisazione di #570, D4 e la precisazione di #426, D6, sezione 3.8 e rischio K7.
- #415, #438, #517: utente degli agent e sigillatura delle CLI.
- #374: lavoro di un turno scaduto salvato come patch.
- #515, #572, #573: prova della guida rapida, eccezioni delle CLI, avviatore e copia fissata.
- #586: richiesta di questo ADR.
