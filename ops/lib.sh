# shellcheck shell=bash
# Funzioni comuni agli script di ops/: parametri (params.env), --dry-run, conferme, piattaforma.
# Si include con `. "$OPS_DIR/lib.sh"`; non si esegue da solo.
#
# Regole per chi scrive uno script di ops/:
# - ogni azione che cambia qualcosa passa da `run`, `as_user` o `put_file`: con --dry-run la
#   stampano senza farla;
# - prima di agire si controlla lo stato (utente, directory, file): rieseguire non cambia nulla;
# - nessun segreto: gli script non leggono, non scrivono e non stampano chiavi o token.

set -euo pipefail

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DRY_RUN=0
ASSUME_YES=0
PARAMS_FILE="$OPS_DIR/params.env"
OPS_SHIFT=1
FAILURES=0
WARNINGS=0

# La piattaforma su cui gli script sono stati verificati.
VERIFIED_ID=ubuntu
VERIFIED_VERSION=26.04
VERIFIED_ARCH=aarch64

# Lo state_dir di devloop, relativo al clone, che scripts/devloop-pinned usa senza --state-dir
# (.devloop dal #563; mai sotto .ssdlc/, la directory del vecchio motore SSDLC).
OPS_STATE_DIR=.devloop

# I segnaposto @NOME@ che `render` sostituisce nei file dichiarativi (unità, sudoers).
OPS_TEMPLATE_VARS='REPO DEVLOOP_USER SUPERVISOR_USER AGENT_USER COORDINATOR_USER DEVLOOP_HOME AGENT_HOME DEVLOOP_CLONE STATE_DIR STATE_PATH LAUNCHER_CMD DEVLOOP_PATH AGENT_PATH SERVICE_NAME REPO_LOCK_DIR REPO_LOCK_GROUP COORDINATOR_OPS_DIR COORDINATION_DIR WATCHDOG_USER WATCHDOG_OPS_DIR SENTINEL_USER DEVLOOP_PROJECTS_DIR DEVLOOP_COMMAND OPS_INSTANCE'

# I file di ops/ che check-host.sh legge (#530): setup-coordinator.sh li copia, con i parametri
# (params.env), nella directory di root COORDINATOR_OPS_DIR, da cui il coordinatore esegue come
# root `check-host.sh --read-only`, mai dal clone di devloop né da un checkout scrivibile. Con
# loro il risveglio dei ruoli (role-wait.sh, #602) e il suo involucro coordinator-wait.sh (#543).
OPS_COORDINATOR_FILES='lib.sh host/check-host.sh host/role-wait.sh host/coordinator-wait.sh host/role-watchdog.sh host/role-sentinel.sh host/sudoers-supervisor host/sudoers-agent host/sudoers-coordinator host/tmpfiles-devloop-repos.conf host/devloop.service host/devloop@.service host/devloop-project.conf host/devloop-role-watchdog@.service host/devloop-role-watchdog@.timer host/devloop-role-watchdog-failed@.service host/role-watchdog-project.conf host/devloop-role-sentinel@.service host/devloop-role-sentinel-failed@.service host/role-sentinel-project.conf host/devloop-command host/devloop-command-project host/devloop-failed@.service'
# Il risveglio del supervisore (#602): setup-supervisor.sh copia questi file, con i parametri,
# nella directory di root SUPERVISOR_OPS_DIR, da cui il supervisore lo esegue.
OPS_SUPERVISOR_FILES='lib.sh host/role-wait.sh'
# Il watchdog dei ruoli (#602): setup-supervisor.sh e setup-coordinator.sh copiano questi file, con
# i parametri del progetto, nella directory di root WATCHDOG_OPS_DIR, da cui lo esegue l'istanza
# devloop-role-watchdog@<progetto>; le unità template (OPS_WATCHDOG_UNITS) vanno in SYSTEMD_DIR,
# una volta per host, con una drop-in per istanza (host/role-watchdog-project.conf).
# Con il watchdog, dalla stessa copia, la sentinella dei ruoli a riposo (#684): l'istanza
# devloop-role-sentinel@<progetto>, con le sue unità template (OPS_SENTINEL_UNITS) e la sua
# drop-in (host/role-sentinel-project.conf).
OPS_WATCHDOG_FILES='lib.sh host/role-watchdog.sh host/role-sentinel.sh'
OPS_WATCHDOG_UNITS='devloop-role-watchdog@.service devloop-role-watchdog@.timer devloop-role-watchdog-failed@.service'
OPS_SENTINEL_UNITS='devloop-role-sentinel@.service devloop-role-sentinel-failed@.service'

# La casella fra supervisore e coordinatore (#543): sotto COORDINATION_DIR una directory di
# messaggi per destinatario, ciascuna con la sua done/ per i messaggi letti. Tutte di root, del
# gruppo COORDINATION_GROUP (supervisore e coordinatore, nessun altro), 2770.
COORDINATION_BOXES='to-coordinator to-supervisor'
# La cartella delle comunicazioni dei ruoli all'operatore (#663), accanto alle due caselle, con la
# sua done/ e gli stessi permessi: i ruoli ci scrivono, l'operatore la legge dalla sua shell.
# Non è fra COORDINATION_BOXES: il watchdog dei ruoli non ci scrive.
COORDINATION_OPERATOR_BOX=to-operator
# I battiti dei ruoli (#602): COORDINATION_DIR/heartbeat/<ruolo>, che role-wait.sh scrive a ogni
# avvio e il watchdog dei ruoli legge; la directory è come le caselle, senza done/.
COORDINATION_HEARTBEAT=heartbeat
# Il riposo dei ruoli (#684): COORDINATION_DIR/rest/, di root, del gruppo dei ruoli, 3770 (lo
# sticky bit: ognuno rimuove o sostituisce solo i suoi file). Dentro, di ciascun ruolo, lo stato
# di riposo <ruolo> ("<inizio> <motivo>", che role-wait.sh --rest scrive e role-wait.sh toglie),
# il file del suo lock <ruolo>.lock (tenuto con flock da role-wait.sh --rest mentre aspetta) e,
# della sentinella, il segnale di risveglio <ruolo>.wake ("<ora> <motivo>").
COORDINATION_REST=rest

# Il flusso degli eventi (#683), come devloop/events.py: la directory dello state_dir che devloop
# run scrive (DIRECTORY), con i segmenti, il segno di vita (ALIVE) e l'identità del flusso. I
# ruoli la leggono come sé stessi, con l'ACL di sola lettura che setup-supervisor.sh e
# setup-coordinator.sh mettono (ensure_events_acl).
EVENTS_DIR=events
EVENTS_ALIVE=alive
# Il lavoro trattenuto (#684), HOLD_FILE di devloop/dispatcher.py: devloop run lo scrive nella
# directory del flusso mentre non avvia un controllo di prontezza o un'implementazione perché un
# ruolo è a riposo ("<prima volta> <ruoli> <azioni>"), e lo toglie quando non trattiene più nulla.
# La sentinella sveglia i ruoli a riposo; il watchdog lo segnala dopo REST_WAKE_TIMEOUT.
EVENTS_HOLD=hold

# Nella home dell'utente degli agent (#438), come CLI_DIRS, CLI_SEALED e CLIS di
# devloop/agentuser.py: le directory delle CLI, di root, del gruppo dell'utente degli agent e con
# lo sticky bit (1770, #517), dove l'utente degli agent crea e sostituisce le proprie voci ma non
# rinomina né rimuove quelle di root; ciò che porta istruzioni, configurazione, plugin o binari,
# dove non scrive nulla, con un segnaposto di root dove non esiste (un file se il nome ha
# un'estensione, altrimenti una directory); le CLI di ~/.local/bin. Un turno non cambia ciò che
# usano i turni dopo.
# ~/.gemini/config è di agy: config.json abilita i plugin, mcp_config.json elenca i server MCP che
# carica, projects i suoi progetti, tutti sigillati; resta dell'utente degli agent solo .migrated.
AGENT_CLI_DIRS=(.claude .codex .gemini .gemini/config .gemini/antigravity-cli)
AGENT_CLI_SEALED=(
    .claude/settings.json .claude/CLAUDE.md .claude/rules .claude/agents .claude/commands
    .claude/skills .claude/hooks .claude/output-styles .claude/plugins
    .codex/config.toml .codex/AGENTS.md .codex/AGENTS.override.md .codex/prompts
    .codex/rules .codex/skills .codex/packages .codex/plugins .codex/memories
    .gemini/settings.json .gemini/GEMINI.md .gemini/config/config.json
    .gemini/config/mcp_config.json .gemini/config/projects
    .gemini/antigravity-cli/settings.json
    .local
)
AGENT_CLIS=(claude codex agy)
# Le sole voci che le CLI possono tenere nelle directory di AGENT_CLI_DIRS (#517), oltre a quelle
# sigillate e alle directory delle CLI, di root: <directory>/<modello del nome>:<tipi>, come
# CLI_ALLOWED di devloop/agentuser.py (f un file, d una directory, l un link: solo una voce di
# AGENT_CLI_LINKS, con la sua regola). Una voce che manca qui si
# aggiunge con una PR, in entrambe le liste; l'overlay può solo toglierne ([agent_user]
# cli_denied).
AGENT_CLI_ALLOWED=(
    .claude/.credentials.json:f '.claude/.credentials.json.*:f' .claude/backups:d .claude/cache:d
    .claude/projects:d .claude/sessions:d .claude/todos:d .claude/shell-snapshots:d
    .claude/statsig:d .claude/plans:d .claude/file-history:d .claude/session-env:d
    .claude/debug:d .claude/ide:d .claude/paste-cache:d .claude/telemetry:d
    .claude/history.jsonl:f .claude/stats-cache.json:f .claude/.last-cleanup:f
    .codex/auth.json:f '.codex/.auth-*:f' .codex/auth.lock:f .codex/auth.json.lock:f
    .codex/.sandbox_migration:fd .codex/.sqlite-maintenance.lock:f .codex/cache:d
    '.codex/goals_1.sqlite*:f' .codex/installation_id:f '.codex/logs_2.sqlite*:f'
    '.codex/memories_1.sqlite*:f' .codex/models_cache.json:f '.codex/queue_1.sqlite*:f'
    .codex/sessions:d .codex/shell_snapshots:d '.codex/state_5.sqlite*:f'
    .codex/thread-writer-locks:d '.codex/thread_history_1.sqlite*:f' .codex/tmp:d
    .gemini/config/.migrated:f
    .gemini/antigravity-cli/antigravity-oauth-token:f .gemini/antigravity-cli/bin:d
    .gemini/antigravity-cli/brain:d .gemini/antigravity-cli/builtin:d
    .gemini/antigravity-cli/cache:d .gemini/antigravity-cli/cli.log:fl
    '.gemini/antigravity-cli/conversation_summaries.db*:f'
    .gemini/antigravity-cli/conversations:d .gemini/antigravity-cli/crashes:d
    .gemini/antigravity-cli/implicit:d .gemini/antigravity-cli/installation_id:f
    .gemini/antigravity-cli/jetbox_summaries_proto.pb:f
    '.gemini/antigravity-cli/jetbox_summaries_proto.pb.*.tmp:f'
    .gemini/antigravity-cli/jetski_state.pbtxt:f
    '.gemini/antigravity-cli/jetski_state.pbtxt.*.tmp:f'
    .gemini/antigravity-cli/last_check.timestamp:f .gemini/antigravity-cli/log:d
    .gemini/antigravity-cli/presence:d .gemini/antigravity-cli/updater:d
)
# Le voci di AGENT_CLI_ALLOWED che possono essere un link (#525), come CLI_LINKS di
# devloop/agentuser.py: <voce>:<directory accanto alla voce>. Il link deve portare a un file di
# quella directory, con il percorso relativo <directory>/<nome> (un solo nome, né . né ..), e
# quella voce deve essere un file regolare, non a sua volta un link che porti altrove; ogni altro
# link con quel nome è rifiutato. agy crea a ogni avvio cli.log -> log/cli-<data>.log.
AGENT_CLI_LINKS=(.gemini/antigravity-cli/cli.log:log)

# agent_cli_link_allowed TARGET INSIDE: il contenuto TARGET di un link di AGENT_CLI_LINKS porta a
# un file della directory INSIDE accanto al link: esattamente INSIDE/<nome>, relativo, <nome>
# né vuoto né . né .., come link_allowed di devloop/agentuser.py.
agent_cli_link_allowed() {
    local target=$1 inside=$2 rest
    case $target in
        "$inside"/*) rest=${target#"$inside"/} ;;
        *) return 1 ;;
    esac
    case $rest in
        '' | . | .. | */*) return 1 ;;
    esac
    return 0
}

