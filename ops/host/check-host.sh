#!/usr/bin/env bash
# Verifica l'installazione di devloop e del supervisore: utenti, directory private (700), chiavi
# delle app (600, del proprietario giusto, solo quelle attese, nessuna rimasta nella key_dir di
# default quando il parametro ne indica un'altra, #582), ACL sulla home di devloop,
# sudoers, la lettura dell'overlay e del file di progetto per il supervisore (ACL r--, #543), la
# casella fra supervisore e coordinatore (gruppo, directory 2770 di root, #543), il flusso degli
# eventi (scrivibile solo da devloop, leggibile dai ruoli con le loro ACL, e nessuna regola dei
# sudoers dei ruoli per devloop-pinned watch, #683), l'utente degli agent (non legge key_dir né state_dir, non scrive .git né le directory
# che contengono worktree e turni, è nell'overlay, esegue git nei worktree di devloop, non cambia
# istruzioni, configurazione e binari delle sue CLI, agy si fida al più della directory dei
# worktree, #572; l'ambiente dei turni gli arriva con sudo -E,
# che sudo-rs ignora), il lock per repository (directory comune, gruppo, overlay; ADR 0006, R8),
# servizio, comando devloop dell'host e file del progetto (#588), CLI degli agent (--version, e un `git --version` con il loro strumento di shell,
# #494), che i clone dei ruoli si aggiornino (per un repository privato con la loro app, #717) e
# `devloop apps`, e il coordinatore, se l'overlay del progetto lo dichiara (#571; key_dir e chiave della sua app, ACL e lettura dell'overlay, #549;
# sudoers e copia di root di questo script, #530; il suo clone, #643), e Claude Code dei ruoli
# senza aggiornamenti automatici (#643), la sentinella dei ruoli a riposo (unità, utente, ACL) e la
# directory rest/ della casella (3770, #684). Con più progetti sullo stesso host si esegue una volta
# per progetto, con i suoi parametri (--params). Per un progetto con SOURCE_REPO, un avviso se il
# checkout da cui gira non è al tag SOURCE_REF: le copie di root di ops/ si confrontano con
# quelle del checkout (#687).
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
# --verbose: i percorsi reali dell'host, per la diagnosi locale. Senza, l'output (anche gli errori
# dei comandi) nomina i parametri ($STATE_DIR, $DEVLOOP_KEY_DIR, $AGENT_HOME, ...), i ruoli delle
# chiavi, il servizio ($SERVICE_NAME) e gli utenti non di default (<AGENT_USER>) al posto dei
# valori, e si può citare come evidenza in un testo pubblico (#583). Un errore nel caricamento dei
# parametri, prima che la mappa esista, resta generico: il dettaglio con --verbose.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e  # una verifica fallita si conta, non interrompe le altre

CHECK_APPS=1
READ_ONLY=0
VERBOSE=0

usage() {
    say "Uso: sudo bash $0 [--params FILE] [--dry-run] [--no-apps] [--read-only] [--verbose]"
    say
    say '  --no-apps       non esegue `devloop apps` né la verifica dei clone dei ruoli (che'
    say '                  interrogano GitHub)'
    say '  --read-only     nessuna CLI avviata, niente `devloop apps`, nessuna scrittura (il'
    say '                  coordinatore, #530)'
    say '  --verbose       i percorsi reali dell'"'"'host, per la diagnosi locale: senza, l'"'"'output'
    say '                  nomina i parametri ($STATE_DIR, ...) e si può citare (#583)'
    say "$OPS_COMMON_HELP"
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --no-apps) CHECK_APPS=0; shift ;;
        --read-only) READ_ONLY=1; CHECK_APPS=0; shift ;;
        --verbose) VERBOSE=1; shift ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

# Senza --verbose, nemmeno il caricamento dei parametri mostra i valori dell'host (#583): i suoi
# errori nominano il file dei parametri e i valori rifiutati, prima che la mappa del filtro
# esista. Il suo output si raccoglie; se i parametri non si caricano, resta un messaggio
# generico e il dettaglio si vede con --verbose; altrimenti passa dal filtro, qui sotto.
load_failed() {
    local status=$?
    rm -f "$LOAD_OUT"
    [ "$status" = 0 ] || printf '%s: %s %s\n' "$(basename "$0")" \
        'i parametri non si caricano o non sono validi: il dettaglio,' \
        "con i valori dell'host, con --verbose (#583)" >&9
    exit "$status"
}
if [ "$VERBOSE" = 0 ]; then
    LOAD_OUT=$(mktemp) || die 'non riesce a creare un file temporaneo'
    exec 9>&2
    trap load_failed EXIT
    ops_load_params >"$LOAD_OUT" 2>&1
    trap - EXIT
    exec 9>&-
else
    ops_load_params
fi
# Senza --verbose, l'output e gli errori passano dal filtro dei parametri (#583): nessun percorso
# dell'host. Il filtro finisce di scrivere prima che lo script esca.
if [ "$VERBOSE" = 0 ]; then
    REDACT_MAP=$(ops_redactions 2>/dev/null) || {
        rm -f "$LOAD_OUT"
        die 'la mappa delle sostituzioni non si costruisce: il dettaglio con --verbose (#583)'
    }
    exec > >(ops_redact_filter "$REDACT_MAP")
    REDACT_OUT=$!
    exec 2> >(ops_redact_filter "$REDACT_MAP" >&2)
    REDACT_ERR=$!
    trap 'status=$?; exec >&- 2>&-; wait "$REDACT_OUT" "$REDACT_ERR"; exit "$status"' EXIT
    # Quel che il caricamento ha scritto (per esempio un avviso di bash sul file dei parametri),
    # ora dal filtro, con $PARAMS_FILE al posto del file dei parametri, che la mappa non ha.
    while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "${line//"$PARAMS_FILE"/\$PARAMS_FILE}"
    done <"$LOAD_OUT" >&2
    rm -f "$LOAD_OUT"
fi
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
# STRICT=1: una chiave di un'app non attesa è un errore, altrimenti un avviso. I ruoli di OPTIONAL
# sono riconosciuti, ma la loro chiave non è richiesta (l'implementer senza [apps.implementer], #569).
check_keys() {
    local dir=$1 owner=$2 expected=$3 strict=$4 optional=${5:-} key name slug role found actual
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
        for role in $expected $optional; do
            [ "$slug" = "$APP_PREFIX$role" ] && found=1
        done
        if [ "$found" = 0 ]; then
            if [ "$strict" = 1 ]; then
                fail "$key: chiave non attesa in $dir (attese: $expected${optional:+ $optional})"
            else
                warn "$key: chiave di un'app non elencata ($expected${optional:+ $optional})"
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
    for role in $optional; do
        if compgen -G "$dir/$APP_PREFIX$role.*.private-key.pem" >/dev/null; then
            ok "chiave di $APP_PREFIX$role presente"
        else
            ok "nessuna chiave di $APP_PREFIX$role: l'overlay non dichiara [apps.$role]"
        fi
    done
}

