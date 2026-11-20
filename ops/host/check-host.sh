#!/usr/bin/env bash
# Verifica l'installazione di devloop e del supervisore: utenti, directory private (700), chiavi
# delle app (600, del proprietario giusto, solo quelle attese), ACL sulla home di devloop,
# sudoers, l'utente degli agent (non legge key_dir né state_dir, non scrive .git né le directory
# che contengono worktree e turni, è nell'overlay, non cambia istruzioni, configurazione e
# binari delle sue CLI), il lock per repository (directory comune,
# gruppo, overlay; ADR 0006, R8), servizio, CLI degli agent e `devloop apps`. Con più progetti
# sullo stesso host si esegue una volta per progetto, con i suoi parametri (--params).
# Non cambia nulla.
# Esce con 0 se non trova errori, con 1 altrimenti; gli avvisi non contano come errori.
# Va eseguito come root (sudo): alcune verifiche leggono file degli altri utenti.
#
# --dry-run: fa le verifiche locali in sola lettura e stampa, senza eseguirle, quelle che passano
# da sudo o dalla rete (--version delle CLI, lettura del log come supervisore, visudo, devloop apps).

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e  # una verifica fallita si conta, non interrompe le altre

CHECK_APPS=1

usage() {
    say "Uso: sudo bash $0 [--params FILE] [--dry-run] [--no-apps]"
    say
    say '  --no-apps       non esegue `devloop apps` (che interroga GitHub)'
    say "$OPS_COMMON_HELP"
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --no-apps) CHECK_APPS=0; shift ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

ops_load_params
ops_check_platform readonly
[ "$(id -u)" = 0 ] || warn 'non eseguito come root: le verifiche sui file degli altri utenti possono fallire'

# Esegue CMD come USER e conta l'esito; con --dry-run lo stampa soltanto.
check_as() {
    local user=$1 what=$2
    shift 2
    if [ "$DRY_RUN" = 1 ]; then
        run sudo -u "$user" -H -- "$@"
        return 0
    fi
    local -a cmd=("$@")
    [ "$(id -un)" = "$user" ] || cmd=(sudo -u "$user" -H -- "$@")
    if "${cmd[@]}"; then
        ok "$what"
    else
        fail "$what: non riuscito"
    fi
}

# La directory DIR esiste, con MODE e proprietario OWNER.
check_dir() {
    local dir=$1 mode=$2 owner=$3 actual
    if [ ! -d "$dir" ]; then
        fail "$dir non esiste"
        return
    fi
    actual=$(stat -c '%a %U' "$dir")
    if [ "$actual" = "$mode $owner" ]; then
        ok "$dir: $mode, $owner"
    else
        fail "$dir è $actual, deve essere $mode $owner"
    fi
}

