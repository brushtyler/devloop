# Migrazione su un'altra macchina

Spostare devloop e il supervisore da una VM vecchia a una nuova. I percorsi sono quelli di
`params.env` (qui con i valori dell'installazione attuale: utenti `devloop` e `supervisor`,
clone in `/home/devloop/ssdlc`, `state_dir` = `.ssdlc/devloop`).

**Regola:** mai due dispatcher attivi sullo stesso repository. Il dispatcher nuovo parte solo
quando quello vecchio è fermo e non ripartirà.

## Ordine

1. **Preparare la macchina nuova** senza avviare nulla: `setup-devloop.sh`,
   `setup-agent-user.sh`, `setup-supervisor.sh` e, se c'è il coordinatore, `setup-coordinator.sh`
   ([ops/README.md](../README.md)). Il servizio viene installato ma non
   avviato. `sudo` deve essere sudo 1.9, non sudo-rs, il default di Ubuntu, che ignora `sudo -E`
   e farebbe girare gli agent senza l'ambiente che devloop prepara (#496): come root,
   `update-alternatives --set sudo /usr/bin/sudo.ws`, poi `sudo --version | head -n 1` dice
   `Sudo version 1.9…` ([ops/README.md](../README.md#piattaforma-verificata)).
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
  Per `devloop-agent` fra `setup-agent-user.sh --unseal` e una nuova esecuzione dello script
  senza, che rende di root istruzioni, configurazione e binari delle CLI (#438).
- **Il token di sola lettura del supervisore**: uno nuovo
  ([ops/github/tokens.md](../github/tokens.md)), e si revoca quello della macchina vecchia.
- **Il servizio**: lo installa `setup-devloop.sh` da `ops/host/devloop.service`; non si copia
  l'unità della macchina vecchia.
- **Il sudoers del supervisore**: lo installa `setup-supervisor.sh`.
- **Il coordinatore**, se c'è (#530): utente, copia di root di `check-host.sh` e sudoers in sola
  lettura li installa `setup-coordinator.sh` ([Utente del coordinatore](#utente-del-coordinatore-530));
  la CLI del suo agent, il login e il token di sola lettura si rifanno a mano.

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
codice 4, che systemd non riavvia (`RestartPreventExitStatus=4`). Una copia in uso che contiene
#453 non si ferma: lascia cadere la richiesta, avvisa una volta su `alert_issue`, mostra il motivo
in `devloop status` e continua con la copia di prima; il pin a mano del passo 6 resta da fare.

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

6. **Clone di devloop, pin e riavvio.** Prima il clone di devloop, portato su `origin/main` e
   come `devloop`, perché resti suo; sempre, anche prima del merge, e **prima** del pin e del
   riavvio. Il clone è in detached HEAD: `pull` non funziona (`You are not currently on a
   branch`), si fa il checkout di `origin/main`:

   ```bash
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach origin/main
   sudo -u devloop -H git -C /home/devloop/ssdlc status --short
   ```

   Il launcher `scripts/devloop-pinned` viene dal clone, non dalla copia fissata: è lui che passa
   `--project-config` alla copia, e quello di un clone vecchio non lo passa, così pin e loop
   partono senza file di progetto anche dopo i passi 1–5. Se il checkout fallisce, o `status
   --short` stampa qualcosa, il clone ha modifiche locali: vanno capite prima di andare avanti.

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
testo breve con il rimando a questa guida), e il reviewer controlla che ci sia. Dal #531 il
rimando nomina una sezione di questa guida o uno script di `ops/host/`, che la PR aggiunge o
aggiorna se manca, mai comandi liberi, e il reviewer verifica che esista e descriva il passo:
l'agent dell'host, che l'operatore lancia con `start-role.sh host` di questa directory
([I quattro ruoli](../README.md#i-quattro-ruoli), [runbook](../../docs/devloop/roles/host.md)), esegue solo quella sezione o quello script, dalla
versione su `main` dopo il merge. devloop mergia la
PR come le altre, ma con `[pin] auto = true` **non fa il pin automatico** di quel commit né dei
successivi: scrive un allarme con il passo su `[run].alert_issue` e mette `agent:needs-operator`
sull'issue chiusa. Dopo il passo, a loop fermo, il pin a mano (`scripts/devloop-pinned pin
origin/main`, oppure il pin con il codice nuovo come nelle sezioni qui sotto) fissa la copia e
fa riprendere il pin automatico ([docs/devloop/guide.md](../../docs/devloop/guide.md#step-on-the-host)).
All'avvio, e a ogni rilettura, devloop scrive nel log, e una volta su `[run].alert_issue`, le
tabelle del file di progetto che la copia in uso conosce e che la configurazione non ha
(`[prompts]`, `[tests]`).

## File delle CLI di root sull'utente degli agent (#438)

Dal #438 devloop, all'avvio di `tick` e `run`, non parte se `devloop-agent` può scrivere,
possedere, creare, rinominare o rimuovere le istruzioni, la configurazione o i binari delle sue
CLI ([ops/README.md](../README.md#5-utente-degli-agent-e-cli)). Un host preparato prima ha la home
e le directory delle CLI di `devloop-agent`: la copia nuova rifiuterebbe di partire. Il passo si
fa dopo il merge, prima del pin, a loop fermo; la copia di prima non controlla questi file e
parte anche dopo il passo:

Gli script di `ops/host/` si eseguono **come root dal checkout dell'operatore**
(`/root/repos/ssdlc`), mai dal clone di devloop: quel clone è scrivibile da `devloop`, e uno
script eseguito da lì come root permetterebbe a chi controlla `devloop` di eseguire codice come
root; e lì manca `ops/params.env`.

1. fermare il loop: `sudo systemctl stop devloop`;
2. aggiornare il checkout dell'operatore, come root, per avere gli script nuovi, e il clone di
   devloop, come `devloop`, per avere il launcher nuovo. Il clone di devloop è in detached HEAD:
   `pull` non funziona, si fa il checkout di `origin/main`:

   ```bash
   git -C /root/repos/ssdlc fetch origin
   git -C /root/repos/ssdlc checkout --detach origin/main
   test -f /root/repos/ssdlc/ops/params.env
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach origin/main
   ```

   Un ramo del checkout dell'operatore resta com'è (`git -C /root/repos/ssdlc switch -` per
   tornarci dopo). Verificare che l'overlay non abbia `--settings` nei `write_args` o nei
   `review_args` di un agent claude: devloop passa a ogni turno di Claude Code il proprio
   `--settings`, con la memoria automatica spenta, e rifiuta di partire con un altro;
3. rendere di root la home di `devloop-agent` e i file delle sue CLI, come root:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh
   ```

4. verificare, come root, con
   `sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps`: nessun errore nel passo
   `File delle CLI di devloop-agent`. Un link segnalato dallo script perché porta fuori da ciò
   che è sigillato (per esempio un eseguibile fuori da `~/.local`) va reso di root a mano, o
   sostituito. I file sigillati restano leggibili da `devloop-agent` tramite il suo gruppo,
   anche quelli che erano 600 (come il `settings.json` di una versione precedente);
5. provare lo strumento di shell di una CLI per tipo come `devloop-agent`, con un prompt che
   esegue `git --version`: una domanda senza comandi non lo avvia, e agy crea la directory del
   wrapper del suo strumento di shell solo al primo comando (#494). Il passo 4 lo fa già
   (`check-host.sh`, passo `CLI degli agent`); a mano, come root:

   ```bash
   P='Run the shell command `git --version` with your shell tool, then reply with its output only.'
   cd /tmp
   sudo -u devloop-agent -H /home/devloop-agent/.local/bin/claude -p "$P" \
       --allowedTools 'Bash(git --version)'
   sudo -u devloop-agent -H /home/devloop-agent/.local/bin/codex exec --skip-git-repo-check \
       -s read-only "$P"
   sudo -u devloop-agent -H /home/devloop-agent/.local/bin/agy -p "$P" --output-format json \
       --settings '{"permissions": {"allow": ["command(git --version)"]}}'
   ```

   Ognuna deve rispondere con `git version …`. Se poi una directory delle CLI contiene una voce
   che devloop non ammette, vedere [qui sotto](#una-voce-sconosciuta-in-una-directory-delle-cli);
6. il pin a mano (`sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin
   origin/main`) e `sudo systemctl start devloop`.

**Rollback.** La copia di prima parte anche con i file di root. Per riaprirli,
`sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh --unseal`: devloop di #438 però non
parte finché non si richiudono.

### Una voce sconosciuta in una directory delle CLI

Dal #517 le directory delle CLI sono scrivibili, con lo sticky bit
([qui sotto](#directory-delle-cli-con-lo-sticky-bit-517)), e possono contenere solo le voci della
lista del codice (`CLI_ALLOWED` di `devloop/agentuser.py`, `AGENT_CLI_ALLOWED` di `ops/lib.sh`).
Una CLI che al primo uso, o dopo un aggiornamento, crea una voce nuova, o un turno o i test che
ne lasciano una, fanno rifiutare a devloop ogni turno successivo, prima di avviarlo, con
un'escalation `configuration` che nomina la voce; all'avvio devloop non parte, e
`check-host.sh` e `setup-agent-user.sh` la segnalano. A loop fermo
(`sudo systemctl stop devloop`):

1. guardare che cosa è, come `devloop-agent`, per esempio
   `sudo -u devloop-agent ls -la /home/devloop-agent/.codex`, e cercare nei log di `state_dir`
   il turno o i test che l'hanno scritta;
2. se non serve alla CLI, o porta istruzioni, plugin o binari, rimuoverla come `devloop-agent`;
3. se è una voce di stato della CLI (un login, un lock, una cache, un database, un temporaneo),
   aggiungerla con una PR, con il suo tipo, a `CLI_ALLOWED` di `devloop/agentuser.py` e
   `AGENT_CLI_ALLOWED` di `ops/lib.sh`, dicendo se la CLI la rilegge come contesto in una
   sessione successiva (allora va anche in `CLI_CLEARED`, o spenta, o sigillata). Fino al pin di
   quella PR la voce va rimossa;
4. verificare: `sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps`, poi riprendere le
   issue e le PR che hanno avuto l'escalation.

Le voci che le CLI creano al primo uso e che devono essere di `devloop-agent` prima del primo uso
(`AGENT_CLI_WRITABLE` di `ops/lib.sh`, `CLI_WRITABLE` di `devloop/agentuser.py`) le crea
`setup-agent-user.sh`.

Una voce che le CLI scrivono non deve portare istruzioni o binari ai turni dopo. Per `bin` di
agy, dove agy riscrive a ogni avvio dello strumento di shell il wrapper `agentapi`
(`#!/bin/sh` e `exec "/home/devloop-agent/.local/bin/agy" agentapi "$@"`), devloop la legge come
`devloop-agent` prima di ogni turno di agy: un altro file, o un `agentapi` diverso, rifiuta il
turno prima di avviarlo, con un `tool_failure` che dice che cosa è cambiato. A loop fermo si
guarda che cosa c'è (`sudo -u devloop-agent ls -la
/home/devloop-agent/.gemini/antigravity-cli/bin`), si cerca nei log di `state_dir` il turno che
l'ha scritto, e si rimuove come `devloop-agent` ciò che non è il wrapper. Se il wrapper è cambiato
perché agy è stato aggiornato e lo scrive in un altro modo, serve una PR che aggiorni
`agy_wrapper` di `devloop/agentuser.py`.

### Il link `cli.log` di agy (#525)

agy crea a ogni avvio `~/.gemini/antigravity-cli/cli.log` come link simbolico al log del giorno
(`cli.log -> log/cli-<YYYYMMDD_HHMMSS>.log`). Prima del #525 la lista ammetteva `cli.log` solo
come file, e dopo ogni turno di agy il turno seguente era rifiutato con un'escalation
`configuration`. Dal #525 `cli.log` può essere anche un link (`CLI_LINKS` di
`devloop/agentuser.py`, `AGENT_CLI_LINKS` di `ops/lib.sh`), solo se porta, con un percorso
relativo, a un file direttamente dentro `log/` accanto a lui (`log/<nome>`), e `log/<nome>` è
un file regolare, non a sua volta un link che potrebbe uscire da `log/`. Un link con un
percorso assoluto, con `..`, verso un'altra directory o più in fondo in `log/`, o verso una voce
di `log/` che è un link, una directory o non c'è, resta rifiutato,
all'avvio, prima di ogni turno, in `check-host.sh` e in `setup-agent-user.sh`. Claude Code e codex
non creano link nelle loro directory (i link di `~/.codex/packages` sono sigillati).

Con la stessa PR la lista ammette anche `~/.claude/.last-cleanup`, solo come file: Claude Code
(2.1.326) vi scrive a ogni avvio la data della sua pulizia periodica (per esempio
`2026-11-21T13:32:16.320Z`), e prima della PR ogni turno di qualunque CLI era rifiutato finché
c'era. È un dato della CLI, senza istruzioni né configurazione. Dopo il pin non serve più
rimuoverlo: il sorvegliante che lo faceva sull'host si può togliere.

Il rimedio temporaneo, fino al pin della PR del #525, era togliere agy dal gruppo dei reviewer
nell'overlay (`[roles] reviewers = ["codex-reviewer", "claude-reviewer"]`) e rimuovere il link
prima dell'avvio. Per rimettere agy nel gruppo dei reviewer **dopo il pin** di quella PR (quello
automatico dopo il merge o quello a mano), a loop fermo (`sudo systemctl stop devloop`):

1. verificare che la copia fissata contenga la PR: il commit in uso, che
   `sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned status` mostra, deve
   essere quello del merge o uno successivo;
2. verificare le directory delle CLI, come root, con
   `sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps`: nel passo
   `File delle CLI di devloop-agent` deve esserci
   `ok: /home/devloop-agent/.gemini/antigravity-cli contiene solo voci della lista`. Il link
   `cli.log -> log/…`, se c'è, non è più un errore; uno che porta altrove sì, e va rimosso come
   `devloop-agent`:
   `sudo -u devloop-agent rm /home/devloop-agent/.gemini/antigravity-cli/cli.log`;
3. nell'overlay rimettere `agy-reviewer` in `[roles] reviewers`, nella posizione di prima:

   ```bash
   sudo -u devloop -H "${EDITOR:-vi}" /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
   ```

   Se l'overlay aveva solo quella riga per escludere agy, si può anche togliere la chiave:
   vale allora il gruppo di `config.toml` (`["codex-reviewer", "agy-reviewer",
   "claude-reviewer"]`);
4. `sudo systemctl start devloop`. All'avvio devloop rifà le verifiche delle directory delle
   CLI; dopo il primo turno di agy, `check-host.sh --no-apps` deve dare ancora `ok` su
   `~/.gemini/antigravity-cli`, con il link ricreato da agy.

## Attivazione dell'utente degli agent (#415)

Finché l'overlay non imposta `[agent_user] name`, gli agent e i test girano come `devloop`:
possono leggere le chiavi delle app in `/home/devloop/.ghapps` (anche quella del merger), lo
`state_dir` e l'overlay, e la protezione della sezione precedente vale per una home che nessuno
usa. devloop lo segnala a ogni avvio di `tick` e `run` nel log, e una volta su
`[run].alert_issue` (`Agent senza utente dedicato`), ma non si ferma. `check-host.sh --no-apps`
lo segnala come errore (`non imposta [agent_user] name = "devloop-agent"`), insieme alle CLI che
mancano a `devloop-agent` (`…/.local/bin/codex non trovato`).

L'attivazione è dell'operatore, a loop fermo: i login delle CLI sono interattivi. Presuppone
`devloop-agent` creato da `setup-agent-user.sh` (sezione precedente). Gli script di `ops/host/`
girano come root dal checkout dell'operatore (`/root/repos/ssdlc`), il clone di devloop si
aggiorna come `devloop`; ogni comando dice l'utente con cui gira.

1. **Fermare il loop e aggiornare i checkout**, come nella sezione precedente:

   ```bash
   sudo systemctl stop devloop
   git -C /root/repos/ssdlc fetch origin
   git -C /root/repos/ssdlc checkout --detach origin/main
   test -f /root/repos/ssdlc/ops/params.env
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach origin/main
   ```

2. **Le versioni delle CLI di `devloop`**, quelle con cui devloop gira oggi (il `PATH` è quello
   dell'unità, `DEVLOOP_PATH` di `ops/lib.sh`):

   ```bash
   for cli in claude codex agy; do
       sudo -u devloop -H env PATH=/home/devloop/.local/bin:/usr/local/bin:/usr/bin:/bin \
           "$cli" --version
   done
   ```

3. **Aprire le directory delle CLI** a `devloop-agent`, come root:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh --unseal
   ```

4. **Installare `claude`, `codex` e `agy`** in `/home/devloop-agent/.local/bin`, come
   `devloop-agent` (`sudo -iu devloop-agent`), **con lo stesso metodo e le stesse versioni** del
   passo 2: il metodo di ciascuna è nella tabella di
   [ops/README.md](../README.md#5-utente-degli-agent-e-cli) e, per Claude Code con una versione
   data, in [docs/devloop/guide.md](../../docs/devloop/guide.md#agent-clis)
   (`@anthropic-ai/claude-code@<versione>`). Poi la verifica, come `devloop-agent`: ogni
   versione deve essere quella del passo 2.

   ```bash
   for cli in claude codex agy; do
       sudo -u devloop-agent -H /home/devloop-agent/.local/bin/$cli --version
   done
   ```

5. **Login di ciascuna CLI** come `devloop-agent`, in una sessione interattiva: ogni CLI avviata
   una volta chiede il suo login.

   ```bash
   sudo -iu devloop-agent /home/devloop-agent/.local/bin/claude
   sudo -iu devloop-agent /home/devloop-agent/.local/bin/codex
   sudo -iu devloop-agent /home/devloop-agent/.local/bin/agy
   ```

6. **Richiudere** le directory delle CLI, come root:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh
   ```

7. **L'overlay**, come `devloop`, perché resti suo:

   ```bash
   sudo -u devloop -H "${EDITOR:-vi}" /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
   ```

   con la tabella (se l'overlay ha già `[agent_user]`, la chiave va in quella):

   ```toml
   [agent_user]
   name = "devloop-agent"
   ```

8. **Verifica**, come root: esce con 0, senza errori (il passo `devloop apps` è saltato).

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps
   ```

   Nel passo `CLI degli agent` ogni CLI esegue come `devloop-agent` il comando git --version
   con il suo strumento di shell (#494). Una voce che una CLI crea e che devloop non ammette:
   [Una voce sconosciuta in una directory delle CLI](#una-voce-sconosciuta-in-una-directory-delle-cli).

   Con sudo-rs (`ERRORE: sudo è sudo-rs …`, `ERRORE: l'ambiente non arriva a devloop-agent con
   sudo -E …`) gli agent girerebbero senza l'ambiente che devloop prepara (#496): come root,
   `update-alternatives --set sudo /usr/bin/sudo.ws`, poi di nuovo la verifica.

9. **Riavvio**: `sudo systemctl start devloop`. All'avvio devloop verifica che l'ambiente arrivi
   a `devloop-agent` con `sudo -E` (#496) e, come `devloop-agent`, che non legga le chiavi, lo
   `state_dir` e l'overlay, e che non possa cambiare i file delle sue CLI; se qualcosa non va non parte e dice perché in
   `/home/devloop/ssdlc/.ssdlc/devloop/run.log` (`sudo tail -n 50
   /home/devloop/ssdlc/.ssdlc/devloop/run.log`). `[agent_user]` è una chiave di fiducia: il
   cambio entra nel journal all'avvio.

**Rollback.** A loop fermo, come `devloop`, si toglie `name` da `[agent_user]` (o si lascia
`name = ""`) e si riavvia:

```bash
sudo systemctl stop devloop
sudo -u devloop -H "${EDITOR:-vi}" /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
sudo systemctl start devloop
```

Gli agent tornano a girare come `devloop`, con i login delle CLI di `devloop`, e l'avviso su
`[run].alert_issue` parte di nuovo, una volta.

## git nei worktree come utente degli agent (#500)

I worktree sono di `devloop`, agent e test girano come `devloop-agent`: git li rifiuta
(`detected dubious ownership`) se non sono in `safe.directory`. Dal #500 devloop passa a ogni
turno e a ogni esecuzione dei test, nell'ambiente (`GIT_CONFIG_*`, con `sudo -n -E`),
`safe.directory` per il solo worktree di quel turno, e all'avvio non parte se git, come
`devloop-agent`, rifiuta un worktree di `devloop`. Serve che `sudo` mantenga l'ambiente che
devloop prepara. La soluzione temporanea `/home/devloop-agent/.gitconfig` (di root, con
`safe.directory = …/worktrees/*`) non serve più e va tolta: copre ogni worktree, e nasconderebbe
a `check-host.sh` un `sudo` che non mantiene l'ambiente. A loop fermo, come root, dal checkout
dell'operatore aggiornato come al passo 1 della sezione precedente:

```bash
sudo systemctl stop devloop
sudo rm -f /home/devloop-agent/.gitconfig
sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps
```

`check-host.sh` deve dare `ok: devloop-agent esegue git in … con il suo safe.directory` (con
almeno un worktree in `worktrees`). Poi il pin a mano (`sudo -u devloop -H python3
/home/devloop/ssdlc/scripts/devloop-pinned pin origin/main`) e `sudo systemctl start devloop`;
all'avvio devloop rifà la stessa prova su un worktree di prova. Se git rifiuta il worktree,
`sudo` non mantiene l'ambiente: il file si rimette com'era e si resta sul pin precedente.

## Directory delle CLI con lo sticky bit (#517)

Dal #438 le directory delle CLI di `devloop-agent` erano di root, `755`: le CLI che salvano con un
file temporaneo e un `rename`, o che creano lock, non riuscivano a scrivere. Claude Code non
salvava il token rinnovato (turni falliti con `401`, #507), codex non potrebbe salvare
`auth.json` al primo rinnovo (un `.auth-XXXX` rinominato sopra `auth.json`, con `auth.lock` e
`auth.json.lock`), agy non scriveva alcuni suoi file. Dal #517, decisione dell'operatore:

- `~/.claude`, `~/.codex`, `~/.gemini`, `~/.gemini/config` e `~/.gemini/antigravity-cli` sono
  come la home: di root, del gruppo di `devloop-agent`, `1770`. `devloop-agent` vi crea e
  sostituisce le proprie voci, ma non rinomina né rimuove quelle di root;
- ogni voce sigillata (`AGENT_CLI_SEALED` di `ops/lib.sh`, con in più `~/.codex/plugins`,
  `~/.codex/memories` e, in `~/.gemini/config`, `config.json`, `mcp_config.json` e `projects`)
  che non esiste diventa un segnaposto di root (un file vuoto, `{}` per un `.json`, o una
  directory vuota), che `devloop-agent` non può creare, scrivere, rinominare né rimuovere. In
  `~/.gemini/config`, che prima era `700` di `devloop-agent`, `config.json` abilita i plugin di
  agy e `mcp_config.json` elenca i server MCP che carica: un turno che li scrivesse li darebbe ai
  turni di agy dopo (#438). Vi resta di `devloop-agent` solo `.migrated`;
- le directory possono contenere solo le voci della lista del codice (`CLI_ALLOWED` di
  `devloop/agentuser.py`, `AGENT_CLI_ALLOWED` di `ops/lib.sh`), oltre a quelle sigillate e alle
  directory delle CLI, di root. devloop lo verifica all'avvio e prima di ogni turno, come
  `devloop-agent`: una voce sconosciuta rifiuta il turno con un'escalation `configuration` che la
  nomina ([qui sopra](#una-voce-sconosciuta-in-una-directory-delle-cli)). L'overlay può solo
  togliere voci dalla lista (`[agent_user] cli_denied`);
- le memorie che agy rilegge fra un turno e l'altro (`brain`, `implicit`, `builtin`,
  `conversation_summaries.db*` e `jetbox_summaries_proto.pb*`) devloop le rimuove prima di ogni
  turno di agy, che non ha un interruttore noto, e i turni di agy girano uno alla volta anche
  con le azioni in parallelo. Le memorie di codex (`memories_1.sqlite*`) sono spente: il
  `~/.codex/config.toml` sigillato imposta `[features] memories = false` in modo esplicito
  (codex 0.169.0 le ha già spente per default, `codex features list`: `memories  stable  false`,
  ma un cambio del default non deve riaccenderle). `setup-agent-user.sh` scrive la riga;
  devloop all'avvio e `check-host.sh` rifiutano un `config.toml` senza di essa, con
  `memories = true` o con un profilo che le riaccende. I turni di codex non svuotano nulla e
  restano in parallelo; la memoria automatica di Claude Code resta spenta (#438);
- il token di lunga durata di claude di #507 (`[claude_token]`) è tolto: una configurazione che
  nomina ancora la tabella ferma devloop all'avvio.

### Passo sull'host

A loop fermo, dopo il merge e prima del pin, come root dal checkout dell'operatore aggiornato
come al passo 2 di [File delle CLI di root sull'utente degli
agent](#file-delle-cli-di-root-sullutente-degli-agent-438):

1. fermare il loop: `sudo systemctl stop devloop`;
2. se è stato attivato il token di #507: togliere la tabella `[claude_token]` dall'overlay
   (`/home/devloop/ssdlc/.ssdlc/devloop/config.local.toml`), rimuovere il file
   (`sudo -u devloop -H rm /home/devloop/.ghapps/claude.<data>.oauth-token`) e revocare il token
   su claude.ai, dalle impostazioni dell'account dove sono elencate le autorizzazioni di Claude
   Code. Se il login di `devloop-agent` era stato tolto, rifarlo dopo il passo 4, in una sessione
   interattiva di `devloop-agent` (`sudo -iu devloop-agent`): ora la CLI lo salva da sé;
3. rileggere, come root, la configurazione di agy che lo script sta per sigillare con il
   contenuto che ha: finora `devloop-agent`, quindi ogni turno, poteva scriverla.

   ```bash
   sudo ls -la --time-style=full-iso /home/devloop-agent/.gemini/config \
       /home/devloop-agent/.gemini/config/projects
   sudo cat /home/devloop-agent/.gemini/config/config.json \
       /home/devloop-agent/.gemini/config/mcp_config.json
   ```

   `mcp_config.json` non deve elencare server MCP che l'operatore non ha messo, `config.json` solo
   i plugin voluti (oggi `{"plugins": {"swarm": {"enabled": true}}}`), `projects` solo i progetti
   di agy (`default-cli-project.json`). Ciò che non torna si toglie prima del passo 4, come root.
   Le date di modifica dicono anche se agy li riscrive a ogni avvio: se coincidono con l'ultimo
   turno di agy, il primo turno di agy dopo il pin va seguito come al passo 6;
4. rieseguire lo script dell'utente degli agent, prima in prova:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh
   ```

   Rende le cinque directory `1770`, di root e del gruppo di `devloop-agent` (in `~/.claude` e
   `~/.codex`, che hanno l'ACL di `devloop` di una versione precedente, imposta anche
   `group::rwx`: con un'ACL `chmod` cambia solo la maschera), crea i segnaposto
   di root delle voci sigillate che mancano, rende di root `~/.codex/plugins` e `config.json`,
   `mcp_config.json` e `projects` di `~/.gemini/config` (oggi di `devloop-agent`), aggiunge
   `[features] memories = false` a `~/.codex/config.toml` (nella tabella `[features]` se c'è,
   altrimenti in una nuova in fondo; la prova la mostra con `scriverebbe`), e segnala come
   `da fare a mano` ogni voce che non è nella lista. Se segnala da fare a mano anche
   `config.toml` (un valore diverso di `memories`, un `[features]` che non si estende, un link),
   la riga va messa a mano, come root, prima del passo 5. Una voce
   segnalata si rimuove come `devloop-agent` (per esempio
   `sudo -u devloop-agent rm -r /home/devloop-agent/.codex/<voce>`), o si aggiunge alla lista
   con una PR, come [qui sopra](#una-voce-sconosciuta-in-una-directory-delle-cli);
5. verificare, come root: `sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps`,
   nessun errore nel passo `File delle CLI di devloop-agent` (le directory `1770 root`, scrivibili
   e con lo sticky bit; le voci sigillate di root; le directory con le sole voci della lista;
   `ok: …/.codex/config.toml imposta [features] memories = false`) né nel passo `CLI degli
   agent`. `codex features list`, come `devloop-agent`, deve dare `memories  stable  false`;
6. il pin a mano (`sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin
   origin/main`) e `sudo systemctl start devloop`. All'avvio devloop rifà le stesse verifiche.
   Al primo turno di agy, controllare che non fallisca scrivendo in `~/.gemini/config`
   (`permission denied` su `config.json`, `mcp_config.json` o `projects` nel log del turno o in
   `~/.gemini/antigravity-cli/cli.log`). Se agy deve riscriverne uno a ogni avvio, il turno
   fallisce chiuso: si torna indietro come qui sotto e lo si segnala sull'issue, per una forma
   più stretta che regga (un controllo del contenuto atteso, come per `bin/agentapi` nel #494).

**Rollback.** La copia di prima rifiuta di partire con le directory delle CLI scrivibili da
`devloop-agent`. Per tornare al pin precedente, a loop fermo, si richiudono come root
(`chown root:root` e `chmod 755` su `~/.claude`, `~/.codex`, `~/.gemini`, `~/.gemini/config` e
`~/.gemini/antigravity-cli` di `devloop-agent`), poi il pin a mano; i segnaposto restano, e il
rinnovo dei login torna quello rotto del #438.

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

## Identità di ssdlc nell'overlay

Con l'ADR 0006, R6, `devloop/config.toml` non ha più `repo`, `trusted_users` né `[apps]`: il
repository gestito, gli utenti fidati e l'insieme di app del progetto sono dell'operatore,
nell'overlay del suo `state_dir`
([docs/devloop/reference.md](../../docs/devloop/reference.md#identities-of-a-project)). Gli
stessi valori che `config.toml` aveva stanno in [`ssdlc/overlay.toml`](ssdlc/overlay.toml). Senza
`repo` e `trusted_users` nell'overlay la copia di R6 non parte, e il suo pin fallisce lasciando la
copia di prima (`pin failed, the pinned copy is unchanged`).

La copia di prima accetta già queste chiavi nell'overlay, e con gli stessi valori la
configurazione effettiva e la versione della policy non cambiano: l'overlay si aggiorna **prima**
del pin della copia di R6, anche a loop avviato (la rilettura trova gli stessi valori). Come
utente `devloop`, dal clone:

1. **Le chiavi nell'overlay.** `repo`, `trusted_users` e le tabelle `[apps]` (con `key_dir`) di
   `ssdlc/overlay.toml` della testa della PR di R6, o di `origin/main` dopo il merge
   (`git show origin/main:ops/host/ssdlc/overlay.toml`), vanno aggiunte a
   `.ssdlc/devloop/config.local.toml`. Una chiave che l'overlay ha già resta com'è.
2. **Verifica dell'equivalenza**, con la copia fissata e un checkout del commit di R6:

   ```bash
   cd ~/ssdlc && git fetch origin
   git worktree add --detach /tmp/ssdlc-r6 origin/main
   python3 -B /tmp/ssdlc-r6/ops/host/ssdlc/check-equivalence.py \
       --before .ssdlc/devloop/current --before-local .ssdlc/devloop/config.local.toml \
       --before-project .ssdlc/devloop/project.toml \
       --after /tmp/ssdlc-r6 --after-local .ssdlc/devloop/config.local.toml \
       --after-project .ssdlc/devloop/project.toml
   git worktree remove /tmp/ssdlc-r6
   ```

   Esce con 0 ed `equivalent: …`; con 2 (`trusted_users` o `repo` nel messaggio) all'overlay
   manca una chiave del passo 1, e il pin non va fatto.
3. **Pin della copia di R6**, come ogni pin: dopo un merge con `[pin] auto`, oppure a mano
   (`python3 scripts/devloop-pinned pin origin/main`, poi stop e avvio del loop). Il pin la
   verifica con l'overlay del passo 1. `python3 scripts/devloop-pinned apps` nomina per prima
   cosa il repository e la `key_dir` del progetto (`apps of brushtyler/ssdlc, keys in …`).
4. **Token.** Dal pin della copia di R6, `python3 scripts/agent-token.py` passa da `devloop token`
   della copia fissata, con `repo` e `[apps]` dell'overlay; prima, la copia fissata non ha il
   sottocomando `token`. Il supervisore, dal suo clone aggiornato, aggiunge
   `--state-dir /home/devloop/ssdlc/.ssdlc/devloop` ai comandi di `scripts/supervisor-token.py`:
   ne legge l'overlay come legge `run.log`, senza segreti.

**Rollback.** Il pin della copia di prima (`python3 scripts/devloop-pinned pin <sha12>`) basta:
accetta le stesse chiavi nell'overlay, che possono restare.

## Chiavi per ruolo su un host attivo

Con #499 `devloop/config.toml` nomina gli agent per ruolo (ADR 0005, P5-3, P5-5, D8):
`claude-implementer`, che firma con l'app `implementer`, `codex-reviewer`, `agy-reviewer` e,
ultimo del pool, `claude-reviewer` con Sonnet 5.5, che sostituisce agy quando agy non è
disponibile. Le chiavi `claude`, `codex` e `agy` non ci sono più
([docs/devloop/reference.md](../../docs/devloop/reference.md#the-implementer-app-and-the-keys-by-role)).
L'overlay di ssdlc ha ancora le tabelle `[agents.claude]`, `[agents.codex]` e `[agents.agy]`
(con `provider` e i comandi di agy) e `preflight = "codex"`: con la copia di #499 devloop si
ferma all'avvio e dice cosa rinominare (`agents.agy: the agents have keys by role: rename
[agents.agy] of the local overlay to [agents.agy-reviewer] …`, `preflight: unknown agent 'codex':
the agents have keys by role, use 'codex-reviewer' …`). La copia di prima, al contrario, non
accetta l'overlay rinominato: le tabelle nuove non hanno `kind`. Quindi il passo si fa **a loop
fermo**, tra il merge e il pin, e il pin si fa **con il codice nuovo**, dal clone aggiornato,
come per R3: `devloop-pinned pin` eseguirebbe la copia in uso, che non legge più l'overlay.

devloop mergia la PR di #499 come le altre, ma non fa il pin automatico di quel commit né dei
successivi finché l'operatore non fa il pin a mano dopo il passo
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

### Prima

- **Coda vuota** (ADR 0005, D10): nessuna PR di devloop aperta e nessuna azione in corso. Con una
  PR aperta il cui marker nomina `claude`, `codex` o `agy`, la copia nuova si ferma all'avvio.
  `sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned status` non deve
  elencare PR di devloop.
- **La chiave dell'app implementer** nella `key_dir` di `devloop`: da ora `claude-implementer`
  firma commit, push, PR e record dei fix con `brushtyler-devloop-implementer`, e devloop ne
  legge la chiave. Deve esserci, con mode 600:

  ```bash
  sudo -u devloop -H ls -l /home/devloop/.ghapps/ | grep brushtyler-devloop-implementer
  ```

- **Il supervisore.** Se l'overlay ha `roles.supervisor = "claude"` (una chiave di agent), la
  copia nuova si ferma (`roles.supervisor: unknown agent 'claude'`): una chiave con un ruolo non
  è mai il supervisore. Prima va attivata l'app del supervisore, la tabella `[roles.supervisor]`
  ([docs/devloop/reference.md](../../docs/devloop/reference.md#the-supervisor-app)), che è una
  decisione dell'operatore a sé. Con la tabella, o senza supervisore, non serve altro.

### Comandi

Come root, con i percorsi dell'installazione attuale: `state_dir`
`/home/devloop/ssdlc/.ssdlc/devloop`, clone di devloop `/home/devloop/ssdlc`.

1. **Loop fermo**, dopo il merge della PR di #499:

   ```bash
   sudo systemctl stop devloop
   ```

2. **Copia dell'overlay di prima e commit della copia in uso**, per il rollback, una volta sola:
   se ci sono già, sono quelli di prima e non vanno sovrascritti. Il commit va salvato **prima
   del pin** del passo 6: dopo, `current` indica la copia nuova.

   ```bash
   test -e /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml.prima-499 || \
       install -o devloop -m 600 /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml \
           /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml.prima-499
   test -s /home/devloop/ssdlc/.ssdlc/devloop/commit.prima-499 || \
       python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["commit"])' \
           /home/devloop/ssdlc/.ssdlc/devloop/current/pin.json \
           > /home/devloop/ssdlc/.ssdlc/devloop/commit.prima-499
   cat /home/devloop/ssdlc/.ssdlc/devloop/commit.prima-499
   ```

   `cat` deve stampare un commit di 40 cifre esadecimali, quello del `pin.json` della copia in
   uso (`readlink /home/devloop/ssdlc/.ssdlc/devloop/current` dà `pinned-` e le sue prime 12
   cifre). Se non lo stampa, il file va tolto e il passo ripetuto, prima di andare avanti.

3. **L'overlay**, modificato come `devloop`, perché resti suo:

   ```bash
   sudo -u devloop -H "${EDITOR:-vi}" /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
   ```

   - `[agents.claude]` diventa `[agents.claude-implementer]`, `[agents.codex]` diventa
     `[agents.codex-reviewer]`, `[agents.agy]` diventa `[agents.agy-reviewer]`: si rinomina solo
     l'intestazione, le chiavi della tabella (`provider`, `fallback_models`, `write_commands`,
     `review_commands`, …) restano come sono. Una chiave che `config.toml` ha già con lo stesso
     valore si può togliere;
   - `preflight = "codex"` diventa `preflight = "codex-reviewer"`: stessa app (`codex-agent`) e
     stesso modello di prima;
   - ogni altro valore che nomina `claude`, `codex` o `agy` passa alla chiave per ruolo:
     `conclave` (chiavi di reviewer, mai `claude-implementer`), `[roles]` `implementer` e
     `reviewers` (si possono togliere: `config.toml` ha già i valori nuovi), le tabelle
     `[budget.usage.*]`;
   - in `[review_policy]`: `advisory_reviewer = "claude-reviewer"` (la revisione consultiva di
     #369) e `provider_quorum = true`, senza il quale `claude-reviewer` non rivede mai il codice
     di `claude-implementer`. Un `findings_reviewer = "claude-reviewer"` va tolto: ora
     `claude-reviewer` è nel pool, e un reviewer di soli finding fuori dal pool non lo è. Per
     tenerlo serve una chiave in più fuori dal pool, come `claude-findings` con Sonnet, con una
     GitHub App sua (`docs/devloop/reference.md`, "Findings-only reviewer").

   Poi nessuna riga deve nominare le chiavi di prima; il comando non stampa nulla:

   ```bash
   grep -nE '^\[(agents|budget\.usage)\.(claude|codex|agy)\]|"(claude|codex|agy)"' \
       /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
   ```

   Le tabelle `[apps.claude]`, `[apps.codex]` e `[apps.agy]` restano: sono le app dei reviewer.

4. **Clone di devloop** su `origin/main`, come `devloop`, **prima** del pin:

   ```bash
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach origin/main
   sudo -u devloop -H git -C /home/devloop/ssdlc status --short
   ```

   Se il checkout fallisce, o `status --short` stampa qualcosa, il clone ha modifiche locali: vanno
   capite prima di andare avanti.
5. **Verifica** con il codice nuovo, dal clone: carica la configurazione con l'overlay e il file
   di progetto e controlla ogni app, quella dell'implementer compresa.

   ```bash
   cd /home/devloop/ssdlc && sudo -u devloop -H python3 -m devloop \
       --project-config /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
       --local-config /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml apps
   ```

   `devloop: invalid config: …` nomina la chiave da correggere (passo 3); `implementer` deve
   comparire tra le app, con la sua installazione.
6. **Pin con il codice nuovo e avvio**:

   ```bash
   cd /home/devloop/ssdlc && sudo -u devloop -H python3 -m devloop \
       --project-config /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
       --local-config /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml pin origin/main
   sudo systemctl start devloop
   ```

   Il pin a mano rilascia il blocco del pin automatico. Al primo tick devloop sposta la memoria
   dei modelli non disponibili di `claude`, `codex` e `agy` alle chiavi per ruolo, e lo scrive nel
   log una volta. I pin successivi tornano al launcher.
7. **Pulizia**: `config.local.toml.prima-499` e `commit.prima-499` servono al rollback e restano
   finché può servire; si cancellano insieme quando la copia di #499 gira e il rollback non serve
   più.

Le chiavi rinominate cambiano la versione della policy: con la coda vuota non decade nessuna
approvazione.

**Rollback.** La copia di prima non legge l'overlay rinominato, e quella di #499 non legge
l'overlay di prima: si torna indietro a loop fermo, con l'overlay di prima e il pin fatto dal
codice di prima. Il commit della copia di prima è quello salvato al passo 2 in
`commit.prima-499`: dopo il pin `current` indica la copia di #499, e `readlink` non lo dà più.

```bash
sha=$(cat /home/devloop/ssdlc/.ssdlc/devloop/commit.prima-499) && echo "$sha"
```

Se non stampa il commit di 40 cifre, ci si ferma: senza il commit salvato il rollback non si fa.
Altrimenti, nella stessa shell:

```bash
sudo systemctl stop devloop
install -o devloop -m 600 /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml.prima-499 \
    /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach "$sha"
cd /home/devloop/ssdlc && sudo -u devloop -H python3 -m devloop \
    --project-config /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
    --local-config /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml pin "$sha"
sudo systemctl start devloop
```

Dopo il rollback, con `[pin] auto = true` un merge successivo prova il pin di `origin/main`, che
contiene #499: la verifica della copia fallisce con l'overlay di prima e la copia in uso resta
(`pin failed, the pinned copy is unchanged`). Finché il passo non si riprova conviene `[pin] auto
= false` nell'overlay. La memoria dei modelli già spostata alle chiavi per ruolo non vale per la
copia di prima: quei modelli si riprovano. Il clone torna su `origin/main` quando si riprova il
passo.

**Un altro progetto** sulla stessa installazione fa lo stesso passo prima di fissare un commit
che contiene #499: gli agent di `config.toml` dichiarano le app `implementer`, `claude`, `codex` e
`agy`, e il suo overlay deve avere le loro tabelle `[apps.*]`, con le chiavi nella sua
`key_dir`.

## Il motore SSDLC esce da `main` (ADR 0007, R1, #538)

Con R1 dell'[ADR 0007](../../docs/adr/0007-da-ssdlc-a-devloop.md) il motore SSDLC (l'orchestratore
e i suoi moduli, gli schemi e gli standard, lo script di setup, i suoi test e documenti) esce da
`main`. Resta nella storia e nel tag `ssdlc-engine-final`. Con il motore escono i vincoli che
portava nel file di progetto di ssdlc: i suoi percorsi sensibili, la lista della baseline del
replay gate (`sensitive_path_lists`), il prefisso dei suoi moduli in `[tests]`
(`source_prefixes`) e le sue voci di `[docs.map]`. Il codice di devloop non cambia: le chiavi
tolte sono facoltative, e la copia in uso accetta già il file di progetto nuovo.

I passi sono dell'operatore, nell'ordine. I primi due si fanno **prima del merge** della PR di R1.

1. **Il tag**, dal checkout dell'operatore, con le sue credenziali (devloop non crea tag). Va
   sull'ultimo commit di `main` che contiene il motore, il base della PR di R1: finché la PR non
   è mergiata è `origin/main`, se nel frattempo non è avanzato con altri merge.

   ```bash
   cd /root/repos/ssdlc && git fetch origin
   base=$(git rev-parse origin/main)
   git cat-file -e "$base:orchestrator.py" && echo "contiene il motore: $base"
   git tag -a ssdlc-engine-final "$base" -m "SSDLC engine, frozen (ADR 0007)"
   git push origin ssdlc-engine-final
   ```

   Se la PR è già mergiata, il base è il primo genitore del suo commit di merge
   (`git rev-parse <merge>^1`); `git cat-file -e` deve trovare `orchestrator.py` anche lì.
2. **La CI e il ruleset**, con una PR dell'operatore su `.github/` (devloop non tocca
   `.github/`, `protected_paths`): da `.github/workflows/test.yml` si toglie il job
   `recovery-benchmark` (`Isolated A5 Recovery Benchmark`), che esegue il replay gate del motore,
   e il ruleset di `main` non lo richiede più: lo si applica da
   [`ops/github/ruleset-main.json`](../github/ruleset-main.json) come in
   [ops/README.md](../README.md#3-ruleset-del-branch-principale). Senza questo passo il check
   obbligatorio della PR di R1 fallisce, e dopo il merge resterebbe atteso su ogni PR.
3. **Il file di progetto sull'host**, a loop fermo, dopo il merge e prima del pin del commit di
   R1: la copia di R1 non ha più `schemas/recovery-grader-baseline.json`, e il suo pin fallisce
   (`… is not a file`) finché il file di progetto lo elenca in `sensitive_path_lists`. Come root:

   ```bash
   sudo systemctl stop devloop
   install -o devloop -m 600 /home/devloop/ssdlc/.ssdlc/devloop/project.toml \
       /home/devloop/ssdlc/.ssdlc/devloop/project.toml.prima-r1
   sudo -u devloop -H "${EDITOR:-vi}" /home/devloop/ssdlc/.ssdlc/devloop/project.toml
   ```

   Nel file di progetto, come nell'esempio [`ops/host/ssdlc/project.toml`](ssdlc/project.toml):
   `sensitive_paths` perde `schemas/` e i moduli `ssdlc_*.py`; `sensitive_path_lists` si toglie;
   in `[tests]` si toglie `source_prefixes`; in `[docs.map]` si tolgono le voci dei moduli del
   motore, del suo script di setup e di `schemas/`. Se il file sull'host è l'esempio senza
   modifiche locali, si può copiare l'esempio intero come al passo 3 della [migrazione di
   R3](#migrazione-di-r3-su-un-host-attivo).
4. **Il pin a mano** del commit di R1 e il riavvio. Come utente `devloop`
   (`sudo -u devloop -i`): `cd /home/devloop/ssdlc && python3 scripts/devloop-pinned pin
   origin/main`; poi, come root, `sudo systemctl start devloop`. La PR di R1 dichiara il passo
   (`host_step`): con `[pin] auto = true` devloop non fa il pin automatico finché non lo fa
   l'operatore.

L'hash del file di progetto cambia ed entra nella versione della policy: le approvazioni delle PR
aperte decadono. Meglio con la coda vuota.

**Rollback.** Prima il pin della copia di prima, poi il file di progetto di prima, nell'ordine: la
copia di prima accetta il file nuovo, mentre la copia di R1 non parte con il file di prima (la
lista manca). A loop fermo, come utente `devloop`:

```bash
cd /home/devloop/ssdlc
python3 scripts/devloop-pinned pin <sha12>
cp -p .ssdlc/devloop/project.toml.prima-r1 .ssdlc/devloop/project.toml
```

Poi, come root, `sudo systemctl start devloop`. `<sha12>` è la copia di prima (`pinned-<sha12>`
nello `state_dir`). Il tag resta: è solo un nome per un commit della storia.

## Utente del coordinatore (#530)

Il coordinatore ha sull'host **solo lettura**: l'utente `coordinator`, con un sudoers di soli
comandi di lettura e `check-host.sh --read-only` da una copia di root
([ops/README.md](../README.md#10-coordinatore-facoltativo)). Il passo non tocca devloop né la
copia fissata: si fa a loop attivo, quando l'operatore assegna il ruolo, e nessun pin lo aspetta.

Come root, **dal checkout dell'operatore** (`/root/repos/ssdlc`), mai dal clone di devloop:

1. aggiornare il checkout dell'operatore, che deve avere `ops/params.env`:

   ```bash
   git -C /root/repos/ssdlc fetch origin
   git -C /root/repos/ssdlc checkout --detach origin/main
   test -f /root/repos/ssdlc/ops/params.env
   ```

2. installare utente, copia di root e sudoers:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh
   ```

   La copia va in `/usr/local/lib/devloop-coordinator` (con `PROJECT`,
   `/usr/local/lib/devloop-coordinator-<progetto>`), il sudoers in
   `/etc/sudoers.d/devloop-coordinator` (con `PROJECT`, `-<progetto>`), dopo `visudo -c`; l'ACL
   di solo attraversamento `u:coordinator:--x` sulla home di devloop, per leggere l'overlay dello
   `state_dir`; la `key_dir` della sua app in `/home/coordinator/.ghapps` (con `PROJECT`,
   `~/.ghapps-<progetto>`), 700. Poi la chiave dell'app del coordinatore va lì, 600, proprietario
   `coordinator`, e il coordinatore conia i token con `DEVLOOP_APP_KEY_DIR` impostata a quella
   `key_dir` ([ops/README.md](../README.md#10-coordinatore-facoltativo), "App del coordinatore",
   #549; `setup-coordinator.sh` stampa il comando con i valori dell'installazione);
3. verificare, come root: `sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps`, nessun
   errore nel passo `Coordinatore coordinator`: la `key_dir` (700) con la sola chiave dell'app
   del coordinatore (600), l'ACL `user:coordinator:--x` sulla home di devloop e nessuna sulle sue
   directory private, l'overlay leggibile dal coordinatore, il sudoers (440, root, uguale a
   quello generato), la copia e le directory sopra di root, ogni file della copia che `coincide`
   con il checkout;
4. verificare **come `coordinator`**, con il suo sudoers: `sudo -n -l` elenca solo i comandi di
   [ops/README.md](../README.md#10-coordinatore-facoltativo), e la verifica in sola lettura non
   avvia nessuna CLI, non esegue `devloop apps` e non scrive nulla:

   ```bash
   sudo -u coordinator -H sudo -n -l
   sudo -u coordinator -H sudo -n /usr/bin/bash /usr/local/lib/devloop-coordinator/host/check-host.sh --read-only
   ```

5. a mano: la CLI dell'agent del coordinatore e il suo login, come `coordinator`, e un token di
   sola lettura per `gh` ([ops/github/tokens.md](../github/tokens.md)).

**Dopo ogni aggiornamento di `ops/`** si rifà il passo 2 dal checkout aggiornato: la copia di root
non segue il checkout, e `check-host.sh` lo segnala (`ATTENZIONE: … è diverso da …: rieseguire
setup-coordinator.sh`).

Dal #530 anche `check-host.sh` completo, che l'operatore o l'agent dell'host esegue come root,
riporta delle prove delle CLI solo l'esito e il codice di uscita, mai il testo del modello: per
vederlo si ripete la prova a mano, come al passo 5 di [File delle CLI di root sull'utente degli
agent](#file-delle-cli-di-root-sullutente-degli-agent-438).

**Rollback.** Come root, prima il sudoers, poi la copia, l'ACL sulla home di devloop e l'utente:

```bash
rm /etc/sudoers.d/devloop-coordinator
rm -r /usr/local/lib/devloop-coordinator
setfacl -x u:coordinator /home/devloop
userdel -r coordinator
```
