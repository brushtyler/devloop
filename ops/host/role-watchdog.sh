#!/usr/bin/env bash
# Il watchdog dei ruoli di devloop (#602; ADR 0002, seconda precisazione del 2026-11-23, punto
# 6): controlla i battiti che role-wait.sh di supervisore e coordinatore scrive in
# COORDINATION_DIR/heartbeat/ e, quando un ruolo è fermo, lo dice all'altro nella casella e
# all'operatore. Rileva e non agisce: non ha chiavi GitHub, non avvia né ferma nulla.
#
# Lo esegue il timer devloop-role-watchdog@<progetto>.timer, ogni 5 minuti, con l'unità
# devloop-role-watchdog@<progetto>.service: come WATCHDOG_USER, un utente di sistema del solo
# progetto, che non è nel gruppo dei ruoli ma ha, con le ACL che mettono setup-supervisor.sh e
# setup-coordinator.sh, solo l'attraversamento di COORDINATION_DIR e di heartbeat/ e la scrittura
# in to-supervisor/ e to-coordinator/ della casella del suo progetto. Dalla copia di root in
# WATCHDOG_OPS_DIR, con i parametri del progetto copiati lì (params.env).
#
# Un ruolo è fermo se il suo battito manca o è più vecchio di due periodi (il periodo è quello
# scritto nel battito, "<ora> <periodo>"):
# - supervisore fermo, coordinatore no: un messaggio in to-coordinator/;
# - coordinatore fermo, supervisore no: un messaggio in to-supervisor/ e un avviso all'operatore;
# - entrambi fermi: un avviso all'operatore, e un messaggio in entrambe le caselle, per il primo
#   ruolo che riparte.
# Ogni avviso nomina il progetto. Lo stesso avviso non si ripete finché lo stato non cambia: lo
# stato dell'ultimo controllo sta in STATE_DIRECTORY (StateDirectory= dell'unità). Gli avvisi
# all'operatore passeranno da `devloop notify` del progetto (#593); finché non c'è, vanno nelle
# caselle e nel journal di systemd (le righe "avviso all'operatore:").
#
# Con DEVLOOP_WATCHDOG_MODE=failed (l'unità devloop-role-watchdog-failed@<progetto>.service,
# l'OnFailure= dell'unità del controllo) scrive invece in entrambe le caselle che il watchdog è
# fallito, una volta finché un controllo non riesce di nuovo.
#
# Esce con 0 dopo un controllo, anche con un ruolo fermo; con 1 se non legge un battito o non
# scrive un messaggio (l'unità fallisce, e il suo OnFailure= lo segnala); con 2 su un'opzione
# sbagliata.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e

usage() {
    say "Uso: bash $0 [--params FILE]"
    say
    say '  --params FILE  i parametri del progetto (default: params.env accanto a questa copia)'
    say
    say 'DEVLOOP_WATCHDOG_MODE=failed: segnala nelle caselle che il watchdog è fallito.'
    say "STATE_DIRECTORY: dove tiene lo stato dell'ultimo controllo (systemd lo imposta)."
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --params)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            PARAMS_FILE=$2
            shift 2
            ;;
        *) printf '%s: opzione sconosciuta: %s (vedi --help)\n' "$(basename "$0")" "$1" >&2; exit 2 ;;
    esac
done
MODE=${DEVLOOP_WATCHDOG_MODE:-check}
case $MODE in check | failed) ;; *) die "DEVLOOP_WATCHDOG_MODE vuole check o failed: '$MODE'" ;; esac

ops_load_params
[ "$(id -un)" = "$WATCHDOG_USER" ] || die "va eseguito come $WATCHDOG_USER, il watchdog dei ruoli"
STATE=${STATE_DIRECTORY:-}
[ -n "$STATE" ] && [ -d "$STATE" ] && [ -w "$STATE" ] \
    || die "manca STATE_DIRECTORY, la directory del suo stato (StateDirectory= dell'unità $WATCHDOG_UNIT)"
cd / || exit 1

if [ -n "$PROJECT" ]; then
    WHO="progetto $PROJECT ($REPO)"
else
    WHO="progetto $REPO"
fi
BEATS=$COORDINATION_DIR/$COORDINATION_HEARTBEAT

