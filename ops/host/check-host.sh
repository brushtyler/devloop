#!/usr/bin/env bash
# Verifica l'installazione di devloop e del supervisore: utenti, directory private (700), chiavi
# delle app (600, del proprietario giusto, solo quelle attese), ACL sulla home di devloop,
# sudoers, servizio, CLI degli agent e `devloop apps`. Non cambia nulla.
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
for user in "$DEVLOOP_USER" "$SUPERVISOR_USER"; do
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

step "sudoers del supervisore ($SUDOERS_FILE)"
if [ ! -e "$SUDOERS_FILE" ]; then
    fail "$SUDOERS_FILE non esiste (setup-supervisor.sh), oppure non è visibile senza root"
else
    actual=$(stat -c '%a %U' "$SUDOERS_FILE")
    [ "$actual" = '440 root' ] && ok "$SUDOERS_FILE: 440, root" || fail "$SUDOERS_FILE è $actual, deve essere 440 root"
    if cmp -s "$SUDOERS_FILE" <(render "$OPS_DIR/host/sudoers-supervisor"); then
        ok "$SUDOERS_FILE coincide con ops/host/sudoers-supervisor"
    else
        warn "$SUDOERS_FILE è diverso da ops/host/sudoers-supervisor con questi parametri"
    fi
    if [ "$DRY_RUN" = 1 ]; then
        run visudo -cf "$SUDOERS_FILE"
    elif visudo -cf "$SUDOERS_FILE" >/dev/null; then
        ok 'visudo accetta il file'
    else
        fail 'visudo rifiuta il file'
    fi
fi

step "Servizio $SERVICE_NAME"
UNIT="$SYSTEMD_DIR/$SERVICE_NAME.service"
if [ ! -f "$UNIT" ]; then
    fail "$UNIT non esiste (setup-devloop.sh)"
elif cmp -s "$UNIT" <(render "$OPS_DIR/host/devloop.service"); then
    ok "$UNIT coincide con ops/host/devloop.service"
else
    warn "$UNIT è diverso da ops/host/devloop.service con questi parametri"
fi
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
    exe="$DEVLOOP_HOME/.local/bin/$cli"
    if [ -x "$exe" ]; then
        check_as "$DEVLOOP_USER" "$cli --version come $DEVLOOP_USER" "$exe" --version
    else
        fail "$exe non trovato"
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
