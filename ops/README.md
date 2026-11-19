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

## Contenuto

```
ops/
  README.md                  questa guida
  params.env.example         i parametri: copiarlo in ops/params.env (locale: git lo ignora)
  lib.sh                     funzioni comuni agli script (parametri, --dry-run, conferme)
  host/
    setup-devloop.sh         pacchetti, utente devloop, clone, state_dir e log, overlay d'esempio,
                             key_dir e ~/.claude, ~/.codex 700, impostazioni di Claude Code, unità
    setup-agent-user.sh      utente degli agent (devloop-agent): directory delle CLI, ACL su
                             state_dir, worktree e sessioni, sudoers dell'avvio degli agent
    setup-supervisor.sh      utente supervisor, ACL di attraversamento su /home/<devloop>,
                             sudoers, clone, key_dir 700, impostazioni di Claude Code
    devloop.service          unità systemd (con segnaposto @NOME@)
    devloop@.service         unità template, per più progetti sullo stesso host (PROJECT)
    devloop-project.conf     drop-in di un progetto per devloop@<progetto> (con segnaposto)
    tmpfiles-devloop-repos.conf  directory dei lock per repository, comune all'host
    sudoers-agent            sudoers con cui devloop avvia CLI e test come utente degli agent
    sudoers-supervisor       sudoers minimale del supervisore (con segnaposto @NOME@)
    config.local.toml.example  overlay di devloop, tutto commentato
    check-host.sh            verifica utenti, permessi (700/600), chiavi, ACL, sudoers, utente
                             degli agent, servizio, CLI e `devloop apps`
    migrate.md               cosa copiare dalla macchina vecchia e cosa ricreare, e la
                             configurazione di ssdlc nel suo file di progetto (ADR 0006, R3)
    ssdlc/project.toml       il file di progetto di ssdlc, d'esempio: l'operatore lo copia in
                             <state_dir>/project.toml
    ssdlc/overlay.toml       le chiavi che l'overlay di ssdlc riceve con R3 (comandi di agy)
    ssdlc/check-equivalence.py  confronta la configurazione prima e dopo R3 su un host
  github/
    apps/<ruolo>.json        manifest delle GitHub App (flusso "manifest" di GitHub)
    create-apps.sh           genera una pagina HTML locale che crea ciascuna app dal suo manifest
    ruleset-main.json        ruleset del branch principale
    tokens.md                token fine-grained da creare a mano
```

### Regole degli script

- **Parametri.** Leggono `ops/params.env` (o il file di `--params`). I valori finiscono in un
  sudoers e in un'unità systemd, quindi gli script accettano solo forme note (nomi utente, percorsi
  assoluti senza spazi, `owner/nome`) e si fermano su un valore diverso. `STATE_DIR` è
  `.ssdlc/devloop` del clone per default; può essere un altro percorso relativo al clone o un
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

## Da una VM vuota a devloop e supervisore attivi

I comandi si eseguono da un clone del repository dell'operatore (per esempio nella sua home), come
utente con `sudo`. Dove compare `brushtyler/ssdlc`, `devloop` o `supervisor`, valgono i valori di
`params.env`.

### 1. Parametri

```bash
cp ops/params.env.example ops/params.env
$EDITOR ops/params.env               # REPO, APP_PREFIX, utenti e percorsi
```

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
| `supervisor` | issues: write, pull_requests: write | **nessun contents** |

Nessuna app ha `workflows` o permessi di amministrazione. I permessi che devloop chiede per ogni
azione sono in `devloop/apps.py` (`IMPLEMENT`, `REVIEW`, `PR_COMMENT`, `ISSUE_COMMENT`, `MERGE`,
`RERUN`); un test (`tests/test_ops.py`) controlla che i manifest li coprano.

```bash
bash ops/github/create-apps.sh --dry-run            # le app e i loro permessi
bash ops/github/create-apps.sh --out ~/create-apps.html
```

La pagina generata ha un pulsante per app; va aperta nel browser con la sessione GitHub del
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
5. **Configurazione**: `app_id` e `slug` (non segreti) in `[apps]` di `devloop/config.toml`, con
   una PR. L'app `supervisor` è in `[apps.supervisor]` (ADR 0005, P5-4): devloop non ne legge la
   chiave, che sta solo nella `key_dir` del supervisore. L'app `implementer` non è ancora in
   `[apps]`: si crea quando serve (ADR 0005, P5-3).