# Le chiavi di KEY_DIR: ogni file .pem è 600 e di OWNER, c'è una chiave per ogni ruolo atteso;
# STRICT=1: una chiave di un'app non attesa è un errore, altrimenti un avviso.
check_keys() {
    local dir=$1 owner=$2 expected=$3 strict=$4 key name slug role found actual
    [ -d "$dir" ] || return
    if ! [ -r "$dir" ] || ! [ -x "$dir" ]; then
        fail "$dir non è leggibile da $(id -un): eseguire come root"
        return
    fi
    for key in "$dir"/*.pem; do
        [ -e "$key" ] || continue
        actual=$(stat -c '%a %U' "$key")
        if [ "$actual" = "600 $owner" ]; then
            ok "$key: 600, $owner"
        else
            fail "$key è $actual, deve essere 600 $owner"
        fi
        name=$(basename "$key")
        slug=${name%%.*}
        found=0
        for role in $expected; do
            [ "$slug" = "$APP_PREFIX$role" ] && found=1
        done
        if [ "$found" = 0 ]; then
            if [ "$strict" = 1 ]; then
                fail "$key: chiave non attesa in $dir (attese: $expected)"
            else
                warn "$key: chiave di un'app non elencata ($expected)"
            fi
        fi
    done
    for role in $expected; do
        if compgen -G "$dir/$APP_PREFIX$role.*.private-key.pem" >/dev/null; then
            ok "chiave di $APP_PREFIX$role presente"
        else
            fail "manca la chiave di $APP_PREFIX$role in $dir"
        fi
    done
}

step 'Utenti'
for user in "$DEVLOOP_USER" "$SUPERVISOR_USER" "$AGENT_USER"; do
    if user_exists "$user"; then ok "l'utente $user esiste"; else fail "l'utente $user non esiste"; fi
done

step 'Directory private di devloop e del supervisore (700)'
check_dir "$DEVLOOP_KEY_DIR" 700 "$DEVLOOP_USER"
check_dir "$DEVLOOP_HOME/.claude" 700 "$DEVLOOP_USER"
check_dir "$DEVLOOP_HOME/.codex" 700 "$DEVLOOP_USER"
check_dir "$SUPERVISOR_KEY_DIR" 700 "$SUPERVISOR_USER"
check_dir "$SUPERVISOR_HOME/.claude" 700 "$SUPERVISOR_USER"

step 'Chiavi delle GitHub App (600)'
check_keys "$DEVLOOP_KEY_DIR" "$DEVLOOP_USER" "$DEVLOOP_KEYS" 0
check_keys "$SUPERVISOR_KEY_DIR" "$SUPERVISOR_USER" "$SUPERVISOR_KEYS" 1

step "ACL: solo attraversamento di $DEVLOOP_HOME per $SUPERVISOR_USER"
if ! command -v getfacl >/dev/null; then
    fail 'getfacl non trovato (pacchetto acl)'
elif [ ! -d "$DEVLOOP_HOME" ]; then
    fail "$DEVLOOP_HOME non esiste"
else
    entry=$(getfacl -p --omit-header "$DEVLOOP_HOME" 2>/dev/null | grep "^user:$SUPERVISOR_USER:")
    case $entry in
        "user:$SUPERVISOR_USER:--x") ok "ACL $entry su $DEVLOOP_HOME" ;;
        '') fail "manca l'ACL u:$SUPERVISOR_USER:--x su $DEVLOOP_HOME (setup-supervisor.sh)" ;;
        *) fail "ACL $entry su $DEVLOOP_HOME: deve essere --x, senza elenco né scrittura" ;;
    esac
    other=$(stat -c '%A' "$DEVLOOP_HOME")
    [ "${other:7:1}" = r ] && warn "$DEVLOOP_HOME è leggibile da tutti ($other)"
    for private in "$DEVLOOP_KEY_DIR" "$DEVLOOP_HOME/.claude" "$DEVLOOP_HOME/.codex"; do
        [ -d "$private" ] || continue
        if getfacl -p --omit-header "$private" 2>/dev/null | grep -q "^user:$SUPERVISOR_USER:"; then
            fail "$private ha un'ACL per $SUPERVISOR_USER: deve restare 700 senza eccezioni"
        fi
    done
fi
LOG="$STATE_PATH/run.log"
check_as "$SUPERVISOR_USER" "$SUPERVISOR_USER legge $LOG" test -r "$LOG"

# Il sudoers FILE: 440 di root, uguale a ops/host/TEMPLATE con questi parametri, accettato da
# visudo; SETUP è lo script che lo installa.
check_sudoers() {
    local file=$1 template=$2 setup=$3 actual
    if [ ! -e "$file" ]; then
        fail "$file non esiste ($setup), oppure non è visibile senza root"
        return
    fi
    actual=$(stat -c '%a %U' "$file")
    [ "$actual" = '440 root' ] && ok "$file: 440, root" || fail "$file è $actual, deve essere 440 root"
    if cmp -s "$file" <(render "$OPS_DIR/host/$template"); then
        ok "$file coincide con ops/host/$template"
    else
        warn "$file è diverso da ops/host/$template con questi parametri"
    fi
    if [ "$DRY_RUN" = 1 ]; then
        run visudo -cf "$file"
    elif visudo -cf "$file" >/dev/null; then
        ok 'visudo accetta il file'
    else
        fail 'visudo rifiuta il file'
    fi
}

step "sudoers del supervisore ($SUDOERS_FILE)"
check_sudoers "$SUDOERS_FILE" sudoers-supervisor setup-supervisor.sh

step "Utente degli agent $AGENT_USER (#415)"
STATE="$STATE_PATH"
for private in "$DEVLOOP_KEY_DIR" "$STATE" "$STATE/config.local.toml"; do
    check_as "$AGENT_USER" "$AGENT_USER non legge $private" /usr/bin/bash -c '! [ -r "$1" ]' _ "$private"
done
check_as "$AGENT_USER" "$AGENT_USER non attraversa $DEVLOOP_KEY_DIR" /usr/bin/bash -c '! [ -x "$1" ]' _ "$DEVLOOP_KEY_DIR"
# Le voci dello state_dir, tranne i worktree e la directory condivisa: né lette né attraversate,
# così nemmeno un file in una directory non elencabile è alla sua portata.
for entry in "$STATE"/* "$STATE"/.[!.]*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    case $(basename "$entry") in worktrees | agent-shared) continue ;; esac
    check_as "$AGENT_USER" "$AGENT_USER non legge né attraversa $entry" \
        /usr/bin/bash -c '! [ -r "$1" ] && ! { [ -d "$1" ] && [ -x "$1" ]; }' _ "$entry"
done
# Le directory che contengono ciò che devloop crea per gli agent: vi entra ma non le scrive,
# così non sostituisce né collega quello che devloop vi apre (#415).
for container in "$STATE/worktrees" "$STATE/agent-shared" "$STATE/agent-shared/schemas" \
    "$STATE/agent-shared/turns" "$STATE/agent-shared/tmp"; do
    check_as "$AGENT_USER" "$AGENT_USER entra in $container ma non lo scrive" \
        /usr/bin/bash -c '[ -r "$1" ] && [ -x "$1" ] && ! [ -w "$1" ]' _ "$container"
done
# I metadati git: chi li scrive fa eseguire a devloop un hook suo quando committa.
check_as "$AGENT_USER" "$AGENT_USER non scrive nulla in $DEVLOOP_CLONE/.git" \
    /usr/bin/bash -c '[ -z "$(find "$1" -writable -print -quit 2>/dev/null)" ]' _ "$DEVLOOP_CLONE/.git"
OVERLAY="$STATE/config.local.toml"
if [ -f "$OVERLAY" ] && python3 - "$OVERLAY" "$AGENT_USER" <<'EOF'
import sys, tomllib
data = tomllib.load(open(sys.argv[1], 'rb'))
sys.exit(0 if data.get('agent_user', {}).get('name') == sys.argv[2] else 1)
EOF
then
    ok "$OVERLAY imposta [agent_user] name = \"$AGENT_USER\""
else
    fail "$OVERLAY non imposta [agent_user] name = \"$AGENT_USER\": gli agent girerebbero come $DEVLOOP_USER"
fi

step "File delle CLI di $AGENT_USER: un turno non li cambia per i turni dopo (#438)"
actual=$(stat -c '%a %U' "$AGENT_HOME" 2>/dev/null)
if [ "$actual" = '1770 root' ]; then
    ok "$AGENT_HOME: 1770, root"
else
    fail "$AGENT_HOME è ${actual:-assente}, deve essere 1770 root (setup-agent-user.sh)"
fi
# Come il controllo all'avvio di devloop (devloop/agentuser.py), come l'utente degli agent:
# fixed PATH, PATH non è scrivibile né suo, e né PATH né una directory sopra si crea, si rinomina
# o si rimuove (in una directory scrivibile solo lo sticky bit protegge una voce, e solo se né la
# voce né la directory sono sue), e un link lungo il percorso porta, un passo alla volta, a un
# percorso fixed (un link illeggibile o una catena che non finisce è rifiutata); sealed PATH,
# nemmeno nulla dentro PATH è scrivibile o suo, tutto è leggibile e le directory attraversabili,
# e un link dentro porta a un percorso sealed; bin PATH, anche il file a cui porta PATH, e la sua
# directory sigillata. Solo gli strumenti del sistema: il PATH dell'utente degli agent comincia
# da ~/.local/bin, che controlla proprio sull'installazione da rifiutare.
CLI_PROBE='PATH=/usr/bin:/bin; me=$(id -u); bad=0; n=0
declare -A seen
mine() { [ "$(stat -c %u -- "$1" 2>/dev/null)" = "$me" ]; }
no() { printf "    %s %s\n" "$1" "$2" >&2; bad=1; }
to() {
    n=$((n + 1))
    if [ "$2" -ge 40 ] || [ "$n" -gt 1000 ] || ! T=$(readlink -- "$1"); then
        no "link non seguibile:" "$1"; return 1
    fi
    case $T in /*) ;; *) T=${1%/*}/$T ;; esac
}
fixed() {
    local q=$1 h=${2:-0} d
    [ -e "$q" ] && [ -w "$q" ] && no "scrivibile:" "$q"
    while [ "$q" != / ] && [ -n "$q" ]; do
        mine "$q" && no "suo:" "$q"
        d=${q%/*}; d=${d:-/}
        if [ -w "$d" ]; then
            if ! [ -e "$q" ] && ! [ -L "$q" ]; then no "creabile:" "$q"
            elif ! [ -k "$d" ] || mine "$q" || mine "$d"; then no "sostituibile:" "$q"; fi
        fi
        [ -L "$q" ] && to "$q" "$h" && fixed "$T" $((h + 1))
        q=$d
    done
}
sealed() {
    local c l w
    fixed "$1"
    [ -e "$1" ] && c=$(readlink -e -- "$1") || return 0
    [ -n "${seen[$c]}" ] && return 0
    seen[$c]=1
    w=$(find -H "$1" \( -writable -o -user "$me" \) -print -quit 2>/dev/null)
    [ -z "$w" ] || no "scrivibile o suo:" "$w"
    w=$(find -H "$1" ! -type l \( ! -readable -o -type d ! -executable \) -print -quit 2>/dev/null)
    [ -z "$w" ] || no "illeggibile:" "$w"
    while IFS= read -r -d "" l; do
        to "$l" 0 && sealed "$T"
    done < <(find -H "$1" -mindepth 1 -type l -print0 2>/dev/null)
}
case $1 in
    fixed) fixed "$2" ;;
    sealed) sealed "$2" ;;
    bin) fixed "$2"
         t=$(readlink -e -- "$2") && [ "$t" != "$2" ] && { fixed "$t"; sealed "${t%/*}"; } ;;
esac
[ "$bad" = 0 ]'
for dir in "${AGENT_CLI_DIRS[@]}"; do
    check_as "$AGENT_USER" "$AGENT_USER non crea nulla in $AGENT_HOME/$dir e non la sostituisce" \
        /usr/bin/bash -c "$CLI_PROBE" _ fixed "$AGENT_HOME/$dir"
done
for entry in "${AGENT_CLI_SEALED[@]}"; do
    check_as "$AGENT_USER" "$AGENT_USER non scrive né sostituisce $AGENT_HOME/$entry" \
        /usr/bin/bash -c "$CLI_PROBE" _ sealed "$AGENT_HOME/$entry"
done
for cli in "${AGENT_CLIS[@]}"; do
    check_as "$AGENT_USER" "$AGENT_USER non scrive né sostituisce $AGENT_HOME/.local/bin/$cli e il file a cui porta" \
        /usr/bin/bash -c "$CLI_PROBE" _ bin "$AGENT_HOME/.local/bin/$cli"
done

step "sudoers dell'utente degli agent ($AGENT_SUDOERS_FILE)"
check_sudoers "$AGENT_SUDOERS_FILE" sudoers-agent setup-agent-user.sh

step "Lock per repository: $REPO_LOCK_DIR (ADR 0006, R8)"
if [ ! -d "$REPO_LOCK_DIR" ]; then
    fail "$REPO_LOCK_DIR non esiste (setup-devloop.sh, systemd-tmpfiles)"
else
    actual=$(stat -c '%a %G' "$REPO_LOCK_DIR")
    if [ "$actual" = "3770 $REPO_LOCK_GROUP" ]; then
        ok "$REPO_LOCK_DIR: 3770, gruppo $REPO_LOCK_GROUP"
    else
        fail "$REPO_LOCK_DIR è $actual, deve essere 3770 del gruppo $REPO_LOCK_GROUP"
    fi
    owner=$(stat -c '%U' "$REPO_LOCK_DIR")
    [ "$owner" = root ] && ok "$REPO_LOCK_DIR è di root" \
        || fail "$REPO_LOCK_DIR è di $owner, deve essere di root"
fi
if id -nG "$DEVLOOP_USER" 2>/dev/null | tr ' ' '\n' | grep -qxF "$REPO_LOCK_GROUP"; then
    ok "$DEVLOOP_USER è nel gruppo $REPO_LOCK_GROUP"
else
    fail "$DEVLOOP_USER non è nel gruppo $REPO_LOCK_GROUP (setup-devloop.sh)"
fi
# Il gruppo è solo degli utenti devloop: un agent che vi entrasse potrebbe tenere il lock d'altri.
for user in "$AGENT_USER" "$SUPERVISOR_USER"; do
    [ "$user" = "$DEVLOOP_USER" ] && continue
    if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qxF "$REPO_LOCK_GROUP"; then
        fail "$user è nel gruppo $REPO_LOCK_GROUP: deve restarne fuori"
    fi
done
check_as "$AGENT_USER" "$AGENT_USER non entra in $REPO_LOCK_DIR" \
    /usr/bin/bash -c '! [ -x "$1" ]' _ "$REPO_LOCK_DIR"
if [ ! -f "$TMPFILES_FILE" ]; then
    fail "$TMPFILES_FILE non esiste (setup-devloop.sh): $REPO_LOCK_DIR non tornerebbe dopo un riavvio"
elif cmp -s "$TMPFILES_FILE" <(render "$OPS_DIR/host/tmpfiles-devloop-repos.conf"); then
    ok "$TMPFILES_FILE coincide con ops/host/tmpfiles-devloop-repos.conf"
else
    warn "$TMPFILES_FILE è diverso da ops/host/tmpfiles-devloop-repos.conf con questi parametri"
fi
if [ -f "$OVERLAY" ] && python3 - "$OVERLAY" "$REPO_LOCK_DIR" <<'EOF'
import sys, tomllib
data = tomllib.load(open(sys.argv[1], 'rb'))
sys.exit(0 if data.get('repo_lock_dir') == sys.argv[2] else 1)
EOF
then
    ok "$OVERLAY imposta repo_lock_dir = \"$REPO_LOCK_DIR\""
else
    fail "$OVERLAY non imposta repo_lock_dir = \"$REPO_LOCK_DIR\": un altro dispatcher sullo stesso repository non sarebbe escluso"
fi

step "Servizio $SERVICE_NAME"
check_unit() {
    local file=$1 template=$2
    if [ ! -f "$file" ]; then
        fail "$file non esiste (setup-devloop.sh)"
    elif cmp -s "$file" <(render "$OPS_DIR/host/$template"); then
        ok "$file coincide con ops/host/$template"
    else
        warn "$file è diverso da ops/host/$template con questi parametri"
    fi
}
check_unit "$UNIT_FILE" "$UNIT_TEMPLATE"
[ -z "$DROPIN_FILE" ] || check_unit "$DROPIN_FILE" devloop-project.conf
# Un repository, un host (ADR 0006, R8): nessun'altra unità di devloop di questo host lo gestisce.
for other in "$SYSTEMD_DIR"/devloop*.service "$SYSTEMD_DIR"/devloop@*.service.d/project.conf; do
    [ -f "$other" ] || continue
    [ "$other" = "$UNIT_FILE" ] || [ "$other" = "$DROPIN_FILE" ] && continue
    if grep -qixF "Description=devloop continuous loop ($REPO)" "$other"; then
        fail "$other gestisce anche $REPO: un repository, un servizio"
    fi
done
state=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)
case $state in
    active) ok "$SERVICE_NAME è attivo" ;;
    failed) fail "$SERVICE_NAME è fallito: journalctl -u $SERVICE_NAME e $LOG" ;;
    *) warn "$SERVICE_NAME non è attivo (${state:-sconosciuto}): l'avvio è dell'operatore" ;;
esac
enabled=$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null)
say "  $SERVICE_NAME all'avvio della macchina: ${enabled:-sconosciuto}"
killmode=$(systemctl show -p KillMode --value "$SERVICE_NAME" 2>/dev/null)
if [ -n "$killmode" ] && [ "$killmode" != mixed ]; then
    fail "KillMode=$killmode: deve essere mixed"
fi

step 'CLI degli agent'
for cli in claude codex agy; do
    exe="$AGENT_HOME/.local/bin/$cli"
    if [ -x "$exe" ]; then
        check_as "$AGENT_USER" "$cli --version come $AGENT_USER" "$exe" --version
    else
        fail "$exe non trovato (setup-agent-user.sh)"
    fi
done
exe="$SUPERVISOR_HOME/.local/bin/claude"
if [ -x "$exe" ]; then
    check_as "$SUPERVISOR_USER" "claude --version come $SUPERVISOR_USER" "$exe" --version
else
    fail "$exe non trovato"
fi

if [ -n "$SOURCE_REPO" ]; then
    step "Avviatore ($LAUNCHER)"
    if [ ! -f "$LAUNCHER" ]; then
        fail "$LAUNCHER non esiste (setup-devloop.sh, dal tag $SOURCE_REF)"
    else
        actual=$(stat -c '%a %U' "$LAUNCHER")
        [ "$actual" = '755 root' ] && ok "$LAUNCHER: 755, root" \
            || fail "$LAUNCHER è $actual, deve essere 755 root"
    fi
    [ -d "$SOURCE_CLONE/.git" ] && ok "$SOURCE_CLONE è un clone della sorgente" \
        || fail "$SOURCE_CLONE non è un clone (setup-devloop.sh)"
fi

step 'GitHub App (devloop apps)'
if [ "$CHECK_APPS" = 1 ]; then
    if [ -e "$STATE_PATH/current" ]; then
        # shellcheck disable=SC2086  # LAUNCHER_ARGS: nessuna parola o "--state-dir <percorso>"
        check_as "$DEVLOOP_USER" 'devloop apps' \
            env -C "$DEVLOOP_CLONE" /usr/bin/python3 "$LAUNCHER" $LAUNCHER_ARGS apps
    else
        fail "nessuna copia fissata in $STATE_PATH/current: fare il primo pin"
    fi
else
    say '  saltato (--no-apps)'
fi

step 'Esito'
say "$FAILURES errori, $WARNINGS avvisi"
[ "$FAILURES" = 0 ]