# Le key_dir di default del supervisore o del coordinatore, con la home HOME: ~/.ghapps e, con
# PROJECT, ~/.ghapps-<progetto>. Con PROJECT, ~/.ghapps è anche la key_dir di default di
# un'installazione senza PROJECT dello stesso host (un'unità devloop*.service che non è quella di
# questo progetto): se c'è, le chiavi che vi stanno possono essere sue (un insieme di app
# condiviso, ADR 0006, D3), e non si guarda.
default_key_dirs() {
    local home=$1 other
    if [ -z "$PROJECT" ]; then
        printf '%s\n' "$home/.ghapps"
        return
    fi
    for other in "$SYSTEMD_DIR"/devloop*.service; do
        # Le unità del watchdog dei ruoli (#602), dell'avviso di un servizio caduto (#593), della
        # scadenza della manutenzione (#705) e della pulizia delle caselle (#707) non sono
        # un'installazione.
        case $(basename "$other") in
            devloop-role-watchdog* | devloop-role-sentinel* | devloop-failed@* \
                | devloop-maintenance@* | devloop-mailbox-prune@*) continue ;;
        esac
        if [ -f "$other" ] && [ "$other" != "$UNIT_FILE" ]; then
            printf '%s\n' "$home/.ghapps-$PROJECT"
            return
        fi
    done
    printf '%s\n' "$home/.ghapps" "$home/.ghapps-$PROJECT"
}

# project_apps OVERLAY: le app del progetto, una per riga: quelle di APPS, delle chiavi dei ruoli
# (DEVLOOP_KEYS, SUPERVISOR_KEYS, COORDINATOR_KEYS) e delle tabelle [apps.<ruolo>] dell'overlay
# OVERLAY. APPS da solo non basta: con un insieme condiviso elenca soltanto le app da creare
# (ops/README.md).
project_apps() {
    local overlay=$1 app
    {
        for app in $APPS $DEVLOOP_KEYS $SUPERVISOR_KEYS $COORDINATOR_KEYS; do
            printf '%s\n' "$app"
        done
        [ -f "$overlay" ] && python3 -I -B - "$overlay" <<'EOF'
import sys, tomllib
try:
    with open(sys.argv[1], 'rb') as fh:
        apps = tomllib.load(fh).get('apps')
except (OSError, tomllib.TOMLDecodeError):
    sys.exit(0)
if isinstance(apps, dict):
    for name, table in apps.items():
        if isinstance(table, dict):
            print(name)
EOF
    } | grep -E '^[a-z0-9-]+$' | sort -u
}

# Le chiavi dimenticate nel vecchio posto (#582): quando il parametro PARAM (DEVLOOP_KEY_DIR,
# SUPERVISOR_KEY_DIR, COORDINATOR_KEY_DIR) indica una key_dir che non è quella di default, nessuna
# chiave di un'app del progetto (project_apps) resta nelle key_dir di default OLD... del ruolo: le
# chiavi si spostano, non si copiano (ops/host/migrate.md, "Spostare una key_dir"). Le righe
# nominano il parametro, non la key_dir nuova, che non va pubblicata.
check_left_keys() {
    local param=$1 dir=${!1} old app key count
    shift
    for old in "$@"; do
        [ -d "$old" ] || continue
        if [ "$old" = "$dir" ] || [ "$old" -ef "$dir" ]; then
            continue
        fi
        if ! [ -r "$old" ] || ! [ -x "$old" ]; then
            fail "$old non è leggibile da $(id -un): eseguire come root"
            continue
        fi
        count=0
        for app in $(project_apps "$STATE_PATH/config.local.toml"); do
            for key in "$old/$APP_PREFIX$app".*.private-key.pem; do
                [ -e "$key" ] && count=$((count + 1))
            done
        done
        if [ "$count" = 0 ]; then
            ok "$old, key_dir di default, non ha chiavi del progetto: $param indica un'altra key_dir"
        else
            fail "$old, key_dir di default, ha ancora $count chiavi di app del progetto, ma $param indica un'altra key_dir: spostarle, non copiarle (ops/host/migrate.md, \"Spostare una key_dir\")"
        fi
    done
}

# Il checkout da cui gira questo script, per un progetto con la sorgente a un tag (#687): le copie
# di root di ops/ si confrontano con quelle di questo checkout, e per quel progetto valgono solo
# se è al tag SOURCE_REF fissato. Un avviso se non lo è, o se non ha il tag. Nessun checkout di
# git (la copia di root del coordinatore): nessun confronto. Il git del sistema, non quello del
# PATH; nessun percorso nell'output.
if [ -n "$SOURCE_REPO" ]; then
    step "Checkout di check-host.sh e tag SOURCE_REF $SOURCE_REF (#687)"
    checkout=$(dirname "$OPS_DIR")
    if ! [ -e "$checkout/.git" ]; then
        say "  questo script non gira da un checkout di git: nessun confronto con il tag $SOURCE_REF"
    else
        head=$(GIT_OPTIONAL_LOCKS=0 /usr/bin/git -C "$checkout" rev-parse --verify --quiet \
            'HEAD^{commit}' 2>/dev/null)
        tagged=$(GIT_OPTIONAL_LOCKS=0 /usr/bin/git -C "$checkout" rev-parse --verify --quiet \
            "refs/tags/$SOURCE_REF^{commit}" 2>/dev/null)
        if [ -z "$head" ]; then
            warn "git non legge il commit del checkout di questo script: non si sa se è al tag $SOURCE_REF fissato, e le copie di root di ops/ si confrontano con quelle del checkout (ops/host/migrate.md, #687)"
        elif [ -z "$tagged" ]; then
            warn "il checkout di questo script (${head:0:12}) non ha il tag $SOURCE_REF fissato (git fetch origin --tags): le copie di root di ops/ si confrontano con quelle del checkout, non del tag; per questo progetto check-host.sh e setup-*.sh si eseguono dal checkout del tag (ops/host/migrate.md, #687)"
        elif [ "$head" != "$tagged" ]; then
            warn "il checkout di questo script è a ${head:0:12}, non al tag $SOURCE_REF fissato (${tagged:0:12}): le copie di root di ops/ si confrontano con quelle del checkout, non del tag; per questo progetto check-host.sh e setup-*.sh si eseguono dal checkout del tag (ops/host/migrate.md, #687)"
        else
            ok "il checkout di questo script è al tag $SOURCE_REF fissato (${head:0:12})"
        fi
    fi