Se una regola del branch principale limita chi può fare push o merge, aggiungere `merger` fra gli
ammessi.

### 3. Ruleset del branch principale

`ops/github/ruleset-main.json` è il ruleset attuale: niente cancellazione né force push; PR
obbligatoria, con merge `merge`, `squash` o `rebase`; check obbligatori `Unit Tests (Python 3.12)`
e `Isolated A5 Recovery Benchmark` di GitHub Actions (`integration_id` 15368); bypass solo per il
ruolo admin (`actor_id` 5); si applica al branch di default (`~DEFAULT_BRANCH`).

Lo applica l'operatore, con il proprio login `gh` di amministratore (non con il token del
supervisore). Per non crearne un doppione, si aggiorna quello che c'è:

```bash
REPO=brushtyler/ssdlc
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
crea l'utente `devloop` e il suo clone, `state_dir` con `run.log`, l'overlay d'esempio (solo se non
esiste), `~/.ghapps`, `~/.claude` e `~/.codex` con mode 700, `~/.claude/settings.json` con gli
aggiornamenti automatici spenti (solo se non esiste), e l'unità `devloop.service`. Non avvia il
servizio. Le CLI degli agent non sono di `devloop`: le prepara il passo 5.

Prepara anche il **lock per repository** (ADR 0006, R8): il gruppo `REPO_LOCK_GROUP`
(`devloop-repos`), con dentro l'utente `devloop`, e la directory comune `REPO_LOCK_DIR`
(`/run/devloop/repos`, di root, mode 3770), che `systemd-tmpfiles` ricrea a ogni avvio da
`TMPFILES_FILE` (`/etc/tmpfiles.d/devloop-repos.conf`). Non usa una directory che esiste già, non
è vuota e non è di `root:<gruppo>`. L'overlay deve avere `repo_lock_dir` con lo stesso valore: lo
script lo ricorda finché manca ([Più progetti sullo stesso host](#più-progetti-sullo-stesso-host)).

Per ssdlc servono anche il **file di progetto** e le chiavi che l'overlay riceve con l'ADR 0006,
R3: lo script non li crea. L'operatore copia [`ssdlc/project.toml`](host/ssdlc/project.toml) in
`<state_dir>/project.toml` (mode 600, utente `devloop`) e aggiunge all'overlay le chiavi di
[`ssdlc/overlay.toml`](host/ssdlc/overlay.toml); senza file di progetto devloop non parte e il
primo pin fallisce ([migrate.md](host/migrate.md#configurazione-di-ssdlc-nel-suo-file-di-progetto)).

Poi le **chiavi**: in `key_dir` di `devloop` (`/home/devloop/.ghapps`) quelle delle app che usa il
dispatcher (`DEVLOOP_KEYS`: `dispatcher`, `merger`, `claude-agent`, `codex-agent`, `agy-agent`):

```bash
sudo install -o devloop -g devloop -m 600 <slug>.<data>.private-key.pem /home/devloop/.ghapps/
```

### 5. Utente degli agent e CLI

Gli agent e i test non girano come `devloop`, che può leggere le chiavi delle app e lo
`state_dir`, ma come un utente proprio, `devloop-agent` (`AGENT_USER`, #415): il dispatcher li
avvia con `sudo -n -E -u devloop-agent`
([docs/devloop/guide.md](../docs/devloop/guide.md#agent-user)).

```bash
sudo bash ops/host/setup-agent-user.sh --dry-run
sudo bash ops/host/setup-agent-user.sh
```

Crea l'utente `devloop-agent`, i suoi `~/.claude` e `~/.codex` (700) con
`~/.claude/settings.json` senza aggiornamenti automatici, e `~/.local/bin`; poi le ACL:

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
(`!use_pty`). Rieseguire lo script dopo aver copiato file nello `state_dir`
([host/migrate.md](host/migrate.md)): le ACL delle voci già presenti sono sue.

Poi l'**overlay** deve avere `[agent_user] name = "devloop-agent"` (lo script lo segnala se
manca; `check-host.sh` lo considera un errore). All'avvio di `tick` e `run` devloop verifica come
`devloop-agent` che non legga né attraversi `key_dir` e le voci private dello `state_dir`, che non
legga l'overlay, che non scriva nulla in `.git` né le directory che contengono worktree e turni,
e che scriva nelle directory dei suoi turni; altrimenti non parte.

Le **CLI** e i loro login non sono automatizzati: il login è interattivo e ogni fornitore ha il
suo installatore. Tutto come utente `devloop-agent` (`sudo -iu devloop-agent`), con gli
eseguibili in `~/.local/bin`, che è il primo elemento del `secure_path` del sudoers:

| CLI | Installazione | Eseguibile |
|---|---|---|
| `claude` | `npm install -g --prefix ~/.local @anthropic-ai/claude-code` | `~/.local/bin/claude` |
| `codex` | installazione standalone del fornitore, in `~/.codex/packages/standalone` | raggiungibile come `~/.local/bin/codex` |
| `agy` | secondo le istruzioni del fornitore | `~/.local/bin/agy` |

Per ciascuna: login (`claude`, `codex`, `agy` avviati una volta in una sessione interattiva) e
verifica con `~/.local/bin/<cli> --version`.

Gli aggiornamenti automatici restano spenti: devloop non deve avviare un turno mentre una CLI
sostituisce il proprio eseguibile. Per Claude Code, `~/.claude/settings.json` con
`"autoUpdates": false` e `"env": {"DISABLE_AUTOUPDATER": "1"}` (lo scrive `setup-agent-user.sh`), e il
launcher esporta `DISABLE_AUTOUPDATER=1`. Le CLI si aggiornano a mano con il loop fermo
([docs/devloop/guide.md](../docs/devloop/guide.md#agent-clis)).

### 6. Overlay e primo pin

L'overlay `/home/devloop/ssdlc/.ssdlc/devloop/config.local.toml` contiene solo le chiavi che
l'operatore sovrascrive (merge mode, `[pin] auto`, `[run] alert_issue`, …); quello d'esempio è
tutto commentato. Su questo host ha anche `repo_lock_dir = "/run/devloop/repos"`. Accanto, il
file di progetto `/home/devloop/ssdlc/.ssdlc/devloop/project.toml`, copiato da
[`ssdlc/project.toml`](host/ssdlc/project.toml), e nell'overlay le chiavi di
[`ssdlc/overlay.toml`](host/ssdlc/overlay.toml) (vedi sopra). Poi il primo pin, che fa anche lo
smoke test. Lo esegue il devloop del clone, non il launcher: file di progetto e overlay si passano
a mano, prima del comando, perché dall'ADR 0006, R3 `devloop/config.toml` non ha `test_cmd`:

```bash
sudo -u devloop -H sh -c 'cd ~/ssdlc && python3 -m devloop \
    --project-config .ssdlc/devloop/project.toml \
    --local-config .ssdlc/devloop/config.local.toml pin origin/main'
