# ADR 0002: ruoli e responsabilità, e modalità di merge `off`, `strict` e `auto`

- **Stato:** approvato dall'operatore con il merge di PR #189 (2026-11-17), e implementato: le modalità di merge con #172 (PR #188) e lo smistamento delle escalation con #182 (PR #198). La modalità `auto` è attiva dal 2026-11-17. La decisione dell'operatore del 2026-11-22 su #524 (modello a quattro ruoli e nuovo principio 4) è scritta come [precisazione](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527) e nel principio 4, con #527. La decisione dell'operatore del 2026-11-23 sui compiti del coordinatore è scritta come [precisazione](#precisazione-del-2026-11-23-compiti-del-coordinatore-594), con #594. La seconda decisione dell'operatore del 2026-11-23, sul modello operativo dei ruoli in assenza dell'operatore, è scritta come [seconda precisazione](#seconda-precisazione-del-2026-11-23-modello-operativo-dei-ruoli-597), con #597. La decisione dell'operatore del 2026-11-24 sui ruoli con più progetti sullo stesso host è scritta come [precisazione](#precisazione-del-2026-11-24-ruoli-per-progetto-629), con #629. La decisione dell'operatore del 2026-11-24 su #643 (utenti dei ruoli per insieme di app e letture del supervisore con la sua app) la precisa, ed è scritta come [precisazione](#precisazione-del-2026-11-24-utenti-dei-ruoli-per-insieme-di-app-643), con #643. La decisione dell'operatore del 2026-11-26 su #691 (issue aperte senza stato e loro chiusura da parte del coordinatore) allarga il punto 2 della seconda precisazione del 2026-11-23, ed è scritta come [precisazione](#precisazione-del-2026-11-26-issue-aperte-senza-stato-691), con #691. La decisione dell'operatore del 2026-11-26 su #684 (il riposo dei ruoli, con la sentinella e il controllo giornaliero) precisa i punti 2 e 6 della seconda precisazione del 2026-11-23, ed è scritta come [precisazione](#precisazione-del-2026-11-26-il-riposo-dei-ruoli-684), con #684. Il resto del documento è il registro della decisione, scritto prima delle implementazioni: le frasi su "oggi" descrivono lo stato del 2026-11-16.
- **Fase:** design (#175).
- **Data:** 2026-11-16.

## Contesto

- L'operatore ha chiesto il 2026-11-16 (diario #76) di formalizzare chi fa cosa, in modo che i ruoli non dipendano dall'agent che li ricopre.
- Oggi il supervisore è Claude Code. L'operatore potrà promuoverlo a un livello superiore e affidare la supervisione a un altro agent.
- Oggi devloop non mergia: porta la PR in `agent:ready-to-merge` e il merge lo fa una persona. Le modalità di merge descritte qui sono la decisione da implementare in #172 e #182.

## Decisione

### Principi

1. **I ruoli sono assegnati ad attori.** Un attore è un agent con la propria identità (GitHub App) oppure l'operatore. L'assegnazione è nella configurazione (`[roles]` di devloop) o in questo ADR, non nel codice. Lo stesso attore può avere più ruoli, purché siano compatibili, vedi il punto 4.
2. **Tutto passa dal tracker.** Ogni decisione e azione di un ruolo lascia una traccia su GitHub (marker, commento, label o review) firmata con l'identità di chi la prende. Nessuna decisione resta solo in una chat o in un file locale.
3. **Le decisioni riservate restano all'operatore**, come da ADR 0001: rollout e attivazione su run reali, budget e round, credenziali e permessi, regole di fiducia, baseline del replay gate, modalità di merge, attivazione del ciclo continuo e del pin automatico, approvazione degli ADR.
4. **Separazione.**
   - L'implementer non fa review della propria PR.
   - Il supervisore non implementa e non fa review.
   - Il supervisore non decide escalation o trattenute sulle PR di cui è anche implementer: in quel caso decide l'operatore.
   - **Precisazione del 2026-11-22, su #524 e #527**, che sostituisce il punto precedente (vedi la [precisazione sul modello a quattro ruoli](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527)): il supervisore non decide escalation **che superino o sostituiscano un verdetto dei reviewer**. Le escalation di processo (`retry`, `rereview`, `grant-round` entro i tetti, `accept-defaults`, `decide`, `hold` e `release`, merge delegato) le decide anche sulle PR del proprio agent. Il merge contro un verdetto negativo resta solo dell'operatore.

### Ruoli

| Ruolo | Attore oggi | Fa | Non fa |
|---|---|---|---|
| **Operatore** | `brushtyler` | Prende le decisioni riservate e la direzione strategica: ADR, `AGENTS.md`, configurazione, `trusted_users` e chiavi, i tetti (`merge-budget`, budget), il merge contro un verdetto negativo dei reviewer; scrive la direzione del progetto nell'issue di `[direction]` (dalla [seconda precisazione del 2026-11-23](#seconda-precisazione-del-2026-11-23-modello-operativo-dei-ruoli-597)); risolve le escalation critiche (`agent:needs-operator`); approva gli ADR; assegna i ruoli; può sempre mergiare, bloccare o riprendere un'attività | — |
| **Coordinatore** (facoltativo, `[roles.coordinator]`) | l'agent di `[roles.coordinator]`, con l'app `coordinator` | Responsabile dell'operatività: decide come risolvere i problemi operativi critici, detta le priorità e l'ordine del lavoro, sblocca il processo quando si ferma; autorizza il lavoro (`/devloop authorize`) e traduce la direzione dell'operatore in priorità; verifica il lavoro del supervisore e, se il supervisore non risponde, mette in lavorazione direttamente; al controllo periodico esamina le issue aperte senza stato di autori fidati e chiude quelle finite o superate ([precisazione del 2026-11-26](#precisazione-del-2026-11-26-issue-aperte-senza-stato-691)); in assenza dell'operatore gestisce i finding aperti e le escalation per mancanza di reviewer indipendenti, vota al posto di un membro indisponibile del conclave e trattiene il merge delle PR con un passo sull'host; mergia le PR riservate che soddisfano tutte le condizioni, senza una decisione dell'operatore PR per PR e anche dopo un conclave unanime ([precisazione del 2026-11-24](#precisazione-del-2026-11-24-merge-riservato-del-coordinatore-624)); prepara le decisioni per l'operatore. Agisce con il perimetro chiuso di comandi delle precisazioni del [2026-11-22](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527) e del 2026-11-23 ([prima](#precisazione-del-2026-11-23-compiti-del-coordinatore-594) e [seconda](#seconda-precisazione-del-2026-11-23-modello-operativo-dei-ruoli-597)). Delega dell'operatore, revocabile | Le decisioni riservate; il lavoro che cambia i poteri di un ruolo o la politica di fiducia; approvare con meno reviewer del livello o sostituire un reviewer; il merge contro un verdetto negativo |
| **Supervisore** | Claude Code (`claude-agent`) | Sorveglia devloop (watch); analizza le RUN; apre issue per problemi e miglioramenti, e le segnala al coordinatore (`agent:needs-coordinator`); mette in lavorazione (`agent:ready`, priorità) solo le issue autorizzate di autori fidati, nell'ordine dettato dal coordinatore; scrive nel diario ogni fermo voluto della coda; in modalità `auto` trattiene o mette in sequenza i merge e decide le escalation non critiche; fa escalation all'operatore | Implementare, fare review, cambiare configurazione, policy, budget o credenziali; autorizzare da solo le issue che apre; mergiare: lo fa il dispatcher |
| **Dispatcher** | devloop (`dispatcher`) | Esegue le fasi: preflight, implement, review, fix, sync, round sulla CI, promozione, merge (modalità `strict` o `auto`), pin (se attivo), ciclo continuo (se attivo); scrive i marker; fa escalation | Le decisioni riservate; mergiare con il merge disattivato |
| **Preflight** | Codex (`codex-agent`) | Controlla la prontezza di un'issue in sola lettura | Scrivere su GitHub: lo fa il dispatcher |
| **Implementer** | Claude (`claude-agent`) | Implementa e corregge nel worktree | Pubblicare, pushare, mergiare |
| **Reviewer** | Codex (livello 1); Codex + agy (livello 2) | Review strutturata con verdetto | Correggere |

### Priorità del lavoro

- **L'operatore fissa la strategia:** ADR, fasce di priorità (P0, P1, P2), cosa è rimandabile. Può sempre cambiare l'ordine.
- **Il supervisore decide l'ordine operativo dentro quella strategia.** Mette in coda le issue (`agent:ready`) e assegna `priority:*` e `prerequisito`. Alza la priorità di ciò che sblocca o accelera il lavoro, e toglie dalla coda ciò che può aspettare. Ogni riordino è annotato nel diario.
- **Il dispatcher esegue in quell'ordine** (#146).
- **Label del supervisore.** Il dispatcher accetta queste label anche dall'identità del ruolo `supervisor`, ma solo su issue aperte da `trusted_users` o dalle app di devloop. Per le issue di altri autori resta valida solo la label messa dall'operatore. L'implementazione è in #178.

### Modalità di merge e autonomia del supervisore

Si configurano con `[merge].mode`, con un override facoltativo per livello in `[merge.levels]`. Vince la modalità più restrittiva. I dettagli tecnici sono in #172 e #182.

| Modalità | Chi decide | Comportamento |
|---|---|---|
| `off` (default) | Operatore | Spento: devloop porta la PR in `agent:ready-to-merge` e si ferma. Mergia una persona. Ogni escalation va all'operatore |
| `strict` | Direttive di merge | devloop mergia **solo** quando le direttive di merge sono soddisfatte. Ogni escalation va all'operatore |
| `auto` | Direttive + supervisore | Come `strict`. In più il supervisore può trattenere un merge (`/devloop hold`, `/devloop release`) e **decide da solo le escalation non critiche** (`agent:needs-supervisor`). Quelle critiche vanno all'operatore |

**Direttive di merge.**
- Approvazione di ogni reviewer richiesto dal livello.
- Nessun finding aperto.
- Head uguale al commit approvato.
- Ricevuta di policy coincidente.
- CI superata.
- PR mergeable senza conflitti.
- Nessuna label `agent:needs-operator` o `agent:merge-hold`.

**Nessuna modalità, e nessun agent, mergia con CI non verde, con finding bloccanti aperti o senza le approvazioni richieste.**

### Critico e non critico

**Critico: sempre all'operatore, anche in `auto`.**
- Le decisioni riservate dell'ADR 0001, compresi round e budget oltre i limiti.
- Le PR che toccano `docs/adr/`, `AGENTS.md` o `CLAUDE.md` (`operator_only_paths`).
- Le decisioni `operator_decision` del preflight.
- Contenuti di autori non fidati, o testo modificato dopo `agent:ready`.
- Ogni caso non classificato.

**Non critico: in `auto` decide il supervisore, con traccia su GitHub.**
- Ritentare un passo fallito per un problema di strumento (agent caduto, modello non disponibile, permesso negato), senza contare un round e con al massimo 2 tentativi.
- Accettare i default tecnici del preflight (`default_available`) sulle issue di autori fidati.
- Trattenere o mettere in sequenza i merge.
- Richiedere una nuova review.
- Parcheggiare il lavoro P2 secondo la strategia dell'operatore.

### Precisazione del 2026-11-22: modello a quattro ruoli (#524, #527)

Decisione dell'operatore presa dopo l'approvazione di questo ADR, con il [`/devloop approve` su #524 del 2026-11-22](https://github.com/brushtyler/ssdlc/issues/524#issuecomment-7082340609), e registrata qui con il [`/devloop approve` su #527](https://github.com/brushtyler/ssdlc/issues/527#issuecomment-7082800200). Il testo approvato è il corpo di #524 di quel momento, riportato nel [commento su #527](https://github.com/brushtyler/ssdlc/issues/527#issuecomment-7083066148). Precisa il principio 4 e la tabella dei ruoli; il resto dell'ADR non cambia. La stessa precisazione è registrata nell'[ADR 0005](0005-deleghe-e-ruoli-degli-agent.md#4-decisioni-riservate-alloperatore), sotto D2.

**Motivo.** Dal 2026-11-20 l'operatore delega a un agent, come root e con il proprio login `gh`, la gestione quotidiana. Così uno stesso attore legge testo non controllato (issue, PR, output degli agent, log), ha i privilegi di root sull'host e firma con l'identità personale dell'operatore: un testo manipolato in una PR potrebbe arrivare fino a root. Il modello separa i privilegi dall'esposizione a testo non controllato.

**I quattro ruoli, con privilegi e input.**

| Ruolo | Chi | Privilegi | Legge | Fa |
|---|---|---|---|---|
| **Operatore** | la persona | tutti | — | direzione strategica e decisioni di persona (`/devloop approve`, decisioni riservate di questo ADR, ADR, `AGENTS.md`, configurazione, `trusted_users` e chiavi, i tetti (`merge-budget` e budget), il merge contro un verdetto negativo dei reviewer, credenziali, login delle CLI, GitHub App, abbonamenti); lancia l'agent dell'host; può sempre mergiare, bloccare o riprendere |
| **Coordinatore** | agent, periodico (a intervalli e sugli allarmi), non sempre acceso | la sua GitHub App; sull'host solo **lettura** (sudoers con comandi di lettura per log, stato, versioni e permessi) | i riepiloghi di devloop (`report`, #316), il ledger, i record del dispatcher, le azioni del supervisore, gli allarmi, le metriche (quote, round, fallimenti); la direzione dell'operatore (`[direction] issue`) e la casella del suo ruolo | responsabile dell'operatività (dalla [precisazione del 2026-11-23](#precisazione-del-2026-11-23-compiti-del-coordinatore-594)): decide come risolvere i problemi operativi critici, detta le priorità e l'ordine del lavoro, sblocca il processo quando si ferma; autorizza il lavoro e, in assenza dell'operatore, decide quanto previsto dalla [seconda precisazione del 2026-11-23](#seconda-precisazione-del-2026-11-23-modello-operativo-dei-ruoli-597); verifica che il supervisore faccia quanto previsto; individua i problemi di sistema che rallentano il supervisore o devloop e li scrive come issue; mergia le PR riservate che la delega del merge (#509) non copre, dopo il quorum, senza una decisione dell'operatore PR per PR e anche dopo un conclave unanime (dalla [precisazione del 2026-11-24](#precisazione-del-2026-11-24-merge-riservato-del-coordinatore-624)); prepara all'operatore le decisioni e un riepilogo periodico; chiede all'operatore un intervento dell'agent dell'host, con il riferimento alla procedura |
| **Supervisore** | agent, continuo | la sua GitHub App | issue e PR di autori fidati, escalation, log dei turni | messa in lavorazione e ordine della coda delle sole issue autorizzate, seguendo le priorità del coordinatore; segnala al coordinatore le issue che apre (`agent:needs-coordinator`); escalation non critiche; analisi delle RUN e issue sui problemi che vede; merge delegati (#509) |
| **Agent dell'host** | agent **occasionale**, lanciato dall'operatore, **fuori dal ciclo** di devloop, mai automatico | `sudo` sull'host; **nessuna** scrittura su GitHub | **solo** procedure e script già su `main` (`ops/host/`, `ops/host/migrate.md`, `docs/devloop/quickstart.md`) e l'istruzione dell'operatore, **mai** testo di issue, PR o output degli agent | installazione di devloop per un progetto, avvio dei servizi, aggiornamenti e migrazioni (pin dopo un passo sull'host, nuove versioni delle CLI). Riporta l'esito all'operatore, e lo registra l'operatore o il coordinatore |

Durante il ciclo di sviluppo di un progetto (issue → PR → review → merge) l'agent dell'host non serve: interviene solo all'avvio e negli aggiornamenti o migrazioni dell'installazione. Il dispatcher, il preflight, l'implementer e i reviewer restano come nella tabella dei ruoli sopra.

**Principio 4.** Diventa: "il supervisore non decide escalation **che superino o sostituiscano un verdetto dei reviewer**". Oggi non ce n'è nessuna. Le escalation di processo (`retry`, `rereview`, `grant-round` entro i tetti, `accept-defaults`, `decide`, `hold` e `release`, merge delegato) il supervisore le decide anche sulle PR del proprio agent: il loro rischio è quota sprecata, non codice peggiore. Il merge contro un verdetto negativo resta solo dell'operatore. I tetti delle deleghe ([ADR 0005](0005-deleghe-e-ruoli-degli-agent.md), D4) restano.

**Nessun vincolo fra gli agent dei ruoli.** Lo stesso agent può ricoprire più ruoli. È raccomandato un modello del coordinatore diverso da quello del supervisore, quando la quota lo permette.

**Perimetro chiuso dei comandi del coordinatore** (punto 3 di #524), con `[roles.coordinator]` nell'overlay e la sua app `[apps.coordinator]`, creata dall'operatore, spento per default:
- **delegati al coordinatore**, che valgono come quelli di un utente fidato: `retry`, `rereview`, `recheck`, `fix`, `hold`, `release`, `grant-round` (entro i tetti delle deleghe), `accept-defaults`, `decide` (solo per lacune che non riguardano la policy di fiducia, le credenziali o il budget), la ripresa con le label (togliere `agent:needs-operator` e mettere la label del passo) e `merge` (punto seguente);
- **riservati all'operatore di persona**, mai del coordinatore: `approve`, `merge-budget`, `grant-budget`, `capacity`, `reconcile`, ogni modifica di `trusted_users` e delle chiavi di fiducia;
- un comando che non è in nessuno dei due elenchi è rifiutato per il coordinatore (fallisce chiuso).

**Merge riservato del coordinatore** (punto 3b di #524), attraverso il dispatcher e mai con contenuti scrivibili dalla sua app: `/devloop merge <sha> <motivo>` dall'identità del coordinatore, eseguito dall'app merger con il record e la ricevuta di un merge automatico più il comando e il motivo. Il dispatcher verifica sullo `sha`:
- le condizioni di #509: head attuale e approvato, quorum approvato, nessun finding aperto, CI verde, review consultiva `ok` o `dubbi`, `MERGEABLE` e `CLEAN`;
- **senza** le esclusioni di #509 per i percorsi della regola di fiducia, `docs/adr/` e il passo sull'host: sono proprio i casi del coordinatore. Una PR con un passo sull'host si mergia, e il pin aspetta l'agent dell'host;
- quando un reviewer del quorum ha dichiarato toccata la policy di fiducia, o il diff tocca `docs/adr/`, la issue che la PR chiude deve avere un `/devloop approve` valido dell'operatore (`issue_decisions`), e ogni reviewer del quorum deve avere `operator_reason` `decision` o `none`, mai `trust_policy`; altrimenti il comando è rifiutato e la PR resta all'operatore;
- al massimo 10 merge del coordinatore per giorno UTC, una costante del codice che l'overlay può solo abbassare; il riepilogo di #316 li elenca.

**Sudoers in sola lettura** (punto 4 di #524): un utente di sistema `coordinator` con un sudoers (`ops/host/sudoers-coordinator`) che ammette solo `journalctl` e `systemctl status` delle unità di devloop, la lettura di `run.log`, `devloop-pinned status` e `check-host.sh --read-only`, una modalità nuova di `check-host.sh` senza prove delle CLI (nessun modello avviato, nessuna quota spesa, nessuna scrittura nelle sessioni o nelle cache), senza scritture, con sole righe di stato. Niente che modifichi. L'agent dell'host esegue `check-host.sh` completo, ma delle prove delle CLI riporta solo l'esito (passato o fallito, con il codice di uscita), mai il testo prodotto dal modello.

**Passi sull'host e agent dell'host** (punti 5 e 7 di #524): il `host_step` di una PR nomina una sezione di `ops/host/migrate.md` o uno script di `ops/host/` della PR stessa, non comandi liberi, e il reviewer lo verifica. L'agent dell'host esegue solo quella sezione, dalla versione su `main`. Finché il coordinatore non ha la sua app e il suo utente, l'agent che oggi fa il coordinatore come root esegue passi sull'host solo da procedure su `main`, mai da testo di PR.

**Attuazione.** Questa precisazione e la rimozione, in devloop, del controllo "il supervisore ha implementato questa PR" per le escalation di processo sono in #527 (devloop/dispatch_supervision.py, [guida](../devloop/guide.md#escalations-to-the-supervisor)): un'escalation non critica su una PR dell'agent del supervisore va al supervisore come ogni altra, e `[delegations] retry_own_prs` non ha più effetto. Un verdetto dei reviewer che chiede l'operatore resta una classe critica, che non arriva mai al supervisore, e la sua `rereview` resta rifiutata quando ogni reviewer del quorum ha un verdetto. La concessione di budget (`grant-budget`), che l'elenco delle escalation di processo non nomina, resta all'operatore sulle PR dell'agent del supervisore. Il ruolo del coordinatore, il suo perimetro e il suo merge, il sudoers, `check-host.sh --read-only`, la regola dei passi sull'host, `start-role.sh` e i runbook dei ruoli sono le parti successive del piano di #524, che citano questa precisazione.

### Precisazione del 2026-11-23: compiti del coordinatore (#594)

Decisione dell'operatore presa dopo l'approvazione di questo ADR e registrata qui con il [`/devloop approve` su #594 del 2026-11-23](https://github.com/brushtyler/devloop/issues/594#issuecomment-7117069104). Il testo approvato è il corpo di #594 di quel momento. Precisa i compiti del coordinatore, la tabella dei ruoli, la tabella dei quattro ruoli, la sezione [Priorità del lavoro](#priorità-del-lavoro) e il perimetro dei comandi della [precisazione del 2026-11-22](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527); il resto dell'ADR non cambia.

**Motivo.** Il coordinatore è assegnato e attivo, e la tabella dei ruoli e la precisazione del 2026-11-22 ne descrivevano i compiti in modo diverso. L'operatore ha deciso come ripartirli.

**Le decisioni.**

1. **Il coordinatore è responsabile dell'operatività.** Decide come risolvere i problemi operativi critici, detta le priorità e l'ordine del lavoro, sblocca il processo quando si ferma. Autorizza da solo il lavoro operativo, tranne quello che cambia i poteri di un ruolo o la politica di fiducia, che resta dell'operatore.
2. **Il supervisore mette in lavorazione** seguendo le priorità del coordinatore. Nella sezione [Priorità del lavoro](#priorità-del-lavoro), l'ordine operativo che il supervisore decide segue quindi le priorità dettate dal coordinatore.
3. **Perimetro del coordinatore.** Il perimetro chiuso dei comandi della precisazione del 2026-11-22 si estende a `capacity` e `grant-budget`, entro i tetti delle deleghe, e alle etichette di priorità (#595). I due comandi passano dall'elenco dei riservati all'operatore di persona a quello dei delegati al coordinatore; gli altri riservati (`approve`, `merge-budget`, `reconcile`, ogni modifica di `trusted_users` e delle chiavi di fiducia) restano dell'operatore.
4. **L'operatore si occupa della direzione strategica**: ADR, `AGENTS.md`, configurazione, `trusted_users` e chiavi, i tetti (`merge-budget`, budget), il merge contro un verdetto negativo dei reviewer.

**Fuori da questa precisazione.** Le parti del modello ancora in discussione (conclave con voto sostitutivo, risveglio del supervisore, passi sull'host in assenza dell'operatore) non sono decise qui: le decide la [seconda precisazione del 2026-11-23](#seconda-precisazione-del-2026-11-23-modello-operativo-dei-ruoli-597).

**Attuazione.** Questa precisazione non cambia il codice. Finché l'estensione del perimetro non è implementata in devloop, il dispatcher continua a rifiutare `capacity` e `grant-budget` dall'identità del coordinatore come riservati all'operatore (`COORDINATOR_RESERVED` in `devloop/supervision.py`); le etichette di priorità del coordinatore sono in #595.

**Attuazione del punto 3 (#595).** L'estensione del perimetro è implementata con #595, approvata dall'operatore con il [`/devloop approve` su #595](https://github.com/brushtyler/devloop/issues/595#issuecomment-7118306639). `capacity` e `grant-budget` passano in `COORDINATOR_DELEGATED`; `COORDINATOR_RESERVED` resta `approve`, `merge-budget` e `reconcile`, con `trusted_users` e le chiavi di fiducia. `capacity` vale con gli stessi tetti della delega del supervisore, conta in `max_grants_per_day` e chiede la modalità `auto`, ma per il coordinatore la accende `[roles.coordinator]`, non `[delegations] capacity`. `grant-budget` concede l'unica estensione della storia, alle condizioni e con i tetti di #411, sulle escalation `budget_grantable` e sulle escalation `budget` all'operatore; una seconda estensione resta dell'operatore. Le etichette di priorità del coordinatore contano dove conta il suo `agent:ready`; quelle di dipendenza e di implementer no. L'app del coordinatore è fra le app di devloop (`_devloop_logins`), quindi le sue issue le mettono in coda il supervisore e il coordinatore; un'issue del coordinatore che il controllo di prontezza dichiara toccare la policy di fiducia (`trust_policy` diverso da `none`) va in `agent:needs-operator` finché l'`agent:ready` non è di un utente di `trusted_users`, e senza controllo di prontezza sulle sue issue contano solo le etichette degli utenti fidati. Guida: [The coordinator](../devloop/guide.md#the-coordinator).

### Seconda precisazione del 2026-11-23: modello operativo dei ruoli (#597)

Decisione dell'operatore presa dopo l'approvazione di questo ADR e registrata qui con il [`/devloop approve` su #597](https://github.com/brushtyler/devloop/issues/597#issuecomment-7118311348) (ripetuto dopo il [primo](https://github.com/brushtyler/devloop/issues/597#issuecomment-7117832752)). Il testo approvato è il corpo di #597 di quel momento. Precisa come lavorano i ruoli quando l'operatore è assente, la tabella dei ruoli e la tabella dei quattro ruoli; il resto dell'ADR, comprese le decisioni riservate e i tetti, non cambia.

**Motivo.** L'operatore deve potersi assentare con la certezza che l'operatività continui. La sua presenza serve per la direzione strategica (ADR, `AGENTS.md`) e per i tetti, come stabilito nella [prima precisazione del 2026-11-23](#precisazione-del-2026-11-23-compiti-del-coordinatore-594).

**Le decisioni.**

1. **Autorizzazione e messa in lavorazione.**
   - Il coordinatore autorizza il lavoro con `/devloop authorize [priority:<p>]` sull'issue. Il supervisore mette in lavorazione (`agent:ready`) solo issue autorizzate, nell'ordine dettato dal coordinatore.
   - Le issue aperte dal supervisore partono solo con l'autorizzazione del coordinatore: chi propone un lavoro non lo autorizza da solo. Il supervisore le segnala con la label `agent:needs-coordinator`, che sveglia il coordinatore come oggi lo sveglia `agent:needs-operator`. Il coordinatore autorizza, toglie la label, e il supervisore mette in lavorazione. Un'issue del supervisore senza label e senza autorizzazione, trovata al controllo periodico, viene segnalata al supervisore.
   - Restano all'operatore le issue che cambiano i poteri di un ruolo o la politica di fiducia (`trust_policy` diverso da `none` nel controllo di prontezza), come in #595.
2. **Lavoro fermo.**
   - Al controllo periodico (ogni ora), se il coordinatore trova issue autorizzate pronte, con slot liberi, che restano ferme, chiede al supervisore nella casella il perché.
   - Una ragione valida del supervisore si rispetta. Se contrasta con le priorità, decide il coordinatore e lo scrive nel diario.
   - Se il messaggio non è letto entro 15 minuti (resta fuori da `done/`), o il supervisore risulta fermo (punto 6), il coordinatore mette in lavorazione direttamente, avvisa l'operatore, cerca la causa e la corregge. Può aprire e autorizzare le issue che servono.
   - Il supervisore scrive nel diario, subito, ogni volta che ferma volutamente la coda.
   - Il controllo vale anche per le issue aperte di autori fidati senza alcuno stato: lo allarga la [precisazione del 2026-11-26](#precisazione-del-2026-11-26-issue-aperte-senza-stato-691).
   - Mentre il coordinatore è a riposo il controllo è giornaliero, a un'ora fissa: lo precisa la [precisazione del 2026-11-26 sul riposo dei ruoli](#precisazione-del-2026-11-26-il-riposo-dei-ruoli-684).
3. **Decisioni dei reviewer in assenza dell'operatore.**
   - Conclave attivo (`conclave` nella configurazione, a cura dell'operatore). Se un membro è indisponibile, il terzo voto è del coordinatore, registrato come voto sostitutivo con il suo modello. Il perimetro riservato del conclave resta com'è.
   - I finding aperti che oggi chiude solo l'operatore (`open_findings`) li gestisce il coordinatore, con il motivo nel diario. Restano all'operatore quelli sulla politica di fiducia.
   - Le escalation per mancanza di reviewer indipendenti (`review_level`) le gestisce il coordinatore. Quando i reviewer indipendenti tornano disponibili, riprende la review con le label (togliere `agent:needs-operator`, mettere `agent:review`): è già fra i suoi comandi delegati. Non approva mai con meno reviewer del livello e non sostituisce un reviewer. Un'indisponibilità lunga va nel riepilogo per l'operatore.
   - Il merge contro un verdetto negativo resta solo dell'operatore.
4. **Slot parcheggiati.**
   - Il coordinatore evita di riempire `max_parked_prs`. In assenza dell'operatore non mette in coda le issue che prevedibilmente finiranno a lui (`docs/adr/`, `AGENTS.md`, politica di fiducia, passi sull'host).
   - Quando gli slot parcheggiati arrivano a limite − 1, alza a `high` il lavoro indipendente.
   - Se serve, alza `max_parked_prs` con `/devloop capacity`, entro i tetti (#595).
5. **Passi sull'host in assenza dell'operatore.**
   - Le issue con un passo sull'host prevedibile non vanno in coda.
   - Le altre partono, e il coordinatore trattiene il merge (`/devloop hold`) fino al ritorno dell'operatore.
   - Nessun agent dell'host automatico: dare root a un agent senza una persona è una decisione di direzione, da un ADR a parte se mai servirà.
6. **Risveglio e controllo dei ruoli.**
   - Uno script di risveglio unico per i due ruoli (`role-wait.sh --role supervisor|coordinator`, generalizzazione di `coordinator-wait.sh`). Sveglia sulla casella del ruolo, sugli eventi di devloop (tutti per il supervisore, filtrati per il coordinatore), sul servizio fermo e alla fine del periodo (1 ora per entrambi). Usa solo i comandi già nei sudoers dei ruoli.
   - A ogni avvio lo script scrive il battito del ruolo nella casella fra i ruoli (`COORDINATION_DIR`).
   - Un timer systemd (`role-watchdog`), indipendente dagli agent, controlla i battiti:
     - supervisore fermo da più di 2 periodi → messaggio al coordinatore;
     - coordinatore fermo → messaggio al supervisore e avviso all'operatore;
     - tutti e due fermi → avviso all'operatore.

     Il timer rileva e non agisce: non ha chiavi GitHub. Gli avvisi all'operatore passano da `devloop notify` (#593). Finché la #593 non c'è, il timer scrive solo nelle caselle.
   - Il riposo dichiarato di un ruolo, la sentinella che lo sveglia e il watchdog a tre stati sono nella [precisazione del 2026-11-26 sul riposo dei ruoli](#precisazione-del-2026-11-26-il-riposo-dei-ruoli-684).
7. **Direzione strategica per progetto.**
   - `[direction] issue` nell'overlay, come `[report] issue`: l'issue in cui l'operatore scrive la direzione (temi in ordine, temi in pausa, vincoli). Contano solo i commenti di `trusted_users`, e l'ultimo vale.
   - Il coordinatore la traduce in priorità:
     - `high`: ciò che sblocca il primo tema, e i problemi operativi critici;
     - `normal`: il lavoro dei temi attivi;
     - `low`: il resto utile;
     - non in coda: i temi in pausa e le proposte fuori tema, segnalate all'operatore nel riepilogo.
   - Un'etichetta di priorità messa dall'operatore su un'issue vince.
   - La direzione non entra nei prompt di implementer e reviewer. Senza `[direction]` le priorità restano alle etichette dell'operatore.

**Piano delle issue di attuazione**, da aprire dopo il merge di questa precisazione, citandone questa sezione su `main`:

1. `/devloop authorize`, label `agent:needs-coordinator` e il suo risveglio, autorizzazione obbligatoria sulle issue del supervisore (codice, guida, runbook).
2. Voto sostitutivo del coordinatore nel conclave (codice, guida).
3. `open_findings` fra i comandi del coordinatore (codice, guida).
4. `role-wait.sh`, battiti e timer `role-watchdog` (`ops/`, `check-host.sh`, `start-role.sh`, runbook). Gli avvisi all'operatore dipendono da #593.
5. `[direction] issue` (codice, guida, guida rapida, runbook).
6. Runbook di coordinatore e supervisore per i punti 2, 3 (`review_level`), 4 e 5 (solo documenti).

L'issue 4 è indipendente dalle altre. La 1 viene dopo #595, che tocca lo stesso codice.

**Attuazione.** Questa precisazione non cambia il codice. Finché le issue del piano non sono implementate, devloop si comporta come prima: il dispatcher non conosce `/devloop authorize` né `agent:needs-coordinator`, rifiuta dall'identità del coordinatore i comandi che non sono nel suo perimetro chiuso, e le escalation `open_findings` e `review_level` restano all'operatore.

**Attuazione del punto 3 per `open_findings` (#606).** Implementata con #606, approvata dall'operatore con il [`/devloop approve` su #606](https://github.com/brushtyler/devloop/issues/606#issuecomment-7119124203). `finding` entra in `COORDINATOR_DELEGATED`: con `[roles.coordinator]` il coordinatore chiude i finding di un'escalation `open_findings` con la forma della chiusura dell'operatore, `/devloop finding <round/reviewer/ID> resolved|withdrawn <motivo>`, come prima riga di un commento sulla PR (una riga per finding). Il motivo è obbligatorio e finisce nel record `finding` della PR (`by: coordinator`) e nel riepilogo giornaliero. Il comando è rifiutato per intero se un reviewer dell'ultimo round non approva l'head (il coordinatore chiude un finding, non scavalca un verdetto), per un finding che una review `trust_policy` apre o tiene aperto (resta dell'operatore), e da qualunque altra identità. Quando non resta nessun finding aperto, la PR torna in `agent:review` e l'approvazione registrata viene riapplicata. Guida: [Closing findings as the coordinator](../devloop/guide.md#closing-findings-as-the-coordinator).

### Precisazione del 2026-11-24: merge riservato del coordinatore (#624)

Decisione dell'operatore presa dopo l'approvazione di questo ADR e registrata qui con il [`/devloop approve` su #624 del 2026-11-24](https://github.com/brushtyler/devloop/issues/624#issuecomment-7131560809). Il testo approvato è il corpo di #624 di quel momento. Precisa il **merge riservato del coordinatore** della [precisazione del 2026-11-22](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527) e la riga del coordinatore nella tabella dei quattro ruoli; il resto dell'ADR, compreso il merge contro un verdetto negativo dei reviewer, che resta solo dell'operatore, non cambia.

**Motivo.** Con `[merge] mode = "auto"` devloop mergia da solo le PR ordinarie, e all'operatore restavano tutte le PR riservate, anche senza problemi aperti: il merge riservato del coordinatore era ammesso solo con la decisione dell'operatore PR per PR, e mai dopo un conclave.

**Le decisioni.**

1. **Nessuna decisione dell'operatore PR per PR.** Il coordinatore scrive `/devloop merge <sha> <motivo>` quando il dispatcher può verificare sullo `sha` tutte le condizioni del merge riservato: head approvato, quorum completo del livello, nessun finding aperto, CI `pass`, mappa di accettazione valida, review consultiva `ok` o `dubbi`, `MERGEABLE` e `CLEAN`, nessun `/devloop hold`, tetto giornaliero. Restano le condizioni che il codice già impone: un `/devloop approve` valido dell'operatore sull'issue quando un reviewer ha dichiarato toccata la policy di fiducia o il diff tocca `docs/adr/` (la decisione dell'operatore resta quella data prima dell'implementazione, non al merge), e nessun reviewer del quorum con `operator_reason` `trust_policy` sull'ultima review dello `sha`.
2. **Merge del coordinatore dopo un conclave.** Dopo la ripartenza del lavoro per una decisione del conclave (#142) devloop continua a non mergiare la PR da solo, in `strict` come in `auto`. È ammesso il solo merge riservato del coordinatore, quando:
   - la decisione era `UNANIMOUS` e resta valida: non annullata con `/devloop veto` e non decaduta (la richiesta dell'issue è ancora quella su cui è stata presa);
   - dopo la ripartenza un round di review completo sull'head da mergiare l'ha approvato con il quorum del livello;
   - valgono tutte le condizioni del punto 1;
   - il coordinatore ha verificato che la risoluzione del conclave resta dentro la richiesta dell'issue autorizzata, con le decisioni dell'operatore su di essa, e lo scrive nel motivo del comando. Se ne esce, o il dubbio resta, non mergia e porta la PR all'operatore.
3. **Merge d'ufficio.** Al controllo periodico il coordinatore mergia le PR riservate pronte che soddisfano tutte le condizioni. L'operatore può fermarne una con `/devloop hold`.

**Attuazione.** Con #624 il dispatcher accetta il comando del coordinatore su una PR il cui lavoro è ripartito dopo un conclave alle condizioni del punto 2 verificabili dal codice (`_conclave_merge_problem` in `devloop/dispatch_conclave.py`); il merge automatico di devloop e il merge delegato del supervisore restano esclusi dopo un conclave. La verifica della direzione resta del coordinatore. Guida: [The reserved merge of the coordinator](../devloop/guide.md#the-reserved-merge-of-the-coordinator).

### Precisazione del 2026-11-24: ruoli per progetto (#629)

Decisione dell'operatore del 2026-11-24, presa durante la prova di un progetto di prova (#515) sullo stesso host del repository di devloop, e registrata qui con il [`/devloop approve` su #629](https://github.com/brushtyler/devloop/issues/629#issuecomment-7138340811). Il testo approvato è il corpo di #629 di quel momento, con la scelta (b) per R1. Precisa la tabella dei quattro ruoli della [precisazione del 2026-11-22](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527) quando un host serve più progetti ([ADR 0006, sezione 3.8](0006-devloop-su-altri-repository.md#38-più-progetti-sullo-stesso-host)); il resto dell'ADR non cambia.

**Le decisioni.**

1. **Supervisore e coordinatore sono per progetto.** Ciascuno serve un solo progetto, quello del suo repository. Il coordinatore del repository di devloop è il coordinatore di quel progetto, non di tutti i progetti dell'host; lo stesso vale per il supervisore.
2. **Nessun canale fra i ruoli di progetti diversi**, nemmeno sullo stesso host. I progetti di terzi girano su altri host e non hanno nessun collegamento con i ruoli di devloop; di norma non ce l'hanno nemmeno i progetti dello stesso proprietario su host diversi. La separazione è un valore: un coordinatore non riceve interruzioni né informazioni sbagliate o fuorvianti da altri progetti.
3. **Un progetto usa un altro progetto come farebbe chiunque**: apre un'issue sul suo repository per chiedere una funzionalità o segnalare un difetto, e ne segue i rilasci, invece di essere avvisato. Un difetto di devloop visto dai ruoli di un progetto servito da devloop non va ai ruoli del repository di devloop: il ruolo lo riporta all'operatore, nel suo riepilogo, con i link pubblici che lo mostrano, e l'operatore decide se aprire un'issue sul repository di devloop. L'agent dell'host scrive una nota nella casella di un altro progetto solo su richiesta esplicita dell'operatore.
4. **Lo sviluppo a cavallo di più repository** (per esempio una libreria e il progetto che la usa) è fuori perimetro. Se servirà, sarà un livello in più sopra i progetti, che fa il drill-down sui progetti e apre issue nei loro repository.
5. **Un utente di sistema per ruolo e per progetto** (R1, scelta (b)). Con più progetti sullo stesso host, supervisore e coordinatore di ciascun progetto hanno il loro utente, con i propri login delle CLI (quindi una quota contata a parte), il proprio token di `gh` in sola lettura, la propria `key_dir` e una casella con un gruppo del progetto: la sessione di un progetto non legge la casella né la chiave dell'app di ruolo di un altro, e non ne conia i token. La separazione è del sistema, non solo del contesto della sessione.

**Attuazione.** Con #629: le sessioni dei ruoli si chiamano `devloop-<ruolo>-<progetto>` (`ops/host/start-role.sh`), e il loro primo messaggio dice che il ruolo serve solo quel progetto; con `PROJECT` gli utenti dei ruoli hanno il default `supervisor-<progetto>` e `coordinator-<progetto>`, e il gruppo della casella `devloop-roles-<progetto>`; gli script di `ops/host/` rifiutano un utente di ruolo che un'altra installazione dell'host ha già, e `check-host.sh` un utente estraneo nel gruppo della casella. Un host con i ruoli condivisi li separa con [ops/host/migrate.md, "Utenti dei ruoli per progetto"](../../ops/host/migrate.md#utenti-dei-ruoli-per-progetto-629). Le regole del punto 3 sono nei runbook del [supervisore](../devloop/roles/supervisor.md), del [coordinatore](../devloop/roles/coordinator.md) e dell'[agent dell'host](../devloop/roles/host.md); i parametri in [ops/README.md](../../ops/README.md#più-progetti-sullo-stesso-host).

### Precisazione del 2026-11-24: utenti dei ruoli per insieme di app (#643)

Decisione dell'operatore del 2026-11-24, presa durante la migrazione di #629 e l'aggiunta del coordinatore al progetto di prova di #515, e registrata qui con le [decisioni su #643](https://github.com/brushtyler/devloop/issues/643#issuecomment-7147682282) e il [`/devloop approve` su #643](https://github.com/brushtyler/devloop/issues/643#issuecomment-7147818209). Il testo approvato è il corpo di #643 di quel momento, con le scelte S1 e S2. Precisa il punto 5 della [precisazione del 2026-11-24 sui ruoli per progetto](#precisazione-del-2026-11-24-ruoli-per-progetto-629); i punti da 1 a 4 di quella precisazione e il resto dell'ADR non cambiano.

**Motivo.** Con un insieme di app condiviso fra repository dello stesso proprietario ([ADR 0006, precisazione di D3](0006-devloop-su-altri-repository.md)), la chiave dell'app supervisore e quella dell'app coordinatore valgono per tutti i repository dell'insieme: un supervisore che legge la chiave in un progetto conia già token per un altro progetto dello stesso insieme, con o senza utenti di sistema distinti. Dentro un insieme, utenti per progetto proteggevano solo il sudoers del servizio dell'altro progetto, la lettura del suo overlay e della sua casella e le impostazioni della CLI: allo stesso livello di fiducia, niente che contasse più della chiave condivisa, a fronte di un login e di un token per ruolo e per progetto.

**Le decisioni.**

1. **Un utente di sistema per ruolo e per insieme di app** (S1), non per progetto. Supervisore e coordinatore restano due utenti distinti fra loro: il confine fra i due ruoli serve (`/devloop authorize`, `/devloop merge`, #607).
2. **Condivisione solo fra progetti dello stesso insieme.** Gli script di `ops/host/` la verificano con lo stesso `app_id` in `[apps.supervisor]`, o in `[apps.coordinator]` per il coordinatore, negli overlay dei progetti; altrimenti si fermano.
3. **Per progetto restano** la sessione (`devloop-<ruolo>-<progetto>`), la casella con il suo gruppo, lo `state_dir`, il clone del repository gestito, i sudoers e la copia di root del coordinatore. **Per insieme diventano** l'utente, con un nome di default derivato dall'insieme (`<ruolo>-<APP_SET>`, da `APP_PREFIX`), la `key_dir`, con una sola copia della chiave, e il login della CLI.
4. **Il repository di devloop**, che ha un insieme suo, tiene utenti suoi, come prima.
5. **Il limite, dichiarato dove si sceglie l'insieme condiviso**: l'utente di ruolo appartiene ai gruppi delle caselle di tutti i progetti dell'insieme. Che una sessione non legga la casella di un altro progetto dell'insieme resta una regola del runbook, non un vincolo del sistema. Fra insiemi diversi la separazione del punto 5 della precisazione di #629 resta del sistema.
6. **Letture del supervisore con la sua app** (S2): come il coordinatore, il supervisore legge GitHub con un token della sua app, e non ha più un token personale di `gh`. L'app supervisore ha in più `actions`, `checks` e `statuses` in sola lettura, che `scripts/supervisor-token.py` concede solo con un `--perm` esplicito. Ogni lettura conia un token dalla chiave dell'app, che diventa l'unica credenziale del supervisore.

**Attuazione.** Con #643: in `ops/lib.sh`, con `PROJECT`, gli utenti dei ruoli hanno il default `supervisor-<APP_SET>` e `coordinator-<APP_SET>` (`APP_SET` per default è `APP_PREFIX` senza il `-` finale) e le `key_dir` `~/.ghapps` di quegli utenti; `ops_check_host_users` ammette un utente di ruolo di un'altra installazione dell'host solo nello stesso ruolo e con lo stesso `app_id`. Il manifest `ops/github/apps/supervisor.json` e `scripts/supervisor-token.py` hanno i permessi di lettura della CI; `ops/github/tokens.md` non ha più il token personale. I parametri e il limite in [ops/README.md](../../ops/README.md#più-progetti-sullo-stesso-host); la migrazione in [ops/host/migrate.md, "Utenti dei ruoli per insieme di app"](../../ops/host/migrate.md#utenti-dei-ruoli-per-insieme-di-app-643) e ["Letture del supervisore con la sua app"](../../ops/host/migrate.md#letture-del-supervisore-con-la-sua-app-643); le letture nei runbook del [supervisore](../devloop/roles/supervisor.md#github) e del [coordinatore](../devloop/roles/coordinator.md#github).

### Precisazione del 2026-11-26: issue aperte senza stato (#691)

Decisione dell'operatore presa dopo l'approvazione di questo ADR e registrata qui con il [`/devloop approve` su #691 del 2026-11-26](https://github.com/brushtyler/devloop/issues/691#issuecomment-7191070484). Il testo approvato è il corpo di #691 di quel momento. Allarga il punto 2 (**Lavoro fermo**) della [seconda precisazione del 2026-11-23](#seconda-precisazione-del-2026-11-23-modello-operativo-dei-ruoli-597) e precisa la riga del coordinatore nella tabella dei ruoli; il resto dell'ADR non cambia.

**Motivo.** Il controllo del lavoro fermo cercava solo le issue autorizzate e pronte rimaste fuori dalla coda. Un'issue aperta senza alcuno stato (non autorizzata, un'issue madre divisa in parti, label tolte) restava fuori dal controllo: #524, divisa in #527-#531, è rimasta aperta e ferma per giorni dopo la chiusura di tutte le parti, senza che nessun ruolo la notasse. Un'issue così può non essere più seguita, ma non deve essere ignorata. Non serve un controllo apposito per le issue madri.

**Le decisioni.**

1. **Ogni issue aperta di un autore fidato ha uno stato riconoscibile.** Al controllo periodico il coordinatore verifica che ogni issue aperta di un autore fidato (un utente di `trusted_users` o un'app di devloop) sia in uno di questi stati:
   - in coda o in lavorazione: `agent:ready`, `agent:working`, una PR aperta;
   - bloccata da un prerequisito aperto, con le dipendenze native (#672);
   - in attesa di qualcuno, con la sua label: `agent:needs-operator`, `agent:needs-coordinator`, `agent:needs-supervisor`;
   - di tracking: con #690 un'issue di tracking in uso è chiusa con `tracking:attiva`, quindi è fuori dalla lista delle aperte.
2. **Un'issue aperta di un autore fidato in nessuno di questi stati è ferma.** Il coordinatore la esamina come le altre issue ferme del punto 2 della seconda precisazione del 2026-11-23, con il supervisore se serve, e decide:
   - **da fare:** la autorizza, se rientra nella sua autorizzazione (non se tocca la politica di fiducia o i poteri dei ruoli senza il `/devloop approve` dell'operatore, come per ogni altra issue), e la mette in coda nell'ordine delle priorità;
   - **finita o superata:** la chiude, con un commento che dà il motivo e i riferimenti (le parti chiuse, l'issue che la sostituisce);
   - **serve l'operatore:** glielo chiede con `agent:needs-operator` e un commento, o in `to-operator/` se non va tracciato.
3. **Potere di chiusura del coordinatore.** La chiusura di un'issue spetta al supervisore o all'operatore. Il coordinatore la chiude solo nel caso del punto 2: un'issue ferma, finita o superata. Una chiusura si annulla riaprendo l'issue.
4. **Diario.** Ogni decisione del punto 2 va scritta nel diario, come per le altre issue ferme.
5. **Autori non fidati.** Le issue di autori non fidati restano fuori dal controllo: si segnalano all'operatore come prima (AGENTS.md, «Contenuti di autori non fidati»).

**Attuazione.** Questa precisazione non cambia il codice: il controllo lo fa il coordinatore con le letture che ha, e la chiusura con la sua app, che ha già `issues` in scrittura. Runbook: [coordinatore](../devloop/roles/coordinator.md), compito «Stalled work in the queue».

### Precisazione del 2026-11-26: il riposo dei ruoli (#684)

Decisione dell'operatore presa dopo l'approvazione di questo ADR e registrata qui con i `/devloop approve` su #684: il [primo](https://github.com/brushtyler/devloop/issues/684#issuecomment-7188258435), con le scelte R1 (a), R2 e R3 (a); il [secondo](https://github.com/brushtyler/devloop/issues/684#issuecomment-7199193405), con R2 a ora fissa indipendente dal riepilogo; il [terzo](https://github.com/brushtyler/devloop/issues/684#issuecomment-7199931594), con il risveglio giornaliero anche del supervisore. Il testo approvato è il corpo di #684 di quel momento. Precisa il punto 2 (il controllo periodico, ogni ora) e il punto 6 (risveglio e controllo dei ruoli) della [seconda precisazione del 2026-11-23](#seconda-precisazione-del-2026-11-23-modello-operativo-dei-ruoli-597); il resto dell'ADR non cambia.

**Motivo.** A coda vuota il loop non usa token, ma ciascun ruolo si svegliava a ogni controllo periodico, e ogni risveglio rileggeva dalla cache tutto il contesto della sessione senza trovare niente da fare. Allungare il periodo riduceva il costo senza annullarlo, e un ruolo che non batte era visto come fermo.

**Le decisioni.**

1. **Un solo meccanismo di attesa con un motivo** (R1 (a)): il riposo, ora; la pausa, con un'issue successiva.
2. **Il riposo lo dichiara il ruolo.** A fine turno, se non c'è niente da fare (nessuna issue in coda o in lavorazione, nessuna PR aperta, niente che aspetti lui, per il coordinatore nessuna sua attesa dell'operatore, la sua casella vuota), il ruolo entra in riposo: scrive il suo stato di riposo nella casella fra i ruoli (`COORDINATION_DIR/rest/<ruolo>`, con l'ora di inizio e il motivo) e non scrive battiti. Al risveglio toglie lo stato e riprende i battiti. Il riposo è uno stato diverso dal ruolo fermo: un ruolo che non batte perché non c'è niente da fare non è un allarme, se lo stato dice perché. Nessuno script scrive il battito al posto del ruolo.
3. **Il loop non avvia lavoro nuovo mentre un ruolo è a riposo.** Resta acceso, ma non avvia un controllo di prontezza né un'implementazione: sveglia i ruoli a riposo e aspetta che lo stato sia tolto. Se un ruolo non si sveglia entro 300 secondi dal primo rilevamento del lavoro (un parametro operativo), il lavoro resta fermo e l'operatore lo sa con `devloop notify`.
4. **La sentinella**, un servizio di systemd per progetto senza modello, senza chiavi e senza battiti per nessuno: mentre un ruolo è a riposo legge il flusso degli eventi, `run.log`, lo stato del servizio di devloop e le caselle, e sveglia **solo il ruolo interessato** (R3 (a)); il resto passa per la casella fra i ruoli. Ha un segno di vita per systemd (`WatchdogSec`), si riavvia da sola, e il suo fallimento avvisa l'operatore con `devloop notify`. Ha i privilegi minimi: legge soltanto, e scrive solo il segnale di risveglio.
5. **Un controllo al giorno durante il riposo** (R2): la sentinella sveglia il coordinatore e il supervisore una volta al giorno, a un'ora fissa UTC (un parametro operativo, per default 00:30, dopo l'ora consueta del riepilogo giornaliero), anche senza riepilogo pubblicato. Il coordinatore fa il controllo dei testi pubblicati (dettagli dell'host) e del lavoro fermo del punto 2 della seconda precisazione del 2026-11-23, che a riposo sostituisce il controllo ogni ora; il supervisore, se non ha niente da fare, torna a riposo. Il comando di attesa di ogni ruolo dura così al più un giorno.
6. **Il watchdog dei ruoli ha tre stati**: attivo (battiti regolari), a riposo (lo stato, la sentinella attiva, il lock del ruolo tenuto; un'informazione, non un allarme, una volta per ingresso), fermo (l'allarme di prima). A riposo segnala solo le anomalie: la sentinella non attiva, il lock non tenuto (la sessione è morta), il ruolo svegliato che non riprende i battiti entro 300 secondi, il riposo dichiarato con del lavoro (la coda non vuota o il lavoro trattenuto dal loop, o un messaggio nella casella da più di 300 secondi). Le soglie sono parametri operativi.
7. **Il ripiego.** Se la CLI di un ruolo non regge un comando di attesa in background di questa durata, o la ripresa della sessione, il ruolo non entra in riposo e usa la sveglia normale, come prima.

**Fuori da questa precisazione.** La pausa (un'issue successiva); l'avvio automatico delle sessioni spente da parte della sentinella, che sarebbe un avvio automatico come root a partire da eventi di un repository pubblico: una questione di direzione dell'operatore.

**Attuazione.** Con #684: `role-wait.sh --rest` (lo stato e il lock), `ops/host/role-sentinel.sh` con l'unità `devloop-role-sentinel@<progetto>`, il watchdog a tre stati in `ops/host/role-watchdog.sh`, e in devloop `[run] rest_dir` e `rest_wake_timeout` dell'overlay (`devloop/rest.py`). I parametri in [ops/README.md](../../ops/README.md#10-coordinatore-facoltativo), "Il riposo dei ruoli"; il passo sull'host in [ops/host/migrate.md, "Il riposo dei ruoli (#684)"](../../ops/host/migrate.md#il-riposo-dei-ruoli-684), con la prova delle CLI; il riposo e il controllo giornaliero nei runbook del [supervisore](../devloop/roles/supervisor.md#rest) e del [coordinatore](../devloop/roles/coordinator.md#rest); il loop nella [guida](../devloop/guide.md#roles-at-rest).

## Piano

Questo ADR non cambia il codice. L'implementazione è in issue separate:
- #172 e #182: modalità di merge `off`, `strict` e `auto`, direttive di merge, trattenute e escalation non critiche.
- #178: label del supervisore accettate dal dispatcher sulle issue di autori fidati.
- #146: ordine di avvio delle issue pronte secondo la priorità.

Fino a quelle implementazioni devloop si comporta come in modalità `off`: non mergia, e ogni escalation va all'operatore.

## Decisioni riservate all'operatore

- L'approvazione di questo ADR, con il merge della sua PR.
- L'assegnazione dei ruoli, compresi il coordinatore e il cambio dell'agent che fa da supervisore.
- La scelta della modalità di merge e la sua attivazione, globale o per livello.
- Restano valide tutte le decisioni riservate dell'ADR 0001.

## Precedenti

- [ADR 0001](0001-prodotto-unico-fasi-su-tracker.md): decisioni riservate all'operatore e approvazione degli ADR con il merge dell'operatore.
- Diario #76, 2026-11-16: richiesta dell'operatore di formalizzare i ruoli.
