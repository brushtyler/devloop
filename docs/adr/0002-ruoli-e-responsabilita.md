# ADR 0002: ruoli e responsabilità, e modalità di merge `off`, `strict` e `auto`

- **Stato:** approvato dall'operatore con il merge di PR #189 (2026-11-17), e implementato: le modalità di merge con #172 (PR #188) e lo smistamento delle escalation con #182 (PR #198). La modalità `auto` è attiva dal 2026-11-17. La decisione dell'operatore del 2026-11-22 su #524 (modello a quattro ruoli e nuovo principio 4) è scritta come [precisazione](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527) e nel principio 4, con #527. La decisione dell'operatore del 2026-11-23 sui compiti del coordinatore è scritta come [precisazione](#precisazione-del-2026-11-23-compiti-del-coordinatore-594), con #594. Il resto del documento è il registro della decisione, scritto prima delle implementazioni: le frasi su "oggi" descrivono lo stato del 2026-11-16.
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
| **Operatore** | `brushtyler` | Prende le decisioni riservate e la direzione strategica: ADR, `AGENTS.md`, configurazione, `trusted_users` e chiavi, i tetti (`merge-budget`, budget), il merge contro un verdetto negativo dei reviewer; risolve le escalation critiche (`agent:needs-operator`); approva gli ADR; assegna i ruoli; può sempre mergiare, bloccare o riprendere un'attività | — |
| **Coordinatore** (facoltativo, `[roles.coordinator]`) | l'agent di `[roles.coordinator]`, con l'app `coordinator` | Responsabile dell'operatività: decide come risolvere i problemi operativi critici, detta le priorità e l'ordine del lavoro, sblocca il processo quando si ferma; verifica il lavoro del supervisore; mergia le PR riservate con la decisione dell'operatore; prepara le decisioni per l'operatore. Agisce con il perimetro chiuso di comandi delle precisazioni del [2026-11-22](#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527) e del [2026-11-23](#precisazione-del-2026-11-23-compiti-del-coordinatore-594). Delega dell'operatore, revocabile | Le decisioni riservate; il lavoro che cambia i poteri di un ruolo o la politica di fiducia |
| **Supervisore** | Claude Code (`claude-agent`) | Sorveglia devloop (watch); analizza le RUN; apre issue per problemi e miglioramenti; mette in lavorazione (`agent:ready`, priorità) le issue di autori fidati, seguendo le priorità del coordinatore; in modalità `auto` trattiene o mette in sequenza i merge e decide le escalation non critiche; fa escalation all'operatore | Implementare, fare review, cambiare configurazione, policy, budget o credenziali; mergiare: lo fa il dispatcher |
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
| **Coordinatore** | agent, periodico (a intervalli e sugli allarmi), non sempre acceso | la sua GitHub App; sull'host solo **lettura** (sudoers con comandi di lettura per log, stato, versioni e permessi) | i riepiloghi di devloop (`report`, #316), il ledger, i record del dispatcher, le azioni del supervisore, gli allarmi, le metriche (quote, round, fallimenti) | responsabile dell'operatività (dalla [precisazione del 2026-11-23](#precisazione-del-2026-11-23-compiti-del-coordinatore-594)): decide come risolvere i problemi operativi critici, detta le priorità e l'ordine del lavoro, sblocca il processo quando si ferma; verifica che il supervisore faccia quanto previsto; individua i problemi di sistema che rallentano il supervisore o devloop e li scrive come issue; mergia le PR riservate che la delega del merge (#509) non copre, dopo il quorum e con la decisione dell'operatore; prepara all'operatore le decisioni e un riepilogo periodico; chiede all'operatore un intervento dell'agent dell'host, con il riferimento alla procedura |
| **Supervisore** | agent, continuo | la sua GitHub App | issue e PR di autori fidati, escalation, log dei turni | messa in lavorazione e ordine della coda, seguendo le priorità del coordinatore; escalation non critiche; analisi delle RUN e issue sui problemi che vede; merge delegati (#509) |
| **Agent dell'host** | agent **occasionale**, lanciato dall'operatore, **fuori dal ciclo** di devloop | `sudo` sull'host; **nessuna** scrittura su GitHub | **solo** procedure e script già su `main` (`ops/host/`, `ops/host/migrate.md`, `docs/devloop/quickstart.md`) e l'istruzione dell'operatore, **mai** testo di issue, PR o output degli agent | installazione di devloop per un progetto, avvio dei servizi, aggiornamenti e migrazioni (pin dopo un passo sull'host, nuove versioni delle CLI). Riporta l'esito all'operatore, e lo registra l'operatore o il coordinatore |

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

**Fuori da questa precisazione.** Le parti del modello ancora in discussione (conclave con voto sostitutivo, risveglio del supervisore, passi sull'host in assenza dell'operatore) non sono decise qui.

**Attuazione.** Questa precisazione non cambia il codice. Finché l'estensione del perimetro non è implementata in devloop, il dispatcher continua a rifiutare `capacity` e `grant-budget` dall'identità del coordinatore come riservati all'operatore (`COORDINATOR_RESERVED` in `devloop/supervision.py`); le etichette di priorità del coordinatore sono in #595.

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