# agent_cli_entry_known DIR NAME TYPE [TARGET HOME]: la voce NAME, di tipo TYPE (%y di find),
# della directory delle CLI DIR (relativa alla home) è nella lista, o è sigillata o una directory
# delle CLI; TARGET è il contenuto del link, quando TYPE è l, e HOME la home in cui si trova DIR.
# Torna 0 se è nota (sigillata e directory delle CLI: il proprietario lo controlla chi chiama), 1
# se manca dalla lista, 2 se è nella lista con un altro tipo, 3 se è un link ammesso per quel nome
# che porta fuori dalla sua directory, 4 se porta dentro la sua directory ma a una voce che non è
# un file regolare (un altro link, che potrebbe uscirne, o nulla; senza HOME non si verifica).
agent_cli_entry_known() {
    local dir=$1 name=$2 type=$3 target=${4-} home=${5-} entry pattern types link to matched=0
    for entry in "${AGENT_CLI_DIRS[@]}" "${AGENT_CLI_SEALED[@]}"; do
        [ "$entry" = "$dir/$name" ] && return 0
    done
    for entry in "${AGENT_CLI_ALLOWED[@]}"; do
        pattern=${entry%:*} types=${entry##*:}
        [ "${pattern%/*}" = "$dir" ] || continue
        # shellcheck disable=SC2254 # il modello del nome è un glob
        case $name in
            ${pattern##*/})
                matched=1
                case $types in
                    *"$type"*)
                        [ "$type" = l ] || return 0
                        for link in "${AGENT_CLI_LINKS[@]}"; do
                            [ "${link%:*}" = "$pattern" ] || continue
                            agent_cli_link_allowed "$target" "${link##*:}" || continue
                            to=$home/$dir/$target
                            [ -n "$home" ] && [ -f "$to" ] && ! [ -L "$to" ] && return 0
                            return 4
                        done
                        return 3 ;;
                esac ;;
        esac
    done
    return $((matched + 1))
}
# Le voci che le CLI creano al primo uso dentro AGENT_CLI_DIRS (#494), dove l'utente degli agent
# non crea nulla: setup-agent-user.sh le crea sue (700), come CLI_WRITABLE di
# devloop/agentuser.py. Una CLI che si ferma con `permission denied` su una voce nuova di una
# directory sigillata vuole una voce in più qui (ops/host/migrate.md).
AGENT_CLI_WRITABLE=(.gemini/antigravity-cli/bin)

# codex_memories FILE [DEST]: la configurazione di codex FILE spegne in modo esplicito le sue
# memorie (#517), con [features] memories = false, e nessun profilo ([profiles.<nome>.features])
# le riaccende, come codex_config_problems di devloop/agentuser.py: altrimenti codex
# rileggerebbe in un turno le memorie (memories_1.sqlite) che un altro vi ha lasciato. Torna 0
# se è così, 1 se non lo è (con il motivo su stdout). Con DEST, se manca solo la riga (anche
# FILE che non esiste), scrive in DEST FILE con la riga aggiunta (nella tabella [features], o in
# una nuova in fondo) e torna 3; una riga con un altro valore non la cambia: torna 1.
# Python isolato (-I) e senza bytecode (-B): gira come root da qualunque directory, e un modulo
# nella directory corrente non deve essere importato al posto di quello della libreria.
codex_memories() {
    python3 -I -B - "$@" <<'EOF'
import re, sys, tomllib
path, dest = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
LIMIT = 1 << 20


def no(why):
    print(f'{path} {why}')
    sys.exit(1)


def shown(value):
    return str(value).lower() if isinstance(value, bool) else repr(value)


def profiles(data):
    found = data.get('profiles')
    for name, profile in (found.items() if isinstance(found, dict) else ()):
        on = profile.get('features') if isinstance(profile, dict) else None
        if isinstance(on, dict) and 'memories' in on and on['memories'] is not False:
            no(f'imposta [profiles.{name}.features] memories = {shown(on["memories"])}')


try:
    with open(path, 'rb') as fh:
        raw = fh.read(LIMIT + 1)
except FileNotFoundError:
    if dest is None:
        no('non esiste')
    raw = b''
except OSError as exc:
    no(f'non si legge ({exc.strerror})')
if len(raw) > LIMIT:
    no(f'supera {LIMIT} byte')
try:
    text = raw.decode('utf-8')
    data = tomllib.loads(text)
except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
    no(f'non è un TOML valido ({exc})')
features = data.get('features')
if isinstance(features, dict) and 'memories' in features:
    if features['memories'] is not False:
        no(f'imposta [features] memories = {shown(features["memories"])}')
    profiles(data)
    sys.exit(0)
profiles(data)
if dest is None:
    no('non imposta [features] memories = false')
lines = text.splitlines(keepends=True)
header = re.compile(r'[ \t]*\[[ \t]*features[ \t]*\][ \t]*(#.*)?\r?\n?')
for i, line in enumerate(lines):
    if header.fullmatch(line):
        lines[i] = line if line.endswith('\n') else line + '\n'
        lines.insert(i + 1, 'memories = false\n')
        break
else:
    if lines:
        lines[-1] = lines[-1] if lines[-1].endswith('\n') else lines[-1] + '\n'
        lines.append('\n')
    lines += ['[features]\n', 'memories = false\n']
new = ''.join(lines)
try:
    written = tomllib.loads(new).get('features')
except tomllib.TOMLDecodeError:
    written = None
if not (isinstance(written, dict) and written.get('memories') is False):
    no('non imposta [features] memories = false, e la riga non si aggiunge da sola: '
       'aggiungerla a mano')
with open(dest, 'w', encoding='utf-8') as fh:
    fh.write(new)
sys.exit(3)
EOF
}

# agy_trusted_workspaces narrow FILE WORKTREES DEST
# agy_trusted_workspaces check FILE STATE [HOME...]
# trustedWorkspaces della configurazione di agy FILE (~/.gemini/antigravity-cli/settings.json,
# sigillata, #572): le directory di cui agy si fida. L'utente degli agent non ha un clone suo: i
# turni girano nei worktree dello state_dir, e basta la loro directory.
# Una voce sta dentro una directory solo se ci sta come è scritta e anche dove portano i suoi
# link: un link sotto i worktree che porta fuori non è dentro.
# narrow: se trustedWorkspaces manca, o nomina una directory fuori da WORKTREES, scrive in DEST
# FILE con trustedWorkspaces = [WORKTREES] e torna 3; un valore già dentro WORKTREES (anche una
# lista vuota) non lo tocca e torna 0. FILE che non esiste vale {}.
# check: stampa una riga per ogni voce che è o contiene una home (degli utenti del sistema o una
# di HOME), anche attraverso un link, o che è fuori dallo state_dir STATE, e torna 1 se ce n'è;
# FILE che non esiste, o senza trustedWorkspaces, torna 0.
# Torna 1 con il motivo su stdout anche se FILE non si legge o non è un oggetto JSON, o se
# trustedWorkspaces non è una lista di percorsi. Python isolato, come codex_memories.
agy_trusted_workspaces() {
    python3 -I -B - "$@" <<'EOF'
import json, os, pwd, sys
mode, path, base, rest = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
LIMIT = 1 << 20
KEY = 'trustedWorkspaces'


def no(why):
    print(f'{path} {why}')
    sys.exit(1)


def inside(entry, top):
    return entry == top or entry.startswith(top.rstrip('/') + '/')


def tops(directory):
    return {os.path.normpath(directory), os.path.realpath(directory)}


def within(entry, top):
    # entry sta sotto top come è scritta e anche dove portano i suoi link: una voce sotto i
    # worktree che è un link fuori non è più stretta (#572).
    return (any(inside(os.path.normpath(entry), t) for t in tops(top))
            and inside(os.path.realpath(entry), os.path.realpath(top)))


try:
    with open(path, 'rb') as fh:
        raw = fh.read(LIMIT + 1)
except FileNotFoundError:
    raw = b''
except OSError as exc:
    no(f'non si legge ({exc.strerror})')
if len(raw) > LIMIT:
    no(f'supera {LIMIT} byte')
try:
    data = json.loads(raw.decode('utf-8')) if raw.strip() else {}
except (UnicodeDecodeError, ValueError) as exc:
    no(f'non è un JSON valido ({exc})')
if not isinstance(data, dict):
    no('non è un oggetto JSON')
value = data.get(KEY)
if value is not None and not (isinstance(value, list)
                              and all(isinstance(e, str) for e in value)):
    no(f'imposta {KEY} a un valore che non è una lista di percorsi')
entries = value or []
if mode == 'check':
    homes = {p.pw_dir for p in pwd.getpwall() if p.pw_dir.startswith('/')}
    homes |= {h for h in rest if h.startswith('/')}
    homes = set().union(*(tops(h) for h in homes))
    bad = False
    for entry in entries:
        n = os.path.normpath(entry) if entry.startswith('/') else None
        real = os.path.realpath(entry) if n is not None else None
        # Dove porta la voce, per i casi in cui il problema sta lì: un link, o una voce che
        # passa da un link.
        led = f' (porta a {real})'
        held = sorted(h for h in homes if n is not None and inside(h, n))
        held_real = sorted(h for h in homes if real is not None and inside(h, real))
        if n is None:
            print(f'{path}: {KEY} nomina {entry!r}, che non è un percorso assoluto')
        elif held:
            print(f'{path}: {KEY} nomina {entry}, che è o contiene la home {held[0]}')
        elif held_real:
            print(f'{path}: {KEY} nomina {entry}{led}, che è o contiene la home '
                  f'{held_real[0]}')
        elif not any(inside(n, top) for top in tops(base)):
            print(f'{path}: {KEY} nomina {entry}, fuori dallo state_dir {base}')
        elif not within(entry, base):
            print(f'{path}: {KEY} nomina {entry}{led}, fuori dallo state_dir {base}')
        else:
            continue
        bad = True
    sys.exit(1 if bad else 0)
worktrees, dest = os.path.normpath(base), rest[0]
if value is not None and all(e.startswith('/') and within(e, base) for e in entries):
    sys.exit(0)
data[KEY] = [worktrees]
with open(dest, 'w', encoding='utf-8') as fh:
    fh.write(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
sys.exit(3)
EOF
}

# overlay_declares_app FILE ROLE: l'overlay FILE ha la tabella [apps.ROLE]. Un overlay che manca o
# non si legge non dichiara nulla.
overlay_declares_app() {
    [ -f "$1" ] && python3 -I -B - "$1" "$2" <<'EOF'
import sys, tomllib
try:
    with open(sys.argv[1], 'rb') as fh:
        data = tomllib.load(fh)
except (OSError, tomllib.TOMLDecodeError):
    sys.exit(1)
apps = data.get('apps')
sys.exit(0 if isinstance(apps, dict) and isinstance(apps.get(sys.argv[2]), dict) else 1)
EOF
}

# ops_devloop_keys OVERLAY: divide DEVLOOP_KEYS fra le chiavi richieste nella key_dir di devloop
# (DEVLOOP_KEYS_REQUIRED) e quelle soltanto riconosciute (DEVLOOP_KEYS_OPTIONAL), #569. Con il
# default, la chiave dell'implementer è richiesta solo se OVERLAY dichiara [apps.implementer]:
# un'installazione senza quell'app non la deve avere. Un DEVLOOP_KEYS di params.env le richiede
# tutte.
ops_devloop_keys() {
    local role
    DEVLOOP_KEYS_REQUIRED=$DEVLOOP_KEYS
    DEVLOOP_KEYS_OPTIONAL=
    if [ "$DEVLOOP_KEYS_EXPLICIT" = 0 ] && ! overlay_declares_app "$1" implementer; then
        DEVLOOP_KEYS_REQUIRED=
        for role in $DEVLOOP_KEYS; do
            if [ "$role" = implementer ]; then
                DEVLOOP_KEYS_OPTIONAL=$role
            else
                DEVLOOP_KEYS_REQUIRED="${DEVLOOP_KEYS_REQUIRED:+$DEVLOOP_KEYS_REQUIRED }$role"
            fi
        done
    fi
}

# I testi pubblicati nominano i parametri, non i percorsi dell'host (#583). I parametri che hanno
# per valore un percorso, sostituiti dal più lungo (STATE_DIR è STATE_PATH, lo state_dir risolto),
# e quelli che sono nomi di utenti dei ruoli, sostituiti solo da soli e solo se non sono i default
# documentati (OPS_PUBLIC_USERS, come PUBLIC_USERS di devloop/redact.py).
OPS_REDACT_PATHS='STATE_DIR DEVLOOP_KEY_DIR SUPERVISOR_KEY_DIR COORDINATOR_KEY_DIR COORDINATOR_CLONE DEVLOOP_HOME AGENT_HOME SUPERVISOR_HOME COORDINATOR_HOME COORDINATION_DIR COORDINATOR_OPS_DIR SUPERVISOR_OPS_DIR WATCHDOG_OPS_DIR LAUNCHER_DIR REPO_LOCK_DIR SYSTEMD_DIR SUDOERS_FILE AGENT_SUDOERS_FILE COORDINATOR_SUDOERS_FILE TMPFILES_FILE DEVLOOP_PROJECTS_DIR DEVLOOP_COMMAND'
OPS_REDACT_USERS='DEVLOOP_USER AGENT_USER SUPERVISOR_USER COORDINATOR_USER WATCHDOG_USER SENTINEL_USER'
# Gli altri nomi dell'installazione, sostituiti da soli e nei nomi dei percorsi e delle unità
# ($SYSTEMD_DIR/$SERVICE_NAME.service), alle condizioni degli utenti.
OPS_REDACT_NAMES='SERVICE_NAME'
# La mappa delle sostituzioni che setup-devloop.sh scrive nello state_dir e devloop legge
# all'avvio (MAP_FILE di devloop/redact.py).
OPS_REDACT_FILE=redactions.json

# ops_redactions: la mappa delle sostituzioni dei parametri risolti (ops_load_params), in JSON:
# {"paths": {NOME: percorso}, "users": {NOME: utente}, "names": {NOME: valore}, "app_prefix":
# APP_PREFIX}. Nessuna chiave letta: solo i valori dei parametri.
ops_redactions() {
    local name value
    local -a args=("$APP_PREFIX")
    for name in $OPS_REDACT_PATHS; do
        value=${!name:-}
        [ "$name" = STATE_DIR ] && value=$STATE_PATH
        args+=("path:$name=$value")
    done
    for name in $OPS_REDACT_USERS; do
        args+=("user:$name=${!name:-}")
    done
    for name in $OPS_REDACT_NAMES; do
        args+=("name:$name=${!name:-}")
    done
    python3 -I -B - "${args[@]}" <<'EOF'
import json, sys
data = {'paths': {}, 'users': {}, 'names': {}, 'app_prefix': sys.argv[1]}
for arg in sys.argv[2:]:
    kind, _, rest = arg.partition(':')
    name, _, value = rest.partition('=')
    if value:
        data[{'path': 'paths', 'user': 'users', 'name': 'names'}[kind]][name] = value
print(json.dumps(data, indent=2))
EOF
}

# ops_redact_filter MAP: copia lo standard input sull'output, una riga alla volta, con i nomi dei
# parametri della mappa MAP (ops_redactions) al posto dei valori dell'host: i percorsi dal più
# lungo e fino alla fine di un nome ($STATE_DIR/worktrees/...), i file di chiave come ruolo
# dell'app (<chiave dell'app dispatcher>), gli altri nomi ($SERVICE_NAME, anche nel nome
# dell'unità), gli utenti da soli (<DEVLOOP_USER>). La stessa regola
# di devloop/redact.py, qui perché la copia di root del coordinatore ha solo i file di ops/.
ops_redact_filter() {
    python3 -I -B -u -c '
import json, re, sys
data = json.loads(sys.argv[1])
prefix = data.get("app_prefix") or ""
public = {"devloop", "devloop-agent", "supervisor", "coordinator", "role-watchdog",
          "role-sentinel", "root"}
paths = {}
for name, value in data.get("paths", {}).items():
    value = value.rstrip("/")
    if value.startswith("/") and value.count("/") >= 2 and value not in paths:
        paths[value] = name
users = {}
for name, value in data.get("users", {}).items():
    if len(value) >= 3 and value not in public and value not in users:
        users[value] = name
names = {}
for name, value in data.get("names", {}).items():
    if len(value) >= 3 and value not in public and re.fullmatch(r"[A-Za-z0-9_@-]+", value) \
            and value not in names:
        names[value] = name
def alt(values):
    return "|".join(re.escape(v) for v in sorted(values, key=lambda v: (-len(v), v)))
path_re = re.compile(r"(?<![\w.-])(" + alt(paths) + r")(?![\w.-])") if paths else None
user_re = re.compile(r"(?<![\w./-])(" + alt(users) + r")(?![\w/-])") if users else None
name_re = re.compile(r"(?<![\w.@-])(" + alt(names) + r")(?![\w@-])") if names else None
key_re = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9-]*)\.\d{4}-\d{2}-\d{2}\.private-key\.pem\b")
def key(m):
    slug = m.group(1)
    return ("<chiave dell\x27app %s>" % slug[len(prefix):]) if prefix and slug.startswith(prefix) \
        and len(slug) > len(prefix) else "<chiave di un\x27app>"
sys.stdin.reconfigure(errors="replace")
for line in sys.stdin:
    if path_re:
        line = path_re.sub(lambda m: "$" + paths[m.group(1)], line)
    line = key_re.sub(key, line)
    if name_re:
        line = name_re.sub(lambda m: "$" + names[m.group(1)], line)
    if user_re:
        line = user_re.sub(lambda m: "<%s>" % users[m.group(1)], line)
    sys.stdout.write(line)
' "$1"
}

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
ok() { printf '  ok: %s\n' "$*"; }
todo() { printf '  da fare a mano: %s\n' "$*"; }
warn() { WARNINGS=$((WARNINGS + 1)); printf '  ATTENZIONE: %s\n' "$*"; }
fail() { FAILURES=$((FAILURES + 1)); printf '  ERRORE: %s\n' "$*"; }
die() { printf '%s: %s\n' "$(basename "$0")" "$*" >&2; exit 1; }

