#!/usr/bin/env bash
# Il risveglio dei ruoli di devloop (#602; ADR 0002, seconda precisazione del 2026-11-23, punto
# 6), generalizzazione della sveglia del coordinatore (#543): aspetta, come utente del ruolo,
# finché succede qualcosa che lo riguarda, lo stampa ed esce. La sessione del ruolo la avvia come
# comando in background, legge le righe quando esce, agisce e la riavvia
# (docs/devloop/roles/supervisor.md, docs/devloop/roles/coordinator.md).
#
#   role-wait.sh --role supervisor|coordinator [--period SECONDI] [--params FILE]
#
# A ogni avvio scrive il battito del ruolo, COORDINATION_DIR/heartbeat/<ruolo>: l'ora (secondi
# dall'epoca) e il periodo, "<ora> <periodo>", 664 del gruppo dei ruoli. Il watchdog dei ruoli
# (role-watchdog.sh, il timer devloop-role-watchdog@<progetto>) lo legge: un battito più vecchio
# di due periodi è un ruolo fermo. Un battito che non si scrive è un avviso su stderr, non ferma
# il risveglio.
#
# Si sveglia, ed esce con 0, su:
# - un messaggio nella casella del ruolo (to-supervisor/ o to-coordinator/ di COORDINATION_DIR):
#   lo segnala senza consumarlo; il ruolo lo legge, poi lo sposta in done/ o lo rimuove;
# - un evento di devloop-pinned watch come utente devloop, con il comando dei sudoers dei ruoli
#   e un file di stato per ruolo dello state_dir (watch-state.json, il default di devloop, per il
#   supervisore; coordinator-watch-state.json per il coordinatore), così un ruolo non consuma
#   gli eventi dell'altro. Il supervisore si sveglia su ogni evento (non sulla riga della base,
#   "watch baseline:"). Il coordinatore solo sui suoi: la label agent:needs-operator aggiunta,
#   "il merge spetta all'operatore", un commento sull'issue degli allarmi ([run].alert_issue
#   dell'overlay), un errore del watch;
# - solo per il coordinatore, una riga nuova di run.log (tail -n 200 come utente devloop, dal suo
#   sudoers) con un Traceback, un allarme (ALERT:), un 401 o "Bad credentials", o il rifiuto
#   dell'avvio ("devloop: ...", l'uscita del comando);
# - il servizio non attivo (systemctl is-active), una volta per ogni cambio di stato;
# - la fine del periodo (--period; per default COORDINATOR_WAIT_PERIOD per il coordinatore e
#   ROLE_WAIT_PERIOD per il supervisore, un'ora), senza eventi: il controllo periodico.
#
# Ogni riga dice perché si è svegliata: "casella:", "evento:", "log:", "servizio:", "periodo:".
# Esce con 1 se devloop watch non riesce (il sudoers manca o non è aggiornato, devloop non
# parte), con 2 su un'opzione sbagliata. Non scrive nulla fuori dal battito e dal suo stato, in
# ~/.local/state/devloop-<ruolo>-wait/ dell'utente del ruolo: le righe di run.log già viste e
# l'ultimo stato del servizio. Va eseguito come l'utente del ruolo, dalla copia di root che
# setup-supervisor.sh (SUPERVISOR_OPS_DIR) o setup-coordinator.sh (COORDINATOR_OPS_DIR)
# installa, con i suoi parametri.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e  # un comando che fallisce si gestisce qui, non interrompe la sveglia

# Il comando di devloop watch dei sudoers dei ruoli (ops/host/sudoers-coordinator,
# ops/host/sudoers-supervisor): gli stessi argomenti, nello stesso ordine. Ogni giro aspetta al più
# WATCH_TIMEOUT secondi, poi rilegge la casella, il log e il servizio.
WATCH_INTERVAL=60
WATCH_TIMEOUT=300
LOG_LINES=200
NEEDS_OPERATOR=agent:needs-operator

usage() {
    say "Uso: bash $0 --role supervisor|coordinator [--params FILE] [--period SECONDI]"
    say
    say '  --role RUOLO      il ruolo che aspetta: supervisor o coordinator'
    say '  --period SECONDI  il controllo periodico (default: ROLE_WAIT_PERIOD, 3600; per il'
    say '                    coordinatore COORDINATOR_WAIT_PERIOD, se impostato)'
    say '  --params FILE     i parametri (default: params.env accanto a questa copia)'
}

ROLE=''
PERIOD=''
while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --role)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            ROLE=$2
            shift 2
            ;;
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

case $ROLE in
    supervisor) INBOX=to-supervisor ROLE_NAME=supervisore WATCH_STATE_NAME=watch-state.json ;;
    coordinator) INBOX=to-coordinator ROLE_NAME=coordinatore WATCH_STATE_NAME=coordinator-watch-state.json ;;
    *) printf '%s: --role vuole supervisor o coordinator\n' "$(basename "$0")" >&2; exit 2 ;;
esac

ops_load_params
if [ "$ROLE" = supervisor ]; then
    ROLE_USER=$SUPERVISOR_USER
    : "${PERIOD:=$ROLE_WAIT_PERIOD}"
else
    ROLE_USER=$COORDINATOR_USER
    : "${PERIOD:=$COORDINATOR_WAIT_PERIOD}"
fi
[[ $PERIOD =~ ^[1-9][0-9]{0,6}$ ]] || { say "--period vuole un numero di secondi: '$PERIOD'" >&2; exit 2; }
[ "$(id -un)" = "$ROLE_USER" ] || die "va eseguito come $ROLE_USER, il $ROLE_NAME"
cd / || exit 1

