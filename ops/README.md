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
| **Coordinatore** | `coordinator` (`COORDINATOR_USER`, passo 10) | la sua app; sull'host solo lettura (`sudoers-coordinator`) | [coordinator.md](../docs/devloop/roles/coordinator.md) |
| **Supervisore** | `supervisor` (`SUPERVISOR_USER`, passo 7) | la sua app; sull'host i comandi di `sudoers-supervisor` | [supervisor.md](../docs/devloop/roles/supervisor.md) |
| **Agent dell'host** | quello dell'operatore che lo lancia | `sudo` sull'host; nessuna scrittura su GitHub; legge solo procedure e script di `main`, mai issue, PR o output degli agent | [host.md](../docs/devloop/roles/host.md) |

devloop (dispatcher, preflight, implementer, reviewer) gira come `devloop` e `devloop-agent` e
non ha un runbook: le sue istruzioni sono i prompt di `devloop/prompts/`.

**Avvio di una sessione.** `ops/host/start-role.sh` avvia la sessione di un ruolo agent in tmux,
con l'utente del ruolo e il suo runbook come istruzioni iniziali:

```bash
sudo bash ops/host/start-role.sh --dry-run supervisor     # stampa il comando senza avviarlo
sudo bash ops/host/start-role.sh supervisor
sudo bash ops/host/start-role.sh coordinator
sudo bash ops/host/start-role.sh host --cli claude --model claude-opus-5-5
sudo -u supervisor -H tmux attach -t devloop-supervisor   # per entrare nella sessione
```

- **Supervisore e coordinatore:** CLI e modello sono `kind` e `model` della tabella
  `[roles.supervisor]` o `[roles.coordinator]`, letta da `devloop/config.toml` del checkout con
  sopra l'overlay (`<state_dir>/config.local.toml`), come li compone devloop. Senza la tabella,
  o con `roles.supervisor` come chiave di un agent, lo script si ferma. La sessione gira con
  `SUPERVISOR_USER` nel suo clone (`SUPERVISOR_CLONE`) o con `COORDINATOR_USER` nella sua home.
- **Agent dell'host:** CLI e modello li sceglie l'operatore al lancio (`--cli`, `--model`). La
  sessione gira con l'utente dell'operatore che lancia lo script con `sudo` (`SUDO_USER`, quello
  che ha `sudo`), mai come root né come l'utente di un altro ruolo, nel checkout da cui parte lo
  script: va lanciato da un checkout di `main` aggiornato.
- La CLI si cerca in `~/.local/bin` dell'utente del ruolo, poi in `/usr/local/bin`, `/usr/bin` e
  `/bin`, e parte interattiva con il modello e il runbook intero come primo messaggio:
  `claude --model <modello> <runbook>`, `codex --model <modello> <runbook>`,
  `agy --model <modello> -i <runbook>`.
