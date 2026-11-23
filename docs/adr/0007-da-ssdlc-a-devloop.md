# ADR 0007: da ssdlc a devloop, motore SSDLC congelato e rinomina del repository

- **Stato:** approvato dall'operatore il 2026-11-22 con il [commento di approvazione su #533](https://github.com/brushtyler/ssdlc/issues/533#issuecomment-7083511946), che accetta le decisioni e i default dei punti da fissare. Il merge di questa PR è dell'operatore (`docs/adr/` è in `operator_only_paths`). Supera l'[ADR 0001](0001-prodotto-unico-fasi-su-tracker.md) per la parte del motore SSDLC.
- **Fase:** design (#533).
- **Data:** 2026-11-22.

## Contesto

- Il repository nasce come **ssdlc** ([ADR 0001](0001-prodotto-unico-fasi-su-tracker.md)): un motore locale per il ciclo di sviluppo sicuro (`orchestrator.py`, i moduli `ssdlc_*.py`, `core/`, `standards/`, `schemas/`, il replay gate), che funziona senza infrastruttura. Ha però problemi non facilmente risolvibili: un costo di implementazione troppo alto, troppi blocchi, troppe decisioni e troppo coinvolgimento dell'operatore per arrivare al risultato.
- Il progetto è evoluto in **devloop** (#59, ADR [0002](0002-ruoli-e-responsabilita.md)-[0006](0006-devloop-su-altri-repository.md)): un approccio diverso, con agent, review strutturata su GitHub e merge automatico, che cerca di ridurre il più possibile le azioni dell'operatore, verso un sistema autonomo. Si aspettavano i risultati di devloop per decidere la direzione. Dal 18 al 22 novembre 2026 devloop ha implementato e mergiato decine di PR quasi da solo, e l'operatore è intervenuto solo su decisioni puntuali.
- devloop non importa nulla del motore. I legami sono al contorno:
  - alcuni test del motore nella suite (per esempio `tests/test_review_closure.py`, che ha bloccato #488 il 20 novembre);
  - i percorsi sensibili del motore nel file di progetto di ssdlc (`ssdlc_writer_gate.py` e simili);
  - la baseline del replay gate in `sensitive_path_lists`;
  - `setup-ssdlc.sh` e una parte di `docs/`.
- Questi vincoli portano molte PR al livello 2, quindi più review e più quota.

## Decisioni

Decise dall'operatore il 2026-11-22 ([approvazione su #533](https://github.com/brushtyler/ssdlc/issues/533#issuecomment-7083511946)).

1. **Il motore SSDLC è congelato** dal 2026-11-22: niente sviluppo nuovo. Le sue issue aperte (#1, #4, #253) sono chiuse come non pianificate.
2. **Il repository si rinomina** da `brushtyler/ssdlc` a **`brushtyler/devloop`**. GitHub reindirizza i vecchi URL, e le installazioni delle app seguono il repository.
3. **devloop è il prodotto**, e il repository è la sua sorgente: `SOURCE_REPO` delle installazioni diventa `brushtyler/devloop`.

## Punti fissati

L'operatore ha accettato il default proposto per ogni punto.

| Punto | Decisione |
|---|---|
| Il codice del motore | **Si toglie dall'albero** di `main`, dopo un tag `ssdlc-engine-final` sull'ultimo commit che lo contiene. Resta nella storia e nel tag, senza un repository a parte. Si tolgono anche i vincoli che porta: test del motore, percorsi sensibili del motore, replay gate e baseline, `setup-ssdlc.sh`, la documentazione del motore e i `PLAN_*.md`. |
| L'ADR 0001 | Resta nella storia, con lo stato "superato per la parte del motore da ADR 0007". I principi che devloop eredita (tracciabilità su GitHub, decisioni riservate) restano validi tramite gli ADR 0002-0006. |
| I percorsi sull'host | **Invariati** (`/home/devloop/ssdlc`, `.ssdlc/devloop`), per non toccare un'installazione che funziona. Si aggiornano `repo` nell'overlay, i remote dei clone e il nome del progetto nei parametri. La rinomina dei percorsi, se servirà, è un passo a parte. Cambiati poi con #563: vedi la precisazione del 2026-11-23 qui sotto. |
| L'ordine | Prima la rimozione del motore, poi la rinomina su GitHub, poi l'aggiornamento dell'host (agent dell'host), poi la documentazione (README, `AGENTS.md`, riferimenti a "ssdlc"). Tutto **prima** del primo progetto esterno (#515), così `SOURCE_REPO` è `devloop` fin dall'inizio. |
| La rinomina su GitHub | È un atto dell'operatore (impostazioni del repository), oppure dell'agent con il suo permesso esplicito. |

## Piano

Le issue nascono dopo il merge di questo ADR e citano la sua sezione su `main`.

| Parte | Cosa | Dipende da |
|---|---|---|
| R1 | Tag `ssdlc-engine-final`; rimozione del codice, dei test, dei documenti e dei vincoli del motore; file di progetto di ssdlc senza i percorsi del motore | ADR 0007 |
| R2 | Rinomina su GitHub (operatore) | R1 |
| R3 | Host: `repo` nell'overlay, remote dei clone, parametri, verifica con `apps` e `check-host` (agent dell'host, da una sezione di [`ops/host/migrate.md`](../../ops/host/migrate.md)) | R2 |
| R4 | Documentazione e nomi: README, `AGENTS.md`, `docs/`, prefisso dei messaggi, esempi `SOURCE_REPO` | R2 |

## Precisazione del 2026-11-23: i percorsi sull'host (#563)

Decisione dell'operatore nel [`/devloop approve` su #563](https://github.com/brushtyler/devloop/issues/563#issuecomment-7090267453), presa dopo R1-R4. Il punto "I percorsi sull'host" sopra li lasciava invariati; l'operatore ha deciso di cambiarli adesso, prima del primo progetto esterno (#515), e di passare questo host all'unità template `devloop@.service` dell'[ADR 0006](0006-devloop-su-altri-repository.md), R8.

- **Perché.** `.ssdlc/` è la directory di stato del vecchio motore SSDLC, che l'operatore usa ancora in altri repository: il default di devloop `.ssdlc/devloop` metteva lo stato di devloop dentro quella del motore, ed aveva senso solo finché motore e devloop stavano nello stesso repository. Con `devloop.service` c'è una sola istanza per host; con `devloop@<progetto>` ogni progetto ha la sua. E dopo la rinomina `ssdlc` restava solo nei percorsi.
- **Cosa cambia.** Lo `state_dir` di default è `.devloop`, senza ripiego su `.ssdlc/devloop`: il launcher si ferma se trova solo quello. Questo host passa a `PROJECT=devloop` con l'istanza `devloop@devloop`, con gli utenti di prima (`devloop`, `devloop-agent`, `supervisor`, `coordinator`) e le `key_dir` di prima (`~/.ghapps`, quindi nessuna chiave di fiducia cambia). I clone diventano `/home/devloop/devloop`, `/home/supervisor/devloop` e `/home/coordinator/devloop`; sudoers, casella e copia di root del coordinatore prendono i default di `PROJECT`. Il checkout dell'operatore `/root/repos/ssdlc` resta com'è.
- **Cosa non cambia.** Il tag `ssdlc-engine-final` e le directory `.ssdlc/` del vecchio motore negli altri repository: devloop non le legge né le scrive. Il nome del file di progetto di ssdlc e il resto di questo ADR.
- **Il passo sull'host** è la sezione [Percorsi devloop e istanza devloop@devloop](../../ops/host/migrate.md#percorsi-devloop-e-istanza-devloopdevloop-563) di `ops/host/migrate.md`, a loop fermo, dopo il merge della PR di #563.