fi

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

# check_claude_settings USER HOME SETUP: le impostazioni di Claude Code dell'utente di un ruolo
# senza aggiornamenti automatici (#643), come le scrive lo script SETUP. Un avviso: la CLI del
# ruolo può non essere claude.
check_claude_settings() {
    local why
    if why=$(claude_no_autoupdate "$2/.claude/settings.json"); then
        ok "$2/.claude/settings.json: autoUpdates false, DISABLE_AUTOUPDATER=1"
    else
        warn "$why: Claude Code di $1 si aggiornerebbe da solo ($3)"
    fi
}
step "Claude Code del supervisore senza aggiornamenti automatici (#643)"
check_claude_settings "$SUPERVISOR_USER" "$SUPERVISOR_HOME" setup-supervisor.sh

step 'Chiavi delle GitHub App (600)'
ops_devloop_keys "$STATE_PATH/config.local.toml"
check_keys "$DEVLOOP_KEY_DIR" "$DEVLOOP_USER" "$DEVLOOP_KEYS_REQUIRED" 0 "$DEVLOOP_KEYS_OPTIONAL"
check_keys "$SUPERVISOR_KEY_DIR" "$SUPERVISOR_USER" "$SUPERVISOR_KEYS" 1
# La key_dir di default di devloop è sempre ~/.ghapps: con PROJECT l'utente è del progetto.
check_left_keys DEVLOOP_KEY_DIR "$DEVLOOP_HOME/.ghapps"
# shellcheck disable=SC2046  # percorsi senza spazi (ops_load_params)
check_left_keys SUPERVISOR_KEY_DIR $(default_key_dirs "$SUPERVISOR_HOME")

# Il token del bot degli avvisi all'operatore (#593), se l'overlay ha [notify]: un file regolare,
# non un link, dell'utente devloop, 600, che nessun altro legge (devloop notify lo rifiuta
# altrimenti). Non se ne legge il contenuto.
step "Token del bot degli avvisi ([notify], #593)"
NOTIFY_TOKEN=$([ -f "$STATE_PATH/config.local.toml" ] && python3 -I -B - "$STATE_PATH/config.local.toml" <<'EOF'
import sys, tomllib
try:
    table = tomllib.load(open(sys.argv[1], 'rb')).get('notify') or {}
except (OSError, ValueError):
    sys.exit(0)
value = table.get('token_file') if isinstance(table, dict) else None
if isinstance(value, str):
    print(value)
EOF
)
if [ -z "$NOTIFY_TOKEN" ]; then
    ok "l'overlay non ha [notify] con token_file: nessun canale per gli avvisi, devloop li scrive solo nel log"
elif [ -L "$NOTIFY_TOKEN" ] || ! [ -f "$NOTIFY_TOKEN" ]; then
    fail "il token_file di [notify] non esiste o non è un file regolare: devloop notify non invia (docs/devloop/guide.md, \"Notices to the operator\")"
else
    actual=$(stat -c '%a %U' "$NOTIFY_TOKEN")
    if [ "$actual" = "600 $DEVLOOP_USER" ]; then
        ok "token_file di [notify]: 600, $DEVLOOP_USER"
    else
        fail "il token_file di [notify] è $actual, deve essere 600 $DEVLOOP_USER: devloop notify lo rifiuta"
    fi
fi

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
        # L'output di getfacl si legge per intero prima di cercarvi la voce: con pipefail,
        # getfacl | grep -q perde la voce se grep esce prima che getfacl abbia finito di
        # scrivere (SIGPIPE, 141).
        entry=$(getfacl -p --omit-header "$private" 2>/dev/null)
        if grep -q "^user:$user:" <<<"$entry"; then
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

# check_root_files DIR SETUP MISSING FILE...: i FILE di ops/ e params.env nella copia di root DIR,
# 644 di root e uguali al checkout e ai parametri; SETUP li installa. Un file che manca è un
# errore con MISSING=fail, un avviso con MISSING=warn.
check_root_files() {
    local dir=$1 setup=$2 missing=$3 name copy source_file actual
    shift 3
    for name in "$@" params.env; do
        copy=$dir/$name
        [ "$name" = params.env ] && source_file=$PARAMS_FILE || source_file=$OPS_DIR/$name
        if ! [ -f "$copy" ] || [ -L "$copy" ]; then
            "$missing" "$copy non esiste ($setup)"
            continue
        fi
        actual=$(stat -c '%a %U' "$copy")
        [ "$actual" = '644 root' ] || fail "$copy è $actual, deve essere 644 root"
        [ "$copy" -ef "$source_file" ] && continue
        if cmp -s "$copy" "$source_file"; then
            ok "$copy coincide con $source_file"
        else
            warn "$copy è diverso da $source_file: rieseguire $setup"
        fi
    done
}

# check_read_acl USER FILE SETUP: su FILE l'ACL di sola lettura user:USER:r--, che mette SETUP.
check_read_acl() {
    local user=$1 file=$2 setup=$3 entry
    command -v getfacl >/dev/null && [ -f "$file" ] || return 0
    # getfacl può aggiungere alla riga un commento #effective.
    entry=$(getfacl -p --omit-header "$file" 2>/dev/null | grep "^user:$user:" | head -n 1)
    entry=${entry%%[[:space:]]*}
    case $entry in
        "user:$user:r--") ok "ACL $entry su $file" ;;
        '') fail "manca l'ACL u:$user:r-- su $file ($setup)" ;;
        *) fail "ACL $entry su $file: deve essere r--, senza scrittura" ;;
    esac
}

# L'overlay e il file di progetto dello state_dir (#543): il supervisore li legge con l'ACL che
# mette setup-supervisor.sh, che una sostituzione del file (install, cp, rsync) cancella; l'utente
# degli agent no (lo prova il suo passo, qui sotto).
step "Lettura dell'overlay e del file di progetto per $SUPERVISOR_USER (#543)"
for file in "$STATE_PATH/config.local.toml" "$STATE_PATH/project.toml"; do
    if ! [ -f "$file" ] || [ -L "$file" ]; then
        fail "$file non esiste o è un link"
        continue
    fi
    check_read_acl "$SUPERVISOR_USER" "$file" setup-supervisor.sh
    check_as "$SUPERVISOR_USER" "$SUPERVISOR_USER legge $file" \
        /usr/bin/bash -c '[ -r "$1" ]' _ "$file"