- La sessione si chiama `devloop-<ruolo>`; se esiste già, lo script si ferma e dice come
  entrarci. Serve `tmux` sull'host (`apt-get install tmux`).

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
    setup-supervisor.sh      utente supervisor, ACL di attraversamento su /home/<devloop>,
                             sudoers, clone, key_dir 700, impostazioni di Claude Code
    setup-coordinator.sh     utente coordinator (facoltativo), ACL di attraversamento su
                             /home/<devloop> e di lettura sull'overlay, key_dir 700, copia di
                             root di check-host.sh, sudoers in sola lettura
    devloop.service          unità systemd (con segnaposto @NOME@)
    devloop@.service         unità template, per più progetti sullo stesso host (PROJECT)
    devloop-project.conf     drop-in di un progetto per devloop@<progetto> (con segnaposto)
    tmpfiles-devloop-repos.conf  directory dei lock per repository, comune all'host
    sudoers-agent            sudoers con cui devloop avvia CLI e test come utente degli agent
    sudoers-supervisor       sudoers minimale del supervisore (con segnaposto @NOME@)
    sudoers-coordinator      sudoers del coordinatore, solo comandi di lettura (con segnaposto)
    config.local.toml.example  overlay di devloop, tutto commentato
    check-host.sh            verifica utenti, permessi (700/600), chiavi, ACL, sudoers, utente
                             degli agent, coordinatore, servizio, CLI e `devloop apps`;
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
| `coordinator` | issues: write, pull_requests: write, actions/checks/statuses: read | **nessun contents**; le letture per la diagnosi della CI; solo se c'è il coordinatore (passo 10) |

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
5. **Configurazione**: `app_id` e `slug` (non segreti), con la `key_dir`, in `[apps]`
   dell'overlay locale dello `state_dir` del progetto (`config.local.toml`; per ssdlc dalle chiavi
   di [`ssdlc/overlay.toml`](host/ssdlc/overlay.toml)), non in `devloop/config.toml`, che da
   ADR 0006, R6 non ha identità di progetto (sezione 4). L'app `supervisor` è in
   `[apps.supervisor]` (ADR 0005, P5-4): devloop non ne legge la
   chiave, che sta solo nella `key_dir` del supervisore. L'app `coordinator` è in
   `[apps.coordinator]` (#528): devloop non ne legge la chiave, che sta solo nella `key_dir` del
   coordinatore (passo 10). L'app `implementer` non è ancora in `[apps]`: si crea quando serve
   (ADR 0005, P5-3).

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
R3 e R6: lo script non li crea. L'operatore copia [`ssdlc/project.toml`](host/ssdlc/project.toml) in
`<state_dir>/project.toml` (mode 600, utente `devloop`) e aggiunge all'overlay le chiavi di
[`ssdlc/overlay.toml`](host/ssdlc/overlay.toml): i comandi di agy e, da R6, `repo`,
`trusted_users` e l'insieme di app di ssdlc (`[apps]`), che `devloop/config.toml` non ha più.
Senza `repo` e `trusted_users` nell'overlay devloop non parte; senza file di progetto devloop non parte e il
primo pin fallisce ([migrate.md](host/migrate.md#configurazione-di-ssdlc-nel-suo-file-di-progetto)).
Su un host già in servizio l'ordine conta: vedi [Migrazione di R3 su un host
attivo](host/migrate.md#migrazione-di-r3-su-un-host-attivo).

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
  root, né le directory stesse. `~/.local` e `~/.local/bin` sono di root, `755`;
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
verifica con `~/.local/bin/<cli> --version`.

Gli aggiornamenti automatici restano spenti: devloop non deve avviare un turno mentre una CLI
sostituisce il proprio eseguibile. Per Claude Code, `~/.claude/settings.json` con
`"autoUpdates": false` e `"env": {"DISABLE_AUTOUPDATER": "1"}` (lo scrive `setup-agent-user.sh`), e il
launcher esporta `DISABLE_AUTOUPDATER=1`. Le CLI si aggiornano a mano con il loop fermo, fra
`--unseal` e la nuova esecuzione dello script senza
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
  dell'azione (solo `issues` e `pull_requests`), con `--state-dir` sullo `state_dir` di devloop
  (`/home/devloop/ssdlc/.ssdlc/devloop`), dal cui overlay legge `repo` e `[apps.supervisor]`
  (ADR 0006, R6); prima passano da `scripts/agent-token.py`, con
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
servizio; che `devloop-agent` scriva le voci di `AGENT_CLI_WRITABLE`; `--version` di ogni CLI
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
Se il coordinatore è installato (passo 10) verifica anche il suo sudoers e la copia di root di
`check-host.sh`; altrimenti lo dice in una riga, senza errore.

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

### 10. Coordinatore (facoltativo)

Il coordinatore ([ADR 0002](../docs/adr/0002-ruoli-e-responsabilita.md#precisazione-del-2026-11-22-modello-a-quattro-ruoli-524-527))
ha sull'host **solo lettura**: un utente di sistema suo, `coordinator` (`COORDINATOR_USER`,
diverso da `devloop`, `devloop-agent` e `supervisor`), con un sudoers di soli comandi di lettura.
Si installa quando l'operatore assegna il ruolo; devloop non lo richiede.

```bash
sudo bash ops/host/setup-coordinator.sh --dry-run
sudo bash ops/host/setup-coordinator.sh
```

Lo script crea l'utente e la sua `key_dir` (`COORDINATOR_KEY_DIR`, `/home/coordinator/.ghapps`,
700, del coordinatore; con `PROJECT`, `~/.ghapps-<progetto>`), gli dà **solo l'attraversamento**
della home di devloop (`setfacl -m u:coordinator:--x /home/devloop`, come al supervisore: raggiunge
per percorso l'overlay dello `state_dir`, da cui `scripts/coordinator-token.py` legge `repo` e
`[apps.coordinator]`, ma non elenca la home, e `~/.ghapps`, `~/.claude` e `~/.codex` di devloop
restano 700), e la **lettura dell'overlay** con un'ACL, se manca (`setfacl -m u:coordinator:r--
<state_dir>/config.local.toml`, #554: l'overlay dell'operatore è di solito 600; il file di
progetto no, `coordinator-token.py` non lo legge), installa la **copia di root** di
`check-host.sh`, dei file che legge
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

- come `devloop`: `scripts/devloop-pinned status` (lo stesso comando dell'unità, come per il
  supervisore) e `/usr/bin/tail -n 200` o `-n 1000` di `<state_dir>/run.log`;
- come root: `/usr/bin/journalctl --no-pager -n 200` o `-n 1000 -u devloop`,
  `/usr/bin/systemctl status --no-pager devloop` e
  `/usr/bin/bash /usr/local/lib/devloop-coordinator/host/check-host.sh --read-only`.

Niente che modifichi: né `start`, `stop` o `restart` del servizio, né `pin`, `tick` o `watch`, né
un pager o una shell. Per esempio, come `coordinator`:

```bash
sudo -u devloop /usr/bin/python3 /home/devloop/ssdlc/scripts/devloop-pinned status
sudo -u devloop /usr/bin/tail -n 200 /home/devloop/ssdlc/.ssdlc/devloop/run.log
sudo /usr/bin/journalctl --no-pager -n 200 -u devloop
sudo /usr/bin/systemctl status --no-pager devloop
sudo /usr/bin/bash /usr/local/lib/devloop-coordinator/host/check-host.sh --read-only
```

**`check-host.sh --read-only`** fa le verifiche del passo 8 senza avviare nessuna CLI (né
`--version` né lo strumento di shell: nessun modello, nessuna quota, nessuna scrittura nelle
sessioni o nelle cache delle CLI), senza `devloop apps` e senza scrivere nulla; stampa solo righe
di stato. Delle CLI verifica che ci siano. Gira dalla directory `/` e con Python isolato, così
nessun file della directory di chi lo lancia entra nelle verifiche.

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
4. Il coordinatore, come utente `coordinator` e dal proprio clone, conia i token con
   `scripts/coordinator-token.py`, che legge `repo` e `[apps.coordinator]` dall'overlay dello
   `state_dir` di `--state-dir` e solo la propria chiave dalla `key_dir` di
   `DEVLOOP_APP_KEY_DIR`; il token arriva al comando solo in `GH_TOKEN`, mai stampato.
   **`DEVLOOP_APP_KEY_DIR` va sempre impostata** al valore di `COORDINATOR_KEY_DIR`
   (`/home/coordinator/.ghapps`; con `PROJECT`, `/home/coordinator/.ghapps-<progetto>`): senza,
   lo script cerca la chiave in `key_dir` di `[apps]` dell'overlay, che è quella di devloop
   (`DEVLOOP_KEY_DIR`), e non la trova. `setup-coordinator.sh` stampa il comando con i valori
   dell'installazione. Senza `--perm` il token ha `issues=write` e
   `pull_requests=write`; si possono chiedere solo `issues` e `pull_requests` (`read` o `write`) e
   `actions`, `checks`, `statuses` e `metadata` in lettura: ogni altro permesso è rifiutato.
   `scripts/agent-token.py` non espone l'app.

   ```bash
   DEVLOOP_APP_KEY_DIR=/home/coordinator/.ghapps python3 scripts/coordinator-token.py \
       --state-dir /home/devloop/ssdlc/.ssdlc/devloop -- gh issue comment <ISSUE> --body-file <FILE>
   DEVLOOP_APP_KEY_DIR=/home/coordinator/.ghapps python3 scripts/coordinator-token.py \
       --state-dir /home/devloop/ssdlc/.ssdlc/devloop --perm actions=read -- \
       gh run view <RUN> --log-failed
   ```

`check-host.sh` verifica la `key_dir` (700, del coordinatore) e che contenga solo la chiave
dell'app del coordinatore (600, del coordinatore), l'ACL `user:coordinator:--x` sulla home di
devloop, nessuna ACL per il coordinatore sulle directory private di devloop, l'ACL
`user:coordinator:r--` sull'overlay, e che il coordinatore legga l'overlay. Le prove di lettura
come un altro utente usano il `[ -r ]` di bash, che rispetta le ACL, mai `/usr/bin/test`: quello
di uutils, il default di Ubuntu 26.04, guarda solo i bit del modo (#554).

**Overlay sostituito.** Un `install -m 600`, un `cp` o un `rsync` che mette un file nuovo al posto
dell'overlay ne toglie le ACL, quella del coordinatore e quella del supervisore, se l'operatore
gliel'ha data. Dopo ogni sostituzione si riesegue `setup-coordinator.sh`, che rimette solo l'ACL
del coordinatore, e si verifica con `check-host.sh`
([host/migrate.md](host/migrate.md#utente-del-coordinatore-530)).

Poi, a mano: la CLI dell'agent del coordinatore e il suo login, come utente `coordinator`, e un
token di sola lettura per `gh` ([github/tokens.md](github/tokens.md)). Su un host già attivo i
passi sono in [host/migrate.md](host/migrate.md#utente-del-coordinatore-530).

## Migrazione su un'altra macchina

Vedi [host/migrate.md](host/migrate.md): cosa copiare dallo `state_dir` e dal supervisore, cosa
ricreare (copie fissate, login delle CLI, servizio), come trattare le chiavi delle app e in che
ordine, senza mai due dispatcher attivi sullo stesso repository.

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
devloop: `pin` prende il codice da un clone di ssdlc, a un suo tag
([docs/devloop/guide.md](../docs/devloop/guide.md#installation-source)). In `params.env`:
`REPO` del progetto, `SOURCE_REPO=brushtyler/ssdlc`, `SOURCE_REF` con il tag da fissare, e di
solito uno `STATE_DIR` assoluto. `setup-devloop.sh` clona la sorgente in `SOURCE_CLONE`
(`/home/devloop/devloop-source`) e installa l'avviatore dal tag `SOURCE_REF` in `LAUNCHER_DIR`
(`/usr/local/lib/devloop/devloop-pinned`, di root), fuori dalla copia fissata e dal clone gestito;
unità e sudoers lo chiamano con `--state-dir`. L'overlay nello `state_dir` ha `source`,
`state_dir` e le chiavi del progetto (`repo`, `trusted_users`, `base_branch`, `[apps]` con la
`key_dir` del progetto, …; ADR 0006, R6): lo script ricorda `source`, `state_dir` e `repo` finché
mancano. `[apps]` è l'insieme di app di quel repository, creato con il suo `APP_PREFIX` e
installato solo lì (D3); le sessioni scrivono con `scripts/agent-token.py --state-dir <state_dir>`
del clone della sorgente, cioè con `devloop token` della copia fissata. I fatti del progetto (`test_cmd`, `[docs.map]`, …) stanno nel file di
progetto `project.toml` dello stesso `state_dir` (ADR 0006, R3). Il primo pin si fa dal clone
della sorgente, con il file di progetto e l'overlay prima del comando:

```bash
sudo -u devloop -H sh -c 'cd ~/widget && PYTHONPATH=~/devloop-source python3 -P -m devloop \
    --project-config /var/lib/devloop/widget/project.toml \
    --local-config /var/lib/devloop/widget/config.local.toml pin v1.2.0'
```

Il pin dopo un merge resta spento: un nuovo tag si fissa a mano, con `devloop-pinned
--state-dir … pin <tag>`. I dati del progetto nei prompt (le istruzioni da leggere, il comando
di un solo modulo di test, le regole sugli ADR) stanno nella tabella `[prompts]` del file di
progetto (ADR 0006, R4, [docs/devloop/reference.md](../docs/devloop/reference.md#project-data-in-the-prompts)):
senza, i prompt tolgono le frasi che li citano. I test locali e la lettura della CI seguono
`[tests]` del file di progetto (R5): vale quanto sotto per `local_tests` e `ci_rerun_unrelated`.

**devloop nel repository gestito**, come ssdlc:

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
   - nell'overlay, `[apps]`: `app_id` e `slug` delle app create al passo 2, con un `APP_PREFIX`
     nuovo, e la `key_dir` del progetto;
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
| `COORDINATOR_SUDOERS_FILE`, `COORDINATOR_OPS_DIR` | `/etc/sudoers.d/devloop-coordinator-widget`, `/usr/local/lib/devloop-coordinator-widget` |

`SUPERVISOR_USER`, `COORDINATOR_USER`, `REPO_LOCK_DIR`, `REPO_LOCK_GROUP` e `TMPFILES_FILE` sono
gli stessi per tutti i progetti dell'host. Un `params.env` senza `PROJECT`, come quello di ssdlc, continua a installare
`devloop.service` come prima.

**Utenti distinti.** I nomi derivati da `PROJECT` non coincidono mai con l'utente di un altro
ruolo o di un altro progetto. Ogni script lo verifica anche sull'host: legge gli utenti delle altre
installazioni di devloop dalle loro unità (`User=`) e dai loro sudoers (`devloop-*`), e si ferma
se `DEVLOOP_USER` o `AGENT_USER` vi hanno già un ruolo, o se `SUPERVISOR_USER` vi è l'utente
devloop o degli agent. Solo il supervisore è lo stesso in ogni progetto, e così il coordinatore
(`COORDINATOR_USER`), che non deve essere l'utente di nessun ruolo di un'altra installazione: i
sudoers `devloop-coordinator*` non danno ruoli, e la verifica li salta. I sudoers sono di root:
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
