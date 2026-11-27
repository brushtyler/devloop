# Migrazione su un'altra macchina

Spostare devloop e il supervisore da una VM vecchia a una nuova. I percorsi sono quelli di
`params.env` (qui con i valori dell'installazione attuale, dal #563: `PROJECT=devloop`, utenti
`devloop`, `devloop-agent`, `supervisor` e `coordinator`, clone in `/home/devloop/devloop`,
`state_dir` = `.devloop`, servizio `devloop@devloop`).

**Percorsi delle sezioni di prima.** Le sezioni dei passi già fatti su questo host, da
[Configurazione di ssdlc nel suo file di progetto](#configurazione-di-ssdlc-nel-suo-file-di-progetto)
a [Rinomina del repository in devloop](#rinomina-del-repository-in-devloop-adr-0007-r3-540),
sono la storia di quei passi e citano i percorsi del loro tempo. Dopo [Percorsi devloop e istanza
devloop@devloop](#percorsi-devloop-e-istanza-devloopdevloop-563) valgono: `/home/devloop/devloop`
al posto di `/home/devloop/ssdlc`, `.devloop` al posto di `.ssdlc/devloop`,
`/home/supervisor/devloop` al posto di `/home/supervisor/ssdlc`, il servizio `devloop@devloop` al
posto di `devloop`, e i nomi con il suffisso `-devloop` per sudoers, casella e copia di root del
coordinatore. Il checkout dell'operatore resta `/root/repos/ssdlc`.

**Regola:** mai due dispatcher attivi sullo stesso repository. Il dispatcher nuovo parte solo
quando quello vecchio è fermo e non ripartirà.

**Un progetto fermo a un tag (#687).** Le sezioni di questo file che si eseguono "per ogni
progetto dell'host" dal checkout dell'operatore su `origin/main` valgono così per i progetti che
seguono `main`, come il repository di devloop. Un progetto con la sorgente a un tag
(`SOURCE_REPO` e `SOURCE_REF`) non le applica da `main`: le applica al tag successivo che le
contiene, con la procedura di [Un progetto con la sorgente a un tag
nuovo](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666), e dal checkout di quel tag. Gli script di
`ops/host/` installano le copie di root di `ops/` dei ruoli dal checkout da cui girano: da `main`,
quel progetto avrebbe il loop del tag con le sveglie dei ruoli di `main`. Per un progetto con
`SOURCE_REPO`, `check-host.sh` avvisa quando il checkout da cui gira non è al tag `SOURCE_REF`.

## Spostare l'installazione su un altro host (#543)

La procedura per l'[agent dell'host](../../docs/devloop/roles/host.md), lanciato dall'operatore
sulla macchina nuova da un checkout di `main` aggiornato, con `ops/params.env` dell'installazione.
Quello che sta solo sull'host (casella, ACL, ruoli) è negli script o in questa sezione: niente si
ricostruisce dalla memoria di una sessione. Le sottosezioni qui sotto ne danno i dettagli; i passi
che chiedono una persona (chiavi, login, token, avvio) li fa l'operatore, e l'agent si ferma e
glieli chiede.

1. **Piattaforma e `sudo`** (#496): sudo 1.9, non sudo-rs (passo 1 di [Ordine](#ordine)).
2. **Utenti**, senza avviare nulla, nell'ordine: `setup-devloop.sh`, `setup-agent-user.sh`,
   `setup-supervisor.sh` e, se c'è il coordinatore, `setup-coordinator.sh`, ciascuno prima con
   `--dry-run` ([ops/README.md](../README.md)). Creano anche il gruppo `devloop-roles` e la
   casella fra supervisore e coordinatore (passo 6).
3. **Fermare devloop sulla macchina vecchia** e disabilitarlo (passo 2 di [Ordine](#ordine)).
4. **`state_dir`**: copiare i file di [Da copiare](#da-copiare), fra cui il **file di progetto**
   (`project.toml`) e l'**overlay** (`config.local.toml`), 600 di `devloop`.
5. **Le ACL dell'overlay e del file di progetto**: i file copiati sono nuovi e non hanno ACL.
   Rieseguire `setup-agent-user.sh` (le chiude a `devloop-agent`), `setup-supervisor.sh` (la
   lettura `u:supervisor:r--`) e, se c'è il coordinatore, `setup-coordinator.sh` (`u:coordinator:r--`
   sull'overlay), come in [ACL dell'overlay e del file di
   progetto](#acl-delloverlay-e-del-file-di-progetto-543).
6. **La casella** (`COORDINATION_DIR`, `/var/lib/devloop-coordination-devloop`): la ricreano vuota
   `setup-supervisor.sh` e `setup-coordinator.sh`. I messaggi non letti della macchina vecchia
   li legge il loro destinatario prima dello spostamento; lo stato del lavoro è su GitHub (il
   diario #76 e le issue), e non si copia.
7. **Chiavi delle GitHub App** in ogni `key_dir`, 600 ([Chiavi delle GitHub
   App](#chiavi-delle-github-app)): passo dell'operatore.
8. **Copie fissate, worktree, servizio e sudoers** come in [Da ricreare](#da-ricreare): il
   primo pin dal clone, l'unità e i sudoers dagli script.
9. **Login delle CLI**, per `devloop-agent`, `supervisor` e `coordinator` ([Da
   ricreare](#da-ricreare)): passi dell'operatore. Nessun token personale per `gh`: i ruoli
   leggono con le loro app (#643).
10. **Verifica finale**: `sudo bash <checkout>/ops/host/check-host.sh`, nessun errore (fra
    l'altro: l'ACL `user:supervisor:r--` sull'overlay e sul file di progetto, la casella e il
    gruppo, il coordinatore e la sua copia, il comando `devloop`), poi `devloop status` e
    `devloop tick --dry-run`, il comando dell'host
    ([Comando devloop dell'host](#comando-devloop-dellhost-588); [ops/README.md](../README.md#8-verifica)). L'agent riporta all'operatore cosa ha
    eseguito, come quale utente, e il codice di uscita.
11. **Avvio**, dell'operatore: il servizio (`systemctl start devloop@devloop`, ed `enable`), poi le
    sessioni dei ruoli con `ops/host/start-role.sh supervisor` e `coordinator`
    ([ops/README.md](../README.md#i-quattro-ruoli)): ciascuna parte dalla sezione *At startup*
    del suo runbook, rilegge la sua casella, il diario (#76) e il riepilogo (#316), e riprende a
    sorvegliare (il coordinatore con `coordinator-wait.sh`).
12. **La macchina vecchia** come in [Dopo](#dopo).

### Ordine

1. **Preparare la macchina nuova** senza avviare nulla: `setup-devloop.sh`,
   `setup-agent-user.sh`, `setup-supervisor.sh` e, se c'è il coordinatore, `setup-coordinator.sh`
   ([ops/README.md](../README.md)). Il servizio viene installato ma non
   avviato. `sudo` deve essere sudo 1.9, non sudo-rs, il default di Ubuntu, che ignora `sudo -E`
   e farebbe girare gli agent senza l'ambiente che devloop prepara (#496): come root,
   `update-alternatives --set sudo /usr/bin/sudo.ws`, poi `sudo --version | head -n 1` dice
   `Sudo version 1.9…` ([ops/README.md](../README.md#piattaforma-verificata)).
2. **Fermare devloop sulla macchina vecchia** e attendere la fine delle azioni in corso:

   ```bash
   sudo systemctl stop devloop@devloop      # SIGTERM al solo processo principale (KillMode=mixed)
   systemctl is-active devloop@devloop      # "inactive": il loop è uscito
   sudo systemctl disable devloop@devloop   # non deve ripartire a un riavvio della VM vecchia
   ```

   `stop` ritorna quando il loop è uscito: devloop smette di pianificare e aspetta le azioni in
   corso, al più `agent_timeout_seconds` (`TimeoutStopSec=4200`). Se l'unità non c'è (loop
   avviato a mano), creare il file `STOP` nello `state_dir` e attendere che
   `pgrep -u devloop -af 'm devloop'` non trovi più nulla.
3. **Copiare** i file elencati sotto, conservando proprietario e permessi.
4. **Ricreare** quello che non si copia (sotto).
5. **Verificare** con `sudo bash ops/host/check-host.sh` e `devloop status`, poi **avviare** sulla
   macchina nuova: `sudo systemctl start devloop@devloop` (e `enable`, se deve ripartire al boot).

### Da copiare

Dallo `state_dir` di devloop (`/home/devloop/devloop/.devloop`), come utente `devloop`:

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
    vecchia:/home/devloop/devloop/.devloop/{turn-journal.jsonl,turn-journal-state.json,ledger-open-turns.json,model-unavailability.json,activity.json,config.local.toml,project.toml} \
    /home/devloop/devloop/.devloop/
```

Un file che sulla macchina vecchia non c'è non va creato.

L'overlay e il file di progetto copiati sono file nuovi, senza ACL: dopo la copia si riesegue
`setup-supervisor.sh`, che rimette `u:supervisor:r--` su entrambi, e, se c'è il coordinatore,
`setup-coordinator.sh`, che rimette `u:coordinator:r--` sull'overlay ([ACL dell'overlay e del
file di progetto](#acl-delloverlay-e-del-file-di-progetto-543), #554).

Dal supervisore, come utente `supervisor`:

- la directory degli strumenti e dei file del diario, `~/supervisor`: sono strumenti locali del
  supervisore, non di devloop, e non si installano dal repository. Lo stato del lavoro (cosa è in
  corso, cosa aspetta l'operatore) non sta lì né in un `HANDOFF.md` locale, ma su GitHub, nel
  diario #76 e nelle issue (#543);
- la memoria di Claude Code, `~/.claude/projects/<progetto>/memory`. `<progetto>` è il percorso
  del clone del supervisore con `/` sostituito da `-` (per esempio `-home-supervisor-devloop`): se il
  clone nuovo ha lo stesso percorso il nome resta valido, altrimenti va rinominata.

### Da ricreare

- **Le copie fissate** (`pinned-*` e `current`): non si copiano. Al primo avvio si fa un nuovo pin
  del branch principale, che rifà anche lo smoke test sulla macchina nuova. Lo esegue il devloop
  del clone: il file di progetto e l'overlay copiati sopra si passano prima del comando, come fa
  il launcher (senza file di progetto devloop non parte, ADR 0006, R3):

  ```bash
  sudo -u devloop -H sh -c 'cd ~/devloop && python3 -m devloop \
      --project-config .devloop/project.toml \
      --local-config .devloop/config.local.toml pin origin/main'
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
- **Nessun token personale** del supervisore né del coordinatore: dal #643 leggono GitHub con le
  loro app ([ops/github/tokens.md](../github/tokens.md)). Un token di sola lettura rimasto dalla
  macchina vecchia si revoca.
- **Il servizio**: lo installa `setup-devloop.sh`, con `PROJECT` l'unità template
  `ops/host/devloop@.service` e la drop-in della sua istanza (`devloop@devloop`); non si copia
  l'unità della macchina vecchia.
- **Il sudoers del supervisore**: lo installa `setup-supervisor.sh`.
- **La casella fra supervisore e coordinatore** (#543): il gruppo `devloop-roles` e
  `COORDINATION_DIR` (`/var/lib/devloop-coordination-devloop`) con `to-coordinator/`, `to-supervisor/` e le loro `done/`, di
  root, del gruppo, 2770; la creano `setup-supervisor.sh` e `setup-coordinator.sh`. Non si copia.
- **Il coordinatore**, se c'è (#530): utente, copia di root di `check-host.sh` e della sveglia
  `coordinator-wait.sh`, e sudoers in sola lettura li installa `setup-coordinator.sh`
  ([Utente del coordinatore](#utente-del-coordinatore-530)); la CLI del suo agent, il login e la
  chiave della sua app si rifanno a mano.
- **Le sessioni dei ruoli**: si avviano con `ops/host/start-role.sh supervisor` e `coordinator`
  ([ops/README.md](../README.md#i-quattro-ruoli)), che passa a ciascuna il suo runbook e i
  valori della casella; non si copia nessun file di stato delle sessioni.

### Chiavi delle GitHub App

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

### Dopo

- La macchina vecchia: servizio disabilitato, chiavi e token revocati o cancellati, login delle
  CLI chiusi, sessioni dei ruoli chiuse.
- Se cambia l'host, cambia anche il nome suggerito del token del supervisore: rinominarlo.

## ACL dell'overlay e del file di progetto (#543)

Il supervisore legge l'overlay (`<state_dir>/config.local.toml`: `scripts/supervisor-token.py` ne
legge `repo` e `[apps.supervisor]`) e il file di progetto (`<state_dir>/project.toml`) con
un'ACL di sola lettura, `u:supervisor:r--`, che mette `setup-supervisor.sh`; il coordinatore
l'overlay con `u:coordinator:r--`, che mette `setup-coordinator.sh`. I due file sono
dell'operatore, 600 di `devloop`; `devloop-agent` non li legge.

**Sostituire uno dei due file cancella le ACL.** Un `install -m 600`, un `cp` senza `-p`, un
`rsync` o un editor che scrive un file nuovo al posto di quello vecchio lasciano un file senza
ACL. Il 21/11/2026 la sostituzione dell'overlay della migrazione di #510 ha cancellato così
`user:supervisor:r--`: l'app del supervisore non firmava più, e nessun controllo se n'è
accorto. Da #543 `check-host.sh` lo segnala come errore (`ERRORE: manca l'ACL u:supervisor:r--
su …`).

Per rimetterle, come root, dal checkout dell'operatore aggiornato (`/root/repos/ssdlc`), dopo
**ogni** sostituzione di uno dei due file:

```bash
sudo bash /root/repos/ssdlc/ops/host/setup-supervisor.sh --dry-run
sudo bash /root/repos/ssdlc/ops/host/setup-supervisor.sh
sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh    # se c'è il coordinatore
sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps
```

Il primo rimette `u:supervisor:r--` sull'overlay e sul file di progetto, il secondo
`u:coordinator:r--` sull'overlay; ciascuno solo se manca, e riduce a `r--` un'ACL più ampia.
`check-host.sh` non deve dare errori nei passi `Lettura dell'overlay e del file di progetto per
supervisor (#543)` e `Coordinatore coordinator`, e deve trovare che `devloop-agent` non legge
nessuno dei due. Le copie di sicurezza (`install … config.local.toml.prima-…`) sono file nuovi e
non toccano l'originale.

## Casella fra i ruoli e sveglia del coordinatore (#543)

Il passo sull'host di #543, su un host già attivo, a loop attivo: non tocca devloop né la copia
fissata. Porta nel repository la casella fra supervisore e coordinatore, la lettura dell'overlay e
del file di progetto per il supervisore, e la sveglia del coordinatore, che prima esistevano solo
sull'host ([ops/README.md](../README.md#i-quattro-ruoli)).

Prima, **il coordinatore** (non l'agent dell'host, che non legge i messaggi degli agent): legge
i file `to-coordinator` e `to-supervisor` della casella di prima
(`/var/lib/devloop-coordination/`, file al posto delle directory, `root:supervisor` 2770) e ne
riporta ciò che serve ancora nel diario #76, in un'issue o, dopo il passo, come messaggio nella
casella nuova; trasferisce lo stato di `HANDOFF.md` del supervisore nel diario #76 e nelle issue.
Poi dice all'operatore che il passo si può fare.

Come root, **dal checkout dell'operatore** (`/root/repos/ssdlc`), aggiornato come al passo 1 di
[Utente del coordinatore](#utente-del-coordinatore-530):

1. mettere da parte i file della casella di prima, che `setup-supervisor.sh` non sostituisce (si
   ferma con `… non è una directory: la casella di prima va spostata`):

   ```bash
   cd /var/lib/devloop-coordination
   for f in to-coordinator to-supervisor; do test -f "$f" && mv "$f" "$f.prima-543"; done
   ```

2. il supervisore: il gruppo `devloop-roles` con dentro `supervisor`, la casella (`root:devloop-roles`,
   2770, con `to-coordinator/`, `to-supervisor/` e le loro `done/`) e l'ACL `u:supervisor:r--`
   sull'overlay e sul file di progetto:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-supervisor.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-supervisor.sh
   ```

3. il coordinatore, se c'è: `coordinator` nel gruppo, la copia di root aggiornata con
   `coordinator-wait.sh`, e il sudoers con la riga di `devloop-pinned watch` della sveglia:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh
   ```

4. verificare: `sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps`, nessun errore nei
   passi `Lettura dell'overlay e del file di progetto per supervisor (#543)`, `Casella fra
   supervisore e coordinatore` e `Coordinatore coordinator`; poi, come `coordinator`, il suo
   sudoers ammette la sveglia:

   ```bash
   sudo -u coordinator -H sudo -n -l
   ```

5. chiudere le sessioni di supervisore e coordinatore e riavviarle con `ops/host/start-role.sh`
   (passo dell'operatore): l'appartenenza al gruppo vale dalla sessione nuova, che parte dalla
   sezione *At startup* del suo runbook. La sveglia di prima, `/root/coordinator-wait.sh`, non
   serve più: il coordinatore usa `/usr/local/lib/devloop-coordinator/host/coordinator-wait.sh`.
   Rimuoverla: `rm /root/coordinator-wait.sh`. I file `*.prima-543` si rimuovono quando il
   coordinatore dice che il loro contenuto è stato riportato.

Il `HANDOFF.md` locale del supervisore non serve più: le istruzioni sono nei runbook, lo stato
su GitHub.

**Rollback.** Come root: `gpasswd -d supervisor devloop-roles` e `gpasswd -d coordinator
devloop-roles`, `rm -r /var/lib/devloop-coordination/to-coordinator
/var/lib/devloop-coordination/to-supervisor`, `chgrp supervisor /var/lib/devloop-coordination`,
poi i file `*.prima-543` al loro posto con `mv`, e
`setup-coordinator.sh` dal checkout della versione di prima, che rimette copia e sudoers di
prima. L'ACL `u:supervisor:r--` sui due file resta: è quella che serviva già.

## Risveglio dei ruoli e watchdog dei ruoli (#602)

Il passo sull'host di #602, su un host già attivo, a loop attivo: non tocca devloop né la copia
fissata. Porta il risveglio dei ruoli (`ops/host/role-wait.sh`) al supervisore, con la sua copia
di root, i battiti nella casella e il watchdog dei ruoli, il timer
`devloop-role-watchdog@<progetto>` ([ops/README.md](../README.md#10-coordinatore-facoltativo),
"Watchdog dei ruoli"). Nei comandi, `<checkout>` è il checkout dell'operatore e `<progetto>` il
`PROJECT` del file di parametri (`_default` senza `PROJECT`); `$COORDINATOR_USER`,
`$COORDINATION_GROUP` e `$WATCHDOG_USER` sono i valori del file di parametri o i loro default
(`ops/params.env.example`). Per un altro progetto dello stesso host si ripete il passo con il suo
file di parametri (`--params`).

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`):

1. **I parametri.** Nessun parametro nuovo è obbligatorio: `SUPERVISOR_OPS_DIR`,
   `WATCHDOG_OPS_DIR` e `WATCHDOG_USER` hanno default per progetto (`ops/params.env.example`).
   Il periodo del risveglio è ora di un'ora per entrambi i ruoli (`ROLE_WAIT_PERIOD`): se il file
   di parametri scrive `COORDINATOR_WAIT_PERIOD`, il coordinatore tiene quel valore; per l'ora
   della decisione si toglie la riga.

   ```bash
   grep -n '^COORDINATOR_WAIT_PERIOD=' <checkout>/ops/params.env
   ```

2. **Il supervisore**: la directory `heartbeat/` della casella, la copia di root di
   `role-wait.sh` e le unità template del watchdog; con il coordinatore del progetto (l'overlay lo
   dichiara), anche l'istanza:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh
   ```

3. **Il coordinatore**, se c'è: la copia di root aggiornata, con `role-wait.sh`, e l'istanza del
   watchdog, cioè l'utente `$WATCHDOG_USER` (fuori dal gruppo `$COORDINATION_GROUP`), le sue ACL
   sulla casella del progetto, la copia di root del watchdog, le drop-in di
   `devloop-role-watchdog@<progetto>` e `devloop-role-watchdog-failed@<progetto>`, e il timer
   abilitato e avviato:

   ```bash
   sudo bash <checkout>/ops/host/setup-coordinator.sh --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh
   ```

4. **Verificare**: nessun errore nei passi `Casella fra supervisore e coordinatore`, `Risveglio del
   supervisore` e `Watchdog dei ruoli`; il timer attivo, con il prossimo controllo entro 10
   minuti:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --no-apps
   systemctl list-timers 'devloop-role-watchdog@*'
   ```

5. **Riavviare le sessioni** di supervisore e coordinatore con `ops/host/start-role.sh` (passo
   dell'operatore), prima del primo controllo del watchdog: il primo messaggio dà a ciascun ruolo
   il comando di risveglio suo e dell'altro, e la sezione *Watching* del runbook lo avvia. La
   sveglia di prima del coordinatore si ferma con
   `pkill -u "$COORDINATOR_USER" -f coordinator-wait.sh`. Dopo il primo controllo, il journal dice
   lo stato dei due ruoli:

   ```bash
   journalctl -u devloop-role-watchdog@<progetto> -n 20 --no-pager
   ```

   La riga `supervisore ok (…), coordinatore ok (…)` dice che i battiti arrivano. Un messaggio del
   watchdog arrivato prima del riavvio lo leggono i ruoli all'avvio, e lo spostano in `done/`.

**Rollback**: `systemctl disable --now devloop-role-watchdog@<progetto>.timer` ferma il watchdog;
i ruoli tornano alla sveglia di prima con `coordinator-wait.sh`, che resta lo stesso comando.

## Cartella to-operator della casella (#663)

Il passo sull'host di #663, su un host con supervisore e coordinatore già installati: non tocca
devloop né i ruoli. Aggiunge alla casella di ogni progetto (`COORDINATION_DIR`) la cartella
`to-operator/` con la sua `done/`, di root, del gruppo `$COORDINATION_GROUP`, 2770 come le altre
cartelle della casella. I ruoli ci lasciano le comunicazioni per l'operatore che non vanno su
GitHub: il coordinatore quelle che prima lasciava nel riepilogo della sua sessione, il supervisore
solo nei casi in cui scrive all'operatore direttamente (i runbook del
[coordinatore](../../docs/devloop/roles/coordinator.md#the-operators-folder-663) e del
[supervisore](../../docs/devloop/roles/supervisor.md#channel-with-the-coordinator)). Finché la
cartella manca, `check-host.sh` la segnala come errore. Nei comandi, `<checkout>` è il checkout
dell'operatore e `<parametri>` il file di parametri del progetto; `$COORDINATION_DIR` e
`$COORDINATION_GROUP` sono i valori di quel file o i loro default (`ops/params.env.example`).

Il codice di devloop non ne ha bisogno, ma devloop mergia la PR di #663 come le altre e non fa il
pin automatico di quel commit né dei successivi finché l'operatore non fa il pin a mano dopo il
passo ([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #663), **per ogni progetto
dell'host** con supervisore o coordinatore (uno con `SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **la cartella**, prima con `--dry-run`: le azioni nuove sono le due `install -d -m 2770 -o root
   -g $COORDINATION_GROUP` di `$COORDINATION_DIR/to-operator` e `$COORDINATION_DIR/to-operator/done`.
   Un'altra azione che il passo non spiega vuol dire che l'host non corrisponde ai parametri: ci si
   ferma e si chiede all'operatore. Lo script del supervisore crea la cartella, quello del
   coordinatore, se c'è, la trova già:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

2. **la verifica**: nessun `ERRORE` nel passo `Casella fra supervisore e coordinatore`, con le
   righe `ok: …/to-operator: 2770, gruppo …` e `ok: …/to-operator/done: 2770, gruppo …`:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

3. **le sessioni dei ruoli**: chiuderle e riavviarle con `ops/host/start-role.sh` (passo
   dell'operatore), perché leggano il runbook che descrive `to-operator/`;

4. **il pin a mano**, a loop fermo, del commit di #663 o di uno successivo, come utente
   `DEVLOOP_USER`, poi l'avvio: `systemctl stop <SERVICE_NAME>`, `devloop pin origin/main` (con
   più progetti `devloop --project <progetto> pin origin/main`), `systemctl start
   <SERVICE_NAME>`. Il pin a mano fa riprendere il pin automatico.

**Leggere i messaggi.** L'operatore li legge dalla sua shell, come root, in
`$COORDINATION_DIR/to-operator/` (un file per messaggio, con la data e l'ora UTC nel nome), poi li
sposta in `to-operator/done/` o li rimuove. Nessun ruolo legge quella cartella, e il risveglio dei
ruoli non la guarda.

**Rollback**: dopo aver letto i messaggi rimasti, `rm -r $COORDINATION_DIR/to-operator`; i ruoli
tornano a lasciare le comunicazioni per l'operatore nel riepilogo della sessione con il runbook di
prima.

## Sveglia del coordinatore sulle risposte dell'operatore (#676)

Il passo sull'host di #676, su un host con il coordinatore già installato. Con #676 `devloop
watch` scrive `operator reply`, al posto di `operator comment`, per un commento che l'operatore
scrive di persona quando la sua prima riga è un comando `/devloop` o quando l'issue o la PR è in
`agent:needs-operator`; la sveglia dei ruoli (`ops/host/role-wait.sh --role coordinator`) esce
anche su quella riga ([runbook del
coordinatore](../../docs/devloop/roles/coordinator.md#watching)). La sveglia gira dalla copia di
root di `ops/` del coordinatore: finché resta quella di prima, il coordinatore vede le risposte
dell'operatore solo al controllo periodico o con un messaggio nella casella. Il supervisore si
sveglia su ogni evento e non cambia, ma la sua copia di root di `role-wait.sh` si aggiorna
anche lei, perché `check-host.sh` non la segnali diversa da quella del checkout. Nei comandi,
`<checkout>` è il checkout dell'operatore e `<parametri>` il file di parametri del progetto.

L'ordine fra pin e copie non conta: la copia fissata di prima non scrive `operator reply`, e la
sveglia di prima non la aspetta (il supervisore, con la copia nuova di devloop, la riceve come
ogni altro evento). devloop mergia la PR di #676 come le altre, ma non fa il pin automatico di
quel commit né dei successivi finché l'operatore non fa il pin a mano dopo il passo ([Ogni
cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #676), **per ogni progetto
dell'host** con supervisore o coordinatore (uno con `SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **le copie di root**, prima con `--dry-run`: le azioni nuove sono solo l'aggiornamento delle
   copie di root di `ops/` (con `host/role-wait.sh`). Un'altra azione che il passo non spiega
   vuol dire che l'host non corrisponde ai parametri: ci si ferma e si chiede all'operatore. Lo
   script del coordinatore si esegue solo se il progetto ha il coordinatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

2. **la verifica**: nessun `ERRORE` nei passi `Risveglio del supervisore` e del coordinatore:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

3. **il pin a mano**, a loop fermo, del commit di #676 o di uno successivo, come utente
   `DEVLOOP_USER`, poi l'avvio: `systemctl stop <SERVICE_NAME>`, `devloop pin origin/main` (con
   più progetti `devloop --project <progetto> pin origin/main`), `systemctl start
   <SERVICE_NAME>`. Il pin a mano fa riprendere il pin automatico;

4. **le sessioni dei ruoli**: non serve riavviarle per la sveglia, che ogni risveglio rilancia
   dalla copia di root; il coordinatore legge però la parte nuova del suo runbook solo da una
   sessione nuova, che l'operatore avvia quando vuole con `ops/host/start-role.sh`.

**Rollback**: i passi 1 e 2 dal checkout della versione di prima rimettono la sveglia di prima;
il pin a mano della copia di prima, a loop fermo, rimette `operator comment` su ogni commento
dell'operatore.

## Risveglio dei ruoli senza la routine (#682)

Il passo sull'host di #682, su un host con il supervisore o il coordinatore già installati. Con
#682 `devloop watch` segna, per l'autore che verifica, i commenti delle app di ruolo
(`supervisor comment` con `[roles.supervisor]` come tabella, `coordinator comment` con
`[roles.coordinator]`) e l'archivio del riepilogo giornaliero scritto dal dispatcher (`report`);
la sveglia dei ruoli (`ops/host/role-wait.sh`) non esce più su quelle righe quando sono i
commenti del ruolo stesso, il diario dell'altro ruolo sull'issue degli allarmi o il riepilogo
([runbook del supervisore](../../docs/devloop/roles/supervisor.md#watching), [del
coordinatore](../../docs/devloop/roles/coordinator.md#watching)). La sveglia gira dalle copie di
root di `ops/` dei ruoli: finché restano quelle di prima, il supervisore si sveglia ancora sulla
routine. Nei comandi, `<checkout>` è il checkout dell'operatore e `<parametri>` il file di
parametri del progetto.

L'ordine fra pin e copie non conta: la sveglia nuova con la copia fissata di prima non salta
niente (quella copia non scrive le righe nuove), e la sveglia di prima con la copia fissata nuova
sveglia il supervisore su ogni riga, come prima, e il coordinatore solo sui commenti dell'issue
degli allarmi che non sono dei ruoli né il riepilogo. devloop mergia la PR di #682 come le altre,
ma non fa il pin automatico di quel commit né dei successivi finché l'operatore non fa il pin a
mano dopo il passo ([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #682), **per ogni progetto
dell'host** con supervisore o coordinatore (uno con `SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **le copie di root**, prima con `--dry-run`: le azioni nuove sono solo l'aggiornamento delle
   copie di root di `ops/` (con `host/role-wait.sh`). Un'altra azione che il passo non spiega
   vuol dire che l'host non corrisponde ai parametri: ci si ferma e si chiede all'operatore. Lo
   script del coordinatore si esegue solo se il progetto ha il coordinatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

2. **la verifica**: nessun `ERRORE` nei passi `Risveglio del supervisore` e del coordinatore:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

3. **il pin a mano**, a loop fermo, del commit di #682 o di uno successivo, come utente
   `DEVLOOP_USER`, poi l'avvio: `systemctl stop <SERVICE_NAME>`, `devloop pin origin/main` (con
   più progetti `devloop --project <progetto> pin origin/main`), `systemctl start
   <SERVICE_NAME>`. Il pin a mano fa riprendere il pin automatico;

4. **le sessioni dei ruoli**: non serve riavviarle per la sveglia, che ogni risveglio rilancia
   dalla copia di root; i ruoli leggono però la parte nuova del loro runbook solo da una sessione
   nuova, che l'operatore avvia quando vuole con `ops/host/start-role.sh`.

**Rollback**: i passi 1 e 2 dal checkout della versione di prima rimettono la sveglia di prima;
il pin a mano della copia di prima, a loop fermo, rimette `comment` sui commenti delle app di
ruolo e sull'archivio del riepilogo.

## Il flusso degli eventi per le sveglie dei ruoli (#683)

Il passo sull'host di #683, su un host con il supervisore o il coordinatore già installati. Con
#683 GitHub lo legge solo `devloop run`, che scrive gli eventi nel flusso degli eventi dello
`state_dir` (`<state_dir>/events/`, [reference](../../docs/devloop/reference.md#the-stream-of-the-events));
la sveglia dei ruoli (`ops/host/role-wait.sh`) lo legge come utente del ruolo, con un'ACL di sola
lettura, e non esegue più `devloop-pinned watch` come `DEVLOOP_USER`: la regola dei sudoers dei
ruoli per `watch` si toglie ([ops/README.md](../README.md#10-coordinatore-facoltativo), "Il
flusso degli eventi"). Servono quindi la copia fissata nuova (che scrive il flusso), le ACL dei
ruoli sul flusso, i sudoers nuovi e le copie di root nuove di `ops/` (con `host/role-wait.sh` e
`lib.sh`). Nei comandi, `<checkout>` è il checkout dell'operatore e `<parametri>` il file di
parametri del progetto.

L'ordine conta: prima il pin, così il flusso esiste e si scrive quando arrivano le sveglie nuove;
poi le copie, le ACL e i sudoers. Fino al passo 2 le sveglie di prima leggono GitHub con il
`watch` dei sudoers di prima, come sempre. devloop mergia la PR di #683 come le altre, ma non fa
il pin automatico di quel commit né dei successivi finché l'operatore non fa il pin a mano
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)): qui il pin a mano è il
passo 1.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #683), **per ogni progetto
dell'host** con supervisore o coordinatore (uno con `SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **il pin a mano**, a loop fermo, del commit di #683 o di uno successivo, come utente
   `DEVLOOP_USER`, poi l'avvio: `systemctl stop <SERVICE_NAME>`, `devloop pin origin/main` (con
   più progetti `devloop --project <progetto> pin origin/main`), `systemctl start
   <SERVICE_NAME>`. Il pin a mano fa riprendere il pin automatico. Entro un minuto dall'avvio
   `devloop run` crea `<state_dir>/events/` (di `DEVLOOP_USER`, 750) con `id` e `alive`;

2. **le ACL, i sudoers e le copie di root**, prima con `--dry-run`: le azioni nuove sono solo
   l'ACL `u:<ruolo>:r-x,d:u:<ruolo>:r--` su `<state_dir>/events` e `u:<ruolo>:r--` sui suoi
   file, il sudoers del ruolo senza la riga di `devloop-pinned watch`, e l'aggiornamento delle
   copie di root di `ops/` (con `host/role-wait.sh` e `lib.sh`). Un'altra azione che il passo non
   spiega vuol dire che l'host non corrisponde ai parametri: ci si ferma e si chiede
   all'operatore. Lo script del coordinatore si esegue solo se il progetto ha il coordinatore. Con
   un insieme di app condiviso (#643) l'utente di un ruolo riceve l'ACL sul flusso di ciascun
   progetto dall'esecuzione per quel progetto:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

   Una sveglia di prima che gira in quel momento esce con `errore:` al suo giro successivo (il
   sudoers non ha più `watch`): il ruolo la riavvia, dalla copia nuova. Un giro di `devloop watch`
   di prima ancora in corso finisce da sé, entro 300 secondi;

3. **la verifica**: nessun `ERRORE` nei passi `Flusso degli eventi`, `Risveglio del supervisore`
   e del coordinatore, e la riga `non ha la regola dei sudoers per devloop-pinned watch` per ogni
   sudoers dei ruoli:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

4. **il file di stato di prima** del watch del coordinatore, `<state_dir>/coordinator-watch-state.json`,
   non serve più: lo si può togliere, come `DEVLOOP_USER`. `<state_dir>/watch-state.json` resta:
   è il file di stato di `devloop watch` interattivo;

5. **le sessioni dei ruoli**: non serve riavviarle per la sveglia, che ogni risveglio rilancia
   dalla copia di root. Al primo risveglio dalla copia nuova ciascun ruolo crea la sua base alla
   fine del flusso (`flusso: base …`): ciò che è cambiato fra l'ultimo giro di prima e la base lo
   vede al controllo periodico. I ruoli leggono la parte nuova del loro runbook solo da una
   sessione nuova, che l'operatore avvia quando vuole con `ops/host/start-role.sh`.

`EVENTS_STALE_AFTER` (300 secondi per default) si scrive in `params.env` solo per un'altra soglia
del flusso fermo; poi si rieseguono gli script del passo 2, che ne aggiornano la copia.

**Rollback**: il passo 2 dal checkout della versione di prima rimette le sveglie di prima, con
la regola dei sudoers per `devloop-pinned watch`; il pin a mano della copia di prima, a loop
fermo, ferma il flusso, che resta nello `state_dir` senza lettori. Le ACL di sola lettura sul
flusso non danno altro che la lettura del flusso stesso; per toglierle, `setfacl -b
<state_dir>/events` e sui suoi file, come root.

## Il riposo dei ruoli (#684)

Il passo sull'host di #684, su un host con il supervisore e il coordinatore già installati e con
il flusso degli eventi di [#683](#il-flusso-degli-eventi-per-le-sveglie-dei-ruoli-683). Con #684
un ruolo che a fine turno non ha niente da fare entra in riposo (`role-wait.sh --rest`): dichiara
lo stato `$COORDINATION_DIR/rest/<ruolo>`, tiene il lock `rest/<ruolo>.lock` e non scrive
battiti; lo sveglia la sentinella dei ruoli, il servizio `devloop-role-sentinel@<progetto>`
(`ops/host/role-sentinel.sh`), senza modello né chiavi; `devloop run` non avvia un controllo di
prontezza né un'implementazione mentre un ruolo è a riposo, e il watchdog dei ruoli distingue il
riposo dal ruolo fermo ([ops/README.md](../README.md#10-coordinatore-facoltativo), "Il riposo
dei ruoli"; [ADR 0002](../../docs/adr/0002-ruoli-e-responsabilita.md), precisazione del
2026-11-26). Servono quindi:

- la directory `$COORDINATION_DIR/rest/`, di root, del gruppo dei ruoli, 3770;
- l'utente della sentinella (`SENTINEL_USER`, per default `role-sentinel`, con `PROJECT`
  `role-sentinel-<progetto>`), fuori dal gruppo dei ruoli, con le sue ACL: `--x` sulla casella,
  `-wx` su `rest/` (il segnale di risveglio), `r-x` su `to-supervisor/` e `to-coordinator/`, la
  lettura del flusso degli eventi e di `run.log`; le sue unità e la drop-in dell'istanza, il
  servizio abilitato e avviato. L'overlay non lo legge: l'issue degli allarmi la trova nel file
  `alert_issue` del flusso, che `devloop run` scrive al suo avvio dopo il pin (passo 3);
- per il watchdog dei ruoli, `--x` su `rest/`, `rwx` (non più `-wx`) sulle due caselle, per
  l'età dei messaggi di un ruolo a riposo, e la lettura del flusso degli eventi, con la sua unità
  che legge lo `state_dir` (`ProtectHome=read-only`);
- per `DEVLOOP_USER`, `--x` su `$COORDINATION_DIR` e su `rest/`, per leggere lo stato di riposo;
- le copie di root nuove di `ops/` (con `host/role-wait.sh`, `host/role-sentinel.sh`,
  `host/role-watchdog.sh` e `lib.sh`);
- la chiave `rest_dir` di `[run]` nell'overlay del progetto, con il percorso di `rest/`: senza,
  `devloop run` non guarda il riposo dei ruoli e avvia il lavoro come prima.

Nei comandi, `<checkout>` è il checkout dell'operatore e `<parametri>` il file di parametri del
progetto. devloop mergia la PR di #684 come le altre, ma non fa il pin automatico di quel commit
né dei successivi finché l'operatore non fa il pin a mano
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)): qui è il passo 3,
che fa il pin prima di scrivere `rest_dir` nell'overlay e avvia il servizio dopo.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #684), **per ogni progetto
dell'host** con supervisore e coordinatore (uno con `SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **il loop fermo**: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli restano: finché non
   leggono il runbook nuovo usano la sveglia normale;

2. **la casella, gli utenti, le ACL, le unità e le copie di root**, prima con `--dry-run`. Le
   azioni nuove sono quelle elencate sopra; un'altra azione che il passo non spiega vuol dire che
   l'host non corrisponde ai parametri: ci si ferma e si chiede all'operatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

   Il secondo script trova già fatto ciò che il primo ha fatto;

3. **il pin a mano, l'overlay e l'avvio**, in quest'ordine, con il loop ancora fermo. Il pin si
   fa con l'overlay ancora senza `rest_dir`: il codice fissato prima del pin non conosce la
   chiave, rifiuta le chiavi sconosciute di `[run]` e il pin si fermerebbe con `devloop: invalid
   config: local config: unknown key 'run.rest_dir'`
   ([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)):

   1. come utente `DEVLOOP_USER`, `devloop pin origin/main` (con più progetti
      `devloop --project <progetto> pin origin/main`). Il pin a mano fa riprendere il pin
      automatico;
   2. come l'operatore (l'overlay è suo), in `[run]` di `<state_dir>/config.local.toml` la riga
      `rest_dir = "<COORDINATION_DIR>/rest"`, con il valore di `COORDINATION_DIR` dei parametri;
      `rest_wake_timeout` (300 secondi per default) solo per un'altra soglia;
   3. `systemctl start <SERVICE_NAME>`, che legge l'overlay con `rest_dir` al suo avvio.

   Per un progetto con `SOURCE_REF`, dalla [sua
   procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666), l'ordine è lo stesso: il pin è il
   suo passo 5, `rest_dir` si scrive al suo passo 6, prima dell'avvio, mai al suo passo 3;

4. **la verifica**: nessun `ERRORE` nei passi `Casella fra supervisore e coordinatore`,
   `Watchdog dei ruoli`, `Sentinella dei ruoli a riposo` e `Flusso degli eventi`, con la riga
   `devloop-role-sentinel@<progetto>.service è attivo`:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

5. **la prova delle CLI dei ruoli**, una breve per ogni CLI in uso come ruolo (`claude`, `codex`
   o `agy` delle tabelle `[roles.supervisor]` e `[roles.coordinator]`), da una sessione di prova
   avviata con `ops/host/start-role.sh` come per il ruolo: si chiede alla sessione di lanciare in
   background `bash <copia di root>/host/role-wait.sh --role <ruolo> --rest` e di dire che cosa
   stampa quando esce; dopo qualche minuto, come root, un messaggio nella casella del ruolo
   (`<COORDINATION_DIR>/to-<ruolo>/`, con il comando del runbook, "Channel"). La prova riesce se
   la sentinella sveglia il ruolo (la riga `sentinella: casella: …` arriva alla sessione), e se,
   ripresa la sessione dopo averla chiusa con il comando in attesa, la CLI vede ancora l'uscita
   del comando o lo rilancia. Una CLI che non regge l'attesa (il comando esce prima del segnale,
   la sessione non ne vede l'uscita, o la ripresa lo perde) non usa il riposo: l'operatore lo
   scrive nella casella del ruolo, e il ruolo usa la sveglia normale, come prima ([runbook del
   supervisore](../../docs/devloop/roles/supervisor.md#rest), [del
   coordinatore](../../docs/devloop/roles/coordinator.md#rest)). Il messaggio di prova si toglie
   dalla casella;

6. **le sessioni dei ruoli**: leggono la parte nuova del loro runbook solo da una sessione nuova,
   che l'operatore avvia quando vuole con `ops/host/start-role.sh`.

`REST_DAILY_CHECK` (l'ora UTC del controllo giornaliero, 00:30), `REST_WAKE_TIMEOUT` e
`REST_MAILBOX_AGE` (300 secondi) si scrivono in `params.env` solo per altri valori; poi si
rieseguono gli script del passo 2, che ne aggiornano la copia.

**Rollback**: senza `rest_dir` nell'overlay (e un riavvio del servizio) `devloop run` non guarda
più il riposo; per il pin della copia di prima, che rifiuta la chiave, `rest_dir` (e
`rest_wake_timeout`) si toglie **prima** del pin. Un ruolo a riposo si sveglia con un messaggio
nella sua casella finché la sentinella è attiva, e poi usa la sveglia normale. `systemctl disable --now
devloop-role-sentinel@<progetto>.service` ferma la sentinella; il passo 2 dal checkout della
versione di prima rimette le copie e le ACL di prima del watchdog. La directory `rest/` e
l'utente della sentinella restano senza effetto; per toglierli, come root, `rm -r
<COORDINATION_DIR>/rest` e `userdel <SENTINEL_USER>`.

## La pausa dell'operatore (#685)

Il passo sull'host di #685, su un host con il riposo dei ruoli di
[#684](#il-riposo-dei-ruoli-684) già fatto (la directory `rest/`, la sentinella e `rest_dir`
nell'overlay, scritto dopo il pin a mano del passo 3 di #684). Con #685 l'operatore ferma tutto,
anche con issue e PR aperte, con il comando `devloop` dell'host ([#588](#comando-devloop-dellhost-588)): `devloop --project <progetto> pause`
scrive come root la pausa `$COORDINATION_DIR/rest/pause` e ferma il servizio; `resume` la toglie
e riavvia il servizio. In pausa i ruoli aspettano con `role-wait.sh` come a riposo, con il motivo
`pausa`, la sentinella li sveglia solo per un messaggio o un allarme, il watchdog dei ruoli li vede
in pausa e dice una volta il lavoro in attesa, e `devloop run` non parte finché la pausa c'è
([ops/README.md](../README.md#10-coordinatore-facoltativo), "La pausa dell'operatore"; [ADR
0002](../../docs/adr/0002-ruoli-e-responsabilita.md), precisazione del 2026-11-26 sulla pausa).
Servono quindi:

- il file del progetto del comando `devloop` (`$DEVLOOP_PROJECTS_DIR/params-<progetto>.env`) con le
  due righe nuove `SERVICE_NAME` e `COORDINATION_DIR`, e il comando nuovo con `pause` e `resume`:
  senza, `pause` e `resume` si fermano e chiedono di rieseguire `setup-devloop.sh`, e gli altri
  sottocomandi funzionano come prima;
- le copie di root nuove di `ops/` (con `lib.sh`, `host/role-wait.sh`, `host/role-sentinel.sh`,
  `host/role-watchdog.sh` e `host/check-host.sh`), e la sentinella riavviata, che altrimenti gira
  ancora la copia di prima;
- `$COORDINATION_DIR` con lo sticky bit, 3770 invece di 2770: senza, un ruolo, che vi scrive,
  rinominerebbe `rest/` e la pausa sparirebbe dal percorso che devloop e i ruoli leggono. `pause`
  si ferma finché `$COORDINATION_DIR` e `rest/` non hanno lo sticky bit;
- il pin del codice nuovo di devloop, che non parte durante la pausa e scrive nel flusso degli
  eventi la coda dell'ultimo tick (`events/queue`), per il watchdog.

Nessuna chiave nuova dell'overlay, nessuna unità, nessun sudoers e nessuna ACL nuovi: la pausa
vive in `rest/`, che il riposo ha già dato a tutti quelli che la leggono; cambia solo il modo di
`$COORDINATION_DIR`. devloop mergia la PR di
#685 come le altre, ma non fa il pin automatico di quel commit né dei successivi finché
l'operatore non fa il pin a mano ([Ogni cambio con un passo
sull'host](#ogni-cambio-con-un-passo-sullhost)): qui è il passo 4.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #685), **per ogni progetto
dell'host** con supervisore e coordinatore, con il suo file di parametri `<parametri>` (uno con
`SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **il loop fermo**: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli restano;

2. **il comando `devloop` e il file del progetto**, prima con `--dry-run`: le azioni nuove sono la
   riscrittura del file del progetto e del comando. Un'altra azione che il passo non spiega vuol
   dire che l'host non corrisponde ai parametri: ci si ferma e si chiede all'operatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri>
   ```

3. **le copie di root di `ops/` e lo sticky bit della casella**, prima con `--dry-run`: le azioni
   nuove sono solo le copie (`SUPERVISOR_OPS_DIR`, `COORDINATOR_OPS_DIR`, `WATCHDOG_OPS_DIR`) e,
   su `$COORDINATION_DIR`, `chown root:<COORDINATION_GROUP>` e `chmod 3770`; poi la sentinella
   riavviata sulla copia nuova:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   sudo systemctl restart devloop-role-sentinel@<progetto>.service
   ```

   Il secondo script trova già fatto ciò che il primo ha fatto. `<progetto>` è `PROJECT`, o
   `_default` senza `PROJECT`;

4. **il pin a mano**: come utente `DEVLOOP_USER`, `devloop pin origin/main` (con più progetti
   `devloop --project <progetto> pin origin/main`), poi `systemctl start <SERVICE_NAME>`. Il pin a
   mano fa riprendere il pin automatico;

5. **la verifica**: nessun `ERRORE`, nel passo *Comando devloop* le righe `ok: … coincide con
   ops/host/devloop-command` e `… devloop-command-project`, nel passo della casella la riga
   `ok: $COORDINATION_DIR: 3770, gruppo …`, e la riga
   `devloop-role-sentinel@<progetto>.service è attivo`:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

6. **le sessioni dei ruoli**: leggono la parte nuova del loro runbook solo da una sessione nuova,
   che l'operatore avvia quando vuole con `ops/host/start-role.sh`. Le loro sveglie già in attesa
   girano la copia di prima finché il ruolo non le rilancia: fino ad allora una pausa ferma il
   servizio, ma non le mette in pausa.

La pausa la apre e la chiude solo l'operatore, di persona: l'agent dell'host non esegue `pause`
né `resume`, nemmeno per provarli. La prova, se l'operatore la vuole: `devloop --project
<progetto> pause`, poi, dopo
qualche minuto, nel journal del watchdog (`journalctl -u devloop-role-watchdog@<progetto>`) la riga
`informazione: … in pausa per ordine dell'operatore dal …` con il lavoro in attesa, e
`check-host.sh` che dice la pausa; poi `devloop --project <progetto> resume`, con il servizio di
nuovo attivo e i ruoli che si svegliano con una riga `ripresa:`.

**Rollback**: senza pausa in corso (`devloop --project <progetto> resume`, se serve), il passo 2 e
il passo 3 dal checkout della versione di prima rimettono il comando, il file del progetto e le
copie di prima, e il pin della versione di prima il codice di prima. Una pausa lasciata da togliere
a mano si toglie come root con `rm <COORDINATION_DIR>/rest/pause`, poi `systemctl start
<SERVICE_NAME>`.

## La manutenzione dell'host (#705)

Il passo sull'host di #705, su un host con la pausa dell'operatore di
[#685](#la-pausa-delloperatore-685) già fatta. Con #705 chi fa un passo sull'host lo apre con
`devloop --project <progetto> maintenance start "<sezioni>"` e lo chiude con `devloop --project
<progetto> maintenance end`, sottocomandi del comando `devloop` dell'host
([#588](#comando-devloop-dellhost-588)): `start` crea come root
`$COORDINATION_DIR/rest/maintenance` e ferma il servizio, `end` la toglie e riavvia il servizio
(salvo una pausa dell'operatore). Mentre la manutenzione c'è, `devloop run` non fa tick e
`devloop pin` si rifiuta, salvo il pin dello stesso comando `devloop` dell'host; i ruoli ricevono
ogni cambio dalla loro sveglia e aspettano in manutenzione, la sentinella li sveglia solo per un
messaggio, il watchdog dei ruoli li vede in manutenzione e alla scadenza lo dice all'operatore
([ops/README.md](../README.md#10-coordinatore-facoltativo), "La manutenzione dell'host"). Servono
quindi:

- il file del progetto del comando `devloop` (`$DEVLOOP_PROJECTS_DIR/params-<progetto>.env`) con le
  tre righe nuove `STATE_PATH` (lo state_dir, con il lock dei pin che `maintenance start` aspetta),
  `MAINTENANCE_TIMEOUT` e `MAINTENANCE_ACK_TIMEOUT`, e il comando nuovo con `maintenance`: senza
  `STATE_PATH` il comando rifiuta `maintenance start`, senza le altre due usa i default (7200 e 300
  secondi), e `check-host.sh` vede il file diverso da quello che `setup-devloop.sh` scrive;
- le due unità nuove dell'avviso della scadenza, comuni ai progetti dell'host,
  `$SYSTEMD_DIR/devloop-maintenance@.service` e `$SYSTEMD_DIR/devloop-maintenance@.timer`, e il
  timer del progetto abilitato, `devloop-maintenance@<progetto>.timer`: ogni 5 minuti, come root,
  `devloop --project <progetto> maintenance expired`, che senza una manutenzione scaduta non fa
  nulla;
- le copie di root nuove di `ops/` (con `lib.sh`, `host/role-wait.sh`, `host/role-sentinel.sh`,
  `host/role-watchdog.sh` e `host/check-host.sh`), e la sentinella riavviata, che altrimenti gira
  ancora la copia di prima;
- il pin del codice nuovo di devloop, che non fa tick e rifiuta il pin durante la manutenzione.

Nessuna chiave nuova dell'overlay: l'ordine pin poi overlay di
[#702](#il-riposo-dei-ruoli-684) vale qui senza il secondo passo. Nessun sudoers e nessuna ACL
nuovi: la manutenzione vive in `rest/`, che il riposo ha già dato a tutti quelli che la leggono, e
l'avviso della scadenza è il timer di root che `setup-devloop.sh` installa e abilita, con il
comando `devloop` dell'host. `MAINTENANCE_TIMEOUT` (la durata, contata dallo stop
del servizio, 7200 secondi) e `MAINTENANCE_ACK_TIMEOUT` (l'attesa delle conferme dei ruoli, 300
secondi) si scrivono in `params.env` solo per altri valori, prima del passo 2. devloop mergia la
PR di #705 come le altre, ma non fa il pin automatico di quel commit né dei successivi finché
l'operatore non fa il pin a mano ([Ogni cambio con un passo
sull'host](#ogni-cambio-con-un-passo-sullhost)): qui è il passo 4. Questo passo non usa ancora la
manutenzione: il codice che la rispetta arriva con il suo pin.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #705), **per ogni progetto
dell'host** con supervisore e coordinatore, con il suo file di parametri `<parametri>` (uno con
`SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **il loop fermo**: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli restano;

2. **il comando `devloop`, il file del progetto e il timer della scadenza**, prima con
   `--dry-run`: le azioni nuove sono la riscrittura del file del progetto e del comando, la
   scrittura di `devloop-maintenance@.service` e `devloop-maintenance@.timer` in `SYSTEMD_DIR`
   (al primo progetto dell'host), con `systemctl daemon-reload`, e `systemctl enable --now
   devloop-maintenance@<progetto>.timer`. Un'altra azione che il passo non spiega vuol dire che
   l'host non corrisponde ai parametri: ci si ferma e si chiede all'operatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri>
   ```

3. **le copie di root di `ops/`**, prima con `--dry-run`: le azioni nuove sono solo le copie
   (`SUPERVISOR_OPS_DIR`, `COORDINATOR_OPS_DIR`, `WATCHDOG_OPS_DIR`); poi la sentinella riavviata
   sulla copia nuova:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   sudo systemctl restart devloop-role-sentinel@<progetto>.service
   ```

   Il secondo script trova già fatto ciò che il primo ha fatto. `<progetto>` è `PROJECT`, o
   `_default` senza `PROJECT`;

4. **il pin a mano**: come utente `DEVLOOP_USER`, `devloop pin origin/main` (con più progetti
   `devloop --project <progetto> pin origin/main`), poi `systemctl start <SERVICE_NAME>`. Il pin a
   mano fa riprendere il pin automatico;

5. **la verifica**: nessun `ERRORE`, nel passo *Comando devloop* le righe `ok: … coincide con
   ops/host/devloop-command` e `… devloop-command-project`, nel passo *Servizio* le righe `ok: …
   coincide con ops/host/devloop-maintenance@.service`, `… devloop-maintenance@.timer` e
   `devloop-maintenance@<progetto>.timer è abilitato`, e la riga
   `devloop-role-sentinel@<progetto>.service è attivo`:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

6. **le sessioni dei ruoli**: le loro sveglie già in attesa girano la copia di prima finché il
   ruolo non le rilancia: fino ad allora una manutenzione ferma il servizio, ma non arriva ai
   ruoli. Chi la usa e quando è in [Ogni cambio con un passo
   sull'host](#ogni-cambio-con-un-passo-sullhost) (#706), e i compiti dei ruoli nei loro runbook.

**Un progetto con `SOURCE_REF`** (#687) ha il comando e le sveglie del suo tag: finché il tag non
contiene #705, per quel progetto la manutenzione non c'è, e i passi sull'host si fanno come oggi
(`systemctl stop`, i passi, `systemctl start`), senza `maintenance`. Questa sezione la applica al
tag che la contiene, con [la sua procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666).

**La prova**, se l'operatore la vuole, a fine passo: `devloop --project <progetto> maintenance
start "prova"`, che ferma il servizio e dice quali ruoli confermano; dopo qualche minuto, nel
journal del watchdog (`journalctl -u devloop-role-watchdog@<progetto>`) la riga
`informazione: … in manutenzione dal …`, e `check-host.sh` che dice la manutenzione; poi
`devloop --project <progetto> maintenance end`, con il servizio di nuovo attivo e i ruoli che si
svegliano con una riga `manutenzione: finita`.

**Rollback**: senza manutenzione in corso (`devloop --project <progetto> maintenance end`, se
serve), il passo 2 e il passo 3 dal checkout della versione di prima rimettono il comando, il file
del progetto e le copie di prima, e il pin della versione di prima il codice di prima; poi, come
root, `systemctl disable --now devloop-maintenance@<progetto>.timer` e, all'ultimo progetto
dell'host, `rm <SYSTEMD_DIR>/devloop-maintenance@.service <SYSTEMD_DIR>/devloop-maintenance@.timer`
e `systemctl daemon-reload`. Una manutenzione lasciata da togliere a mano si toglie come root con
`rm <COORDINATION_DIR>/rest/maintenance`, poi `systemctl start <SERVICE_NAME>`.

## Script delle caselle e pulizia di done/ (#707)

Il passo sull'host di #707. Con #707 le caselle fra i ruoli (`$COORDINATION_DIR`: `to-supervisor/`,
`to-coordinator/`, `to-operator/`) hanno uno script, `ops/host/mailbox.sh`, al posto del comando a
mano dei runbook: i ruoli scrivono con `send` e leggono la propria casella con `list`, `show` e
`done`, l'operatore legge `to-operator/` come root con `read`, e `prune` toglie dalle `done/` delle
tre caselle i messaggi più vecchi di `MAILBOX_DONE_DAYS` giorni (30 per default, la scelta
dell'operatore). Servono quindi:

- lo script nelle copie di root dei ruoli: `mailbox.sh` in `SUPERVISOR_OPS_DIR` e in
  `COORDINATOR_OPS_DIR`, accanto a `role-wait.sh`, e nella copia del coordinatore anche le unità
  nuove che `check-host.sh --read-only` confronta;
- le due unità nuove della pulizia, comuni ai progetti dell'host,
  `$SYSTEMD_DIR/devloop-mailbox-prune@.service` e `$SYSTEMD_DIR/devloop-mailbox-prune@.timer`, la
  drop-in del progetto `$SYSTEMD_DIR/devloop-mailbox-prune@<progetto>.service.d/project.conf`, con
  la copia da cui gira lo script (quella del coordinatore, o del supervisore se il progetto non ha
  il coordinatore), e il timer del progetto abilitato: una volta al giorno, come root,
  `mailbox.sh prune`, che non tocca i messaggi non letti, fuori da `done/`.

Il parametro nuovo `MAILBOX_DONE_DAYS` si scrive in `params.env` solo per un valore diverso da 30,
prima del passo 2. Nessuna chiave nuova dell'overlay, nessun sudoers e nessuna ACL nuovi: i ruoli
eseguono lo script come sé stessi, con il gruppo della casella che hanno già, e la pulizia è un
timer di root. Il codice di devloop non cambia: devloop mergia la PR di #707 come le altre, ma non
fa il pin automatico di quel commit né dei successivi finché l'operatore non fa il pin a mano
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)): qui è il passo 3.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #707), **per ogni progetto
dell'host** con il supervisore, con il suo file di parametri `<parametri>` (uno con `SOURCE_REF`
al tag successivo, con [la sua procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **il loop fermo**: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli restano;

2. **le copie di root di `ops/` e la pulizia**, prima con `--dry-run`: le azioni nuove sono le
   copie (`SUPERVISOR_OPS_DIR`, `COORDINATOR_OPS_DIR`, `WATCHDOG_OPS_DIR`) con `host/mailbox.sh`,
   la scrittura di `devloop-mailbox-prune@.service` e `devloop-mailbox-prune@.timer` in
   `SYSTEMD_DIR` (al primo progetto dell'host) e della drop-in del progetto, con `systemctl
   daemon-reload`, e `systemctl enable --now devloop-mailbox-prune@<progetto>.timer`. Con il
   coordinatore la pulizia la installa `setup-coordinator.sh`, dalla sua copia, e
   `setup-supervisor.sh` la lascia ("il progetto ha il coordinatore"); senza, la installa
   `setup-supervisor.sh`, dalla copia del supervisore, e `setup-coordinator.sh` non si esegue.
   Un'altra azione che il passo non spiega vuol dire che l'host non corrisponde ai parametri: ci
   si ferma e si chiede all'operatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

   `<progetto>` è `PROJECT`, o `_default` senza `PROJECT`;

3. **il pin a mano**: come utente `DEVLOOP_USER`, `devloop pin origin/main` (con più progetti
   `devloop --project <progetto> pin origin/main`), poi `systemctl start <SERVICE_NAME>`. Il pin a
   mano fa riprendere il pin automatico;

4. **la verifica**: nessun `ERRORE`, nel passo *Pulizia di done/ delle caselle* le righe `ok: …
   coincide con ops/host/devloop-mailbox-prune@.service`, `… devloop-mailbox-prune@.timer`,
   `… coincide con ops/host/mailbox-prune-project.conf, dalla copia …`,
   `devloop-mailbox-prune@<progetto>.timer è abilitato` e `… è attivo`, e nelle copie di root la
   riga `ok: …/host/mailbox.sh coincide con …`:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

5. **le sessioni dei ruoli**: leggono il runbook nuovo, con lo script, al prossimo avvio
   (`start-role.sh`); fino ad allora usano il comando a mano, che resta valido: lo script pubblica
   i messaggi nello stesso modo, con lo stesso nome.

**Un progetto con `SOURCE_REF`** (#687) ha lo script con il suo tag: finché il tag non contiene
#707, per quel progetto resta il comando a mano dei runbook del tag, e `done/` non si pulisce.
Questa sezione la applica al tag che la contiene, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666).

**La prova**, se l'operatore la vuole, a fine passo: `sudo systemctl start
devloop-mailbox-prune@<progetto>.service`, poi nel journal (`journalctl -u
devloop-mailbox-prune@<progetto>`) una riga `to-…/done/: tolti …` per ciascuna delle tre caselle;
e `sudo bash <COORDINATOR_OPS_DIR>/host/mailbox.sh read` (o dalla copia del supervisore), che
stampa i messaggi non letti di `to-operator/` e li sposta in `to-operator/done/`.

**Rollback**: il passo 2 dal checkout della versione di prima rimette le copie di prima, senza lo
script (che resta nelle copie: si toglie con `rm <SUPERVISOR_OPS_DIR>/host/mailbox.sh
<COORDINATOR_OPS_DIR>/host/mailbox.sh`), e il pin della versione di prima il codice di prima; poi,
come root, `systemctl disable --now devloop-mailbox-prune@<progetto>.timer`, `rm -r
<SYSTEMD_DIR>/devloop-mailbox-prune@<progetto>.service.d` e, all'ultimo progetto dell'host, `rm
<SYSTEMD_DIR>/devloop-mailbox-prune@.service <SYSTEMD_DIR>/devloop-mailbox-prune@.timer`, e
`systemctl daemon-reload`. I messaggi che la pulizia ha già tolto da `done/` non tornano: erano
solo storico.

## Sveglia dei ruoli senza errori su stderr (#711)

Il passo sull'host di #711. Con il codice di #705, `role-wait.sh` scriveva su stderr una riga di
errore a ogni giro dell'attesa finché il ruolo non aveva mai visto una manutenzione (il file
`maintenance` dello stato della sveglia del ruolo mancava, e la redirezione d'ingresso falliva
prima di `2>/dev/null`): un output della sveglia pieno di righe inutili, che il ruolo legge a ogni
risveglio. Con #711 la sveglia legge quel file solo se c'è. Il comportamento non cambia: serve solo
`host/role-wait.sh` nuovo nelle copie di root dei ruoli (`SUPERVISOR_OPS_DIR` e
`COORDINATOR_OPS_DIR`).

Nessun parametro, nessuna chiave dell'overlay, nessun sudoers, nessuna ACL e nessuna unità nuovi;
la sentinella e il watchdog non girano `role-wait.sh`, e non si riavviano. Il codice di devloop non
cambia: devloop mergia la PR di #711 come le altre, ma non fa il pin automatico di quel commit né
dei successivi finché l'operatore non fa il pin a mano ([Ogni cambio con un passo
sull'host](#ogni-cambio-con-un-passo-sullhost)): qui è il passo 3.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #711), **per ogni progetto
dell'host** con il supervisore, con il suo file di parametri `<parametri>` (uno con `SOURCE_REF`
al tag successivo, con [la sua procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **il loop fermo**: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli restano;

2. **le copie di root di `ops/`**, prima con `--dry-run`: le azioni nuove sono solo le copie
   (`SUPERVISOR_OPS_DIR`, `COORDINATOR_OPS_DIR`, `WATCHDOG_OPS_DIR`), con `host/role-wait.sh`
   nuovo. `setup-coordinator.sh` si esegue solo se il progetto ha il coordinatore, e trova già
   fatto ciò che `setup-supervisor.sh` ha fatto. Un'altra azione che il passo non spiega vuol dire
   che l'host non corrisponde ai parametri: ci si ferma e si chiede all'operatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

3. **il pin a mano**: come utente `DEVLOOP_USER`, `devloop pin origin/main` (con più progetti
   `devloop --project <progetto> pin origin/main`), poi `systemctl start <SERVICE_NAME>`. Il pin a
   mano fa riprendere il pin automatico;

4. **la verifica**: nessun `ERRORE`, e nelle copie di root la riga `ok: …/host/role-wait.sh
   coincide con …`:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

5. **le sessioni dei ruoli**: le sveglie già in attesa girano la copia di prima finché il ruolo non
   le rilancia, al prossimo risveglio; da lì l'output della sveglia non ha più quelle righe.

**Un progetto con `SOURCE_REF`** (#687) ha la sveglia del suo tag: finché il tag non contiene
#711, le righe su stderr restano, senza altro effetto. Questa sezione la applica al tag che la
contiene, con [la sua procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666).

**Rollback**: il passo 2 dal checkout della versione di prima rimette le copie di prima, e il pin
della versione di prima il codice di prima.

## Clone dei ruoli di un repository privato (#717)

Il passo sull'host di #717. Supervisore e coordinatore lavorano in un clone del repository gestito
(`SUPERVISOR_CLONE`, `COORDINATOR_CLONE`), che `setup-supervisor.sh` e `setup-coordinator.sh`
facevano senza credenziali: un repository privato non si clonava né si aggiornava. Con #717 i
manifest delle app `supervisor` e `coordinator` hanno `contents: read`, che
`scripts/supervisor-token.py` e `scripts/coordinator-token.py` concedono solo con un `--perm
contents=read` esplicito, e i due script dell'host, quando il repository non si legge senza
credenziali, clonano il clone del ruolo, o lo aggiornano se c'è già, con un token della sua app
limitato a `contents=read` (`scripts/role-git.py`: il token solo nell'ambiente di `git`, come nei
fetch di devloop, #716). `check-host.sh` verifica che il clone di ogni ruolo si aggiorni.

Per un repository pubblico, come quello di devloop, nulla cambia sull'host: il clone si legge senza
credenziali, e gli script lo trovano già fatto. Nessun parametro, nessuna chiave dell'overlay,
nessun sudoers, nessuna ACL e nessuna unità nuovi. Il codice del loop non cambia: devloop mergia
la PR di #717 come le altre, ma non fa il pin automatico di quel commit né dei successivi finché
l'operatore non fa il pin a mano ([Ogni cambio con un passo
sull'host](#ogni-cambio-con-un-passo-sullhost)): qui è il passo 4.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #717), **per ogni progetto
dell'host** con il supervisore, con il suo file di parametri `<parametri>` (uno con `SOURCE_REF`
al tag successivo, con [la sua procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **i permessi delle app reali** (dell'operatore, sul sito di GitHub): per un insieme di app che
   serve un repository privato, le app `<APP_PREFIX>supervisor` e `<APP_PREFIX>coordinator` hanno
   `Contents: Read-only` (impostazioni dell'app, "Permissions & events"), e la richiesta dei
   permessi nuovi è accettata sull'installazione. Per l'insieme di devloop l'operatore li ha già
   aggiunti (2026-11-27); un insieme usato solo per repository pubblici può non averli;

2. **il loop fermo**: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli restano;

3. **i clone dei ruoli**, prima con `--dry-run`: le azioni nuove sono, per un clone che manca, il
   clone senza credenziali e la riga `se … non si legge senza credenziali (repository privato,
   #717)` con il comando dell'app del ruolo (`--perm contents=read -- … role-git.py clone`), e per
   un clone che c'è la stessa riga con `role-git.py fetch`. Senza `--dry-run`, per un repository
   pubblico nulla cambia (`ok: … è già un clone di …`); per uno privato lo script aggiorna il
   clone con l'app del ruolo, o dice fra le cose da fare (`da fare a mano`) cosa manca: la chiave
   del ruolo, o `scripts/role-git.py` nel clone dello script dei token. `setup-coordinator.sh` si
   esegue solo se il progetto ha il coordinatore. Un'altra azione che il passo non spiega vuol
   dire che l'host non corrisponde ai parametri: ci si ferma e si chiede all'operatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

4. **il pin a mano**: come utente `DEVLOOP_USER`, `devloop pin origin/main` (con più progetti
   `devloop --project <progetto> pin origin/main`), poi `systemctl start <SERVICE_NAME>`. Il pin a
   mano fa riprendere il pin automatico;

5. **la verifica**: nessun `ERRORE`, e nel passo "Aggiornamento dei clone dei ruoli (#717)" una
   riga `ok:` per ogni ruolo (per un repository pubblico, `… si legge senza credenziali`). Senza
   `--no-apps`, perché la verifica interroga GitHub:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri>
   ```

**Un progetto con `SOURCE_REF`** (#687) conia i token dei ruoli dal clone della sorgente al suo
tag: finché il tag non contiene #717, `scripts/role-git.py` non c'è e il clone di un repository
privato resta fra le cose da fare. Questa sezione la applica al tag che la contiene, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666).

**Rollback**: il pin della versione di prima rimette il codice di prima; i clone dei ruoli restano
come sono, senza token nella loro configurazione. `contents: read` sulle app reali si toglie dalle
loro impostazioni.

## Permessi delle app dei ruoli contro i manifest (#726)

Il passo sull'host di #726. `scripts/supervisor-token.py` e `scripts/coordinator-token.py` hanno
`--check`: come l'utente del ruolo, con la sua chiave, leggono i permessi dell'installazione
dell'app del ruolo con il suo JWT e li confrontano con il manifest (`ops/github/apps/supervisor.json`,
`coordinator.json`), senza coniare token né eseguire comandi. `check-host.sh` lo esegue, nel passo
"Permessi delle app dei ruoli contro i manifest (#726)", per il supervisore e, se il progetto lo
ha, per il coordinatore, quando l'overlay ne dichiara l'app; lo salta con `--no-apps` e con
`--read-only` ([ops/README.md](../README.md#8-verifica)).

Nessun permesso nuovo né diverso per le app, nessun parametro, nessuna chiave dell'overlay, nessun
sudoers, nessuna ACL e nessuna unità nuovi. Sull'host cambia solo `host/check-host.sh`, che il
coordinatore esegue dalla sua copia di root (`COORDINATOR_OPS_DIR`); la copia del supervisore
(`SUPERVISOR_OPS_DIR`) non lo contiene e non cambia. Gli script dei token vengono dal clone da cui
ogni ruolo li esegue (il suo clone del repository gestito, o con `SOURCE_REPO` quello della
sorgente), non da una copia di root. Il codice del loop non cambia: devloop mergia la PR di #726
come le altre, ma non fa il pin automatico di quel commit né dei successivi finché l'operatore non
fa il pin a mano ([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)): qui è
il passo 3.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #726), **per ogni progetto
dell'host** con il supervisore, con il suo file di parametri `<parametri>` (uno con `SOURCE_REF`
al tag successivo, con [la sua procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **il loop fermo**: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli restano;

2. **la copia di root del coordinatore**, solo se il progetto ha il coordinatore, prima con
   `--dry-run`: l'unica azione nuova è la copia di `host/check-host.sh` in `COORDINATOR_OPS_DIR`.
   Un'altra azione che il passo non spiega vuol dire che l'host non corrisponde ai parametri: ci si
   ferma e si chiede all'operatore. `setup-supervisor.sh` non serve: la sua copia non cambia.

   ```bash
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

3. **il pin a mano**: come utente `DEVLOOP_USER`, `devloop pin origin/main` (con più progetti
   `devloop --project <progetto> pin origin/main`), poi `systemctl start <SERVICE_NAME>`. Il pin a
   mano fa riprendere il pin automatico;

4. **la verifica**, senza `--no-apps`, perché il passo interroga GitHub: nessun `ERRORE`, e nel
   passo "Permessi delle app dei ruoli contro i manifest (#726)" una riga `ok:` per ogni ruolo che
   ha l'app nell'overlay. Un `ERRORE` lì è un permesso del manifest che manca all'installazione o
   che il proprietario non ha ancora accettato: si corregge nelle impostazioni dell'app e
   nell'installazione ([ops/README.md](../README.md#2-github-app), "I permessi di un'app che
   esiste già"), non sull'host. Il dettaglio è nell'output del `--check`, che `check-host.sh`
   mostra:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri>
   ```

Un `ATTENZIONE: … manca o non ha --check (#726)` vuol dire che il clone da cui il ruolo prende i
suoi token ha ancora gli script di prima: il ruolo lo aggiorna come sempre, e la verifica si
ripete dopo. **Un progetto con `SOURCE_REF`** (#687) prende gli script dal clone della sorgente
al suo tag: finché il tag non contiene #726, quell'avviso resta, senza altro effetto. Questa
sezione la applica al tag che la contiene, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666).

**Rollback**: il passo 2 dal checkout della versione di prima rimette la copia di prima di
`check-host.sh`, e il pin della versione di prima il codice di prima.

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

Una chiave nuova dell'overlay si scrive **dopo** il pin del codice che la conosce, e il servizio
si avvia dopo averla scritta: il codice fissato rifiuta le chiavi dell'overlay che non conosce
(`devloop: invalid config: local config: unknown key …`), e con la chiave già scritta si
fermerebbero sia il pin a mano sia il servizio, che systemd riavvierebbe in ciclo. Fa eccezione
solo un pin fatto con il codice nuovo, dal clone aggiornato, come al passo 6 o in [Chiavi per
ruolo su un host attivo](#chiavi-per-ruolo-su-un-host-attivo): lì è il codice nuovo a leggere
l'overlay. Nel rollback vale il contrario: la chiave si toglie **prima** del pin della copia di
prima. Per un progetto con `SOURCE_REF` la chiave si scrive al passo 6 della [sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666), dopo il pin del tag nuovo.

**La manutenzione dell'host** (#705, #706; scelta M4 dell'operatore, [ADR 0002, precisazione del
2026-11-27](../../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-27-la-manutenzione-dellhost-706)).
Ogni sezione di questa guida che ferma il servizio o tocca ciò che usano i ruoli (le sveglie, i
sudoers, le ACL, le copie di root di `ops/`, l'overlay, il pin) si esegue dentro la manutenzione
dell'host ([ops/README.md](../README.md#10-coordinatore-facoltativo), "La manutenzione
dell'host"), per ogni progetto che tocca. La regola vale per ogni sezione, anche per quelle che
non la ripetono:

- **passo zero**, come root, prima del primo comando della sezione: `devloop --project <progetto>
  maintenance start "<sezioni>"`, con i titoli delle sezioni che si eseguono. Ferma il servizio
  (il `systemctl stop <SERVICE_NAME>` della sezione trova il servizio già fermo) e i ruoli
  aspettano in manutenzione. Se `start` si rifiuta (una manutenzione o una pausa già aperte) o non
  riesce (un pin in corso), il passo non comincia: ci si ferma e si chiede all'operatore;
- **durante la manutenzione** il pin a mano del passo si fa con il comando `devloop` dell'host,
  come root (`devloop --project <progetto> pin origin/main`), l'unico che devloop accetta finché
  la manutenzione c'è; un pin fatto in un altro modo si rifiuta. Il `systemctl start
  <SERVICE_NAME>` che la sezione dà dopo il pin non si esegue: lo fa la fine della manutenzione;
- **ultimo passo**, come root, dopo la verifica: `devloop --project <progetto> maintenance end`,
  che toglie la manutenzione e riavvia il servizio (salvo una pausa dell'operatore). Si chiude
  anche dopo la scadenza. Una sezione che si ferma a metà lascia la manutenzione aperta: lo si
  dice all'operatore, che decide (il rollback della sezione, poi la fine, o altro); un servizio
  riavviato su un passo a metà farebbe più danni di un progetto fermo.

La aprono e la chiudono l'agent dell'host, nella stessa richiesta dell'operatore con cui esegue
la sezione e senza un'altra richiesta ([runbook](../../docs/devloop/roles/host.md#the-maintenance-of-the-host)),
e l'operatore, quando esegue un passo lui stesso; nessun altro ruolo. Non si usa per altro, e
non tocca la pausa dell'operatore: una pausa in corso ferma la sezione, come prima. La
manutenzione non aggiunge messaggi nelle caselle: i ruoli la leggono dal suo stato (scelta M1).
Fanno eccezione la sezione [La manutenzione dell'host (#705)](#la-manutenzione-dellhost-705),
che installa il comando e si esegue senza, e un progetto con `SOURCE_REF` il cui tag non contiene
ancora #705, per cui i passi si fanno come prima.

Dal #462 il passo è anche dichiarato nell'output strutturato dell'implementer (`host_step`: un
testo breve con il rimando a questa guida), e il reviewer controlla che ci sia. Dal #531 il
rimando nomina una sezione di questa guida o uno script di `ops/host/`, che la PR aggiunge o
aggiorna se manca, mai comandi liberi, e il reviewer verifica che esista e descriva il passo:
l'agent dell'host, che l'operatore lancia con `start-role.sh host` di questa directory
([I quattro ruoli](../README.md#i-quattro-ruoli), [runbook](../../docs/devloop/roles/host.md)), esegue solo quella sezione o quello script, dalla
versione su `main` dopo il merge. devloop mergia la
PR come le altre, ma con `[pin] auto = true` **non fa il pin automatico** di quel commit né dei
successivi: scrive un allarme con il passo su `[run].alert_issue` e mette `agent:needs-operator`
sull'issue chiusa. Dopo il passo, a loop fermo, il pin a mano (`devloop pin origin/main`, il
[comando dell'host](#comando-devloop-dellhost-588), o `devloop --project <progetto> pin
origin/main` con più progetti; oppure il pin con il codice nuovo come nelle sezioni qui sotto) fissa la copia e
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
`[run].alert_issue` (`Agent senza utente dedicato`, o `Agents without a dedicated user` se la
`language` del progetto è l'inglese), ma non si ferma. `check-host.sh --no-apps`
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
   `state_dir`, e l'ACL di sola lettura `u:coordinator:r--` sull'overlay
   (`/home/devloop/ssdlc/.ssdlc/devloop/config.local.toml`), se manca (#554); la `key_dir` della sua app in `/home/coordinator/.ghapps` (con `PROJECT`,
   `~/.ghapps-<progetto>`), 700. Poi la chiave dell'app del coordinatore va lì, 600, proprietario
   `coordinator`, e il coordinatore conia i token con `DEVLOOP_APP_KEY_DIR` impostata a quella
   `key_dir` ([ops/README.md](../README.md#10-coordinatore-facoltativo), "App del coordinatore",
   #549; `setup-coordinator.sh` stampa il comando con i valori dell'installazione);
3. verificare, come root: `sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps`, nessun
   errore nel passo `Coordinatore coordinator`: la `key_dir` (700) con la sola chiave dell'app
   del coordinatore (600), l'ACL `user:coordinator:--x` sulla home di devloop e nessuna sulle sue
   directory private, l'ACL `user:coordinator:r--` sull'overlay e l'overlay leggibile dal
   coordinatore, il sudoers (440, root, uguale a
   quello generato), la copia e le directory sopra di root, ogni file della copia che `coincide`
   con il checkout;
4. verificare **come `coordinator`**, con il suo sudoers: `sudo -n -l` elenca solo i comandi di
   [ops/README.md](../README.md#10-coordinatore-facoltativo), e la verifica in sola lettura non
   avvia nessuna CLI, non esegue `devloop apps` e non scrive nulla:

   ```bash
   sudo -u coordinator -H sudo -n -l
   sudo -u coordinator -H sudo -n /usr/bin/bash /usr/local/lib/devloop-coordinator/host/check-host.sh --read-only
   ```

5. a mano: la CLI dell'agent del coordinatore e il suo login, come `coordinator`. Nessun token
   personale per `gh`: il coordinatore legge con la sua app ([ops/github/tokens.md](../github/tokens.md)).

**Dopo ogni aggiornamento di `ops/`** si rifà il passo 2 dal checkout aggiornato: la copia di root
non segue il checkout, e `check-host.sh` lo segnala (`ATTENZIONE: … è diverso da …: rieseguire
setup-coordinator.sh`).

**Dopo ogni sostituzione dell'overlay** (#554) si rifà il passo 2, poi il passo 3. Un
`install -m 600`, un `cp` o un `rsync` che scrive un file nuovo al posto di
`/home/devloop/ssdlc/.ssdlc/devloop/config.local.toml`, come la copia dalla macchina vecchia di
[Da copiare](#da-copiare), ne toglie le ACL: quella del coordinatore, che lo script rimette solo
se manca, e quella del supervisore, che rimette `setup-supervisor.sh` ([ACL dell'overlay e del
file di progetto](#acl-delloverlay-e-del-file-di-progetto-543), #543).
Senza, `scripts/coordinator-token.py` si ferma con `Permission denied` e `check-host.sh` dà
`ERRORE: manca l'ACL u:coordinator:r-- su …`. Le copie di sicurezza dell'overlay delle altre
sezioni (`install … config.local.toml.prima-…`) sono file nuovi e non toccano l'overlay.

Su un host dove il coordinatore c'è già, il passo 2 dal checkout aggiornato aggiunge l'ACL
sull'overlay se manca, e il passo 3 la verifica. Fino al #554 `check-host.sh` provava le letture
come un altro utente con `/usr/bin/test -r`, che su Ubuntu 26.04 (uutils) ignora le ACL: un
`ERRORE: … legge …: non riuscito` di prima, su un file leggibile con un'ACL, era falso.

Dal #530 anche `check-host.sh` completo, che l'operatore o l'agent dell'host esegue come root,
riporta delle prove delle CLI solo l'esito e il codice di uscita, mai il testo del modello: per
vederlo si ripete la prova a mano, come al passo 5 di [File delle CLI di root sull'utente degli
agent](#file-delle-cli-di-root-sullutente-degli-agent-438).

**Rollback.** Come root, prima il sudoers, poi la copia, le ACL sull'overlay e sulla home di
devloop e l'utente:

```bash
rm /etc/sudoers.d/devloop-coordinator
rm -r /usr/local/lib/devloop-coordinator
setfacl -x u:coordinator /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml
setfacl -x u:coordinator /home/devloop
userdel -r coordinator
```

## Rinomina del repository in devloop (ADR 0007, R3, #540)

Con R2 dell'[ADR 0007](../../docs/adr/0007-da-ssdlc-a-devloop.md) l'operatore rinomina il
repository su GitHub da `brushtyler/ssdlc` a `brushtyler/devloop`, e questo passo porta l'host al
nome nuovo. Si esegue con l'agent dell'host, e la rinomina cade **nel mezzo** del passo. GitHub
reindirizza i vecchi URL di git e dell'API, ma non la ricerca dell'installazione di un'app:
dopo la rinomina `GET /repos/brushtyler/ssdlc/installation`, con il JWT dell'app, risponde
**404** (#559). Fra la rinomina e il cambio di `repo` nell'overlay **devloop non funziona**:
nessuna app conia token, e `devloop pin` fallisce al `tick --dry-run` del suo smoke test (la
copia resta `rejected-<sha12>`). Per questo il loop si ferma e il pin si fa **prima** della
rinomina, e l'overlay cambia subito dopo. **I percorsi sull'host non cambiano** (ADR 0007): il
clone di devloop resta `/home/devloop/ssdlc`, lo `state_dir` `.ssdlc/devloop`, il clone del
supervisore `/home/supervisor/ssdlc`, il checkout dell'operatore `/root/repos/ssdlc`. Cambiano:

- nell'overlay, `repo = "brushtyler/devloop"` e la chiave nuova `repo_aliases =
  ["brushtyler/ssdlc"]`
  ([docs/devloop/reference.md](../../docs/devloop/reference.md#rename-of-the-repository)): con
  l'alias i record del journal, i marker `story` e i link alle decisioni dell'operatore negli
  ADR scritti con il vecchio nome restano validi. Un link con il vecchio nome vale solo se il
  dispatcher trova quel commento per id nel repository configurato, con l'API, e il suo autore è
  in `trusted_users`;
- il remote `origin` dei clone del repository;
- `ops/params.env` del checkout dell'operatore: `REPO=brushtyler/devloop`, e `DEVLOOP_CLONE` e
  `SUPERVISOR_CLONE` scritti per esteso, perché il loro default viene dal nome del repository
  (`/home/devloop/devloop`). `SOURCE_REPO` resta vuoto: ssdlc è la sorgente di sé stesso, e
  `ops/lib.sh` rifiuta un `SOURCE_REPO` uguale a `REPO`. Le installazioni degli altri progetti
  (#515) avranno `SOURCE_REPO=brushtyler/devloop`;
- la descrizione dell'unità systemd (`devloop continuous loop (brushtyler/devloop)`), che
  `setup-devloop.sh` riscrive, e, con `repo_lock_dir`, il file del lock per repository, che
  diventa `brushtyler--devloop.lock`: devloop lo crea al primo avvio, e quello del vecchio nome
  resta senza che nessuno lo tenga.

Il nome del repository non entra nella versione della policy: le approvazioni delle PR aperte
restano valide, e la coda non deve essere vuota.

### Prima

- **La PR di #540 mergiata** su `main`: la copia che conosce `repo_aliases`. La copia di prima
  rifiuta la chiave nell'overlay (chiave sconosciuta) e non parte: per questo il pin del passo 2
  viene **prima** della modifica dell'overlay. La PR dichiara il passo (`host_step`): con `[pin]
  auto = true` devloop non fa il pin automatico finché non lo fa l'operatore
  ([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).
- **Il repository ancora `brushtyler/ssdlc`**: R2 non è fatto, e
  `gh api repos/brushtyler/ssdlc --jq .full_name` stampa `brushtyler/ssdlc` (dopo la rinomina,
  per il reindirizzamento, stamperebbe il nome nuovo). Se il repository è già stato rinominato,
  il pin del passo 2 fallisce: l'operatore riporta il nome a `ssdlc` e si comincia dal passo 1.
- **L'operatore presente** per la rinomina del passo 3: da lì al passo 5 nessuna app conia
  token, e la finestra deve restare breve.

### Comandi

Come root.

1. **Fermare il loop** e il coordinatore, se c'è: durante la finestra niente deve coniare token
   di app.

   ```bash
   sudo systemctl stop devloop
   systemctl is-active devloop        # "inactive"
   ```

   Se c'è il coordinatore (`id coordinator`), l'operatore ne chiude la sessione
   (`sudo -u coordinator -H tmux kill-session -t =devloop-coordinator`). Lo stesso vale per la
   sessione del supervisore, che conia token con la sua app. Ripartono al passo 9.

2. **Clone di devloop e pin** della copia di #540, con l'overlay ancora di prima, che anche la
   copia nuova accetta. Il pin si fa **prima della rinomina**, finché la ricerca
   dell'installazione delle app con il vecchio nome funziona, e con essa il `tick --dry-run`
   dello smoke test:

   ```bash
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach origin/main
   sudo -u devloop -H git -C /home/devloop/ssdlc status --short
   sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin origin/main
   ```

   `status --short` non deve stampare nulla. Il pin finisce con `pinned …` e il link `current`
   sulla copia nuova. Se il pin fallisce non si rinomina: il repository ha ancora il vecchio
   nome, e il loop riparte sulla copia di prima, con l'avvio del passo 9.

3. **R2, la rinomina** (passo dell'operatore): nelle impostazioni del repository su GitHub,
   `ssdlc` diventa `devloop`. Poi la verifica:

   ```bash
   gh api repos/brushtyler/devloop --jq .full_name     # brushtyler/devloop
   ```

   Da qui al passo 5 le app non coniano token: i passi seguenti si fanno **subito**.

4. **I remote dei clone**, ciascuno come il suo utente, perché resti suo. Il clone di devloop e
   quello del supervisore sono in HTTPS (`setup-devloop.sh`, `setup-supervisor.sh`):

   ```bash
   sudo -u devloop -H git -C /home/devloop/ssdlc remote set-url origin https://github.com/brushtyler/devloop.git
   sudo -u supervisor -H git -C /home/supervisor/ssdlc remote set-url origin https://github.com/brushtyler/devloop.git
   git -C /root/repos/ssdlc remote get-url origin
   ```

   Il checkout dell'operatore mantiene la forma che ha: con `https://…` lo stesso comando, con
   `git@github.com:brushtyler/ssdlc.git`
   `git -C /root/repos/ssdlc remote set-url origin git@github.com:brushtyler/devloop.git`. I
   worktree di devloop condividono il remote del clone. Un altro clone dell'host che nomina
   ancora il vecchio nome compare qui, e si aggiorna allo stesso modo, come il suo utente:

   ```bash
   grep -lE 'github\.com[:/]brushtyler/ssdlc(\.git)?$' /home/*/*/.git/config /root/repos/*/.git/config
   ```

   Il comando non stampa più nulla quando ogni clone è aggiornato. Poi la lettura dal nome
   nuovo, che deve riuscire:

   ```bash
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u supervisor -H git -C /home/supervisor/ssdlc fetch origin
   git -C /root/repos/ssdlc fetch origin
   ```

5. **L'overlay**: una copia di prima, poi `repo` con il nome nuovo e `repo_aliases` subito
   sotto. Il file si riscrive al suo posto (`cat … >`), così proprietario, modo e ACL (quella
   del coordinatore, #554) restano:

   ```bash
   cd /home/devloop/ssdlc/.ssdlc/devloop
   grep -cx 'repo = "brushtyler/ssdlc"' config.local.toml      # 1
   grep -c '^repo_aliases' config.local.toml                   # 0
   install -o devloop -m 600 config.local.toml config.local.toml.prima-rinomina
   sed 's|^repo = "brushtyler/ssdlc"$|repo = "brushtyler/devloop"\nrepo_aliases = ["brushtyler/ssdlc"]|' \
       config.local.toml > config.local.toml.nuovo
   cat config.local.toml.nuovo > config.local.toml && rm config.local.toml.nuovo
   grep -n '^repo' config.local.toml
   ```

   L'ultimo comando stampa `repo = "brushtyler/devloop"` e `repo_aliases =
   ["brushtyler/ssdlc"]` (con `repo_lock_dir`, anche la sua riga). Se il primo `grep` non dà
   `1`, l'overlay non ha `repo` in quella forma: non si va avanti, e si corregge a mano.

6. **`ops/params.env`** del checkout dell'operatore, portato su `origin/main`:

   ```bash
   git -C /root/repos/ssdlc checkout --detach origin/main
   cd /root/repos/ssdlc/ops
   cp -p params.env params.env.prima-rinomina
   sed -i 's|^REPO=brushtyler/ssdlc$|REPO=brushtyler/devloop|' params.env
   grep -q '^DEVLOOP_CLONE=' params.env || echo 'DEVLOOP_CLONE=/home/devloop/ssdlc' >> params.env
   grep -q '^SUPERVISOR_CLONE=' params.env || echo 'SUPERVISOR_CLONE=/home/supervisor/ssdlc' >> params.env
   grep -nE '^(REPO|SOURCE_REPO|DEVLOOP_CLONE|SUPERVISOR_CLONE)=' params.env
   ```

   L'ultimo comando stampa `REPO=brushtyler/devloop`, `DEVLOOP_CLONE=/home/devloop/ssdlc` e
   `SUPERVISOR_CLONE=/home/supervisor/ssdlc`, e nessun `SOURCE_REPO` non vuoto. Un
   `DEVLOOP_CLONE` o un `SUPERVISOR_CLONE` che c'era già resta com'è, e deve essere uno di questi
   percorsi.

7. **Unità e copie di root**, con i parametri nuovi:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh
   ```

   Il dry run mostra l'unità con `Description=devloop continuous loop (brushtyler/devloop)`, e
   per l'overlay `l'overlay ha repo = "brushtyler/devloop"`; lo script riscrive l'unità e fa
   `systemctl daemon-reload`, senza avviare il servizio. Se c'è il coordinatore (`id
   coordinator`), la sua copia di root ha i parametri di prima: si rifà il passo 2 di [Utente del
   coordinatore](#utente-del-coordinatore-530),
   `sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh`.

8. **Verifica**, a loop fermo:

   ```bash
   sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned apps
   sudo bash /root/repos/ssdlc/ops/host/check-host.sh
   sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned tick --dry-run
   ```

   `apps` nomina per prima cosa `apps of brushtyler/devloop, keys in …`, trova ogni app
   installata sul repository e ne conia i token con il nome nuovo. `check-host.sh` non dà
   nessun `ERRORE` (l'unità `coincide`). `tick --dry-run` esce con 0 e pianifica le azioni
   attese, senza `not confirmed by the journal`: i record e i marker scritti con il vecchio nome
   sono del repository. Con un errore, il rollback.

9. **Avvio**:

   ```bash
   sudo systemctl start devloop
   systemctl is-active devloop        # "active"
   ```

   All'avvio il journal di devloop registra il cambio delle chiavi di fiducia `repo` e
   `repo_aliases`, con il valore di prima e quello nuovo. Le sessioni chiuse al passo 1
   ripartono con `ops/host/start-role.sh` (passo dell'operatore). Le copie
   `config.local.toml.prima-rinomina` e `params.env.prima-rinomina` si cancellano quando il loop
   gira con il nome nuovo.

**Rollback.** Se un passo dopo la rinomina fallisce, a loop fermo l'operatore riporta il nome del
repository a `ssdlc` su GitHub, l'overlay torna alla copia `config.local.toml.prima-rinomina` e il
loop riparte sul pin del passo 2, che accetta l'overlay di prima: un altro pin non serve.
L'overlay si riscrive al suo posto, come al passo 5:

```bash
gh api repos/brushtyler/ssdlc --jq .full_name       # brushtyler/ssdlc
cd /home/devloop/ssdlc/.ssdlc/devloop
cat config.local.toml.prima-rinomina > config.local.toml
grep -n '^repo' config.local.toml
cp -p /root/repos/ssdlc/ops/params.env.prima-rinomina /root/repos/ssdlc/ops/params.env
sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh
sudo -u devloop -H git -C /home/devloop/ssdlc remote set-url origin https://github.com/brushtyler/ssdlc.git
sudo -u supervisor -H git -C /home/supervisor/ssdlc remote set-url origin https://github.com/brushtyler/ssdlc.git
sudo systemctl start devloop
```

`gh api` stampa `brushtyler/ssdlc` solo dopo la rinomina inversa; `grep` stampa `repo =
"brushtyler/ssdlc"` e nessun `repo_aliases`. Un passo non ancora fatto si salta: senza la copia
dell'overlay (passo 5) l'overlay è ancora quello di prima, senza la copia di `params.env` (passo
6) anche `params.env`. Con il coordinatore si rifà anche `setup-coordinator.sh`. Il checkout
dell'operatore torna al remote di prima con
`git -C /root/repos/ssdlc remote set-url origin <url di prima>`.

Se invece il loop ha già girato con il nome nuovo (dopo il passo 9), l'overlay torna al vecchio
nome **con il nome nuovo come alias**, non alla copia di prima: i record scritti nel frattempo
con `brushtyler/devloop` restano validi. La copia di #540 accetta entrambe le forme. Al posto del
`cat` della copia:

```bash
cd /home/devloop/ssdlc/.ssdlc/devloop
sed -e 's|^repo = "brushtyler/devloop"$|repo = "brushtyler/ssdlc"|' \
    -e 's|^repo_aliases = \["brushtyler/ssdlc"\]$|repo_aliases = ["brushtyler/devloop"]|' \
    config.local.toml > config.local.toml.nuovo
cat config.local.toml.nuovo > config.local.toml && rm config.local.toml.nuovo
grep -n '^repo' config.local.toml
```

`grep` stampa `repo = "brushtyler/ssdlc"` e `repo_aliases = ["brushtyler/devloop"]`. I remote con
il vecchio nome funzionano finché GitHub reindirizza, cioè finché nessun altro repository prende
il nome `brushtyler/ssdlc`. Tornare a una copia precedente a #540
(`scripts/devloop-pinned pin <sha12>`) richiede prima di togliere `repo_aliases` dall'overlay,
che quella copia rifiuta; senza l'alias, i record scritti con il nome nuovo non sono più del
repository.

## Percorsi devloop e istanza devloop@devloop (#563)

Con #563 (decisione dell'operatore, [precisazione dell'ADR
0007](../../docs/adr/0007-da-ssdlc-a-devloop.md#precisazione-del-2026-11-23-i-percorsi-sullhost-563))
lo `state_dir` di default di devloop diventa `.devloop`, fuori da `.ssdlc/`, la directory di stato
del vecchio motore SSDLC, e questo host passa all'istanza `devloop@devloop` dell'unità template
(ADR 0006, R8). Cambiano:

| Prima | Dopo |
|---|---|
| clone di devloop `/home/devloop/ssdlc` | `/home/devloop/devloop` (il default con `REPO=brushtyler/devloop`) |
| `state_dir` `/home/devloop/ssdlc/.ssdlc/devloop` | `/home/devloop/devloop/.devloop` |
| clone del supervisore `/home/supervisor/ssdlc` | `/home/supervisor/devloop` (`SUPERVISOR_CLONE` scritto) |
| clone del coordinatore `/home/coordinator/ssdlc` | `/home/coordinator/devloop` |
| `devloop.service` | `devloop@devloop`: `devloop@.service` e la drop-in `devloop@devloop.service.d/project.conf` |
| sudoers `devloop-supervisor`, `devloop-agent`, `devloop-coordinator` | gli stessi con il suffisso `-devloop` |
| casella `/var/lib/devloop-coordination` | `/var/lib/devloop-coordination-devloop` |
| copia di root `/usr/local/lib/devloop-coordinator` | `/usr/local/lib/devloop-coordinator-devloop` |

**Non cambiano:** gli utenti (`devloop`, `devloop-agent`, `supervisor`, `coordinator`), scritti in
`params.env` accanto a `PROJECT=devloop`, quindi nessun utente nuovo e nessun nuovo login delle
CLI; le `key_dir` (`~/.ghapps` di `devloop`, `supervisor` e `coordinator`), scritte in
`params.env`, quindi nessuna chiave si sposta e `apps.key_dir` dell'overlay resta com'è; il
checkout dell'operatore `/root/repos/ssdlc`, che si sposta con la migrazione del coordinatore fuori
da root; il gruppo `devloop-roles`, la directory dei lock e il file di progetto. **Il vecchio
motore non si tocca:** il tag `ssdlc-engine-final` resta com'è, e nessun passo legge o scrive le
directory `.ssdlc/` del vecchio motore negli altri repository. Qui si sposta solo
`.ssdlc/devloop` del clone di devloop, che era lo `state_dir`.

**L'ordine.** Il launcher è quello del clone (`scripts/devloop-pinned`), non della copia fissata:
dal #563 cerca `.devloop`, e si ferma se trova solo `.ssdlc/devloop`. La copia di #563, invece,
ha `.devloop` come `state_dir` di default, e con lo `state_dir` di prima il suo smoke test
rifiuterebbe il file di progetto (`project file: … is not …/.devloop/project.toml`). Per questo:

1. il pin della copia nuova si fa **prima** dello spostamento, con lo `state_dir` di prima, dal
   launcher del clone **non ancora aggiornato**, e con `state_dir = ".ssdlc/devloop"` scritto per
   il tempo del pin in cima all'overlay: la copia di prima e quella nuova lo accettano entrambe
   (la chiave esiste da sempre), e la copia nuova trova il file di progetto dove è;
2. poi lo spostamento, e la riga si toglie: l'overlay torna quello di prima;
3. solo dopo il clone passa a `origin/main`, con il launcher nuovo, che trova `.devloop`.

Il passo si fa con l'agent dell'host, a loop fermo, come root. La PR di #563 lo dichiara
(`host_step`): con `[pin] auto = true` devloop non fa il pin automatico finché non lo fa
l'operatore ([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

### Prima

- **La PR di #563 mergiata** su `main`, e la copia in uso precedente a #563.
- **I fatti dell'host** su cui si basa la procedura (verificati il 2026-11-23): lo `state_dir` è
  sullo stesso filesystem dei clone, quindi ogni spostamento è un `mv` (un rename) che tiene
  proprietari, modi e ACL; il link `current` è relativo (`pinned-<sha12>`); il clone di devloop non
  ha worktree attivi. Si riverificano al passo 2.
- **Nessun percorso nuovo esiste già** e nessun'altra istanza di `devloop@` gira sull'host:

  ```bash
  ls -d /home/devloop/devloop /home/supervisor/devloop /home/coordinator/devloop /var/lib/devloop-coordination-devloop
  ls /etc/systemd/system/ | grep '^devloop'
  ```

  `ls -d` dice `No such file or directory` per tutti e quattro; il secondo stampa solo
  `devloop.service`. Altrimenti ci si ferma e si chiede all'operatore.
- **L'operatore presente** per il riavvio delle sessioni dei ruoli (passo 13).

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C /root/repos/ssdlc fetch origin` e
`git -C /root/repos/ssdlc checkout --detach origin/main`), che deve avere `ops/params.env`
(`test -f /root/repos/ssdlc/ops/params.env`). Gli script di `ops/host` girano sempre da lì, mai
dal clone di devloop.

1. **Fermare tutto**: il loop, la sessione del supervisore e quella del coordinatore, con la sua
   sveglia. Durante il fermo nessuno legge la casella né lo `state_dir`.

   ```bash
   systemctl is-enabled devloop > /root/devloop-563-enabled || true
   systemctl stop devloop
   systemctl is-active devloop        # "inactive"
   sudo -u supervisor -H tmux kill-session -t =devloop-supervisor
   sudo -u coordinator -H tmux kill-session -t =devloop-coordinator
   pgrep -u devloop -af 'm devloop'
   pgrep -u coordinator -af coordinator-wait.sh
   ```

   I due `pgrep` non stampano nulla; una sveglia rimasta si ferma con
   `pkill -u coordinator -f coordinator-wait.sh`. Una sessione che non c'è dà `can't find
   session`: va bene.

2. **Copie di sicurezza e fatti**, in una directory di root:

   ```bash
   install -d -m 700 /root/devloop-563-prima
   cd /root/devloop-563-prima
   mv /root/devloop-563-enabled enabled
   cp -p /etc/systemd/system/devloop.service .
   for f in devloop-supervisor devloop-agent devloop-coordinator; do test -e /etc/sudoers.d/$f && cp -p /etc/sudoers.d/$f sudoers-$f; done
   test -d /usr/local/lib/devloop-coordinator && cp -a /usr/local/lib/devloop-coordinator coordinator-ops
   cp -p /root/repos/ssdlc/ops/params.env params.env
   cp -p /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml config.local.toml
   cp -p /home/devloop-agent/.gemini/antigravity-cli/settings.json agy-settings.json
   readlink /home/devloop/ssdlc/.ssdlc/devloop/current > current
   sudo -u devloop -H git -C /home/devloop/ssdlc rev-parse HEAD > clone-head
   getfacl -p /home/devloop /home/devloop/ssdlc/.ssdlc/devloop /home/devloop/ssdlc/.ssdlc/devloop/config.local.toml /home/devloop/ssdlc/.ssdlc/devloop/project.toml > acl-prima
   cat current
   for c in /home/devloop/ssdlc /home/supervisor/ssdlc /home/coordinator/ssdlc; do echo "$c"; sudo -u "$(stat -c %U "$c")" -H git -C "$c" worktree list; done
   stat -c '%d %n' /home/devloop /home/devloop/ssdlc /home/devloop/ssdlc/.ssdlc/devloop /home/supervisor /home/supervisor/ssdlc /home/coordinator /home/coordinator/ssdlc /var/lib /var/lib/devloop-coordination
   ```

   `cat current` stampa `pinned-<sha12>`, relativo: dopo lo spostamento resta valido. Ogni
   `worktree list` deve stampare solo il clone: un worktree in più si ripara al passo 6. Lo
   `stat` dà lo stesso numero di dispositivo per ogni percorso e la sua directory padre (lo
   `state_dir` e il suo clone, ogni clone e la sua home, la casella e `/var/lib`): un `mv` nello
   stesso filesystem è un rename, che tiene proprietari, modi e ACL. Con un numero diverso ci si
   ferma.

3. **Il pin della copia nuova**, prima dello spostamento, con lo `state_dir` di prima. La riga
   `state_dir` va **in cima** all'overlay (una chiave di primo livello, prima di ogni tabella), e
   il file si riscrive al suo posto (`cat … >`), così proprietario, modo e ACL restano. Il clone
   si aggiorna con `fetch` soltanto: il suo launcher resta quello di prima, che cerca ancora
   `.ssdlc/devloop`.

   ```bash
   cd /home/devloop/ssdlc/.ssdlc/devloop
   grep -c '^state_dir' config.local.toml                       # 0
   printf 'state_dir = ".ssdlc/devloop"\n' | cat - config.local.toml > /root/devloop-563-prima/overlay-pin
   cat /root/devloop-563-prima/overlay-pin > config.local.toml
   sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin
   sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin origin/main
   readlink /home/devloop/ssdlc/.ssdlc/devloop/current
   grep -x 'state_dir = ".devloop"' /home/devloop/ssdlc/.ssdlc/devloop/current/devloop/config.toml
   ```

   Il pin finisce con `pinned …` e lo smoke test passato; `readlink` stampa il `pinned-<sha12>`
   nuovo e `grep` la riga del default nuovo: la copia nuova è di #563 o successiva. Se il primo
   `grep` non dà `0`, l'overlay ha già uno `state_dir`: ci si ferma e si chiede all'operatore. Se
   il pin fallisce non si sposta nulla: l'overlay torna quello di prima
   (`cat /root/devloop-563-prima/config.local.toml > config.local.toml`), e il loop riparte con
   `systemctl start devloop` sulla copia di prima, che `current` nomina ancora.

4. **Lo spostamento**, con `mv`. Lo `state_dir` diventa `.devloop` del clone nuovo; `.ssdlc/` del
   clone resta solo se contiene altro, e devloop non la legge.

   ```bash
   mv /home/devloop/ssdlc /home/devloop/devloop
   mv /home/devloop/devloop/.ssdlc/devloop /home/devloop/devloop/.devloop
   rmdir /home/devloop/devloop/.ssdlc || ls -A /home/devloop/devloop/.ssdlc
   mv /home/supervisor/ssdlc /home/supervisor/devloop
   mv /home/coordinator/ssdlc /home/coordinator/devloop
   mv /var/lib/devloop-coordination /var/lib/devloop-coordination-devloop
   ```

   La casella si sposta con i suoi messaggi, `root:devloop-roles` 2770. Le chiavi non si spostano.

5. **Le ACL**, verificate dopo lo spostamento, con `acl-prima` del passo 2:

   ```bash
   getfacl -p /home/devloop /home/devloop/devloop/.devloop /home/devloop/devloop/.devloop/config.local.toml /home/devloop/devloop/.devloop/project.toml
   ```

   Devono esserci, come prima: `user:supervisor:--x` e `user:coordinator:--x` su `/home/devloop`;
   `user:devloop-agent:--x` e `default:user:devloop-agent:---` sullo `state_dir`;
   `user:supervisor:r--`, `user:coordinator:r--` e `user:devloop-agent:---` sull'overlay;
   `user:supervisor:r--` e `user:devloop-agent:---` sul file di progetto. Una voce che manca si
   rimette con gli script del passo 11, che la aggiungono solo se manca.

6. **I worktree di git.** Senza worktree al passo 2 non c'è nulla da fare. Altrimenti, come
   l'utente del clone, `git worktree repair` dal clone nuovo con il percorso nuovo di ogni
   worktree, che ripara i collegamenti nei due sensi; per devloop:

   ```bash
   sudo -u devloop -H git -C /home/devloop/devloop worktree repair /home/devloop/devloop/.devloop/worktrees/*
   sudo -u devloop -H git -C /home/devloop/devloop worktree list
   ```

   `worktree list` mostra i percorsi nuovi, senza `prunable`. Allo stesso modo per un worktree
   del supervisore o del coordinatore, come il loro utente.

7. **Lo `state_dir`**: il link `current`, i percorsi assoluti dei file di stato e la riga del
   passo 3, che si toglie.

   ```bash
   cd /home/devloop/devloop/.devloop
   readlink current
   test -f current/devloop/__main__.py && echo ok
   grep -rlF /home/devloop/ssdlc . --exclude-dir=worktrees --exclude-dir='pinned-*' --exclude-dir='rejected-*' --exclude-dir=logs --exclude-dir=agent-shared --exclude='*.log' --exclude='turn-journal*' --exclude=config.local.toml
   grep -vx 'state_dir = ".ssdlc/devloop"' config.local.toml > /root/devloop-563-prima/overlay-dopo
   cat /root/devloop-563-prima/overlay-dopo > config.local.toml
   cmp config.local.toml /root/devloop-563-prima/config.local.toml && echo uguale
   grep -rlF /home/devloop/ssdlc config.local.toml project.toml
   ```

   `readlink` stampa lo stesso `pinned-<sha12>` del passo 3, relativo, e `test` stampa `ok`. Il
   primo `grep -r` non stampa nulla: i file di stato di devloop (`pin-base.json`, che tiene solo
   il commit, e gli altri `*.json`) non hanno percorsi assoluti. Un file che compare si riporta
   all'operatore e non si corregge a mano. Il journal (`turn-journal*`, una catena di hash) e i
   log tengono i percorsi di prima come storia e non si riscrivono. `cmp` stampa `uguale`: l'overlay
   è quello di prima. L'ultimo `grep` non stampa nulla: l'overlay e il file di progetto non
   nominano il clone (`key_dir` è `/home/devloop/.ghapps`, che non cambia).

8. **Il clone di devloop su `origin/main`**, con il launcher nuovo:

   ```bash
   sudo -u devloop -H git -C /home/devloop/devloop checkout --detach origin/main
   sudo -u devloop -H git -C /home/devloop/devloop status --short
   ```

   `status --short` non stampa nulla (`.devloop/` è ignorata dal `.gitignore` nuovo).

9. **Le CLI dell'utente degli agent.** `trustedWorkspaces` di agy nomina il clone di prima. Il
   file è sigillato (#438): lo riscrive root, al suo posto, così proprietario e modo restano;
   l'utente degli agent non lo scrive. Le cache di agy (`~/.gemini/antigravity-cli/cache/`) e le
   sessioni non si riscrivono.

   ```bash
   f=/home/devloop-agent/.gemini/antigravity-cli/settings.json
   grep -c '/home/devloop/ssdlc' "$f"
   sed -e 's|"/home/devloop/ssdlc/\.ssdlc/devloop\([/"]\)|"/home/devloop/devloop/.devloop\1|g' -e 's|"/home/devloop/ssdlc\([/"]\)|"/home/devloop/devloop\1|g' "$f" > /root/devloop-563-prima/agy-settings-dopo
   python3 -I -m json.tool /root/devloop-563-prima/agy-settings-dopo > /dev/null && echo json
   cat /root/devloop-563-prima/agy-settings-dopo > "$f"
   grep -c '/home/devloop/ssdlc' "$f"
   grep -n '/home/devloop/devloop' "$f"
   stat -c '%U %a' "$f"
   grep -lF /home/devloop/ssdlc /home/devloop-agent/.claude.json /home/devloop-agent/.claude/settings.json /home/devloop-agent/.codex/config.toml /home/devloop-agent/.gemini/settings.json /home/devloop/.codex/config.toml /home/devloop/.claude.json /home/devloop/.claude/settings.json
   ```

   Il primo `grep -c` dà almeno `1`, il secondo `0`; `json` conferma che il file è valido, e
   `grep -n` mostra la voce nuova di `trustedWorkspaces`; `stat` dà il proprietario e il modo di
   prima (`root`, sigillato). L'ultimo `grep` non stampa nomi di file (al più `No such file` per
   un file che non c'è): il 2026-11-23 nessuno di quei file nominava il clone. Un file che compare
   si riscrive allo stesso modo, da root, dopo una copia in `/root/devloop-563-prima`.

10. **`ops/params.env`** del checkout dell'operatore. Si tolgono i valori che con `PROJECT`
    devono prendere il default (clone di devloop, servizio, sudoers, copia di root, casella,
    `state_dir`) e si scrivono quelli che restano di prima:

    ```bash
    cd /root/repos/ssdlc/ops
    sed -i -E '/^(PROJECT|DEVLOOP_CLONE|SUPERVISOR_CLONE|SERVICE_NAME|SUDOERS_FILE|AGENT_SUDOERS_FILE|COORDINATOR_SUDOERS_FILE|COORDINATOR_OPS_DIR|COORDINATION_DIR|STATE_DIR)=/d' params.env
    cat >> params.env <<'EOF'
    # #563: l'istanza devloop@devloop, con gli utenti, le key_dir e i clone di questo host
    PROJECT=devloop
    DEVLOOP_USER=devloop
    AGENT_USER=devloop-agent
    SUPERVISOR_USER=supervisor
    COORDINATOR_USER=coordinator
    SUPERVISOR_CLONE=/home/supervisor/devloop
    DEVLOOP_KEY_DIR=/home/devloop/.ghapps
    SUPERVISOR_KEY_DIR=/home/supervisor/.ghapps
    COORDINATOR_KEY_DIR=/home/coordinator/.ghapps
    EOF
    grep -nE '^(REPO|SOURCE_REPO|PROJECT|DEVLOOP_CLONE|STATE_DIR)=' params.env
    ```

    L'ultimo comando stampa `REPO=brushtyler/devloop` e `PROJECT=devloop`, nessun `SOURCE_REPO`
    non vuoto, nessun `DEVLOOP_CLONE` né `STATE_DIR`.

11. **Unità, sudoers e copie di prima** si tolgono **prima** degli script: finché ci sono, ogni
    script si ferma (`DEVLOOP_USER 'devloop' è già l'utente devloop di un'altra installazione di
    devloop dell'host`), perché con `PROJECT` non sono più dell'installazione. Le loro copie sono
    in `/root/devloop-563-prima`. Poi gli script, ciascuno prima con `--dry-run`:

    ```bash
    systemctl disable devloop
    rm /etc/systemd/system/devloop.service
    rm -f /etc/sudoers.d/devloop-supervisor /etc/sudoers.d/devloop-agent /etc/sudoers.d/devloop-coordinator
    rm -rf /usr/local/lib/devloop-coordinator
    systemctl daemon-reload
    sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh --dry-run
    sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh
    sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh --dry-run
    sudo bash /root/repos/ssdlc/ops/host/setup-agent-user.sh
    sudo bash /root/repos/ssdlc/ops/host/setup-supervisor.sh --dry-run
    sudo bash /root/repos/ssdlc/ops/host/setup-supervisor.sh
    sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh --dry-run
    sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh
    ```

    Nessuno dei quattro stampa `useradd`: gli utenti esistono (`ok: l'utente devloop esiste`, e
    così gli altri). `setup-devloop.sh` scrive `/etc/systemd/system/devloop@.service` e la
    drop-in `/etc/systemd/system/devloop@devloop.service.d/project.conf`, con `User=devloop`,
    `WorkingDirectory=/home/devloop/devloop`, `ExecStart=/usr/bin/python3
    /home/devloop/devloop/scripts/devloop-pinned run` e il log in
    `/home/devloop/devloop/.devloop/run.log`, e trova clone, `state_dir`, overlay e `key_dir`
    (`ok: …`). `setup-agent-user.sh` scrive `/etc/sudoers.d/devloop-agent-devloop` e trova le ACL
    dello `state_dir`. `setup-supervisor.sh` scrive `/etc/sudoers.d/devloop-supervisor-devloop`,
    con `systemctl … devloop@devloop`, e trova il clone (`… è già un clone di brushtyler/devloop`),
    le ACL dell'overlay e la casella `/var/lib/devloop-coordination-devloop`.
    `setup-coordinator.sh` scrive `/etc/sudoers.d/devloop-coordinator-devloop` e la copia
    `/usr/local/lib/devloop-coordinator-devloop`, e trova la `key_dir` `/home/coordinator/.ghapps`.
    Una `key_dir` con il suffisso `-devloop` vuol dire che `params.env` non è quello del passo 10:
    ci si ferma.

12. **Verifiche**, a loop fermo:

    ```bash
    sudo bash /root/repos/ssdlc/ops/host/check-host.sh
    sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned apps
    sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned status
    sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned tick --dry-run
    sudo -u supervisor -H sh -c 'cd ~/devloop && DEVLOOP_APP_KEY_DIR=/home/supervisor/.ghapps python3 scripts/supervisor-token.py --state-dir /home/devloop/devloop/.devloop --perm issues=read -- gh api repos/brushtyler/devloop --jq .full_name'
    sudo -u coordinator -H sh -c 'cd ~/devloop && DEVLOOP_APP_KEY_DIR=/home/coordinator/.ghapps python3 scripts/coordinator-token.py --state-dir /home/devloop/devloop/.devloop --perm issues=read -- gh api repos/brushtyler/devloop --jq .full_name'
    sudo -u coordinator -H sudo -n -l
    ```

    `check-host.sh` non dà nessun `ERRORE` (l'unità e la drop-in `coincide`, i tre sudoers con il
    suffisso, la casella, il coordinatore e la sua copia); `devloop.service` non è più fra le unità.
    `apps` nomina `apps of brushtyler/devloop, keys in /home/devloop/.ghapps` e trova ogni app. I
    due script dei token stampano `brushtyler/devloop`. `tick --dry-run` esce con 0. `sudo -n -l`
    del coordinatore elenca i comandi con `devloop@devloop` e `/home/devloop/devloop/.devloop`.
    Con un errore, il rollback.

13. **Avvio**, e le sessioni dei ruoli:

    ```bash
    grep -qx enabled /root/devloop-563-prima/enabled && systemctl enable devloop@devloop
    systemctl start devloop@devloop
    systemctl is-active devloop@devloop      # "active"
    tail -n 20 /home/devloop/devloop/.devloop/run.log
    sudo -u supervisor -H sh -c 'cd ~/.claude/projects && test -d -home-supervisor-ssdlc && test ! -e -home-supervisor-devloop && mv -- -home-supervisor-ssdlc -home-supervisor-devloop'
    ```

    All'avvio il journal di devloop registra il cambio della chiave di fiducia `state_dir`
    (`.ssdlc/devloop` → `.devloop`). L'ultimo comando porta la memoria di Claude Code del
    supervisore al nome del clone nuovo ([Da copiare](#da-copiare)); se la directory non c'è, non
    fa nulla. Poi l'operatore riavvia le sessioni con `ops/host/start-role.sh`, che passa a
    ciascuna i valori nuovi (lo `state_dir`, la casella, la sveglia nella copia nuova):

    ```bash
    sudo bash /root/repos/ssdlc/ops/host/start-role.sh supervisor
    sudo bash /root/repos/ssdlc/ops/host/start-role.sh coordinator
    ```

    Alla prima sessione nella directory nuova una CLI può chiedere di fidarsi della directory:
    l'operatore conferma. `/root/devloop-563-prima` si cancella quando il loop gira con i percorsi
    nuovi e l'operatore non prevede il rollback.

**Rollback.** A loop fermo (`systemctl stop devloop@devloop`, sessioni chiuse come al passo 1),
come root, l'inverso nell'ordine inverso, dalle copie di `/root/devloop-563-prima`; un passo non
ancora fatto si salta. Non servono gli script di `ops/`: le copie sono i file di prima.

```bash
cd /root/devloop-563-prima
systemctl disable devloop@devloop
rm -r /etc/systemd/system/devloop@devloop.service.d
ls -d /etc/systemd/system/devloop@*.service.d 2>/dev/null || rm /etc/systemd/system/devloop@.service
rm -f /etc/sudoers.d/devloop-supervisor-devloop /etc/sudoers.d/devloop-agent-devloop /etc/sudoers.d/devloop-coordinator-devloop
rm -rf /usr/local/lib/devloop-coordinator-devloop
cp -p devloop.service /etc/systemd/system/devloop.service
for f in devloop-supervisor devloop-agent devloop-coordinator; do test -e sudoers-$f && cp -p sudoers-$f /etc/sudoers.d/$f; done
test -d coordinator-ops && cp -a coordinator-ops /usr/local/lib/devloop-coordinator
systemctl daemon-reload
grep -qx enabled enabled && systemctl enable devloop
cp -p params.env /root/repos/ssdlc/ops/params.env
cat agy-settings.json > /home/devloop-agent/.gemini/antigravity-cli/settings.json
mv /var/lib/devloop-coordination-devloop /var/lib/devloop-coordination
mv /home/coordinator/devloop /home/coordinator/ssdlc
mv /home/supervisor/devloop /home/supervisor/ssdlc
sudo -u devloop -H mkdir -p /home/devloop/devloop/.ssdlc
mv /home/devloop/devloop/.devloop /home/devloop/devloop/.ssdlc/devloop
mv /home/devloop/devloop /home/devloop/ssdlc
sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach "$(cat clone-head)"
```

Con dei worktree, `git worktree repair` come al passo 6, con i percorsi di prima. Poi la copia
fissata di prima, con la riga del passo 3 per il tempo del pin: la copia di #563, che `current`
nomina, accetta lo `state_dir` di prima solo con quella riga. Infine l'overlay di prima, al suo
posto, e l'avvio:

```bash
cd /home/devloop/ssdlc/.ssdlc/devloop
cat /root/devloop-563-prima/overlay-pin > config.local.toml
sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin "$(sed 's/^pinned-//' /root/devloop-563-prima/current)"
cat /root/devloop-563-prima/config.local.toml > config.local.toml
readlink current
systemctl start devloop
```

`readlink` stampa il `pinned-<sha12>` di `/root/devloop-563-prima/current`. La memoria del
supervisore torna al nome di prima con il `mv` inverso del passo 13, come `supervisor`. Dopo il
rollback `check-host.sh` si esegue dalla copia di root ripristinata
(`sudo bash /usr/local/lib/devloop-coordinator/host/check-host.sh --read-only`), che ha i
parametri di prima, non dal checkout dell'operatore su `origin/main`, i cui script hanno già lo
`state_dir` `.devloop`. Le sessioni ripartono con `start-role.sh` dal checkout dell'operatore
portato a un commit precedente a #563.

## Spostare una key_dir (#582)

La posizione delle chiavi delle app non è un segreto: le chiavi le proteggono i permessi (600,
dell'utente del ruolo, in una `key_dir` 700), gli utenti separati e gli agent sigillati. Se però
la posizione di una `key_dir` finisce in un testo pubblico (un'issue, un commento, l'output di
`check-host.sh` o un traceback incollati), l'operatore può spostarla con questa sezione, così la
posizione pubblicata non vale più. **Le chiavi non cambiano**: non è una rotazione, che resta la
procedura per una chiave compromessa ([ops/README.md](../README.md#2-github-app), punto 4).
Quando spostare, e quale ruolo, lo decide l'operatore.

Ogni ruolo ha la sua `key_dir`, un parametro di `params.env`, e si sposta da solo:

| Ruolo (`ruolo`) | Utente | Parametro | Anche | Fermo e riavvio |
|---|---|---|---|---|
| `devloop` | `devloop` | `DEVLOOP_KEY_DIR` | `key_dir` in `[apps]` dell'overlay | il servizio `devloop@devloop`: la sezione si esegue **a loop fermo** |
| `supervisor` | `supervisor` | `SUPERVISOR_KEY_DIR` | `DEVLOOP_APP_KEY_DIR` con cui conia i token | la sua sessione, poi `start-role.sh supervisor` |
| `coordinator` | `coordinator` | `COORDINATOR_KEY_DIR` | la copia di root di `params.env` (`setup-coordinator.sh`) | la sua sessione e la sveglia, poi `start-role.sh coordinator` |

Il passo si fa con l'agent dell'host, come root, dal checkout dell'operatore aggiornato
(`git -C /root/repos/ssdlc fetch origin` e `git -C /root/repos/ssdlc checkout --detach
origin/main`), un ruolo alla volta. Il codice di devloop non cambia: nessun pin lo aspetta.

**Il nome nuovo non si pubblica**: né in un'issue, né in un commento, né in una PR, né in un
output incollato. Lo sceglie il passo 1, a caso e non di default, e sta solo nei file dell'host
(`params.env`, l'overlay, le copie di sicurezza del passo 3). I comandi qui sotto non lo
stampano; `check-host.sh --verbose`, `setup-*.sh` e `devloop-pinned apps` sì: il loro output
resta sull'host. `check-host.sh` senza `--verbose` nomina i parametri, non i percorsi (#583). Nei resoconti all'operatore si scrive "la `key_dir` nuova", non il percorso.

### Comandi

Come root, in una sola shell, che resta aperta fino alla fine. I valori sono quelli di devloop;
per gli altri ruoli quelli dei commenti.

1. **I valori del ruolo e il nome nuovo.** La `key_dir` di prima è quella che gli script di
   `ops/` ricavano da `params.env`, scritta o di default:

   ```bash
   ruolo=devloop                  # supervisor, coordinator
   utente=devloop                 # supervisor, coordinator
   parametro=DEVLOOP_KEY_DIR      # SUPERVISOR_KEY_DIR, COORDINATOR_KEY_DIR
   ops=/root/repos/ssdlc/ops
   params=/root/repos/ssdlc/ops/params.env
   overlay=/home/devloop/devloop/.devloop/config.local.toml
   copie=/root/keydir-582-$ruolo
   valore() { bash -c '. "$1/lib.sh" && PARAMS_FILE=$2 && ops_load_params && printf %s "${!3}"' _ "$ops" "$params" "$1"; }
   vecchia=$(valore "$parametro")
   nuova=$(dirname "$vecchia")/.$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
   test -d "$vecchia" && test ! -e "$nuova" && test ! -e "$copie" && echo pronto
   ```

   Il nome nuovo sta accanto a quello di prima, di solito nella home dell'utente del ruolo: un
   punto e 16 cifre esadecimali casuali. `test` stampa `pronto`; altrimenti ci si ferma. Una
   `$copie` che esiste già è di uno spostamento di prima: si cancella solo se il suo rollback
   non serve più.

2. **Fermare il ruolo**: per devloop il loop, per supervisore e coordinatore la loro sessione.
   Si eseguono solo le righe del ruolo:

   ```bash
   systemctl stop devloop@devloop                                    # devloop
   systemctl is-active devloop@devloop                               # devloop: "inactive"
   sudo -u supervisor -H tmux kill-session -t =devloop-supervisor-devloop    # supervisor
   sudo -u coordinator -H tmux kill-session -t =devloop-coordinator-devloop  # coordinator
   sudo -u supervisor -H tmux kill-session -t =devloop-supervisor    # supervisor, prima di #629
   sudo -u coordinator -H tmux kill-session -t =devloop-coordinator  # coordinator, prima di #629
   pkill -u coordinator -f coordinator-wait.sh                       # coordinator: la sveglia
   ```

   Una sessione che non c'è dà `can't find session`: va bene.

3. **Copie di sicurezza**, per il rollback, in una directory di root:

   ```bash
   install -d -m 700 "$copie"
   cp -p "$params" "$copie/params.env"
   printf '%s\n' "$vecchia" > "$copie/vecchia"
   printf '%s\n' "$nuova" > "$copie/nuova"
   cp -p "$overlay" "$copie/config.local.toml"
   ```

   L'overlay si copia per ogni ruolo, ma cambia solo per devloop (passo 6).

4. **La directory nuova e lo spostamento delle chiavi.** La directory nuova è 700 dell'utente del
   ruolo; le chiavi si **spostano** con `mv`, mai copiate lasciando l'originale. Le due directory
   hanno la stessa directory padre, quindi lo stesso filesystem: `mv` è un rename, che tiene
   modo (600) e proprietario. Poi la directory di prima, vuota, si toglie:

   ```bash
   install -d -m 700 -o "$utente" -g "$(id -gn "$utente")" "$nuova"
   find "$vecchia" -mindepth 1 -maxdepth 1 -exec mv -n -t "$nuova" -- {} +
   rmdir "$vecchia"
   stat -c '%a %U' "$nuova" "$nuova"/*
   test ! -e "$vecchia" && echo spostata
   ```

   `stat` stampa `700` e l'utente del ruolo per la directory, `600` e l'utente per ogni chiave;
   `test` stampa `spostata`. Se `rmdir` fallisce, nella directory di prima è rimasto qualcosa: ci
   si ferma, senza copiare né cancellare nulla, e si chiede all'operatore.

5. **`params.env`**: il parametro del ruolo con la `key_dir` nuova, al posto di quella di prima.

   ```bash
   sed -i "/^$parametro=/d" "$params"
   printf '%s=%s\n' "$parametro" "$nuova" >> "$params"
   test "$(valore "$parametro")" = "$nuova" && echo params
   ```

   `test` stampa `params`: gli script di `ops/` leggono la `key_dir` nuova.

6. **L'overlay, solo per devloop**: `key_dir` in `[apps]`, che devloop legge per coniare i token.
   Il file si riscrive al suo posto (`cat … >`), così proprietario, modo e ACL restano. Senza la
   riga l'overlay usa il default `~/.ghapps`, e la riga si aggiunge sotto `[apps]`. Un overlay può
   non avere la riga `[apps]` e dichiarare solo `[apps.<ruolo>]`: allora `[apps]` con la riga si
   aggiunge in fondo, e le altre tabelle restano come sono:

   ```bash
   if grep -q '^key_dir *=' "$overlay"; then
       sed "s|^key_dir *=.*|key_dir = \"$nuova\"|" "$overlay" > "$copie/overlay-dopo"
   elif grep -Eq '^\[ *apps *\] *(#.*)?$' "$overlay"; then
       sed -E "/^\[ *apps *\] *(#.*)?$/a key_dir = \"$nuova\"" "$overlay" > "$copie/overlay-dopo"
   else
       { cat "$overlay"; printf '\n[apps]\nkey_dir = "%s"\n' "$nuova"; } > "$copie/overlay-dopo"
   fi
   cat "$copie/overlay-dopo" > "$overlay"
   python3 -I -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["apps"]["key_dir"] == sys.argv[2])' "$overlay" "$nuova"
   ```

   `python3` stampa `True`. Altrimenti l'overlay torna quello di prima
   (`cat "$copie/config.local.toml" > "$overlay"`) e si chiede all'operatore.

7. **Lo script di setup del ruolo**, prima con `--dry-run`. Con il coordinatore sull'host
   (`id coordinator`), per ogni ruolo anche `setup-coordinator.sh`: la sua copia di root ha
   `params.env`, che `check-host.sh --read-only` legge.

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-$ruolo.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-$ruolo.sh
   sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh     # con il coordinatore sull'host
   ```

   Il dry run trova la `key_dir` nuova (`ok: … esiste (700, <utente>)`) e ogni chiave (`ok:
   chiave di … presente`), e non crea la directory di prima.

8. **Verifica**, con il ruolo ancora fermo. Oltre a `check-host.sh`, si esegue solo la riga del
   ruolo:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps
   sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned apps     # devloop
   sudo -u supervisor -H sh -c 'cd ~/devloop && DEVLOOP_APP_KEY_DIR="$1" python3 scripts/supervisor-token.py --state-dir /home/devloop/devloop/.devloop --perm issues=read -- gh api repos/brushtyler/devloop --jq .full_name' _ "$nuova"     # supervisor
   sudo -u coordinator -H sh -c 'cd ~/devloop && DEVLOOP_APP_KEY_DIR="$1" python3 scripts/coordinator-token.py --state-dir /home/devloop/devloop/.devloop --perm issues=read -- gh api repos/brushtyler/devloop --jq .full_name' _ "$nuova"   # coordinator
   ```

   `check-host.sh` non dà nessun `ERRORE`: la `key_dir` nuova 700, con le chiavi 600 dell'utente
   del ruolo, e nessuna chiave del progetto rimasta nella `key_dir` di default (`ERRORE: …,
   key_dir di default, ha ancora N chiavi di app del progetto, ma <PARAMETRO> indica un'altra
   key_dir`, #582). Per devloop `apps` trova ogni app con la sua chiave; per supervisore e
   coordinatore lo script dei token stampa `brushtyler/devloop`. Con un errore, il rollback.

9. **Riavvio.** Per devloop:

   ```bash
   systemctl start devloop@devloop
   systemctl is-active devloop@devloop      # "active"
   ```

   All'avvio il journal di devloop, sull'host, registra il cambio della chiave di fiducia `apps`
   (`key_dir`), con il valore di prima e quello nuovo. Per supervisore e coordinatore
   l'operatore riavvia la sessione:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/start-role.sh supervisor
   sudo bash /root/repos/ssdlc/ops/host/start-role.sh coordinator
   ```

   Il primo messaggio del coordinatore porta la `key_dir` nuova (#565). Il supervisore conia i
   token con `DEVLOOP_APP_KEY_DIR` sulla sua `key_dir`: il comando con il valore nuovo è quello
   che `setup-supervisor.sh` stampa alla fine (passo 7), e l'operatore lo dà alla sessione nuova.
   `$copie` si cancella (`rm -r "$copie"`) quando il ruolo gira con la `key_dir` nuova e il
   rollback non serve più.

**Rollback.** Con il ruolo fermo come al passo 2, nella shell del passo 1 (o con i suoi valori,
tranne `vecchia` e `nuova`, che si leggono da `$copie`): le chiavi tornano al loro posto con
`mv`, `params.env` alla copia del passo 3 e, solo per devloop, anche l'overlay. Un passo non
ancora fatto si salta.

```bash
vecchia=$(cat "$copie/vecchia")
nuova=$(cat "$copie/nuova")
install -d -m 700 -o "$utente" -g "$(id -gn "$utente")" "$vecchia"
find "$nuova" -mindepth 1 -maxdepth 1 -exec mv -n -t "$vecchia" -- {} +
rmdir "$nuova"
cp -p "$copie/params.env" "$params"
cat "$copie/config.local.toml" > "$overlay"        # devloop
test "$(valore "$parametro")" = "$vecchia" && echo params
```

`test` stampa `params`. Poi i passi 7, 8 e 9: script di setup, verifica e riavvio. La posizione
di prima è quella pubblicata: il rollback la rimette in uso finché non si riprova lo
spostamento.

## Mappa delle sostituzioni dei testi pubblicati (#583)

Dal #583 devloop mette i nomi dei parametri (`$STATE_DIR`, `$DEVLOOP_KEY_DIR`, `$AGENT_HOME`,
`<AGENT_USER>`, …) al posto dei percorsi e degli utenti dell'host in tutto ciò che pubblica
([reference](../../docs/devloop/reference.md#host-details-in-the-published-texts)). I valori
li prende all'avvio dalla mappa `redactions.json` nello `state_dir`, che `setup-devloop.sh`
scrive dai parametri risolti di `params.env` (600, dell'utente devloop; nessuna chiave letta).
Senza la mappa devloop parte lo stesso, lo scrive nel log, e sostituisce solo i valori che
conosce da sé: il suo `state_dir`, la sua `key_dir`, la sua home e l'utente degli agent con la
sua home; le home e le `key_dir` di supervisore e coordinatore restano nei testi.

Anche `check-host.sh` stampa per default i nomi dei parametri, e i percorsi reali solo con
`--verbose`, per la diagnosi locale: l'output da citare in un'issue è quello senza `--verbose`.
Il coordinatore lo esegue dalla sua copia di root, che `setup-coordinator.sh` aggiorna.

Il passo si fa con l'agent dell'host, come root, **a loop fermo**, dal checkout dell'operatore
aggiornato, prima del pin a mano del commit del #583. Si rifà, con gli stessi comandi, ogni volta
che cambia un parametro di `params.env` che nomina un percorso, un utente o il servizio
(`SERVICE_NAME`): la mappa si
riscrive solo se è diversa.

1. **Il checkout dell'operatore e il loop fermo:**

   ```bash
   git -C /root/repos/ssdlc fetch origin
   git -C /root/repos/ssdlc checkout --detach origin/main
   test -f /root/repos/ssdlc/ops/params.env && echo params
   systemctl stop devloop@devloop
   systemctl is-active devloop@devloop
   ```

   `test` stampa `params`; `is-active` stampa `inactive`.

2. **La mappa**, prima con `--dry-run`:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh --dry-run
   sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh
   stat -c '%a %U' /home/devloop/devloop/.devloop/redactions.json
   ```

   Il dry run stampa `scriverebbe …/redactions.json (mode 600, proprietario devloop)` con il
   contenuto: i percorsi e gli utenti dei parametri, nessun segreto. Il dry run resta sull'host:
   il suo output ha i valori reali e non si pubblica. `stat` stampa `600 devloop`.

3. **La copia di root del coordinatore**, solo con il coordinatore sull'host (`id coordinator`):
   ha `lib.sh` e `check-host.sh`, che cambiano.

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh
   ```

4. **Verifica**, ancora a loop fermo:

   ```bash
   sudo bash /root/repos/ssdlc/ops/host/check-host.sh --no-apps
   ```

   Nessun `ERRORE`, e l'output nomina `$STATE_DIR`, `$DEVLOOP_KEY_DIR` e gli altri parametri, mai
   i percorsi dell'host: è l'output che si può citare.

5. **Il pin a mano e il riavvio.** Il clone di devloop su `origin/main` e il pin, come utente
   devloop, poi l'avvio, come root:

   ```bash
   sudo -u devloop -H git -C /home/devloop/devloop fetch origin
   sudo -u devloop -H git -C /home/devloop/devloop checkout --detach origin/main
   sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned pin origin/main
   systemctl start devloop@devloop
   systemctl is-active devloop@devloop
   ```

   `is-active` stampa `active`. All'avvio il log di devloop non ha la riga `redaction:
   redactions.json in state_dir missing`.

I testi già pubblicati non si riscrivono: la regola vale dai testi nuovi, e la pulizia di quelli
di prima resta manuale dell'operatore.

**Rollback.** La mappa non cambia il comportamento della copia di prima, che non la legge: si
lascia dov'è. Per tornare alla copia di prima, a loop fermo, il pin del suo commit
(`sudo -u devloop -H python3 /home/devloop/devloop/scripts/devloop-pinned pin <sha12>`, con
`<sha12>` di `pinned-<sha12>` nello `state_dir`) e il riavvio.

## Direzione dell'operatore nei sudoers dei ruoli (#604)

Con #604 devloop ha il comando `devloop direction`, che stampa la direzione dell'operatore in
vigore, l'ultimo commento di un utente di `trusted_users` su `[direction] issue` che inizia con la
riga `## Direzione`
([docs/devloop/guide.md](../../docs/devloop/guide.md#project-direction)). Coordinatore e
supervisore la leggono con `devloop-pinned direction`, che i loro sudoers ammettono da #604, in
sola lettura, senza argomenti. Il sudoers installato sull'host è quello di prima finché gli script
di setup non lo riscrivono: fino ad allora `check-host.sh` lo segnala diverso da quello generato,
e i ruoli non leggono la direzione. Il codice di devloop non ha bisogno d'altro: senza
`[direction]` nell'overlay si comporta come prima.

devloop mergia la PR di #604 come le altre, ma non fa il pin automatico di quel commit né dei
successivi finché l'operatore non fa il pin a mano dopo il passo
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

Come root, **dal checkout dell'operatore**, mai dal clone di devloop, con i valori di
`params.env` (`SERVICE_NAME`, `DEVLOOP_USER`, `DEVLOOP_CLONE`, `SUPERVISOR_USER`,
`COORDINATOR_USER`):

1. aggiornare il checkout dell'operatore a `origin/main`, con il merge di #604:

   ```bash
   git fetch origin
   git checkout --detach origin/main
   ```

2. il sudoers del supervisore, che `setup-supervisor.sh` riscrive dopo `visudo -c`:

   ```bash
   sudo bash ops/host/setup-supervisor.sh --dry-run
   sudo bash ops/host/setup-supervisor.sh
   ```

3. il coordinatore, se c'è: il suo sudoers e la copia di root di `ops/`, che
   `setup-coordinator.sh` aggiorna insieme:

   ```bash
   sudo bash ops/host/setup-coordinator.sh --dry-run
   sudo bash ops/host/setup-coordinator.sh
   ```

4. verificare: `sudo bash ops/host/check-host.sh --no-apps`, nessun errore nei passi del sudoers
   del supervisore e del coordinatore;

5. a loop fermo, il pin a mano del commit di #604 o di uno successivo, come utente
   `DEVLOOP_USER`, dal suo clone, poi l'avvio del loop: lo stop è `systemctl stop
   <SERVICE_NAME>`, il pin `python3 scripts/devloop-pinned pin origin/main` nel clone
   `DEVLOOP_CLONE`, l'avvio `systemctl start <SERVICE_NAME>`. Il pin a mano fa riprendere il pin
   automatico;

6. verificare come i ruoli: `sudo -u <COORDINATOR_USER> -H sudo -n -l` e `sudo -u
   <SUPERVISOR_USER> -H sudo -n -l` elencano `devloop-pinned direction`; il comando, eseguito
   come nei sudoers, stampa `direction: none: no [direction] issue is configured; …` finché
   l'overlay non ha `[direction]`.

**La chiave `[direction] issue`** non fa parte del passo: quale issue usare, e quando, lo decide
l'operatore. Crea l'issue e il primo commento di direzione
([docs/devloop/quickstart.md](../../docs/devloop/quickstart.md#later)), poi aggiunge all'overlay
dello `state_dir`, come utente `DEVLOOP_USER`, la tabella con il numero dell'issue:

```toml
[direction]
issue = <N>
```

Un valore che non è il numero di un'issue ferma devloop all'avvio, come `[report] issue`. Il loop
non la usa: `devloop direction` e `devloop status` leggono l'overlay a ogni esecuzione, e non
serve riavviare nulla.

**Rollback.** I sudoers di prima si rimettono rieseguendo i passi 2 e 3 dal checkout della
versione di prima. La tabella `[direction]` si toglie dall'overlay; la copia di prima la rifiuta
(`unknown key`), quindi va tolta **prima** del pin della copia di prima.

## Autorizzazione del coordinatore (#607)

Con #607, e con `[roles.coordinator]` nell'overlay, il coordinatore autorizza il lavoro con
`/devloop authorize [priority:<p>]` e l'`agent:ready` del supervisore fa partire un'issue solo con
un'autorizzazione valida per il testo di ora
([docs/devloop/guide.md](../../docs/devloop/guide.md#authorization-by-the-coordinator)). Il
supervisore segnala le issue che apre con la label `agent:needs-coordinator`, che sveglia il
coordinatore. Il codice di devloop non legge chiavi nuove: senza `[roles.coordinator]` si comporta
come prima. Servono però due cose sull'host e su GitHub:

- la label `agent:needs-coordinator` sul repository, che `devloop setup` crea con le altre;
- la copia di root di `ops/` del coordinatore aggiornata, con la sveglia `role-wait.sh --role
  coordinator` (e il suo involucro `coordinator-wait.sh`) che esce anche su quella label: finché resta quella di prima, il coordinatore vede le issue da
  autorizzare solo al controllo periodico.

devloop mergia la PR di #607 come le altre, ma non fa il pin automatico di quel commit né dei
successivi finché l'operatore non fa il pin a mano dopo il passo
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

**Prima del pin**, con `[roles.coordinator]` attivo: le issue già in `agent:ready` messe dal
supervisore, senza un `agent:ready` dell'operatore o del coordinatore, dopo il pin non partono più
senza autorizzazione. Il coordinatore scrive `/devloop authorize` su quelle da tenere in coda:
devloop registra i comandi al primo tick con la copia nuova, poi le fa partire.

Come root, **dal checkout dell'operatore**, mai dal clone di devloop, con i valori di
`params.env` (`SERVICE_NAME`, `DEVLOOP_USER`, `DEVLOOP_CLONE`, `COORDINATOR_USER`):

1. aggiornare il checkout dell'operatore a `origin/main`, con il merge di #607:

   ```bash
   git fetch origin
   git checkout --detach origin/main
   ```

2. il coordinatore, se c'è: la copia di root di `ops/` con la sveglia nuova, che
   `setup-coordinator.sh` aggiorna insieme al sudoers:

   ```bash
   sudo bash ops/host/setup-coordinator.sh --dry-run
   sudo bash ops/host/setup-coordinator.sh
   ```

3. verificare: `sudo bash ops/host/check-host.sh --no-apps`, nessun errore nei passi del
   coordinatore;

4. a loop fermo, il pin a mano del commit di #607 o di uno successivo e la creazione della label,
   come utente `DEVLOOP_USER`, dal suo clone `DEVLOOP_CLONE`, poi l'avvio del loop: lo stop è
   `systemctl stop <SERVICE_NAME>`, il pin `python3 scripts/devloop-pinned pin origin/main`, la
   label `python3 scripts/devloop-pinned setup` (crea `agent:needs-coordinator`; delle label che
   ci sono già aggiorna solo colore e descrizione), l'avvio `systemctl start <SERVICE_NAME>`. Il pin a mano fa
   riprendere il pin automatico;

5. verificare: `devloop setup` ha stampato `labels ready:` con `agent:needs-coordinator`; la
   label è nell'elenco delle label del repository su GitHub. La sessione del coordinatore usa la
   sveglia nuova dal suo avvio successivo: non serve riavviarla.

**Rollback.** Il pin a mano della copia di prima, a loop fermo, e `setup-coordinator.sh` dal
checkout della versione di prima, che rimette la sveglia di prima. La label
`agent:needs-coordinator` può restare: la copia di prima non la legge. Le autorizzazioni
registrate restano commenti del dispatcher, che la copia di prima ignora.

## Test di `ops/` e dei runbook nel file di progetto (#615)

`tests/test_ops.py` verifica anche `ops/` e i runbook dei ruoli in `docs/devloop/roles/`, ma
`[tests.dir_modules]` collegava solo `devloop/` ai suoi test (`test_devloop`). Un fallimento di
`test_ops.py` causato da una PR che cambia quelle directory risultava quindi non pertinente: con
`ci_rerun_unrelated` il dispatcher rilanciava la CI invece di aprire subito il round, e dopo
`implement` e `fix` `test_ops.py` non girava in locale. Da #615 l'esempio
[`ops/host/ssdlc/project.toml`](ssdlc/project.toml) collega a `test_ops` le due directory.
Il codice di devloop non cambia: la copia in uso accetta già le voci nuove, e senza di esse si
comporta come prima. Il passo serve perché il file di progetto sull'host le abbia.

devloop mergia la PR di #615 come le altre, ma non fa il pin automatico di quel commit né dei
successivi finché l'operatore non fa il pin a mano dopo il passo
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

Con i valori di `params.env` (`SERVICE_NAME`, `DEVLOOP_USER`, `DEVLOOP_CLONE`, `STATE_DIR`; il
file di progetto è `<STATE_DIR>/project.toml` del clone `DEVLOOP_CLONE`):

1. **fermare il loop**, come root: `systemctl stop <SERVICE_NAME>`;
2. **la copia del file di progetto di prima**, per il rollback, come `DEVLOOP_USER` dal clone
   `DEVLOOP_CLONE`: `cp -p <STATE_DIR>/project.toml <STATE_DIR>/project.toml.prima-615`. È un
   file nuovo: l'originale e le sue ACL non cambiano;
3. **le due voci** in `[tests.dir_modules]` del file di progetto, sotto `"devloop/" =
   "test_devloop"`, come nell'esempio:

   ```toml
   "ops/" = "test_ops"
   "docs/devloop/roles/" = "test_ops"
   ```

   Il file si modifica sul posto, come `DEVLOOP_USER` perché resti suo, con un editor che non lo
   sostituisce con un file nuovo: una sostituzione cancella le ACL del supervisore. Dopo la
   modifica, come root, i controlli e i comandi di [ACL dell'overlay e del file di
   progetto](#acl-delloverlay-e-del-file-di-progetto-543);
4. **il pin a mano** del commit di #615 o di uno successivo, come `DEVLOOP_USER`, dal clone
   `DEVLOOP_CLONE`: `python3 scripts/devloop-pinned pin origin/main`. Il pin carica la
   configurazione con il file di progetto: un errore di sintassi nel file lo fa fallire, e la
   copia in uso resta quella di prima. Il pin a mano fa riprendere il pin automatico;
5. **riavviare il loop**, come root: `systemctl start <SERVICE_NAME>`.

L'hash del file di progetto entra nella versione della policy: le approvazioni delle PR aperte
decadono e serve una nuova review. Meglio con la coda vuota.

**Rollback.** A loop fermo, come `DEVLOOP_USER` dal clone, `cp -p
<STATE_DIR>/project.toml.prima-615 <STATE_DIR>/project.toml` (poi, come root, i comandi di [ACL
dell'overlay e del file di progetto](#acl-delloverlay-e-del-file-di-progetto-543)), oppure si
tolgono a mano le due voci; poi `systemctl start <SERVICE_NAME>`. La copia di prima e quella di
#615 accettano il file con le voci e senza: l'ordine fra pin e file non conta.

## Utenti dei ruoli per progetto (#629)

Supervisore e coordinatore sono per progetto, con un utente di sistema per ruolo e per progetto
([ADR 0002, precisazione del
2026-11-24](../../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-24-ruoli-per-progetto-629)).
Dal #629, con `PROJECT`, i default di `ops/lib.sh` sono `supervisor-<progetto>`,
`coordinator-<progetto>` e il gruppo della casella `devloop-roles-<progetto>`, e gli script di
`ops/host/` si fermano se un utente di ruolo del progetto è già il supervisore o il coordinatore di
un'altra installazione dell'host (`SUPERVISOR_USER '…' è già l'utente supervisor di un'altra
installazione di devloop dell'host`). Le sessioni dei ruoli si chiamano
`devloop-<ruolo>-<progetto>` ([ops/README.md](../README.md#i-quattro-ruoli)).

**Dal #643** gli utenti dei ruoli sono per insieme di app, non per progetto
([Utenti dei ruoli per insieme di app](#utenti-dei-ruoli-per-insieme-di-app-643)): con la versione
di oggi degli script, `leggi` di questa sezione dà già gli utenti dell'insieme
(`supervisor-<APP_SET>`, `coordinator-<APP_SET>`) e le loro `key_dir` (`~/.ghapps`) al posto di
`supervisor-<progetto>` e `coordinator-<progetto>`. Questa sezione serve ancora per separare i
ruoli condivisi fra progetti di **insiemi diversi**; due progetti dello stesso insieme possono
tenere gli stessi utenti dei ruoli.

Questa sezione separa i ruoli di un host che li ha condivisi fra più progetti: gli stessi utenti
`supervisor` e `coordinator`, con una `key_dir` e un clone per progetto nella stessa home, e lo
stesso gruppo `devloop-roles`. Un progetto, il **progetto che resta**, tiene gli utenti e il
gruppo di prima, scritti nei suoi parametri; ogni altro progetto, un **progetto che si separa**,
passa ai suoi utenti. Sull'host di questo repository resta il progetto `devloop`, con
`SUPERVISOR_USER=supervisor`, `COORDINATOR_USER=coordinator` e `COORDINATION_GROUP=devloop-roles`
scritti (`ops/params.env.example`); un progetto senza `PROJECT` resta sempre, perché i suoi
default non cambiano. Un host con un solo progetto non ha nulla da separare: per lui contano solo
i parametri del progetto che resta (dopo il passo 8) e il nome nuovo delle sessioni (passo 9).
Con `PROJECT` e senza `COORDINATION_GROUP` scritto, gli script cercherebbero il gruppo
`devloop-roles-<progetto>`, che l'host non ha ancora.

Il codice di devloop non cambia e il loop dei progetti resta attivo: si fermano solo le sessioni
dei ruoli e i loro risvegli. Si esegue come root, con l'[agent dell'host](../../docs/devloop/roles/host.md),
dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`), **prima i progetti che si separano**, uno
alla volta, poi il progetto che resta: finché un progetto che si separa ha il sudoers con
l'utente condiviso, gli script del progetto che resta si fermano. I passi che chiedono una
persona (login, token, avvio delle sessioni) li fa l'operatore.

### Comandi

Per ogni progetto che si separa, nella stessa shell di root, che resta aperta fino alla fine.
`<checkout>` è il checkout dell'operatore, `<progetto>` il `PROJECT` del progetto e
`<parametri>` il suo file di parametri.

1. **I valori e la copia dei parametri.** `leggi FILE NOME` stampa il valore che gli script di
   `ops/` ricavano da `FILE`, senza la verifica degli altri utenti dell'host; `prima NOME` quello
   di prima, con gli utenti condivisi come default:

   ```bash
   progetto=<progetto>
   ops=<checkout>/ops
   params=<parametri>
   copie=/root/roles-629-$progetto
   leggi() { bash -c '. "$1/lib.sh" && ops_check_host_users() { :; } && PARAMS_FILE=$2 && ops_load_params && printf "%s\n" "${!3}"' _ "$ops" "$1" "$2"; }
   prima() { SUPERVISOR_USER=supervisor COORDINATOR_USER=coordinator COORDINATION_GROUP=devloop-roles leggi "$copie/params.env" "$1"; }
   test ! -e "$copie" && install -d -m 700 "$copie" && cp -p "$params" "$copie/params.env" && echo pronto
   ```

   `test` stampa `pronto`. Una `$copie` che esiste già è di un passo di prima: si cancella solo
   se il suo rollback non serve più.

2. **I parametri del progetto che si separa**: si tolgono le righe che scrivono gli utenti di
   ruolo condivisi, le loro home, le loro `key_dir`, i loro clone e il gruppo della casella, così
   valgono i default del progetto:

   ```bash
   grep -nE '^(SUPERVISOR|COORDINATOR)_(USER|HOME|KEY_DIR|CLONE|SOURCE_CLONE)=|^COORDINATION_GROUP=' "$params"
   sed -i -E '/^(SUPERVISOR|COORDINATOR)_(USER|HOME|KEY_DIR|CLONE|SOURCE_CLONE)=|^COORDINATION_GROUP=/d' "$params"
   for p in SUPERVISOR_USER COORDINATOR_USER COORDINATION_GROUP SUPERVISOR_KEY_DIR COORDINATOR_KEY_DIR SUPERVISOR_CLONE; do
       printf '%s: %s -> %s\n' "$p" "$(prima "$p")" "$(leggi "$params" "$p")"
   done
   ```

   Gli utenti diventano `supervisor-<progetto>` e `coordinator-<progetto>`, il gruppo
   `devloop-roles-<progetto>`, e `key_dir` e clone passano nelle home nuove. Se un valore di
   prima non finisce con `-<progetto>` (una `key_dir` o un clone condivisi), ci si ferma e si
   chiede all'operatore: i passi 5 e 8 spostano e cancellano solo ciò che è del progetto.

3. **Fermare le sessioni dei ruoli e il watchdog del progetto.** Con gli utenti condivisi una
   sola sessione per ruolo poteva girare: si fermano quella e i risvegli, anche del progetto che
   resta, che ripartono al passo 9:

   ```bash
   systemctl stop "devloop-role-watchdog@$progetto.timer"
   sudo -u "$(prima SUPERVISOR_USER)" -H tmux kill-session -t =devloop-supervisor
   sudo -u "$(prima COORDINATOR_USER)" -H tmux kill-session -t =devloop-coordinator
   pkill -u "$(prima SUPERVISOR_USER)" -f 'wait\.sh'
   pkill -u "$(prima COORDINATOR_USER)" -f 'wait\.sh'
   ```

   Una sessione che non c'è dà `can't find session`, un risveglio che non c'è nessun output: va
   bene.

4. **Gli script del progetto**, prima con `--dry-run`: creano gli utenti nuovi, il gruppo della
   casella del progetto (e vi portano le directory della casella), le ACL sulla home e sullo
   `state_dir` di devloop, i sudoers del progetto con gli utenti nuovi, le copie di root, i clone,
   le `key_dir` e il watchdog. `setup-coordinator.sh` solo se il progetto ha il coordinatore:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params "$params" --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params "$params"
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params "$params" --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params "$params"
   chgrp -R "$(leggi "$params" COORDINATION_GROUP)" "$(leggi "$params" COORDINATION_DIR)"
   ```

   `chgrp` porta al gruppo nuovo anche i messaggi e i battiti già nella casella, che restano da
   leggere.

   Senza `SOURCE_REPO`, `setup-coordinator.sh` non clona il repository gestito, e la sessione del
   coordinatore parte da quel clone, `COORDINATOR_HOME/<nome del repository>` (`start-role.sh`
   si ferma se manca): lo si clona nella home nuova, come utente nuovo, se il progetto ha il
   coordinatore. Con `SOURCE_REPO` il clone della sorgente l'ha già preparato lo script:

   ```bash
   repo=$(leggi "$params" REPO) chome=$(leggi "$params" COORDINATOR_HOME) cutente=$(leggi "$params" COORDINATOR_USER)
   if [ -z "$(leggi "$params" SOURCE_REPO)" ] && ! [ -d "$chome/${repo#*/}/.git" ]; then sudo -u "$cutente" -H sh -c 'cd && git clone "$1" "$2"' _ "https://github.com/$repo.git" "$chome/${repo#*/}"; fi
   [ -n "$(leggi "$params" SOURCE_REPO)" ] || [ -d "$chome/${repo#*/}/.git" ] && echo "clone pronto"
   ```

   L'ultima riga stampa `clone pronto`.

5. **Le chiavi delle app di ruolo**, dalle `key_dir` di prima a quelle nuove, che il passo 4 ha
   creato (700, dell'utente nuovo). Si **spostano** con `mv`, mai copiate lasciando l'originale,
   e passano all'utente nuovo, 600; poi la `key_dir` di prima, vuota, si toglie:

   ```bash
   vecchia=$(prima SUPERVISOR_KEY_DIR) nuova=$(leggi "$params" SUPERVISOR_KEY_DIR) utente=$(leggi "$params" SUPERVISOR_USER)
   find "$vecchia" -mindepth 1 -maxdepth 1 -name '*.private-key.pem' -exec mv -n -t "$nuova" -- {} +
   chown "$utente:$(id -gn "$utente")" "$nuova"/*.private-key.pem && chmod 600 "$nuova"/*.private-key.pem
   rmdir "$vecchia" && echo spostate
   vecchia=$(prima COORDINATOR_KEY_DIR) nuova=$(leggi "$params" COORDINATOR_KEY_DIR) utente=$(leggi "$params" COORDINATOR_USER)
   find "$vecchia" -mindepth 1 -maxdepth 1 -name '*.private-key.pem' -exec mv -n -t "$nuova" -- {} +
   chown "$utente:$(id -gn "$utente")" "$nuova"/*.private-key.pem && chmod 600 "$nuova"/*.private-key.pem
   rmdir "$vecchia" && echo spostate
   ```

   Ogni `rmdir` stampa `spostate`; le righe del coordinatore solo se il progetto lo ha. Se
   `rmdir` fallisce, nella `key_dir` di prima è rimasto qualcosa: ci si ferma, senza copiare né
   cancellare nulla, e si chiede all'operatore.

6. **Le ACL degli utenti condivisi** sui file del progetto: gli utenti di prima restano i ruoli
   del progetto che resta, e non leggono più l'overlay né il file di progetto di questo:

   ```bash
   home=$(leggi "$params" DEVLOOP_HOME) state=$(leggi "$params" STATE_PATH)
   setfacl -x "u:$(prima SUPERVISOR_USER)" "$home" "$state" "$state/config.local.toml" "$state/project.toml"
   setfacl -x "u:$(prima COORDINATOR_USER)" "$home" "$state" "$state/config.local.toml"
   getfacl -p --omit-header "$home" "$state" "$state/config.local.toml" "$state/project.toml" | grep -E "^user:($(prima SUPERVISOR_USER)|$(prima COORDINATOR_USER)):" || echo nessuna
   ```

   `grep` non trova nulla e si stampa `nessuna`.

7. **Login e token, dell'operatore**, come utenti nuovi: la CLI di `[roles.supervisor]` e quella
   di `[roles.coordinator]`, ciascuna con il suo login (una quota contata a parte), e il token di
   sola lettura per `gh` di ciascun ruolo, con il solo repository del progetto
   ([github/tokens.md](../github/tokens.md)). Lo stato di `gh` e i login degli utenti di prima non
   si copiano.

8. **Verifica.** `check-host.sh` del progetto non dà nessun `ERRORE` (gli utenti nuovi, le loro
   `key_dir` con le chiavi, il gruppo della casella con i soli ruoli del progetto, i sudoers); il
   token dell'app del supervisore si conia dal suo clone nuovo:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params "$params" --no-apps
   sudo -u "$(leggi "$params" SUPERVISOR_USER)" -H sh -c 'cd "$1" && DEVLOOP_APP_KEY_DIR="$2" python3 scripts/supervisor-token.py --state-dir "$3" --perm issues=read -- gh api "repos/$4" --jq .full_name' _ "$(leggi "$params" SUPERVISOR_CLONE)" "$(leggi "$params" SUPERVISOR_KEY_DIR)" "$(leggi "$params" STATE_PATH)" "$(leggi "$params" REPO)"
   ```

   Lo script dei token stampa il repository del progetto (con `SOURCE_REPO` si lancia dal clone
   della sorgente, `SUPERVISOR_SOURCE_CLONE`). I clone del progetto nella home di prima restano
   fino al passo 10: il rollback li riusa.

Poi, per il **progetto che resta**, con i suoi parametri:

- `SUPERVISOR_USER`, `COORDINATOR_USER` e `COORDINATION_GROUP=devloop-roles` scritti, come in
  `ops/params.env.example` per l'host di questo repository (senza `PROJECT` non servono: sono i
  default);
- `setup-supervisor.sh` e, se c'è il coordinatore, `setup-coordinator.sh`, prima con `--dry-run`,
  poi `check-host.sh`, che non dà nessun `ERRORE`: nel gruppo `devloop-roles` restano solo i suoi
  due ruoli.

9. **Avvio delle sessioni, dell'operatore**: per ogni progetto, con i suoi parametri,
   `sudo bash <checkout>/ops/host/start-role.sh --params <parametri> supervisor` e `coordinator`.
   Le sessioni hanno il nome nuovo, `devloop-<ruolo>-<progetto>` (sull'host di questo repository
   `devloop-supervisor-devloop` e `devloop-coordinator-devloop`); `start-role.sh` si ferma finché
   l'utente del ruolo ha ancora una sessione con il nome di prima. Il watchdog del progetto che si
   separa l'ha riavviato `setup-coordinator.sh` (passo 4).

10. **Pulizia, su conferma dell'operatore** che il rollback non serve più (le sessioni nuove
    girano e i ruoli lavorano): per ogni progetto che si separa, nella shell del passo 1, i clone
    del progetto nella home di prima e la copia dei parametri:

    ```bash
    rm -rf -- "$(prima SUPERVISOR_CLONE)"
    rm -rf -- "$copie"
    ```

    Con `SOURCE_REPO` si cancellano allo stesso modo `$(prima SUPERVISOR_SOURCE_CLONE)` e
    `$(prima COORDINATOR_SOURCE_CLONE)`. Un clone di prima si cancella solo se il suo nome finisce
    con `-<progetto>` (passo 2); il clone del coordinatore senza `SOURCE_REPO`,
    `<home di prima>/<nome del repository>`, resta finché l'operatore non decide altrimenti.

**Rollback**, prima del passo 10. Gli script di questa versione rifiutano per costruzione un
utente di ruolo condiviso fra due progetti (`ops_check_host_users` di `ops/lib.sh`), anche con i
parametri di prima: rieseguirli si ferma prima di riscrivere sudoers e ACL. Il rollback torna
quindi alla versione di prima degli script di `ops/`, per **tutti i progetti dell'host** passati da
questa sezione, compreso quello che resta, le cui copie di root hanno già la `lib.sh` nuova.

1. **Le sessioni nuove e i risvegli**, fermi: per ogni progetto che si separa, nella shell del suo
   passo 1, con i valori di questa versione, letti prima di tornare a quella di prima:

   ```bash
   nsup=$(leggi "$params" SUPERVISOR_USER) ncoord=$(leggi "$params" COORDINATOR_USER)
   nsupkd=$(leggi "$params" SUPERVISOR_KEY_DIR) ncoordkd=$(leggi "$params" COORDINATOR_KEY_DIR)
   systemctl stop "devloop-role-watchdog@$progetto.timer"
   sudo -u "$nsup" -H tmux kill-session -t "=devloop-supervisor-$progetto"
   sudo -u "$ncoord" -H tmux kill-session -t "=devloop-coordinator-$progetto"
   pkill -u "$nsup" -f 'wait\.sh'
   pkill -u "$ncoord" -f 'wait\.sh'
   ```

   Per il progetto che resta, le stesse righe di `systemctl`, `tmux` e `pkill`, con il suo
   `PROJECT` e i suoi utenti. Le righe del coordinatore solo se il progetto lo ha.

2. **La versione di prima** degli script, una volta per host: il commit di `main` prima del merge
   di questa sezione (#629, pull request #641). `git log` lo mostra, per conferma
   dell'operatore:

   ```bash
   versione=$(git -C <checkout> rev-parse 'origin/main^{/^Merge pull request #641 }^1')
   git -C <checkout> log -1 --format='%h %s' "$versione"
   git -C <checkout> checkout --detach "$versione"
   ```

   Da qui `leggi` e `prima` usano la `lib.sh` di prima, in cui i default di `PROJECT` sono gli
   utenti condivisi.

3. **Le chiavi**, per ogni progetto che si separa: dalle `key_dir` nuove a quelle di prima, con
   `mv` come al passo 5, a ruoli invertiti; poi le ACL degli utenti nuovi sui file del progetto,
   tolte come al passo 6:

   ```bash
   vecchia=$(prima SUPERVISOR_KEY_DIR) utente=$(prima SUPERVISOR_USER)
   install -d -m 700 -o "$utente" -g "$(id -gn "$utente")" "$vecchia"
   find "$nsupkd" -mindepth 1 -maxdepth 1 -name '*.private-key.pem' -exec mv -n -t "$vecchia" -- {} +
   chown "$utente:$(id -gn "$utente")" "$vecchia"/*.private-key.pem && chmod 600 "$vecchia"/*.private-key.pem
   vecchia=$(prima COORDINATOR_KEY_DIR) utente=$(prima COORDINATOR_USER)
   install -d -m 700 -o "$utente" -g "$(id -gn "$utente")" "$vecchia"
   find "$ncoordkd" -mindepth 1 -maxdepth 1 -name '*.private-key.pem' -exec mv -n -t "$vecchia" -- {} +
   chown "$utente:$(id -gn "$utente")" "$vecchia"/*.private-key.pem && chmod 600 "$vecchia"/*.private-key.pem
   home=$(prima DEVLOOP_HOME) state=$(prima STATE_PATH)
   setfacl -x "u:$nsup" "$home" "$state" "$state/config.local.toml" "$state/project.toml"
   setfacl -x "u:$ncoord" "$home" "$state" "$state/config.local.toml"
   ls "$nsupkd" "$ncoordkd"
   ```

   `ls` non elenca nessun file: se una `key_dir` nuova non è vuota, ci si ferma e si chiede
   all'operatore.

4. **I parametri e gli script di prima**, per ogni progetto che si separa: i parametri tornano
   alla copia, e gli script della versione di prima riscrivono sudoers, ACL, copie di root
   (con la `lib.sh` di prima) e watchdog con gli utenti di prima, che hanno ancora i loro clone
   (il passo 10 non c'è stato); la casella torna al gruppo di prima:

   ```bash
   cp -p "$copie/params.env" "$params"
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params "$params" --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params "$params"
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params "$params" --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params "$params"
   chgrp -R "$(prima COORDINATION_GROUP)" "$(prima COORDINATION_DIR)"
   ```

   Poi, per il progetto che resta, gli stessi `setup-supervisor.sh` e `setup-coordinator.sh`
   con i suoi parametri, che restano come sono: le sue copie di root tornano alla versione di
   prima. Infine `check-host.sh` della versione di prima, per ogni progetto, senza `ERRORE`.

5. **Le sessioni, dell'operatore**, con `start-role.sh` della versione di prima, con i nomi di
   prima (`devloop-<ruolo>`): con gli utenti condivisi, una sessione per ruolo sull'host, come
   prima della sezione.

Gli utenti nuovi e il loro gruppo restano, senza sudoers né ACL: si tolgono (`userdel -r`,
`groupdel`) solo su decisione dell'operatore. Il checkout resta alla versione di prima finché
l'operatore non riprende la sezione, dall'inizio, con `git -C <checkout> checkout --detach
origin/main`.

## Utenti dei ruoli per insieme di app (#643)

Dal #643 supervisore e coordinatore hanno un utente per ruolo e per **insieme di app**, non per
progetto ([ADR 0002, precisazione del
2026-11-24](../../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-24-utenti-dei-ruoli-per-insieme-di-app-643)).
Con `PROJECT`, i default di `ops/lib.sh` sono `supervisor-<APP_SET>` e `coordinator-<APP_SET>`
(`APP_SET`, per default `APP_PREFIX` senza il `-` finale), con la `key_dir` `~/.ghapps` di quegli
utenti; dal #629 erano `supervisor-<progetto>` e `coordinator-<progetto>`, con
`~/.ghapps-<progetto>`. Restano del progetto la sessione, la casella con il suo gruppo, lo
`state_dir`, i clone, i sudoers e le copie di root. Gli script ammettono lo stesso utente di
ruolo in due progetti solo con lo stesso `app_id` in `[apps.supervisor]` (o `[apps.coordinator]`)
dei due overlay. `setup-coordinator.sh` clona anche il repository gestito in `COORDINATOR_CLONE`,
da cui parte la sessione del coordinatore anche con `SOURCE_REPO`, e scrive le impostazioni di
Claude Code senza aggiornamenti automatici.

Questa sezione porta a un utente per ruolo gli insiemi di app di un host che ha ancora gli utenti
per progetto, **senza utenti nuovi né login nuovi**: per ogni insieme si sceglie un **progetto che
resta**, i cui utenti dei ruoli, con le loro `key_dir`, diventano quelli dell'insieme, scritti nei
parametri di tutti i progetti dell'insieme. Gli altri progetti dell'insieme, i **progetti che
passano**, lasciano i loro utenti di prima. Un insieme con un solo progetto non ha nulla da
riunire: scrive gli utenti di prima nei suoi parametri (passo 3) e riesegue gli script (passo 5).
L'host di questo repository, che ha gli utenti dei ruoli già scritti nei parametri
(`ops/params.env.example`), non cambia: solo il passo 5, per le copie di root e il clone del
coordinatore. Un progetto senza `PROJECT` nemmeno: i suoi default restano `supervisor` e
`coordinator`.

Il codice di devloop non cambia e il loop dei progetti resta attivo: si fermano solo le sessioni dei
ruoli dei progetti che passano. Si esegue come root, con l'[agent
dell'host](../../docs/devloop/roles/host.md), dal checkout dell'operatore aggiornato
(`git -C <checkout> fetch origin` e `git -C <checkout> checkout --detach origin/main`), un insieme
alla volta. I passi che chiedono una persona (la scelta del progetto che resta, l'avvio delle
sessioni) li fa l'operatore.

### Comandi

Per ogni insieme di app, nella stessa shell di root, che resta aperta fino alla fine. `<checkout>`
è il checkout dell'operatore, `<parametri del progetto che resta>` il file di parametri del
progetto che resta, scelto dall'operatore, e `<parametri di ogni progetto dell'insieme>` i file di
tutti i progetti dell'insieme (quelli con lo stesso `APP_PREFIX`), compreso quello che resta.

1. **I valori e la copia dei parametri.** `leggi FILE NOME` stampa il valore che gli script di
   `ops/` ricavano da `FILE`, senza la verifica degli altri utenti dell'host; `prima FILE NOME`
   quello di prima del #643, con gli utenti e le `key_dir` per progetto come default:

   ```bash
   ops=<checkout>/ops
   resta=<parametri del progetto che resta>
   insieme=(<parametri di ogni progetto dell'insieme>)
   copie=/root/roles-643-$(basename "$resta" .env)
   leggi() { bash -c '. "$1/lib.sh" && ops_check_host_users() { :; } && PARAMS_FILE=$2 && ops_load_params && printf "%s\n" "${!3}"' _ "$ops" "$1" "$2"; }
   prima() { local p; p=$(bash -c '. "$1" >/dev/null 2>&1; printf %s "${PROJECT:-}"' _ "$1"); SUPERVISOR_USER=supervisor-$p COORDINATOR_USER=coordinator-$p SUPERVISOR_KEY_DIR=/home/supervisor-$p/.ghapps-$p COORDINATOR_KEY_DIR=/home/coordinator-$p/.ghapps-$p leggi "$1" "$2"; }
   copia() { printf '%s/%s\n' "$copie" "$(basename "$1")"; }
   test ! -e "$copie" && install -d -m 700 "$copie" && for f in "${insieme[@]}"; do cp -p "$f" "$(copia "$f")"; done && echo pronto
   ```

   `test` stampa `pronto`. Una `$copie` che esiste già è di un passo di prima: si cancella solo
   se il suo rollback non serve più. `prima` legge le copie, `leggi` i file.

2. **Il confronto**: per ogni progetto dell'insieme, gli utenti e le `key_dir` dei ruoli di prima
   e quelli che gli script ricaverebbero ora; gli utenti di prima del progetto che resta sono
   quelli che l'insieme terrà:

   ```bash
   for f in "${insieme[@]}"; do for n in SUPERVISOR_USER COORDINATOR_USER SUPERVISOR_KEY_DIR COORDINATOR_KEY_DIR; do printf '%s %s: %s -> %s\n' "$(basename "$f")" "$n" "$(prima "$(copia "$f")" "$n")" "$(leggi "$f" "$n")"; done; done
   grep -nE '^(SUPERVISOR|COORDINATOR)_HOME=' "${insieme[@]}" || echo nessuna
   gia=1; for f in "${insieme[@]}"; do for n in SUPERVISOR_USER COORDINATOR_USER SUPERVISOR_KEY_DIR COORDINATOR_KEY_DIR; do v=$(leggi "$f" "$n"); [ "$v" = "$(prima "$(copia "$f")" "$n")" ] && [ "$v" = "$(leggi "$resta" "$n")" ] || gia=0; done; done; [ "$gia" = 1 ] && echo "già dell'insieme" || echo 'da riunire'
   ```

   `grep` stampa `nessuna`; se un file scrive una home di un ruolo, ci si ferma e si chiede
   all'operatore. L'ultima riga stampa `già dell'insieme` solo se ogni progetto scrive già nei
   parametri gli utenti e le `key_dir` dei ruoli (i due valori coincidono) e se sono, per tutti,
   quelli del progetto che resta: l'insieme ha già i suoi utenti, e si passa al passo 5, poi al
   passo 7, senza i passi 3, 4 e 6. Con `da riunire` si va avanti dal passo 3: anche un progetto
   con i valori già scritti, ma diversi da quelli del progetto che resta, ha ancora utenti suoi.

3. **Gli utenti dell'insieme nei parametri** di ogni progetto dell'insieme: quelli di prima del
   progetto che resta, scritti, al posto di ogni riga che scriveva gli utenti o le `key_dir` dei
   ruoli:

   ```bash
   r=$(copia "$resta")
   valori=("SUPERVISOR_USER=$(prima "$r" SUPERVISOR_USER)" "COORDINATOR_USER=$(prima "$r" COORDINATOR_USER)" "SUPERVISOR_KEY_DIR=$(prima "$r" SUPERVISOR_KEY_DIR)" "COORDINATOR_KEY_DIR=$(prima "$r" COORDINATOR_KEY_DIR)")
   for f in "${insieme[@]}"; do sed -i -E '/^(SUPERVISOR|COORDINATOR)_(USER|KEY_DIR)=/d' "$f" && printf '%s\n' "${valori[@]}" >> "$f"; done
   for f in "${insieme[@]}"; do for n in SUPERVISOR_USER COORDINATOR_USER SUPERVISOR_KEY_DIR COORDINATOR_KEY_DIR; do printf '%s %s: %s\n' "$(basename "$f")" "$n" "$(leggi "$f" "$n")"; done; done
   ```

   L'ultima riga stampa, per ogni progetto, gli stessi quattro valori: quelli di prima del
   progetto che resta.

4. **Le sessioni dei progetti che passano**, ferme, con i loro risvegli e il loro watchdog: per
   ogni file `f` dell'insieme diverso da `$resta`, con i suoi utenti di prima, ruolo per ruolo,
   solo per i ruoli il cui utente cambia:

   ```bash
   for f in "${insieme[@]}"; do [ "$f" = "$resta" ] && continue; c=$(copia "$f"); p=$(leggi "$f" PROJECT); systemctl stop "devloop-role-watchdog@$p.timer"; for r in supervisor coordinator; do n=${r^^}_USER; u=$(prima "$c" "$n"); [ "$u" = "$(leggi "$f" "$n")" ] && { echo "resta: $r di $p"; continue; }; sudo -u "$u" -H tmux kill-session -t "=devloop-$r-$p"; pkill -u "$u" -f 'wait\.sh'; done; done
   ```

   Una sessione che non c'è dà `can't find session`, un risveglio che non c'è nessun output: va
   bene. Le righe del coordinatore valgono solo per un progetto che lo ha. `resta:` nomina un
   ruolo il cui utente di prima è già quello dell'insieme: la sua sessione non si ferma, perché
   `pkill` fermerebbe anche i risvegli degli altri progetti dello stesso utente.

5. **Gli script di ogni progetto dell'insieme**, prima con `--dry-run`: danno agli utenti
   dell'insieme le ACL, il gruppo della casella, i sudoers, le copie di root, i clone e il
   watchdog di ogni progetto, e clonano il repository gestito per il coordinatore
   (`COORDINATOR_CLONE`). Il coordinatore entra se l'overlay lo dichiara:

   ```bash
   for f in "${insieme[@]}"; do sudo bash <checkout>/ops/host/setup-project.sh --params "$f" --dry-run || break; done
   for f in "${insieme[@]}"; do sudo bash <checkout>/ops/host/setup-project.sh --params "$f" --yes || break; done
   ```

   Un `--dry-run` che si ferma con `di un altro insieme di app` vuol dire che i due overlay hanno
   `app_id` diversi per l'app del ruolo: i due progetti non sono dello stesso insieme, e ci si
   ferma e si chiede all'operatore. Se `~/.claude/settings.json` del coordinatore esiste già
   senza `autoUpdates: false` e `DISABLE_AUTOUPDATER=1`, lo script lo dice fra i passi a mano:
   l'operatore li aggiunge.

6. **Gli utenti di prima dei progetti che passano**, fuori dai file del progetto: le ACL sulla home
   di devloop, sullo `state_dir`, sull'overlay e sul file di progetto, il gruppo della casella, e
   le chiavi delle app di ruolo nelle loro `key_dir`, copie di quelle dell'insieme, che si
   tolgono solo se sono uguali a quelle della `key_dir` dell'insieme. Ruolo per ruolo, e solo
   dove cambia: un utente di prima che è già quello dell'insieme tiene le sue ACL e i suoi gruppi,
   e una `key_dir` di prima che è già quella dell'insieme (lo stesso percorso, anche per un link)
   tiene le sue chiavi, che sono quelle che restano:

   ```bash
   for f in "${insieme[@]}"; do [ "$f" = "$resta" ] && continue; c=$(copia "$f"); home=$(leggi "$f" DEVLOOP_HOME) state=$(leggi "$f" STATE_PATH) gruppo=$(leggi "$f" COORDINATION_GROUP); for n in SUPERVISOR_USER COORDINATOR_USER; do u=$(prima "$c" "$n"); [ "$u" = "$(leggi "$f" "$n")" ] && { echo "resta: $u"; continue; }; id -u "$u" >/dev/null 2>&1 || continue; setfacl -x "u:$u" "$home" "$state" "$state/config.local.toml" "$state/project.toml" 2>/dev/null; gpasswd -d "$u" "$gruppo" 2>/dev/null; done; done
   for f in "${insieme[@]}"; do [ "$f" = "$resta" ] && continue; c=$(copia "$f"); for n in SUPERVISOR_KEY_DIR COORDINATOR_KEY_DIR; do vecchia=$(prima "$c" "$n") nuova=$(leggi "$f" "$n"); [ "$(realpath -m -- "$vecchia")" = "$(realpath -m -- "$nuova")" ] && { echo "resta: $vecchia"; continue; }; [ -d "$vecchia" ] || continue; for k in "$vecchia"/*.private-key.pem; do [ -e "$k" ] || continue; if cmp -s "$k" "$nuova/$(basename "$k")"; then rm -- "$k"; else echo "diversa: $k"; fi; done; rmdir "$vecchia" && echo "vuota: $vecchia"; done; done
   ```

   La seconda riga stampa `vuota:` per ogni `key_dir` di prima che cambia, e `resta:` per una che
   è già quella dell'insieme. Se stampa `diversa:`, o se un `rmdir` fallisce, nella `key_dir` di
   prima resta una chiave che non è una copia di quella dell'insieme: ci si ferma, senza copiare
   né cancellare altro, e si chiede all'operatore.

7. **Verifica.** `check-host.sh` di ogni progetto dell'insieme non dà nessun `ERRORE`: gli utenti
   dell'insieme, le loro `key_dir` con le chiavi, il gruppo di ogni casella con i soli ruoli del
   progetto, i sudoers, il clone del coordinatore:

   ```bash
   for f in "${insieme[@]}"; do sudo bash <checkout>/ops/host/check-host.sh --params "$f" --no-apps; done
   ```

8. **Avvio delle sessioni, dell'operatore**: per ogni progetto che passa,
   `sudo bash <checkout>/ops/host/start-role.sh --params <parametri> supervisor` e `coordinator`.
   Le sessioni hanno gli stessi nomi di prima, `devloop-<ruolo>-<progetto>`, con l'utente
   dell'insieme; il coordinatore parte dal suo clone del repository gestito. Nessun login nuovo:
   gli utenti dell'insieme li hanno già. Un ruolo che al passo 4 stampava `resta:` ha ancora la
   sua sessione, e `start-role.sh` dice che esiste già: va bene.

9. **Pulizia, su conferma dell'operatore** che il rollback non serve più: la copia dei parametri
   (`rm -rf -- "$copie"`). Gli utenti di prima dei progetti che passano, con le loro home, i loro
   clone e i loro login, restano senza sudoers, ACL né chiavi: si tolgono (`userdel -r`) solo su
   decisione dell'operatore.

**Rollback**, prima del passo 9, nella shell del passo 1. Gli utenti di prima esistono ancora, con
i loro clone e i loro login.

1. Le sessioni dei progetti che passano, ferme, come al passo 4 con gli utenti dell'insieme
   (`leggi` al posto di `prima`).
2. Le chiavi, i parametri e gli script di prima, per ogni progetto che passa: le chiavi tornano
   nelle `key_dir` di prima, copiate da quella dell'insieme, i parametri alla copia, e gli
   script, con i parametri di prima, riscrivono sudoers, ACL e watchdog con gli utenti di prima:

   ```bash
   for f in "${insieme[@]}"; do [ "$f" = "$resta" ] && continue; c=$(copia "$f"); for n in SUPERVISOR:SUPERVISOR_KEY_DIR COORDINATOR:COORDINATOR_KEY_DIR; do u=$(prima "$c" "${n%%:*}_USER") vecchia=$(prima "$c" "${n#*:}") nuova=$(leggi "$f" "${n#*:}"); [ "$(realpath -m -- "$vecchia")" = "$(realpath -m -- "$nuova")" ] && continue; id -u "$u" >/dev/null 2>&1 || continue; install -d -m 700 -o "$u" -g "$(id -gn "$u")" "$vecchia"; for k in "$nuova"/*.private-key.pem; do [ -e "$k" ] && install -m 600 -o "$u" -g "$(id -gn "$u")" "$k" "$vecchia/"; done; done; done
   for f in "${insieme[@]}"; do cp -p "$(copia "$f")" "$f"; done
   ```

   Poi, per ogni progetto che passa, gli script di prima, cioè dalla versione di `main` prima
   del merge di #643, `setup-supervisor.sh` e `setup-coordinator.sh` (se il progetto ha il
   coordinatore) con `--dry-run` e poi senza; per ogni progetto dell'insieme, l'utente
   dell'insieme tolto dal gruppo della casella dei progetti che passano (`gpasswd -d <utente>
   <gruppo>`) e le sue ACL sui loro file (`setfacl -x`, come al passo 6), tranne dove ne era già
   l'utente prima (`resta:` al passo 6); `check-host.sh` di ogni progetto senza `ERRORE`.
3. Le sessioni, dell'operatore, con `start-role.sh` e gli utenti di prima.

## Letture del supervisore con la sua app (#643)

Dal #643 (scelta S2 dell'operatore) il supervisore **legge** GitHub con la sua app, come il
coordinatore: `scripts/supervisor-token.py` con i soli permessi di lettura (`--perm
issues=read`, `--perm pull_requests=read` e, per la CI, `--perm actions=read`, `checks=read` o
`statuses=read`), e non ha più un token personale di `gh`
([github/tokens.md](../github/tokens.md)). Il manifest `ops/github/apps/supervisor.json` ha in
più `actions`, `checks` e `statuses` in sola lettura, e lo script li concede solo con un `--perm`
esplicito. Ogni lettura conia un token dalla chiave dell'app, che diventa l'unica credenziale del
supervisore.

Il codice di devloop non cambia e il loop resta attivo. Passi per ogni insieme di app dell'host
(l'app supervisore è una per insieme), in quest'ordine:

1. **I permessi dell'app, dell'operatore, su GitHub.** Il manifest
   `ops/github/apps/supervisor.json` fa fede (#655): nelle impostazioni dell'app
   `<APP_PREFIX>supervisor` → *Permissions & events* → *Repository permissions* i permessi devono
   essere esattamente quelli di `default_permissions` del manifest: *Issues* e *Pull requests* in
   *Read and write*, *Metadata*, *Actions*, *Checks* e *Commit statuses* in *Read-only*, nessun
   altro (né *Contents*, né permessi di account o di organizzazione). Si aggiungono *Actions*,
   *Checks* e *Commit statuses* in *Read-only* e si tolgono (*No access*) quelli in più, che
   un'app creata a mano prima dei manifest (#402) può avere: chi ha la chiave può chiederli
   coniando un token senza passare da `scripts/supervisor-token.py`, quindi conta quello che
   l'app possiede, non i limiti dello script. *Save changes*. Poi, nell'installazione del
   proprietario (*Install App* → l'installazione → *Review request*), si accettano i permessi
   nuovi: finché non sono accettati, un token che li chiede fallisce. Togliere un permesso non
   chiede di riaccettare l'installazione. Lo stesso confronto per le altre app dell'insieme è in
   [ops/README.md](../README.md#2-github-app) (*I permessi di un'app che esiste già*).
2. **Il clone dei token, a una versione con il #643**, per ogni progetto dell'insieme.
   `scripts/supervisor-token.py` che il supervisore esegue è quello del clone da cui conia i
   token, non quello del checkout dell'operatore: uno di prima del #643 rifiuta `actions`,
   `checks` e `statuses` (`permission actions=read is not allowed`). `<parametri>` è il file di
   parametri del progetto, `leggi` quella del passo 1 di [Utenti dei ruoli per insieme di
   app](#utenti-dei-ruoli-per-insieme-di-app-643).
   - **Senza `SOURCE_REPO`** il clone è `SUPERVISOR_CLONE`, un clone del repository gestito, che
     contiene devloop. Come utente del supervisore, si porta in avanti fino a
     `origin/<DEFAULT_BRANCH>`, solo se è su quel ramo e senza commit suoi:

     ```bash
     u=$(leggi <parametri> SUPERVISOR_USER) clone=$(leggi <parametri> SUPERVISOR_CLONE) ramo=$(leggi <parametri> DEFAULT_BRANCH)
     [ "$(sudo -u "$u" -H git -C "$clone" symbolic-ref --short HEAD)" = "$ramo" ] && echo "sul ramo $ramo" && \
       sudo -u "$u" -H git -C "$clone" fetch origin && \
       sudo -u "$u" -H git -C "$clone" merge --ff-only "origin/$ramo"
     ```

     Il secondo comando stampa `sul ramo …`, e `merge` riesce. Su un altro ramo non stampa nulla
     e termina con un errore, senza `fetch` né `merge`. Un clone su un altro ramo, con modifiche
     o con commit che `origin` non ha, è lavoro del supervisore: ci si ferma, senza cambiarlo, e
     si chiede all'operatore.
   - **Con `SOURCE_REPO`** il clone è `SUPERVISOR_SOURCE_CLONE`, al tag `SOURCE_REF` del
     progetto, a cui `setup-supervisor.sh` lo riporta a ogni esecuzione. Se `SOURCE_REF` è un
     tag di prima del #643 (la prova del passo 3 lo dice), questa migrazione per quel progetto
     aspetta la versione nuova: l'operatore fissa un tag che contiene il #643 e lo scrive in
     `SOURCE_REF`, come per ogni versione nuova di devloop ([guida
     rapida](../../docs/devloop/quickstart.md#later)), poi `setup-project.sh` del progetto, prima
     con `--dry-run`, porta al tag il clone di ogni ruolo. Questa procedura non cambia
     `SOURCE_REF`: con il tag cambia anche l'avviatore del progetto, che è un pin dell'operatore.
3. **La prova**, come utente del supervisore, dal clone da cui conia i token (`SUPERVISOR_CLONE`,
   o `SUPERVISOR_SOURCE_CLONE` con `SOURCE_REPO`), con la sua `key_dir` e lo `state_dir` del
   progetto:

   ```bash
   clone=$(leggi <parametri> SUPERVISOR_CLONE); [ -z "$(leggi <parametri> SOURCE_REPO)" ] || clone=$(leggi <parametri> SUPERVISOR_SOURCE_CLONE)
   sudo -u "$(leggi <parametri> SUPERVISOR_USER)" -H sh -c 'cd "$1" && DEVLOOP_APP_KEY_DIR="$2" python3 scripts/supervisor-token.py --state-dir "$3" --perm actions=read --perm checks=read -- gh api "repos/$4/actions/runs?per_page=1" --jq .total_count' _ "$clone" "$(leggi <parametri> SUPERVISOR_KEY_DIR)" "$(leggi <parametri> STATE_PATH)" "$(leggi <parametri> REPO)"
   ```

   Stampa un numero. Un errore `403` vuol dire che i permessi del passo 1 non sono ancora
   accettati nell'installazione, `permission actions=read is not allowed` che il clone dei token
   è ancora di prima del #643 (passo 2): in entrambi i casi ci si ferma, con il token personale
   ancora al suo posto, e si chiede all'operatore. Il passo 4 viene solo dopo una prova riuscita.
4. **Il token personale, tolto.** Come utente del supervisore (e del coordinatore, se ne aveva
   uno), il login di `gh` si chiude; l'operatore cancella poi i token su GitHub (*Settings* →
   *Developer settings* → *Personal access tokens* → *Fine-grained tokens* → *Delete*):

   ```bash
   sudo -u "$(leggi <parametri> SUPERVISOR_USER)" -H gh auth logout --hostname github.com
   sudo -u "$(leggi <parametri> SUPERVISOR_USER)" -H gh auth status
   ```

   `gh auth status` dice che non c'è nessun login. Un utente senza login dà già `not logged in`
   al primo comando: va bene.
5. **Le sessioni del supervisore** ripartono con `start-role.sh` (dell'operatore), che dà al
   primo messaggio la `key_dir` e il clone dei token del supervisore; il runbook dice come
   leggere con l'app.

**Rollback.** Un token di sola lettura nuovo, creato dall'operatore come nella versione di
`ops/github/tokens.md` prima di #643 e installato con `gh auth login --with-token` come utente del
supervisore. I permessi in più dell'app possono restare: nessuno li usa senza un `--perm`
esplicito.

## Comando devloop dell'host (#588)

Dal #588 `setup-devloop.sh` installa il comando `devloop` dell'host, l'involucro del launcher
([`devloop-command`](devloop-command)), in `DEVLOOP_COMMAND` (`/usr/local/bin/devloop`, 755 di
root), e il file del progetto `params-<progetto>.env` (`params-_default.env` senza `PROJECT`; 644
di root, da [`devloop-command-project`](devloop-command-project)) nella directory di root
`DEVLOOP_PROJECTS_DIR` (`/etc/devloop/projects`). `devloop <sottocomando>` esegue il launcher del
progetto come la sua unità: l'utente `DEVLOOP_USER`, il clone, la copia fissata del servizio e lo
`--state-dir` del progetto ([ops/README.md](../README.md#4-host-di-devloop)). Con più progetti
sull'host vuole `--project <progetto>` prima del sottocomando. `check-host.sh` verifica i due file:
un avviso se mancano, un errore se non sono di root o se altri possono scriverli. I sudoers di
supervisore e coordinatore non cambiano.

Il codice di devloop non ha bisogno del comando: gli avvisi di un pin automatico sospeso lo
nominano (`devloop pin`, o `devloop --project <nome> pin` con più progetti), e finché manca il pin a
mano resta il comando lungo dell'unità. Il passo non ferma il loop.

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`), **per ogni progetto dell'host**, con il suo
file di parametri `<parametri>` (uno con `SOURCE_REF` al tag successivo, con [la sua
procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **lo script**, prima con `--dry-run`: l'elenco delle azioni deve contenere, oltre a quanto
   lo script già ricorda come da fare a mano, solo la directory dei progetti (al primo progetto),
   il file del progetto e il comando, e la mappa delle sostituzioni (`redactions.json`), che dal
   #588 nomina anche `DEVLOOP_COMMAND` e `DEVLOOP_PROJECTS_DIR`. Un'altra azione (un'unità, un
   sudoers, un utente) vuol dire che l'host non corrisponde ai parametri: ci si ferma e si chiede
   all'operatore.

   ```bash
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri>
   ```

2. **la verifica**: `check-host.sh` non dà nessun `ERRORE` e, nel passo *Comando devloop*, due
   righe `ok: … coincide con ops/host/devloop-command` e `… devloop-command-project`:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

3. **la prova**, in sola lettura: `devloop status` (con più progetti `devloop --project <progetto>
   status`) stampa lo stato del progetto, come il comando lungo dell'unità. Con più progetti,
   `devloop status` senza `--project` si ferma ed elenca i progetti installati.

**Togliere un progetto** dal comando: come root, `rm <DEVLOOP_PROJECTS_DIR>/params-<progetto>.env`.
Il servizio e l'installazione del progetto restano; un `setup-devloop.sh` successivo lo rimette.

**Rollback.** Come root, `rm <DEVLOOP_COMMAND>` e i file `params-*.env` di `DEVLOOP_PROJECTS_DIR`,
poi `rmdir <DEVLOOP_PROJECTS_DIR>`. Il servizio non li usa; gli avvisi di devloop nominano ancora
`devloop pin`, che senza il comando si fa con il comando lungo dell'unità (`ExecStart` dell'unità o
della drop-in del progetto, senza `run`, come `DEVLOOP_USER`).

## Avvisi all'operatore (#593)

Con #593 devloop avvisa l'operatore anche fuori da GitHub
([docs/devloop/guide.md](../../docs/devloop/guide.md#notices-to-the-operator)): il comando
`devloop notify`, con il canale Telegram di `[notify]` dell'overlay, solo in uscita; la menzione
dei login di `operator_mentions` nei commenti che chiedono una decisione; l'`OnFailure=` delle
unità del servizio verso la nuova unità template [`devloop-failed@.service`](devloop-failed@.service),
che esegue `devloop notify --service-failed <progetto>` con il comando devloop dell'host (#588);
la regola `devloop-pinned notify *` nei sudoers di supervisore e coordinatore (scelta N2 (a)
dell'operatore: un solo bot, il token leggibile solo dall'utente devloop).

Il codice di devloop non ha bisogno d'altro: senza `[notify]` un avviso è solo una riga nel log,
senza `operator_mentions` i commenti non menzionano nessuno. Il passo serve all'unità e ai sudoers
installati sull'host, che restano quelli di prima finché gli script di setup non li riscrivono:
fino ad allora `check-host.sh` li segnala diversi da quelli generati e segnala come errore che
`devloop-failed@.service` non esiste. devloop mergia la PR di #593 come le altre, ma non fa il pin
automatico di quel commit né dei successivi finché l'operatore non fa il pin a mano dopo il passo
([Ogni cambio con un passo sull'host](#ogni-cambio-con-un-passo-sullhost)).

### Comandi

Come root, dal checkout dell'operatore aggiornato (`git -C <checkout> fetch origin` e
`git -C <checkout> checkout --detach origin/main`, con il merge di #593), **per ogni progetto
dell'host**, con il suo file di parametri `<parametri>` (uno con `SOURCE_REF` al tag successivo,
con [la sua procedura](#un-progetto-con-la-sorgente-a-un-tag-nuovo-666)):

1. **l'unità**, prima con `--dry-run`: l'elenco delle azioni deve contenere, oltre a quanto lo
   script già ricorda come da fare a mano, solo l'unità del servizio (`OnFailure=`), la nuova
   `SYSTEMD_DIR/devloop-failed@.service` (al primo progetto dell'host) e `systemctl
   daemon-reload`. Un'altra azione vuol dire che l'host non corrisponde ai parametri: ci si ferma
   e si chiede all'operatore. Il servizio non si riavvia per questo: `OnFailure=` vale dal
   `daemon-reload`.

   ```bash
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-devloop.sh --params <parametri>
   ```

2. **i sudoers dei ruoli**: quello del supervisore e, se c'è, quello del coordinatore con la sua
   copia di root di `ops/`, che `setup-coordinator.sh` aggiorna insieme:

   ```bash
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-supervisor.sh --params <parametri>
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri> --dry-run
   sudo bash <checkout>/ops/host/setup-coordinator.sh --params <parametri>
   ```

3. **la verifica**: `check-host.sh` non dà nessun `ERRORE`, con le righe `ok: … coincide con
   ops/host/devloop-failed@.service` e, nel passo *Token del bot degli avvisi*, `l'overlay non ha
   [notify]` finché il canale non c'è:

   ```bash
   sudo bash <checkout>/ops/host/check-host.sh --params <parametri> --no-apps
   ```

4. **il pin a mano**, a loop fermo, del commit di #593 o di uno successivo, come utente
   `DEVLOOP_USER`, poi l'avvio: `systemctl stop <SERVICE_NAME>`, `devloop pin origin/main` (con
   più progetti `devloop --project <progetto> pin origin/main`), `systemctl start
   <SERVICE_NAME>`. Il pin a mano fa riprendere il pin automatico. `devloop status` mostra le
   righe `notify: no channel …` e `operator mentions: none …`.

### Il canale e le menzioni (dell'operatore)

Non fanno parte del passo: il bot, il token e i login li sceglie l'operatore, quando vuole, e
il token non passa mai dalla sessione di un agent. Come in
[docs/devloop/guide.md](../../docs/devloop/guide.md#devloop-notify-and-telegram):

1. il bot con `@BotFather` (`/newbot`), il `chat_id` da un messaggio mandato al bot e letto a mano
   una volta con `getUpdates`;
2. il token da solo in un file della key_dir dell'utente devloop (`DEVLOOP_KEY_DIR`), di
   `DEVLOOP_USER`, mode 600, scritto senza passare dalla riga di comando (per esempio con un
   editor, come `DEVLOOP_USER`);
3. nell'overlay dello `state_dir`, come `DEVLOOP_USER`:

   ```toml
   operator_mentions = ["<login dell'operatore>"]

   [notify]
   backend = "telegram"
   chat_id = <chat_id>
   token_file = "<percorso assoluto del file del token>"
   ```

   Un valore non valido ferma devloop all'avvio: si verifica con `devloop status` prima di
   riavviare il loop, che legge le due chiavi al proprio avvio (`devloop notify` le rilegge a ogni
   esecuzione);
4. la prova: `devloop notify --test` (con più progetti `devloop --project <progetto> notify
   --test`) esce con 0 e il messaggio di prova arriva sul telefono; `check-host.sh` dà `ok:
   token_file di [notify]: 600, <DEVLOOP_USER>`;
5. nelle impostazioni del repository su GitHub, *Security*, attivare **Private vulnerability
   reporting** ([SECURITY.md](../../SECURITY.md)) e verificare che la notifica di una nuova
   advisory arrivi davvero sul telefono, con GitHub Mobile. Se non arriva, lo si scrive
   nell'issue #593 o in una nuova: serve il ping su Telegram anche per le advisory (scelta N3).

**Rollback.** I sudoers e l'unità del servizio di prima si rimettono rieseguendo i passi 1 e 2 dal
checkout della versione di prima; poi, come root, `rm <SYSTEMD_DIR>/devloop-failed@.service` e
`systemctl daemon-reload`. `[notify]` e `operator_mentions` si tolgono dall'overlay **prima** del
pin della copia di prima, che li rifiuta (`unknown key`). Il bot si revoca con `@BotFather`
(`/revoke` o `/deletebot`), e il file del token si rimuove.

## Un progetto con la sorgente a un tag nuovo (#666)

La procedura per portare un progetto con la sorgente a parte (`SOURCE_REPO` e `SOURCE_REF`,
[docs/devloop/guide.md](../../docs/devloop/guide.md#installation-source)) da un tag di devloop a
uno nuovo, su qualunque host. Non è un passo da eseguire una volta: si ripete a ogni tag nuovo, e
per ogni progetto che lo vuole. Non riguarda un'installazione senza `SOURCE_REPO`, come quella del
repository di devloop, che segue `main` con il pin dopo un merge. Come si sceglie e si annuncia un
tag è in [Releases of devloop](../../docs/devloop/guide.md#releases-of-devloop).

Qui `<progetto>` è il `PROJECT` del progetto, `<parametri>` il suo file di parametri, `<checkout>`
il checkout dell'operatore di `brushtyler/devloop`, `<tag di prima>` il tag di `SOURCE_REF` di
oggi e `<tag nuovo>` quello da fissare; `SERVICE_NAME`, `SUPERVISOR_USER` e `COORDINATOR_USER`
sono i valori che gli script ricavano da `<parametri>` (`ops/params.env.example`). Si esegue come
root, con l'[agent dell'host](../../docs/devloop/roles/host.md), dal checkout dell'operatore
aggiornato (`git -C <checkout> fetch origin --tags`). I passi che chiedono una persona (l'avvio
delle sessioni) li fa l'operatore.

**Tutto dal checkout del tag (#687).** Per questo progetto ogni script di `ops/host/` (le
migrazioni del passo 3, `setup-*.sh`, `check-host.sh` del passo 4, `start-role.sh` del passo 7)
si esegue da `<checkout del tag>`, un worktree di `<checkout>` fermo su `<tag nuovo>`, mai da
`<checkout>` su `main`. Gli script installano le copie di root di `ops/` dei ruoli
(`SUPERVISOR_OPS_DIR`, `COORDINATOR_OPS_DIR`, con `host/role-wait.sh` e `lib.sh`) dal checkout da
cui girano, e `start-role.sh` dà alla sessione il runbook (`docs/devloop/roles/<ruolo>.md`) e la
configurazione dei ruoli (`devloop/config.toml`) del suo checkout: da `main`, più avanti del tag,
il progetto avrebbe il loop del tag con le sveglie e i runbook di `main`. Così una correzione di
`ops/` arrivata su `main` dopo il tag raggiunge il progetto solo con un tag nuovo. `check-host.sh`
confronta le copie con quelle del checkout da cui gira e, se quel checkout non è al tag
`SOURCE_REF` fissato, lo dice con un avviso. `<checkout>` serve solo ai comandi `git` che leggono
i tag. Il worktree si crea la prima volta e poi si riusa, prima del passo 3:

```bash
git -C <checkout> worktree add --detach <checkout del tag> '<tag nuovo>'   # la prima volta
git -C <checkout del tag> checkout --detach '<tag nuovo>'                  # le volte successive
git -C <checkout del tag> describe --tags --exact-match HEAD
git -C <checkout del tag> status --porcelain
```

`describe` stampa `<tag nuovo>`; `status` non stampa nulla.

### Comandi

1. **Fermare il loop** del progetto: `systemctl stop <SERVICE_NAME>`. Le sessioni dei ruoli
   restano attive fino al passo 7.

2. **Il tag nuovo nei parametri**: in `<parametri>`, `SOURCE_REF=<tag nuovo>` al posto di
   `SOURCE_REF=<tag di prima>`.

3. **Le sezioni introdotte fra i due tag.** Sono i titoli `## ` di questo file nel tag nuovo che
   il tag di prima non ha, nell'ordine del file; il messaggio del tag nuovo li elenca già
   ([Releases of devloop](../../docs/devloop/guide.md#releases-of-devloop)). Il comando li stampa
   dai due tag, senza questa sezione:

   ```bash
   git -C <checkout> show '<tag nuovo>:ops/host/migrate.md' | grep '^## ' \
       | grep -vxF -f <(git -C <checkout> show '<tag di prima>:ops/host/migrate.md' | grep '^## ') \
       | grep -vxF '## Un progetto con la sorgente a un tag nuovo (#666)'
   ```

   Si eseguono le sezioni stampate, una alla volta e nell'ordine del file, per il solo
   `<progetto>`, come le scrive `<checkout del tag>/ops/host/migrate.md`: ci sono tutte anche nel
   tag nuovo. Molte sono scritte per l'installazione del repository di devloop e ne nominano i
   valori (il checkout, il clone, l'utente, lo `state_dir`, il servizio): i loro comandi non si
   eseguono come sono, ma riportati al progetto, con queste regole.

   - **Ogni script di `ops/host/`** si lancia da `<checkout del tag>`, anche dove la sezione
     scrive `<checkout>`, con `--params <parametri>`. Senza, legge `ops/params.env` del checkout,
     cioè i parametri dell'installazione di devloop, e agisce su quella. Il passo che porta
     `<checkout>` a `origin/main` si salta: `<checkout del tag>` resta su `<tag nuovo>`.
   - **I valori dell'installazione di devloop** che una sezione scrive per esteso diventano quelli
     di `<parametri>`: il checkout diventa `<checkout>`, il clone `DEVLOOP_CLONE`, l'utente
     `DEVLOOP_USER`, lo `state_dir` lo `STATE_DIR` del progetto (relativo a `DEVLOOP_CLONE`), il
     servizio `<SERVICE_NAME>`; lo stesso per gli utenti, le home e le directory dei ruoli
     (`SUPERVISOR_USER`, `COORDINATOR_USER`, …). Dove una sezione dice "per ogni progetto
     dell'host" vale il solo `<parametri>`.
   - **Il pin e l'avvio del servizio** che chiudono alcune sezioni non si fanno lì: li fanno i
     passi 5 e 6. Un comando del launcher dal clone di devloop (`python3 scripts/devloop-pinned
     <sottocomando>`) diventa `devloop --project <progetto> <sottocomando>` e, se vuole la copia
     nuova, si esegue dopo il passo 5 e prima del 6.
   - **Una chiave nuova dell'overlay** che una sezione fa scrivere (come `rest_dir` di [Il riposo
     dei ruoli](#il-riposo-dei-ruoli-684)) non si scrive lì: il loop fissato è ancora quello del
     tag di prima, che rifiuta le chiavi che non conosce, e il pin del passo 5 si fermerebbe con
     `devloop: invalid config: local config: unknown key …`. Si annota e si scrive al passo 6,
     dopo il pin e prima dell'avvio ([Ogni cambio con un passo
     sull'host](#ogni-cambio-con-un-passo-sullhost)).
   - **Un passo dell'installazione di devloop**, che porta il suo clone a `origin/main`, si salta:
     il progetto resta al tag.
   - **Il `--dry-run`** che precede uno script nomina solo i valori di `<parametri>`. Un percorso,
     un utente o un servizio di un'altra installazione vuol dire un comando non riportato al
     progetto: ci si ferma e si chiede all'operatore.

   Da v0.2.0 a v0.3.0, per esempio, [Mappa delle sostituzioni dei testi
   pubblicati](#mappa-delle-sostituzioni-dei-testi-pubblicati-583) diventa: ai suoi passi 1–4
   `test -f <parametri>`, `setup-devloop.sh --params <parametri>` (prima con `--dry-run`), `stat
   -c '%a %U'` sul `redactions.json` dello `state_dir` del progetto, che stampa
   `600 <DEVLOOP_USER>`, `setup-coordinator.sh --params <parametri>` se il progetto ha il
   coordinatore e `check-host.sh --params <parametri> --no-apps`; il suo passo 5 lo fanno i passi
   5 e 6 di qui. [Autorizzazione del coordinatore](#autorizzazione-del-coordinatore-607) diventa
   `setup-coordinator.sh --params <parametri>` e `check-host.sh --params <parametri> --no-apps`,
   e dopo il pin del passo 5 `devloop --project <progetto> setup`, che crea la label
   `agent:needs-coordinator`.

   Una sezione stampata che non descrive un passo ma una procedura a richiesta (come questa, o
   [Spostare una key_dir](#spostare-una-key_dir-582)) si salta: lo dice la sua introduzione.

   Queste sezioni rilanciano `setup-project.sh`. Se il comando non stampa nulla, o se nessuna
   delle sezioni stampate lo rilancia, si rilancia comunque, perché porti al tag nuovo i clone
   della sorgente dei ruoli (`SUPERVISOR_SOURCE_CLONE`, `COORDINATOR_SOURCE_CLONE`) e l'avviatore
   (`LAUNCHER_DIR`), con `--coordinator` se il progetto ha il coordinatore:

   ```bash
   sudo bash <checkout del tag>/ops/host/setup-project.sh --params <parametri> --dry-run
   sudo bash <checkout del tag>/ops/host/setup-project.sh --params <parametri>
   ```

4. **La verifica**: `check-host.sh` non dà nessun `ERRORE`, e il suo passo `Checkout di
   check-host.sh e tag SOURCE_REF` dice `ok: il checkout di questo script è al tag <tag nuovo>
   fissato`:

   ```bash
   sudo bash <checkout del tag>/ops/host/check-host.sh --params <parametri>
   ```

5. **Il pin del tag nuovo**, con il [comando devloop dell'host](#comando-devloop-dellhost-588):
   `devloop --project <progetto> pin <tag nuovo>` (con un solo progetto sull'host `devloop pin
   <tag nuovo>`). Il pin finisce con lo smoke test della copia nuova
   ([Pin](../../docs/devloop/guide.md#pin)); `devloop: invalid config: …` nomina una chiave
   dell'overlay o del file di progetto che il tag nuovo legge diversamente: la descrive una delle
   sezioni del passo 3.

6. **L'avvio del servizio**: prima, come l'operatore (l'overlay è suo), le chiavi nuove
   dell'overlay annotate al passo 3, che il codice del tag nuovo, ora fissato, conosce; poi
   `systemctl start <SERVICE_NAME>`, che legge l'overlay con quelle chiavi al suo avvio.

7. **Le sessioni dei ruoli**, perché leggano i runbook del tag nuovo: da `<checkout del tag>`, non
   da `<checkout>`. Prima, `describe` conferma che il worktree è ancora su `<tag nuovo>`; il
   `diff` mostra quanto i runbook di `main` sono già oltre il tag (vuoto se coincidono). Poi
   l'operatore chiude la sessione del supervisore e la riavvia, e quella del coordinatore se il
   progetto lo ha:

   ```bash
   git -C <checkout del tag> describe --tags --exact-match HEAD
   git -C <checkout> diff --stat '<tag nuovo>' origin/main -- docs/devloop/roles/ devloop/config.toml
   sudo -u <SUPERVISOR_USER> -H tmux kill-session -t '=devloop-supervisor-<progetto>'
   sudo bash <checkout del tag>/ops/host/start-role.sh --params <parametri> supervisor
   sudo -u <COORDINATOR_USER> -H tmux kill-session -t '=devloop-coordinator-<progetto>'
   sudo bash <checkout del tag>/ops/host/start-role.sh --params <parametri> coordinator
   ```

   Il primo messaggio di ogni sessione nomina il checkout del suo runbook (`… of the checkout
   <checkout del tag>`): con `<checkout>` la sessione ha i runbook di `main`, e va riavviata da
   `<checkout del tag>`.

**Rollback.** A loop fermo: `git -C <checkout del tag> checkout --detach '<tag di prima>'`, da
cui si esegue tutto il resto; il rollback di ciascuna sezione eseguita al passo 3, dall'ultima
alla prima; `SOURCE_REF=<tag di prima>` in `<parametri>`; `setup-project.sh` e `check-host.sh`
come ai passi 3 e 4, che rimettono le copie di root di `ops/` del tag di prima; il pin del tag di
prima (`devloop --project <progetto> pin <tag di prima>`, che riusa la sua copia se c'è ancora,
[Rollback](../../docs/devloop/guide.md#rollback)); poi il passo 6, senza chiavi nuove (il
rollback delle sezioni le ha già tolte, prima del pin), e il passo 7 con `<tag di prima>` al posto
di `<tag nuovo>`, perché le sessioni tornino ai runbook del tag di prima.
