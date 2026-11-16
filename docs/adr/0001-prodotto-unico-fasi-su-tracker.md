# ADR 0001: un unico prodotto, con le fasi su issue e PR e il backend come porta

- **Stato:** approvato dall'operatore con il merge di PR #137 (2026-11-16). Le decisioni riservate sono state prese il 2026-11-16 (vedi la sezione relativa).
- **Fase:** design (#126, `phase:design`). È la prima fase di design eseguita nella forma che devloop dovrà automatizzare: issue di design, conclave come review, PR con ADR, merge dell'operatore.
- **Data:** 2026-11-15.
- **Revisione:** la versione 2 corregge la prima stesura dopo una verifica di fedeltà ai voti, fatta da Codex.

## Contesto

- Nel repository convivono **ssdlc**, un orchestratore a fasi con specifica, design, piano, sviluppo, gate ed evidenze locali, e **devloop**, un dispatcher di issue e PR su GitHub. #59 aveva previsto ssdlc come esecutore di un'unità di lavoro dentro devloop (decisione 2), ma non era mai stato realizzato. #4 proponeva il modello inverso.
- #126 ha chiesto se integrare i due o separarli, con un criterio decisivo: **niente review duplicate**, perché sarebbero puro spreco (muda).
- Due analisi indipendenti, di Codex e Antigravity, raccomandavano devloop come proprietario del flusso GitHub, ssdlc come esecutore esterno opzionale e repository separati.
- L'operatore ha indicato una terza strada (opzione C), qui decisa.

## Decisione (indirizzo dell'operatore)

1. ssdlc e devloop sono **un unico prodotto**, nello **stesso repository**.
2. **Gli output delle fasi di ssdlc si spostano sul tracker:** specifica, design, piano ed evidenze diventano issue e PR, non file sull'host della run. Cosa diventa pubblico dipende dalla politica di divulgazione, riservata all'operatore (vedi sotto).
3. **Il backend del tracker è una porta.** GitHub è il primo backend. In seguito ce ne potranno essere altri, compreso il filesystem per l'uso offline con ticket ed evidenze locali.
4. **Si accetta una perdita:** due run strutturate e separate sulla stessa issue con configurazioni diverse non sono più possibili. La perdita è accettabile solo se l'entità "tentativo" esiste prima della convergenza (vincolo 3).

## Opzioni considerate

| Opzione | Sintesi | Esito |
|---|---|---|
| A. ssdlc come esecutore esterno di devloop | devloop invoca ssdlc come comando, ssdlc restituisce commit ed Evidence Pack | Scartata dall'operatore: due macchine a stati e un contratto di stop da mantenere |
| B. Separazione dei repository | devloop in un repo dedicato, ssdlc invariato | Scartata dall'operatore: prodotto unico |
| **C. Prodotto unico, fasi sul tracker, backend come porta** | Indirizzo dell'operatore | **Scelta** |

## Esito del conclave (#126)

Il conclave ha votato all'unanimità **APPROVA, con condizioni vincolanti**:
- Codex;
- Claude;
- Antigravity.

Modelli, livelli di reasoning e modalità di ciascun voto sono riportati nelle intestazioni AGENTS.md dei rispettivi commenti su #126, che fanno da fonte. Il voto di Claude viene da una sessione `claude -p` separata da quella che ha proposto l'opzione.

I voti completi sono nei commenti di #126 e prevalgono su questa sintesi in caso di dubbio. Claude ha dichiarato che senza i vincoli 1 e 6 avrebbe votato BLOCCA.

### Vincoli

1. **Nessuna garanzia si toglie prima della parità.**
   - Il motore locale di ssdlc (`orchestrator.py`, journal, generazioni) resta invariato fino al passo 6.
   - A5 resta operativo dall'inizio. Il corpus di replay va portato sul nuovo nucleo **prima** della convergenza (passo 6), che ne è condizionata: i 12 casi devono passare **senza modifiche**, con prove aggiuntive del nuovo percorso, perché i casi attuali non dimostrano la correttezza del nuovo backend.
   - Grader e baseline non si toccano nella stessa PR che cambia il motore. Una nuova versione dell'oracolo A5 è una decisione dell'operatore.
2. **Budget e prenotazione prima di qualsiasi run reale con le fasi.**
   - Ogni azione costosa registra un intento durevole **prima** di chiamare il modello, con blocco sugli esiti incerti (`OUTCOME_UNCERTAIN`) e niente redispatch alla cieca dopo un crash o un timeout.
   - I budget restano cumulativi attraverso fasi, issue figlie, retry e riavvii: una nuova issue non azzera il budget della storia.
   - Consumi `null` o zero non dichiarati vengono rifiutati, e serve una politica dichiarata di prenotazione o di quota (`live_unknown_usage`, #43).
3. **Stato durevole, identità e pubblicazione affidabile.**
   - Prima di introdurre le fasi servono identità stabili di storia, fase e tentativo, le revisioni approvate e le dipendenze. Lo stato dei tentativi è durevole e gli esiti incerti si riconciliano.
   - Restano le garanzie del journal autorevole: gestione delle code incomplete, rifiuto degli snapshot senza journal.
   - L'esecutore salva su disco ricevuta e output **prima** di pubblicare sul tracker, e la pubblicazione è riprendibile, come la outbox di `ssdlc_feedback_delivery`.
   - Le unità generate da un piano portano un marker del dispatcher con la chiave `(sha del design approvato, task-id)`. Alla ripresa si riconcilia su quella chiave: niente duplicati né orfani.
4. **Perimetro.** Il controllo del manifest solo sul diff finale **non equivale** a `SCOPE_REQUIRED`. Un candidato fuori scope va bloccato **prima dell'adozione**, come fa oggi ssdlc. Il controllo deterministico, più `protected_paths`, avviene **prima della review del modello** e prima della promozione.
5. **Contenimento e fencing.**
   - Nessun redispatch senza la prova che i writer sono terminati: un timeout non è una prova.
   - Gli esecutori conservano la modalità Docker di ssdlc: sorgente in sola lettura, `.ssdlc` coperto da tmpfs in verifica, nessuna credenziale dell'host. Conservano anche la separazione delle credenziali e il controllo dell'installazione fissata e verificata.
   - Tutto questo è obbligatorio prima di eseguire più tick in parallelo e prima di affidare alle fasi esecuzioni che sostituiscono ssdlc. Riferimenti: #115–#117, #132.
6. **Il confine di #59 non si rompe in silenzio.** devloop oggi importa solo la stdlib e sé stesso (`tests/test_devloop_boundary.py`). L'operatore lo ha revocato in modo limitato il 2026-11-16 (decisione 1 della sezione "Decisioni riservate all'operatore"), alle condizioni dei voti:
   - nel passo delle fasi si riusano **solo validatori puri** (catalogo dei requisiti, validazione del piano), **senza** innestare cicli stateful (`DesignSession`, `orchestrator.py`) (Antigravity);
   - il codice condiviso va in un pacchetto `core` dichiarato e **aggiunto alla allowlist del test di confine**. **Mai** import diretti di `ssdlc_*`, che è il candidato sotto test del replay gate (Claude).
   - prompt e schemi si riusano come dati, copiati nell'installazione fissata. Era una proposta di Claude, approvata dall'operatore con la decisione 1.
7. **L'approvazione del piano genera issue proposte,** ma non ne autorizza l'esecuzione: `agent:ready` resta un atto dell'operatore.
8. **Evidenze.**
   - Artefatti completi, con digest e riferimenti durevoli.
   - Revisioni identificabili: un commento modificabile non vale come approvazione di contenuti cambiati dopo.
   - Il materiale pubblico va sanitizzato.
   - L'Evidence Pack lo produce il dispatcher in modo deterministico, ed è usabile solo sull'head a cui è legato.
   - **Catena fra fasi** (Antigravity): ogni fase produce un artefatto legato tramite hash nella propria ricevuta, e le fasi a valle verificano l'hash della ricevuta della fase a monte. Il solo Evidence Pack legato all'head non basta.
9. **Riuso delle verifiche.** Si riusano solo se input, commit, piano, ambiente e policy sono compatibili. Le verifiche toccate da una modifica vengono invalidate. Ricevute legate a base, head, policy e reviewer (`devloop/policy.py`). Finding con proprietari distinti (#110). Merge umano.
10. **CI e baseline.** Prima di introdurre filtri per percorso nella CI, `.github/workflows/test.yml` va tolto dagli elementi fissati, con una revisione esplicita della baseline autorizzata dall'operatore. Oggi è fissato in `ssdlc_grader.py` e nella baseline.
11. **Il risparmio va dimostrato.** Prima dell'adozione, la matrice di #126 va completata anche per recon, ops e documentazione, con chiamate e token misurati per fase.

## Piano (ordine rivisto dal conclave)

1. **Porte del tracker, solo refactoring** (vedi "Prima issue").
2. **Budget, prenotazione e stato durevole** (vincoli 2 e 3), sulla base di #133. Comprende l'identità di storia, fase e tentativo.
3. **Fasi come tipi di issue e PR, come percorso aggiuntivo.** Il motore locale resta operativo. Prima la PR di design per le storie con label, poi il piano con le issue proposte (vincoli 3 e 7), poi il controllo deterministico del perimetro (vincolo 4).
4. **Contenimento e fencing degli esecutori** (vincolo 5).
5. **Backend filesystem**, con identità firmate (vedi "Fiducia per backend").
6. **Convergenza del motore locale di ssdlc.** È condizionata al corpus A5 già portato e verde sul nuovo nucleo (vincolo 1). #4 si può chiudere **solo** dopo parità dimostrata, migrazione esplicita delle run e autorizzazione dell'operatore.

### Prima issue (passo 1)

È un refactoring puro di devloop:
- **Porte.** Le porte `Tracker`, `CodeReview` e `CI` contengono i soli metodi che il dispatcher usa oggi da `devloop/github.py`, con dati normalizzati (`author_login`, `merge_state`, `summarize_checks`). PR, review e check appartengono alle rispettive porte, anche se all'inizio una sola classe le implementa tutte.
- **Adattatori concreti.** `GitHub` e `Identities` restano gli adattatori.
- **Fake.** `FakeGH` va in `tests/helpers/` (Claude). `FakeGH`, `SnapshotGH` e `ActingAs` vengono adeguati alla porta (Antigravity). Un **test di conformità** verifica che fake e adattatore abbiano gli stessi metodi con le stesse firme. `FakeGH` resta un doppio in memoria, e non dimostra persistenza né atomicità (Codex).
- **Invariati:** marker, decisioni, identità, ricevute, limiti e comportamento dei tick. Testi e marker pubblicati restano identici byte per byte, la suite di devloop e il test di confine restano verdi, `config.toml` non cambia.
- **Verifiche di equivalenza:**
  - contenuti non fidati;
  - marker contraffatti;
  - modifiche successive all'autorizzazione;
  - riconciliazione dopo un crash.
- **Esclusi:** backend filesystem, riuso di codice di ssdlc, cambi alla CI o alla policy.

Tocca `devloop/`, quindi vale il livello 2: due reviewer.

## Porta del tracker

- **Nella porta:**
  - identificatori, revisioni e relazioni fra unità di lavoro (id, tipo, titolo, corpo, stato, dipendenze);
  - transizioni di stato astratte, non label;
  - storia degli eventi con provenienza verificabile (chi e quando per approvazioni, modifiche, rinomine), usata da `untrusted_change`;
  - commenti e marker con **autore verificato**;
  - esiti distinti per successo, conflitto ed esito incerto;
  - scelta dell'attore con permessi minimi (`DISPATCHER`, `IMPLEMENT`, `REVIEW`, `PR_COMMENT`, `ISSUE_COMMENT`);
  - una capacità dichiarata `identity_assurance`: `authenticated`, `signed` oppure `integrity_only`;
  - un contratto esplicito di conservazione dello stato e delle evidenze.
- **Nel nucleo:** autorizzazione, policy, semantica dei marker, dipendenze, invalidazione delle approvazioni, budget, journal e recupero. L'esecutore fornisce le prove di avvio, consumo e terminazione.
- **Nell'adattatore GitHub:**
  - `gh` con REST e GraphQL;
  - paginazione;
  - token delle App e JWT;
  - label e timeline;
  - codifica dei marker e la convenzione dell'intestazione `> [!NOTE]`;
  - `merge_state`, gli stati aggregati e le review native;
  - l'header HTTP per il push.

## Fiducia per backend

- **Tre cose distinte.** Firma verificata, identità dichiarata e autorità ad approvare non coincidono. Anche su GitHub l'autenticazione non concede autorità da sola: i marker valgono solo se li scrive l'attore previsto.
- **Filesystem:**
  - il journal con hash concatenati dà solo integrità, non provenienza (#3);
  - gli atti dell'operatore (l'equivalente di `agent:ready`, `/devloop rereview`, `/devloop finding`) sono **commit o tag firmati** SSH o GPG. Si verificano con chiavi autorizzate e associate ai ruoli, in un `allowed_signers` che sta nell'**installazione fissata**, mai nel repository su cui lavorano gli agent;
  - le chiavi stanno fuori da ogni sandbox;
  - l'approvazione è legata a repository, ticket (hash di titolo e corpo), fase, tentativo, SHA, digest, policy e sequenza monotona. C'è un'ancora esterna contro rollback e replay, perché una storia vecchia ma firmata validamente si potrebbe ripristinare;
  - gli agent non hanno chiavi. È il dispatcher a firmare gli output validati per conto dei ruoli. Sullo stesso host, l'indipendenza fra implementer e reviewer è quindi attestata dal dispatcher, non dalla crittografia, e va dichiarato;
  - senza chiavi configurate, la ricevuta dichiara `trust_mode: integrity_only`. Un backend `integrity_only` **non può promuovere**: produce al massimo bozze locali. Un profilo di fiducia più debole è ammesso solo per scelta esplicita dell'operatore, riconoscibile nelle evidenze.

## Divisione delle review fra le fasi

Principio condiviso dai tre voti: **ogni fase ha un solo gate di review**, con un ambito distinto. Le fasi a valle non ri-revisionano decisioni già approvate. Per Claude si tiene doppio solo il secondo reviewer di livello 2, come difesa indipendente voluta. Per Codex vanno conservati l'indipendenza e il **numero di reviewer richiesti dal rischio**. Sui dettagli i voti divergono, e le posizioni sono riportate separatamente. La scelta finale si fa nell'issue del passo 3.

| Fase | Codex | Claude | Antigravity |
|---|---|---|---|
| Specifica | Review: requisiti verificabili, criteri di accettazione e di abuso | Nessuna review di modello: approvazione dell'operatore | Non specificato |
| Design | Architettura, STRIDE, mitigazioni | Un reviewer di modello diverso, ambito ADR e STRIDE, l'unico punto di review della sicurezza del design | Gate unico design+piano per storie M/L o con label, omesso per S |
| Piano | Copertura dei requisiti, DAG, manifest, verifiche | Review del piano (DAG, copertura) e perimetro deterministico nel dispatcher | Nel gate unico |
| Implementazione (PR) | Un'unica review del codice: correttezza, regressioni, conformità al piano. `04-review` confluisce qui | Una sola review della PR rispetto al task, agli AC e all'estratto ADR dal commit approvato. `04-review` sparisce | Solo conformità ad ADR, `AC-*` e `SEC-*` e assenza di bug e di vulnerabilità nel diff, dopo i controlli deterministici |
| QA | **Si conserva** la verifica dei criteri di accettazione, riusando i risultati deterministici validi della CI | Test, segreti e SCA in CI, senza modelli | Controlli deterministici in CI al posto della chiamata LLM di `05-qa` |
| AppSec | **Si conserva** la verifica delle mitigazioni e dei controlli di sicurezza previsti dal rischio | Modello solo a `reviews:2` o sui percorsi sensibili | Secondo reviewer con focus sicurezza al livello 2 (`reviews:2`, percorsi sensibili oppure tier `L`), con la promozione vincolata alla ricevuta |
| Supervisione | Completezza e provenienza delle evidenze, merge umano | Evidence Pack deterministico del dispatcher | Ricevuta legata all'hash |

Ops e documentazione diventano criteri dei task (Claude). Fermarsi sempre a `03-dev` perderebbe QA e AppSec (Codex).

## Cosa si perde e come si recupera

| Garanzia di ssdlc | Come si conserva |
|---|---|
| Recupero dai crash, journal, generazioni | Motore locale invariato fino al passo 6. Stato durevole dei tentativi, intenti, pubblicazione riprendibile (vincoli 2 e 3) |
| Fencing dei writer | #115–#117 e #132. Nessun redispatch senza prova di terminazione (vincolo 5) |
| Replay e benchmark A5 | Invariati, poi portati sul nuovo nucleo con prove aggiuntive (vincolo 1) |
| Contenimento | Modalità Docker degli esecutori (vincolo 5, passo 4) |
| Budget e costi ignoti | Nel nucleo, `LimitsStore` e `live_unknown_usage` (#43, #128). Consumi non dichiarati rifiutati (vincolo 2) |
| Perimetro (`SCOPE_REQUIRED`) | Blocco del candidato fuori scope prima dell'adozione (vincolo 4) |
| Più tentativi sulla stessa issue | Entità "tentativo" prima della convergenza (vincolo 3). Come rappresentarli, per esempio branch e PR per variante, è una proposta ulteriore da decidere al passo 2 |

## Decisioni riservate all'operatore

Decise dall'operatore il 2026-11-16, accettando le raccomandazioni proposte in sessione:

1. **Confine di #59: revocato in modo limitato.** devloop può riusare codice di ssdlc **solo** attraverso un pacchetto `core` dichiarato, aggiunto alla allowlist di `tests/test_devloop_boundary.py`. Valgono queste condizioni:
   - solo validatori puri, senza cicli stateful (`DesignSession`, `orchestrator.py`);
   - prompt e schemi riusati come dati;
   - **mai** import diretti di `ssdlc_*`.
2. **Divulgazione.** Threat model e finding AppSec su vulnerabilità **non ancora corrette** restano **locali per default**. Sul tracker pubblico vanno solo un riepilogo redatto, l'hash dell'artefatto e lo stato. Il testo completo diventa pubblico solo dopo il merge della correzione.
3. **Baseline del replay gate.** Lo scorporo di `.github/workflows/test.yml` dagli elementi fissati è **approvato in linea di principio**. Si farà come revisione `i2-fixtures-v6` in una PR dedicata, insieme al lavoro sui filtri della CI. Ogni nuova versione dell'oracolo A5 resta una decisione da prendere caso per caso.
4. **Attivazione, migrazione e #4: non ora.** Se ne riparla dopo la parità, al passo 6. #4 resta aperta come assorbita da questo ADR.
5. **Estensioni di budget e di round.** Restano dell'operatore, **caso per caso**, senza delega.
6. **Merge e label di attivazione e di riapertura.** Restano atti dell'operatore, come oggi.
7. **Profilo di fiducia più debole sul backend filesystem: no**, per ora.

**Chiarimento sul vincolo 1.** "Motore locale invariato fino al passo 6" significa che le sue garanzie **non vengono tolte né sostituite** prima della parità. Non congela lo sviluppo: correzioni e miglioramenti del motore locale (per esempio #115–#117, #118–#120, #128, #72) proseguono normalmente.

## Precedenti

Le fasi di design e piano sono già state eseguite a mano il 2026-11-15:
- il conclave su #45, da cui sono nate #115, #116 e #117;
- il conclave su #93, da cui sono nate #118, #119 e #120;
- le divisioni di #66 (#106, #110, #111, #107) e di #133 (#133, #134, #135) dopo i controlli di prontezza.