done
# L'overlay è dell'operatore e dei ruoli che lo leggono con le loro ACL (#573): 600, o 640 con
# un'ACL; leggibile da tutti, ogni utente dell'host vi trova trusted_users, app_id e key_dir.
OVERLAY_MODE=$(stat -c '%A' "$STATE_PATH/config.local.toml" 2>/dev/null || true)
if [ "${OVERLAY_MODE:7:1}" = r ]; then
    warn "$STATE_PATH/config.local.toml è leggibile da tutti ($OVERLAY_MODE): chmod o-rwx, come lo crea setup-devloop.sh (600)"
fi

# La casella fra supervisore e coordinatore (#543): il gruppo, con dentro i due ruoli e nessun
# altro utente di devloop, e le directory, di root, del gruppo, 2770.
step "Casella fra supervisore e coordinatore: $COORDINATION_DIR, gruppo $COORDINATION_GROUP (#543)"
in_group() { id -nG "$1" 2>/dev/null | tr ' ' '\n' | grep -qxF "$COORDINATION_GROUP"; }
if getent group "$COORDINATION_GROUP" >/dev/null; then
    ok "il gruppo $COORDINATION_GROUP esiste"
else
    fail "il gruppo $COORDINATION_GROUP non esiste (setup-supervisor.sh)"
fi
# Il coordinatore è facoltativo per progetto, non per host (#571): fa parte di questo progetto solo
# se l'overlay ne dichiara l'app ([apps.coordinator]) o il ruolo ([roles.coordinator]). Un utente
# coordinator che l'host ha per un altro progetto non lo rende obbligatorio qui.
OVERLAY_FILE="$STATE_PATH/config.local.toml"
PROJECT_COORDINATOR=0
overlay_declares_coordinator "$OVERLAY_FILE" && PROJECT_COORDINATOR=1
roles=$SUPERVISOR_USER
[ "$PROJECT_COORDINATOR" = 1 ] && user_exists "$COORDINATOR_USER" && roles="$roles $COORDINATOR_USER"
for user in $roles; do
    if in_group "$user"; then
        ok "$user è nel gruppo $COORDINATION_GROUP"
    else
        fail "$user non è nel gruppo $COORDINATION_GROUP (setup-supervisor.sh, setup-coordinator.sh)"
    fi
done
for user in "$DEVLOOP_USER" "$AGENT_USER"; do
    case " $roles " in *" $user "*) continue ;; esac
    if in_group "$user"; then
        fail "$user è nel gruppo $COORDINATION_GROUP: deve restarne fuori"
    fi
done
# Nel gruppo, solo i ruoli di questo progetto (#629): un utente di un altro progetto, o di nessun
# ruolo, leggerebbe e scriverebbe la casella del progetto. Contano i membri supplementari (il
# quarto campo di getent group) e gli utenti che hanno il gruppo come primario (getent passwd).
group_line=$(getent group "$COORDINATION_GROUP" 2>/dev/null || true)
group_gid=$(printf '%s\n' "$group_line" | cut -d: -f3)
members=$(printf '%s\n' "$group_line" | cut -d: -f4 | tr ',' ' ')
if [ -n "$group_gid" ]; then
    members="$members $({ getent passwd 2>/dev/null || true; } \
                        | awk -F: -v gid="$group_gid" '$4 == gid { print $1 }')"
fi
box_listed=' '
for user in $members; do
    case "$box_listed" in *" $user "*) continue ;; esac
    box_listed="$box_listed$user "
    # Gli utenti devloop, degli agent, del watchdog e della sentinella hanno già la loro riga.
    case " $SUPERVISOR_USER $COORDINATOR_USER $DEVLOOP_USER $AGENT_USER $WATCHDOG_USER $SENTINEL_USER " in
        *" $user "*) continue ;;
    esac
    # Il nome di un utente estraneo non è un parametro del progetto, e il filtro non lo
    # sostituirebbe (#583): nell'output ordinario una descrizione generica, il nome con --verbose.
    if [ "$VERBOSE" = 1 ]; then
        fail "$user è nel gruppo $COORDINATION_GROUP, ma non è un ruolo di questo progetto: la casella è solo del supervisore e del coordinatore del progetto (gpasswd -d $user $COORDINATION_GROUP, o un altro gruppo primario; ops/host/migrate.md, \"Utenti dei ruoli per progetto\")"
    else
        fail "un utente che non è un ruolo di questo progetto è nel gruppo $COORDINATION_GROUP: la casella è solo del supervisore e del coordinatore del progetto (il nome e il comando con --verbose; ops/host/migrate.md, \"Utenti dei ruoli per progetto\")"
    fi
done
# La directory del riposo dei ruoli (#684) e COORDINATION_DIR (#685) sono 3770: con lo sticky bit,
# la sentinella e i ruoli rimuovono o sostituiscono solo i propri file, e nessun ruolo rinomina
# rest/ con la pausa dell'operatore.
for dir in $(coordination_dirs); do
    if [ -L "$dir" ] || ! [ -d "$dir" ]; then
        fail "$dir non esiste o non è una directory (setup-supervisor.sh)"
        continue
    fi
    box_mode=$(coordination_mode "$dir")
    actual=$(stat -c '%a %G' "$dir")
    if [ "$actual" = "$box_mode $COORDINATION_GROUP" ]; then
        ok "$dir: $box_mode, gruppo $COORDINATION_GROUP"
    else
        fail "$dir è $actual, deve essere $box_mode del gruppo $COORDINATION_GROUP"
    fi
    owner=$(stat -c '%U' "$dir")
    [ "$owner" = root ] || fail "$dir è di $owner, deve essere di root"
done

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

# Il risveglio del supervisore (#602): la copia di root di role-wait.sh che setup-supervisor.sh
# installa. Senza, il supervisore non ha il suo risveglio né il battito: un avviso; una copia che
# altri possono cambiare è un errore.
step "Risveglio del supervisore: copia di root in $SUPERVISOR_OPS_DIR (#602)"
if ! [ -d "$SUPERVISOR_OPS_DIR" ]; then
    warn "$SUPERVISOR_OPS_DIR non esiste: il supervisore non ha role-wait.sh né il battito (setup-supervisor.sh)"
else
    check_root_path "$SUPERVISOR_OPS_DIR/host"
    # shellcheck disable=SC2086  # nomi di file senza spazi
    check_root_files "$SUPERVISOR_OPS_DIR" setup-supervisor.sh warn $OPS_SUPERVISOR_FILES
fi

# Il coordinatore (#530), facoltativo per progetto (#571): la key_dir della sua app e la lettura
# dell'overlay, con il solo attraversamento della home di devloop (#549), il suo sudoers in sola
# lettura e la copia di root di check-host.sh, dei file che legge e dei parametri, da cui esegue
# `check-host.sh --read-only`. Solo se l'overlay del progetto lo dichiara.
step "Coordinatore $COORDINATOR_USER (facoltativo per progetto, #530, #571)"
if [ "$PROJECT_COORDINATOR" != 1 ]; then
    ok "$OVERLAY_FILE non dichiara né [apps.coordinator] né [roles.coordinator]: nessun coordinatore per questo progetto (per aggiungerlo, setup-coordinator.sh)"
