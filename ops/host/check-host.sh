#!/usr/bin/env bash
# Verifica l'installazione di devloop e del supervisore: utenti, directory private (700), chiavi
# delle app (600, del proprietario giusto, solo quelle attese), ACL sulla home di devloop,
# sudoers, l'utente degli agent (non legge key_dir né state_dir, non scrive .git né le directory
# che contengono worktree e turni, è nell'overlay, esegue git nei worktree di devloop, non cambia
# istruzioni, configurazione e binari delle sue CLI; l'ambiente dei turni gli arriva con sudo -E,
# che sudo-rs ignora), il lock per repository (directory comune, gruppo, overlay; ADR 0006, R8),
# servizio, CLI degli agent (--version, e un `git --version` con il loro strumento di shell,
# #494) e `devloop apps`, e il coordinatore, se c'è (key_dir e chiave della sua app, ACL e lettura dell'overlay, #549;
# sudoers e copia di root di questo script, #530). Con più progetti sullo stesso host si esegue una volta per progetto, con i suoi
# parametri (--params).
# Non cambia nulla, se non le sessioni e le cache che le CLI degli agent scrivono da sé. Delle
# prove delle CLI riporta solo l'esito e il codice di uscita, mai il loro output né il testo del
# modello (#530).
# Esce con 0 se non trova errori, con 1 altrimenti; gli avvisi non contano come errori.
# Va eseguito come root (sudo): alcune verifiche leggono file degli altri utenti.
#
# --dry-run: fa le verifiche locali in sola lettura e stampa, senza eseguirle, quelle che passano
# da sudo o dalla rete (--version e strumento di shell delle CLI, lettura del log come
# supervisore, visudo, sudo -E verso l'utente degli agent, devloop apps).
# --read-only: la verifica del coordinatore (#530): nessuna CLI avviata (né --version né lo
# strumento di shell: nessun modello, nessuna quota, nessuna scrittura nelle sessioni o nelle
# cache), niente `devloop apps`, nessuna scrittura; solo righe di stato.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e  # una verifica fallita si conta, non interrompe le altre

CHECK_APPS=1
READ_ONLY=0

usage() {
    say "Uso: sudo bash $0 [--params FILE] [--dry-run] [--no-apps] [--read-only]"
    say
    say '  --no-apps       non esegue `devloop apps` (che interroga GitHub)'
    say '  --read-only     nessuna CLI avviata, niente `devloop apps`, nessuna scrittura (il'
    say '                  coordinatore, #530)'
    say "$OPS_COMMON_HELP"
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --no-apps) CHECK_APPS=0; shift ;;
        --read-only) READ_ONLY=1; CHECK_APPS=0; shift ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

ops_load_params
# Come root, anche dal sudoers del coordinatore: nessun file della directory di chi lo lancia
# (per esempio un modulo che python importerebbe) entra nelle verifiche.
PARAMS_FILE=$(cd "$(dirname "$PARAMS_FILE")" && pwd)/$(basename "$PARAMS_FILE")
cd / || exit 1
ops_check_platform readonly
[ "$(id -u)" = 0 ] || warn 'non eseguito come root: le verifiche sui file degli altri utenti possono fallire'
[ "$READ_ONLY" = 0 ] || say "Sola lettura (--read-only): nessuna CLI avviata, niente devloop apps."

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

# check_traverse USER SETUP: USER attraversa soltanto la home di devloop (ACL --x, che mette lo
# script SETUP) e non ha ACL sulle directory private di devloop, che restano 700.
check_traverse() {
    local user=$1 setup=$2 entry private
    entry=$(getfacl -p --omit-header "$DEVLOOP_HOME" 2>/dev/null | grep "^user:$user:")
    case $entry in
        "user:$user:--x") ok "ACL $entry su $DEVLOOP_HOME" ;;
        '') fail "manca l'ACL u:$user:--x su $DEVLOOP_HOME ($setup)" ;;
        *) fail "ACL $entry su $DEVLOOP_HOME: deve essere --x, senza elenco né scrittura" ;;
    esac
    for private in "$DEVLOOP_KEY_DIR" "$DEVLOOP_HOME/.claude" "$DEVLOOP_HOME/.codex"; do
        [ -d "$private" ] || continue
        if getfacl -p --omit-header "$private" 2>/dev/null | grep -q "^user:$user:"; then
            fail "$private ha un'ACL per $user: deve restare 700 senza eccezioni"
        fi
    done
}

