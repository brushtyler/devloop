# ops: ricreare l'installazione di devloop e del supervisore

Questa guida porta una VM vuota fino a devloop e al suo supervisore attivi, spiega come
**migrare** l'installazione su un'altra macchina e come **riusarla per un altro progetto**. Gli
script sono parametrici (`params.env`) e idempotenti; i file dichiarativi descrivono
l'installazione in uso dal 2026-11-18.

**Nessun segreto nel repository.** Chiavi private delle GitHub App, token e login delle CLI degli
agent restano fuori: la guida spiega come ricrearli, e nessuno script li legge, li scrive o li
stampa.

Il funzionamento di devloop è descritto in [docs/devloop/guide.md](../docs/devloop/guide.md)
(in particolare *Run from a pinned installation*) e in
[docs/devloop/reference.md](../docs/devloop/reference.md) (*GitHub identities*); ruoli e
responsabilità in [ADR 0002](../docs/adr/0002-ruoli-e-responsabilita.md).

## I quattro ruoli

L'installazione separa i privilegi sull'host dall'esposizione a testo non controllato (issue, PR,
output degli agent), con i [quattro ruoli dell'ADR 0002](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527).
Ogni ruolo agent ha un runbook, valido per qualunque CLI, con input, privilegi, compiti e
divieti:

| Ruolo | Utente sull'host | Privilegi | Runbook |
|---|---|---|---|
| **Operatore** | il suo | tutti: decisioni di persona, credenziali, login delle CLI, GitHub App; lancia l'agent dell'host | — |
| **Coordinatore** | `coordinator` (`COORDINATOR_USER`, passo 10; con `PROJECT`, `coordinator-<progetto>`) | la sua app; sull'host solo lettura (`sudoers-coordinator`) | [coordinator.md](../docs/devloop/roles/coordinator.md) |
| **Supervisore** | `supervisor` (`SUPERVISOR_USER`, passo 7; con `PROJECT`, `supervisor-<progetto>`) | la sua app; sull'host i comandi di `sudoers-supervisor` | [supervisor.md](../docs/devloop/roles/supervisor.md) |
| **Agent dell'host** | quello dell'operatore che lo lancia | `sudo` sull'host; nessuna scrittura su GitHub; legge solo procedure e script di `main`, mai issue, PR o output degli agent | [host.md](../docs/devloop/roles/host.md) |

devloop (dispatcher, preflight, implementer, reviewer) gira come `devloop` e `devloop-agent` e
non ha un runbook: le sue istruzioni sono i prompt di `devloop/prompts/`.

**Ruoli per progetto** ([ADR 0002, precisazione del
2026-11-24](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-24-ruoli-per-progetto-629)):
supervisore e coordinatore servono un solo progetto, anche quando l'host ne ha più di uno, e non
hanno nessun canale con i ruoli di altri progetti. Con `PROJECT` ciascuno ha la sua casella e la
sua sessione, e un utente per ruolo e per insieme di app (#643): i progetti dello stesso insieme
condividono l'utente del ruolo ([Più progetti sullo stesso host](#più-progetti-sullo-stesso-host)).

**Avvio di una sessione.** `ops/host/start-role.sh` avvia la sessione di un ruolo agent in tmux,
con l'utente del ruolo e il suo runbook come istruzioni iniziali:

```bash
sudo bash ops/host/start-role.sh --dry-run supervisor     # stampa il comando senza avviarlo
sudo bash ops/host/start-role.sh supervisor
sudo bash ops/host/start-role.sh coordinator
sudo bash ops/host/start-role.sh host --cli claude --model claude-opus-5-5
sudo -u supervisor -H tmux attach -t devloop-supervisor   # per entrare nella sessione
sudo -u supervisor -H tmux attach -t devloop-supervisor-devloop   # con PROJECT=devloop (#629)
sudo -u supervisor-widget -H tmux attach -t devloop-supervisor-widget   # con PROJECT=widget e gli utenti del #629
sudo -u supervisor-acme-devloop -H tmux attach -t devloop-supervisor-widget   # con l'insieme acme-devloop (#643)
```

- **Supervisore e coordinatore:** CLI e modello sono `kind` e `model` della tabella
  `[roles.supervisor]` o `[roles.coordinator]`, letta da `devloop/config.toml` del checkout con
  sopra l'overlay (`<state_dir>/config.local.toml`), come li compone devloop. Senza la tabella,
  o con `roles.supervisor` come chiave di un agent, lo script si ferma. La sessione gira con
  `SUPERVISOR_USER` nel suo clone (`SUPERVISOR_CLONE`) o con `COORDINATOR_USER` nel suo clone
  (`COORDINATOR_CLONE`, #643): la directory fidata della CLI, mai la home (#621). Il primo
  messaggio dà a entrambi la `key_dir` della loro app e il clone da cui coniano i token (con la
  sorgente a parte, quello della sorgente, con un `cd`).
- **Agent dell'host:** CLI e modello li sceglie l'operatore al lancio (`--cli`, `--model`). La
  sessione gira con l'utente dell'operatore che lancia lo script con `sudo` (`SUDO_USER`, quello
  che ha `sudo`), mai come root né come l'utente di un altro ruolo, nel checkout da cui parte lo
  script: va lanciato da un checkout di `main` aggiornato. Gli script di `ops/host/` per un
  progetto con `SOURCE_REF` li esegue invece dal checkout del tag del progetto ([Un altro
  progetto](#un-altro-progetto), #687).
- La CLI si cerca in `~/.local/bin` dell'utente del ruolo, poi in `/usr/local/bin`, `/usr/bin` e
  `/bin`, e parte interattiva con il modello e il runbook intero come primo messaggio:
  `claude --model <modello> <runbook>`, `codex --model <modello> <runbook>`,
  `agy --model <modello> -i <runbook>`. Il primo messaggio non passa dalla riga di comando di
  tmux, che rifiuta un comando oltre circa 16 KB (#621): lo script lo scrive in un file nuovo
  nella home dell'utente del ruolo (`~/.devloop-start-role-<ruolo>.XXXXXX`, suo, con modo 600) e
  la sessione lo legge, lo rimuove e lo passa alla CLI. Il limite di tmux non conta più; resta
  quello del kernel per un argomento (128 KiB con pagine di 4 KiB), l'unico modo in cui le CLI
  ricevono il primo messaggio di una sessione interattiva: oltre, lo script si ferma prima di
  avviare e lo dice.
- La sessione si chiama `devloop-<ruolo>-<progetto>` con `PROJECT`, `devloop-<ruolo>` senza
  (#629): le sessioni dello stesso ruolo di due progetti dell'host hanno nomi distinti, e
  ciascuna gira con l'utente del ruolo del suo progetto. Anche l'installazione con
  `PROJECT=devloop` prende il nome nuovo, `devloop-supervisor-devloop` e
  `devloop-coordinator-devloop`, senza un parametro per tenere quello di prima. Se la sessione
  esiste già, lo script si ferma e dice come entrarci; con `PROJECT` si ferma anche se l'utente
  del ruolo ha ancora la sessione con il nome di prima (`devloop-<ruolo>`), e dice come
  chiuderla. Serve `tmux` sull'host (`apt-get install tmux`).
- Per supervisore e coordinatore il primo messaggio nomina il repository e dice che il ruolo
  serve solo quel progetto, senza canali con i ruoli di altri progetti (#629).
- Per supervisore e coordinatore, prima del runbook, lo script passa i valori dell'installazione
  per il canale fra i due ruoli (lo `state_dir`, la casella e le sue destinazioni, la sveglia del
  coordinatore) e chiede di cominciare dalla sezione *At startup* del runbook: una sessione
  nuova, anche di un'altra CLI, li segue senza istruzioni dell'operatore. Al coordinatore passa
  anche i valori dei token della sua app (#565): la sua `key_dir` (`COORDINATOR_KEY_DIR`, per
  `DEVLOOP_APP_KEY_DIR`) e il clone da cui lancia `scripts/coordinator-token.py`, cioè
  `COORDINATOR_HOME/<nome del repository>` (`/home/coordinator/devloop` su questo host) o, con
  la sorgente a parte, `COORDINATOR_SOURCE_CLONE`. È anche la directory di lavoro della sessione:
  se il clone manca, lo script si ferma e dice il passo da fare (#621).

**Canale fra supervisore e coordinatore** (#543). I runbook descrivono come si parlano, come
sorveglia ciascuno e cosa fa all'avvio:

- **su GitHub**, per ciò che va tracciato: `agent:needs-operator` con un commento, o un'issue con
  le sue prove. Anche lo stato del lavoro (cosa è in corso, cosa aspetta l'operatore) sta su
  GitHub, nel diario del progetto (#76 per ssdlc) e nelle issue, mai in un file locale;
- **la casella**, per gli avvisi rapidi, sempre disponibile anche senza GitHub:
  `COORDINATION_DIR` (`/var/lib/devloop-coordination`; con `PROJECT`,
  `/var/lib/devloop-coordination-<progetto>`), con `to-coordinator/`, `to-supervisor/` e
  `to-operator/` (#663), ciascuna
  con `done/`, tutte `root:devloop-roles` (`COORDINATION_GROUP`; con `PROJECT`,
  `devloop-roles-<progetto>`) 2770; `COORDINATION_DIR` stessa è 3770, con lo sticky bit (#685).
  Nel gruppo ci sono solo il supervisore e il coordinatore del
  progetto, non `devloop` né `devloop-agent` né i ruoli di altri progetti. Un file per messaggio, con un
  nome univoco (data e ora UTC più un suffisso casuale), pubblicato con una rinomina atomica da
  un file temporaneo che comincia con `.`; il destinatario lo legge, poi lo sposta in `done/` o
  lo rimuove. La casella serve solo per gli avvisi, non per lo stato;
- **la cartella dell'operatore** (#663), `to-operator/` della casella: i ruoli ci lasciano le
  comunicazioni per l'operatore che non vanno su GitHub (domande, scelte con le opzioni,
  segnalazioni), ciascuna con il ruolo, il progetto e l'issue o la PR a cui si riferisce. Ci
  scrive il coordinatore; il supervisore solo quando scrive all'operatore direttamente (il
  coordinatore non ha letto un suo messaggio da 15 minuti, o un'emergenza di sicurezza). La legge
  solo l'operatore, dalla sua shell come root, che sposta i messaggi letti in `to-operator/done/`
  o li rimuove; nessun ruolo la legge, e il risveglio dei ruoli non la guarda. Su un host con i
  ruoli già installati la si aggiunge come in
  [host/migrate.md](host/migrate.md#cartella-to-operator-della-casella-663);
- **la sorveglianza**: il supervisore segue il flusso continuo, il coordinatore solo i suoi
  eventi e un controllo periodico, ciascuno con il risveglio dei ruoli `ops/host/role-wait.sh
  --role supervisor|coordinator` (#602; per il coordinatore anche `coordinator-wait.sh`, passo
  10), che a ogni avvio scrive il battito del ruolo in `heartbeat/` della casella. GitHub lo
  legge solo `devloop run`, che ne scrive gli eventi nel **flusso degli eventi** dello
  state_dir (#683, "Il flusso degli eventi" qui sotto); i risvegli lo leggono come utente del
  ruolo, con un'ACL di sola lettura, senza eseguire `devloop watch`;
- **il watchdog dei ruoli** (#602): il timer `devloop-role-watchdog@<progetto>`, ogni 5 minuti,
  controlla i battiti e dice nella casella e all'operatore quando un ruolo è fermo (passo 10,
  "Watchdog dei ruoli");
- **il riposo** (#684): a coda vuota un ruolo entra in riposo e non batte; lo sveglia la
  sentinella `devloop-role-sentinel@<progetto>`, e il loop non avvia lavoro nuovo finché un ruolo
  riposa (passo 10, "Il riposo dei ruoli");
- **la pausa** (#685): l'operatore ferma tutto, anche con issue e PR aperte, con `devloop
  --project <progetto> pause`, e lo fa ripartire con `resume`; i ruoli aspettano in pausa (passo
  10, "La pausa dell'operatore");
- **la manutenzione dell'host** (#705): un passo sull'host si apre con `devloop --project
  <progetto> maintenance start "<sezioni>"` e si chiude con `maintenance end`; nel frattempo
  `devloop run` non fa tick, `devloop pin` si rifiuta e i ruoli aspettano in manutenzione (passo
  10, "La manutenzione dell'host");
- **l'avvio**: ogni ruolo rilegge la sua casella, il diario (#76) e il riepilogo (#316), poi
  riprende a sorvegliare.

**Passi sull'host.** Il `host_step` di una PR nomina una sezione di
[host/migrate.md](host/migrate.md) o uno script di `ops/host/`, mai comandi liberi, e il reviewer
verifica che esista e descriva il passo
([docs/devloop/guide.md](../docs/devloop/guide.md#step-on-the-host)). Dopo il merge, l'operatore
lancia l'agent dell'host, che esegue solo quella sezione o quello script, dalla versione su
`main`.

## Contenuto

```
ops/
  README.md                  questa guida
  params.env.example         i parametri: copiarlo in ops/params.env (locale: git lo ignora)
  lib.sh                     funzioni comuni agli script (parametri, --dry-run, conferme)
  host/
    setup-devloop.sh         pacchetti, utente devloop, clone, state_dir e log, overlay d'esempio,
                             key_dir e ~/.claude, ~/.codex 700, impostazioni di Claude Code, unità
    setup-agent-user.sh      utente degli agent (devloop-agent): home, file e binari delle CLI
                             di root, ACL su state_dir, worktree e sessioni, sudoers dell'avvio
                             degli agent; --unseal per installare e aggiornare le CLI
    setup-supervisor.sh      utente supervisor, ACL di attraversamento su /home/<devloop> e di
                             lettura su overlay, file di progetto e flusso degli eventi, casella
                             fra i ruoli, sudoers, clone, key_dir 700, impostazioni di Claude Code
    setup-coordinator.sh     utente coordinator (facoltativo), ACL di attraversamento su
                             /home/<devloop> e di lettura su overlay e flusso degli eventi,
                             casella fra i ruoli,
                             clone, key_dir 700, copia di root di check-host.sh e della sveglia,
                             sudoers in sola lettura, impostazioni di Claude Code
    setup-project.sh         un progetto con un solo comando: i quattro setup-*.sh nell'ordine,
                             prima tutti con --dry-run, poi con una sola conferma, e i passi a
                             mano rimasti in un solo blocco (#643)
    role-wait.sh             il risveglio di supervisore e coordinatore (--role): esce sui loro
                             eventi del flusso degli eventi, su un messaggio nella casella o
                             alla fine del periodo, e a ogni avvio scrive il battito del ruolo;
                             con --rest il riposo del ruolo, fino al segnale della sentinella
    coordinator-wait.sh      involucro compatibile: role-wait.sh --role coordinator
    role-watchdog.sh         il watchdog dei ruoli: dai battiti e dal riposo, i ruoli fermi o
                             in anomalia nella casella e all'operatore
    devloop-role-watchdog@.service, devloop-role-watchdog@.timer,
    devloop-role-watchdog-failed@.service  unità template del watchdog, una istanza per
                             progetto, ogni 5 minuti; -failed@ è il suo OnFailure=
    role-watchdog-project.conf  drop-in di un progetto per le istanze del watchdog (segnaposto)
    role-sentinel.sh         la sentinella dei ruoli a riposo: sveglia solo il ruolo interessato
                             e, una volta al giorno, i ruoli a riposo (#684)
    devloop-role-sentinel@.service, devloop-role-sentinel-failed@.service  unità template
                             della sentinella, una istanza per progetto; -failed@ avvisa
                             l'operatore con devloop notify
    role-sentinel-project.conf  drop-in di un progetto per l'istanza della sentinella
    devloop.service          unità systemd (con segnaposto @NOME@)
    devloop@.service         unità template, per più progetti sullo stesso host (PROJECT)
    devloop-project.conf     drop-in di un progetto per devloop@<progetto> (con segnaposto)
    devloop-failed@.service  unità template dell'avviso all'operatore di un servizio caduto, l'
                             OnFailure= delle due unità del servizio: `devloop notify
                             --service-failed <progetto>` con il comando devloop (#593)
    tmpfiles-devloop-repos.conf  directory dei lock per repository, comune all'host
    devloop-command          il comando devloop dell'host: il launcher di un progetto come la
                             sua unità, scelto con --project (con segnaposto); pause e resume,
                             la pausa dell'operatore (#685); maintenance start e end, la
                             manutenzione dell'host (#705)
    devloop-command-project  il file di un progetto per il comando devloop (con segnaposto)
    sudoers-agent            sudoers con cui devloop avvia CLI e test come utente degli agent
    sudoers-supervisor       sudoers minimale del supervisore (con segnaposto @NOME@)
    sudoers-coordinator      sudoers del coordinatore, comandi di lettura e l'avviso
                             all'operatore, devloop-pinned notify (con segnaposto)
    config.local.toml.example  overlay di devloop, tutto commentato
    check-host.sh            verifica utenti, permessi (700/600), chiavi, ACL, sudoers, casella,
                             utente degli agent, coordinatore, servizio, CLI e `devloop apps`;
                             --read-only per il coordinatore
    start-role.sh            avvia in tmux la sessione di supervisore, coordinatore o agent
                             dell'host, con il suo utente, CLI, modello e runbook
    migrate.md               cosa copiare dalla macchina vecchia e cosa ricreare, e la
                             configurazione di ssdlc nel suo file di progetto (ADR 0006, R3)
    ssdlc/project.toml       il file di progetto di ssdlc, d'esempio: l'operatore lo copia in
                             <state_dir>/project.toml
    ssdlc/overlay.toml       le chiavi che l'overlay di ssdlc riceve con R3 (comandi di agy)
    ssdlc/check-equivalence.py  confronta la configurazione prima e dopo R3 su un host
  github/
    apps/<ruolo>.json        manifest delle GitHub App (flusso "manifest" di GitHub)
    create-apps.html         pagina statica che compone i manifest nel browser, dal prefisso
                             dell'insieme, dal repository e dal tipo di proprietario (#643)
    create-apps.sh           genera dai parametri una pagina HTML locale con gli stessi pulsanti
    ruleset-main.json        ruleset del branch principale
    tokens.md                nessun token personale: i ruoli usano le loro app (#643)
```

### Regole degli script

- **Parametri.** Leggono `ops/params.env` (o il file di `--params`). I valori finiscono in un
  sudoers e in un'unità systemd, quindi gli script accettano solo forme note (nomi utente, percorsi
  assoluti senza spazi, `owner/nome`) e si fermano su un valore diverso. `STATE_DIR` è
  `.devloop` del clone per default (dal #563; prima `.ssdlc/devloop`, dentro `.ssdlc/`, la
  directory di stato del vecchio motore SSDLC, che devloop non legge né scrive: un clone che ha
  ancora solo quella ferma il launcher, e si sposta con
  [host/migrate.md](host/migrate.md#percorsi-devloop-e-istanza-devloopdevloop-563)); può essere un altro percorso relativo al clone o un
  percorso assoluto dell'host (per esempio `/var/lib/devloop/<progetto>`). Con un valore diverso
  dal default, o con `SOURCE_REPO`, unità, sudoers e verifiche chiamano il launcher con
  `--state-dir <percorso>` e con `env -C <clone>`, perché giri nel clone gestito, e l'overlay deve
  avere lo stesso `state_dir` ([docs/devloop/guide.md](../docs/devloop/guide.md#launcher)).
  Con `PROJECT` i parametri sono di uno dei progetti dell'host ([Più progetti sullo stesso
  host](#più-progetti-sullo-stesso-host)).
- **`--dry-run`.** Stampa ogni azione (`[dry-run] …`) e il contenuto dei file che scriverebbe,
  senza fare nulla. Le verifiche in sola lettura (un utente esiste, una directory ha già il modo
  giusto) si fanno lo stesso, così l'elenco mostra solo ciò che manca.
- **Idempotenti.** Ogni passo controlla lo stato prima di agire: un utente, un clone, un overlay o
  delle impostazioni già presenti non si toccano; directory e file con modo o contenuto diversi si
  correggono. Rieseguirli non cambia un'installazione già a posto.
- **Avvio.** Si avviano con `bash` (`sudo bash ops/host/setup-devloop.sh`), come il launcher di
  devloop con `python3`: non serve il bit di esecuzione.
- **Root.** `setup-*.sh` vanno eseguiti come root (`sudo`), tranne con `--dry-run`.
  `check-host.sh` non cambia nulla, ma senza root non legge i file degli altri utenti.
- **Piattaforma.** Controllano `/etc/os-release` e l'architettura: su una piattaforma diversa da
  quella verificata avvisano e chiedono conferma (`--yes` per non chiederla), non rifiutano.
- **Verifica.** Tutti superano `bash -n`; un test li esegue con `--dry-run`
  (`tests/test_ops.py`). Prima di cambiarli si raccomanda anche
  [`shellcheck`](https://www.shellcheck.net/) (`shellcheck ops/lib.sh ops/host/*.sh ops/github/*.sh`),
  che non fa parte dei test perché non è disponibile offline.

## Piattaforma verificata

**Ubuntu 26.04 LTS su aarch64**, con `apt`, `systemd` e il Python 3 di sistema (3.14). Su altre
distribuzioni con `apt` e `systemd` gli script dovrebbero funzionare, ma non sono stati provati.

**`sudo` deve essere sudo 1.9, non sudo-rs** (#496). Ubuntu usa per default sudo-rs, che non
supporta `sudo -E`: stampa `sudo: preserving the entire environment is not supported, '-E' is
ignored` e avvia il comando con l'ambiente minimo, anche con il tag `SETENV` nel sudoers. Gli
agent girerebbero allora senza l'ambiente isolato che devloop prepara. La correzione, come root:

```bash
update-alternatives --set sudo /usr/bin/sudo.ws
sudo --version | head -n 1     # "Sudo version 1.9…", non "sudo-rs …"
```

devloop non parte con `[agent_user]` se l'ambiente non arriva all'utente degli agent, e
`check-host.sh` lo segnala come errore ([5. Utente degli agent e
CLI](#5-utente-degli-agent-e-cli)).

## Da una VM vuota a devloop e supervisore attivi

I comandi si eseguono da un clone di devloop (`brushtyler/devloop`) dell'operatore (per esempio
nella sua home), come utente con `sudo`. Dove compare `brushtyler/devloop`, `devloop` o
`supervisor`, valgono i valori di `params.env`. I percorsi `/home/devloop/devloop` e
`/home/supervisor/devloop` sono quelli dell'host che sviluppa questo repository dal #563, che vi
ha spostato i clone e lo `state_dir` e ha portato l'host all'istanza `devloop@devloop`
(`PROJECT=devloop` con gli utenti di prima, [host/migrate.md](host/migrate.md#percorsi-devloop-e-istanza-devloopdevloop-563)).
Gli esempi di questa guida restano quelli di un'installazione senza `PROJECT`
(`devloop.service`): su quell'host il servizio è `devloop@devloop`, e sudoers, casella e copia di
root del coordinatore hanno il suffisso `-devloop`.

**Il vecchio motore SSDLC.** Il tag `ssdlc-engine-final` e le directory `.ssdlc/` del vecchio
motore negli altri repository dell'operatore non c'entrano con questi script né con devloop:
nessuno script e nessun passo li tocca, e devloop non legge né scrive `.ssdlc/` (#563).

**Un solo script per gli script dell'host** (#643). I passi 4, 5, 7 e 10 eseguono ciascuno il suo
`setup-*.sh`, prima con `--dry-run`. `setup-project.sh` li esegue tutti nell'ordine giusto
(`setup-devloop.sh`, `setup-agent-user.sh`, `setup-supervisor.sh` e, se il progetto ha il
coordinatore, `setup-coordinator.sh`), prima tutti con `--dry-run`, poi, con una sola conferma,
le esecuzioni vere, e alla fine elenca in un solo blocco i passi a mano rimasti (chiavi, login
delle CLI, app, verifica, avvio):

```bash
sudo bash ops/host/setup-project.sh --params ops/params.env --dry-run   # solo il primo giro
sudo bash ops/host/setup-project.sh --params ops/params.env             # con il coordinatore: --coordinator
```

Il coordinatore entra se l'overlay lo dichiara (`[apps.coordinator]` o `[roles.coordinator]`) o
con `--coordinator`. Un `--dry-run` che fallisce ferma tutto prima di cambiare qualcosa; un errore
di un'esecuzione vera ferma gli script dopo. Gli script singoli restano, e fanno lo stesso. Dopo
una modifica dei parametri (per esempio `APPS`), che rende diverse le copie di root dei parametri
dei ruoli, basta rieseguire `setup-project.sh`. La [guida rapida](../docs/devloop/quickstart.md)
usa questo script.

### 1. Parametri

```bash
cp ops/params.env.example ops/params.env
$EDITOR ops/params.env               # REPO, APP_PREFIX, utenti e percorsi
```

GitHub accetta per una GitHub App un nome di al più 34 caratteri: `<APP_PREFIX><ruolo>` non li
supera per nessuna app di `APPS`. Con le app di default il ruolo più lungo è `claude-agent` (12
caratteri), quindi `APP_PREFIX` ne ha al più 22; `create-apps.sh`, anche con `--dry-run`, si ferma
prima di scrivere la pagina e nomina le app troppo lunghe (#573).

### 2. GitHub App

Ogni app ha un manifest in `ops/github/apps/`. Per tutte: `metadata: read`, webhook spento,
nessun evento, nessuna autorizzazione OAuth, app privata (installabile solo dall'account che la
possiede).

| App (`<APP_PREFIX><ruolo>`) | Permessi | Note |
|---|---|---|
| `dispatcher` | actions: write, checks: read, contents: read, issues: write, pull_requests: write, statuses: read | `actions: write` per rilanciare i job CI falliti non pertinenti (#200) |
| `merger` | contents: write, pull_requests: write | solo i merge (#201) |
| `claude-agent`, `codex-agent`, `agy-agent` | contents: write, issues: write, pull_requests: write, actions/checks/statuses: read | con l'ADR 0005, quando l'implementer è un'app a sé, i reviewer scendono a contents: read |
| `implementer` | contents: write, issues: write, pull_requests: write | **mai `workflows`**: senza quel permesso GitHub rifiuta i push che cambiano `.github/workflows/` |
| `supervisor` | issues: write, pull_requests: write, actions/checks/statuses: read | **nessun contents**; anche le letture passano dall'app, senza token personale (#643) |
| `coordinator` | issues: write, pull_requests: write, actions/checks/statuses: read | **nessun contents**; le letture per la diagnosi della CI; solo se c'è il coordinatore (passo 10) |

Nessuna app ha `workflows` o permessi di amministrazione. I permessi che devloop chiede per ogni
azione sono in `devloop/apps.py` (`IMPLEMENT`, `REVIEW`, `PR_COMMENT`, `ISSUE_COMMENT`, `MERGE`,
`RERUN`); un test (`tests/test_ops.py`) controlla che i manifest li coprano.

**La pagina delle app** (#643) è [`github/create-apps.html`](github/create-apps.html): una pagina
statica del repository, che si apre nel browser del computer dell'operatore (scaricata dal
repository o da un suo clone), con la sessione GitHub del proprietario del repository. Compone
nel browser i manifest di `ops/github/apps/`, che contiene, da tre campi: il prefisso
dell'insieme, il repository di ritorno (il repository gestito) e il tipo di proprietario (`user`
o `org`). Mostra i valori da scrivere nei parametri (`APP_PREFIX`, `APPS`, `GITHUB_OWNER_TYPE`, e
il nome dell'insieme da cui vengono gli utenti dei ruoli), così che un errore di battitura non
crei app con nomi che `check-host.sh` e l'overlay non trovano. Per un insieme che esiste già si
segnano a mano le app che ha: la pagina crea solo le altre e le elenca in `APPS`. Non chiama
GitHub prima del clic su un pulsante, non legge credenziali e non usa altri file. Lo scambio del
`code` resta sull'host (punto 2): la risposta contiene la chiave privata, che va direttamente
nella `key_dir`.

`create-apps.sh` resta, per chi preferisce generare la pagina dai parametri sull'host:

```bash
bash ops/github/create-apps.sh --dry-run            # le app e i loro permessi
bash ops/github/create-apps.sh --out ~/create-apps.html
```

Le due pagine hanno un pulsante per app; vanno aperte nel browser con la sessione GitHub del
proprietario del repository (`GITHUB_OWNER_TYPE=org` per un'organizzazione). Per ciascuna app:

1. **Crea**: GitHub mostra la pagina di creazione già compilata dal manifest; confermare. Se il
   nome è già usato, l'app esiste già: saltarla. Dopo la creazione GitHub torna alla pagina del
   repository (il `redirect_url` del manifest) con un parametro `code` nell'indirizzo:
   `https://github.com/<repo>?code=<code>`.
2. **Completa lo scambio del `code`**, entro un'ora: il flusso manifest finisce solo con
   `POST /app-manifests/<code>/conversions`
   ([documentazione GitHub](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest#3-you-exchange-the-temporary-code-to-retrieve-the-app-configuration)).
   Il `code` vale una volta sola. La risposta contiene **segreti** (`pem`, la chiave privata, e i
   segreti OAuth del client e del webhook): va scritta solo in una directory privata, mai stampata,
   mai copiata in issue, PR, log o nel repository. Dalla risposta servono soltanto `id`, `slug` e
   `pem`; il resto si cancella. Come utente con `sudo`, sulla macchina dove andrà la chiave:

   ```bash
   CODE='<code>'                                   # dall'indirizzo del passo 1
   work=$(mktemp -d)                               # mode 700
   ( umask 077
     curl -fsS -X POST -H 'Accept: application/vnd.github+json' \
         -o "$work/app.json" "https://api.github.com/app-manifests/$CODE/conversions"
     python3 - "$work" <<'EOF'
   import datetime, json, os, sys
   work = sys.argv[1]
   app = json.load(open(os.path.join(work, 'app.json')))
   name = f"{app['slug']}.{datetime.date.today()}.private-key.pem"
   with open(os.path.join(work, name), 'x') as f:
       f.write(app['pem'])
   print(f"app_id = {app['id']}  slug = {app['slug']}  chiave: {name}")
   EOF
   )
   ```

   Lo `slug` stampato deve essere `<APP_PREFIX><ruolo>`. La chiave
   `$work/<slug>.<data>.private-key.pem` va nella `key_dir` dell'utente che usa l'app (sezioni 4 e
   7 di questa guida), mode 600, per esempio
   `sudo install -o devloop -g devloop -m 600 "$work"/*.private-key.pem /home/devloop/.ghapps/`;
   poi `rm -rf "$work"`, che cancella anche `app.json` con gli altri segreti. L'endpoint non chiede
   autenticazione: non serve nessun token. Se il `code` è scaduto o già usato (risposta 404 o 422)
   e l'app risulta creata, la chiave si genera dalle sue impostazioni (punto 4).
3. **Installa**, solo dopo lo scambio: *Install App* → *Only select repositories* → solo il
   repository gestito.
4. **Chiave privata**: quella del punto 2. Solo se manca (scambio non riuscito, chiave persa o da
   ruotare): *Generate a private key* nelle impostazioni dell'app; il file scaricato,
   `<slug>.<data>.private-key.pem`, va nella `key_dir` dell'utente che usa l'app (sezioni 4 e 7),
   mode 600, e poi tolto dai Download. Le chiavi che non servono più si revocano lì.
5. **Configurazione**: `app_id` e `slug` (non segreti), con la `key_dir`, in `[apps]`
   dell'overlay locale dello `state_dir` del progetto (`config.local.toml`; per ssdlc dalle chiavi
   di [`ssdlc/overlay.toml`](host/ssdlc/overlay.toml)), non in `devloop/config.toml`, che da
   ADR 0006, R6 non ha identità di progetto (sezione 4). L'app `supervisor` è in
   `[apps.supervisor]` (ADR 0005, P5-4): devloop non ne legge la
   chiave, che sta solo nella `key_dir` del supervisore. L'app `coordinator` è in
   `[apps.coordinator]` (#528): devloop non ne legge la chiave, che sta solo nella `key_dir` del
   coordinatore (passo 10). L'app `implementer` è in `[apps.implementer]` (ADR 0005, P5-3): la
   sua chiave sta nella `key_dir` di `devloop` (passo 4), che la legge quando una chiave
   `<agent>-implementer` di `[agents]` la dichiara.

**I permessi di un'app che esiste già** (#655): il manifest fa fede. Un'app creata a mano prima
dei manifest (#402), o da una loro versione più vecchia, può avere permessi che devloop e gli
script non usano; chi ha la sua chiave può però coniare un token con tutti i permessi
dell'installazione, senza passare da devloop né da `scripts/supervisor-token.py` o
`scripts/coordinator-token.py`: i limiti degli script non bastano, conta quello che l'app
possiede. Per ogni app dell'insieme creata così (coordinatore, implementer, merger, dispatcher,
app degli agent, supervisore) si confrontano i permessi con `default_permissions` del suo
manifest in `ops/github/apps/`:

- **dove leggerli**: nelle impostazioni dell'app su GitHub (*Permissions & events* →
  *Repository permissions*, e nessun permesso di account o di organizzazione). Per le app di cui
  devloop legge la chiave (`dispatcher`, `merger`, le app degli agent, `implementer`) anche
  `python3 -m devloop apps`, che `check-host.sh` esegue, stampa i permessi dell'installazione;
  per `supervisor` e `coordinator`, di cui devloop non legge la chiave, valgono solo le
  impostazioni dell'app;
- **un permesso in più** (che il manifest non ha, o in scrittura dove il manifest lo vuole in
  lettura) si toglie (*No access*) o si abbassa lì, poi *Save changes*: togliere o ridurre un
  permesso non chiede di riaccettare l'installazione;
- **un permesso che manca** si aggiunge allo stesso modo, ma va poi accettato nell'installazione
  del proprietario (*Install App* → l'installazione → *Review request*): finché non è accettato,
  un token che lo chiede fallisce.

Se una regola del branch principale limita chi può fare push o merge, aggiungere `merger` fra gli
ammessi.

### 3. Ruleset del branch principale

`ops/github/ruleset-main.json` è il ruleset attuale: niente cancellazione né force push; PR
obbligatoria, con merge `merge`, `squash` o `rebase`; check obbligatorio `Unit Tests (Python 3.12)`
di GitHub Actions (`integration_id` 15368); bypass solo per il ruolo admin (`actor_id` 5); si
applica al branch di default (`~DEFAULT_BRANCH`). Il check `Isolated A5 Recovery Benchmark`, il
replay gate del motore SSDLC, è uscito con il motore (ADR 0007, R1).

Lo applica l'operatore, con il proprio login `gh` di amministratore (non con il token del
supervisore). Per non crearne un doppione, si aggiorna quello che c'è:

```bash
REPO=brushtyler/devloop
id=$(gh api "repos/$REPO/rulesets" --jq '.[] | select(.name == "main") | .id')
if [ -z "$id" ]; then
    gh api -X POST "repos/$REPO/rulesets" --input ops/github/ruleset-main.json
else
    gh api -X PUT "repos/$REPO/rulesets/$id" --input ops/github/ruleset-main.json
fi
```

**Raccomandato:** chiedere una nuova approvazione dopo un push, così un commit aggiunto dopo
l'approvazione non entra senza una review. Il JSON non ammette commenti, quindi l'opzione è qui:
in `ruleset-main.json`, nella regola `pull_request`, impostare

```json
"required_approving_review_count": 1,
"dismiss_stale_reviews_on_push": true,
"require_last_push_approval": true
```

`dismiss_stale_reviews_on_push` e `require_last_push_approval` contano solo con almeno
un'approvazione obbligatoria. Prima di attivarle verificare che le approvazioni che servono
(quelle dei reviewer di devloop o dell'operatore) arrivino da identità che il ruleset conta.

Per un altro progetto: i nomi dei check obbligatori sono quelli dei job della sua CI.

### 4. Host di devloop

```bash
sudo bash ops/host/setup-devloop.sh --dry-run    # cosa farebbe
sudo bash ops/host/setup-devloop.sh
```

Installa i pacchetti che mancano (`python3`, `git`, `gh`, `openssl`, `acl`, `nodejs`, `npm`, …),
crea l'utente `devloop` e il suo clone, `state_dir` (750) con `run.log`, l'overlay d'esempio (600,
solo se non esiste), `~/.ghapps`, `~/.claude` e `~/.codex` con mode 700, `~/.claude/settings.json` con gli
aggiornamenti automatici spenti (solo se non esiste), e l'unità `devloop.service`. Non avvia il
servizio. Le CLI degli agent non sono di `devloop`: le prepara il passo 5.

Lo `state_dir` 750 e l'overlay 600 (#573) non sono leggibili dagli altri utenti dell'host
(l'overlay nomina `trusted_users`, `app_id` e `key_dir`): i ruoli che li usano li raggiungono con le
loro ACL, l'utente degli agent dal passo 5, il supervisore dal passo 7 e il coordinatore dal passo
10. Lo script non cambia uno `state_dir` o un overlay che esistono già; `check-host.sh` avvisa se
l'overlay è leggibile da tutti.

Prepara anche il **lock per repository** (ADR 0006, R8): il gruppo `REPO_LOCK_GROUP`
(`devloop-repos`), con dentro l'utente `devloop`, e la directory comune `REPO_LOCK_DIR`
(`/run/devloop/repos`, di root, mode 3770), che `systemd-tmpfiles` ricrea a ogni avvio da
`TMPFILES_FILE` (`/etc/tmpfiles.d/devloop-repos.conf`). Non usa una directory che esiste già, non
è vuota e non è di `root:<gruppo>`. L'overlay deve avere `repo_lock_dir` con lo stesso valore: lo
script lo ricorda finché manca ([Più progetti sullo stesso host](#più-progetti-sullo-stesso-host)).

Installa infine il **comando `devloop`** dell'host (#588): l'involucro del launcher
[`host/devloop-command`](host/devloop-command), in `DEVLOOP_COMMAND` (`/usr/local/bin/devloop`,
755 di root), e il file del progetto `params-<progetto>.env` (`params-_default.env` senza
`PROJECT`; 644 di root) nella directory di root `DEVLOOP_PROJECTS_DIR` (`/etc/devloop/projects`),
con l'utente, il clone, il `PATH` e il comando del launcher dell'unità
([`host/devloop-command-project`](host/devloop-command-project)); entrambi si riscrivono a ogni
esecuzione. `devloop <sottocomando> [argomenti]` esegue il launcher del progetto come la sua
unità: come l'utente `devloop` (`sudo -u devloop -H`), nel clone, con la copia fissata del servizio e
lo `--state-dir` del progetto, e gli passa sottocomando e argomenti così come sono:

```bash
devloop status
devloop pin origin/main
devloop follow issue-<N>
devloop --project widget status     # con più progetti sull'host
```

Gira come root: un altro utente lo riesegue con `sudo`. Senza `--project` usa il solo progetto
installato; con più progetti si ferma e li elenca. Legge i file dei progetti riga per riga, senza
eseguirli, e rifiuta un file o una directory che non sono di root o che altri possono scrivere.
Per togliere un progetto dal comando si rimuove il suo file da `DEVLOOP_PROJECTS_DIR`. Non
sostituisce i sudoers di supervisore e coordinatore, che usano i comandi esatti del launcher (passi
7 e 10). Il comando ha solo tre sottocomandi suoi: `pause` e `resume`, la pausa dell'operatore
(#685, passo 10, "La pausa dell'operatore"), e `maintenance start "<sezioni>"` e `maintenance end`,
la manutenzione dell'host (#705, passo 10, "La manutenzione dell'host"), per cui il file del
progetto ha anche il nome del servizio (`SERVICE_NAME`), la casella fra i ruoli
(`COORDINATION_DIR`), la durata della manutenzione (`MAINTENANCE_TIMEOUT`) e l'attesa delle
conferme dei ruoli (`MAINTENANCE_ACK_TIMEOUT`); ogni altro sottocomando è di devloop. Durante la
manutenzione il suo `pin` passa a devloop l'inizio della manutenzione: è il pin del passo
sull'host, l'unico che devloop accetta finché la manutenzione c'è.

Per ssdlc servono anche il **file di progetto** e le chiavi che l'overlay riceve con l'ADR 0006,
R3 e R6: lo script non li crea. L'operatore copia [`ssdlc/project.toml`](host/ssdlc/project.toml) in
`<state_dir>/project.toml` (mode 600, utente `devloop`) e aggiunge all'overlay le chiavi di
[`ssdlc/overlay.toml`](host/ssdlc/overlay.toml): i comandi di agy e, da R6, `repo`,
`trusted_users` e l'insieme di app di ssdlc (`[apps]`), che `devloop/config.toml` non ha più.
Senza `repo` e `trusted_users` nell'overlay devloop non parte; senza file di progetto devloop non parte e il
primo pin fallisce ([migrate.md](host/migrate.md#configurazione-di-ssdlc-nel-suo-file-di-progetto)).
Su un host già in servizio l'ordine conta: vedi [Migrazione di R3 su un host
attivo](host/migrate.md#migrazione-di-r3-su-un-host-attivo).

Poi le **chiavi**: in `key_dir` di `devloop` (`/home/devloop/.ghapps`) quelle delle app che usa il
dispatcher (`DEVLOOP_KEYS`: `dispatcher`, `merger`, `claude-agent`, `codex-agent`, `agy-agent`,
`implementer`). Con il default di `DEVLOOP_KEYS`, la chiave di `implementer` è richiesta da
`setup-devloop.sh` e `check-host.sh` solo se l'overlay del progetto dichiara `[apps.implementer]`;
senza quella tabella è soltanto riconosciuta, e la sua assenza non è un errore (#569). Un
`DEVLOOP_KEYS` scritto in `params.env` è autorevole: ogni chiave che elenca è richiesta. Ogni
chiave si installa così:

```bash
sudo install -o devloop -g devloop -m 600 <slug>.<data>.private-key.pem /home/devloop/.ghapps/
```

La `key_dir` di ogni ruolo può avere un nome non di default (`DEVLOOP_KEY_DIR`,
`SUPERVISOR_KEY_DIR`, `COORDINATOR_KEY_DIR` di `params.env` e, per devloop, `key_dir` in `[apps]`
dell'overlay); per spostarla su un host in servizio, per esempio dopo che la sua posizione è
finita in un testo pubblico, vedi [host/migrate.md](host/migrate.md#spostare-una-key_dir-582).

### 5. Utente degli agent e CLI

Gli agent e i test non girano come `devloop`, che può leggere le chiavi delle app e lo
`state_dir`, ma come un utente proprio, `devloop-agent` (`AGENT_USER`, #415): il dispatcher li
avvia con `sudo -n -E -u devloop-agent`
([docs/devloop/guide.md](../docs/devloop/guide.md#agent-user)).

```bash
sudo bash ops/host/setup-agent-user.sh --dry-run
sudo bash ops/host/setup-agent-user.sh
```

Crea l'utente `devloop-agent` e rende di root ciò che i turni successivi usano (#438): con un
solo utente per implementer e reviewer, un turno non deve poter cambiare le istruzioni, la
configurazione o i binari delle CLI per i turni dopo, per esempio aggiungendo a
`~/.claude/CLAUDE.md` un'istruzione che fa approvare.

- la home di `devloop-agent` è di root, `1770`, del gruppo di `devloop-agent`, con lo sticky bit:
  `devloop-agent` vi crea le voci di stato e di cache delle CLI (per esempio `~/.claude.json`), ma
  non rinomina né rimuove quelle di root;
- `~/.claude`, `~/.codex`, `~/.gemini`, `~/.gemini/config` e `~/.gemini/antigravity-cli` sono
  come la home (#517):
  di root, del gruppo di `devloop-agent`, `1770`. Le CLI vi salvano i loro login con un file
  temporaneo e un `rename` (`.credentials.json` di claude, `auth.json` di codex con `auth.lock`),
  e vi creano lock, cache e temporanei; `devloop-agent` non vi rinomina né rimuove ciò che è di
  root, né le directory stesse. `~/.local` e `~/.local/bin` sono di root, del gruppo di
  `devloop-agent`, `755`, come vuole la sigillatura qui sotto (#680);
- ciò che porta istruzioni, configurazione, plugin o binari (`AGENT_CLI_SEALED` di `ops/lib.sh`,
  come in `devloop/agentuser.py`: `~/.claude/settings.json`, scritto senza aggiornamenti
  automatici, `~/.claude/CLAUDE.md`, `rules`, `agents`, `commands`, `skills`, `hooks`,
  `output-styles` e `plugins` di `~/.claude`, `~/.codex/config.toml`, `AGENTS.md`,
  `AGENTS.override.md`, `prompts`, `rules`, `skills`, `packages`, `plugins` e `memories` di
  `~/.codex`, `~/.gemini/settings.json`, `~/.gemini/GEMINI.md`, `config.json` (i plugin che agy
  abilita), `mcp_config.json` (i server MCP che agy carica) e `projects` di `~/.gemini/config`,
  `~/.gemini/antigravity-cli/settings.json` e tutto `~/.local`) diventa di root, del gruppo di
  `devloop-agent`, leggibile e attraversabile dal gruppo e senza scrittura per gruppo e altri
  (`chmod -R g+rX,go-w`): un `settings.json` 600 o una directory 700 restano leggibili da
  `devloop-agent`, che non li scrive e non ne cambia il modo. Ognuna che non esiste diventa un
  segnaposto di root (#517: un file vuoto se il nome ha un'estensione, `{}` per un `.json`,
  altrimenti una directory vuota), che `devloop-agent` non può creare né rinominare o rimuovere
  nelle directory con lo sticky bit. Un link che porta
  fuori da ciò che è sigillato (anche un eseguibile di `~/.local/bin` che porta fuori da
  `~/.local` e da `~/.codex/packages`), o una directory delle CLI che è un link, è segnalato da
  rendere di root a mano: `chown -h` cambia il link, non il file a cui porta;
- restano di `devloop-agent` le altre voci che le CLI creano nelle loro directory: credenziali,
  sessioni (`~/.claude/projects`, `~/.codex/sessions`, che lo script crea), cache, database e
  log. Le directory possono contenere solo le voci della lista del codice, con il loro tipo
  (#517: `AGENT_CLI_ALLOWED` di `ops/lib.sh`, come `CLI_ALLOWED` di `devloop/agentuser.py`; un
  link solo per `cli.log` di agy, e solo verso `log/<nome>` accanto a lui, un file regolare e
  non un altro link, #525:
  `AGENT_CLI_LINKS`, come `CLI_LINKS`), oltre a quelle sigillate e alle directory delle CLI, di root: devloop lo verifica all'avvio e
  prima di ogni turno, come `devloop-agent`, e una voce sconosciuta rifiuta il turno con
  un'escalation `configuration` che la nomina; `setup-agent-user.sh` e `check-host.sh` la
  segnalano. Una voce nuova di una CLI si aggiunge alla lista con una PR
  ([host/migrate.md](host/migrate.md#una-voce-sconosciuta-in-una-directory-delle-cli));
  `[agent_user] cli_denied` nell'overlay può solo togliere voci dalla lista. Le voci che una CLI
  crea al primo uso (`AGENT_CLI_WRITABLE` di `ops/lib.sh`, come `CLI_WRITABLE` di
  `devloop/agentuser.py`: `~/.gemini/antigravity-cli/bin`, dove agy scrive il wrapper del suo
  strumento di shell) le crea lo script, di `devloop-agent`, `700`; prima di ogni turno di agy
  devloop rifiuta il turno se `bin` contiene altro che il wrapper che agy vi scrive (#494). Ciò
  che vi resta scrivibile non porta istruzioni ai turni dopo: prima di ogni turno di agy, che
  non ha un interruttore noto, devloop rimuove le memorie che rileggerebbe in una sessione
  successiva (`brain`, `implicit`, `builtin`, `conversation_summaries.db*` e
  `jetbox_summaries_proto.pb*`), e i turni di agy girano uno alla volta anche con le azioni in
  parallelo, perché nessuno legga ciò che un altro vi scrive nel frattempo. Le memorie di codex
  (`memories_1.sqlite*`) sono invece spente: lo script scrive nel `~/.codex/config.toml`
  sigillato `[features] memories = false` in modo esplicito, così un cambio del default di
  codex non le riaccende (aggiunge la riga che manca, nella tabella `[features]` o in una nuova
  in fondo, e segnala da correggere a mano un valore diverso o un `config.toml` che è un link);
  devloop all'avvio, leggendolo come `devloop-agent`, e `check-host.sh` rifiutano un
  `config.toml` senza la riga, con `memories = true` o con un profilo
  (`[profiles.<nome>.features]`) che le riaccende. I turni di codex non svuotano nulla e girano
  in parallelo; `memories_1.sqlite*` resta fra le voci ammesse e `~/.codex/memories` è
  sigillata; devloop avvia ogni turno di Claude Code senza la memoria automatica di
  `~/.claude/projects` (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` nell'ambiente e, perché i settings
  del progetto nel worktree non la riaccendano, anche in `--settings`) e senza i server MCP di
  `~/.claude.json` (`--strict-mcp-config`). Un `--settings` nei `write_args` o nei `review_args`
  di un agent claude nell'overlay ferma devloop all'avvio.

**Installare, aggiornare, fare un login.** A loop fermo, `--unseal` rende a `devloop-agent` le
directory delle CLI e `~/.local`; dopo l'installazione, il login o l'aggiornamento (e un avvio di
ogni CLI aggiornata, perché crei le sue voci), si riesegue lo script senza `--unseal`. Finché
non lo si fa, devloop non parte:

```bash
sudo bash ops/host/setup-agent-user.sh --unseal
# CLI e login come devloop-agent (tabella qui sotto)
sudo bash ops/host/setup-agent-user.sh
```

Poi le ACL:

- `devloop-agent` attraversa soltanto la home di `devloop` (`--x`) e lo `state_dir` (`--x`), e
  non ha accesso a nessuna voce dello `state_dir` (journal, log, overlay, copie fissate), né a
  quelle che vi si creeranno (ACL di default `d:u:devloop-agent:---`);
- `devloop-agent` entra in `state_dir/worktrees`, `state_dir/agent-shared/turns` e
  `state_dir/agent-shared/tmp` ma non li scrive, e scrive in ciò che devloop vi crea (i worktree,
  le directory dei turni e dei test; ACL di default anche per `devloop`); `agent-shared` e
  `agent-shared/schemas` sono di sola lettura, `agent-shared/gh-config` è scrivibile;
- i metadati git del clone (`.git`) restano di sola lettura per `devloop-agent`: chi li scrive
  può far eseguire a devloop un hook suo quando committa. Lo script toglie le ACL che una
  versione precedente dava a `devloop-agent` su `.git/worktrees`;
- `devloop` legge le sessioni di `devloop-agent` (`~/.claude/projects`, `~/.codex/sessions`), per
  il controllo di stallo e `devloop follow`.

Installa infine il **sudoers** `ops/host/sudoers-agent` in `/etc/sudoers.d/devloop-agent`, dopo
averlo validato con `visudo -c`: come `devloop-agent`, l'utente `devloop` può avviare solo le tre
CLI di `~devloop-agent/.local/bin`, `/usr/bin/bash` (il comando dei test) e `/usr/bin/kill` (il
gruppo di processi a un timeout), con l'ambiente che devloop prepara (`SETENV`) e senza terminale
(`!use_pty`). L'ambiente passa con `sudo -E`, che solo sudo 1.9 rispetta: con sudo-rs serve
`update-alternatives --set sudo /usr/bin/sudo.ws` ([Piattaforma verificata](#piattaforma-verificata)). Rieseguire lo script dopo aver copiato file nello `state_dir`
([host/migrate.md](host/migrate.md)): le ACL delle voci già presenti sono sue.

Poi l'**overlay** deve avere `[agent_user] name = "devloop-agent"` (lo script lo segnala se
manca; `check-host.sh` lo considera un errore; devloop, senza, avvisa una volta su
`[run].alert_issue` e continua). Su un host già in servizio i passi sono in [Attivazione
dell'utente degli agent](host/migrate.md#attivazione-dellutente-degli-agent-415). All'avvio di `tick` e `run` devloop verifica come
`devloop-agent` che non legga né attraversi `key_dir` e le voci private dello `state_dir`, che non
legga l'overlay, che non scriva nulla in `.git` né le directory che contengono worktree e turni,
che scriva nelle directory dei suoi turni, e che non possa scrivere, possedere, creare,
rinominare o rimuovere le directory delle CLI, i file e le directory elencati sopra, gli
eseguibili di `~/.local/bin` e il file a cui portano, con la sua directory, né ciò a cui porta un
link lungo questi percorsi o dentro di essi, e che possa leggerli; altrimenti non parte. Prima
ancora, anche con `[agent_user] check = false`, verifica che l'ambiente arrivi a
`devloop-agent`: una variabile di prova passata con lo stesso `sudo -n -E` dei turni e letta da
`/usr/bin/bash`; se non arriva non parte e dice perché (sudo-rs, con la correzione
`update-alternatives --set sudo /usr/bin/sudo.ws`, o `SETENV` mancante nel sudoers). Il
controllo, come quello di `check-host.sh`, usa solo gli strumenti del sistema (`/usr/bin`,
`/bin`), mai quelli di `~/.local/bin`.

Le **CLI** e i loro login non sono automatizzati: il login è interattivo e ogni fornitore ha il
suo installatore. Tutto come utente `devloop-agent` (`sudo -iu devloop-agent`), dopo
`setup-agent-user.sh --unseal`, con gli eseguibili in `~/.local/bin`, che è il primo elemento del
`secure_path` del sudoers; alla fine, `setup-agent-user.sh` senza `--unseal`:

| CLI | Installazione | Eseguibile |
|---|---|---|
| `claude` | `npm install -g --prefix ~/.local @anthropic-ai/claude-code` | `~/.local/bin/claude` |
| `codex` | installazione standalone del fornitore, in `~/.codex/packages/standalone` | raggiungibile come `~/.local/bin/codex` |
| `agy` | secondo le istruzioni del fornitore | `~/.local/bin/agy` |

Per ciascuna: login (`claude`, `codex`, `agy` avviati una volta in una sessione interattiva) e
verifica con `~/.local/bin/<cli> --version`. Dopo i login, prima di `setup-agent-user.sh` senza
`--unseal` (#572):

- **codex.** Il login interattivo lascia acceso un demone di `devloop-agent`
  (`codex app-server --listen unix://`) e cinque voci di `~/.codex` fuori da
  `AGENT_CLI_ALLOWED`: `app-server-control`, `app-server-daemon`, `version.json`, `.tmp` e
  `tui-thread-reference-capabilities`. Prima si ferma il demone, poi si tolgono le voci, come
  `devloop-agent`; la lista non si allarga:

  ```bash
  sudo pkill -u devloop-agent -f 'codex app-server'
  sudo -u devloop-agent rm -rf -- /home/devloop-agent/.codex/{app-server-control,app-server-daemon,version.json,.tmp,tui-thread-reference-capabilities}
  ```

  `setup-agent-user.sh` senza `--unseal` segnala i processi di `devloop-agent` ancora attivi
  (solo PID e nome) e le voci sconosciute, e stampa i comandi per fermarli e per toglierle; non
  li esegue.
- **agy.** Il login chiede di fidarsi della directory corrente, cioè la home di `devloop-agent`
  (`sudo -iu` parte da lì): si rifiuta. `devloop-agent` non ha un clone suo, i turni girano nei
  worktree dello `state_dir`. `setup-agent-user.sh` senza `--unseal` scrive in
  `trustedWorkspaces` di `~/.gemini/antigravity-cli/settings.json`, sigillato, al più la sola
  directory dei worktree (`<state_dir>/worktrees`), al posto di un valore più largo o che manca;
  un valore già più stretto (solo quella directory o directory dentro di essa, o una lista
  vuota) non lo tocca. `check-host.sh` avvisa se il valore contiene una home intera o una
  directory fuori dallo `state_dir`.

Gli aggiornamenti automatici restano spenti: devloop non deve avviare un turno mentre una CLI
sostituisce il proprio eseguibile. Per Claude Code, `~/.claude/settings.json` con
`"autoUpdates": false` e `"env": {"DISABLE_AUTOUPDATER": "1"}` (lo scrive `setup-agent-user.sh`), e il
launcher esporta `DISABLE_AUTOUPDATER=1`. Le CLI si aggiornano a mano con il loop fermo, fra
`--unseal` e la nuova esecuzione dello script senza
([docs/devloop/guide.md](../docs/devloop/guide.md#agent-clis)).

### 6. Overlay e primo pin

L'overlay `/home/devloop/devloop/.devloop/config.local.toml` contiene solo le chiavi che
l'operatore sovrascrive (merge mode, `[pin] auto`, `[run] alert_issue`, …); quello d'esempio è
tutto commentato. Su questo host ha anche `repo_lock_dir = "/run/devloop/repos"`. Accanto, il
file di progetto `/home/devloop/devloop/.devloop/project.toml`, copiato da
[`ssdlc/project.toml`](host/ssdlc/project.toml), e nell'overlay le chiavi di
[`ssdlc/overlay.toml`](host/ssdlc/overlay.toml) (vedi sopra). Poi il primo pin, che fa anche lo
smoke test. Lo esegue il devloop del clone, non il launcher: file di progetto e overlay si passano
a mano, prima del comando, perché dall'ADR 0006, R3 `devloop/config.toml` non ha `test_cmd`:

```bash
sudo -u devloop -H sh -c 'cd ~/devloop && python3 -m devloop \
    --project-config .devloop/project.toml \
    --local-config .devloop/config.local.toml pin origin/main'
```

### 7. Host del supervisore

```bash
sudo bash ops/host/setup-supervisor.sh --dry-run
sudo bash ops/host/setup-supervisor.sh
```

Crea l'utente `supervisor`, gli dà **solo l'attraversamento** della home di devloop
(`setfacl -m u:supervisor:--x /home/devloop`: raggiunge i log per percorso, ma non elenca la home,
e `~/.ghapps`, `~/.claude` e `~/.codex` di devloop restano 700) e dello `state_dir`, 750
(`setfacl -m u:supervisor:--x <state_dir>`, #573), gli dà la **lettura
dell'overlay e del file di progetto** dello `state_dir` con un'ACL, se manca (`setfacl -m
u:supervisor:r-- <state_dir>/config.local.toml` e `<state_dir>/project.toml`, #543: i due file
dell'operatore sono di solito 600, e `scripts/supervisor-token.py` legge l'overlay), gli dà la
**lettura del flusso degli eventi** del progetto (#683: crea `<state_dir>/events`, di devloop,
750, se manca, e `setfacl -m u:supervisor:r-x,d:u:supervisor:r--` sulla directory, `r--` sui
file che ci sono; "Il flusso degli eventi", passo 10), prepara la
**casella fra supervisore e coordinatore** (il gruppo `devloop-roles` con dentro `supervisor`, e
`/var/lib/devloop-coordination` con `to-coordinator/`, `to-supervisor/`, `to-operator/` (#663) e
le loro `done/`, di root, del gruppo, 2770, e `heartbeat/` per i battiti dei ruoli, #602; [I quattro
ruoli](#i-quattro-ruoli)), installa il sudoers minimale dopo averlo validato con `visudo -c`,
installa la **copia di root del risveglio** `role-wait.sh` con `lib.sh` e i parametri in
`SUPERVISOR_OPS_DIR` (`/usr/local/lib/devloop-supervisor`, 755 e 644, di root; con `PROJECT`,
`-<progetto>`) e il **watchdog dei ruoli** (le unità template e, se il progetto ha anche il
coordinatore, la sua istanza: passo 10, "Watchdog dei ruoli", #602), clona il repository e
prepara `key_dir` e `~/.claude` (700).
Con la sorgente a parte (`SOURCE_REPO`, [Un altro progetto](#un-altro-progetto)) il repository
gestito non contiene `scripts/supervisor-token.py`: lo script clona anche la sorgente, come
`supervisor`, in `SUPERVISOR_SOURCE_CLONE` (`/home/supervisor/devloop-source`; con `PROJECT`,
`~/devloop-source-<progetto>`) e la porta al tag `SOURCE_REF`, a ogni esecuzione (#571). Alla
fine stampa il comando con cui il supervisore conia i token della sua app, dal clone giusto, con
la sua `key_dir` (`DEVLOOP_APP_KEY_DIR`) e lo `state_dir` del progetto (`--state-dir`). Il
comando agisce sul `repo` dell'overlay di quello `state_dir`, che lo script gli passa in
`GH_REPO`, non sull'origin del clone (la sorgente); un `GH_REPO` del chiamante che indica un
altro repository è rifiutato.
Se al posto di `to-coordinator` o `to-supervisor` c'è un file (la casella di prima del #543) lo
script si ferma: si sposta prima, come in
[host/migrate.md](host/migrate.md#casella-fra-i-ruoli-e-sveglia-del-coordinatore-543).

**File sostituito.** Un `install -m 600`, un `cp` o un `rsync` che mette un file nuovo al posto
dell'overlay o del file di progetto ne toglie le ACL: il supervisore non li legge più, e la sua
app non firma. Dopo ogni sostituzione si riesegue `setup-supervisor.sh` (e
`setup-coordinator.sh`, se c'è il coordinatore), poi `check-host.sh`
([host/migrate.md](host/migrate.md#acl-delloverlay-e-del-file-di-progetto-543)).

Il **sudoers** (`ops/host/sudoers-supervisor`) permette:

- come `devloop`, solo `scripts/devloop-pinned` con `status`, `status *`, `report *`,
  `direction` (la direzione dell'operatore, in sola lettura, #604), `tick --dry-run`, `pin *` e
  `notify *` (#593) (con `Defaults>devloop always_set_home, secure_path=…`); nessun `watch`
  dal #683: la sveglia del supervisore legge il flusso degli eventi con la sua ACL; con uno
  `STATE_DIR` diverso dal default o con `SOURCE_REPO`, lo stesso comando dell'unità
  (`/usr/bin/env -C <clone> /usr/bin/python3 <avviatore> --state-dir <state_dir>`);
- come root, solo `systemctl start|stop|restart devloop` e `systemctl status devloop *`.

Nessun comando scrive sull'overlay di devloop. Per esempio:

```bash
sudo -u devloop /usr/bin/python3 /home/devloop/devloop/scripts/devloop-pinned status
sudo /usr/bin/systemctl restart devloop
tail -f /home/devloop/devloop/.devloop/run.log
```

Poi, a mano:

- **chiavi** in `key_dir` del supervisore (`/home/supervisor/.ghapps`, 600): solo `supervisor` e,
  finché P5-4 non è attivo, `claude-agent`. Con P5-4 attivo (`[roles.supervisor]` come tabella
  nell'overlay di devloop), le scritture del supervisore passano da
  `scripts/supervisor-token.py`, che conia un token dell'app `supervisor` con i soli permessi
  dell'azione (`issues` e `pull_requests`, e in sola lettura `actions`, `checks` e `statuses`),
  con `--state-dir` sullo `state_dir` di devloop
  (`/home/devloop/devloop/.devloop`), dal cui overlay legge `repo` e `[apps.supervisor]`
  (ADR 0006, R6); prima passano da `scripts/agent-token.py`, con
  l'attore `claude`, che non espone l'app `supervisor`;
- **nessun token personale** per `gh` (#643, scelta S2 dell'operatore): anche le **letture** del
  supervisore passano dalla sua app, con i soli permessi di lettura chiesti con `--perm`
  (`--perm issues=read --perm pull_requests=read`, e `actions=read`, `checks=read` o
  `statuses=read` per la CI), come per il coordinatore ([github/tokens.md](github/tokens.md));
  nessun passo sul sito, nessuna scadenza;
- **Claude Code** come utente `supervisor`, con lo stesso `npm install` del passo 5 e il login;
- gli strumenti del diario (`~/supervisor`) e la memoria di Claude Code non vengono dal
  repository: su una macchina nuova si copiano da quella vecchia ([host/migrate.md](host/migrate.md)).

### 8. Verifica

```bash
sudo bash ops/host/check-host.sh             # esce con 1 se trova errori
sudo bash ops/host/check-host.sh --no-apps   # senza interrogare GitHub
sudo bash ops/host/check-host.sh --verbose   # con i percorsi reali, per la diagnosi locale
```

Senza `--verbose` l'output, errori dei comandi compresi, nomina i parametri (`$STATE_DIR`,
`$DEVLOOP_KEY_DIR`, `$AGENT_HOME`, …), il ruolo dell'app al posto del nome di un file di chiave
(`<chiave dell'app dispatcher>`), il servizio non di default come `$SERVICE_NAME` e gli utenti
non di default come `<AGENT_USER>`, al posto dei valori dell'host (#583): è l'output che si può
citare in un testo pubblico. Un errore nel caricamento dei parametri resta generico. Con
`--verbose` resta sull'host.

Verifica gli utenti; `key_dir`, `~/.claude` e `~/.codex` con mode 700 e il proprietario giusto;
ogni chiave `*.pem` con mode 600 e del suo utente, una chiave per app attesa e, nella `key_dir`
del supervisore, nessuna chiave in più; quando un parametro indica una `key_dir` non di default,
nessuna chiave di un'app del progetto rimasta nella `key_dir` di default del ruolo (`~/.ghapps`
e, con `PROJECT`, `~/.ghapps-<progetto>` di supervisore e coordinatore: un errore, #582,
[host/migrate.md](host/migrate.md#spostare-una-key_dir-582)); l'ACL `user:supervisor:--x` sulla home di devloop e
nessuna ACL per il supervisore sulle sue directory private; che il supervisore legga `run.log`;
i due sudoers (440, root, accettati da `visudo`, uguali a quelli generati); l'ACL
`user:supervisor:r--` sull'overlay e sul file di progetto, e che il supervisore li legga (#543:
un errore se un file sostituito l'ha persa); la casella (#543: il gruppo `devloop-roles`, con
dentro `supervisor` e, se il progetto lo ha, `coordinator`, e senza `devloop` né `devloop-agent`;
`/var/lib/devloop-coordination`, `to-coordinator/`, `to-supervisor/`, `to-operator/` (#663) e le
loro `done/`, di root,
del gruppo, 2770: un errore se manca); che `devloop-agent` non
legga `key_dir`, lo `state_dir`, l'overlay e il file di progetto, non attraversi `key_dir`, non legga né attraversi le
altre voci dello `state_dir`, entri senza scriverle in `worktrees`, `agent-shared`,
`agent-shared/schemas`, `agent-shared/turns` e `agent-shared/tmp`, non scriva nulla in `.git`,
esegua git nel primo worktree di `worktrees` (avviato da `devloop` con `sudo -n -E` e il solo
`safe.directory` di quel worktree, come in un turno; senza worktree un avviso, #500),
e che l'overlay abbia `[agent_user] name = "devloop-agent"`; la home di `devloop-agent` (1770,
root) e, come `devloop-agent`, i file e i binari delle CLI, come il controllo all'avvio (#438);
le directory delle CLI (1770, root, scrivibili e con lo sticky bit) e che contengano solo le voci
della lista `AGENT_CLI_ALLOWED` (#517);
che `sudo` non sia sudo-rs e che l'ambiente arrivi a `devloop-agent` con `sudo -n -E` come
`devloop`, come il controllo all'avvio (#496; un errore con la correzione `update-alternatives
--set sudo /usr/bin/sudo.ws`, o con il `SETENV` mancante);
il lock per repository (la directory
di root, 3770, del gruppo `devloop-repos`; `devloop` nel gruppo, `devloop-agent` e il supervisore
fuori; `devloop-agent` che non vi entra; il file di `tmpfiles.d`; `repo_lock_dir` nell'overlay);
l'unità (uguale a quella generata, `KillMode=mixed`; con `PROJECT` l'unità template e la drop-in),
che nessun'altra unità di devloop dell'host gestisca lo stesso repository, e lo stato del
servizio; il comando `devloop` e il file del progetto (#588: uguali a quelli generati, 755 e 644 di
root, in una directory di root non scrivibile da altri; un avviso se mancano); che `devloop-agent` scriva le voci di `AGENT_CLI_WRITABLE`; `--version` di ogni CLI
come `devloop-agent` e, sempre come `devloop-agent`, un prompt che le fa eseguire `git --version`
con il loro strumento di shell (#494: passa dal modello, con la rete e il login della CLI; un
errore se non risponde con `git version` o se nell'output c'è un `permission denied`); e
`devloop apps`
(ogni app, la sua installazione e i suoi permessi). Con `--dry-run` stampa, senza eseguirle, le
verifiche che passano da `sudo` o dalla rete.

Delle prove delle CLI (`--version` e strumento di shell) lo script riporta **solo l'esito, con il
codice di uscita** (`ok: … (uscita 0)`, `ERRORE: … (uscita N; l'output non si stampa …)`), mai
l'output della CLI né il testo del modello (#530): chi esegue la verifica, anche un agent, non lo
legge. Per vederlo si ripete la prova a mano
([host/migrate.md](host/migrate.md#file-delle-cli-di-root-sullutente-degli-agent-438), passo 5).
Il coordinatore è facoltativo **per progetto**, non per host (#571): se l'overlay del progetto lo
dichiara (`[apps.coordinator]` o `[roles.coordinator]`) verifica anche l'utente, la sua `key_dir`,
il suo sudoers e la copia di root di `check-host.sh` (passo 10), con un errore se l'utente manca;
altrimenti lo dice in una riga (`nessun coordinatore per questo progetto`), senza errore, anche
se l'host ha il coordinatore di un altro progetto.

Poi, come in [docs/devloop/guide.md](../docs/devloop/guide.md#installation-as-a-dedicated-user),
con il comando `devloop` del passo 4:

```bash
devloop status
devloop tick --dry-run
```

### 9. Avvio

L'attivazione è dell'operatore: il loop spende le quote degli agent.

```bash
sudo systemctl start devloop
sudo systemctl enable devloop       # se deve ripartire al boot
sudo -u devloop -H tail -f /home/devloop/devloop/.devloop/run.log
```

L'unità (`ops/host/devloop.service`, o con `PROJECT` l'istanza dell'unità template
`ops/host/devloop@.service` con la drop-in del progetto, come `devloop@devloop` sull'host di
questo repository dal #563) ha: utente `devloop`,
`WorkingDirectory` sul clone, `PYTHONUNBUFFERED=1`, il `PATH` con `~/.local/bin` prima,
`ExecStart` sul launcher della copia fissata, stdout e stderr in append su `<state_dir>/run.log`,
`KillMode=mixed` (SIGTERM solo al processo principale: devloop smette di pianificare e aspetta le
azioni in corso, fino a `agent_timeout_seconds`), `TimeoutStopSec=4200`, `Restart=on-failure`
con `RestartSec=60`, e `RestartPreventExitStatus=4` (dopo l'allarme di tre tick falliti il loop
resta fermo, come descritto in *Continuous loop* della guida di devloop).

**Avvisi all'operatore (#593).** L'unità ha `OnFailure=devloop-failed@<progetto>.service`
(`_default` senza `PROJECT`): quando systemd la lascia nello stato failed (il codice 4, o il limite
dei riavvii), l'unità template `ops/host/devloop-failed@.service`, che `setup-devloop.sh`
installa una volta per host, esegue come root il comando devloop dell'host con `--project
<progetto> notify --service-failed <progetto>`: l'avviso parte sul canale di `[notify]`
dell'overlay del progetto, al più una volta per intervallo, o resta una riga nel journal senza
canale. Il canale (un bot Telegram, solo in uscita, con il token in un file 600 della key_dir di
`devloop`), `operator_mentions` e la prova `devloop notify --test` sono dell'operatore
([docs/devloop/guide.md](../docs/devloop/guide.md#notices-to-the-operator); su un host attivo
[migrate.md](host/migrate.md#avvisi-alloperatore-593)). Supervisore e coordinatore inviano con
`devloop-pinned notify`, che i loro sudoers ammettono; `check-host.sh` verifica l'unità e, con
`[notify]`, proprietario e permessi del file del token.

### 10. Coordinatore (facoltativo)

Il coordinatore ([ADR 0002](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527))
ha sull'host **solo lettura**: un utente di sistema suo, `coordinator` (`COORDINATOR_USER`,
diverso da `devloop`, `devloop-agent` e `supervisor`), con un sudoers di soli comandi di lettura.
Si installa quando l'operatore assegna il ruolo; devloop non lo richiede. È facoltativo per
progetto: un progetto ha il coordinatore quando il suo overlay dichiara `[apps.coordinator]` o
`[roles.coordinator]`, e solo allora `check-host.sh` lo verifica (#571). Su un host con più
progetti lo script si esegue con `--params` di ciascun progetto che lo vuole.

```bash
sudo bash ops/host/setup-coordinator.sh --dry-run
sudo bash ops/host/setup-coordinator.sh
```

Lo script crea l'utente e la sua `key_dir` (`COORDINATOR_KEY_DIR`, `/home/coordinator/.ghapps`,
700, del coordinatore; con `PROJECT`, quella dell'utente dell'insieme di app, #643), gli dà **solo l'attraversamento**
della home di devloop (`setfacl -m u:coordinator:--x /home/devloop`, come al supervisore: raggiunge
per percorso l'overlay dello `state_dir`, da cui `scripts/coordinator-token.py` legge `repo` e
`[apps.coordinator]`, ma non elenca la home, e `~/.ghapps`, `~/.claude` e `~/.codex` di devloop
restano 700), quello dello `state_dir`, 750 (`setfacl -m u:coordinator:--x <state_dir>`, #573),
e la **lettura dell'overlay** con un'ACL, se manca (`setfacl -m u:coordinator:r--
<state_dir>/config.local.toml`, #554: l'overlay dell'operatore è di solito 600; il file di
progetto no, `coordinator-token.py` non lo legge), la **lettura del flusso degli eventi** del
progetto, come al supervisore (#683, "Il flusso degli eventi" qui sotto), lo mette nella **casella fra supervisore e
coordinatore** (il gruppo `devloop-roles` e le directory di `/var/lib/devloop-coordination`, come
`setup-supervisor.sh`, #543), installa la **copia di root** di
`check-host.sh` e del risveglio `role-wait.sh` (con `coordinator-wait.sh`, #602), dei file che
leggono
(`lib.sh`, i sudoers, le unità e il file di `tmpfiles.d` di `ops/host/`) e dei parametri
(`params.env`) in `COORDINATOR_OPS_DIR` (`/usr/local/lib/devloop-coordinator`, 755 e 644, di
root), e il sudoers (`ops/host/sudoers-coordinator`, in `COORDINATOR_SUDOERS_FILE`,
`/etc/sudoers.d/devloop-coordinator`, 440) dopo averlo validato con `visudo -c`. La copia sta
fuori dalle home e dallo `state_dir`: il coordinatore la esegue come root, quindi non viene né dal
clone di devloop, che `devloop` scrive, né dal checkout dell'operatore. Ogni directory sopra la
copia che esiste già deve essere di root e non scrivibile dal gruppo né dagli altri, e nessuna
può essere un link: altrimenti chi la scrive potrebbe sostituire la copia, e lo script si ferma
prima di creare l'utente, la copia e il sudoers. Il file dei parametri
deve bastare a sé (nessun `.` di un altro file). **Dopo ogni aggiornamento di `ops/`** lo script
si riesegue: `check-host.sh` avvisa se la copia è diversa dal checkout.

Il **sudoers** permette, ciascun comando con i suoi argomenti esatti, senza caratteri jolly:

- come `devloop`: `scripts/devloop-pinned status` e `scripts/devloop-pinned direction` (la
  direzione dell'operatore, #604; lo stesso comando dell'unità, come per il supervisore),
  `/usr/bin/tail -n 200` o `-n 1000` di `<state_dir>/run.log`, e `scripts/devloop-pinned
  notify` (#593); nessun `devloop-pinned watch` dal #683: la sveglia legge il flusso degli
  eventi come utente `coordinator`, con la sua ACL;
- come root: `/usr/bin/journalctl --no-pager -n 200` o `-n 1000 -u devloop`,
  `/usr/bin/systemctl status --no-pager devloop` e
  `/usr/bin/bash /usr/local/lib/devloop-coordinator/host/check-host.sh --read-only`.

Niente che modifichi: né `start`, `stop` o `restart` del servizio, né `pin`, `tick` o `watch`,
né un pager o una shell. Per esempio, come `coordinator`:

```bash
sudo -u devloop /usr/bin/python3 /home/devloop/devloop/scripts/devloop-pinned status
sudo -u devloop /usr/bin/tail -n 200 /home/devloop/devloop/.devloop/run.log
sudo /usr/bin/journalctl --no-pager -n 200 -u devloop
sudo /usr/bin/systemctl status --no-pager devloop
sudo /usr/bin/bash /usr/local/lib/devloop-coordinator/host/check-host.sh --read-only
```

**`check-host.sh --read-only`** fa le verifiche del passo 8 senza avviare nessuna CLI (né
`--version` né lo strumento di shell: nessun modello, nessuna quota, nessuna scrittura nelle
sessioni o nelle cache delle CLI), senza `devloop apps` e senza scrivere nulla; stampa solo righe
di stato. Delle CLI verifica che ci siano. Gira dalla directory `/` e con Python isolato, così
nessun file della directory di chi lo lancia entra nelle verifiche.

**Il risveglio** (#543, #602). Il coordinatore non segue il flusso continuo, che è del
supervisore: la sua sessione avvia in background, come utente `coordinator`, la copia di root del
risveglio dei ruoli, che usa i parametri della copia (`coordinator-wait.sh`, la sveglia di prima,
è lo stesso comando):

```bash
bash /usr/local/lib/devloop-coordinator/host/role-wait.sh --role coordinator                # un'ora
bash /usr/local/lib/devloop-coordinator/host/role-wait.sh --role coordinator --period 7200  # due ore
```

Esce con 0 e una riga per motivo al primo di questi: un messaggio in `to-coordinator/` della
casella (`casella:`, subito, senza consumarlo), un evento del coordinatore su GitHub (`evento:`:
`agent:needs-operator` aggiunta, `agent:needs-coordinator` aggiunta (#607), "il merge spetta
all'operatore", un commento sull'issue degli
allarmi `[run].alert_issue`, una risposta dell'operatore (`operator reply`, #676: un suo commento
di persona che comincia con un comando `/devloop` o su un'issue o una PR in
`agent:needs-operator`), tre letture di GitHub fallite di fila (`watch error:`); gli altri
eventi li salta, anche gli altri commenti dell'operatore e la routine, sotto), un salto del
flusso degli eventi (`flusso:`, sotto), una riga nuova di
`run.log` con un `Traceback`, un `ALERT:`, un `401` o il rifiuto dell'avvio (`log:`), il flusso
fermo (`eventi fermi:`, sotto), il servizio
non attivo (`servizio:`, una volta per cambio di stato), o la fine del periodo
(`COORDINATOR_WAIT_PERIOD`, per default `ROLE_WAIT_PERIOD`, 3600 secondi, o `--period`;
`periodo:`). Legge la casella ogni secondo, il flusso ogni 5 secondi, `run.log`, il servizio e
il segno di vita del flusso ogni minuto. Tiene il suo stato (il cursore del flusso, le righe di
`run.log` già viste, l'ultimo stato del servizio, l'episodio del flusso fermo) in
`~/.local/state/devloop-coordinator-wait/<progetto>/` del coordinatore (`_default` senza
`PROJECT`).

Il supervisore usa lo stesso script dalla sua copia di root, che `setup-supervisor.sh` installa in
`SUPERVISOR_OPS_DIR` (`/usr/local/lib/devloop-supervisor`; con `PROJECT`,
`/usr/local/lib/devloop-supervisor-<progetto>`): `bash
/usr/local/lib/devloop-supervisor/host/role-wait.sh --role supervisor`. Per lui ogni evento del
flusso è un risveglio (non la riga della base di `devloop watch`, né la routine, sotto), con il
suo cursore; non legge `run.log`, che non è nel suo sudoers, né il segno di vita. Il suo stato sta
in `~/.local/state/devloop-supervisor-wait/<progetto>/`.

**Il flusso degli eventi** (#683). GitHub lo legge solo `devloop run`: un suo thread, ogni 60
secondi, anche durante un'azione lunga, scrive gli eventi che `devloop watch` stamperebbe, con le
stesse distinzioni (label, `operator reply`, allarmi, la routine di #682), in
`<state_dir>/events/`: `events.log`, in sola aggiunta, una riga per evento con un numero
progressivo e l'ora UTC; `events.log.1`, il segmento precedente (la **rotazione** avviene a
**10 MiB**, e si conservano solo il segmento corrente e uno precedente); `alive`, il **segno di
vita**, l'ora dell'ultima lettura di GitHub riuscita, anche senza eventi; `id`, l'identità del
flusso. Il formato è nella
[reference](../docs/devloop/reference.md#the-stream-of-the-events). La directory e i file sono di
`devloop` (750 e 640): `setup-supervisor.sh` e `setup-coordinator.sh` danno all'utente del ruolo
**un'ACL di sola lettura** (scelta dell'operatore su #683) sulla directory (`r-x`, con l'ACL di
default `r--` per i segmenti nuovi) e sui file, e l'attraversamento (`--x`) della home di devloop
e dello `state_dir`, nient'altro dello `state_dir`. L'ACL è per progetto: l'utente di un ruolo
condiviso fra più progetti (#643) la riceve sul flusso di ciascun progetto che serve,
dall'esecuzione di `setup-*.sh` di quel progetto. Il ruolo legge il flusso come sé stesso: le
sveglie non eseguono `devloop watch` come `devloop`, e i sudoers dei ruoli non hanno più la sua
regola. `check-host.sh` verifica che il flusso sia scrivibile solo da `devloop` e leggibile dai
ruoli del progetto, e che i sudoers dei ruoli non abbiano più la regola di `devloop-pinned watch`.

Ogni risveglio tiene il suo **cursore** (l'ultimo evento letto) nel suo stato, uno per ruolo e
per progetto: un ruolo non consuma gli eventi dell'altro, e un progetto non legge né sposta il
cursore di un altro. Il filtro su quali eventi svegliano chi resta nel risveglio. **Al primo
avvio** per un progetto, senza cursore, il risveglio crea la base alla fine del flusso e lo dice
in una riga (`flusso: base …`), senza svegliarsi. **Dopo la sostituzione del flusso** (un altro
`id`), o quando il cursore chiede eventi non più conservati, lo dice in una riga (`flusso: …`),
che sveglia il ruolo, e riprende dal primo evento ancora conservato: nessun evento si perde in
silenzio. Un flusso che non si legge (manca, o manca l'ACL) è un avviso su stderr, una volta.

**Il flusso fermo**, solo per il coordinatore: con il servizio attivo, se il segno di vita è più
vecchio di `EVENTS_STALE_AFTER` secondi (un parametro dell'host, `params.env`, default **300**),
contati dal più recente fra il segno di vita e l'avvio del servizio (`systemctl show`), o è
assente o illeggibile, il risveglio esce con una riga distinta, `eventi fermi:`, una volta per
episodio: l'avviso si riarma quando il segno di vita torna recente. Il servizio fermo resta
`servizio:`. `devloop watch` interattivo resta su GitHub, con le sue opzioni e il suo file di
stato: il flusso serve solo le sveglie dei ruoli.

**La routine non sveglia nessuno dei due ruoli** (#682). `devloop run` la riconosce per l'autore
che verifica, mai per il testo: l'archivio di un giorno del riepilogo giornaliero (`report by`, il
record `report` di `devloop report` scritto dall'identità del dispatcher; l'aggiornamento del
corpo dell'issue del riepilogo non è un evento), i commenti delle app di ruolo
(`supervisor comment`, solo con `[roles.supervisor]` come tabella, e `coordinator comment`, con
`[roles.coordinator]`). Il risveglio salta l'archivio del riepilogo, i commenti del ruolo stesso e
quelli dell'altro ruolo sull'issue degli allarmi, l'issue del diario. Restano risvegli, come
prima, gli altri commenti del dispatcher sull'issue degli allarmi (i suoi avvisi), i commenti
dell'operatore, le label e ogni commento che devloop non attribuisce a un ruolo: un supervisore
senza la sua app di ruolo firma con l'app del suo agent, e i suoi commenti svegliano come prima; un
diario tenuto su un'altra issue sveglia ancora il supervisore.

A ogni avvio lo script scrive il **battito** del ruolo, `COORDINATION_DIR/heartbeat/<ruolo>`:
l'ora (secondi dall'epoca) e il periodo, 664 del gruppo dei ruoli. Un battito che non si scrive è
un avviso su stderr, non ferma il risveglio. Il primo messaggio di `start-role.sh` dà a ciascun
ruolo il comando di risveglio suo e dell'altro.

**Watchdog dei ruoli** (#602; ADR 0002, seconda precisazione del 2026-11-23, punto 6). Un timer
systemd per progetto, `devloop-role-watchdog@<progetto>.timer` (`_default` senza `PROJECT`, che
non coincide con nessun progetto), avvia ogni 5 minuti
`devloop-role-watchdog@<progetto>.service`, che esegue `role-watchdog.sh` dalla copia di root `WATCHDOG_OPS_DIR` (`/usr/local/lib/devloop-role-watchdog`; con `PROJECT`,
`-<progetto>`) con i parametri del progetto copiati lì. Un ruolo è fermo se il suo battito manca o
è più vecchio di due periodi (quello scritto nel battito):

- supervisore fermo: un messaggio in `to-coordinator/`;
- coordinatore fermo: un messaggio in `to-supervisor/` e un avviso all'operatore;
- entrambi fermi: un avviso all'operatore e un messaggio in entrambe le caselle.

Ogni avviso nomina il progetto, e lo stesso avviso non si ripete finché lo stato non cambia (lo
stato sta in `/var/lib/devloop-role-watchdog/<progetto>`, `StateDirectory=`). Gli avvisi
all'operatore useranno `devloop notify` del progetto (#593); finché non c'è, il watchdog scrive
solo nelle caselle e nel journal (`journalctl -u devloop-role-watchdog@<progetto>`, le righe
`avviso all'operatore:`). Il timer rileva e non agisce: non ha chiavi GitHub e non avvia né ferma
nulla. Un controllo che fallisce (un battito che non si legge, un messaggio che non si scrive)
attiva l'`OnFailure=`, `devloop-role-watchdog-failed@<progetto>.service`, che lo scrive una volta
in entrambe le caselle.

L'unità gira come `WATCHDOG_USER` (`role-watchdog`; con `PROJECT`, `role-watchdog-<progetto>`), un
utente di sistema senza home né shell, **uno per progetto**, che **non** è nel gruppo dei ruoli:
entra solo nella casella del suo progetto, con le ACL che mettono gli script di setup (`--x` su
`COORDINATION_DIR`, `heartbeat/` e `rest/`, `rwx` su `to-supervisor/` e `to-coordinator/`, che
fino al #684 erano `-wx`), e scrive solo
lì (`ReadWritePaths=`, con `ProtectSystem=strict`). Così le istanze di due progetti sullo stesso
host non leggono la casella dell'altro. `setup-supervisor.sh` e `setup-coordinator.sh`
installano le tre unità template in `SYSTEMD_DIR` (una volta per host) e, quando il progetto ha
sia il supervisore sia il coordinatore, l'utente, le ACL, la copia di root, le drop-in delle due
istanze (`ops/host/role-watchdog-project.conf`) e il timer abilitato e avviato (`systemctl enable
--now`). `check-host.sh` verifica la copia del supervisore e, con il coordinatore del progetto,
le unità, le drop-in, il timer abilitato, l'utente fuori dal gruppo e le sue ACL sulla casella.

**Il riposo dei ruoli** (#684; [ADR 0002, precisazione del
2026-11-26](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-26-il-riposo-dei-ruoli-684)).
A coda vuota ogni risveglio periodico di un ruolo rilegge dalla cache tutto il contesto della
sessione, senza trovare niente da fare. Un ruolo che a fine turno non ha niente da fare entra
quindi in **riposo**: lancia in background `role-wait.sh --role <ruolo> --rest` al posto della
sveglia normale, che prende il lock `COORDINATION_DIR/rest/<ruolo>.lock` (con `flock`: il lock
tenuto prova che la sessione è viva), scrive lo **stato di riposo** `rest/<ruolo>` ("<inizio>
riposo", 664), e non scrive battiti. Se la casella ha già un messaggio, o il flusso un evento per
il ruolo, non entra in riposo: è un risveglio come gli altri. Altrimenti aspetta solo il
**segnale della sentinella**, `rest/<ruolo>.wake`, scritto dopo l'inizio del riposo: non legge
GitHub né altro che possa bloccarsi, ed esce con una riga `sentinella: <motivo>`. Il ruolo
rilancia allora la sveglia normale, che toglie lo stato, scrive il battito e gli dà gli eventi dal
suo cursore. La directory `rest/` è di root, del gruppo dei ruoli, **3770**: con lo sticky bit
ognuno rimuove o sostituisce solo i suoi file. Se la CLI di un ruolo non regge l'attesa, il ruolo
usa la sveglia normale, come prima (la prova delle CLI è nel passo sull'host,
[host/migrate.md](host/migrate.md#il-riposo-dei-ruoli-684)).

La **sentinella** è un servizio per progetto, `devloop-role-sentinel@<progetto>.service`
(`ops/host/role-sentinel.sh`, dalla copia di root del watchdog, `WATCHDOG_OPS_DIR`), senza
modello, senza chiavi e senza battiti per nessuno. Un giro ogni 5 secondi: mentre un ruolo è a
riposo sveglia **solo il ruolo interessato**, con gli stessi filtri della sua sveglia, una volta per
riposo: un messaggio nella sua casella; un evento del flusso scritto dopo l'inizio del riposo (ogni
evento per il supervisore, i suoi per il coordinatore), letto con un cursore della sentinella; per
il coordinatore, una riga nuova di `run.log` con un errore; per entrambi, il servizio di devloop
non attivo (a ogni cambio di stato) e il **lavoro** che `devloop run` trova con un ruolo a riposo
(sotto). L'issue degli allarmi del filtro la legge dal file `alert_issue` che `devloop run` scrive
nel flusso al suo avvio: la sentinella non legge l'overlay, che ha i segreti del progetto. Il
**controllo giornaliero**: all'ora fissa UTC `REST_DAILY_CHECK` (default **00:30**, dopo l'ora
consueta del riepilogo giornaliero, ma indipendente dalla sua pubblicazione) sveglia ogni ruolo il
cui riposo è cominciato prima: il coordinatore per il controllo dei testi pubblicati e del lavoro
fermo, il supervisore che, senza niente da fare, torna a riposo. L'attesa a riposo dura così al
più un giorno (`role-wait.sh --rest` stesso esce dopo 25 ore senza segnale). La sentinella toglie
il suo segnale quando il ruolo non è più a riposo. Contro il blocco, l'unità è `Type=notify` con
`WatchdogSec=120` (un segno di vita a ogni giro, `systemd-notify WATCHDOG=1`), `Restart=on-failure`
e `OnFailure=devloop-role-sentinel-failed@<progetto>.service`, che avvisa l'operatore con `devloop
notify --sentinel-failed <progetto>`, come per il servizio di devloop (#593). Gira come
`SENTINEL_USER` (`role-sentinel`; con `PROJECT`, `role-sentinel-<progetto>`), un utente di sistema
del solo progetto, fuori dal gruppo dei ruoli, con privilegi minimi: `--x` su `COORDINATION_DIR`,
`-wx` su `rest/` (la sola scrittura, il segnale di risveglio, `ReadWritePaths=` della drop-in
`ops/host/role-sentinel-project.conf`), `r-x` su `to-supervisor/` e `to-coordinator/`, la sola
lettura del flusso degli eventi e l'attraversamento della home di devloop e dello `state_dir`, per
`run.log`; il suo stato (i cursori, l'ultimo stato del servizio) in
`/var/lib/devloop-role-sentinel/<progetto>` (`StateDirectory=`).

**Il loop e il riposo.** Con `rest_dir` in `[run]` dell'overlay (il percorso di
`COORDINATION_DIR/rest`, letto da `DEVLOOP_USER` con l'attraversamento `--x` della casella e di
`rest/`), `devloop run` resta acceso ma **non avvia un controllo di prontezza né
un'implementazione mentre un ruolo è a riposo**: trattiene il lavoro e aspetta che lo stato di
riposo sia tolto; poi parte subito. Con un ruolo a riposo scrive il lavoro nel file `hold` del
flusso degli eventi (`<prima volta> <ruoli> <lavoro>`): le azioni trattenute e la coda, anche
quando nessuna azione può partire (un'issue bloccata da un prerequisito); su quel file la
sentinella sveglia i ruoli a riposo. Se un ruolo non si sveglia entro `rest_wake_timeout` secondi
(default **300**) dal primo rilevamento del lavoro, il lavoro resta fermo e l'operatore lo sa con
`devloop notify`, una volta. Se `devloop run` non legge `rest/` (manca, o manca la sua ACL) non lo
prende per un riposo assente: trattiene il lavoro, scrive `unknown` al posto dei ruoli e avvisa
l'operatore, una volta ([reference](../docs/devloop/reference.md#the-roles-at-rest)). Senza
`rest_dir` il loop non guarda il riposo.

**Il watchdog e il riposo.** Il watchdog vede ogni ruolo in tre stati: **attivo** (battiti
regolari); **a riposo** (lo stato di riposo, la sentinella attiva, il lock tenuto: una riga
`informazione:` nel journal, una volta per ingresso, «a riposo dal …, coda vuota», mai un allarme);
**fermo** (nessun riposo valido, e il battito manca o è più vecchio di due periodi: l'allarme di
prima). A riposo segnala solo le **anomalie**, come un ruolo fermo: la sentinella non attiva; il
lock non tenuto (la sessione è morta); il ruolo svegliato dalla sentinella che non toglie il riposo
entro `REST_WAKE_TIMEOUT` secondi dal segnale; il riposo dichiarato con del lavoro, cioè la coda
non vuota o il lavoro trattenuto, come li scrive `devloop run` nel file `hold`, da più di
`REST_WAKE_TIMEOUT` secondi, o un messaggio nella sua casella
da più di `REST_MAILBOX_AGE` secondi dal suo arrivo (entrambi default **300**, parametri di
`params.env`). Per questo il watchdog ha anche `--x` su `rest/`, `rwx` (non più `-wx`) sulle due
caselle (ne legge i nomi e le ore dei messaggi, non il contenuto) e la lettura del flusso degli
eventi, con l'unità che legge lo `state_dir` (`ProtectHome=read-only`). `setup-supervisor.sh` e
`setup-coordinator.sh` preparano tutto con il watchdog; `check-host.sh` verifica la directory
`rest/` (3770), le unità e la drop-in della sentinella, il servizio abilitato e attivo, il suo
utente fuori dal gruppo e le ACL della sentinella e di devloop. Su un host già attivo:
[host/migrate.md](host/migrate.md#il-riposo-dei-ruoli-684).

**La pausa dell'operatore** (#685; [ADR 0002, precisazione del 2026-11-26 sulla
pausa](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-26-la-pausa-delloperatore-685)).
Il riposo vale solo quando non c'è niente da fare; la **pausa** ferma tutto anche con issue e PR
aperte, ed è il secondo motivo dello stesso meccanismo. La apre e la chiude **solo l'operatore**,
come root, con il comando `devloop` dell'host (passo 4): `devloop --project <progetto> pause`
per la pausa (il servizio fermo, i ruoli in pausa), `devloop --project <progetto> resume` per la
ripresa (il servizio riparte, i ruoli si svegliano).

`pause` scrive la pausa `COORDINATION_DIR/rest/pause` ("<inizio> pausa", 644 di root), poi ferma il
servizio con `systemctl stop`: `devloop run` finisce il tick e le azioni in corso, e a `SIGTERM` non
avvia altri tick. `resume` toglie la pausa, poi avvia il servizio. Conta solo il file di root: i
ruoli scrivono in `rest/`, ma un loro file `pause` non è una pausa, e quello di root non lo possono
togliere né sostituire (lo sticky bit di `rest/`), né rinominare `rest/` per farlo sparire (lo
sticky bit di `COORDINATION_DIR`, 3770 con #685: `pause` si ferma senza i due); una loro directory
`pause` al posto del file ferma `pause`, che chiede di toglierla. Nessun ruolo ha una regola di sudo
per il comando `devloop`. Mentre la pausa c'è, `devloop run` (con `rest_dir` nell'overlay) esce subito senza tick,
anche se qualcuno avvia il servizio (il supervisore può farlo, con il suo sudoers): solo `resume`
chiude la pausa. In pausa:

- **i ruoli** aspettano con lo stesso comando, lo stesso stato e la stessa sentinella del riposo, con
  il motivo `pausa`: `role-wait.sh`, con o senza `--rest`, scrive il battito, toglie lo stato di
  prima e, con la casella vuota, scrive lo stato `rest/<ruolo>` "<inizio> pausa", tiene il lock e
  aspetta, quanto dura la pausa; una sveglia normale o un riposo già in attesa entrano in pausa da
  sé, e un segnale della sentinella di prima non li sveglia. Esce con `sentinella: <motivo>`
  al segnale della sentinella, togliendo lo stato e scrivendo il battito, e con `ripresa:` quando
  l'operatore toglie la pausa;
- **la sentinella** sveglia un ruolo solo per un messaggio nella sua casella o per un suo allarme:
  per il coordinatore una riga di `run.log` con un errore, e per entrambi il servizio di devloop di
  nuovo attivo durante la pausa (`allarme:`). Non per gli eventi del flusso, il servizio fermo, il
  lavoro trattenuto né il controllo giornaliero;
- **il watchdog dei ruoli** vede in **pausa** un ruolo che aspetta con il suo stato e il suo lock:
  il lavoro in attesa non è un'anomalia, le altre anomalie del riposo restano (la sentinella non
  attiva, il lock non tenuto, il ruolo svegliato che non riprende, un messaggio vecchio nella
  casella). Del progetto scrive nel journal, una volta per pausa, un'informazione, «in pausa per
  ordine dell'operatore dal …», con il lavoro in attesa: le issue e le PR della coda dell'ultimo
  tick, che `devloop run` scrive nel file `queue` del flusso degli eventi;
- **`check-host.sh`** dice la pausa, e il servizio fermo non è un avviso.

Su un host già attivo: [host/migrate.md](host/migrate.md#la-pausa-delloperatore-685).

**La manutenzione dell'host** (#705). Durante un passo sull'host (le sezioni di
[host/migrate.md](host/migrate.md)) i ruoli vedrebbero il servizio fermo o fallito senza sapere che
è voluto, e un avvio o un pin a metà dei passi romperebbe la procedura. La manutenzione è uno
stato, come la pausa, che accompagna una procedura dell'host: ha una scadenza e si chiude alla
fine. La apre e la chiude chi fa il passo, come root, con il comando `devloop` dell'host (passo 4);
chi la usa e come cambiano i runbook dei ruoli lo dice #706.

- **`devloop --project <progetto> maintenance start "<sezioni>"`** si rifiuta se c'è già una
  manutenzione o una pausa; crea in modo esclusivo `COORDINATION_DIR/rest/maintenance` ("<inizio>
  manutenzione <scadenza> <sezioni>", 644 di root, con `-` al posto della scadenza finché il
  servizio non è fermo), poi ferma il servizio e scrive la scadenza, `MAINTENANCE_TIMEOUT` secondi
  (default **7200**, due ore) da quando il servizio è davvero fermo. Programma con un timer
  transitorio di systemd (`devloop-maintenance-<progetto>-<inizio>`) l'avviso della scadenza
  all'operatore, con `devloop notify` (`maintenance expired`, che non avvisa se quella manutenzione
  è già chiusa; il timer non sopravvive a un riavvio della macchina, il watchdog sì). Poi dice quali
  ruoli aspettano già in manutenzione (lo stato e il lock tenuti dalla loro sveglia) e quali no,
  aspetta le loro conferme per al più `MAINTENANCE_ACK_TIMEOUT` secondi (default **300**), poi
  prosegue comunque e scrive nell'output e nel log di sistema (`logger -t devloop-maintenance`) chi
  non ha confermato: l'ok è una conferma, non una condizione di sicurezza.
- **`devloop --project <progetto> maintenance end`** toglie la manutenzione, ferma il timer della
  scadenza e riavvia il servizio, salvo una pausa dell'operatore aperta nel frattempo, che vince: il
  servizio resta fermo fino al `resume`. Il `resume` di una pausa durante la manutenzione non
  riavvia il servizio: lo fa `maintenance end`.
- **Le difese** non dipendono dai ruoli: mentre `rest/maintenance` di root c'è, anche dopo la
  scadenza (scelta M5 (a): un tick o un pin su un passo a metà fanno più danni di un progetto
  fermo), `devloop run` (con `rest_dir` nell'overlay) non fa tick, anche se qualcuno avvia il
  servizio, e `devloop pin` si rifiuta, con un messaggio che nomina la manutenzione. Passa solo il
  pin del comando `devloop` dell'host, come root, il pin del passo: il comando dà a devloop
  l'inizio della manutenzione (`DEVLOOP_MAINTENANCE_PIN`), che i sudoers dei ruoli, che azzerano
  l'ambiente, non possono dare. Come per la pausa conta solo il file di root, e lo sticky bit di
  `rest/` e di `COORDINATION_DIR` impedisce a un ruolo di toglierlo.
- **I ruoli** ricevono ogni cambio una volta (scelta M1), senza messaggi nella casella: la loro
  sveglia (`role-wait.sh`, normale, a riposo o in pausa) esce con `manutenzione: iniziata …`
  (l'inizio, le sezioni, la scadenza), `manutenzione: in corso …` se la manutenzione c'era già al
  suo avvio (anche un ruolo appena ripartito lo sa prima di qualunque azione),
  `manutenzione: scaduta …` alla scadenza e `manutenzione: finita …` alla fine. Rilanciata durante
  la manutenzione, aspetta in manutenzione: lo stato `rest/<ruolo>` "<inizio> manutenzione" e il
  lock, senza battiti; sveglia solo per un messaggio nella casella, non per il servizio fermo, il
  flusso fermo o gli errori del log.
- **La sentinella** sveglia un ruolo solo per un messaggio nella sua casella.
- **Il watchdog dei ruoli** vede **in manutenzione** un ruolo che aspetta con il suo stato e il suo
  lock: il lavoro in attesa, la sentinella non attiva e i suoi segnali non sono anomalie; il lock
  non tenuto e un messaggio vecchio nella casella sì. Del progetto scrive nel journal, una volta
  per manutenzione, un'informazione, «in manutenzione dal …, sezioni …, scadenza …», e alla
  scadenza, se nessuno l'ha chiusa, una volta, la riga `allarme all'operatore:`.
- **`check-host.sh`** dice la manutenzione, e il servizio fermo non è un avviso.

Un progetto con `SOURCE_REF` (#687) ha il comando e le sveglie del suo tag: finché il tag non
contiene #705, per quel progetto la manutenzione non c'è, e i passi sull'host si fanno come prima.
Su un host già attivo: [host/migrate.md](host/migrate.md#la-manutenzione-dellhost-705).

**App del coordinatore** (#549). Il coordinatore firma comandi, commenti e label con un'app sua,
`<APP_PREFIX>coordinator`, che l'operatore crea come le altre (passo 2), dal manifest
`ops/github/apps/coordinator.json`: `issues: write`, `pull_requests: write`, `metadata: read` e,
in sola lettura per la diagnosi della CI, `actions`, `checks` e `statuses`; **nessun accesso ai
contenuti**.

1. `bash ops/github/create-apps.sh --dry-run` la elenca (`coordinator` è in `APPS`) con i suoi
   permessi; la pagina generata ha il suo pulsante. Creata l'app, si completa lo scambio del
   `code` e la si installa solo sul repository gestito (passo 2, punti 2 e 3).
2. **La chiave** `<APP_PREFIX>coordinator.<data>.private-key.pem` va **solo** nella `key_dir`
   del coordinatore, mode 600, proprietario `coordinator`:
   `sudo install -o coordinator -g coordinator -m 600 "$work"/*.private-key.pem
   /home/coordinator/.ghapps/`, poi `rm -rf "$work"`. Mai nella `key_dir` di devloop né in quella
   del supervisore: devloop non la legge.
3. `app_id` e `slug` vanno in `[apps.coordinator]` dell'overlay dello `state_dir` di devloop
   ([host/config.local.toml.example](host/config.local.toml.example)). L'app conta come
   coordinatore solo con `[roles.coordinator]`, decisione dell'operatore
   ([docs/devloop/guide.md](../docs/devloop/guide.md), "The coordinator").
4. Il coordinatore, come utente `coordinator` e dal proprio clone, `COORDINATOR_CLONE` (con la
   sorgente a parte, con un `cd` nel clone della sorgente, `COORDINATOR_SOURCE_CLONE`,
   `/home/coordinator/devloop-source` o, con `PROJECT`, `~/devloop-source-<progetto>`, che
   `setup-coordinator.sh` clona al tag `SOURCE_REF`, #571), conia i token con
   `scripts/coordinator-token.py`, che legge `repo` e `[apps.coordinator]` dall'overlay dello
   `state_dir` di `--state-dir` e solo la propria chiave dalla `key_dir` di
   `DEVLOOP_APP_KEY_DIR`; il token arriva al comando solo in `GH_TOKEN`, mai stampato, e il
   comando agisce sul `repo` dell'overlay, in `GH_REPO`, non sull'origin del clone (#571).
   **`DEVLOOP_APP_KEY_DIR` va sempre impostata** al valore di `COORDINATOR_KEY_DIR`
   (`/home/coordinator/.ghapps`; con `PROJECT`, `~/.ghapps` dell'utente dell'insieme): senza,
   lo script cerca la chiave in `key_dir` di `[apps]` dell'overlay, che è quella di devloop
   (`DEVLOOP_KEY_DIR`), e non la trova. `setup-coordinator.sh` stampa il comando con i valori
   dell'installazione. Senza `--perm` il token ha `issues=write` e
   `pull_requests=write`; si possono chiedere solo `issues` e `pull_requests` (`read` o `write`) e
   `actions`, `checks`, `statuses` e `metadata` in lettura: ogni altro permesso è rifiutato.
   `scripts/agent-token.py` non espone l'app.

   ```bash
   DEVLOOP_APP_KEY_DIR=/home/coordinator/.ghapps python3 scripts/coordinator-token.py \
       --state-dir /home/devloop/devloop/.devloop -- gh issue comment <ISSUE> --body-file <FILE>
   DEVLOOP_APP_KEY_DIR=/home/coordinator/.ghapps python3 scripts/coordinator-token.py \
       --state-dir /home/devloop/devloop/.devloop --perm actions=read -- \
       gh run view <RUN> --log-failed
   ```

`check-host.sh` verifica la `key_dir` (700, del coordinatore) e che contenga solo la chiave
dell'app del coordinatore (600, del coordinatore), l'ACL `user:coordinator:--x` sulla home di
devloop, nessuna ACL per il coordinatore sulle directory private di devloop, l'ACL
`user:coordinator:r--` sull'overlay, che il coordinatore legga l'overlay, che sia nel gruppo
della casella, e che la copia della sveglia coincida con il checkout. Le prove di lettura
come un altro utente usano il `[ -r ]` di bash, che rispetta le ACL, mai `/usr/bin/test`: quello
di uutils, il default di Ubuntu 26.04, guarda solo i bit del modo (#554).

**Overlay sostituito.** Un `install -m 600`, un `cp` o un `rsync` che mette un file nuovo al posto
dell'overlay ne toglie le ACL, quella del coordinatore e quella del supervisore. Dopo ogni
sostituzione si riesegue `setup-coordinator.sh`, che rimette l'ACL del coordinatore, e
`setup-supervisor.sh`, che rimette quella del supervisore, e si verifica con `check-host.sh`
([host/migrate.md](host/migrate.md#acl-delloverlay-e-del-file-di-progetto-543)).

**Clone e CLI** (#643). Lo script clona anche il repository gestito, come `coordinator`, in
`COORDINATOR_CLONE` (`/home/coordinator/<nome del repository>`), e la sessione del coordinatore
parte da lì, come quella del supervisore dal suo clone (#621): anche con la sorgente a parte il
coordinatore lavora nel clone del suo progetto, e conia i token dal clone della sorgente con un
`cd`. Scrive le impostazioni di Claude Code senza aggiornamenti automatici (`autoUpdates: false`
e `DISABLE_AUTOUPDATER=1` in `~/.claude/settings.json`), come per il supervisore;
`check-host.sh` avvisa se mancano, per il supervisore e per il coordinatore, e dà errore se manca
il clone del coordinatore.

Poi, a mano: la CLI dell'agent del coordinatore e il suo login, come utente `coordinator`.
Nessun token personale per `gh`: il coordinatore legge e scrive GitHub con la sua app
([github/tokens.md](github/tokens.md)). Su un host già attivo i passi sono in
[host/migrate.md](host/migrate.md#utente-del-coordinatore-530).

## Migrazione su un'altra macchina

Vedi [host/migrate.md](host/migrate.md#spostare-linstallazione-su-un-altro-host-543), la
procedura per l'agent dell'host: utenti, chiavi, `state_dir`, file di progetto e overlay con le
loro ACL, casella fra i ruoli, ruoli, servizi, login delle CLI e verifica finale, cosa copiare e
cosa ricreare, in che ordine, senza mai due dispatcher attivi sullo stesso repository.

Un host già attivo prima del #438 fa una volta il passo di
[host/migrate.md](host/migrate.md#file-delle-cli-di-root-sullutente-degli-agent-438): file e
binari delle CLI di `devloop-agent` di root, prima del pin della copia che lo controlla.

### Migrazione di R3 su un host attivo

Il passaggio di un host in servizio alla copia di R3 (ADR 0006) è in [host/migrate.md](host/migrate.md#migrazione-di-r3-su-un-host-attivo):
file di progetto e chiavi dell'overlay **prima** che devloop prenda la copia nuova (prima del
merge della PR, o a loop fermo), `check-equivalence.py` eseguito **come root** dal checkout
dell'operatore, il significato dei codici di uscita (0 equivalente, 1 differenze, 2 un lato non si
carica) e come ripartire se il loop si è fermato.

### Identità nell'overlay (R6)

Con l'ADR 0006, R6, `repo`, `trusted_users` e `[apps]` lasciano `devloop/config.toml` per
l'overlay. L'overlay li riceve **prima** che devloop prenda la copia di R6: la copia di prima li
accetta con gli stessi valori, quella di R6 senza di essi non parte. `scripts/agent-token.py`
diventa un involucro di `devloop token` della copia fissata, e funziona dal pin della copia di
R6; il supervisore passa a `scripts/supervisor-token.py` lo `state_dir` di devloop con
`--state-dir`. I passi sono in [host/migrate.md](host/migrate.md#identità-di-ssdlc-nelloverlay).

Ogni PR che richiede un passo sull'host dice, nella descrizione e nella guida, **che cosa fare
prima del merge** ([host/migrate.md](host/migrate.md#ogni-cambio-con-un-passo-sullhost)).

## Un altro progetto

Per default devloop lavora sul repository che lo contiene: `pin` estrae devloop dallo stesso
repository di cui crea i worktree. Ci sono due strade. La prima, passo per passo con un esempio
di file di progetto e di overlay, è nella [guida rapida per un nuovo
progetto](../docs/devloop/quickstart.md) (ADR 0006, R9).

**Sorgente dell'installazione a parte** (ADR 0006, R1). Il repository gestito non contiene
devloop: `pin` prende il codice da un clone di devloop, a un suo tag
([docs/devloop/guide.md](../docs/devloop/guide.md#installation-source)). In `params.env`:
`REPO` del progetto, `SOURCE_REPO=brushtyler/devloop`, `SOURCE_REF` con il tag da fissare, e di
solito uno `STATE_DIR` assoluto. `setup-devloop.sh` clona la sorgente in `SOURCE_CLONE`
(`/home/devloop/devloop-source`) e installa l'avviatore dal tag `SOURCE_REF` in `LAUNCHER_DIR`
(`/usr/local/lib/devloop/devloop-pinned`, di root; con `PROJECT`
`/usr/local/lib/devloop/<progetto>/devloop-pinned`), fuori dalla copia fissata e dal clone gestito;
unità e sudoers lo chiamano con `--state-dir`. L'overlay nello `state_dir` ha `source`,
`state_dir` e le chiavi del progetto (`repo`, `trusted_users`, `base_branch`, `[apps]` con la
`key_dir` del progetto, …; ADR 0006, R6): lo script ricorda `source`, `state_dir` e `repo` finché
mancano. `[apps]` è l'insieme di app del progetto: per default quello del repository, creato
con il suo `APP_PREFIX` e installato solo lì, oppure un insieme condiviso ([Insieme di app per
repository o condiviso](#insieme-di-app-per-repository-o-condiviso)); le sessioni scrivono con `scripts/agent-token.py --state-dir <state_dir>`
del clone della sorgente, cioè con `devloop token` della copia fissata. I fatti del progetto (`test_cmd`, `[docs.map]`, …) stanno nel file di
progetto `project.toml` dello stesso `state_dir` (ADR 0006, R3). Il primo pin si fa dal clone
della sorgente, con il file di progetto e l'overlay prima del comando:

```bash
sudo -u devloop -H sh -c 'cd ~/widget && PYTHONPATH=~/devloop-source python3 -P -m devloop \
    --project-config /var/lib/devloop/widget/project.toml \
    --local-config /var/lib/devloop/widget/config.local.toml pin v1.2.0'
```

Il pin dopo un merge resta spento: un nuovo tag si fissa a mano, con il comando dell'host
(`devloop --project widget pin <tag>`, passo 4), che chiama l'avviatore con lo `--state-dir` del
progetto. I dati del progetto nei prompt (le istruzioni da leggere, il comando
di un solo modulo di test, le regole sugli ADR) stanno nella tabella `[prompts]` del file di
progetto (ADR 0006, R4, [docs/devloop/reference.md](../docs/devloop/reference.md#project-data-in-the-prompts)):
senza, i prompt tolgono le frasi che li citano. I test locali e la lettura della CI seguono
`[tests]` del file di progetto (R5): vale quanto sotto per `local_tests` e `ci_rerun_unrelated`.

**Gli script di un progetto con `SOURCE_REF` dal checkout del suo tag** (#687). Per un progetto
con la sorgente a parte, ogni script di `ops/host/` (`setup-*.sh`, `check-host.sh`,
`start-role.sh`) e ogni sezione di [host/migrate.md](host/migrate.md) si eseguono da un checkout
dell'operatore al tag `SOURCE_REF` del progetto, mai da uno su `main`: gli script installano le
copie di root di `ops/` dei ruoli (`SUPERVISOR_OPS_DIR`, `COORDINATOR_OPS_DIR`, con
`host/role-wait.sh` e `lib.sh`) dal checkout da cui girano, e da `main` il progetto avrebbe il
loop del tag con le sveglie di `main`. Una correzione di `ops/`, o una sezione di `migrate.md`
che dice "per ogni progetto dell'host", arrivata su `main` dopo il tag raggiunge il progetto
solo con il tag successivo, con la procedura di [Un progetto con la sorgente a un tag
nuovo](host/migrate.md#un-progetto-con-la-sorgente-a-un-tag-nuovo-666). `check-host.sh`
confronta le copie con quelle del checkout da cui gira, e con `SOURCE_REPO` avvisa quando quel
checkout non è al tag `SOURCE_REF` (o non ha il tag). Il repository di devloop, senza
`SOURCE_REPO`, resta su `main`.

**devloop nel repository gestito**, come il repository di devloop stesso (il progetto ssdlc):

1. **Portare devloop nel repository** (con una PR, come ogni cambiamento):
   - `devloop/` (con i prompt, `devloop/prompts/`, e gli schemi, `devloop/schemas/`);
   - `core/`, che devloop importa per il budget;
   - `scripts/devloop-pinned` (il launcher), `scripts/agent-token.py` (le scritture delle
     sessioni con le app del progetto, attraverso `devloop token`) e
     `scripts/supervisor-token.py` (quelle del supervisore con la sua app);
   - i file che `review_policy.sensitive_path_lists` elenca, se il progetto ne ha (ssdlc non ne
     ha più da ADR 0007, R1): `pin` li estrae e rifiuta una copia in cui mancano;
   - `ops/`, per ricreare l'installazione;
   - le istruzioni per gli agent (per esempio un `AGENTS.md`): i prompt di `devloop/prompts/`
     chiedono di leggere quelle elencate in `[prompts]` del file di progetto (ADR 0006, R4),
     senza modificare i prompt.
2. **Configurare `devloop/config.toml`** ([docs/devloop/reference.md](../docs/devloop/reference.md)
   descrive ogni chiave):
   - `base_branch`, `branch_prefix`, `language`. `repo`, `trusted_users` e `[apps]` non stanno
     in `devloop/config.toml` ma nell'overlay dello `state_dir` sull'host (ADR 0006, R6);
   - `test_cmd` con la suite del progetto. `local_tests = "changed"` vuole una selezione in
     `[tests]` (oggi `selection = "python-unittest-modules"`, che presuppone i moduli
     `tests/test_*.py` di `unittest`, con le regole di nome del progetto come parametri); senza,
     vale `"full"`. `ci_rerun_unrelated` vuole anche `[tests] ci_log_format = "run-tests"` (le
     righe `FAILED MODULE` di `scripts/run-tests.py`); senza, è spento
     ([docs/devloop/reference.md](../docs/devloop/reference.md#local-tests));
   - nell'overlay, `[apps]`: `app_id` e `slug` delle app del progetto, e la `key_dir` del
     progetto. Le app sono, a scelta di chi installa, un insieme nuovo creato al passo 2 con un
     `APP_PREFIX` nuovo (il default), oppure un insieme condiviso con altri repository dello
     stesso proprietario sullo stesso host, con i limiti di [Insieme di app per repository o
     condiviso](#insieme-di-app-per-repository-o-condiviso);
   - `[roles]` e `[agents.*]`: implementer, reviewer, supervisore e modelli;
   - `protected_paths`, `review_policy.sensitive_paths` e `sensitive_path_lists`,
     `merge.operator_only_paths` e `[docs.map]`, con i percorsi del progetto;
   - i check della CI in `[review_policy.zero] checks`, se si usa il livello 0.
3. **Parametri**: un `ops/params.env` con `REPO`, `APP_PREFIX`, utenti e percorsi del progetto.
   Sulla stessa macchina di un altro progetto, con `PROJECT` ([Più progetti sullo stesso
   host](#più-progetti-sullo-stesso-host)): ogni dispatcher ha il suo utente, il suo clone, il suo
   `state_dir`, le sue chiavi e il suo servizio.
4. **GitHub**: app nuove dai manifest ([github/create-apps.html](github/create-apps.html) o
   `create-apps.sh`), installate solo sul nuovo repository, oppure le app di un insieme condiviso
   installate anche lì (sotto); un ruleset con i check della sua CI. Nessun token personale per i
   ruoli: leggono con le loro app (#643).
5. **Host**: i passi 4-9 sopra, con quei parametri (gli script dell'host con
   `setup-project.sh`).
6. **Label**: devloop lavora sulle issue con `agent:ready` e usa le label descritte nella sua
   guida; crearle sul nuovo repository se mancano.

### Insieme di app per repository o condiviso

L'insieme di GitHub App di un progetto si sceglie per progetto, da chi installa (ADR 0006, D3 e
la sua precisazione del 2026-11-23, #570):

- **insieme per repository**, il default di questa guida e della guida rapida: app nuove dai
  manifest con un `APP_PREFIX` proprio, installate solo su quel repository;
- **insieme condiviso**: lo stesso insieme di app installato su più repository, ammesso solo fra
  repository dello **stesso proprietario** serviti dallo **stesso host**. Un altro proprietario,
  o un altro host, vuole un insieme a parte.

Il repository di devloop, sorgente dell'installazione degli altri progetti, ha un insieme suo, non
condiviso con i progetti che serve; per gli altri repository di un operatore un insieme condiviso
fra loro è una scelta ragionevole. In entrambi i modi devloop conia ogni token d'installazione
limitato al solo repository del progetto. I limiti dell'insieme condiviso, da pesare prima di
sceglierlo:

- **la chiave vale per tutti i repository dell'installazione**: la stessa chiave privata sta
  nella `key_dir` di ogni progetto che usa l'insieme, e in quelle del supervisore e del
  coordinatore per le loro app. Chi la legge in un progetto, per un difetto dell'host o di
  devloop o per un accesso di root, conia token per tutti i repository dove l'app è installata:
  devloop limita i suoi token al repository del progetto, la chiave non ha limiti;
- **i permessi sono gli stessi ovunque**: un'app ha un solo insieme di permessi, e non si può
  dare a un repository meno di quanto serve a un altro con la stessa app;
- **un limite di richieste condiviso**: GitHub conta il limite orario delle richieste per
  installazione; più repository serviti dalla stessa installazione consumano lo stesso budget, e
  con molti progetti attivi il watch e i tick possono avvicinarsi al limite;
- **rotazione e revoca insieme**: una chiave compromessa si revoca per tutti i repository
  dell'insieme, e si fermano tutti i progetti finché la chiave nuova non è in ogni `key_dir`;
- **installazione allargata con un clic**: aggiungere un repository all'installazione dà subito
  all'app i suoi permessi anche lì, e un errore nella selezione dei repository vale per tutti i
  progetti dell'insieme;
- **registri meno distinti**: su GitHub le azioni di tutti i progetti portano lo stesso login
  (`<slug>[bot]`), e per capire da quale installazione di devloop viene un'azione serve guardare
  il repository;
- **mai fra host diversi**: una copia della chiave su un altro host moltiplica i posti da cui può
  uscire; resta anche "un repository, un host".

Con un insieme condiviso che esiste già:

1. nel file di parametri del progetto `APP_PREFIX` è il prefisso dell'insieme, qualunque sia il
   nome del repository: `setup-devloop.sh`, `setup-supervisor.sh`, `setup-coordinator.sh` e
   `check-host.sh` cercano le chiavi di `<APP_PREFIX><ruolo>`;
2. nessuna app nuova per quelle che l'insieme ha già. Se al progetto ne serve una che manca (per
   esempio `merger`), nella pagina delle app ([github/create-apps.html](github/create-apps.html))
   si segnano quelle che l'insieme ha, e la pagina crea solo le altre e dà il valore di `APPS`;
   oppure `APPS` nel file di parametri elenca solo quelle, e `create-apps.sh` genera la pagina con
   i pulsanti di quelle sole;
3. ogni app si installa anche sul repository nuovo: nelle impostazioni dell'app su GitHub,
   l'installazione del proprietario → *Only select repositories* → aggiungere il repository,
   tenendo gli altri. Mai *All repositories*: `devloop apps` lo segnala con un avviso. `merger`
   solo sui repository dove il merge di devloop è acceso: con un insieme condiviso si sceglie per
   app su quali repository installarla;
4. le chiavi di devloop si copiano dalla `key_dir` di un progetto che usa già l'insieme nella
   `key_dir` del progetto nuovo, mode 600, del proprietario giusto, senza stamparle, con
   `sudo install -o <utente> -g <utente> -m 600`. Quelle del supervisore e del coordinatore no:
   con `PROJECT` i loro utenti e le loro `key_dir` sono dell'insieme (#643), e le chiavi ci sono
   già ([Più progetti sullo stesso host](#più-progetti-sullo-stesso-host));
5. nell'overlay, `[apps]` ha gli stessi `app_id` e `slug` degli altri progetti dell'insieme, con
   la `key_dir` del progetto. Una chiave ruotata va poi in ogni `key_dir` dell'insieme.

## Più progetti sullo stesso host

Un host può servire più progetti (ADR 0006, R8), ciascuno con **il suo utente di sistema** e il suo
utente degli agent (decisione D6), quindi con la sua home, il suo clone, il suo `state_dir`, la sua
`key_dir` con le chiavi del suo insieme di app (D3) e il suo servizio. Un progetto di terzi sta
sullo stesso host di ssdlc solo così, con utenti separati.

**Un repository, un host.** Ogni repository ha un solo dispatcher, su un solo host. Il lock per
repository lo garantisce sull'host: ogni dispatcher prende all'avvio, senza attendere, il lock del
suo repository in `REPO_LOCK_DIR`, e si ferma con `another dispatcher is running on <repo>` se un
altro, con un altro `state_dir` o un altro utente, lo tiene già
([docs/devloop/guide.md](../docs/devloop/guide.md#several-projects-on-one-host)). Fra host diversi
non c'è esclusione: lo stesso repository non va configurato su due host.

**Parametri.** Un file per progetto (per esempio `ops/params-widget.env`), passato a ogni script
con `--params`, con `PROJECT` (minuscole, cifre e `-` interni, al più 16 caratteri; non `agent` e
non `…-agent`, il suffisso dell'utente degli agent). Con `PROJECT` i default diventano del
progetto:

| Parametro | Default con `PROJECT=widget` |
|---|---|
| `DEVLOOP_USER`, `AGENT_USER` | `devloop-widget`, `devloop-widget-agent` |
| `DEVLOOP_HOME`, `AGENT_HOME`, `DEVLOOP_CLONE`, `DEVLOOP_KEY_DIR` | nella home dell'utente del progetto |
| `SERVICE_NAME` | `devloop@widget`, l'unica forma ammessa |
| `SUDOERS_FILE`, `AGENT_SUDOERS_FILE` | `/etc/sudoers.d/devloop-supervisor-widget`, `/etc/sudoers.d/devloop-agent-widget` |
| `APP_SET` | `APP_PREFIX` senza il `-` finale (con `APP_PREFIX=acme-devloop-`, `acme-devloop`): il nome dell'insieme di app, al più 20 caratteri, da cui vengono gli utenti dei ruoli (#643); uguale per tutti i progetti dell'insieme |
| `SUPERVISOR_USER`, `COORDINATOR_USER` | `supervisor-acme-devloop`, `coordinator-acme-devloop` (`<ruolo>-<APP_SET>`): gli utenti dei ruoli, uno per ruolo e per insieme di app (#643, che precisa #629) |
| `SUPERVISOR_HOME`, `COORDINATOR_HOME` | `/home/supervisor-acme-devloop`, `/home/coordinator-acme-devloop` |
| `COORDINATION_GROUP` | `devloop-roles-widget`: il gruppo della casella del progetto, con i suoi due ruoli (#629) |
| `SUPERVISOR_KEY_DIR` | `/home/supervisor-acme-devloop/.ghapps`: le chiavi dell'insieme, una sola copia per tutti i progetti dell'insieme |
| `SUPERVISOR_CLONE` | `/home/supervisor-acme-devloop/devloop-widget`: il clone del supervisore per il progetto |
| `COORDINATOR_KEY_DIR` | `/home/coordinator-acme-devloop/.ghapps`: la chiave dell'app del coordinatore dell'insieme |
| `COORDINATOR_CLONE` | `/home/coordinator-acme-devloop/widget`: il clone del coordinatore per il progetto, da cui parte la sua sessione (#643); senza `PROJECT`, `/home/coordinator/<nome del repository>` |
| `COORDINATOR_SUDOERS_FILE`, `COORDINATOR_OPS_DIR` | `/etc/sudoers.d/devloop-coordinator-widget`, `/usr/local/lib/devloop-coordinator-widget` |
| `SUPERVISOR_OPS_DIR` | `/usr/local/lib/devloop-supervisor-widget`: la copia di root del risveglio del supervisore (#602) |
| `WATCHDOG_USER`, `WATCHDOG_OPS_DIR` | `role-watchdog-widget`, `/usr/local/lib/devloop-role-watchdog-widget`: il watchdog dei ruoli del progetto, istanza `devloop-role-watchdog@widget`, che entra solo nella casella del progetto (#602) |
| `SENTINEL_USER` | `role-sentinel-widget`: la sentinella dei ruoli a riposo del progetto, istanza `devloop-role-sentinel@widget`, dalla copia del watchdog, che entra solo nella casella del progetto (#684) |
| `SUPERVISOR_SOURCE_CLONE`, `COORDINATOR_SOURCE_CLONE` | con `SOURCE_REPO`, `/home/supervisor-acme-devloop/devloop-source-widget` e `/home/coordinator-acme-devloop/devloop-source-widget`: i clone della sorgente, al tag del progetto, da cui i due ruoli coniano i token delle loro app (#571) |
| `LAUNCHER_DIR` | `/usr/local/lib/devloop/widget`: l'avviatore del tag `SOURCE_REF` del progetto, che nessun altro progetto sovrascrive (#573) |

`REPO_LOCK_DIR`, `REPO_LOCK_GROUP` e `TMPFILES_FILE` sono gli stessi per tutti i progetti
dell'host, come `DEVLOOP_COMMAND` e `DEVLOOP_PROJECTS_DIR`. Un `params.env` senza `PROJECT`
continua a installare `devloop.service` come prima.

**Comando `devloop`** (#588). Ogni `setup-devloop.sh` scrive il file del suo progetto,
`params-<progetto>.env`, in `DEVLOOP_PROJECTS_DIR` (passo 4): con più progetti il comando vuole
`--project` prima del sottocomando, e senza si ferma ed elenca i progetti installati.

```bash
devloop --project widget status
devloop --project widget pin origin/main
devloop --project=widget follow issue-12
```

Rimuovere `params-<progetto>.env` toglie il progetto dal comando, senza toccare il servizio.

**Ruoli per progetto, utenti per insieme di app** (#629, #643; ADR 0002, precisazioni del
2026-11-24 [sui ruoli per progetto](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-24-ruoli-per-progetto-629)
e [sugli utenti per insieme di app](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-24-utenti-dei-ruoli-per-insieme-di-app-643)).
Supervisore e coordinatore servono un solo progetto: per progetto restano la sessione
(`devloop-<ruolo>-<progetto>`), la casella con il suo gruppo, lo `state_dir`, il clone del
repository gestito, i sudoers e la copia di root del coordinatore. Nessun canale fra i ruoli di
progetti diversi, nemmeno sullo stesso host: un progetto usa un altro progetto come chiunque, con
un'issue sul suo repository, e ne segue i rilasci.

L'**utente** di ciascun ruolo invece è **per insieme di app**: con un insieme condiviso la chiave
dell'app supervisore e quella dell'app coordinatore valgono per tutti i repository dell'insieme, e
utenti distinti per progetto dentro l'insieme proteggerebbero poco più della chiave condivisa.
Sono dell'insieme l'utente, con il nome di default `<ruolo>-<APP_SET>`, la `key_dir`, con una sola
copia della chiave, e il login della CLI (quindi una quota per insieme). Supervisore e
coordinatore restano due utenti distinti fra loro. Il costo dei ruoli di un progetto nuovo in un
insieme esistente scende così quasi a zero: nessun utente nuovo, nessun login, nessuna chiave da
copiare per i ruoli; `setup-supervisor.sh` e `setup-coordinator.sh` (o `setup-project.sh`) danno
all'utente dell'insieme le ACL, la casella, i sudoers e il clone del progetto nuovo.

**Il limite, dichiarato qui dove si sceglie l'insieme condiviso**: l'utente di un ruolo appartiene
ai gruppi delle caselle di tutti i progetti dell'insieme, e legge gli overlay di tutti. Che la
sessione di un progetto non legga la casella di un altro progetto dell'insieme resta una regola del
runbook, non un vincolo del sistema. Fra insiemi diversi la separazione resta del sistema, come
dal #629: gli utenti, le `key_dir` e i gruppi sono distinti. Il repository di devloop ha un
insieme suo e utenti suoi (`supervisor`, `coordinator`). Un host con utenti per progetto dello
stesso insieme li riunisce con
[host/migrate.md](host/migrate.md#utenti-dei-ruoli-per-insieme-di-app-643); uno con i ruoli
condivisi fra progetti di insiemi diversi li separa con
[host/migrate.md](host/migrate.md#utenti-dei-ruoli-per-progetto-629).

**Avviatore.** Con `SOURCE_REPO`, ogni progetto ha il suo avviatore, dal suo `SOURCE_REF`, in
`/usr/local/lib/devloop/<progetto>` (#573): prima era `/usr/local/lib/devloop` per tutti, e
l'ultimo `setup-devloop.sh` decideva l'avviatore di ogni progetto. Un progetto installato prima
tiene il percorso di prima scrivendo `LAUNCHER_DIR=/usr/local/lib/devloop` nei suoi parametri;
altrimenti, a servizio fermo, si rieseguono `setup-devloop.sh`, `setup-supervisor.sh`,
`setup-coordinator.sh` (se c'è il coordinatore) e `check-host.sh` con i suoi parametri, che
installano l'avviatore nel nuovo percorso e vi portano unità e sudoers. L'host che sviluppa questo
repository non ha `SOURCE_REPO`: il suo avviatore resta quello del clone.

**Valori scritti.** Ogni parametro scritto in `params.env` vale anche con `PROJECT`, al posto del
suo default. Così l'host che sviluppa questo repository è passato all'istanza `devloop@devloop`
(#563) tenendo i suoi utenti (`DEVLOOP_USER=devloop`, `AGENT_USER=devloop-agent`,
`SUPERVISOR_USER=supervisor`, `COORDINATOR_USER=coordinator`, senza nuovi login delle CLI) e le
sue `key_dir` (`~/.ghapps` di `devloop`, `supervisor` e `coordinator`), con
`SUPERVISOR_CLONE=/home/supervisor/devloop`; il clone di devloop è il default,
`/home/devloop/devloop`, e sudoers, casella e copia di root del coordinatore hanno i default di
`PROJECT` (l'esempio è in [params.env.example](params.env.example)). Dal #629 tiene scritto anche
il gruppo della casella, `COORDINATION_GROUP=devloop-roles`: gli utenti `supervisor` e
`coordinator` e il gruppo sono solo suoi, e gli altri progetti dell'host hanno i loro.

**Utenti distinti.** I nomi derivati da `PROJECT` non coincidono mai con l'utente di un altro
ruolo o di un altro progetto. Ogni script lo verifica anche sull'host: legge gli utenti delle altre
installazioni di devloop dalle loro unità (`User=`) e dai loro sudoers (`devloop-*`, compresi
quelli del coordinatore, `devloop-coordinator*`), e si ferma se uno degli utenti del progetto
(`DEVLOOP_USER`, `AGENT_USER`, `SUPERVISOR_USER`, `COORDINATOR_USER`, `WATCHDOG_USER`) vi ha già
un ruolo. Il supervisore e il coordinatore si condividono solo nello stesso ruolo e fra progetti
dello stesso insieme (#643): gli script lo verificano con lo stesso `app_id` in
`[apps.supervisor]`, o in `[apps.coordinator]` per il coordinatore, nell'overlay del progetto e in
quello dell'altro (trovato dal log della sua unità). Con `app_id` diversi si fermano sempre;
finché uno dei due overlay non dichiara l'app del ruolo, `setup-supervisor.sh`,
`setup-coordinator.sh` e `check-host.sh` si fermano, mentre un `--dry-run`, `setup-devloop.sh` e
`setup-agent-user.sh`, che non toccano gli utenti dei ruoli, avvisano soltanto. Un progetto senza
coordinatore non dà nulla al suo `COORDINATOR_USER`, che può avere il nome del coordinatore di un
altro progetto dell'insieme: la verifica del coordinatore vale solo se l'overlay lo dichiara, e in
`setup-coordinator.sh` e `start-role.sh coordinator`, che lo predispongono o lo avviano. I
sudoers sono di root: senza root (`--dry-run`) la verifica legge solo le unità. `check-host.sh` verifica anche che
nel gruppo della casella non ci sia nessun utente oltre ai due ruoli del progetto.
`setup-supervisor.sh` e `setup-coordinator.sh` si fermano anche se `SUPERVISOR_CLONE` o
`COORDINATOR_CLONE` è già un clone di un altro repository.

**Unità.** Con `PROJECT`, `setup-devloop.sh` installa l'unità template `devloop@.service`, comune
ai progetti, e i parametri del progetto (utente, clone, avviatore, log) nella drop-in
`devloop@<progetto>.service.d/project.conf`. Un'istanza senza drop-in non parte. Il supervisore ha
un sudoers per unità, con `systemctl start|stop|restart devloop@<progetto>` e lo stesso avviatore
dell'unità:

```bash
sudo bash ops/host/setup-project.sh --params ops/params-widget.env   # i quattro setup-*.sh
sudo bash ops/host/check-host.sh --params ops/params-widget.env
sudo systemctl start devloop@widget
sudo bash ops/host/start-role.sh --params ops/params-widget.env supervisor   # dopo il login dell'utente dell'insieme
```

**Lock per repository.** `setup-devloop.sh` aggiunge l'utente del progetto al gruppo
`devloop-repos` (vale dal prossimo avvio del servizio) e prepara la directory comune; l'overlay del
progetto ha `repo_lock_dir = "/run/devloop/repos"`. Il gruppo non dà accesso agli `state_dir`, e
gli utenti degli agent e il supervisore ne restano fuori. Un container di devloop (ADR 0006, R7)
monta la stessa directory dell'host.

**Verifica.** `check-host.sh` si esegue una volta per progetto, con i suoi parametri. Oltre alle
verifiche di sempre, controlla l'unità template e la drop-in del progetto, il lock per repository,
e che nessun'altra unità di devloop dell'host gestisca lo stesso repository.