elif ! user_exists "$COORDINATOR_USER"; then
    fail "$OVERLAY_FILE dichiara il coordinatore, ma l'utente $COORDINATOR_USER non esiste (setup-coordinator.sh)"
else
    ok "l'utente $COORDINATOR_USER esiste"
    # La key_dir della sua app (#549): 700, solo la chiave del coordinatore, 600.
    check_dir "$COORDINATOR_KEY_DIR" 700 "$COORDINATOR_USER"
    check_keys "$COORDINATOR_KEY_DIR" "$COORDINATOR_USER" "$COORDINATOR_KEYS" 1
    # shellcheck disable=SC2046  # percorsi senza spazi (ops_load_params)
    check_left_keys COORDINATOR_KEY_DIR $(default_key_dirs "$COORDINATOR_HOME")
    # L'overlay di devloop, da cui scripts/coordinator-token.py legge repo e [apps.coordinator]
    # (#549): solo l'attraversamento della home di devloop, l'ACL di sola lettura sull'overlay
    # (#554), e che il coordinatore lo legga.
    if command -v getfacl >/dev/null && [ -d "$DEVLOOP_HOME" ]; then
        check_traverse "$COORDINATOR_USER" setup-coordinator.sh
    fi
    check_read_acl "$COORDINATOR_USER" "$OVERLAY_FILE" setup-coordinator.sh
    check_as "$COORDINATOR_USER" "$COORDINATOR_USER legge $OVERLAY_FILE" \
        /usr/bin/bash -c '[ -r "$1" ]' _ "$OVERLAY_FILE"
    check_sudoers "$COORDINATOR_SUDOERS_FILE" sudoers-coordinator setup-coordinator.sh
    # Il clone del repository gestito, da cui parte la sua sessione (#643), e Claude Code senza
    # aggiornamenti automatici, come per il supervisore.
    if [ -d "$COORDINATOR_CLONE/.git" ]; then
        ok "$COORDINATOR_CLONE è un clone"
    else
        fail "$COORDINATOR_CLONE non è un clone: la sessione del coordinatore parte da lì (setup-coordinator.sh)"
    fi
    check_claude_settings "$COORDINATOR_USER" "$COORDINATOR_HOME" setup-coordinator.sh
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

# check_acls USER PRODUTTORE: le ACL "<directory> <permessi>" che PRODUTTORE (watchdog_acls,
# sentinel_acls, rest_devloop_acls) dà a USER, ognuna presente ed esatta.
check_acls() {
    local user=$1 dir perms entry
    while read -r dir perms; do
        [ -d "$dir" ] || continue
        entry=$(getfacl -p --omit-header "$dir" 2>/dev/null | grep "^user:$user:" | head -n 1)
        entry=${entry%%[[:space:]]*}
        if [ "$entry" = "user:$user:$perms" ]; then
            ok "ACL $entry su $dir"
        else
            fail "su $dir serve l'ACL u:$user:$perms, non '${entry:-nessuna}' (setup-supervisor.sh, setup-coordinator.sh)"
        fi
    done < <("$2")
}

# Il watchdog dei ruoli (#602), quando il progetto ha sia il supervisore sia il coordinatore: le
# unità template dell'host, le drop-in dell'istanza del progetto, il timer abilitato, l'utente,
# fuori dal gruppo dei ruoli e con le sole ACL sulla casella del progetto, e la copia di root.
step "Watchdog dei ruoli: $WATCHDOG_UNIT (#602)"
if [ "$PROJECT_COORDINATOR" != 1 ] || ! user_exists "$COORDINATOR_USER"; then
    ok "il progetto non ha il coordinatore: nessun watchdog dei ruoli"
else
    for unit in $OPS_WATCHDOG_UNITS; do
        if ! [ -f "$SYSTEMD_DIR/$unit" ]; then
            fail "$SYSTEMD_DIR/$unit non esiste (setup-supervisor.sh, setup-coordinator.sh)"
        elif cmp -s "$SYSTEMD_DIR/$unit" <(render "$OPS_DIR/host/$unit"); then
            ok "$SYSTEMD_DIR/$unit coincide con ops/host/$unit"
        else
            warn "$SYSTEMD_DIR/$unit è diverso da ops/host/$unit"
        fi
    done
    for dropin in "$WATCHDOG_DROPIN" "$WATCHDOG_FAILED_DROPIN"; do
        if ! [ -f "$dropin" ]; then
            fail "$dropin non esiste (setup-supervisor.sh, setup-coordinator.sh)"
        elif cmp -s "$dropin" <(render "$OPS_DIR/host/role-watchdog-project.conf"); then
            ok "$dropin coincide con ops/host/role-watchdog-project.conf"
        else
            warn "$dropin è diverso da ops/host/role-watchdog-project.conf con questi parametri"
        fi
    done
    enabled=$(systemctl is-enabled "$WATCHDOG_UNIT.timer" 2>/dev/null)
    if [ "$enabled" = enabled ]; then
        ok "$WATCHDOG_UNIT.timer è abilitato"
    else
        fail "$WATCHDOG_UNIT.timer non è abilitato (${enabled:-sconosciuto}): setup-supervisor.sh o setup-coordinator.sh"
    fi
    if ! user_exists "$WATCHDOG_USER"; then
        fail "l'utente $WATCHDOG_USER non esiste (setup-supervisor.sh, setup-coordinator.sh)"
    else
        ok "l'utente $WATCHDOG_USER esiste"
        if in_group "$WATCHDOG_USER"; then
            fail "$WATCHDOG_USER è nel gruppo $COORDINATION_GROUP: entra solo nella casella del suo progetto, con le ACL"
        fi
        check_acls "$WATCHDOG_USER" watchdog_acls
    fi
    check_root_path "$WATCHDOG_OPS_DIR/host"
    # shellcheck disable=SC2086  # nomi di file senza spazi
    check_root_files "$WATCHDOG_OPS_DIR" 'setup-supervisor.sh o setup-coordinator.sh' fail \
        $OPS_WATCHDOG_FILES
fi

# La sentinella dei ruoli a riposo (#684), con il watchdog: le unità template dell'host, la drop-in
# dell'istanza, il servizio abilitato e attivo, l'utente, fuori dal gruppo dei ruoli e con le sole
# ACL sulla casella (in rest/ la scrittura del segnale di risveglio), e l'attraversamento della
# casella e di rest/ per devloop, che legge lo stato di riposo dei ruoli.
step "Sentinella dei ruoli a riposo: $SENTINEL_UNIT (#684)"
if [ "$PROJECT_COORDINATOR" != 1 ] || ! user_exists "$COORDINATOR_USER"; then
    ok "il progetto non ha il coordinatore: nessuna sentinella dei ruoli"