step "ACL: solo attraversamento di $DEVLOOP_HOME per $SUPERVISOR_USER"
if ! command -v getfacl >/dev/null; then
    fail 'getfacl non trovato (pacchetto acl)'
elif [ ! -d "$DEVLOOP_HOME" ]; then
    fail "$DEVLOOP_HOME non esiste"
else
    check_traverse "$SUPERVISOR_USER" setup-supervisor.sh
    other=$(stat -c '%A' "$DEVLOOP_HOME")
    [ "${other:7:1}" = r ] && warn "$DEVLOOP_HOME è leggibile da tutti ($other)"
fi
LOG="$STATE_PATH/run.log"
# Letture e scritture si provano con [ -r ] / [ -w ] di bash, che usa access(2) e rispetta le ACL,
# mai con /usr/bin/test: quello di uutils (il default di Ubuntu 26.04) guarda solo i bit del modo
# e darebbe un falso errore su una lettura concessa con un'ACL (#554).
check_as "$SUPERVISOR_USER" "$SUPERVISOR_USER legge $LOG" /usr/bin/bash -c '[ -r "$1" ]' _ "$LOG"

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

# PATH e ogni directory sopra: di root e non scrivibili dal gruppo né dagli altri, così nessun
# altro utente cambia ciò che il coordinatore esegue come root.
check_root_path() {
    local path=$1 q=$1 owner mode bad=0
    while :; do
        if ! [ -e "$q" ] || [ -L "$q" ]; then
            fail "$q non esiste o è un link (setup-coordinator.sh)"
            return
        fi
        read -r owner mode < <(stat -c '%U %a' "$q")
        if [ "$owner" != root ] || (( 8#$mode & 8#022 )); then
            fail "$q è $mode $owner: deve essere di root e non scrivibile dal gruppo né dagli altri"
            bad=1
        fi
        [ "$q" = / ] && break
        q=$(dirname "$q")
    done
    [ "$bad" = 1 ] || ok "$path e le directory sopra sono di root, non scrivibili da altri"
}

# Il coordinatore (#530), facoltativo: la key_dir della sua app e la lettura dell'overlay, con
# il solo attraversamento della home di devloop (#549), il suo sudoers in sola
# lettura e la copia di root di check-host.sh, dei file che legge e dei parametri, da cui esegue
# `check-host.sh --read-only`.
step "Coordinatore $COORDINATOR_USER (facoltativo, #530)"
if ! user_exists "$COORDINATOR_USER"; then
    say "  l'utente $COORDINATOR_USER non esiste: coordinatore non installato (setup-coordinator.sh)"
else
    ok "l'utente $COORDINATOR_USER esiste"
    # La key_dir della sua app (#549): 700, solo la chiave del coordinatore, 600.
    check_dir "$COORDINATOR_KEY_DIR" 700 "$COORDINATOR_USER"
    check_keys "$COORDINATOR_KEY_DIR" "$COORDINATOR_USER" "$COORDINATOR_KEYS" 1
    # L'overlay di devloop, da cui scripts/coordinator-token.py legge repo e [apps.coordinator]
    # (#549): solo l'attraversamento della home di devloop, l'ACL di sola lettura sull'overlay
    # (#554), e che il coordinatore lo legga.
    OVERLAY_FILE="$STATE_PATH/config.local.toml"
    if command -v getfacl >/dev/null && [ -d "$DEVLOOP_HOME" ]; then
        check_traverse "$COORDINATOR_USER" setup-coordinator.sh
    fi
    if command -v getfacl >/dev/null && [ -f "$OVERLAY_FILE" ]; then
        # getfacl può aggiungere alla riga un commento #effective.
        entry=$(getfacl -p --omit-header "$OVERLAY_FILE" 2>/dev/null \
            | grep "^user:$COORDINATOR_USER:" | head -n 1)
        entry=${entry%%[[:space:]]*}
        case $entry in
            "user:$COORDINATOR_USER:r--") ok "ACL $entry su $OVERLAY_FILE" ;;
            '') fail "manca l'ACL u:$COORDINATOR_USER:r-- su $OVERLAY_FILE (setup-coordinator.sh)" ;;
            *) fail "ACL $entry su $OVERLAY_FILE: deve essere r--, senza scrittura" ;;
        esac
    fi
    check_as "$COORDINATOR_USER" "$COORDINATOR_USER legge $OVERLAY_FILE" \
        /usr/bin/bash -c '[ -r "$1" ]' _ "$OVERLAY_FILE"
    check_sudoers "$COORDINATOR_SUDOERS_FILE" sudoers-coordinator setup-coordinator.sh
    check_root_path "$COORDINATOR_OPS_DIR/host"
    for name in $OPS_COORDINATOR_FILES params.env; do
        copy=$COORDINATOR_OPS_DIR/$name
        [ "$name" = params.env ] && source_file=$PARAMS_FILE || source_file=$OPS_DIR/$name
        if ! [ -f "$copy" ] || [ -L "$copy" ]; then
            fail "$copy non esiste (setup-coordinator.sh)"
            continue
        fi
        actual=$(stat -c '%a %U' "$copy")
        [ "$actual" = '644 root' ] || fail "$copy è $actual, deve essere 644 root"
        # Da questa stessa copia (il coordinatore) non c'è altro con cui confrontarla.
        [ "$copy" -ef "$source_file" ] && continue
        if cmp -s "$copy" "$source_file"; then
            ok "$copy coincide con $source_file"
        else
            warn "$copy è diverso da $source_file: rieseguire setup-coordinator.sh"
        fi
    done