# Le opzioni comuni a tutti gli script. Imposta OPS_SHIFT con il numero di argomenti usati;
# torna 1 su un'opzione sconosciuta.
ops_common_arg() {
    OPS_SHIFT=1
    case $1 in
        --dry-run) DRY_RUN=1 ;;
        --yes | -y) ASSUME_YES=1 ;;
        --params)
            [ $# -ge 2 ] || die '--params vuole un file'
            PARAMS_FILE=$2
            OPS_SHIFT=2
            ;;
        --params=*) PARAMS_FILE=${1#--params=} ;;
        *)
            printf '%s: opzione sconosciuta: %s (vedi --help)\n' "$(basename "$0")" "$1" >&2
            return 1
            ;;
    esac
}

OPS_COMMON_HELP='Opzioni comuni:
  --params FILE   i parametri (default: ops/params.env, da copiare da ops/params.env.example)
  --dry-run       stampa le azioni senza farle
  --yes           non chiede conferma su una piattaforma diversa da quella verificata
  -h, --help      questo aiuto'

_require_name() {
    [[ ${!1} =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "$1 non è un nome utente valido: '${!1}'"
}

_require_abs() {
    [[ ${!1} =~ ^/[A-Za-z0-9._/-]*$ ]] || die "$1 deve essere un percorso assoluto senza spazi: '${!1}'"
}

# Legge params.env, completa i valori derivati e li controlla: finiscono in sudoers e in
# un'unità systemd, quindi si accettano solo forme note.
ops_load_params() {
    [ -f "$PARAMS_FILE" ] || die "manca $PARAMS_FILE: copiare ops/params.env.example in ops/params.env e adattarlo"
    set -a
    # shellcheck disable=SC1090
    . "$PARAMS_FILE"
    set +a
    : "${REPO:?REPO non impostato in $PARAMS_FILE}"
    : "${APP_PREFIX:?APP_PREFIX non impostato in $PARAMS_FILE}"
    # Più progetti sullo stesso host (ADR 0006, R8): con PROJECT i default sono del progetto,
    # con un utente di sistema per progetto (D6) e l'istanza devloop@<progetto> dell'unità
    # template. Senza, un solo progetto con devloop.service, come prima. Ogni valore scritto in
    # params.env vale anche con PROJECT: un host che passa all'istanza tiene così i suoi utenti,
    # i suoi clone e le sue key_dir (#563).
    : "${PROJECT:=}"
    if [ -n "$PROJECT" ]; then
        [[ $PROJECT =~ ^[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?$ ]] \
            || die "PROJECT ammette minuscole, cifre e '-' interni, al più 16 caratteri: '$PROJECT'"
        # Il suffisso -agent è dell'utente degli agent: con PROJECT=x-agent, devloop-x-agent
        # sarebbe l'utente degli agent del progetto x, e con PROJECT=agent devloop-agent quello
        # dell'installazione senza PROJECT. Così i nomi derivati non coincidono mai fra ruoli né
        # fra progetti.
        [[ $PROJECT != agent && $PROJECT != *-agent ]] \
            || die "PROJECT non può essere 'agent' né finire con '-agent': devloop-$PROJECT sarebbe l'utente degli agent di un altro progetto"
        : "${DEVLOOP_USER:=devloop-$PROJECT}"
        : "${AGENT_USER:=devloop-$PROJECT-agent}"
        : "${SERVICE_NAME:=devloop@$PROJECT}"
        : "${SUDOERS_FILE:=/etc/sudoers.d/devloop-supervisor-$PROJECT}"
        : "${AGENT_SUDOERS_FILE:=/etc/sudoers.d/devloop-agent-$PROJECT}"
        # Supervisore e coordinatore hanno un utente per ruolo e per insieme di app (ADR 0002,
        # precisazione del 2026-11-24 su #643, che precisa quella di #629): la chiave della loro
        # app vale per tutti i repository dell'insieme, e utenti per progetto dentro un insieme
        # proteggerebbero poco. Il nome di default viene dall'insieme (APP_SET), non dal
        # progetto; utente, home, key_dir (una sola copia della chiave) e login della CLI sono
        # dell'insieme. Restano del progetto la sessione, la casella con il suo gruppo, lo
        # state_dir, il clone, i sudoers e la copia di check-host.sh del coordinatore. Un utente
        # di ruolo si condivide solo fra progetti dello stesso insieme (ops_check_host_users).
        if [ -z "${SUPERVISOR_USER:-}" ] || [ -z "${COORDINATOR_USER:-}" ]; then
            : "${APP_SET:=${APP_PREFIX%-}}"
            [[ $APP_SET =~ ^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$ ]] \
                || die "APP_SET, il nome dell'insieme di app nei nomi degli utenti dei ruoli, ammette minuscole, cifre e '-' interni, al più 20 caratteri: '$APP_SET' (per default APP_PREFIX senza il '-' finale; scriverlo in params.env, uguale per tutti i progetti dell'insieme)"
            : "${SUPERVISOR_USER:=supervisor-$APP_SET}"
            : "${COORDINATOR_USER:=coordinator-$APP_SET}"
        fi
        : "${COORDINATION_GROUP:=devloop-roles-$PROJECT}"
        : "${COORDINATOR_SUDOERS_FILE:=/etc/sudoers.d/devloop-coordinator-$PROJECT}"
        : "${COORDINATOR_OPS_DIR:=/usr/local/lib/devloop-coordinator-$PROJECT}"
        # La key_dir del supervisore è dell'insieme, nella sua home (una sola copia della chiave);
        # il suo clone del repository gestito è del progetto.
        : "${SUPERVISOR_KEY_DIR:=${SUPERVISOR_HOME:-/home/$SUPERVISOR_USER}/.ghapps}"
        : "${SUPERVISOR_CLONE:=${SUPERVISOR_HOME:-/home/$SUPERVISOR_USER}/devloop-$PROJECT}"
        # Anche la key_dir della sua app del coordinatore (#549) è dell'insieme.
        : "${COORDINATOR_KEY_DIR:=${COORDINATOR_HOME:-/home/$COORDINATOR_USER}/.ghapps}"
        # Con SOURCE_REPO, i clone della sorgente da cui supervisore e coordinatore coniano i
        # token delle loro app (#571), al tag SOURCE_REF del progetto.
        : "${SUPERVISOR_SOURCE_CLONE:=${SUPERVISOR_HOME:-/home/$SUPERVISOR_USER}/devloop-source-$PROJECT}"
        : "${COORDINATOR_SOURCE_CLONE:=${COORDINATOR_HOME:-/home/$COORDINATOR_USER}/devloop-source-$PROJECT}"
        # La casella fra supervisore e coordinatore (#543) è del progetto, come la sveglia del
        # coordinatore che la legge.
        : "${COORDINATION_DIR:=/var/lib/devloop-coordination-$PROJECT}"
        # La copia di root del risveglio del supervisore e il watchdog dei ruoli (#602): del
        # progetto, con un utente del watchdog per progetto, che entra solo nella sua casella.
        : "${SUPERVISOR_OPS_DIR:=/usr/local/lib/devloop-supervisor-$PROJECT}"
        : "${WATCHDOG_OPS_DIR:=/usr/local/lib/devloop-role-watchdog-$PROJECT}"
        : "${WATCHDOG_USER:=role-watchdog-$PROJECT}"
        # La sentinella dei ruoli a riposo (#684): un utente per progetto, come il watchdog.
        : "${SENTINEL_USER:=role-sentinel-$PROJECT}"
        # L'avviatore della sorgente a parte, dal tag SOURCE_REF del progetto (#573): una
        # directory per progetto, così due progetti con tag diversi non si sovrascrivono a
        # turno lo stesso file.
        : "${LAUNCHER_DIR:=/usr/local/lib/devloop/$PROJECT}"
        [ "$SERVICE_NAME" = "devloop@$PROJECT" ] \
            || die "con PROJECT il servizio è l'istanza devloop@$PROJECT dell'unità template: '$SERVICE_NAME'"
    fi
    : "${DEVLOOP_USER:=devloop}"
    : "${SUPERVISOR_USER:=supervisor}"
    : "${AGENT_USER:=devloop-agent}"
    : "${COORDINATOR_USER:=coordinator}"
    : "${DEFAULT_BRANCH:=main}"
    : "${GITHUB_OWNER_TYPE:=user}"
    : "${DEVLOOP_HOME:=/home/$DEVLOOP_USER}"
    : "${SUPERVISOR_HOME:=/home/$SUPERVISOR_USER}"
    : "${AGENT_HOME:=/home/$AGENT_USER}"
    : "${COORDINATOR_HOME:=/home/$COORDINATOR_USER}"
    : "${DEVLOOP_CLONE:=$DEVLOOP_HOME/${REPO#*/}}"
    : "${SUPERVISOR_CLONE:=$SUPERVISOR_HOME/${REPO#*/}}"
    # Il clone del repository gestito del coordinatore, la directory di lavoro della sua sessione
    # (#621), che setup-coordinator.sh prepara (#643). Con PROJECT resta il nome del repository:
    # i progetti di un insieme sono dello stesso proprietario, quindi i loro repository hanno nomi
    # diversi anche nella stessa home.
    : "${COORDINATOR_CLONE:=$COORDINATOR_HOME/${REPO#*/}}"
    : "${STATE_DIR:=$OPS_STATE_DIR}"
    : "${DEVLOOP_KEY_DIR:=$DEVLOOP_HOME/.ghapps}"
    : "${SUPERVISOR_KEY_DIR:=$SUPERVISOR_HOME/.ghapps}"
    : "${COORDINATOR_KEY_DIR:=$COORDINATOR_HOME/.ghapps}"
    # Un DEVLOOP_KEYS scritto in params.env è autorevole: ogni chiave elencata è richiesta. Il
    # default comprende implementer, ma la sua chiave è richiesta solo se l'overlay del progetto
    # dichiara [apps.implementer] (#569; ops_devloop_keys).
    DEVLOOP_KEYS_EXPLICIT=0
    [ -n "${DEVLOOP_KEYS:-}" ] && DEVLOOP_KEYS_EXPLICIT=1
    : "${DEVLOOP_KEYS:=dispatcher merger claude-agent codex-agent agy-agent implementer}"
    : "${SUPERVISOR_KEYS:=supervisor claude-agent}"
    : "${COORDINATOR_KEYS:=coordinator}"
    : "${DEVLOOP_PATH:=$DEVLOOP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin}"
    : "${AGENT_PATH:=$AGENT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin}"
    : "${SERVICE_NAME:=devloop}"
    : "${SYSTEMD_DIR:=/etc/systemd/system}"
    : "${SUDOERS_FILE:=/etc/sudoers.d/devloop-supervisor}"
    : "${AGENT_SUDOERS_FILE:=/etc/sudoers.d/devloop-agent}"
    : "${COORDINATOR_SUDOERS_FILE:=/etc/sudoers.d/devloop-coordinator}"
    : "${COORDINATOR_OPS_DIR:=/usr/local/lib/devloop-coordinator}"
    : "${APPS:=dispatcher merger claude-agent codex-agent agy-agent implementer supervisor coordinator}"
    : "${SOURCE_REPO:=}"
    : "${SOURCE_REF:=}"
    : "${SOURCE_CLONE:=$DEVLOOP_HOME/devloop-source}"
    : "${SUPERVISOR_SOURCE_CLONE:=$SUPERVISOR_HOME/devloop-source}"
    : "${COORDINATOR_SOURCE_CLONE:=$COORDINATOR_HOME/devloop-source}"
    : "${LAUNCHER_DIR:=/usr/local/lib/devloop}"
    : "${REPO_LOCK_DIR:=/run/devloop/repos}"
    : "${REPO_LOCK_GROUP:=devloop-repos}"
    : "${TMPFILES_FILE:=/etc/tmpfiles.d/devloop-repos.conf}"
    : "${COORDINATION_DIR:=/var/lib/devloop-coordination}"
    : "${COORDINATION_GROUP:=devloop-roles}"
    : "${SUPERVISOR_OPS_DIR:=/usr/local/lib/devloop-supervisor}"
    : "${WATCHDOG_OPS_DIR:=/usr/local/lib/devloop-role-watchdog}"
    : "${WATCHDOG_USER:=role-watchdog}"
    : "${SENTINEL_USER:=role-sentinel}"
    # Il comando devloop dell'host (#588): l'involucro del launcher, e la directory di root con un
    # file per progetto da cui sceglie il progetto. Gli stessi per tutti i progetti dell'host.
    : "${DEVLOOP_PROJECTS_DIR:=/etc/devloop/projects}"
    : "${DEVLOOP_COMMAND:=/usr/local/bin/devloop}"
    # Il periodo del risveglio dei ruoli (#602), un'ora; per il coordinatore
    # COORDINATOR_WAIT_PERIOD, se params.env lo scrive (#543).
    : "${ROLE_WAIT_PERIOD:=3600}"
    : "${COORDINATOR_WAIT_PERIOD:=$ROLE_WAIT_PERIOD}"
    # Il flusso fermo (#683): la sveglia del coordinatore lo dice quando il segno di vita del
    # flusso degli eventi è più vecchio di tanti secondi, con il servizio attivo.
    : "${EVENTS_STALE_AFTER:=300}"
    # Il riposo dei ruoli (#684): l'ora fissa UTC (HH:MM) del controllo giornaliero, a cui la
    # sentinella sveglia i ruoli a riposo; i secondi entro cui un ruolo svegliato toglie il suo
    # riposo, oltre cui anche il lavoro trattenuto da devloop run è un'anomalia per il watchdog; e
    # quelli oltre cui lo è un messaggio nella casella di un ruolo a riposo.
    : "${REST_DAILY_CHECK:=00:30}"
    : "${REST_WAKE_TIMEOUT:=300}"
    : "${REST_MAILBOX_AGE:=300}"
    # Il nome del progetto nei nomi per progetto dei ruoli (#602): PROJECT, o _default senza
    # PROJECT. '_' non è ammesso in PROJECT, così l'installazione senza PROJECT non coincide mai
    # con un progetto (PROJECT=default compreso): unità, drop-in e stato restano distinti.
    OPS_INSTANCE=${PROJECT:-_default}
    # L'istanza del watchdog dei ruoli (#602).
    WATCHDOG_INSTANCE=$OPS_INSTANCE
    WATCHDOG_UNIT=devloop-role-watchdog@$WATCHDOG_INSTANCE
    WATCHDOG_DROPIN=$SYSTEMD_DIR/$WATCHDOG_UNIT.service.d/project.conf
    WATCHDOG_FAILED_DROPIN=$SYSTEMD_DIR/devloop-role-watchdog-failed@$WATCHDOG_INSTANCE.service.d/project.conf
    # La sentinella dei ruoli a riposo (#684), della stessa istanza.
    SENTINEL_UNIT=devloop-role-sentinel@$WATCHDOG_INSTANCE
    SENTINEL_DROPIN=$SYSTEMD_DIR/$SENTINEL_UNIT.service.d/project.conf
    # Il file del progetto per il comando devloop (#588), con il nome dell'istanza.
    DEVLOOP_PROJECT_FILE=$DEVLOOP_PROJECTS_DIR/params-$OPS_INSTANCE.env

    [[ $REPO =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] || die "REPO deve essere owner/nome: '$REPO'"
    [[ $APP_PREFIX =~ ^[a-z0-9-]*$ ]] || die "APP_PREFIX ammette solo minuscole, cifre e '-': '$APP_PREFIX'"
    [[ $DEFAULT_BRANCH =~ ^[A-Za-z0-9._/-]+$ ]] || die "DEFAULT_BRANCH non valido: '$DEFAULT_BRANCH'"
    [[ $GITHUB_OWNER_TYPE =~ ^(user|org)$ ]] || die "GITHUB_OWNER_TYPE deve essere user o org"
    # STATE_DIR: assoluto, oppure relativo al clone gestito, senza componenti "." o "..".
    [[ $STATE_DIR =~ ^/?[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] && ! [[ /$STATE_DIR/ =~ /\.\.?/ ]] \
        || die "STATE_DIR deve essere un percorso assoluto o relativo al clone, senza spazi né '.' o '..': '$STATE_DIR'"
    if [[ $STATE_DIR == /* ]]; then
        STATE_PATH=$STATE_DIR
    else
        STATE_PATH=$DEVLOOP_CLONE/$STATE_DIR
    fi
    # La sorgente dell'installazione (ADR 0006, R1): vuota, è il repository gestito, come per
    # ssdlc. Altrimenti un clone a parte, e l'avviatore installato dal suo tag SOURCE_REF.
    if [ -n "$SOURCE_REPO" ]; then
        [[ $SOURCE_REPO =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] \
            || die "SOURCE_REPO deve essere owner/nome: '$SOURCE_REPO'"
        [ "${SOURCE_REPO,,}" != "${REPO,,}" ] \
            || die "SOURCE_REPO è il repository gestito: lasciarlo vuoto"
        [[ $SOURCE_REF =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] && [[ $SOURCE_REF != *..* ]] \
            || die "SOURCE_REF deve essere il tag di $SOURCE_REPO da cui installare l'avviatore: '$SOURCE_REF'"
        _require_abs SOURCE_CLONE
        _require_abs SUPERVISOR_SOURCE_CLONE
        _require_abs COORDINATOR_SOURCE_CLONE
        _require_abs LAUNCHER_DIR
        LAUNCHER=$LAUNCHER_DIR/devloop-pinned
    else
        LAUNCHER=$DEVLOOP_CLONE/scripts/devloop-pinned
    fi
    # Il comando dell'avviatore, nell'unità, nel sudoers e nelle verifiche. Con lo state_dir di
    # default e senza sorgente a parte è quello di sempre; altrimenti riceve lo state_dir come
    # parametro e gira nel clone gestito.
    if [ -z "$SOURCE_REPO" ] && [ "$STATE_DIR" = "$OPS_STATE_DIR" ]; then
        LAUNCHER_ARGS=''
        LAUNCHER_CMD="/usr/bin/python3 $LAUNCHER"
    else
        LAUNCHER_ARGS="--state-dir $STATE_PATH"
        LAUNCHER_CMD="/usr/bin/env -C $DEVLOOP_CLONE /usr/bin/python3 $LAUNCHER $LAUNCHER_ARGS"
    fi
    # L'unità del servizio: devloop.service, oppure con PROJECT l'unità template devloop@.service
    # con i parametri del progetto in una drop-in della sua istanza.
    if [ -n "$PROJECT" ]; then
        UNIT_FILE=$SYSTEMD_DIR/devloop@.service
        UNIT_TEMPLATE=devloop@.service
        DROPIN_FILE=$SYSTEMD_DIR/$SERVICE_NAME.service.d/project.conf
    else
        [[ $SERVICE_NAME =~ ^[A-Za-z0-9_-]+$ ]] || die "SERVICE_NAME non valido: '$SERVICE_NAME'"
        UNIT_FILE=$SYSTEMD_DIR/$SERVICE_NAME.service
        UNIT_TEMPLATE=devloop.service
        DROPIN_FILE=''
    fi
    # L'unità dell'avviso di un servizio caduto (#593), l'OnFailure= del servizio: template,
    # comune ai progetti dell'host, con il progetto come istanza.
    FAILED_UNIT_FILE=$SYSTEMD_DIR/devloop-failed@.service
    # La directory dei lock per repository (ADR 0006, R8): comune agli utenti devloop dell'host,
    # fuori dalle loro home e dagli state_dir, almeno due livelli sotto la radice.
    [[ $REPO_LOCK_DIR =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] && ! [[ $REPO_LOCK_DIR/ =~ /\.\.?/ ]] \
        || die "REPO_LOCK_DIR deve essere un percorso assoluto di almeno due livelli, senza spazi né '.' o '..': '$REPO_LOCK_DIR'"
    local inside
    for inside in "$DEVLOOP_HOME" "$STATE_PATH" "$DEVLOOP_CLONE"; do
        [[ $REPO_LOCK_DIR/ != "$inside"/* ]] && [[ $inside/ != "$REPO_LOCK_DIR"/* ]] \
            || die "REPO_LOCK_DIR non può stare dentro né contenere $inside: è comune a tutti i progetti dell'host"
    done
    [[ $DEVLOOP_PATH =~ ^[A-Za-z0-9._/:-]+$ ]] || die "DEVLOOP_PATH non valido: '$DEVLOOP_PATH'"
    [[ $AGENT_PATH =~ ^[A-Za-z0-9._/:-]+$ ]] || die "AGENT_PATH non valido: '$AGENT_PATH'"
    local sudoers
    for sudoers in SUDOERS_FILE AGENT_SUDOERS_FILE COORDINATOR_SUDOERS_FILE; do
        [[ $(basename "${!sudoers}") =~ ^[A-Za-z0-9_-]+$ ]] \
            || die "il nome di $sudoers non può contenere '.' né '~' (sudo lo ignorerebbe)"
    done
    _require_name DEVLOOP_USER
    _require_name SUPERVISOR_USER
    _require_name AGENT_USER
    _require_name COORDINATOR_USER
    _require_name WATCHDOG_USER
    _require_name SENTINEL_USER
    _require_name REPO_LOCK_GROUP
    [ "$AGENT_USER" != "$DEVLOOP_USER" ] || die "AGENT_USER deve essere diverso da DEVLOOP_USER"
    [ "$AGENT_USER" != "$SUPERVISOR_USER" ] || die "AGENT_USER deve essere diverso da SUPERVISOR_USER"
    # Il coordinatore (#530) ha solo comandi di lettura: un utente suo, che non ha i privilegi
    # di nessun altro ruolo.
    local role
    for role in DEVLOOP_USER SUPERVISOR_USER AGENT_USER; do
        [ "$COORDINATOR_USER" != "${!role}" ] || die "COORDINATOR_USER deve essere diverso da $role"
    done
    # Il watchdog dei ruoli (#602) entra solo nella casella del suo progetto: un utente suo.
    for role in DEVLOOP_USER SUPERVISOR_USER AGENT_USER COORDINATOR_USER; do
        [ "$WATCHDOG_USER" != "${!role}" ] || die "WATCHDOG_USER deve essere diverso da $role"
    done
    # La sentinella (#684) legge soltanto, e scrive solo il segnale di risveglio: un utente suo.
    for role in DEVLOOP_USER SUPERVISOR_USER AGENT_USER COORDINATOR_USER WATCHDOG_USER; do
        [ "$SENTINEL_USER" != "${!role}" ] || die "SENTINEL_USER deve essere diverso da $role"
    done
    local path
    for path in DEVLOOP_HOME SUPERVISOR_HOME AGENT_HOME COORDINATOR_HOME DEVLOOP_CLONE \
        SUPERVISOR_CLONE COORDINATOR_CLONE DEVLOOP_KEY_DIR SUPERVISOR_KEY_DIR COORDINATOR_KEY_DIR SYSTEMD_DIR \
        SUDOERS_FILE \
        AGENT_SUDOERS_FILE COORDINATOR_SUDOERS_FILE TMPFILES_FILE; do
        _require_abs "$path"
    done
    # La copia di root di check-host.sh che il coordinatore esegue come root (#530): fuori dalle
    # home degli utenti e dallo state_dir, che altri possono scrivere.
    [[ $COORDINATOR_OPS_DIR =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] \
        && ! [[ $COORDINATOR_OPS_DIR/ =~ /\.\.?/ ]] \
        || die "COORDINATOR_OPS_DIR deve essere un percorso assoluto di almeno due livelli, senza spazi né '.' o '..': '$COORDINATOR_OPS_DIR'"
    for inside in "$DEVLOOP_HOME" "$SUPERVISOR_HOME" "$AGENT_HOME" "$COORDINATOR_HOME" \
        "$STATE_PATH" "$DEVLOOP_CLONE"; do
        [[ $COORDINATOR_OPS_DIR/ != "$inside"/* ]] && [[ $inside/ != "$COORDINATOR_OPS_DIR"/* ]] \
            || die "COORDINATOR_OPS_DIR non può stare dentro né contenere $inside: il coordinatore ne esegue i file come root"
    done
    # La casella fra supervisore e coordinatore (#543): di root, fuori dalle home e dallo
    # state_dir, con un gruppo suo (check-host.sh verifica che gli utenti devloop non ci siano).
    [[ $COORDINATION_DIR =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] \
        && ! [[ $COORDINATION_DIR/ =~ /\.\.?/ ]] \
        || die "COORDINATION_DIR deve essere un percorso assoluto di almeno due livelli, senza spazi né '.' o '..': '$COORDINATION_DIR'"
    for inside in "$DEVLOOP_HOME" "$SUPERVISOR_HOME" "$AGENT_HOME" "$COORDINATOR_HOME" \
        "$STATE_PATH" "$DEVLOOP_CLONE" "$COORDINATOR_OPS_DIR"; do
        [[ $COORDINATION_DIR/ != "$inside"/* ]] && [[ $inside/ != "$COORDINATION_DIR"/* ]] \
            || die "COORDINATION_DIR non può stare dentro né contenere $inside"
    done
    _require_name COORDINATION_GROUP
    # Le copie di root del risveglio del supervisore e del watchdog dei ruoli (#602), come quella
    # del coordinatore, e il comando devloop con i file dei progetti (#588), che root esegue e
    # legge: fuori dalle home, dallo state_dir e dalla casella.
    local copy
    for copy in SUPERVISOR_OPS_DIR WATCHDOG_OPS_DIR DEVLOOP_PROJECTS_DIR DEVLOOP_COMMAND; do
        [[ ${!copy} =~ ^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] && ! [[ ${!copy}/ =~ /\.\.?/ ]] \
            || die "$copy deve essere un percorso assoluto di almeno due livelli, senza spazi né '.' o '..': '${!copy}'"
        for inside in "$DEVLOOP_HOME" "$SUPERVISOR_HOME" "$AGENT_HOME" "$COORDINATOR_HOME" \
            "$STATE_PATH" "$DEVLOOP_CLONE" "$COORDINATION_DIR"; do
            [[ ${!copy}/ != "$inside"/* ]] && [[ $inside/ != "${!copy}"/* ]] \
                || die "$copy non può stare dentro né contenere $inside"
        done
    done
    for copy in ROLE_WAIT_PERIOD COORDINATOR_WAIT_PERIOD EVENTS_STALE_AFTER REST_WAKE_TIMEOUT \
        REST_MAILBOX_AGE; do
        [[ ${!copy} =~ ^[1-9][0-9]{0,6}$ ]] \
            || die "$copy è un numero di secondi, da 1 a 9999999: '${!copy}'"
    done
    [[ $REST_DAILY_CHECK =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] \
        || die "REST_DAILY_CHECK è un'ora UTC HH:MM: '$REST_DAILY_CHECK'"
    ops_check_host_users
}

# Gli utenti delle altre installazioni di devloop dell'host (ADR 0006, R8), una riga
# "<utente> <ruolo> <file>" ciascuno (con supervisor e coordinator, anche l'utente devloop di
# quell'installazione: "<utente> <ruolo> <file> <devloop>", #643), con ruolo devloop, agent, supervisor o coordinator: dalle
# loro unità (User=) e dai loro sudoers (devloop-*: "<devloop> ALL=(<agent>) NOPASSWD:SETENV:" di
# sudoers-agent, "<supervisore> ALL=(<devloop>) NOPASSWD:" di sudoers-supervisor,
# "<coordinatore> ALL=(<devloop>) NOPASSWD:" di sudoers-coordinator, #629). Salta i file di questa
# installazione e quelli che non può leggere: i sudoers sono di root, quindi senza root
# (--dry-run) legge solo le unità.
_ops_host_users() {
    local file dir role
    for file in "$SYSTEMD_DIR"/devloop*.service "$SYSTEMD_DIR"/devloop@*.service.d/project.conf; do
        [ -f "$file" ] && [ -r "$file" ] || continue
        [ "$file" = "$UNIT_FILE" ] || [ "$file" = "$DROPIN_FILE" ] && continue
        # Le unità del watchdog dei ruoli (#602) e dell'avviso di un servizio caduto (#593) non
        # sono il servizio di un progetto.
        case $(basename "$file") in
            devloop-role-watchdog* | devloop-role-sentinel* | devloop-failed@*) continue ;;
        esac
        sed -n "s|^User=\([a-z_][a-z0-9_-]*\)\$|\1 devloop $file|p" "$file"
    done
    # Il watchdog dei ruoli di un altro progetto (#602) e la sua sentinella (#684): i loro utenti
    # entrano solo nella casella di quel progetto.
    for file in "$SYSTEMD_DIR"/devloop-role-watchdog@*.service.d/project.conf; do
        [ -f "$file" ] && [ -r "$file" ] || continue
        [ "$file" = "$WATCHDOG_DROPIN" ] && continue
        sed -n "s|^User=\([a-z_][a-z0-9_-]*\)\$|\1 watchdog $file|p" "$file"
    done
    for file in "$SYSTEMD_DIR"/devloop-role-sentinel@*.service.d/project.conf; do
        [ -f "$file" ] && [ -r "$file" ] || continue
        [ "$file" = "$SENTINEL_DROPIN" ] && continue
        sed -n "s|^User=\([a-z_][a-z0-9_-]*\)\$|\1 sentinel $file|p" "$file"
    done
    for dir in $(printf '%s\n' "$(dirname "$SUDOERS_FILE")" "$(dirname "$AGENT_SUDOERS_FILE")" | sort -u); do
        for file in "$dir"/devloop-*; do
            [ -f "$file" ] && [ -r "$file" ] || continue
            [ "$file" = "$SUDOERS_FILE" ] || [ "$file" = "$AGENT_SUDOERS_FILE" ] && continue
            [ "$file" = "$COORDINATOR_SUDOERS_FILE" ] && continue
            role=supervisor
            case $(basename "$file") in devloop-coordinator*) role=coordinator ;; esac
            awk -v file="$file" -v role="$role" '
                $2 ~ /^ALL=[(][a-z_][a-z0-9_-]*[)]$/ && $1 ~ /^[a-z_][a-z0-9_-]*$/ \
                        && $3 ~ /^NOPASSWD:/ {
                    target = substr($2, 6, length($2) - 6)
                    if ($3 ~ /^NOPASSWD:SETENV:/) {
                        print $1 " devloop " file; print target " agent " file
                    } else if (target != "root") {
                        print $1 " " role " " file " " target; print target " devloop " file
                    }
                }' "$file"
        done
    done
}

# Le impostazioni di Claude Code degli utenti dei ruoli (~/.claude/settings.json): nessun
# aggiornamento automatico, che cambierebbe la CLI di una sessione in corso (#643).
CLAUDE_NO_AUTOUPDATE='{
  "autoUpdates": false,
  "env": {
    "DISABLE_AUTOUPDATER": "1"
  }
}'

# claude_no_autoupdate FILE: le impostazioni di Claude Code FILE hanno autoUpdates: false e
# DISABLE_AUTOUPDATER=1 in env. Torna 1, con il motivo su stdout, se non è così o non si leggono.
claude_no_autoupdate() {
    python3 -I -B - "$1" <<'EOF'
import json, sys
path = sys.argv[1]
try:
    with open(path, 'rb') as fh:
        data = json.loads(fh.read(1 << 20).decode('utf-8'))
except FileNotFoundError:
    print(f'{path} non esiste')
    sys.exit(1)
except (OSError, UnicodeDecodeError, ValueError) as exc:
    print(f'{path} non si legge come JSON ({exc})')
    sys.exit(1)
env = data.get('env') if isinstance(data, dict) else None
if not (isinstance(data, dict) and data.get('autoUpdates') is False
        and isinstance(env, dict) and env.get('DISABLE_AUTOUPDATER') == '1'):
    print(f'{path} non imposta autoUpdates: false e DISABLE_AUTOUPDATER=1')
    sys.exit(1)
EOF
}

# ensure_claude_no_autoupdate USER HOME: ~/.claude (700) e settings.json di Claude Code di USER
# senza aggiornamenti automatici (#643), come per il supervisore: lo scrive se manca; uno che c'è
# senza i due valori è un passo a mano, perché il file può avere altre impostazioni dell'operatore.
ensure_claude_no_autoupdate() {
    local user=$1 home=$2 settings=$2/.claude/settings.json why
    ensure_dir "$home/.claude" 700 "$user"
    if ! [ -f "$settings" ]; then
        put_file "$settings" 600 "$user" "$CLAUDE_NO_AUTOUPDATE" || true
    elif why=$(claude_no_autoupdate "$settings"); then
        ok "$settings: autoUpdates false, DISABLE_AUTOUPDATER=1"
    else
        todo "$why: aggiungere a mano \"autoUpdates\": false e \"env\": {\"DISABLE_AUTOUPDATER\": \"1\"}"
    fi
}

# overlay_app_id FILE ROLE: l'app_id di [apps.ROLE] dell'overlay FILE, o nulla se l'overlay manca,
# non si legge o non dichiara quell'app.
overlay_app_id() {
    [ -f "$1" ] && [ -r "$1" ] && python3 -I -B - "$1" "$2" <<'EOF'
import sys, tomllib
try:
    with open(sys.argv[1], 'rb') as fh:
        apps = tomllib.load(fh).get('apps')
except (OSError, tomllib.TOMLDecodeError):
    sys.exit(0)
table = apps.get(sys.argv[2]) if isinstance(apps, dict) else None
value = table.get('app_id') if isinstance(table, dict) else None
if isinstance(value, int) and not isinstance(value, bool):
    print(value)
EOF
}

# _ops_unit_overlay USER: l'overlay dell'installazione dell'host il cui servizio gira come USER, dal
# log dell'unità (StandardOutput=append:<state_dir>/run.log), o nulla.
_ops_unit_overlay() {
    local file
    for file in "$SYSTEMD_DIR"/devloop*.service "$SYSTEMD_DIR"/devloop@*.service.d/project.conf; do
        [ -f "$file" ] && [ -r "$file" ] || continue
        case $(basename "$file") in
            devloop-role-watchdog* | devloop-role-sentinel* | devloop-failed@*) continue ;;
        esac
        grep -qxF "User=$1" "$file" || continue
        sed -n 's|^StandardOutput=append:\(/.*\)/run\.log$|\1/config.local.toml|p' "$file" | head -n 1
        return
    done
}

# Rifiuta un utente di questa installazione che sull'host abbia già un ruolo in un'altra (ADR
# 0006, R8; D6): ogni progetto ha i suoi utenti devloop, degli agent e del watchdog, che non sono
# di nessun altro progetto né di un altro ruolo. Supervisore e coordinatore sono per insieme di
# app (ADR 0002, precisazione del 2026-11-24 su #643): lo stesso utente di ruolo, nello stesso
# ruolo, si ammette solo se l'overlay di questo progetto e quello dell'altro dichiarano lo stesso
# app_id per l'app del ruolo ([apps.supervisor], [apps.coordinator]). Con app_id diversi si
# ferma; se un overlay non lo dichiara ancora (un progetto nuovo), si ferma anche, tranne con
# --dry-run o negli script che non toccano gli utenti dei ruoli (OPS_DEFER_ROLE_USERS=1), dove
# avvisa: setup-supervisor.sh, setup-coordinator.sh e check-host.sh lo rifanno.
ops_check_host_users() {
    local user role file target var hint app mine theirs
    while read -r user role file target; do
        for var in DEVLOOP_USER AGENT_USER SUPERVISOR_USER COORDINATOR_USER WATCHDOG_USER \
            SENTINEL_USER; do
            [ "${!var}" = "$user" ] || continue
            app=''
            case $var:$role in
                SUPERVISOR_USER:supervisor) app=supervisor ;;
                COORDINATOR_USER:coordinator) app=coordinator ;;
            esac
            # Un progetto senza coordinatore non dà nulla al suo COORDINATOR_USER: il nome di
            # default dell'insieme può essere quello del coordinatore di un altro progetto
            # dell'insieme, e non lo ferma. Il controllo vale quando l'overlay lo dichiara, o
            # quando lo script sta per predisporlo o avviarlo (OPS_WITH_COORDINATOR=1:
            # setup-coordinator.sh, start-role.sh coordinator), prima di dargli accessi.
            if [ "$app" = coordinator ] && [ "${OPS_WITH_COORDINATOR:-0}" != 1 ] \
                && ! overlay_declares_coordinator "$STATE_PATH/config.local.toml"; then
                continue
            fi
            if [ -n "$app" ]; then
                mine=$(overlay_app_id "$STATE_PATH/config.local.toml" "$app" || true)
                theirs=''
                [ -z "$target" ] \
                    || theirs=$(overlay_app_id "$(_ops_unit_overlay "$target" || true)" "$app" || true)
                [ -n "$mine" ] && [ "$mine" = "$theirs" ] && continue
                if [ -n "$mine" ] && [ -n "$theirs" ]; then
                    die "$var '$user' è già l'utente $role di un'altra installazione di devloop dell'host ($file), di un altro insieme di app: app_id $theirs in [apps.$app] del suo overlay, $mine in quello di questo progetto. Un utente di ruolo si condivide solo fra progetti dello stesso insieme (#643): scegliere un altro $var"
                fi
                if [ "$DRY_RUN" = 1 ] || [ "${OPS_DEFER_ROLE_USERS:-0}" = 1 ]; then
                    warn "$var '$user' è già l'utente $role di un'altra installazione di devloop dell'host ($file): si condivide solo se i due overlay dichiarano lo stesso app_id in [apps.$app] (#643), e uno dei due non lo dichiara ancora. setup-$app.sh e check-host.sh si fermano finché non lo dichiarano"
                    continue
                fi
                die "$var '$user' è già l'utente $role di un'altra installazione di devloop dell'host ($file): si condivide solo fra progetti dello stesso insieme di app, con lo stesso app_id in [apps.$app] dei due overlay (#643), e uno dei due non lo dichiara. Scrivere [apps.$app] dell'insieme nell'overlay $STATE_PATH/config.local.toml, o scegliere un altro $var"
            fi
            hint=''
            case $var in
                SUPERVISOR_USER | COORDINATOR_USER)
                    hint=' (supervisore e coordinatore si condividono solo nello stesso ruolo, fra progetti dello stesso insieme di app, #643)' ;;
            esac
            die "$var '$user' è già l'utente $role di un'altra installazione di devloop dell'host ($file): ogni progetto ha i suoi utenti$hint"
        done
    done < <(_ops_host_users)
}

# Esegue un comando, o con --dry-run lo stampa soltanto.
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '  [dry-run]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    printf '  +'
    printf ' %q' "$@"
    printf '\n'
    "$@"
}

# Esegue un comando come USER (sudo -u USER -H), o direttamente se lo script gira già come USER.
as_user() {
    local user=$1
    shift
    if [ "$(id -un)" = "$user" ]; then
        run "$@"
    else
        run sudo -u "$user" -H -- "$@"
    fi
}

user_exists() { id -u "$1" >/dev/null 2>&1; }

# put_file DEST MODE OWNER CONTENUTO: scrive DEST solo se il contenuto è diverso. Con --dry-run
# stampa il contenuto che scriverebbe. Torna 0 se ha scritto (o scriverebbe), 1 se era già così.
# Se non riesce a scrivere ferma lo script con die: si chiama dentro `if` o con `|| true`, dove
# bash non applica errexit al corpo della funzione, quindi ogni errore va gestito qui.
put_file() {
    local dest=$1 mode=$2 owner=$3 content=$4 group tmp
    if [ -f "$dest" ] && cmp -s "$dest" <(printf '%s\n' "$content"); then
        ok "$dest è già aggiornato"
        return 1
    fi
    if [ "$DRY_RUN" = 1 ]; then
        say "  [dry-run] scriverebbe $dest (mode $mode, proprietario $owner):"
        printf '%s\n' "$content" | sed 's/^/    | /'
        return 0
    fi
    group=$(id -gn "$owner") || die "non scrivo $dest: l'utente $owner non esiste"
    tmp=$(mktemp) || die "non scrivo $dest: mktemp non riesce"
    if ! printf '%s\n' "$content" >"$tmp"; then
        rm -f "$tmp"
        die "non scrivo $dest: non riesco a preparare il file temporaneo"
    fi
    if ! run install -m "$mode" -o "$owner" -g "$group" "$tmp" "$dest"; then
        rm -f "$tmp"
        die "install non riesce a scrivere $dest: setup interrotto"
    fi
    rm -f "$tmp"
    return 0
}

# Una directory DIR di OWNER con MODE: la crea, o ne corregge modo e proprietario.
ensure_dir() {
    local dir=$1 mode=$2 owner=$3 group
    group=$(id -gn "$owner" 2>/dev/null || printf '%s' "$owner")
    if [ ! -d "$dir" ]; then
        run install -d -m "$mode" -o "$owner" -g "$group" "$dir"
    elif [ "$(stat -c '%a %U' "$dir")" = "$mode $owner" ]; then
        ok "$dir esiste ($mode, $owner)"
    else
        run chown "$owner:$group" "$dir"
        run chmod "$mode" "$dir"
    fi
}

# ensure_read_acl USER FILE: l'ACL di sola lettura u:USER:r-- su FILE (#554, #543), se manca;
# una più ampia la riduce a r--. Un FILE che non esiste, o che è un link, è un avviso: un file
# sostituito (install, cp, rsync) perde le ACL, e lo script si riesegue dopo averlo rimesso.
ensure_read_acl() {
    local user=$1 file=$2 entry
    if ! [ -f "$file" ] || [ -L "$file" ]; then
        warn "$file non esiste o è un link: crearlo (setup-devloop.sh), poi rieseguire questo script"
        return 0
    fi
    # getfacl può aggiungere alla riga un commento #effective.
    entry=$(getfacl -p --omit-header "$file" 2>/dev/null | grep "^user:$user:" | head -n 1 || true)
    entry=${entry%%[[:space:]]*}
    if [ "$entry" = "user:$user:r--" ]; then
        ok "ACL presente: $entry"
    else
        [ -z "$entry" ] || warn "ACL attuale $entry: la riduco a r--"
        run setfacl -m "u:$user:r--" "$file"
    fi
}

# ensure_traverse_acl USER DIR: l'ACL di solo attraversamento u:USER:--x sulla directory DIR
# (#573), se manca; una più ampia la riduce a --x. Serve a raggiungere per percorso i file di uno
# state_dir 750 (run.log, l'overlay con la sua ACL di sola lettura), senza elencarlo. Una DIR che
# non esiste, o che è un link, è un avviso: si crea (setup-devloop.sh), poi si riesegue lo script.
ensure_traverse_acl() {
    local user=$1 dir=$2 entry
    if ! [ -d "$dir" ] || [ -L "$dir" ]; then
        warn "$dir non esiste o è un link: crearlo (setup-devloop.sh), poi rieseguire questo script"
        return 0
    fi
    entry=$(getfacl -p --omit-header "$dir" 2>/dev/null | grep "^user:$user:" | head -n 1 || true)
    entry=${entry%%[[:space:]]*}
    if [ "$entry" = "user:$user:--x" ]; then
        ok "ACL presente: $entry"
    else
        [ -z "$entry" ] || warn "ACL attuale $entry: la riduco a --x"
        run setfacl -m "u:$user:--x" "$dir"
    fi
}

# ensure_events_acl USER: la lettura del flusso degli eventi del progetto (#683) per USER, il
# supervisore o il coordinatore, che la sveglia dei ruoli legge come sé stessa. La directory
# STATE_PATH/events, di DEVLOOP_USER, 750 (la crea, se manca: devloop run vi scrive); su di essa
# l'ACL u:USER:r-x, e l'ACL di default d:u:USER:r--, che ogni segmento nuovo eredita; su ogni file
# che c'è già u:USER:r--. Nessuna scrittura: una voce più ampia si riduce. L'attraversamento
# delle directory sopra (la home di devloop, lo state_dir) lo mettono gli script dei ruoli. Per
# progetto: l'utente di un ruolo condiviso fra più progetti la riceve sul flusso di ciascun
# progetto da setup-*.sh di quel progetto.
ensure_events_acl() {
    local user=$1 dir=$STATE_PATH/$EVENTS_DIR entries file
    if [ -L "$dir" ] || { [ -e "$dir" ] && ! [ -d "$dir" ]; }; then
        warn "$dir non è una directory: rimuoverlo come $DEVLOOP_USER, poi rieseguire questo script"
        return 0
    fi
    if ! [ -d "$STATE_PATH" ]; then
        warn "$STATE_PATH non esiste: crearlo (setup-devloop.sh), poi rieseguire questo script"
        return 0
    fi
    ensure_dir "$dir" 750 "$DEVLOOP_USER"
    # getfacl può aggiungere alle righe un commento #effective.
    entries=$(getfacl -p --omit-header "$dir" 2>/dev/null | sed 's/[[:space:]].*//' || true)
    if grep -qx "user:$user:r-x" <<<"$entries" && grep -qx "default:user:$user:r--" <<<"$entries"
    then
        ok "ACL presenti su $dir: user:$user:r-x, default:user:$user:r--"
    else
        run setfacl -m "u:$user:r-x,d:u:$user:r--" "$dir"
    fi
    for file in "$dir"/*; do
        [ -f "$file" ] && ! [ -L "$file" ] || continue
        ensure_read_acl "$user" "$file"
    done
}

# ops_alert_issue: l'issue degli allarmi ([run].alert_issue dell'overlay dello state_dir), se si
# legge; nulla altrimenti.
ops_alert_issue() {
    python3 -I -B - "$STATE_PATH/config.local.toml" 2>/dev/null <<'EOF'
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    value = (tomllib.load(fh).get('run') or {}).get('alert_issue')
if isinstance(value, int) and not isinstance(value, bool) and value > 0:
    print(value)
EOF
}

# ops_role_event ROLE LINE: LINE, una riga del flusso degli eventi, sveglia ROLE (supervisor o
# coordinator; docs/devloop/guide.md, "Waking the supervising agent"). LINE può avere l'ora
# ("HH:MM ") davanti, come la stampa devloop watch: i filtri la saltano. ALERT_ISSUE è l'issue
# degli allarmi (ops_alert_issue). Il filtro della sveglia dei ruoli (role-wait.sh, #602) e della
# sentinella dei ruoli a riposo (role-sentinel.sh, #684). La riga della base di devloop watch non
# è un evento per nessuno.
ops_role_event() {
    local role=$1 line=$2 other=supervisor
    local operator=agent:needs-operator coordinator=agent:needs-coordinator
    [ "$role" = supervisor ] && other=coordinator
    [[ $line =~ ^[0-9]{2}:[0-9]{2}\ (.*)$ ]] && line=${BASH_REMATCH[1]}
    [ -n "$line" ] || return 1
    [[ $line == 'watch baseline:'* ]] && return 1
    # La routine (#682), riconosciuta da devloop run per l'autore, mai per il testo: l'archivio
    # del riepilogo giornaliero ("report by"), i commenti del ruolo stesso, il diario dell'altro
    # ruolo (un suo commento sull'issue degli allarmi).
    [[ $line =~ ^(PR|issue)\ \#[0-9]+\ (report|$role\ comment)\ by\  ]] && return 1
    [ -n "${ALERT_ISSUE:-}" ] && [[ $line =~ ^issue\ \#$ALERT_ISSUE\ $other\ comment\ by\  ]] \
        && return 1
    [ "$role" = supervisor ] && return 0
    # Una delle due label aggiunta, o fra quelle di un'issue o di una PR nuova ("new PR #N [a,b]
    # titolo").
    [[ $line == *" +$operator"* || $line == *" +$coordinator"* ]] && return 0
    if [[ $line =~ ^new\ (PR|issue)\ \#[0-9]+\ \[([^]]*)\] ]]; then
        case ",${BASH_REMATCH[2]}," in
            *",$operator,"* | *",$coordinator,"*) return 0 ;;
        esac
    fi
    [[ ${line,,} == *"merge spetta all'operatore"* ]] && return 0
    [[ $line == 'watch error:'* ]] && return 0
    # Una risposta dell'operatore (#676): devloop run la distingue da un commento qualunque.
    [[ $line =~ ^(PR|issue)\ \#[0-9]+\ operator\ reply\ by\  ]] && return 0
    [ -n "${ALERT_ISSUE:-}" ] && [[ $line =~ ^issue\ \#$ALERT_ISSUE\ (operator\ )?comment\ by\  ]] \
        && return 0
    return 1
}

# ops_log_error LINE: LINE, una riga di run.log, è un errore che sveglia il coordinatore: un
# Traceback, un allarme (ALERT:), un 401 o "Bad credentials", il rifiuto dell'avvio ("devloop:").
ops_log_error() {
    grep -Eq 'Traceback|ALERT:|(^|[^0-9])401([^0-9]|$)|[Bb]ad credentials|^devloop: ' <<<"$1"
}

# ops_rest_start ROLE: l'inizio (secondi dall'epoca) del riposo dichiarato di ROLE (#684), da
# COORDINATION_DIR/rest/ROLE ("<inizio> <motivo>"); nulla, e 1, se non è a riposo o lo stato non
# si legge.
ops_rest_start() {
    local at='' why=''
    [ -f "$COORDINATION_DIR/$COORDINATION_REST/$1" ] || return 1
    read -r at why <"$COORDINATION_DIR/$COORDINATION_REST/$1" 2>/dev/null || return 1
    [[ $at =~ ^[0-9]{1,12}$ ]] || return 1
    printf '%s\n' "$at"
}

# ops_rest_wake ROLE: l'ora (secondi dall'epoca) del segnale di risveglio di ROLE che la
# sentinella ha scritto (COORDINATION_DIR/rest/ROLE.wake, "<ora> <motivo>"), e il motivo; nulla,
# e 1, se non c'è.
ops_rest_wake() {
    local at='' why=''
    [ -f "$COORDINATION_DIR/$COORDINATION_REST/$1.wake" ] || return 1
    read -r at why <"$COORDINATION_DIR/$COORDINATION_REST/$1.wake" 2>/dev/null || return 1
    [[ $at =~ ^[0-9]{1,12}$ ]] || return 1
    printf '%s %s\n' "$at" "$why"
}

# coordination_dirs: le directory della casella, una per riga: COORDINATION_DIR, una directory di
# messaggi per destinatario (COORDINATION_BOXES e COORDINATION_OPERATOR_BOX, #663) con la sua
# done/, e quella dei battiti (#602, COORDINATION_HEARTBEAT).
coordination_dirs() {
    local box
    printf '%s\n' "$COORDINATION_DIR"
    for box in $COORDINATION_BOXES $COORDINATION_OPERATOR_BOX; do
        printf '%s\n' "$COORDINATION_DIR/$box" "$COORDINATION_DIR/$box/done"
    done
    printf '%s\n' "$COORDINATION_DIR/$COORDINATION_HEARTBEAT"
    printf '%s\n' "$COORDINATION_DIR/$COORDINATION_REST"
}

# coordination_mode DIR: il modo di una directory di coordination_dirs: 3770 per quella del
# riposo (#684), con lo sticky bit, 2770 per le altre.
coordination_mode() {
    if [ "$1" = "$COORDINATION_DIR/$COORDINATION_REST" ]; then
        printf '3770'
    else
        printf '2770'
    fi
}

# ensure_coordination USER: la casella fra supervisore e coordinatore (#543). Il gruppo
# COORDINATION_GROUP con dentro USER, e le directory di coordination_dirs, tutte
# root:COORDINATION_GROUP 2770 (3770 quella del riposo, #684): i due ruoli si scrivono a vicenda
# e scrivono all'operatore in to-operator/ (#663), nessun altro utente vi entra se non con le ACL
# del watchdog, della sentinella e di devloop. Una voce che è un link o un file al
# posto di una directory (la casella di prima, con un file per destinatario) ferma lo script.
ensure_coordination() {
    local user=$1 dir actual mode
    if getent group "$COORDINATION_GROUP" >/dev/null; then
        ok "il gruppo $COORDINATION_GROUP esiste"
    else
        run groupadd --system "$COORDINATION_GROUP"
    fi
    if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qxF "$COORDINATION_GROUP"; then
        ok "$user è nel gruppo $COORDINATION_GROUP"
    else
        # Vale dalla prossima sessione dell'utente.
        run usermod -aG "$COORDINATION_GROUP" "$user"
    fi
    for dir in $(coordination_dirs); do
        if [ -L "$dir" ] || { [ -e "$dir" ] && ! [ -d "$dir" ]; }; then
            die "$dir non è una directory: la casella di prima va spostata (ops/host/migrate.md, \"Casella fra i ruoli e sveglia del coordinatore (#543)\"); niente cambiato da qui"
        fi
        mode=$(coordination_mode "$dir")
        if ! [ -d "$dir" ]; then
            run install -d -m "$mode" -o root -g "$COORDINATION_GROUP" "$dir"
            continue
        fi
        actual=$(stat -c '%a %U %G' "$dir")
        if [ "$actual" = "$mode root $COORDINATION_GROUP" ]; then
            ok "$dir: $mode, root, gruppo $COORDINATION_GROUP"
        else
            run chown "root:$COORDINATION_GROUP" "$dir"
            run chmod "$mode" "$dir"
        fi
    done
}

# ops_copy_file SRC DEST: DEST uguale a SRC byte per byte, 644 di root; lo riscrive solo se serve.
# Per le copie di root degli script dei ruoli (COORDINATOR_OPS_DIR, SUPERVISOR_OPS_DIR,
# WATCHDOG_OPS_DIR).
ops_copy_file() {
    local src=$1 dest=$2
    [ -f "$src" ] || die "manca $src"
    if [ -f "$dest" ] && ! [ -L "$dest" ] && cmp -s "$src" "$dest" \
        && [ "$(stat -c '%a %U' "$dest")" = '644 root' ]; then
        ok "$dest è già aggiornato"
        return
    fi
    run install -m 644 -o root -g root "$src" "$dest" \
        || die "install non riesce a scrivere $dest: setup interrotto"
}

# ops_root_copy DIR FILE...: in DIR, di root 755, i FILE di ops/ e i parametri (params.env).
ops_root_copy() {
    local dir=$1 name
    shift
    ensure_dir "$dir" 755 root
    ensure_dir "$dir/host" 755 root
    for name in "$@"; do
        ops_copy_file "$OPS_DIR/$name" "$dir/$name"
    done
    ops_copy_file "$PARAMS_FILE" "$dir/params.env"
}

# ensure_dir_acl USER DIR PERMS: sulla directory DIR l'ACL u:USER:PERMS (per esempio --x o -wx),
# se manca; una diversa la riporta a PERMS. Una DIR che non esiste, o che è un link, è un avviso.
ensure_dir_acl() {
    local user=$1 dir=$2 perms=$3 entry
    if ! [ -d "$dir" ] || [ -L "$dir" ]; then
        warn "$dir non esiste o è un link: crearlo, poi rieseguire questo script"
        return 0
    fi
    entry=$(getfacl -p --omit-header "$dir" 2>/dev/null | grep "^user:$user:" | head -n 1 || true)
    entry=${entry%%[[:space:]]*}
    if [ "$entry" = "user:$user:$perms" ]; then
        ok "ACL presente: $entry su $dir"
    else
        [ -z "$entry" ] || warn "ACL attuale $entry su $dir: la riporto a $perms"
        run setfacl -m "u:$user:$perms" "$dir"
    fi
}

# overlay_declares_coordinator FILE: l'overlay FILE dichiara il coordinatore del progetto (#571),
# con la sua app ([apps.coordinator]) o il suo ruolo ([roles.coordinator]).
overlay_declares_coordinator() {
    [ -f "$1" ] && python3 -I -B - "$1" <<'EOF'
import sys, tomllib
try:
    with open(sys.argv[1], 'rb') as fh:
        data = tomllib.load(fh)
except (OSError, tomllib.TOMLDecodeError):
    sys.exit(1)
declared = any(isinstance(data.get(table), dict) and 'coordinator' in data[table]
               for table in ('apps', 'roles'))
sys.exit(0 if declared else 1)
EOF
}

# Le ACL del watchdog dei ruoli (#602) sulla casella del suo progetto, una "<directory> <permessi>"
# per riga: l'attraversamento di COORDINATION_DIR, di heartbeat/ (legge i battiti, 664, senza
# elencarli) e di rest/ (lo stato di riposo, il lock e il segnale di risveglio, #684), e in
# to-supervisor/ e to-coordinator/ la scrittura dei messaggi e, dal #684, l'elenco (i nomi e le
# ore dei messaggi in attesa di un ruolo a riposo; i messaggi, 640 del gruppo, non li legge).
watchdog_acls() {
    local box
    printf '%s --x\n' "$COORDINATION_DIR" "$COORDINATION_DIR/$COORDINATION_HEARTBEAT" \
        "$COORDINATION_DIR/$COORDINATION_REST"
    for box in $COORDINATION_BOXES; do
        printf '%s rwx\n' "$COORDINATION_DIR/$box"
    done
}

# Le ACL della sentinella dei ruoli a riposo (#684) sulla casella del suo progetto, come
# watchdog_acls: l'attraversamento di COORDINATION_DIR, in rest/ la scrittura del segnale di
# risveglio (senza elencare: legge per nome lo stato dei due ruoli; lo sticky bit le lascia
# rimuovere solo i suoi file) e l'elenco di to-supervisor/ e to-coordinator/, senza scrittura.
sentinel_acls() {
    local box
    printf '%s --x\n' "$COORDINATION_DIR"
    printf '%s -wx\n' "$COORDINATION_DIR/$COORDINATION_REST"
    for box in $COORDINATION_BOXES; do
        printf '%s r-x\n' "$COORDINATION_DIR/$box"
    done
}

# Le ACL di devloop sulla casella (#684): l'attraversamento di COORDINATION_DIR e di rest/, per
# leggere per nome lo stato di riposo dei ruoli ([run] rest_dir dell'overlay). Nessun'altra.
rest_devloop_acls() {
    printf '%s --x\n' "$COORDINATION_DIR" "$COORDINATION_DIR/$COORDINATION_REST"
}

# ensure_role_watchdog BOTH: il watchdog dei ruoli (#602). Le unità template
# devloop-role-watchdog@.service, @.timer e -failed@.service in SYSTEMD_DIR, una volta per host;
# con BOTH=1 (supervisore e coordinatore del progetto ci sono) anche l'utente WATCHDOG_USER, fuori
# dal gruppo dei ruoli, le sue ACL sulla casella del progetto, la copia di root WATCHDOG_OPS_DIR
# con i parametri, le drop-in dell'istanza e il timer abilitato e avviato. Con BOTH=0 lo dice e
# lascia l'istanza com'è.
ensure_role_watchdog() {
    local both=$1 unit changed=0 dropin line dir perms
    for unit in $OPS_WATCHDOG_UNITS; do
        put_file "$SYSTEMD_DIR/$unit" 644 root "$(render "$OPS_DIR/host/$unit")" && changed=1
    done
    if [ "$both" != 1 ]; then
        ok "il progetto non ha ancora sia il supervisore sia il coordinatore: l'istanza $WATCHDOG_UNIT si prepara quando ci sono entrambi (rieseguire setup-supervisor.sh o setup-coordinator.sh)"
        [ "$changed" = 0 ] || run systemctl daemon-reload
        return 0
    fi
    if user_exists "$WATCHDOG_USER"; then
        ok "l'utente $WATCHDOG_USER esiste"
    else
        run useradd --system --user-group --no-create-home --home-dir / \
            --shell /usr/sbin/nologin "$WATCHDOG_USER"
    fi
    if id -nG "$WATCHDOG_USER" 2>/dev/null | tr ' ' '\n' | grep -qxF "$COORDINATION_GROUP"; then
        die "$WATCHDOG_USER è nel gruppo $COORDINATION_GROUP: il watchdog entra solo nella casella del suo progetto, con le ACL (gpasswd -d $WATCHDOG_USER $COORDINATION_GROUP)"
    fi
    while read -r dir perms; do
        ensure_dir_acl "$WATCHDOG_USER" "$dir" "$perms"
    done < <(watchdog_acls)
    # Il lavoro trattenuto da devloop run (#684), nella directory del flusso degli eventi: il
    # watchdog la legge, come i ruoli, con l'attraversamento della home di devloop e dello
    # state_dir.
    ensure_state_reader "$WATCHDOG_USER"
    ops_root_copy "$WATCHDOG_OPS_DIR" $OPS_WATCHDOG_FILES
    line=$(render "$OPS_DIR/host/role-watchdog-project.conf")
    for dropin in "$WATCHDOG_DROPIN" "$WATCHDOG_FAILED_DROPIN"; do
        ensure_dir "$(dirname "$dropin")" 755 root
        put_file "$dropin" 644 root "$line" && changed=1
    done
    ensure_role_sentinel
    [ "$SENTINEL_CHANGED" = 0 ] || changed=1
    [ "$changed" = 0 ] || run systemctl daemon-reload
    run systemctl enable --now "$WATCHDOG_UNIT.timer"
    run systemctl enable --now "$SENTINEL_UNIT.service"
}

# ensure_state_reader USER: USER legge il flusso degli eventi del progetto (#683) e run.log:
# l'attraversamento della home di devloop e dello state_dir, e le ACL di sola lettura del flusso.
ensure_state_reader() {
    local user=$1
    ensure_traverse_acl "$user" "$DEVLOOP_HOME"
    ensure_traverse_acl "$user" "$STATE_PATH"
    ensure_events_acl "$user"
}

# ensure_role_sentinel: la sentinella dei ruoli a riposo (#684), con il watchdog: le unità
# template in SYSTEMD_DIR, l'utente SENTINEL_USER fuori dal gruppo dei ruoli, le sue ACL sulla
# casella (sentinel_acls) e sullo state_dir (ensure_state_reader), la drop-in dell'istanza; e
# l'attraversamento della casella e di rest/ per devloop (rest_devloop_acls), che non avvia lavoro
# nuovo con un ruolo a riposo. SENTINEL_CHANGED=1 se ha scritto (o scriverebbe) un'unità o la
# drop-in.
SENTINEL_CHANGED=0
ensure_role_sentinel() {
    local unit dir perms
    SENTINEL_CHANGED=0
    for unit in $OPS_SENTINEL_UNITS; do
        put_file "$SYSTEMD_DIR/$unit" 644 root "$(render "$OPS_DIR/host/$unit")" \
            && SENTINEL_CHANGED=1
    done
    if user_exists "$SENTINEL_USER"; then
        ok "l'utente $SENTINEL_USER esiste"
    else
        run useradd --system --user-group --no-create-home --home-dir / \
            --shell /usr/sbin/nologin "$SENTINEL_USER"
    fi
    if id -nG "$SENTINEL_USER" 2>/dev/null | tr ' ' '\n' | grep -qxF "$COORDINATION_GROUP"; then
        die "$SENTINEL_USER è nel gruppo $COORDINATION_GROUP: la sentinella entra solo nella casella del suo progetto, con le ACL (gpasswd -d $SENTINEL_USER $COORDINATION_GROUP)"
    fi
    while read -r dir perms; do
        ensure_dir_acl "$SENTINEL_USER" "$dir" "$perms"
    done < <(sentinel_acls)
    ensure_state_reader "$SENTINEL_USER"
    while read -r dir perms; do
        ensure_dir_acl "$DEVLOOP_USER" "$dir" "$perms"
    done < <(rest_devloop_acls)
    ensure_dir "$(dirname "$SENTINEL_DROPIN")" 755 root
    put_file "$SENTINEL_DROPIN" 644 root "$(render "$OPS_DIR/host/role-sentinel-project.conf")" \
        && SENTINEL_CHANGED=1
    return 0
}

# clone_origin DIR: l'URL dell'origin del clone DIR, letto da .git/config senza eseguire git nel
# clone d'altri; vuoto se non si legge. origin_slug URL: owner/nome di quell'URL di GitHub.
clone_origin() {
    awk '/^[[:space:]]*\[/ { inside = ($0 ~ /^[[:space:]]*\[remote "origin"\]/) }
         inside && /^[[:space:]]*url[[:space:]]*=/ { sub(/^[^=]*=[[:space:]]*/, ""); print; exit }' \
        "$1/.git/config" 2>/dev/null || true
}
origin_slug() { printf '%s' "$1" | sed -E 's#^.*github\.com[:/]##; s#/+$##; s#\.git$##'; }

# ensure_source_clone USER DIR: in DIR, di USER, un clone di SOURCE_REPO con il tag SOURCE_REF
# di origin estratto (#571). Da lì il supervisore e il coordinatore di un progetto con la
# sorgente a parte lanciano scripts/supervisor-token.py e scripts/coordinator-token.py, che il
# repository gestito non contiene. Un clone di un altro repository ferma lo script; uno che c'è
# già passa al tag SOURCE_REF, che si riesegue lo script dopo averlo cambiato.
ensure_source_clone() {
    local user=$1 dir=$2 origin slug
    if [ -d "$dir/.git" ]; then
        origin=$(clone_origin "$dir")
        slug=$(origin_slug "$origin")
        [ "${slug,,}" = "${SOURCE_REPO,,}" ] \
            || die "$dir è un clone di '${origin:-origine sconosciuta}', non di $SOURCE_REPO: scegliere un altro percorso"
        ok "$dir è già un clone di $SOURCE_REPO"
    else
        as_user "$user" git clone --quiet "https://github.com/$SOURCE_REPO.git" "$dir"
    fi
    # Il tag come è su origin, anche se il clone ne aveva uno locale con lo stesso nome.
    as_user "$user" git -C "$dir" fetch --quiet --no-tags origin \
        "+refs/tags/$SOURCE_REF:refs/tags/$SOURCE_REF"
    as_user "$user" git -C "$dir" checkout --quiet --detach "refs/tags/$SOURCE_REF"
}

# Sostituisce i segnaposto @NOME@ di un file dichiarativo con i valori dei parametri.
render() {
    local text var
    text=$(<"$1")
    for var in $OPS_TEMPLATE_VARS; do
        text=${text//"@$var@"/"${!var}"}
    done
    if [[ $text =~ @[A-Z_]+@ ]]; then
        die "segnaposto non risolto in $1: ${BASH_REMATCH[0]}"
    fi
    printf '%s\n' "$text"
}

confirm() {
    local answer
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || die "$1: serve una conferma e non c'è un terminale; rieseguire con --yes"
    read -r -p "$1 [s/N] " answer
    [[ $answer =~ ^[sSyY] ]]
}

# Controlla /etc/os-release (OPS_OS_RELEASE nei test) e l'architettura. Su una piattaforma
# diversa da quella verificata avvisa e chiede conferma; non rifiuta.
ops_check_platform() {
    local file=${OPS_OS_RELEASE:-/etc/os-release} id='' version='' arch
    arch=${OPS_ARCH:-$(uname -m)}
    if [ -r "$file" ]; then
        # shellcheck disable=SC1090
        id=$(. "$file" && printf '%s' "${ID:-}")
        # shellcheck disable=SC1090
        version=$(. "$file" && printf '%s' "${VERSION_ID:-}")
    fi
    step 'Piattaforma'
    if [ "$id" = "$VERIFIED_ID" ] && [ "$version" = "$VERIFIED_VERSION" ] && [ "$arch" = "$VERIFIED_ARCH" ]; then
        ok "Ubuntu $version su $arch: piattaforma verificata"
        return 0
    fi
    warn "piattaforma non verificata: ${id:-sconosciuta} ${version:-?} su $arch (verificata: Ubuntu 26.04 LTS su aarch64)"
    if [ "$DRY_RUN" = 1 ] || [ "${1:-}" = readonly ]; then
        return 0
    fi
    confirm 'Continuare su una piattaforma non verificata?' || die "interrotto dall'operatore"
}

require_root() {
    [ "$DRY_RUN" = 1 ] || [ "$(id -u)" = 0 ] || die 'va eseguito come root (sudo), oppure con --dry-run'
}