```

### 7. Host del supervisore

```bash
sudo bash ops/host/setup-supervisor.sh --dry-run
sudo bash ops/host/setup-supervisor.sh
```

Crea l'utente `supervisor`, gli dà **solo l'attraversamento** della home di devloop
(`setfacl -m u:supervisor:--x /home/devloop`: raggiunge i log per percorso, ma non elenca la home,
e `~/.ghapps`, `~/.claude` e `~/.codex` di devloop restano 700), installa il sudoers minimale dopo
averlo validato con `visudo -c`, clona il repository e prepara `key_dir` e `~/.claude` (700).

Il **sudoers** (`ops/host/sudoers-supervisor`) permette:

- come `devloop`, solo `scripts/devloop-pinned` con `status`, `status *`, `watch *`, `report *`,
  `tick --dry-run` e `pin *` (con `Defaults>devloop always_set_home, secure_path=…`); con uno
  `STATE_DIR` diverso dal default o con `SOURCE_REPO`, lo stesso comando dell'unità
  (`/usr/bin/env -C <clone> /usr/bin/python3 <avviatore> --state-dir <state_dir>`);
- come root, solo `systemctl start|stop|restart devloop` e `systemctl status devloop *`.

Nessun comando scrive sull'overlay di devloop. Per esempio:

```bash
sudo -u devloop /usr/bin/python3 /home/devloop/ssdlc/scripts/devloop-pinned status
sudo /usr/bin/systemctl restart devloop
tail -f /home/devloop/ssdlc/.ssdlc/devloop/run.log
```

Poi, a mano:

- **chiavi** in `key_dir` del supervisore (`/home/supervisor/.ghapps`, 600): solo `supervisor` e,
  finché P5-4 non è attivo, `claude-agent`. Con P5-4 attivo (`[roles.supervisor]` come tabella
  nell'overlay di devloop), le scritture del supervisore passano da
  `scripts/supervisor-token.py`, che conia un token dell'app `supervisor` con i soli permessi
  dell'azione (solo `issues` e `pull_requests`); prima passano da `scripts/agent-token.py`, con
  l'attore `claude`, che non espone l'app `supervisor`;
- **token di sola lettura** per `gh` ([github/tokens.md](github/tokens.md));
- **Claude Code** come utente `supervisor`, con lo stesso `npm install` del passo 5 e il login;
- gli strumenti del diario (`~/supervisor`) e la memoria di Claude Code non vengono dal
  repository: su una macchina nuova si copiano da quella vecchia ([host/migrate.md](host/migrate.md)).

### 8. Verifica

```bash
sudo bash ops/host/check-host.sh             # esce con 1 se trova errori
sudo bash ops/host/check-host.sh --no-apps   # senza interrogare GitHub
```

Verifica gli utenti; `key_dir`, `~/.claude` e `~/.codex` con mode 700 e il proprietario giusto;
ogni chiave `*.pem` con mode 600 e del suo utente, una chiave per app attesa e, nella `key_dir`
del supervisore, nessuna chiave in più; l'ACL `user:supervisor:--x` sulla home di devloop e
nessuna ACL per il supervisore sulle sue directory private; che il supervisore legga `run.log`;
i due sudoers (440, root, accettati da `visudo`, uguali a quelli generati); che `devloop-agent` non
legga `key_dir`, lo `state_dir` e l'overlay, non attraversi `key_dir`, non legga né attraversi le
altre voci dello `state_dir`, entri senza scriverle in `worktrees`, `agent-shared`,
`agent-shared/schemas`, `agent-shared/turns` e `agent-shared/tmp`, non scriva nulla in `.git`,
e che l'overlay abbia `[agent_user] name = "devloop-agent"`; il lock per repository (la directory
di root, 3770, del gruppo `devloop-repos`; `devloop` nel gruppo, `devloop-agent` e il supervisore
fuori; `devloop-agent` che non vi entra; il file di `tmpfiles.d`; `repo_lock_dir` nell'overlay);
l'unità (uguale a quella generata, `KillMode=mixed`; con `PROJECT` l'unità template e la drop-in),
che nessun'altra unità di devloop dell'host gestisca lo stesso repository, e lo stato del
servizio; `--version` di ogni CLI come
`devloop-agent`; e `devloop apps`
(ogni app, la sua installazione e i suoi permessi). Con `--dry-run` stampa, senza eseguirle, le
verifiche che passano da `sudo` o dalla rete.

Poi, come in [docs/devloop/guide.md](../docs/devloop/guide.md#installation-as-a-dedicated-user):

```bash
sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned status
sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned tick --dry-run
```

### 9. Avvio

L'attivazione è dell'operatore: il loop spende le quote degli agent.

```bash
sudo systemctl start devloop
sudo systemctl enable devloop       # se deve ripartire al boot
sudo -u devloop -H tail -f /home/devloop/ssdlc/.ssdlc/devloop/run.log
```

L'unità (`ops/host/devloop.service`) è quella dell'installazione attuale: utente `devloop`,
`WorkingDirectory` sul clone, `PYTHONUNBUFFERED=1`, il `PATH` con `~/.local/bin` prima,
`ExecStart` sul launcher della copia fissata, stdout e stderr in append su `<state_dir>/run.log`,
`KillMode=mixed` (SIGTERM solo al processo principale: devloop smette di pianificare e aspetta le
azioni in corso, fino a `agent_timeout_seconds`), `TimeoutStopSec=4200`, `Restart=on-failure`
con `RestartSec=60`, e `RestartPreventExitStatus=4` (dopo l'allarme di tre tick falliti il loop
resta fermo, come descritto in *Continuous loop* della guida di devloop).

## Migrazione su un'altra macchina

Vedi [host/migrate.md](host/migrate.md): cosa copiare dallo `state_dir` e dal supervisore, cosa
ricreare (copie fissate, login delle CLI, servizio), come trattare le chiavi delle app e in che
ordine, senza mai due dispatcher attivi sullo stesso repository.

## Un altro progetto

Per default devloop lavora sul repository che lo contiene: `pin` estrae devloop dallo stesso
repository di cui crea i worktree. Ci sono due strade.

**Sorgente dell'installazione a parte** (ADR 0006, R1). Il repository gestito non contiene
devloop: `pin` prende il codice da un clone di ssdlc, a un suo tag
([docs/devloop/guide.md](../docs/devloop/guide.md#installation-source)). In `params.env`:
`REPO` del progetto, `SOURCE_REPO=brushtyler/ssdlc`, `SOURCE_REF` con il tag da fissare, e di
solito uno `STATE_DIR` assoluto. `setup-devloop.sh` clona la sorgente in `SOURCE_CLONE`
(`/home/devloop/devloop-source`) e installa l'avviatore dal tag `SOURCE_REF` in `LAUNCHER_DIR`
(`/usr/local/lib/devloop/devloop-pinned`, di root), fuori dalla copia fissata e dal clone gestito;
unità e sudoers lo chiamano con `--state-dir`. L'overlay nello `state_dir` ha `source`,
`state_dir` e le chiavi del progetto (`repo`, `base_branch`, `[apps]`, …): lo script ricorda le
prime due finché mancano. I fatti del progetto (`test_cmd`, `[docs.map]`, …) stanno nel file di
progetto `project.toml` dello stesso `state_dir` (ADR 0006, R3). Il primo pin si fa dal clone
della sorgente, con il file di progetto e l'overlay prima del comando:

```bash
sudo -u devloop -H sh -c 'cd ~/widget && PYTHONPATH=~/devloop-source python3 -P -m devloop \
    --project-config /var/lib/devloop/widget/project.toml \
    --local-config /var/lib/devloop/widget/config.local.toml pin v1.2.0'