fi

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
# git come utente degli agent in un worktree di $DEVLOOP_USER (#500): come in un turno, devloop
# passa con `sudo -n -E` safe.directory per quel worktree soltanto (GIT_CONFIG_*), e git non lo
# rifiuta per "dubious ownership". Si prova sul primo worktree che c'è, senza scrivervi nulla.
GIT_PROBE='export PATH=/usr/bin:/bin GIT_OPTIONAL_LOCKS=0; git -C "$1" status --porcelain >/dev/null'
GIT_WORKTREE=
for entry in "$STATE"/worktrees/*/; do
    [ -e "${entry%/}/.git" ] && { GIT_WORKTREE=${entry%/}; break; }
done
if [ -n "$GIT_WORKTREE" ]; then
    check_as "$DEVLOOP_USER" "$AGENT_USER esegue git in $GIT_WORKTREE con il suo safe.directory" \
        /usr/bin/env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory \
        "GIT_CONFIG_VALUE_0=$GIT_WORKTREE" \
        sudo -n -E -u "$AGENT_USER" -- /usr/bin/bash -c "$GIT_PROBE" _ "$GIT_WORKTREE"
else
    warn "nessun worktree in $STATE/worktrees: git come $AGENT_USER non verificato (lo verifica devloop all'avvio)"
fi
OVERLAY="$STATE/config.local.toml"
if [ -f "$OVERLAY" ] && python3 -I -B - "$OVERLAY" "$AGENT_USER" <<'EOF'
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
    [ -e "$1" ] && [ -w "$1" ] && no "scrivibile:" "$1"
    above "$1" "${2:-0}"
}
above() {
    local q=$1 h=$2 d
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
    cli) if [ -e "$2" ] || [ -L "$2" ]; then
             { [ -d "$2" ] && ! [ -L "$2" ] && [ -k "$2" ]; } || no "senza sticky bit:" "$2"
             { [ -w "$2" ] && [ -x "$2" ]; } || no "non scrivibile:" "$2"
         fi
         above "$2" 0 ;;
    sealed) sealed "$2" ;;
    bin) fixed "$2"
         t=$(readlink -e -- "$2") && [ "$t" != "$2" ] && { fixed "$t"; sealed "${t%/*}"; } ;;
esac
[ "$bad" = 0 ]'
for dir in "${AGENT_CLI_DIRS[@]}"; do
    check_dir "$AGENT_HOME/$dir" 1770 root
    check_as "$AGENT_USER" "$AGENT_USER scrive in $AGENT_HOME/$dir, con lo sticky bit, e non la sostituisce (#517)" \
        /usr/bin/bash -c "$CLI_PROBE" _ cli "$AGENT_HOME/$dir"
done
for entry in "${AGENT_CLI_SEALED[@]}"; do
    check_as "$AGENT_USER" "$AGENT_USER non scrive né sostituisce $AGENT_HOME/$entry" \
        /usr/bin/bash -c "$CLI_PROBE" _ sealed "$AGENT_HOME/$entry"
done
for cli in "${AGENT_CLIS[@]}"; do
    check_as "$AGENT_USER" "$AGENT_USER non scrive né sostituisce $AGENT_HOME/.local/bin/$cli e il file a cui porta" \
        /usr/bin/bash -c "$CLI_PROBE" _ bin "$AGENT_HOME/.local/bin/$cli"
done
# Le directory delle CLI contengono solo le voci della lista (#517), come controlla devloop prima
# di ogni turno: una voce sconosciuta, o di un altro tipo, è un errore. Che le voci sigillate e
# le directory delle CLI siano di root lo controllano le verifiche qui sopra.
for dir in "${AGENT_CLI_DIRS[@]}"; do
    [ -d "$AGENT_HOME/$dir" ] && ! [ -L "$AGENT_HOME/$dir" ] || continue
    bad=0
    while IFS= read -r -d '' record && IFS= read -r -d '' target; do
        type=${record%%/*} name=${record#*/}
        if agent_cli_entry_known "$dir" "$name" "$type" "$target" "$AGENT_HOME"; then
            known=0
        else
            known=$?
        fi
        case $known in
            1) fail "$AGENT_HOME/$dir/$name non è nella lista AGENT_CLI_ALLOWED: devloop rifiuta i turni finché c'è (rimuoverla come $AGENT_USER a loop fermo, o aggiungerla con una PR)"; bad=1 ;;
            2) fail "$AGENT_HOME/$dir/$name è di tipo $type, che la lista AGENT_CLI_ALLOWED non ammette per quel nome"; bad=1 ;;
            3) fail "$AGENT_HOME/$dir/$name è un link a $target, fuori dalla directory che AGENT_CLI_LINKS ammette per quel nome: devloop rifiuta i turni finché c'è (rimuoverlo come $AGENT_USER a loop fermo)"; bad=1 ;;
            4) fail "$AGENT_HOME/$dir/$name è un link a $target, che non è un file regolare (un altro link, che potrebbe uscire dalla directory, o nulla): devloop rifiuta i turni finché c'è (rimuoverlo come $AGENT_USER a loop fermo)"; bad=1 ;;
        esac
    done < <(find "$AGENT_HOME/$dir" -mindepth 1 -maxdepth 1 -printf '%y/%f\0%l\0' 2>/dev/null)
    [ "$bad" = 1 ] || ok "$AGENT_HOME/$dir contiene solo voci della lista"
