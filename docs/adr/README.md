# Architecture Decision Records

Questa directory contiene gli ADR del repository: le decisioni di progetto, con il contesto, le alternative e le scelte dell'operatore. `docs/adr/` è in `operator_only_paths` ([`devloop/config.toml`](../../devloop/config.toml)): una PR che la tocca la mergia solo l'operatore.

| ADR | Titolo |
|---|---|
| [0001](0001-prodotto-unico-fasi-su-tracker.md) | Un unico prodotto, con le fasi su issue e PR e il backend come porta |
| [0002](0002-ruoli-e-responsabilita.md) | Ruoli e responsabilità, e modalità di merge `off`, `strict` e `auto` |
| [0003](0003-struttura-della-documentazione.md) | Audit della documentazione e struttura di destinazione |
| [0004](0004-passo-2-budget-stato-durevole.md) | Piano del passo 2 dell'ADR 0001 (budget, prenotazione e stato durevole) |
| [0005](0005-deleghe-e-ruoli-degli-agent.md) | Deleghe al supervisore, implementer generico e pool dei reviewer |
| [0006](0006-devloop-su-altri-repository.md) | devloop su altri repository (parità con il motore locale) |
| [0007](0007-da-ssdlc-a-devloop.md) | Da ssdlc a devloop, motore SSDLC congelato e rinomina del repository |
| [0008](0008-pool-di-esecuzione.md) | Pool di esecuzione condiviso fra progetti (worker, budget comune, isolamento per job) |

## Ciclo di un ADR

1. **Proposta.** L'ADR nasce in una PR con lo stato "proposto". Le decisioni riservate all'operatore stanno in una sezione dedicata, ciascuna con le alternative e una raccomandazione, senza prenderle.
2. **Approvazione, prima del merge.** Quando l'operatore approva, **la stessa PR**, prima del merge, aggiorna:
   - lo stato in "approvato dall'operatore con il merge di PR #N (data)";
   - la sezione delle decisioni in "Decise dall'operatore il <data>", con la scelta fatta per ogni punto e, a parte, i punti che restano aperti.

   Le domande con le alternative restano nel documento come registro storico. Il modello è la sezione "Decisioni riservate all'operatore" dell'[ADR 0001](0001-prodotto-unico-fasi-su-tracker.md#decisioni-riservate-alloperatore).
3. **Merge.** Il merge dell'operatore (`operator_only_paths`) è l'approvazione: un ADR su `main` con lo stato "approvato" riporta le decisioni dell'operatore.
4. **Attuazione.** Le issue che attuano le decisioni **citano la sezione dell'ADR su `main`**, non commenti di PR, che il controllo di prontezza delle issue non legge. Il controllo di prontezza ([`devloop/prompts/preflight.md`](../../devloop/prompts/preflight.md)) tratta una decisione registrata come "Decisa dall'operatore" in un ADR su `main` come decisione dell'operatore per le issue che la citano; le decisioni ancora aperte nell'ADR restano lacune.