else
    for unit in $OPS_SENTINEL_UNITS; do
        if ! [ -f "$SYSTEMD_DIR/$unit" ]; then
            fail "$SYSTEMD_DIR/$unit non esiste (setup-supervisor.sh, setup-coordinator.sh; ops/host/migrate.md, \"Il riposo dei ruoli (#684)\")"
        elif cmp -s "$SYSTEMD_DIR/$unit" <(render "$OPS_DIR/host/$unit"); then
            ok "$SYSTEMD_DIR/$unit coincide con ops/host/$unit"
        else
            warn "$SYSTEMD_DIR/$unit è diverso da ops/host/$unit"
        fi
    done
    if ! [ -f "$SENTINEL_DROPIN" ]; then
        fail "$SENTINEL_DROPIN non esiste (setup-supervisor.sh, setup-coordinator.sh)"
    elif cmp -s "$SENTINEL_DROPIN" <(render "$OPS_DIR/host/role-sentinel-project.conf"); then
        ok "$SENTINEL_DROPIN coincide con ops/host/role-sentinel-project.conf"
    else
        warn "$SENTINEL_DROPIN è diverso da ops/host/role-sentinel-project.conf con questi parametri"
    fi
    enabled=$(systemctl is-enabled "$SENTINEL_UNIT.service" 2>/dev/null)
    if [ "$enabled" = enabled ]; then
        ok "$SENTINEL_UNIT.service è abilitato"
    else
        fail "$SENTINEL_UNIT.service non è abilitato (${enabled:-sconosciuto}): setup-supervisor.sh o setup-coordinator.sh"
    fi
    active=$(systemctl is-active "$SENTINEL_UNIT.service" 2>/dev/null)
    if [ "$active" = active ]; then
        ok "$SENTINEL_UNIT.service è attivo"
    else
        fail "$SENTINEL_UNIT.service non è attivo (${active:-sconosciuto}): i ruoli a riposo non si svegliano (journalctl -u $SENTINEL_UNIT)"
    fi
    if ! user_exists "$SENTINEL_USER"; then
        fail "l'utente $SENTINEL_USER non esiste (setup-supervisor.sh, setup-coordinator.sh)"
    else
        ok "l'utente $SENTINEL_USER esiste"
        if in_group "$SENTINEL_USER"; then
            fail "$SENTINEL_USER è nel gruppo $COORDINATION_GROUP: entra solo nella casella del suo progetto, con le ACL"
        fi
        check_acls "$SENTINEL_USER" sentinel_acls
    fi
    check_acls "$DEVLOOP_USER" rest_devloop_acls
fi

# La pulizia di done/ delle caselle (#707): le unità template dell'host, la drop-in dell'istanza
# del progetto, con la copia di root da cui gira mailbox.sh prune (quella del coordinatore, o del
# supervisore se il progetto non ha il coordinatore), e il timer abilitato e attivo. La copia di
# mailbox.sh si verifica con le copie di root, sopra.
step "Pulizia di done/ delle caselle: $MAILBOX_UNIT (#707)"
for unit in $OPS_MAILBOX_UNITS; do
    if ! [ -f "$SYSTEMD_DIR/$unit" ]; then
        fail "$SYSTEMD_DIR/$unit non esiste (setup-coordinator.sh, o setup-supervisor.sh senza coordinatore)"
    elif cmp -s "$SYSTEMD_DIR/$unit" <(render "$OPS_DIR/host/$unit"); then
        ok "$SYSTEMD_DIR/$unit coincide con ops/host/$unit"
    else
        warn "$SYSTEMD_DIR/$unit è diverso da ops/host/$unit"
    fi
done
if [ "$PROJECT_COORDINATOR" = 1 ] && user_exists "$COORDINATOR_USER"; then
    expected=$COORDINATOR_OPS_DIR other=$SUPERVISOR_OPS_DIR
else
    expected=$SUPERVISOR_OPS_DIR other=$COORDINATOR_OPS_DIR
fi
if ! [ -f "$MAILBOX_DROPIN" ]; then
    fail "$MAILBOX_DROPIN non esiste (setup-coordinator.sh, o setup-supervisor.sh senza coordinatore)"
elif MAILBOX_OPS_DIR=$expected \
    && cmp -s "$MAILBOX_DROPIN" <(render "$OPS_DIR/host/mailbox-prune-project.conf"); then
    ok "$MAILBOX_DROPIN coincide con ops/host/mailbox-prune-project.conf, dalla copia $expected"
elif MAILBOX_OPS_DIR=$other \
    && cmp -s "$MAILBOX_DROPIN" <(render "$OPS_DIR/host/mailbox-prune-project.conf"); then
    if [ "$other" = "$COORDINATOR_OPS_DIR" ]; then
        ok "$MAILBOX_DROPIN coincide con ops/host/mailbox-prune-project.conf, dalla copia $other"
    else
        warn "$MAILBOX_DROPIN usa la copia del supervisore, $other: il progetto ha il coordinatore, rieseguire setup-coordinator.sh"
    fi
else
    warn "$MAILBOX_DROPIN è diverso da ops/host/mailbox-prune-project.conf con questi parametri"
fi
enabled=$(systemctl is-enabled "$MAILBOX_UNIT.timer" 2>/dev/null)
if [ "$enabled" = enabled ]; then
    ok "$MAILBOX_UNIT.timer è abilitato"
else
    fail "$MAILBOX_UNIT.timer non è abilitato (${enabled:-sconosciuto}): setup-coordinator.sh o setup-supervisor.sh"
fi
active=$(systemctl is-active "$MAILBOX_UNIT.timer" 2>/dev/null)
if [ "$active" = active ]; then
    ok "$MAILBOX_UNIT.timer è attivo"
else
    fail "$MAILBOX_UNIT.timer non è attivo (${active:-sconosciuto}): done/ delle caselle non si pulisce (systemctl start $MAILBOX_UNIT.timer)"
fi

