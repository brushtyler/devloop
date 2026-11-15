# ADR 0001: un unico prodotto, con le fasi su issue e PR e il backend come porta

- **Stato:** proposto. Il merge di questa PR da parte dell'operatore vale come approvazione della direzione e del piano. Non vale per le decisioni elencate in "Decisioni riservate all'operatore", che restano aperte finché l'operatore non le prende in modo esplicito.
- **Fase:** design (#126, `phase:design`). È la prima fase di design eseguita nella forma che devloop dovrà automatizzare: issue di design, conclave come review, PR con ADR, merge dell'operatore.
- **Data:** 2026-11-15.

## Contesto

- Nel repository convivono **ssdlc**, un orchestratore a fasi con specifica, design, piano, sviluppo, gate ed evidenze locali, e **devloop**, un dispatcher di issue e PR su GitHub. #59 aveva previsto ssdlc come esecutore di un'unità di lavoro dentro devloop (decisione 2), ma non era mai stato realizzato. #4 proponeva il modello inverso.
- #126 ha chiesto se integrare i due o separarli, con un criterio decisivo: **niente review duplicate**, perché sarebbero puro spreco (muda).
- Due analisi indipendenti, di Codex e Antigravity, raccomandavano devloop come proprietario del flusso GitHub, ssdlc come esecutore esterno opzionale e repository separati. Divergevano sulla divisione delle review.
- L'operatore ha indicato una terza strada (opzione C), qui decisa.

## Decisione (indirizzo dell'operatore)

1. ssdlc e devloop sono **un unico prodotto**, nello **stesso repository**.
2. **Gli output delle fasi di ssdlc si spostano sul tracker:** specifica, design, piano ed evidenze diventano issue e PR, non file sull'host della run.
3. **Il backend del tracker è una porta.** GitHub è il primo backend. In seguito ce ne potranno essere altri, compreso il filesystem per l'uso offline con ticket ed evidenze locali.
4. **Si accetta una perdita:** due run strutturate e separate sulla stessa issue con configurazioni diverse non sono più possibili. Si recuperano con l'entità "tentativo" (vedi sotto).

## Opzioni considerate

| Opzione | Sintesi | Esito |
|---|---|---|
| A. ssdlc come esecutore esterno di devloop | devloop invoca ssdlc come comando, ssdlc restituisce commit ed Evidence Pack | Scartata: due macchine a stati, un contratto di stop da mantenere, review duplicate o tagliate in modo uniforme |
| B. Separazione dei repository | devloop in un repo dedicato, ssdlc invariato | Scartata: con un prodotto unico non serve, e i costi della convivenza si risolvono dentro il repo |
| **C. Prodotto unico, fasi sul tracker, backend come porta** | Indirizzo dell'operatore | **Scelta** |

## Esito del conclave (#126)

Il conclave ha votato all'unanimità **APPROVA, con condizioni vincolanti**:
- Codex (gpt-6.1-sol, high);
- Claude (Opus 5.5, high, in una sessione nuova senza il contesto di chi ha proposto l'opzione);
- Antigravity (Gemini 4 Argon, high).

I voti completi sono nei commenti di #126.

### Vincoli (sintesi dei tre voti)

1. **Nessuna garanzia si toglie prima della parità.**
   - Il motore locale di ssdlc resta invariato fino al passo 5.
   - Al passo 5 i 12 casi A5 devono passare **senza modifiche** sul nuovo nucleo, con prove aggiuntive del nuovo percorso.
   - Grader e baseline non si toccano nella stessa PR che cambia il motore.
2. **Budget e prenotazione prima delle fasi multi-round.** Ogni azione costosa registra un intento durevole **prima** di chiamare il modello, con blocco sugli esiti incerti (`OUTCOME_UNCERTAIN`). I budget restano cumulativi attraverso fasi, issue figlie, retry e riavvii: una nuova issue non azzera il budget della storia.
3. **Pubblicazione affidabile.** L'esecutore salva su disco ricevuta e output **prima** di pubblicare sul tracker, e la pubblicazione è riprendibile, come la outbox di `ssdlc_feedback_delivery`. Le issue generate da un piano portano un marker del dispatcher con la chiave `(sha del design approvato, task-id)`. Alla ripresa si riconcilia su quella chiave: niente duplicati né orfani.
4. **Perimetro.** Il controllo del manifest **solo sul diff finale non equivale** a `SCOPE_REQUIRED`, che ferma il candidato fuori scope prima dell'adozione. Serve un controllo deterministico prima della promozione, più `protected_paths`, documentando la differenza.
5. **Contenimento e fencing** (container, separazione delle credenziali, fencing dei writer #115–#117) sono **obbligatori prima di eseguire più tick in parallelo** e prima di affidare alle fasi esecuzioni che sostituiscono ssdlc.
6. **Il riuso di codice di ssdlc non rompe in silenzio il confine di #59.** devloop oggi importa solo la stdlib e sé stesso (`tests/test_devloop_boundary.py`).
   - Prompt e schemi si riusano come **dati**.
   - Il codice condiviso, cioè i validatori puri (catalogo dei requisiti, validazione del piano), passa in un pacchetto `core` dichiarato.
   - Niente import diretti di `ssdlc_*`, che è il candidato sotto test del replay gate, e niente cicli stateful (`DesignSession`, `orchestrator.py`).
7. **L'approvazione del piano genera issue proposte,** ma non ne autorizza l'esecuzione: `agent:ready` resta un atto dell'operatore.

## Piano (ordine rivisto dal conclave)

1. **Porte del tracker, solo refactoring** (vedi "Prima issue").
2. **Budget e prenotazione in devloop** (vincolo 2), sulla base di #133. Prima di qualsiasi fase multi-round reale.
3. **Fasi come tipi di issue e PR.** Prima la PR di design per le storie con label, con il conclave come review. Poi il piano, che genera le issue proposte (vincoli 3 e 7). Infine il controllo deterministico del perimetro (vincolo 4).
4. **Contenimento e fencing degli esecutori** (vincolo 5).
5. **Backend filesystem**, con identità firmate (vedi "Fiducia per backend").
6. **Convergenza del motore locale di ssdlc,** con il benchmark A5 sul nuovo nucleo (vincolo 1). #4 viene chiusa come assorbita.

### Prima issue (passo 1)

È un refactoring puro di devloop:
- **Porte.** Le porte `Tracker`, `CodeReview` e `CI` contengono i soli metodi che il dispatcher usa oggi da `devloop/github.py`, con dati normalizzati (`author_login`, `merge_state`, `summarize_checks`).
- **Adattatori concreti.** `GitHub` e `Identities` restano gli adattatori.
- **Fake.** `FakeGH` e gli altri fake vanno in `tests/helpers/`, con un **test di conformità**: fake e adattatore hanno gli stessi metodi con le stesse firme.
- **Criteri:**
  - suite di devloop invariata;
  - testi e marker pubblicati identici byte per byte;
  - test di confine verde;
  - `config.toml` invariato.
- **Esclusi:** backend filesystem, import di ssdlc, cambi alla CI o alla policy.

Tocca `devloop/`, quindi vale il livello 2: due reviewer.

## Porta del tracker

- **Nella porta:**
  - unità di lavoro (id, tipo, titolo, corpo, stato);
  - transizioni di stato astratte, non label;
  - storia degli eventi con provenienza (chi e quando per approvazioni, modifiche, rinomine), usata da `untrusted_change`;
  - commenti e marker con **autore verificato**;
  - esiti distinti per successo, conflitto ed esito incerto;
  - una capacità dichiarata `identity_assurance`: `authenticated`, `signed` oppure `integrity_only`.
- **Nel nucleo:** autorizzazione, policy, semantica dei marker, dipendenze, invalidazione delle approvazioni, budget, journal e recupero. L'esecutore fornisce le prove di avvio, consumo e terminazione.
- **Nell'adattatore GitHub:**
  - `gh` con REST e GraphQL;
  - paginazione;
  - token delle App e JWT;
  - label e timeline;
  - codifica dei marker e la convenzione dell'intestazione `> [!NOTE]`;
  - `merge_state`, gli stati aggregati e le review native.

## Fiducia per backend

- **GitHub:** eventi e identità delle App sono autenticati. L'autenticazione non concede autorità da sola: i marker valgono solo se li scrive l'attore previsto.
- **Filesystem:**
  - il journal con hash concatenati dà solo integrità, non provenienza (#3);
  - gli atti dell'operatore (l'equivalente di `agent:ready`, `/devloop rereview`, `/devloop finding`) sono **commit o tag firmati** SSH o GPG. Si verificano con un `allowed_signers` che sta nell'**installazione fissata**, mai nel repository su cui lavorano gli agent;
  - l'approvazione è legata a hash del ticket, SHA, policy e sequenza monotona, e c'è un'ancora esterna contro rollback e replay;
  - le chiavi stanno fuori da ogni sandbox;
  - l'indipendenza fra implementer e reviewer sullo stesso host è attestata dal dispatcher, non dalla crittografia, e va dichiarato;
  - un backend `integrity_only` **non può promuovere**: produce al massimo bozze locali.

## Divisione delle review fra le fasi

| Fase | Unica review, ambito | Artefatto |
|---|---|---|
| Specifica | Requisiti verificabili, criteri di accettazione e di abuso. Approvazione dell'operatore | Specifica con `AC-*` e `SEC-*` |
| Design | Architettura, STRIDE, mitigazioni. Conclave per le storie con label | ADR, threat model, obblighi |
| Piano | Copertura dei requisiti, DAG, manifest, verifiche | Piano approvato e issue proposte |
| Verifica deterministica | Test, controllo del perimetro sul manifest, segreti, scansione delle dipendenze. CI e dispatcher, nessun modello | Esiti legati al commit |
| Implementazione (PR) | Una sola review del codice: correttezza, regressioni, conformità ad ADR, `AC-*` e `SEC-*` presi dal design approvato. Il `04-review` interno confluisce qui | Ricevuta sul commit (#135) |
| AppSec da modello | Solo al livello 2 o sui percorsi sensibili: secondo reviewer con focus sugli invarianti di sicurezza (#111) | Finding con un proprietario |
| Supervisione | Completezza e provenienza delle evidenze, merge umano | Merge |

Ops e documentazione diventano criteri dei task, non fasi separate. Prima dell'adozione, la matrice di #126 va completata con chiamate e token misurati per fase: il risparmio va dimostrato.

## Cosa si perde e come si recupera

| Garanzia di ssdlc | Come si conserva |
|---|---|
| Recupero dai crash, journal, generazioni | Motore locale invariato fino al passo 6. Intenti durevoli e pubblicazione riprendibile (vincoli 2 e 3) |
| Fencing dei writer | #115–#117. Per GitHub, artefatti legati allo SHA (approvazioni e ricevute di policy) |
| Replay e benchmark A5 | Invariati, poi portati sul nuovo nucleo (vincolo 1) |
| Contenimento | Esecutori in container (passo 4) |
| Budget e costi ignoti | `LimitsStore` e `live_unknown_usage` (#43, #128) nel nucleo (passo 2) |
| Perimetro (`SCOPE_REQUIRED`) | Controllo deterministico prima della promozione (vincolo 4) |
| Più tentativi sulla stessa issue | Entità "tentativo", con branch e PR per variante (#135) |

## Decisioni riservate all'operatore (aperte)

1. **Confine di #59:** se e come revocarlo, cioè il pacchetto `core` dichiarato e i prompt e gli schemi riusati come dati (vincolo 6).
2. **Divulgazione.** Il repository è pubblico: threat model STRIDE e finding AppSec su vulnerabilità non ancora corrette **non** devono diventare issue o PR pubbliche per default. Va deciso cosa resta locale o redatto prima del passo 3.
3. **Revisioni della baseline del replay gate,** necessarie per filtrare la CI per percorso, perché il workflow è fissato. Ed eventuali nuove versioni dell'oracolo A5.
4. **Attivazione delle fasi sulle run reali, migrazione delle run esistenti e chiusura di #4.**
5. **Estensioni di budget e di round.**

## Precedenti

Le fasi di design e piano sono già state eseguite a mano il 2026-11-15:
- il conclave su #45, da cui sono nate #115, #116 e #117;
- il conclave su #93, da cui sono nate #118, #119 e #120;
- le divisioni di #66 (#106, #110, #111, #107) e di #133 (#133, #134, #135) dopo i controlli di prontezza.
