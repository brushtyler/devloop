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

Per esempio, dalla macchina nuova:

```bash
sudo -u devloop -H rsync -a --chmod=F600 \
    vecchia:/home/devloop/ssdlc/.ssdlc/devloop/{turn-journal.jsonl,turn-journal-state.json,ledger-open-turns.json,model-unavailability.json,activity.json,config.local.toml} \
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
  del branch principale, che rifà anche lo smoke test sulla macchina nuova:

  ```bash
  sudo -u devloop -H sh -c 'cd ~/ssdlc && python3 -m devloop pin origin/main'
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