done
# Le memorie di codex spente in modo esplicito (#517), come controlla devloop all'avvio: i turni
# di codex girano in parallelo, e nessuno deve rileggere ciò che un altro lascia in
# memories_1.sqlite.
if codex_why=$(codex_memories "$AGENT_HOME/.codex/config.toml"); then
    ok "$AGENT_HOME/.codex/config.toml imposta [features] memories = false"
else
    fail "${codex_why:-$AGENT_HOME/.codex/config.toml non si legge}: devloop non parte (setup-agent-user.sh aggiunge [features] memories = false)"
fi
# Le voci che le CLI creano al primo uso (#494): restano dell'utente degli agent, che le scrive.
for entry in "${AGENT_CLI_WRITABLE[@]}"; do
    check_as "$AGENT_USER" "$AGENT_USER scrive in $AGENT_HOME/$entry (setup-agent-user.sh)" \
        /usr/bin/bash -c '[ -d "$1" ] && ! [ -L "$1" ] && [ -w "$1" ] && [ -x "$1" ]' _ \
        "$AGENT_HOME/$entry"
done

step "sudoers dell'utente degli agent ($AGENT_SUDOERS_FILE)"
check_sudoers "$AGENT_SUDOERS_FILE" sudoers-agent setup-agent-user.sh

