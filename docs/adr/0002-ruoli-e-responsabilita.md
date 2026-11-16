# ADR 0002: ruoli e responsabilità, e modalità di merge `off`, `strict` e `auto`

- **Stato:** proposto. Come previsto dall'ADR 0001, diventa approvato solo con l'approvazione e il merge dell'operatore.
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

### Ruoli

| Ruolo | Attore oggi | Fa | Non fa |
|---|---|---|---|
| **Operatore** | `brushtyler` | Prende le decisioni riservate; risolve le escalation (`agent:needs-human`); approva gli ADR; assegna i ruoli; può sempre mergiare, bloccare o riprendere un'attività | — |
| **Coordinatore** (facoltativo, oggi non assegnato) | — | Piano e priorità del backlog, bozze di ADR, verifica del lavoro del supervisore. Delega dell'operatore, revocabile | Le decisioni riservate |
| **Supervisore** | Claude Code (`claude-agent`) | Sorveglia devloop (watch); analizza le RUN; apre issue per problemi e miglioramenti; mette in coda (`agent:ready`, priorità) le issue di autori fidati, secondo le priorità approvate; in modalità `auto` trattiene o mette in sequenza i merge e decide le escalation non critiche; fa escalation all'operatore | Implementare, fare review, cambiare configurazione, policy, budget o credenziali; mergiare: lo fa il dispatcher |
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
- Nessuna label `agent:needs-human` o `agent:merge-hold`.

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