# Il flusso degli eventi (#683): devloop run lo scrive nello state_dir, le sveglie dei ruoli lo
# leggono come sé stesse. Scrivibile solo da devloop (di DEVLOOP_USER, nessuna scrittura per il
# gruppo, gli altri o un'ACL), leggibile dai ruoli del progetto con le ACL di sola lettura di
# setup-supervisor.sh e setup-coordinator.sh, anche di default per i segmenti nuovi.
step "Flusso degli eventi: $STATE_PATH/$EVENTS_DIR (#683)"
EVENTS_PATH=$STATE_PATH/$EVENTS_DIR
# check_events_writer PATH: PATH di DEVLOOP_USER, senza scrittura per altri, nemmeno con un'ACL.
check_events_writer() {
    local path=$1 owner mode writers
    read -r owner mode < <(stat -c '%U %a' "$path")
    if [ "$owner" != "$DEVLOOP_USER" ] || (( 8#$mode & 8#022 )); then
        fail "$path è $mode $owner: il flusso è di $DEVLOOP_USER e solo devloop lo scrive"
        return
    fi
    if command -v getfacl >/dev/null; then
        writers=$(getfacl -p --omit-header "$path" 2>/dev/null \
                      | grep -Ev '^(default:|user::|mask::|#)' \
                      | awk -F: '$3 ~ /w/ { print $1 ":" $2 ":" $3 }' || true)
        if [ -n "$writers" ]; then
            fail "$path ha un'ACL di scrittura ($(tr '\n' ' ' <<<"$writers" | sed 's/ $//')): solo devloop scrive il flusso"
            return
        fi
    fi
    ok "$path: $mode, $owner, scritto solo da devloop"
}
if [ -L "$EVENTS_PATH" ] || { [ -e "$EVENTS_PATH" ] && ! [ -d "$EVENTS_PATH" ]; }; then
    fail "$EVENTS_PATH non è una directory: le sveglie dei ruoli non leggono gli eventi"
elif ! [ -d "$EVENTS_PATH" ]; then
    warn "$EVENTS_PATH non esiste: le sveglie dei ruoli non hanno eventi finché setup-supervisor.sh (o setup-coordinator.sh) non lo crea e devloop run non lo scrive"
else
    check_events_writer "$EVENTS_PATH"
    for file in "$EVENTS_PATH"/*; do
        [ -f "$file" ] && ! [ -L "$file" ] || continue
        check_events_writer "$file"
    done
    readers="$SUPERVISOR_USER:setup-supervisor.sh"
    [ "$PROJECT_COORDINATOR" = 1 ] && user_exists "$COORDINATOR_USER" \
        && readers="$readers $COORDINATOR_USER:setup-coordinator.sh"
    # Il watchdog e la sentinella dei ruoli (#684): il lavoro trattenuto da devloop run, e la
    # sentinella anche gli eventi.
    for user in "$WATCHDOG_USER" "$SENTINEL_USER"; do
        [ "$PROJECT_COORDINATOR" = 1 ] && user_exists "$COORDINATOR_USER" && user_exists "$user" \
            && readers="$readers $user:setup-coordinator.sh"
    done
    for reader in $readers; do
        user=${reader%%:*}
        setup=${reader#*:}
        if command -v getfacl >/dev/null; then
            entries=$(getfacl -p --omit-header "$EVENTS_PATH" 2>/dev/null | sed 's/[[:space:]].*//' || true)
            for wanted in "user:$user:r-x" "default:user:$user:r--"; do
                if grep -qx "$wanted" <<<"$entries"; then
                    ok "ACL $wanted su $EVENTS_PATH"
                else
                    fail "su $EVENTS_PATH serve l'ACL $wanted, non '$(grep "^${wanted%:*}:" <<<"$entries" | head -n 1 || true)' ($setup)"
                fi
            done
            for file in "$EVENTS_PATH"/*; do
                [ -f "$file" ] && ! [ -L "$file" ] || continue
                check_read_acl "$user" "$file" "$setup"
            done
        fi
        if [ -f "$EVENTS_PATH/$EVENTS_ALIVE" ]; then
            check_as "$user" "$user legge $EVENTS_PATH/$EVENTS_ALIVE" \
                /usr/bin/bash -c '[ -r "$1" ]' _ "$EVENTS_PATH/$EVENTS_ALIVE"
        fi
    done
fi
# La regola dei sudoers dei ruoli per devloop-pinned watch (#683) non c'è più: le sveglie leggono
# il flusso. Un sudoers che la contiene ancora è di prima: si riesegue lo script del ruolo.
for installed in "$SUDOERS_FILE:setup-supervisor.sh" "$COORDINATOR_SUDOERS_FILE:setup-coordinator.sh"; do
    file=${installed%:*}
    [ -f "$file" ] && [ -r "$file" ] || continue
    if grep -Eq 'devloop-pinned[^,]* watch([ ,]|$)' "$file"; then
        fail "$file contiene ancora la regola dei sudoers per devloop-pinned watch, tolta dal #683: rieseguire ${installed##*:} (ops/host/migrate.md, \"Il flusso degli eventi per le sveglie dei ruoli (#683)\")"
    else
        ok "$file non ha la regola dei sudoers per devloop-pinned watch (#683)"
    fi
done

step "Utente degli agent $AGENT_USER (#415)"
STATE="$STATE_PATH"
for private in "$DEVLOOP_KEY_DIR" "$STATE" "$STATE/config.local.toml" "$STATE/project.toml"; do
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
# Le directory di cui agy si fida (#572): al più quella dei worktree dello state_dir, dove girano
# i turni. Una home intera, o una directory fuori dallo state_dir, è un avviso.
AGY_SETTINGS="$AGENT_HOME/.gemini/antigravity-cli/settings.json"
if agy_why=$(agy_trusted_workspaces check "$AGY_SETTINGS" "$STATE" "$DEVLOOP_HOME" \
        "$SUPERVISOR_HOME" "$AGENT_HOME" "$COORDINATOR_HOME"); then
    ok "$AGY_SETTINGS: trustedWorkspaces non nomina home né directory fuori da $STATE"
else
    while IFS= read -r line; do
        [ -n "$line" ] && warn "$line (al più [\"$STATE/worktrees\"]: setup-agent-user.sh)"
    done <<<"${agy_why:-$AGY_SETTINGS non si legge}"
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
# L'OnFailure= del servizio (#593): l'avviso all'operatore di un servizio caduto.
check_unit "$FAILED_UNIT_FILE" devloop-failed@.service
# L'avviso della scadenza della manutenzione dell'host (#705): il timer, abilitato, e la sua unità.
check_unit "$MAINTENANCE_UNIT_FILE" devloop-maintenance@.service
check_unit "$MAINTENANCE_TIMER_FILE" devloop-maintenance@.timer
enabled=$(systemctl is-enabled "$MAINTENANCE_UNIT.timer" 2>/dev/null)
if [ "$enabled" = enabled ]; then
    ok "$MAINTENANCE_UNIT.timer è abilitato"
else
    warn "$MAINTENANCE_UNIT.timer non è abilitato (${enabled:-sconosciuto}): la scadenza di una manutenzione dell'host non avvisa l'operatore (setup-devloop.sh)"
fi
# Un repository, un host (ADR 0006, R8): nessun'altra unità di devloop di questo host lo gestisce.
for other in "$SYSTEMD_DIR"/devloop*.service "$SYSTEMD_DIR"/devloop@*.service.d/project.conf; do
    [ -f "$other" ] || continue
    [ "$other" = "$UNIT_FILE" ] || [ "$other" = "$DROPIN_FILE" ] && continue
    if grep -qixF "Description=devloop continuous loop ($REPO)" "$other"; then
        fail "$other gestisce anche $REPO: un repository, un servizio"
    fi
done
state=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)
# La pausa dell'operatore (#685): il servizio fermo è la pausa, finché l'operatore non la toglie.
pause=$(ops_pause_start) || pause=''
if [ -n "$pause" ]; then
    say "  in pausa per ordine dell'operatore dal $(date -u -d "@$pause" +%Y-%m-%dT%H:%M:%SZ): devloop run non parte finché l'operatore non la toglie (devloop resume)"
fi
# La manutenzione dell'host (#705): il servizio fermo è la manutenzione, finché non la si chiude.
maintenance=$(ops_maintenance) || maintenance=''
if [ -n "$maintenance" ]; then
    say "  in manutenzione $(ops_maintenance_text "$maintenance"): devloop run non fa tick e devloop pin si rifiuta finché non la si chiude (devloop maintenance end)"
fi
case $state in
    active) ok "$SERVICE_NAME è attivo" ;;
    failed) fail "$SERVICE_NAME è fallito: journalctl -u $SERVICE_NAME e $LOG" ;;
    *)
        if [ -n "$maintenance" ]; then
            ok "$SERVICE_NAME non è attivo (${state:-sconosciuto}): la manutenzione dell'host"
        elif [ -n "$pause" ]; then
            ok "$SERVICE_NAME non è attivo (${state:-sconosciuto}): la pausa dell'operatore"
        else
            warn "$SERVICE_NAME non è attivo (${state:-sconosciuto}): l'avvio è dell'operatore"
        fi
        ;;
esac
enabled=$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null)
say "  $SERVICE_NAME all'avvio della macchina: ${enabled:-sconosciuto}"
killmode=$(systemctl show -p KillMode --value "$SERVICE_NAME" 2>/dev/null)
if [ -n "$killmode" ] && [ "$killmode" != mixed ]; then
    fail "KillMode=$killmode: deve essere mixed"
fi

# Il comando devloop dell'host (#588): l'involucro e il file di questo progetto, come li genera
# setup-devloop.sh. Senza, il servizio gira lo stesso: un avviso. Root li esegue e li legge: un
# file o una directory che altri possono cambiare è un errore.
step "Comando devloop: $DEVLOOP_COMMAND, progetto $OPS_INSTANCE (#588)"
check_generated() {
    local file=$1 template=$2 mode=$3 actual
    if ! [ -f "$file" ] || [ -L "$file" ]; then
        warn "$file non esiste o è un link: il comando devloop non c'è per questo progetto (setup-devloop.sh)"
        return
    fi
    actual=$(stat -c '%a %U' "$file")
    [ "$actual" = "$mode root" ] || fail "$file è $actual, deve essere $mode root (setup-devloop.sh)"
    if cmp -s "$file" <(render "$OPS_DIR/host/$template"); then
        ok "$file coincide con ops/host/$template"
    else
        warn "$file è diverso da ops/host/$template con questi parametri: rieseguire setup-devloop.sh"
    fi
}
if [ -d "$DEVLOOP_PROJECTS_DIR" ] && ! [ -L "$DEVLOOP_PROJECTS_DIR" ]; then
    read -r owner mode < <(stat -c '%U %a' "$DEVLOOP_PROJECTS_DIR")
    if [ "$owner" != root ] || (( 8#$mode & 8#022 )); then
        fail "$DEVLOOP_PROJECTS_DIR è $mode $owner: deve essere di root e non scrivibile dal gruppo né dagli altri"
    else
        ok "$DEVLOOP_PROJECTS_DIR è di root, non scrivibile da altri"
    fi
fi
check_generated "$DEVLOOP_COMMAND" devloop-command 755
check_generated "$DEVLOOP_PROJECT_FILE" devloop-command-project 644

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

# check_role_clone RUOLO UTENTE CLONE KEY_DIR TOKEN_CLONE: che il clone del ruolo si aggiorni
# (#717). Un repository che si legge senza credenziali si aggiorna con git fetch; uno privato con
# l'app del ruolo, limitata a contents=read: come l'utente del ruolo, `role-git.py check`, che
# legge il repository con quel token senza scrivere nulla nel clone. Interroga GitHub: non con
# --no-apps né con --read-only; con --dry-run stampa soltanto.
check_role_clone() {
    local role=$1 user=$2 clone=$3 key_dir=$4 token_clone=$5
    if [ ! -d "$clone/.git" ]; then
        say "  $clone non è un clone: nessun aggiornamento da verificare (setup-$role.sh)"
        return 0
    fi
    role_git_cmd "$role" "$key_dir" "$token_clone" check "$clone"
    if [ "$DRY_RUN" = 1 ]; then
        say "  se $REPO non si legge senza credenziali (repository privato, #717):"
        check_as "$user" "$clone si aggiorna con l'app $role" "${ROLE_GIT[@]}"
    elif repo_public; then
        ok "$REPO si legge senza credenziali: $clone si aggiorna senza l'app $role"
    else
        check_as "$user" "$clone si aggiorna con l'app $role (contents: read, #717)" "${ROLE_GIT[@]}"
    fi
}
step "Aggiornamento dei clone dei ruoli (#717)"
if [ "$CHECK_APPS" = 1 ]; then
    token_clone=$SUPERVISOR_CLONE
    [ -z "$SOURCE_REPO" ] || token_clone=$SUPERVISOR_SOURCE_CLONE
    check_role_clone supervisor "$SUPERVISOR_USER" "$SUPERVISOR_CLONE" "$SUPERVISOR_KEY_DIR" \
        "$token_clone"
    if [ "$PROJECT_COORDINATOR" = 1 ] && user_exists "$COORDINATOR_USER"; then
        token_clone=$COORDINATOR_CLONE
        [ -z "$SOURCE_REPO" ] || token_clone=$COORDINATOR_SOURCE_CLONE
        check_role_clone coordinator "$COORDINATOR_USER" "$COORDINATOR_CLONE" \
            "$COORDINATOR_KEY_DIR" "$token_clone"
    fi
elif [ "$READ_ONLY" = 1 ]; then
    say '  saltato (--read-only)'
else
    say '  saltato (--no-apps)'
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
