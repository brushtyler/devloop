# Migrazione su un'altra macchina

Spostare devloop e il supervisore da una VM vecchia a una nuova. I percorsi sono quelli di
`params.env` (qui con i valori dell'installazione attuale: utenti `devloop` e `supervisor`,
clone in `/home/devloop/ssdlc`, `state_dir` = `.ssdlc/devloop`).

**Regola:** mai due dispatcher attivi sullo stesso repository. Il dispatcher nuovo parte solo
quando quello vecchio è fermo e non ripartirà.

## Ordine

1. **Preparare la macchina nuova** senza avviare nulla: `setup-devloop.sh`,
   `setup-agent-user.sh` e `setup-supervisor.sh` ([ops/README.md](../README.md)). Il servizio viene installato ma non
   avviato.
2. **Fermare devloop sulla macchina vecchia** e attendere la fine delle azioni in corso:

   ```bash
   sudo systemctl stop devloop        # SIGTERM al solo processo principale (KillMode=mixed)
   systemctl is-active devloop        # "inactive": il loop è uscito
   sudo systemctl disable devloop     # non deve ripartire a un riavvio della VM vecchia
   ```

   `stop` ritorna quando il loop è uscito: devloop smette di pianificare e aspetta le azioni in
   corso, al più `agent_timeout_seconds` (`TimeoutStopSec=4200`). Se l'unità non c'è (loop
   avviato a mano), creare il file `STOP` nello `state_dir` e attendere che
   `pgrep -u devloop -af 'm devloop'` non trovi più nulla.
3. **Copiare** i file elencati sotto, conservando proprietario e permessi.
4. **Ricreare** quello che non si copia (sotto).
5. **Verificare** con `sudo bash ops/host/check-host.sh` e `devloop-pinned status`, poi **avviare** sulla
   macchina nuova: `sudo systemctl start devloop` (e `enable`, se deve ripartire al boot).

## Da copiare

Dallo `state_dir` di devloop (`/home/devloop/ssdlc/.ssdlc/devloop`), come utente `devloop`:

| File | Cos'è |
|---|---|
| `turn-journal.jsonl`, `turn-journal-state.json` | il journal dei turni (#362): intenti ed esiti dei turni, prenotazioni del budget |
| `ledger-open-turns.json` | la traccia dei turni aperti che il journal sostituisce, se c'è ancora |
| `model-unavailability.json` | la memoria dei modelli trovati non disponibili |
| `activity.json` | le azioni del tick in corso, per `status`: dopo un arresto pulito restano quelle interrotte, se ce ne sono |
| `config.local.toml` | l'overlay dell'operatore |
| `project.toml` | il file di progetto di ssdlc (ADR 0006, R3): senza, devloop non parte |

Per esempio, dalla macchina nuova:

```bash
sudo -u devloop -H rsync -a --chmod=F600 \
    vecchia:/home/devloop/ssdlc/.ssdlc/devloop/{turn-journal.jsonl,turn-journal-state.json,ledger-open-turns.json,model-unavailability.json,activity.json,config.local.toml,project.toml} \
    /home/devloop/ssdlc/.ssdlc/devloop/
```

Un file che sulla macchina vecchia non c'è non va creato.

Dal supervisore, come utente `supervisor`:

- la directory degli strumenti e dei file del diario, `~/supervisor`: sono strumenti locali del
  supervisore, non di devloop, e non si installano dal repository;
- la memoria di Claude Code, `~/.claude/projects/<progetto>/memory`. `<progetto>` è il percorso
  del clone del supervisore con `/` sostituito da `-` (per esempio `-home-supervisor-ssdlc`): se il
  clone nuovo ha lo stesso percorso il nome resta valido, altrimenti va rinominata.

## Da ricreare

- **Le copie fissate** (`pinned-*` e `current`): non si copiano. Al primo avvio si fa un nuovo pin
  del branch principale, che rifà anche lo smoke test sulla macchina nuova. Lo esegue il devloop
  del clone: il file di progetto e l'overlay copiati sopra si passano prima del comando, come fa
  il launcher (senza file di progetto devloop non parte, ADR 0006, R3):

  ```bash
  sudo -u devloop -H sh -c 'cd ~/ssdlc && python3 -m devloop \
      --project-config .ssdlc/devloop/project.toml \
      --local-config .ssdlc/devloop/config.local.toml pin origin/main'
  ```

- **I worktree** (`worktrees/`): non si copiano; devloop li ricrea dai branch su GitHub quando
  riprende un'azione.
- **L'utente degli agent** (`devloop-agent`, #415): lo prepara `setup-agent-user.sh`, con le ACL
  sullo `state_dir` e il suo sudoers. Va rieseguito **dopo** la copia dei file dello `state_dir`:
  dà a ogni voce già presente l'ACL che la chiude a `devloop-agent`.
- **I login delle CLI** (`claude`, `codex`, `agy` per `devloop-agent`; `claude` per
  `supervisor`): interattivi, uno per CLI ([ops/README.md](../README.md#5-utente-degli-agent-e-cli)).
- **Il token di sola lettura del supervisore**: uno nuovo
  ([ops/github/tokens.md](../github/tokens.md)), e si revoca quello della macchina vecchia.
- **Il servizio**: lo installa `setup-devloop.sh` da `ops/host/devloop.service`; non si copia
  l'unità della macchina vecchia.
- **Il sudoers del supervisore**: lo installa `setup-supervisor.sh`.

## Chiavi delle GitHub App

Due modi:

- **meglio:** generare chiavi nuove dalle impostazioni di ciascuna app (*Settings → Developer
  settings → GitHub Apps → \<app\> → Generate a private key*), metterle nella `key_dir` della
  macchina nuova (mode 600) e, quando la macchina nuova funziona, **revocare** le vecchie dalla
  stessa pagina (*Delete* accanto alla chiave). devloop usa la chiave più recente di ogni app;
- **altrimenti:** trasferire le chiavi esistenti su un canale sicuro (per esempio `scp` fra le due
  macchine, mai via email, chat o repository), con mode 600 all'arrivo, e cancellarle dalla
  macchina vecchia quando non serve più.

In entrambi i casi: in `key_dir` di `devloop` le chiavi delle app che usa il dispatcher; in quella
del supervisore solo la chiave `supervisor` e, finché P5-4 non è attivo, `claude-agent`.
`check-host.sh` lo verifica.

## Dopo

- La macchina vecchia: servizio disabilitato, chiavi e token revocati o cancellati, login delle
  CLI chiusi.
- Se cambia l'host, cambia anche il nome suggerito del token del supervisore: rinominarlo.

## Configurazione di ssdlc nel suo file di progetto

Con l'ADR 0006, R3, `devloop/config.toml` non contiene più i fatti di ssdlc né le restrizioni che
aggiunge (`test_cmd`, `local_tests`, `language`, `priority_labels`, `[docs.map]`,
`protected_paths`, `blocked_labels`, `sensitive_paths`, `sensitive_path_lists`,
`operator_only_paths`): stanno nel file di progetto sull'host, `<state_dir>/project.toml`, di cui
[`ops/host/ssdlc/project.toml`](ssdlc/project.toml) è l'esempio. I comandi di agy derivati dai
test di ssdlc passano all'overlay ([`ops/host/ssdlc/overlay.toml`](ssdlc/overlay.toml)). `repo`,
`trusted_users` e `[apps]` restano in `config.toml` fino a R6 (#425). Senza file di progetto la
copia di R3 non parte e il suo pin fallisce: i passi sull'host vanno fatti **prima** del pin
della copia di R3, nell'ordine di [Migrazione di R3 su un host
attivo](#migrazione-di-r3-su-un-host-attivo).

**Dopo.** I fatti di ssdlc si cambiano sull'host: una PR aggiorna l'esempio
`ops/host/ssdlc/project.toml` (per esempio una voce nuova di `[docs.map]`) e, dopo il merge,
l'operatore copia il file sull'host come al passo 3 di [Migrazione di R3 su un host
attivo](#migrazione-di-r3-su-un-host-attivo). Una PR su `devloop/config.toml` cambia solo i
default dell'installazione.

## Migrazione di R3 su un host attivo

Il passaggio di un host già in servizio alla copia di R3 (ADR 0006): il file di progetto nello
`state_dir`, le chiavi di [`ops/host/ssdlc/overlay.toml`](ssdlc/overlay.toml) nell'overlay, la
verifica con [`ops/host/ssdlc/check-equivalence.py`](ssdlc/check-equivalence.py), poi il pin.
I percorsi sono quelli dell'installazione attuale: `state_dir` di devloop
`/home/devloop/ssdlc/.ssdlc/devloop`, checkout dell'operatore `/root/repos/ssdlc`, checkout
aggiornato `/tmp/ssdlc-r3`.

### Ordine

Il file di progetto e le chiavi dell'overlay vanno sull'host **prima** che devloop prenda la
copia di R3, e il loop va riavviato perché li veda: il launcher passa `--project-config` solo
all'avvio, e un pin dopo un merge riparte con gli stessi argomenti. Altrimenti, con
`[pin] auto = true`, il primo tick dopo il merge fissa `origin/main`, il controllo della copia
fallisce (`config: missing 'test_cmd'`) e dopo tre tick falliti `devloop run` si ferma con il
codice 4, che systemd non riavvia (`RestartPreventExitStatus=4`).

- **Prima del merge della PR di R3** (meglio): i passi 1–5 con il checkout della testa della PR,
  l'aggiornamento del clone di devloop del passo 6, poi `sudo systemctl restart devloop`, poi il
  merge. La copia di prima legge già il file di
  progetto (R2) e con le chiavi dell'overlay dà gli stessi valori; il pin dopo il merge (auto, o
  il passo 6 a mano) trova il file di progetto.
- **Dopo il merge**: fermare il loop **prima** del merge (`sudo systemctl stop devloop`), così
  nessun tick prova il pin; poi i passi 1–6 con `origin/main` e `sudo systemctl start devloop`.
- **Se il loop si è già fermato** (il log dice `auto-pin of origin/main failed, the pinned copy is
  unchanged: … missing 'test_cmd'` e poi `ALERT: 3 consecutive ticks failed`): `current` è
  ancora la copia di prima e la richiesta di pin resta. I passi 1–6 con `origin/main`, poi
  `sudo systemctl start devloop`. Non serve altro: il pin a mano del passo 6 lascia cadere la
  richiesta rimasta.

In ogni caso l'hash del file di progetto entra nella versione della policy: le approvazioni delle
PR aperte decadono e serve una nuova review. Meglio con la coda vuota.

### Comandi

Tutti **come root**, con il checkout dell'operatore. `check-equivalence.py` legge e non scrive:
come root legge lo `state_dir` di `devloop` e la copia fissata, e il checkout aggiornato è di root
(`git` lo accetta). Come `devloop` dal checkout dell'operatore fallisce con `Permission denied`:
`devloop` non entra in `/root`.

1. **Il checkout aggiornato**, in una directory a sé (il checkout dell'operatore non cambia).
   Dopo il merge:

   ```bash
   cd /root/repos/ssdlc && git fetch origin
   git worktree add --detach /tmp/ssdlc-r3 origin/main
   ```

   Prima del merge, la testa della PR (`<PR>` è il suo numero):

   ```bash
   cd /root/repos/ssdlc && git fetch origin pull/<PR>/head
   git worktree add --detach /tmp/ssdlc-r3 FETCH_HEAD
   ```

2. **Copia dell'overlay di prima**, per il confronto, una volta sola: se c'è già, è quella di
   prima e non va sovrascritta.

   ```bash
   test -e /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml.prima-r3 || \
       install -o devloop -m 600 /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml \
           /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml.prima-r3
   ```

   Senza overlay non si crea nulla e il confronto si fa senza `--before-local`.
3. **Il file di progetto**, dal checkout aggiornato, di `devloop` e con mode 600:

   ```bash
   install -o devloop -m 600 /tmp/ssdlc-r3/ops/host/ssdlc/project.toml \
       /home/devloop/ssdlc/.ssdlc/devloop/project.toml
   ```

   Deve essere un file regolare a sé: né un link né un hard link al file del repository.
4. **L'overlay** riceve le chiavi di `/tmp/ssdlc-r3/ops/host/ssdlc/overlay.toml`
   (`write_commands` e `review_commands` di `[agents.agy]`), accanto a quelle che ha già. Se
   l'overlay ha già `[agents.agy]`, le due chiavi vanno in quella tabella. Si modifica come
   `devloop`, perché resti suo:

   ```bash
   sudo -u devloop -H "${EDITOR:-vi}" /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
   ```

5. **Verifica dell'equivalenza**: prima la copia fissata in uso con l'overlay di prima, dopo il
   checkout aggiornato con l'overlay e il file di progetto nuovi:

   ```bash
   python3 /tmp/ssdlc-r3/ops/host/ssdlc/check-equivalence.py \
       --before /home/devloop/ssdlc/.ssdlc/devloop/current \
       --before-local /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml.prima-r3 \
       --after /tmp/ssdlc-r3 \
       --after-local /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml \
       --after-project /home/devloop/ssdlc/.ssdlc/devloop/project.toml
   ```

   Confronta la configurazione effettiva chiave per chiave, i prompt resi e i moduli di test
   selezionati per ogni file del checkout aggiornato (`--root`, per default il checkout dello
   script). Lascia fuori, dichiarandolo, l'hash del file di progetto (`review_policy.project`),
   che R2 mette nella versione della policy: la versione si confronta senza. Il codice di uscita:

   | Codice | Significato | Cosa fare |
   |---|---|---|
   | 0 | `equivalent: …`: nessuna differenza | avanti: riavvio prima del merge, o il passo 6 |
   | 1 | una riga per differenza, poi `N difference(s): the migration is not equivalent` | niente merge né pin finché non si è capito perché. Di solito: le chiavi del passo 4 mancanti (`config agents.agy.…`), una chiave dell'overlay che sostituisce un valore del file di progetto, un file di progetto copiato da un altro commit. Correggere e rilanciare |
   | 2 | un lato non si carica, o un argomento è sbagliato: il messaggio dice quale (`not an installation of devloop`, `the configuration does not load: …`, `not a file`) | correggere il percorso o il file indicato (per esempio un file di progetto link, con più hard link o con un errore di sintassi) e rilanciare; il confronto non è stato fatto |

6. **Clone di devloop, pin e riavvio.** Prima il clone di devloop, aggiornato con un
   fast-forward e come `devloop`, perché resti suo; sempre, anche prima del merge, e **prima** del
   pin e del riavvio:

   ```bash
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u devloop -H git -C /home/devloop/ssdlc merge --ff-only origin/main
   ```

   Il launcher `scripts/devloop-pinned` viene dal clone, non dalla copia fissata: è lui che passa
   `--project-config` alla copia, e quello di un clone vecchio non lo passa, così pin e loop
   partono senza file di progetto anche dopo i passi 1–5. Se il fast-forward fallisce, il clone ha
   commit o modifiche locali: vanno capite prima di andare avanti.

   Prima del merge basta poi `sudo systemctl restart devloop`. Dopo il merge, il pin e il
   riavvio:

   ```bash
   sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin origin/main
   sudo systemctl start devloop      # restart, se il loop è in esecuzione
   ```

   **Quando la copia in uso è precedente a R3**, il pin si fa invece con il codice nuovo, dal
   clone aggiornato, passando file di progetto e overlay:

   ```bash
   cd /home/devloop/ssdlc && sudo -u devloop -H python3 -m devloop \
       --project-config /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
       --local-config /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml pin origin/main
   sudo systemctl start devloop      # restart, se il loop è in esecuzione
   ```

   `devloop-pinned pin` non basta perché il pin lo fa la copia in uso: il launcher esegue la copia
   fissata, che controlla la copia nuova con gli argomenti che conosce lei. Una copia precedente a
   R2 non passa il file di progetto al controllo della copia nuova, che la rifiuta (`config:
   missing 'test_cmd'`). `python3 -m devloop` dal clone esegue invece il codice del clone appena
   aggiornato. I pin successivi tornano al launcher.

   Lo smoke test del pin usa il file di progetto. Un loop in corso legge un file di progetto
   nuovo solo al riavvio.
7. **Pulizia**: `git -C /root/repos/ssdlc worktree remove /tmp/ssdlc-r3`. La copia
   `config.local.toml.prima-r3` si cancella quando la copia di R3 gira.

**Rollback.** Il pin della copia di prima (`scripts/devloop-pinned pin <sha12>`) basta: con la
copia di prima il file di progetto e le chiavi dell'overlay danno gli stessi valori. Si possono
anche togliere, e la versione della policy torna quella di prima.

### Ogni cambio con un passo sull'host

Una PR che richiede un passo sull'host (un file nello `state_dir`, una chiave dell'overlay, un
comando di root) dice nella descrizione e nella guida di `ops/` **che cosa fare prima del merge**,
con i comandi completi, l'utente che li esegue e cosa fare se devloop ha già preso la copia nuova.

Se la PR cambia il launcher (`scripts/devloop-pinned`) o ciò che il pin deve passare alla copia
nuova (un argomento, un file dello `state_dir`), la descrizione dice anche di aggiornare il clone
di devloop prima del pin e del riavvio e di fare il pin con il codice nuovo, dal clone aggiornato,
come al passo 6: la copia in uso non sa passare alla copia nuova ciò che ancora non conosce.

Dal #462 il passo è anche dichiarato nell'output strutturato dell'implementer (`host_step`: un
testo breve con il rimando a questa guida), e il reviewer controlla che ci sia. devloop mergia la
PR come le altre, ma con `[pin] auto = true` **non fa il pin automatico** di quel commit né dei
successivi: scrive un allarme con il passo su `[run].alert_issue` e mette `agent:needs-operator`
sull'issue chiusa. Dopo il passo, a loop fermo, il pin a mano (`scripts/devloop-pinned pin
origin/main`, oppure il pin con il codice nuovo come nelle sezioni qui sotto) fissa la copia e
fa riprendere il pin automatico ([docs/devloop/guide.md](../../docs/devloop/guide.md#step-on-the-host)).
All'avvio, e a ogni rilettura, devloop scrive nel log, e una volta su `[run].alert_issue`, le
tabelle del file di progetto che la copia in uso conosce e che la configurazione non ha
(`[prompts]`, `[tests]`).

## Dati di ssdlc nei prompt

Con l'ADR 0006, R4, i prompt di `devloop/prompts/` non contengono più il testo di ssdlc: le
istruzioni da leggere (`AGENTS.md`, la sezione "Developing SSDLC" del README), il comando di un
solo modulo di test e le regole sugli ADR del preflight stanno nella tabella `[prompts]` del file
di progetto ([docs/devloop/reference.md](../../docs/devloop/reference.md#project-data-in-the-prompts)).
Senza la tabella la copia di R4 parte, ma i prompt perdono quelle frasi; con la tabella la copia
di R3 non parte (chiave sconosciuta). Il file di progetto sull'host e la copia fissata cambiano
quindi insieme, a loop fermo. Sono passi dell'operatore, dopo il merge della PR di R4.

Il pin della copia di R4 non passa da `scripts/devloop-pinned`: il launcher esegue la copia
fissata, che carica la configurazione con il file di progetto prima di aggiornare il pin, e la
copia di prima rifiuta `[prompts]` (`project file: unknown key 'prompts'`). Il pin si fa una
volta con il codice di R4, da un checkout del suo commit, passando file di progetto e overlay.
I pin successivi tornano al launcher.

Se l'host non ha ancora fatto la migrazione di R3, basta quella della sezione precedente con
l'esempio di R4: il passo 3 copia anche `[prompts]`, e il passo 5 confronta anche i prompt resi.
Il passo 6, però, non usa `devloop-pinned`: il pin si fa come al passo 5 qui sotto, come utente
`devloop` e con un checkout di R4 del suo clone (`git -C /home/devloop/ssdlc worktree add
--detach /tmp/ssdlc-r4 origin/main`, dopo `git -C /home/devloop/ssdlc fetch origin`; il
checkout di root in `/tmp/ssdlc-r3` non è di `devloop`), poi `sudo systemctl start devloop`.
Per il rollback, come qui sotto, prima si toglie `[prompts]` (con `[prompts.adr]`) dal file di
progetto, poi si fa il pin della copia di prima.
Altrimenti, come utente `devloop`, dal clone:

1. **Quando.** L'hash del file di progetto cambia: le approvazioni delle PR aperte decadono.
   Meglio con la coda vuota. Fermare il loop (`sudo systemctl stop devloop`): una copia di R3 in
   corso rifiuterebbe il file nuovo a ogni rilettura.
2. **Copia del file di progetto di prima**, per il confronto e il rollback:

   ```bash
   cd ~/ssdlc && git fetch origin
   cp -p .ssdlc/devloop/project.toml .ssdlc/devloop/project.toml.prima-r4
   ```

3. **La tabella `[prompts]`** (con `[prompts.adr]`) dell'esempio di R4 su `origin/main`
   (`git show origin/main:ops/host/ssdlc/project.toml`) va aggiunta al file di progetto
   sull'host. Se il file sull'host è ancora l'esempio di R3 senza modifiche, si può copiare
   l'esempio intero come al passo 3 della sezione precedente.
4. **Verifica dell'equivalenza**, con la copia fissata di R3 e un checkout del commit di R4:

   ```bash
   git worktree add --detach /tmp/ssdlc-r4 origin/main
   python3 /tmp/ssdlc-r4/ops/host/ssdlc/check-equivalence.py \
       --before .ssdlc/devloop/current --before-local .ssdlc/devloop/config.local.toml \
       --before-project .ssdlc/devloop/project.toml.prima-r4 \
       --after /tmp/ssdlc-r4 --after-local .ssdlc/devloop/config.local.toml \
       --after-project .ssdlc/devloop/project.toml
   ```

   Lascia fuori, dichiarandoli, l'hash del file di progetto e i valori di `[prompts]`, che prima
   di R4 erano testo dei modelli: al loro posto confronta i prompt resi, che devono essere
   identici. Esce con 0 ed `equivalent: …`; altrimenti il pin non va fatto. Il checkout
   `/tmp/ssdlc-r4` resta fino alla fine del passo 5.
5. **Pin con il codice di R4**, dal clone e con il checkout del passo 4 (non con
   `devloop-pinned`, che eseguirebbe la copia di R3):

   ```bash
   cd /home/devloop/ssdlc
   PYTHONPATH=/tmp/ssdlc-r4 python3 -P -m devloop \
       --project-config /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
       --local-config /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml pin origin/main
   git worktree remove /tmp/ssdlc-r4
   sudo systemctl start devloop
   ```

   Se il pin fallisce (`pin failed, the pinned copy is unchanged`), `current` è ancora la copia
   di R3: prima di avviare il loop si rimette il file di progetto di prima (rollback qui sotto).

**Rollback.** Prima il file di progetto di prima, poi il pin della copia di R3, nell'ordine: la
copia di R4 accetta il file senza `[prompts]`, mentre quella di R3 rifiuta il file con
`[prompts]`, e il pin la verifica con il file di progetto dell'host.

```bash
sudo systemctl stop devloop
cd /home/devloop/ssdlc
cp -p .ssdlc/devloop/project.toml.prima-r4 .ssdlc/devloop/project.toml
python3 scripts/devloop-pinned pin <sha12>
sudo systemctl start devloop
```

`<sha12>` è la copia di R3 (`pinned-<sha12>` nello `state_dir`).

## Test di ssdlc nel file di progetto

Con l'ADR 0006, R5, le regole con cui devloop sceglie i moduli di test di `local_tests =
"changed"` (`ssdlc_<nome>.py` → `test_<nome>*.py`, `devloop/` → `test_devloop*.py`) e il formato
dei log della CI (le righe di `scripts/run-tests.py`) non stanno più nel codice: sono la tabella
`[tests]` del file di progetto
([docs/devloop/reference.md](../../docs/devloop/reference.md#local-tests)). Le due copie non
accettano lo stesso file:

- la copia di R5 **senza** `[tests]` parte, ma `local_tests = "changed"` vale `"full"` (ogni
  turno di scrittura esegue l'intera suite) e `ci_rerun_unrelated` è spento;
- la copia di prima **con** `[tests]` non parte: la chiave è sconosciuta.

Il file di progetto sull'host e la copia fissata cambiano quindi insieme, a loop fermo.

Il pin della copia di R5 non passa da `scripts/devloop-pinned`: il launcher esegue la copia
fissata, che carica la configurazione con il file di progetto prima di aggiornare il pin, e la
copia di prima rifiuta `[tests]` (`project file: unknown key 'tests'`). Il pin si fa una volta con
il codice di R5, da un checkout del suo commit, passando file di progetto e overlay (passo 5 qui
sotto). I pin successivi tornano al launcher.

Se l'host non ha ancora fatto la migrazione di R3, basta quella di [Migrazione di R3 su un host
attivo](#migrazione-di-r3-su-un-host-attivo) con l'esempio di R5, nel caso "Dopo il merge" (loop
fermo): la copia di prima non accetterebbe il file nuovo a un riavvio. Il passo 6, però, non usa
`devloop-pinned`: il pin si fa come al passo 5 qui sotto, come utente `devloop` e con un checkout
di R5 del suo clone, poi `sudo systemctl start devloop`. Per il rollback, come qui sotto, prima si
tolgono dal file di progetto le tabelle che la copia di prima non conosce (`[tests]` con
`[tests.dir_modules]`, e `[prompts]` con `[prompts.adr]` se è anteriore a R4), poi si fa il pin
della copia di prima. Altrimenti, dopo il
merge della PR di R5, **come root** con il checkout dell'operatore, come per R3:

1. **Fermare il loop**: `sudo systemctl stop devloop`. Una copia di prima in corso rifiuterebbe
   il file nuovo a ogni rilettura, e un pin automatico di R5 con il file vecchio passerebbe a
   `"full"`.
2. **Il checkout aggiornato** e la **copia del file di progetto di prima**, per il confronto e il
   rollback:

   ```bash
   cd /root/repos/ssdlc && git fetch origin
   git worktree add --detach /tmp/ssdlc-r5 origin/main
   install -o devloop -m 600 /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
       /home/devloop/ssdlc/.ssdlc/devloop/project.toml.prima-r5
   ```

3. **La tabella `[tests]`** (con `[tests.dir_modules]`) di
   `/tmp/ssdlc-r5/ops/host/ssdlc/project.toml` va aggiunta al file di progetto sull'host, come
   `devloop`: `sudo -u devloop -H "${EDITOR:-vi}" /home/devloop/ssdlc/.ssdlc/devloop/project.toml`.
   Se il file sull'host è ancora l'esempio di R3 senza modifiche, si può copiare l'esempio intero
   come al passo 3 della migrazione di R3.
4. **Verifica dell'equivalenza**: prima la copia fissata in uso con il file di progetto di prima,
   dopo il checkout aggiornato con quello nuovo, lo stesso overlay sui due lati:

   ```bash
   python3 /tmp/ssdlc-r5/ops/host/ssdlc/check-equivalence.py \
       --before /home/devloop/ssdlc/.ssdlc/devloop/current \
       --before-local /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml \
       --before-project /home/devloop/ssdlc/.ssdlc/devloop/project.toml.prima-r5 \
       --after /tmp/ssdlc-r5 \
       --after-local /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml \
       --after-project /home/devloop/ssdlc/.ssdlc/devloop/project.toml
   ```

   Per la copia di prima, che aveva le regole nel codice, i valori di `tests` sono quelli che il
   suo codice applicava, e i moduli li sceglie il suo codice: per ogni file del checkout
   aggiornato devono essere gli stessi. I codici di uscita sono quelli della migrazione di R3. Con
   `[tests]` mancante o diversa le righe dicono `local_tests: before changed, after full` o
   `config tests.…`.
5. **Pin con il codice di R5**, non con `devloop-pinned`, che eseguirebbe la copia di prima. Il
   pin gira come `devloop`, dal suo clone e con un checkout di R5 suo: il checkout di root in
   `/tmp/ssdlc-r5` non è di `devloop`. Come utente `devloop` (`sudo -u devloop -i`):

   ```bash
   cd /home/devloop/ssdlc && git fetch origin
   git worktree add --detach /tmp/ssdlc-r5-pin origin/main
   git -C /tmp/ssdlc-r5-pin rev-parse HEAD
   PYTHONPATH=/tmp/ssdlc-r5-pin python3 -P -m devloop \
       --project-config /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
       --local-config /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml pin origin/main
   git worktree remove /tmp/ssdlc-r5-pin
   ```

   L'hash stampato da `rev-parse` deve essere quello del checkout verificato al passo 4
   (`git -C /tmp/ssdlc-r5 rev-parse HEAD`, come root): se `origin/main` è andato avanti, si
   rifà il passo 4 sul commit nuovo prima del pin. Solo se il pin riesce, di nuovo come root:
   `sudo systemctl start devloop`. Se il pin fallisce (`pin failed, the pinned copy is
   unchanged`), `current` è ancora la copia di prima: prima di avviare il loop si rimette il file
   di progetto di prima (rollback qui sotto, senza il pin).
6. **Pulizia**: `git -C /root/repos/ssdlc worktree remove /tmp/ssdlc-r5`. La copia
   `project.toml.prima-r5` si cancella quando la copia di R5 gira.

L'hash del file di progetto cambia ed entra nella versione della policy: le approvazioni delle PR
aperte decadono. Meglio con la coda vuota.

**Rollback.** Prima il file di progetto di prima, poi il pin della copia di prima, nell'ordine: la
copia di R5 accetta il file senza `[tests]`, mentre quella di prima rifiuta il file con `[tests]`,
e il pin la verifica con il file di progetto dell'host. Come utente `devloop`, con loop fermo
(`sudo systemctl stop devloop`, come root):

```bash
cd /home/devloop/ssdlc
cp -p .ssdlc/devloop/project.toml.prima-r5 .ssdlc/devloop/project.toml
python3 scripts/devloop-pinned pin <sha12>
```

Poi, come root, `sudo systemctl start devloop`. `<sha12>` è la copia di prima (`pinned-<sha12>`
nello `state_dir`).
