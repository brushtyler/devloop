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
  poi `sudo systemctl restart devloop`, poi il merge. La copia di prima legge già il file di
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

6. **Pin e riavvio**, dopo il merge (prima del merge basta `sudo systemctl restart devloop`):

   ```bash
   sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin origin/main
   sudo systemctl start devloop      # restart, se il loop è in esecuzione
   ```

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

## Dati di ssdlc nei prompt

Con l'ADR 0006, R4, i prompt di `devloop/prompts/` non contengono più il testo di ssdlc: le
istruzioni da leggere (`AGENTS.md`, la sezione "Developing SSDLC" del README), il comando di un
solo modulo di test e le regole sugli ADR del preflight stanno nella tabella `[prompts]` del file
di progetto ([docs/devloop/reference.md](../../docs/devloop/reference.md#project-data-in-the-prompts)).
Senza la tabella la copia di R4 parte, ma i prompt perdono quelle frasi; con la tabella la copia
di R3 non parte (chiave sconosciuta). Il file di progetto sull'host e la copia fissata cambiano
quindi insieme, a loop fermo. Sono passi dell'operatore, dopo il merge della PR di R4.

Se l'host non ha ancora fatto la migrazione di R3, basta quella della sezione precedente con
l'esempio di R4: il passo 3 copia anche `[prompts]`, e il passo 5 confronta anche i prompt resi.
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
   identici. Esce con 0 ed `equivalent: …`; altrimenti il pin non va fatto. Poi
   `git worktree remove /tmp/ssdlc-r4`.
5. **Pin e avvio**: `scripts/devloop-pinned pin origin/main`, poi `sudo systemctl start devloop`.

**Rollback.** Il pin della copia di R3 (`scripts/devloop-pinned pin <sha12>`) **insieme** al file
di progetto di prima (`project.toml.prima-r4`, rimesso al suo posto con `cp -p`): la copia di R3
non legge `[prompts]`.
