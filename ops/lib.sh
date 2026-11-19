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
OPS_TEMPLATE_VARS='REPO DEVLOOP_USER SUPERVISOR_USER DEVLOOP_HOME DEVLOOP_CLONE STATE_DIR STATE_PATH LAUNCHER_CMD DEVLOOP_PATH SERVICE_NAME'

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
    : "${DEVLOOP_USER:=devloop}"
    : "${SUPERVISOR_USER:=supervisor}"
    : "${DEFAULT_BRANCH:=main}"
    : "${GITHUB_OWNER_TYPE:=user}"
    : "${DEVLOOP_HOME:=/home/$DEVLOOP_USER}"
    : "${SUPERVISOR_HOME:=/home/$SUPERVISOR_USER}"
    : "${DEVLOOP_CLONE:=$DEVLOOP_HOME/${REPO#*/}}"
    : "${SUPERVISOR_CLONE:=$SUPERVISOR_HOME/${REPO#*/}}"
    : "${STATE_DIR:=$OPS_STATE_DIR}"
    : "${DEVLOOP_KEY_DIR:=$DEVLOOP_HOME/.ghapps}"
    : "${SUPERVISOR_KEY_DIR:=$SUPERVISOR_HOME/.ghapps}"
    : "${DEVLOOP_KEYS:=dispatcher merger claude-agent codex-agent agy-agent}"
    : "${SUPERVISOR_KEYS:=supervisor claude-agent}"
    : "${DEVLOOP_PATH:=$DEVLOOP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin}"
    : "${SERVICE_NAME:=devloop}"
    : "${SYSTEMD_DIR:=/etc/systemd/system}"
    : "${SUDOERS_FILE:=/etc/sudoers.d/devloop-supervisor}"
    : "${APPS:=dispatcher merger claude-agent codex-agent agy-agent implementer supervisor}"
    : "${SOURCE_REPO:=}"
    : "${SOURCE_REF:=}"
    : "${SOURCE_CLONE:=$DEVLOOP_HOME/devloop-source}"
    : "${LAUNCHER_DIR:=/usr/local/lib/devloop}"

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
    [[ $SERVICE_NAME =~ ^[A-Za-z0-9_-]+$ ]] || die "SERVICE_NAME non valido: '$SERVICE_NAME'"
    [[ $DEVLOOP_PATH =~ ^[A-Za-z0-9._/:-]+$ ]] || die "DEVLOOP_PATH non valido: '$DEVLOOP_PATH'"
    [[ $(basename "$SUDOERS_FILE") =~ ^[A-Za-z0-9_-]+$ ]] \
        || die "il nome di SUDOERS_FILE non può contenere '.' né '~' (sudo lo ignorerebbe)"
    _require_name DEVLOOP_USER
    _require_name SUPERVISOR_USER
    local path
    for path in DEVLOOP_HOME SUPERVISOR_HOME DEVLOOP_CLONE SUPERVISOR_CLONE DEVLOOP_KEY_DIR \
        SUPERVISOR_KEY_DIR SYSTEMD_DIR SUDOERS_FILE; do
        _require_abs "$path"
    done
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