# Il battito (#602): prima di tutto, così anche un risveglio che poi si ferma su un errore dice
# che il ruolo è vivo. Atomico, da un file temporaneo che comincia con "." (il watchdog lo salta).
HEARTBEAT=$COORDINATION_DIR/$COORDINATION_HEARTBEAT/$ROLE
beat() {
    local tmp
    tmp=$(mktemp "$COORDINATION_DIR/$COORDINATION_HEARTBEAT/.tmp-XXXXXX" 2>/dev/null) || return 1
    if printf '%s %s\n' "$(date +%s)" "$PERIOD" >"$tmp" && chmod 664 "$tmp" \
        && mv -f "$tmp" "$HEARTBEAT"; then
        return 0
    fi
    rm -f "$tmp"
    return 1
}
beat || printf 'role-wait: non scrivo il battito %s: il watchdog dei ruoli vedrà il %s fermo (setup-supervisor.sh, setup-coordinator.sh)\n' \
    "$HEARTBEAT" "$ROLE_NAME" >&2

BOX=$COORDINATION_DIR/$INBOX
[ -d "$BOX" ] && [ -r "$BOX" ] && [ -x "$BOX" ] \
    || die "non leggo la casella $BOX: setup-$ROLE.sh, poi una sessione nuova del $ROLE_NAME (il gruppo $COORDINATION_GROUP)"
LOG=$STATE_PATH/run.log
WATCH_STATE=$STATE_PATH/$WATCH_STATE_NAME
MINE=${HOME:?}/.local/state/devloop-$ROLE-wait/${PROJECT:-default}
mkdir -p -m 700 "$MINE" || die "non creo $MINE"
SEEN=$MINE/run.log.seen
SERVICE_SEEN=$MINE/service

# L'issue degli allarmi ([run].alert_issue dell'overlay), se il ruolo la legge.
ALERT_ISSUE=$(python3 -I -B - "$STATE_PATH/config.local.toml" 2>/dev/null <<'EOF'
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    value = (tomllib.load(fh).get('run') or {}).get('alert_issue')
if isinstance(value, int) and not isinstance(value, bool) and value > 0:
    print(value)
EOF
)

# Una riga di devloop watch che sveglia il ruolo (docs/devloop/guide.md, "Waking the supervising
# agent"). devloop watch antepone l'ora ("HH:MM ") a ogni riga: i filtri la saltano. La riga
# della base non è un evento per nessuno.
role_event() {
    local line=$1
    [[ $line =~ ^[0-9]{2}:[0-9]{2}\ (.*)$ ]] && line=${BASH_REMATCH[1]}
    [ -n "$line" ] || return 1
    [[ $line == 'watch baseline:'* ]] && return 1
    [ "$ROLE" = supervisor ] && return 0
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

# Le righe aggiunte a run.log: quelle della finestra nuova (stdin) dopo la parte che continua la
# finestra già vista (SEEN), cioè il più lungo suffisso di SEEN che è anche un prefisso della
# nuova. Si confrontano sequenze, non testi: una riga uguale a una già vista, aggiunta dopo, è
# nuova. Senza sovrapposizione (il log ruotato o più di LOG_LINES righe nuove) è nuova tutta.
new_log_lines() {
    python3 -I -B -c '
import sys
with open(sys.argv[1], "rb") as fh:
    old = fh.read().split(b"\n")
new = sys.stdin.buffer.read().split(b"\n")
old, new = [line for line in old if line], [line for line in new if line]
k = min(len(old), len(new))
while k and old[len(old) - k:] != new[:k]:
    k -= 1
sys.stdout.buffer.write(b"".join(line + b"\n" for line in new[k:]))
' "$1"
}

TAIL_FAILED=0
# Le righe nuove di run.log con un errore, in EVENTS (solo il coordinatore: il tail è nel suo
# sudoers).
log_events() {
    local tail_out new line
    if tail_out=$(sudo -n -u "$DEVLOOP_USER" -- /usr/bin/tail -n "$LOG_LINES" "$LOG" 2>/dev/null); then
        # La prima volta le righe di run.log sono la base, non eventi.
        if [ -f "$SEEN" ]; then
            new=$(new_log_lines "$SEEN" <<<"$tail_out")
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
        printf 'role-wait: tail di %s come %s non riuscito: il log non si sorveglia\n' \
            "$LOG" "$DEVLOOP_USER" >&2
    fi
}

# In EVENTS le righe della casella, del log e del servizio, una per evento; vuoto se nulla.
local_events() {
    local found=() name names='' state last
    EVENTS=''
    while IFS= read -r -d '' name; do
        found+=("$name")
    done < <(find "$BOX" -mindepth 1 -maxdepth 1 -type f ! -name '.*' -printf '%f\0' 2>/dev/null \
                 | sort -z)
    if [ ${#found[@]} -gt 0 ]; then
        names=$(printf ' %s' "${found[@]}")
        EVENTS+="casella: ${#found[@]} messaggi in $BOX:$names"$'\n'
    fi
    [ "$ROLE" = coordinator ] && log_events
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
        say "periodo: ${PERIOD}s senza eventi del $ROLE_NAME: il controllo periodico"
        exit 0
    fi
    out=$("${WATCH[@]}")
    status=$?
    case $status in
        0)
            events=''
            while IFS= read -r line; do
                role_event "$line" && events+="evento: $line"$'\n'
            done <<<"$out"
            if [ -n "$events" ]; then
                printf '%s' "$events"
                exit 0
            fi
            ;;
        3) ;;  # nessuna novità entro WATCH_TIMEOUT
        *)
            say "errore: devloop watch come $DEVLOOP_USER è uscito con $status (sudoers del $ROLE_NAME, setup-$ROLE.sh; o devloop non parte)"
            exit 1
            ;;
    esac
done
