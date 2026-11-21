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

# Lo state_dir di devloop, relativo al clone, che scripts/devloop-pinned usa senza --state-dir.
OPS_STATE_DIR=.ssdlc/devloop

# I segnaposto @NOME@ che `render` sostituisce nei file dichiarativi (unità, sudoers).
OPS_TEMPLATE_VARS='REPO DEVLOOP_USER SUPERVISOR_USER AGENT_USER DEVLOOP_HOME AGENT_HOME DEVLOOP_CLONE STATE_DIR STATE_PATH LAUNCHER_CMD DEVLOOP_PATH AGENT_PATH SERVICE_NAME REPO_LOCK_DIR REPO_LOCK_GROUP'

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
    .claude/history.jsonl:f .claude/stats-cache.json:f
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
# quella directory, con il percorso relativo <directory>/<nome> (un solo nome, né . né ..);
# ogni altro link con quel nome è rifiutato. agy crea a ogni avvio cli.log -> log/cli-<data>.log.
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

# agent_cli_entry_known DIR NAME TYPE [TARGET]: la voce NAME, di tipo TYPE (%y di find), della
# directory delle CLI DIR (relativa alla home) è nella lista, o è sigillata o una directory delle
# CLI; TARGET è il contenuto del link, quando TYPE è l. Torna 0 se è nota (sigillata e directory
# delle CLI: il proprietario lo controlla chi chiama), 1 se manca dalla lista, 2 se è nella lista
# con un altro tipo, 3 se è un link ammesso per quel nome che porta fuori dalla sua directory.
agent_cli_entry_known() {
    local dir=$1 name=$2 type=$3 target=${4-} entry pattern types link matched=0
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
                            agent_cli_link_allowed "$target" "${link##*:}" && return 0
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
codex_memories() {
    python3 - "$@" <<'EOF'
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
    # template. Senza, un solo progetto con devloop.service, come prima.
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
        # Il supervisore è uno per host: la sua key_dir e il suo clone sono del progetto.
        : "${SUPERVISOR_KEY_DIR:=${SUPERVISOR_HOME:-/home/${SUPERVISOR_USER:-supervisor}}/.ghapps-$PROJECT}"
        : "${SUPERVISOR_CLONE:=${SUPERVISOR_HOME:-/home/${SUPERVISOR_USER:-supervisor}}/devloop-$PROJECT}"
        [ "$SERVICE_NAME" = "devloop@$PROJECT" ] \
            || die "con PROJECT il servizio è l'istanza devloop@$PROJECT dell'unità template: '$SERVICE_NAME'"
    fi
    : "${DEVLOOP_USER:=devloop}"
    : "${SUPERVISOR_USER:=supervisor}"
    : "${AGENT_USER:=devloop-agent}"
    : "${DEFAULT_BRANCH:=main}"
    : "${GITHUB_OWNER_TYPE:=user}"
    : "${DEVLOOP_HOME:=/home/$DEVLOOP_USER}"
    : "${SUPERVISOR_HOME:=/home/$SUPERVISOR_USER}"
    : "${AGENT_HOME:=/home/$AGENT_USER}"
    : "${DEVLOOP_CLONE:=$DEVLOOP_HOME/${REPO#*/}}"
    : "${SUPERVISOR_CLONE:=$SUPERVISOR_HOME/${REPO#*/}}"
    : "${STATE_DIR:=$OPS_STATE_DIR}"
    : "${DEVLOOP_KEY_DIR:=$DEVLOOP_HOME/.ghapps}"
    : "${SUPERVISOR_KEY_DIR:=$SUPERVISOR_HOME/.ghapps}"
    : "${DEVLOOP_KEYS:=dispatcher merger claude-agent codex-agent agy-agent}"
    : "${SUPERVISOR_KEYS:=supervisor claude-agent}"
    : "${DEVLOOP_PATH:=$DEVLOOP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin}"
    : "${AGENT_PATH:=$AGENT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin}"
    : "${SERVICE_NAME:=devloop}"
    : "${SYSTEMD_DIR:=/etc/systemd/system}"
    : "${SUDOERS_FILE:=/etc/sudoers.d/devloop-supervisor}"
    : "${AGENT_SUDOERS_FILE:=/etc/sudoers.d/devloop-agent}"
    : "${APPS:=dispatcher merger claude-agent codex-agent agy-agent implementer supervisor}"
    : "${SOURCE_REPO:=}"
    : "${SOURCE_REF:=}"
    : "${SOURCE_CLONE:=$DEVLOOP_HOME/devloop-source}"
    : "${LAUNCHER_DIR:=/usr/local/lib/devloop}"
    : "${REPO_LOCK_DIR:=/run/devloop/repos}"
    : "${REPO_LOCK_GROUP:=devloop-repos}"
    : "${TMPFILES_FILE:=/etc/tmpfiles.d/devloop-repos.conf}"

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
    for sudoers in SUDOERS_FILE AGENT_SUDOERS_FILE; do
        [[ $(basename "${!sudoers}") =~ ^[A-Za-z0-9_-]+$ ]] \
            || die "il nome di $sudoers non può contenere '.' né '~' (sudo lo ignorerebbe)"
    done
    _require_name DEVLOOP_USER
    _require_name SUPERVISOR_USER
    _require_name AGENT_USER
    _require_name REPO_LOCK_GROUP
    [ "$AGENT_USER" != "$DEVLOOP_USER" ] || die "AGENT_USER deve essere diverso da DEVLOOP_USER"
    [ "$AGENT_USER" != "$SUPERVISOR_USER" ] || die "AGENT_USER deve essere diverso da SUPERVISOR_USER"
    local path
    for path in DEVLOOP_HOME SUPERVISOR_HOME AGENT_HOME DEVLOOP_CLONE SUPERVISOR_CLONE \
        DEVLOOP_KEY_DIR SUPERVISOR_KEY_DIR SYSTEMD_DIR SUDOERS_FILE AGENT_SUDOERS_FILE \
        TMPFILES_FILE; do
        _require_abs "$path"
    done
    ops_check_host_users
}

# Gli utenti delle altre installazioni di devloop dell'host (ADR 0006, R8), una riga
# "<utente> <ruolo> <file>" ciascuno, con ruolo devloop, agent o supervisor: dalle loro unità
# (User=) e dai loro sudoers (devloop-*: "<devloop> ALL=(<agent>) NOPASSWD:SETENV:" di
# sudoers-agent, "<supervisore> ALL=(<devloop>) NOPASSWD:" di sudoers-supervisor). Salta i file
# di questa installazione e quelli che non può leggere: i sudoers sono di root, quindi senza
# root (--dry-run) legge solo le unità.
_ops_host_users() {
    local file dir
    for file in "$SYSTEMD_DIR"/devloop*.service "$SYSTEMD_DIR"/devloop@*.service.d/project.conf; do
        [ -f "$file" ] && [ -r "$file" ] || continue
        [ "$file" = "$UNIT_FILE" ] || [ "$file" = "$DROPIN_FILE" ] && continue
        sed -n "s|^User=\([a-z_][a-z0-9_-]*\)\$|\1 devloop $file|p" "$file"
    done
    for dir in $(printf '%s\n' "$(dirname "$SUDOERS_FILE")" "$(dirname "$AGENT_SUDOERS_FILE")" | sort -u); do
        for file in "$dir"/devloop-*; do
            [ -f "$file" ] && [ -r "$file" ] || continue
            [ "$file" = "$SUDOERS_FILE" ] || [ "$file" = "$AGENT_SUDOERS_FILE" ] && continue
            awk -v file="$file" '
                $2 ~ /^ALL=[(][a-z_][a-z0-9_-]*[)]$/ && $1 ~ /^[a-z_][a-z0-9_-]*$/ \
                        && $3 ~ /^NOPASSWD:/ {
                    target = substr($2, 6, length($2) - 6)
                    if ($3 ~ /^NOPASSWD:SETENV:/) {
                        print $1 " devloop " file; print target " agent " file
                    } else if (target != "root") {
                        print $1 " supervisor " file; print target " devloop " file
                    }
                }' "$file"
        done
    done
}

# Rifiuta un utente di questa installazione che sull'host abbia già un ruolo in un'altra (ADR
# 0006, R8; D6): ogni progetto ha il suo utente devloop e il suo utente degli agent, che non sono
# di nessun altro progetto né di un altro ruolo. Solo il supervisore, uno per host, è lo stesso in
# ogni progetto.
ops_check_host_users() {
    local user role file var
    while read -r user role file; do
        for var in DEVLOOP_USER AGENT_USER SUPERVISOR_USER; do
            [ "${!var}" = "$user" ] || continue
            [ "$var" = SUPERVISOR_USER ] && [ "$role" = supervisor ] && continue
            die "$var '$user' è già l'utente $role di un'altra installazione di devloop dell'host ($file): ogni progetto ha i suoi utenti"
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
