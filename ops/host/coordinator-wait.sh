#!/usr/bin/env bash
# La sveglia del coordinatore di devloop (#543): aspetta, come utente del coordinatore, finché
# succede qualcosa che lo riguarda, lo stampa ed esce. La sessione del coordinatore la avvia come
# comando in background, legge le righe quando esce, agisce e la riavvia
# (docs/devloop/roles/coordinator.md).
#
# Si sveglia, ed esce con 0, su:
# - un messaggio nella casella to-coordinator/ di COORDINATION_DIR (#543): lo segnala senza
#   consumarlo; il coordinatore lo legge, poi lo sposta in done/ o lo rimuove;
# - un evento del coordinatore su GitHub, da devloop-pinned watch come utente devloop, con il
#   comando esatto del suo sudoers e il suo file di stato (coordinator-watch-state.json dello
#   state_dir), distinto da quello del supervisore, così non ne consuma gli eventi: la label
#   agent:needs-operator aggiunta, "il merge spetta all'operatore", un commento sull'issue degli
#   allarmi ([run].alert_issue dell'overlay), un errore del watch. Gli altri eventi li salta;
# - una riga nuova di run.log (tail -n 200 come utente devloop, dal suo sudoers) con un
#   Traceback, un allarme (ALERT:), un 401 o "Bad credentials", o il rifiuto dell'avvio
#   ("devloop: ...", l'uscita del comando);
# - il servizio non attivo (systemctl is-active), una volta per ogni cambio di stato;
# - la fine del periodo (COORDINATOR_WAIT_PERIOD, 4 ore per default; --period), senza eventi:
#   il controllo periodico.
#
# Ogni riga dice perché si è svegliata: "casella:", "evento:", "log:", "servizio:", "periodo:".
# Esce con 1 se devloop watch non riesce (il sudoers manca o non è aggiornato, devloop non
# parte), con 2 su un'opzione sbagliata. Non scrive nulla fuori dal suo stato, in
# ~/.local/state/devloop-coordinator-wait/ del coordinatore: le righe di run.log già viste e
# l'ultimo stato del servizio. Va eseguito come COORDINATOR_USER, dalla copia di root che
# setup-coordinator.sh installa in COORDINATOR_OPS_DIR, con i suoi parametri.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e  # un comando che fallisce si gestisce qui, non interrompe la sveglia

# Il comando di devloop watch del sudoers del coordinatore (ops/host/sudoers-coordinator): gli
# stessi argomenti, nello stesso ordine. Ogni giro aspetta al più WATCH_TIMEOUT secondi, poi
# rilegge la casella, il log e il servizio.
WATCH_INTERVAL=60
WATCH_TIMEOUT=300
WATCH_STATE_NAME=coordinator-watch-state.json
LOG_LINES=200
NEEDS_OPERATOR=agent:needs-operator

usage() {
    say "Uso: bash $0 [--params FILE] [--period SECONDI]"
    say
    say '  --period SECONDI  il controllo periodico (default: COORDINATOR_WAIT_PERIOD, 14400)'
    say '  --params FILE     i parametri (default: params.env accanto a questa copia)'
}

PERIOD=''
while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --period)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            PERIOD=$2
            shift 2
            ;;
        --params)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            PARAMS_FILE=$2
            shift 2
            ;;
        *) printf '%s: opzione sconosciuta: %s (vedi --help)\n' "$(basename "$0")" "$1" >&2; exit 2 ;;
    esac
done

ops_load_params
: "${PERIOD:=$COORDINATOR_WAIT_PERIOD}"
[[ $PERIOD =~ ^[1-9][0-9]{0,6}$ ]] || { say "--period vuole un numero di secondi: '$PERIOD'" >&2; exit 2; }
[ "$(id -un)" = "$COORDINATOR_USER" ] || die "va eseguito come $COORDINATOR_USER, il coordinatore"
cd / || exit 1

BOX=$COORDINATION_DIR/to-coordinator
[ -d "$BOX" ] && [ -r "$BOX" ] && [ -x "$BOX" ] \
    || die "non leggo la casella $BOX: setup-coordinator.sh, poi una sessione nuova del coordinatore (il gruppo $COORDINATION_GROUP)"
LOG=$STATE_PATH/run.log
WATCH_STATE=$STATE_PATH/$WATCH_STATE_NAME
MINE=${HOME:?}/.local/state/devloop-coordinator-wait/${PROJECT:-default}
mkdir -p -m 700 "$MINE" || die "non creo $MINE"
SEEN=$MINE/run.log.seen
SERVICE_SEEN=$MINE/service