# L'ambiente isolato che devloop prepara arriva all'utente degli agent (#496), come al controllo
# all'avvio di devloop (devloop/agentuser.py): una variabile di prova passata come $DEVLOOP_USER
# con lo stesso `sudo -n -E` dei turni e letta da /usr/bin/bash. sudo-rs, il default di Ubuntu,
# ignora -E anche con SETENV: serve sudo 1.9.
step "Ambiente dei turni: sudo -E come $DEVLOOP_USER verso $AGENT_USER (#496)"
SUDO_FIX='update-alternatives --set sudo /usr/bin/sudo.ws'
probe_value="devloop-env-probe-$$-$RANDOM"
env_cmd=(env DEVLOOP_ENV_PROBE="$probe_value" sudo -n -E -u "$AGENT_USER" --
         /usr/bin/bash -c 'printf %s "$DEVLOOP_ENV_PROBE"')
[ "$(id -un)" = "$DEVLOOP_USER" ] || env_cmd=(sudo -u "$DEVLOOP_USER" -H -- "${env_cmd[@]}")
if [ "$DRY_RUN" = 1 ]; then
    run sudo --version
    run "${env_cmd[@]}"
else
    sudo_version=$(sudo --version 2>/dev/null | head -n 1)
    case $sudo_version in
        sudo-rs*) fail "sudo è $sudo_version: ignora -E e gli agent girerebbero senza l'ambiente che devloop prepara; serve sudo 1.9: $SUDO_FIX" ;;
        '') warn 'sudo --version non risponde' ;;
        *) ok "sudo: $sudo_version" ;;
    esac
    # Con stderr: un avviso di sudo, come quello di sudo-rs su -E, è già un esito diverso.
    env_out=$("${env_cmd[@]}" 2>&1)
    env_status=$?
    env_detail=$(printf '%s' "${env_out:0:300}" | tr '\n' ' ')
    if [ "$env_status" = 0 ] && [ "$env_out" = "$probe_value" ]; then
        ok "l'ambiente arriva a $AGENT_USER con sudo -E"
    else
        case $env_detail in
            *sudo-rs* | *"'-E' is ignored"* | *'not supported'*)
                reason="sudo-rs ignora -E: $SUDO_FIX" ;;
            *'preserve the environment'*)
                reason="manca il tag SETENV nel sudoers ($AGENT_SUDOERS_FILE, setup-agent-user.sh)" ;;
            *) reason="sudo-rs ignora -E ($SUDO_FIX); con sudo 1.9 serve il tag SETENV nel sudoers" ;;
        esac
        fail "l'ambiente non arriva a $AGENT_USER con sudo -E (uscita $env_status): $reason${env_detail:+ [sudo: $env_detail]}"
    fi
fi

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
if [ -f "$OVERLAY" ] && python3 -I -B - "$OVERLAY" "$REPO_LOCK_DIR" <<'EOF'
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