# send BOX TESTO: un messaggio in COORDINATION_DIR/BOX, come lo scrivono i ruoli (i runbook,
# "Channel"): un file temporaneo che comincia con ".", poi una rinomina atomica.
send() {
    local dir=$COORDINATION_DIR/$1 text=$2 tmp
    tmp=$(mktemp "$dir/.tmp-XXXXXX" 2>/dev/null) \
        || { say "errore: non scrivo in $dir (ACL di $WATCHDOG_USER: setup-supervisor.sh, setup-coordinator.sh)" >&2; return 1; }
    if chmod 640 "$tmp" && printf '%s\n' "$text" >"$tmp" \
        && mv "$tmp" "$dir/$(date -u +%Y%m%dT%H%M%SZ)-watchdog-${tmp##*.tmp-}.txt"; then
        say "messaggio in $1/: $text"
        return 0
    fi
    rm -f "$tmp"
    say "errore: non scrivo in $dir" >&2
    return 1
}

# Il fallimento del watchdog (OnFailure=): una volta in entrambe le caselle.
if [ "$MODE" = failed ]; then
    if [ -e "$STATE/failed" ]; then
        say "watchdog fallito, già segnalato: nessun messaggio nuovo"
        exit 0
    fi
    text="watchdog dei ruoli, $WHO: il controllo dei battiti è fallito, i ruoli fermi non si segnalano. Vedere journalctl -u $WATCHDOG_UNIT.service e check-host.sh."
    send to-coordinator "$text" && send to-supervisor "$text" || exit 1
    : >"$STATE/failed"
    exit 0
fi

NOW=$(date +%s)
# beat ROLE: in BEAT_STATE ok o fermo, in BEAT_WHY il perché; torna 1 se il battito non si legge.
beat() {
    local file=$BEATS/$1 at='' period='' age
    BEAT_STATE=fermo
    if ! [ -e "$file" ]; then
        BEAT_WHY="nessun battito in $file"
        return 0
    fi
    if ! [ -f "$file" ] || ! read -r at period <"$file"; then
        say "errore: non leggo il battito $file (deve essere un file 664: role-wait.sh lo scrive così)" >&2
        return 1
    fi
    if ! [[ $at =~ ^[0-9]{1,12}$ && $period =~ ^[1-9][0-9]{0,6}$ ]]; then
        BEAT_WHY="battito illeggibile in $file: '$at $period'"
        return 0
    fi
    age=$((NOW - at))
    if [ "$age" -gt $((2 * period)) ]; then
        BEAT_WHY="ultimo battito $(date -u -d "@$at" +%Y-%m-%dT%H:%M:%SZ), $((age / 60)) minuti fa, più di due periodi di ${period}s"
        return 0
    fi
    BEAT_STATE=ok
    BEAT_WHY="battito di $((age / 60)) minuti fa"
}

beat supervisor || exit 1
SUPERVISOR_STATE=$BEAT_STATE SUPERVISOR_WHY=$BEAT_WHY
beat coordinator || exit 1
COORDINATOR_STATE=$BEAT_STATE COORDINATOR_WHY=$BEAT_WHY
rm -f "$STATE/failed"

NEW="supervisor=$SUPERVISOR_STATE coordinator=$COORDINATOR_STATE"
OLD=$(cat "$STATE/last" 2>/dev/null || printf 'supervisor=ok coordinator=ok')
say "$WHO: supervisore $SUPERVISOR_STATE ($SUPERVISOR_WHY), coordinatore $COORDINATOR_STATE ($COORDINATOR_WHY)"
if [ "$NEW" = "$OLD" ]; then
    say "stato invariato: nessun avviso nuovo"
    exit 0
fi

# L'avviso all'operatore: `devloop notify` del progetto quando ci sarà (#593); finché non c'è,
# il journal (questa riga) e le caselle.
operator() {
    say "avviso all'operatore: $1 (devloop notify non c'è ancora, #593: solo il journal e le caselle)"
}

case $NEW in
    'supervisor=fermo coordinator=ok')
        send to-coordinator "watchdog dei ruoli, $WHO: il supervisore è fermo ($SUPERVISOR_WHY). Vedere il runbook, \"Stopped roles\"." \
            || exit 1
        ;;
    'supervisor=ok coordinator=fermo')
        text="watchdog dei ruoli, $WHO: il coordinatore è fermo ($COORDINATOR_WHY)."
        send to-supervisor "$text Vedere il runbook, \"Stopped roles\"." || exit 1
        operator "$text"
        ;;
    'supervisor=fermo coordinator=fermo')
        text="watchdog dei ruoli, $WHO: supervisore e coordinatore sono fermi (supervisore: $SUPERVISOR_WHY; coordinatore: $COORDINATOR_WHY)."
        operator "$text"
        send to-coordinator "$text" && send to-supervisor "$text" || exit 1
        ;;
    *)
        say "$WHO: supervisore e coordinatore di nuovo attivi"
        ;;
esac
printf '%s\n' "$NEW" >"$STATE/last.tmp" && mv -f "$STATE/last.tmp" "$STATE/last" \
    || { say "errore: non salvo lo stato in $STATE" >&2; exit 1; }
exit 0