```

Il pin dopo un merge resta spento: un nuovo tag si fissa a mano, con `devloop-pinned
--state-dir … pin <tag>`. Prompt, test locali e lettura della CI sono ancora quelli di ssdlc
(issue R2-R5 dell'ADR 0006): vale quanto sotto per `local_tests` e `ci_rerun_unrelated`.

**devloop nel repository gestito**, come ssdlc:

1. **Portare devloop nel repository** (con una PR, come ogni cambiamento):
   - `devloop/` (con i prompt, `devloop/prompts/`, e gli schemi, `devloop/schemas/`);
   - `core/`, che devloop importa per il budget;
   - `scripts/devloop-pinned` (il launcher) e `scripts/agent-token.py` (le scritture del
     supervisore con la sua app);
   - `schemas/`, almeno i file che `review_policy.sensitive_path_lists` elenca: `pin` li estrae e
     rifiuta una copia in cui mancano;
   - `ops/`, per ricreare l'installazione;
   - un `AGENTS.md` per gli agent: i prompt di `devloop/prompts/` chiedono di leggerlo, e citano la
     sezione "Developing SSDLC" del README: adattarli al progetto.
2. **Configurare `devloop/config.toml`** ([docs/devloop/reference.md](../docs/devloop/reference.md)
   descrive ogni chiave):
   - `repo`, `base_branch`, `branch_prefix`, `language`, `trusted_users`;
   - `test_cmd` con la suite del progetto. `local_tests = "changed"` e `ci_rerun_unrelated`
     presuppongono la struttura di questo repository (`tests/test_*.py`, le righe `FAILED MODULE`
     di `scripts/run-tests.py`): altrove usare `local_tests = "full"` o `"none"` e
     `ci_rerun_unrelated = false`;
   - `[apps]`: `app_id` e `slug` delle app create al passo 2, con un `APP_PREFIX` nuovo;
   - `[roles]` e `[agents.*]`: implementer, reviewer, supervisore e modelli;
   - `protected_paths`, `review_policy.sensitive_paths` e `sensitive_path_lists`,
     `merge.operator_only_paths` e `[docs.map]`, con i percorsi del progetto;
   - i check della CI in `[review_policy.zero] checks`, se si usa il livello 0.
3. **Parametri**: un `ops/params.env` con `REPO`, `APP_PREFIX`, utenti e percorsi del progetto.
   Sulla stessa macchina di un altro progetto, con `PROJECT` ([Più progetti sullo stesso
   host](#più-progetti-sullo-stesso-host)): ogni dispatcher ha il suo utente, il suo clone, il suo
   `state_dir`, le sue chiavi e il suo servizio.
4. **GitHub**: app nuove dai manifest (`create-apps.sh`), installate solo sul nuovo repository;
   un ruleset con i check della sua CI; un token di sola lettura per il suo supervisore.
5. **Host**: i passi 4-9 sopra, con quei parametri.
6. **Label**: devloop lavora sulle issue con `agent:ready` e usa le label descritte nella sua
   guida; crearle sul nuovo repository se mancano.

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
| `SUPERVISOR_KEY_DIR` | `/home/supervisor/.ghapps-widget`: le chiavi dell'insieme di app del progetto |
| `SUPERVISOR_CLONE` | `/home/supervisor/devloop-widget`: il clone del supervisore per il progetto |

`SUPERVISOR_USER`, `REPO_LOCK_DIR`, `REPO_LOCK_GROUP` e `TMPFILES_FILE` sono gli stessi per tutti i
progetti dell'host. Un `params.env` senza `PROJECT`, come quello di ssdlc, continua a installare
`devloop.service` come prima.

**Utenti distinti.** I nomi derivati da `PROJECT` non coincidono mai con l'utente di un altro
ruolo o di un altro progetto. Ogni script lo verifica anche sull'host: legge gli utenti delle altre
installazioni di devloop dalle loro unità (`User=`) e dai loro sudoers (`devloop-*`), e si ferma
se `DEVLOOP_USER` o `AGENT_USER` vi hanno già un ruolo, o se `SUPERVISOR_USER` vi è l'utente
devloop o degli agent. Solo il supervisore è lo stesso in ogni progetto. I sudoers sono di root:
senza root (`--dry-run`) la verifica legge solo le unità. `setup-supervisor.sh` si ferma anche se
`SUPERVISOR_CLONE` è già un clone di un altro repository.

**Unità.** Con `PROJECT`, `setup-devloop.sh` installa l'unità template `devloop@.service`, comune
ai progetti, e i parametri del progetto (utente, clone, avviatore, log) nella drop-in
`devloop@<progetto>.service.d/project.conf`. Un'istanza senza drop-in non parte. Il supervisore ha
un sudoers per unità, con `systemctl start|stop|restart devloop@<progetto>` e lo stesso avviatore
dell'unità:

```bash
sudo bash ops/host/setup-devloop.sh --params ops/params-widget.env
sudo bash ops/host/setup-agent-user.sh --params ops/params-widget.env
sudo bash ops/host/setup-supervisor.sh --params ops/params-widget.env
sudo bash ops/host/check-host.sh --params ops/params-widget.env
sudo systemctl start devloop@widget
```

**Lock per repository.** `setup-devloop.sh` aggiunge l'utente del progetto al gruppo
`devloop-repos` (vale dal prossimo avvio del servizio) e prepara la directory comune; l'overlay del
progetto ha `repo_lock_dir = "/run/devloop/repos"`. Il gruppo non dà accesso agli `state_dir`, e
gli utenti degli agent e il supervisore ne restano fuori. Un container di devloop (ADR 0006, R7)
monta la stessa directory dell'host.

**Verifica.** `check-host.sh` si esegue una volta per progetto, con i suoi parametri. Oltre alle
verifiche di sempre, controlla l'unità template e la drop-in del progetto, il lock per repository,
e che nessun'altra unità di devloop dell'host gestisca lo stesso repository.