# shell_tool CLI ARG...: la CLI di AGENT_USER, con ARG, esegue come AGENT_USER `git --version`
# con il suo strumento di shell (#494): un --version non lo avvia, e una CLI che al primo comando
# vuole creare una voce in una directory sigillata si ferma con `permission denied`. Passa dal
# modello: ci vuole la rete e il login della CLI. Gira in /tmp, con un limite di 300 secondi.
SHELL_PROMPT='Run the shell command `git --version` with your shell tool, then reply with its output only.'
shell_tool() {
    local cli=$1 out status
    shift
    local -a cmd=(timeout 300 "$AGENT_HOME/.local/bin/$cli" "$@")
    [ "$(id -un)" = "$AGENT_USER" ] || cmd=(sudo -u "$AGENT_USER" -H -- "${cmd[@]}")
    if [ "$DRY_RUN" = 1 ]; then
        run "${cmd[@]}"
        return 0
    fi
    out=$(cd /tmp && "${cmd[@]}" </dev/null 2>&1)
    status=$?
    # Riuscita: uscita 0, l'output di git --version (la versione, non le sole parole, che una
    # risposta che descrive il fallimento può contenere) e nessun diniego, anche con uscita 0.
    # Si riporta solo l'esito, con il codice di uscita: mai l'output, che è testo del modello
    # (#530); per vederlo si ripete la prova a mano (ops/host/migrate.md).
    if [ "$status" = 0 ] && grep -Eq 'git version [0-9]+\.[0-9]+' <<<"$out" \
        && ! grep -Eiq 'permission denied|operation not permitted' <<<"$out"; then
        ok "$cli come $AGENT_USER esegue git --version con il suo strumento di shell (uscita 0)"
        return
    fi
    fail "$cli come $AGENT_USER non esegue git --version con il suo strumento di shell (uscita $status; l'output non si stampa: ripetere la prova a mano, ops/host/migrate.md, una CLI che si ferma con permission denied)"
}

# cli_version USER EXE: EXE --version come USER. Come shell_tool, solo l'esito e il codice di
# uscita, senza l'output della CLI.
cli_version() {
    local user=$1 exe=$2 status
    local -a cmd=("$exe" --version)
    [ "$(id -un)" = "$user" ] || cmd=(sudo -u "$user" -H -- "${cmd[@]}")
    if [ "$DRY_RUN" = 1 ]; then
        run "${cmd[@]}"
        return 0
    fi
    (cd /tmp && "${cmd[@]}" </dev/null >/dev/null 2>&1)
    status=$?
    if [ "$status" = 0 ]; then
        ok "$(basename "$exe") --version come $user (uscita 0)"
    else
        fail "$(basename "$exe") --version come $user non riuscito (uscita $status)"
    fi
}

# Con --read-only nessuna CLI si avvia: si verifica solo che ci sia.
step 'CLI degli agent'
for cli in claude codex agy; do
    exe="$AGENT_HOME/.local/bin/$cli"
    if ! [ -x "$exe" ]; then
        fail "$exe non trovato (setup-agent-user.sh)"
    elif [ "$READ_ONLY" = 1 ]; then
        ok "$exe presente"
    else
        cli_version "$AGENT_USER" "$exe"
    fi
done
if [ "$READ_ONLY" = 1 ]; then
    say '  prove delle CLI saltate (--read-only): nessuna CLI avviata'
else
    for cli in claude codex agy; do
        [ -x "$AGENT_HOME/.local/bin/$cli" ] || continue
        case $cli in
            claude) shell_tool claude -p "$SHELL_PROMPT" --allowedTools 'Bash(git --version)' ;;
            codex) shell_tool codex exec --skip-git-repo-check -s read-only "$SHELL_PROMPT" ;;
            agy) shell_tool agy -p "$SHELL_PROMPT" --output-format json \
                     --settings '{"permissions": {"allow": ["command(git --version)"]}}' ;;
        esac
    done
fi
exe="$SUPERVISOR_HOME/.local/bin/claude"
if ! [ -x "$exe" ]; then
    fail "$exe non trovato"
elif [ "$READ_ONLY" = 1 ]; then
    ok "$exe presente"
else
    cli_version "$SUPERVISOR_USER" "$exe"
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
elif [ "$READ_ONLY" = 1 ]; then
    say '  saltato (--read-only)'
else
    say '  saltato (--no-apps)'
fi

step 'Esito'
say "$FAILURES errori, $WARNINGS avvisi"
[ "$FAILURES" = 0 ]
