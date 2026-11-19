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
copia di R3 non parte e il suo pin fallisce, quindi i passi qui sotto vanno fatti **prima** del
pin della copia di R3. Sono passi dell'operatore, dopo il merge della PR di R3.

I percorsi sono quelli dell'installazione attuale (`state_dir` = `.ssdlc/devloop` del clone);
i comandi vanno eseguiti come utente `devloop`, dal clone.

1. **Quando.** L'hash del file di progetto entra nella versione della policy: le approvazioni
   delle PR aperte decadono e serve una nuova review. Meglio con la coda vuota.
2. **Copia dell'overlay di prima**, per il confronto:

   ```bash
   cd ~/ssdlc && git fetch origin
   cp -p .ssdlc/devloop/config.local.toml .ssdlc/devloop/config.local.toml.prima-r3
   ```

   Senza overlay, non si crea nulla e il confronto si fa senza `--before-local`.
3. **Il file di progetto**, dal commit di R3 su `origin/main`, con i permessi dell'overlay:

   ```bash
   git show origin/main:ops/host/ssdlc/project.toml > .ssdlc/devloop/project.toml
   chmod 600 .ssdlc/devloop/project.toml
   ```

   Deve essere un file regolare a sé: né un link né un hard link al file del repository.
4. **L'overlay** riceve le chiavi di `ops/host/ssdlc/overlay.toml` (`write_commands` e
   `review_commands` di `[agents.agy]`), accanto a quelle che ha già. Se l'overlay ha già
   `[agents.agy]`, le due chiavi vanno in quella tabella.
5. **Verifica dell'equivalenza**, con la copia fissata di prima e un checkout del commit di R3:

   ```bash
   git worktree add --detach /tmp/ssdlc-r3 origin/main
   python3 /tmp/ssdlc-r3/ops/host/ssdlc/check-equivalence.py \
       --before .ssdlc/devloop/current --before-local .ssdlc/devloop/config.local.toml.prima-r3 \
       --after /tmp/ssdlc-r3 --after-local .ssdlc/devloop/config.local.toml \
       --after-project .ssdlc/devloop/project.toml
   ```

   Confronta la configurazione effettiva chiave per chiave, i prompt resi e i moduli di test
   selezionati. Lascia fuori, dichiarandolo, l'hash del file di progetto
   (`review_policy.project`), che R2 mette nella versione della policy: la versione si confronta
   senza. Esce con 0 ed `equivalent: …` se non c'è differenza; altrimenti elenca ogni
   differenza ed esce con 1, e il pin non va fatto finché non si è capito perché (di solito una
   chiave dell'overlay che sostituisce un valore del file di progetto, o le chiavi del passo 4
   mancanti). Poi `git worktree remove /tmp/ssdlc-r3`.
6. **Pin e riavvio**: `scripts/devloop-pinned pin origin/main` (lo smoke test usa il file di
   progetto), poi `sudo systemctl restart devloop`. Un loop in corso legge un file di progetto
   nuovo solo al riavvio.

**Rollback.** Il pin della copia di prima (`scripts/devloop-pinned pin <sha12>`) basta: con la
copia di prima il file di progetto e le chiavi dell'overlay danno gli stessi valori. Si possono
anche togliere, e la versione della policy torna quella di prima.

**Dopo.** I fatti di ssdlc si cambiano sull'host: una PR aggiorna l'esempio
`ops/host/ssdlc/project.toml` (per esempio una voce nuova di `[docs.map]`) e, dopo il merge,
l'operatore copia il file sull'host come al passo 3. Una PR su `devloop/config.toml` cambia solo i
default dell'installazione.