# L'issue degli allarmi ([run].alert_issue dell'overlay), se il coordinatore la legge.
ALERT_ISSUE=$(python3 -I -B - "$STATE_PATH/config.local.toml" 2>/dev/null <<'EOF'
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    value = (tomllib.load(fh).get('run') or {}).get('alert_issue')
if isinstance(value, int) and not isinstance(value, bool) and value > 0:
    print(value)
EOF
)

# Una riga di devloop watch che riguarda il coordinatore (docs/devloop/guide.md, "Waking the
# supervising agent").
coordinator_event() {
    local line=$1
    # La label aggiunta, o fra quelle di un'issue o di una PR nuova ("new PR #N [a,b] titolo").
    [[ $line == *" +$NEEDS_OPERATOR"* ]] && return 0
    if [[ $line =~ ^new\ (PR|issue)\ \#[0-9]+\ \[([^]]*)\] ]]; then
        case ",${BASH_REMATCH[2]}," in *",$NEEDS_OPERATOR,"*) return 0 ;; esac
    fi
    [[ ${line,,} == *"merge spetta all'operatore"* ]] && return 0
    [[ $line == 'watch error:'* ]] && return 0
    [ -n "$ALERT_ISSUE" ] && [[ $line =~ ^issue\ \#$ALERT_ISSUE\ (operator\ )?comment\ by\  ]] \
        && return 0
    return 1
}

# Il contenuto atomico di FILE: TESTO.
save() {
    local file=$1 text=$2
    printf '%s' "$text" >"$file.tmp" && mv -f "$file.tmp" "$file"
}

TAIL_FAILED=0
# In EVENTS le righe della casella, del log e del servizio, una per evento; vuoto se nulla.
local_events() {
    local found=() name names='' tail_out new line state last
    EVENTS=''
    while IFS= read -r -d '' name; do
        found+=("$name")
    done < <(find "$BOX" -mindepth 1 -maxdepth 1 -type f ! -name '.*' -printf '%f\0' 2>/dev/null \
                 | sort -z)
    if [ ${#found[@]} -gt 0 ]; then
        names=$(printf ' %s' "${found[@]}")
        EVENTS+="casella: ${#found[@]} messaggi in $BOX:$names"$'\n'
    fi
    if tail_out=$(sudo -n -u "$DEVLOOP_USER" -- /usr/bin/tail -n "$LOG_LINES" "$LOG" 2>/dev/null); then
        # La prima volta le righe di run.log sono la base, non eventi.
        if [ -f "$SEEN" ]; then
            new=$(grep -Fxv -f "$SEEN" <<<"$tail_out")
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                if grep -Eq 'Traceback|ALERT:|(^|[^0-9])401([^0-9]|$)|[Bb]ad credentials|^devloop: ' \
                    <<<"$line"; then
                    EVENTS+="log: ${line:0:300}"$'\n'
                fi
            done <<<"$new"
        fi
        save "$SEEN" "$tail_out"$'\n'
    elif [ "$TAIL_FAILED" = 0 ]; then
        TAIL_FAILED=1
        printf 'coordinator-wait: tail di %s come %s non riuscito: il log non si sorveglia\n' \
            "$LOG" "$DEVLOOP_USER" >&2
    fi
    state=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)
    state=${state:-sconosciuto}
    last=$(cat "$SERVICE_SEEN" 2>/dev/null || printf active)
    if [ "$state" != active ] && [ "$state" != "$last" ]; then
        EVENTS+="servizio: $SERVICE_NAME è $state"$'\n'
    fi
    [ "$state" = "$last" ] || save "$SERVICE_SEEN" "$state"
}

# shellcheck disable=SC2206  # LAUNCHER_CMD: percorsi senza spazi, controllati da ops_load_params
WATCH=(sudo -n -u "$DEVLOOP_USER" -- $LAUNCHER_CMD watch --until-change
       --interval "$WATCH_INTERVAL" --timeout "$WATCH_TIMEOUT" --state "$WATCH_STATE")

SECONDS=0
while :; do
    local_events
    if [ -n "$EVENTS" ]; then
        printf '%s' "$EVENTS"
        exit 0
    fi
    if [ "$SECONDS" -ge "$PERIOD" ]; then
        say "periodo: ${PERIOD}s senza eventi del coordinatore: il controllo periodico"
        exit 0
    fi
    out=$("${WATCH[@]}")
    status=$?
    case $status in
        0)
            events=''
            while IFS= read -r line; do
                coordinator_event "$line" && events+="evento: $line"$'\n'
            done <<<"$out"
            if [ -n "$events" ]; then
                printf '%s' "$events"
                exit 0
            fi
            ;;
        3) ;;  # nessuna novità entro WATCH_TIMEOUT
        *)
            say "errore: devloop watch come $DEVLOOP_USER è uscito con $status (sudoers del coordinatore, setup-coordinator.sh; o devloop non parte)"
            exit 1
            ;;
    esac
done
