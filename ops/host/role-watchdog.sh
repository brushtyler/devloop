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
# Ogni ruolo è in uno di tre stati (#684; ADR 0002, precisazione del 2026-11-26):
# - attivo: battiti regolari, niente da fare;
# - a riposo: lo stato di riposo che il ruolo ha dichiarato (COORDINATION_DIR/rest/<ruolo>, che
#   role-wait.sh --rest scrive), la sentinella dei ruoli attiva (devloop-role-sentinel@<progetto>)
#   e il lock del ruolo tenuto (rest/<ruolo>.lock, con flock): un'informazione nel journal, non un
#   allarme, una volta per ingresso («a riposo dal …, coda vuota»). A riposo un ruolo non batte, e
#   il watchdog segnala solo le anomalie: la sentinella non attiva; il lock non tenuto (la sessione
#   è morta); il ruolo svegliato dalla sentinella (rest/<ruolo>.wake) che non toglie il suo riposo
#   entro REST_WAKE_TIMEOUT secondi dal segnale; il riposo dichiarato con del lavoro: la coda non
#   vuota (issue in coda, anche bloccate da prerequisiti, o in lavorazione, PR aperte) o un
#   controllo di prontezza o un'implementazione trattenuti, che devloop run scrive nel file hold
#   del flusso degli eventi, da più di REST_WAKE_TIMEOUT secondi, o un messaggio nella sua casella
#   da più di REST_MAILBOX_AGE secondi dal suo arrivo. Un ruolo a riposo con un'anomalia si
#   segnala come un ruolo fermo;
# - fermo: nessun riposo valido, e il battito manca o è più vecchio di due periodi (il periodo è
#   quello scritto nel battito, "<ora> <periodo>").
# Un ruolo fermo o in anomalia:
# - il supervisore, con il coordinatore attivo o a riposo: un messaggio in to-coordinator/;
# - il coordinatore, con il supervisore attivo o a riposo: un messaggio in to-supervisor/ e un
#   avviso all'operatore;
# - entrambi: un avviso all'operatore, e un messaggio in entrambe le caselle, per il primo
#   ruolo che riparte.
# Un messaggio a un ruolo a riposo lo sveglia, attraverso la sentinella.
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
REST=$COORDINATION_DIR/$COORDINATION_REST
HOLD=$STATE_PATH/$EVENTS_DIR/$EVENTS_HOLD
# Lo stato della sentinella, letto solo con un ruolo a riposo.
SENTINEL=''

# utc SECONDI: l'ora UTC di SECONDI dall'epoca.
utc() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }

# lock_held ROLE: il lock del riposo di ROLE è tenuto (da role-wait.sh --rest, in esclusiva): un
# lock condiviso non si ottiene. Il file si apre in sola lettura, senza crearlo.
lock_held() {
    local fd held=1
    # Fra graffe: la redirezione di stderr vale solo per l'apertura, non per il resto dello script.
    { exec {fd}<"$REST/$1.lock"; } 2>/dev/null || return 1
    flock -n -s "$fd" || held=0
    exec {fd}<&-
    [ "$held" = 0 ]
}

# rest ROLE: se ROLE ha dichiarato il riposo, in BEAT_STATE riposo o anomalia, in BEAT_KEY la chiave
# dello stato (per non ripetere gli avvisi: l'inizio del riposo e le anomalie), in BEAT_WHY il
# perché; torna 1 se ROLE non è a riposo.
rest() {
    local role=$1 start wake at why oldest age text queue='coda vuota' anomalies=()
    start=$(ops_rest_start "$role") || return 1
    if [ -z "$SENTINEL" ]; then
        SENTINEL=$(systemctl is-active "$SENTINEL_UNIT.service" 2>/dev/null)
        SENTINEL=${SENTINEL:-sconosciuto}
    fi
    if [ "$SENTINEL" != active ]; then
        anomalies+=("sentinella:la sentinella $SENTINEL_UNIT non è attiva ($SENTINEL): nessuno sveglia il ruolo")
    fi
    wake=$(ops_rest_wake "$role")
    if [ -n "$wake" ] && [ "${wake%% *}" -ge "$start" ]; then
        at=${wake%% *}
        why=${wake#* }
        if [ $((NOW - at)) -gt "$REST_WAKE_TIMEOUT" ]; then
            anomalies+=("svegliato:la sentinella l'ha svegliato alle $(utc "$at") ($why), ma dopo più di ${REST_WAKE_TIMEOUT}s non ha ripreso i battiti (REST_WAKE_TIMEOUT)")
        fi
    else
        lock_held "$role" \
            || anomalies+=("lock:il riposo è dichiarato, ma il lock $REST/$role.lock non è tenuto: la sessione del ruolo non è viva")
        # Il file del lavoro con un ruolo a riposo, che devloop run scrive con un controllo di
        # prontezza o un'implementazione trattenuti o con la coda non vuota (anche solo di issue
        # bloccate da prerequisiti, che nessuna azione avvia): "<prima volta> <ruoli> <lavoro>".
        if [ -f "$HOLD" ] && read -r at why text <"$HOLD" 2>/dev/null && [[ $at =~ ^[0-9]{1,12}$ ]]
        then
            queue="c'è lavoro dalle $(utc "$at") (${text:-lavoro}), la sentinella lo sveglia"
            [ $((NOW - at)) -gt "$REST_WAKE_TIMEOUT" ] \
                && anomalies+=("lavoro:c'è lavoro: ${text:-lavoro} dalle $(utc "$at"), da più di ${REST_WAKE_TIMEOUT}s (REST_WAKE_TIMEOUT)")
        fi
        oldest=$(find "$COORDINATION_DIR/to-$role" -mindepth 1 -maxdepth 1 -type f ! -name '.*' \
                     -printf '%T@\n' 2>/dev/null | sort -n | head -n 1)
        oldest=${oldest%%.*}
        if [ -n "$oldest" ]; then
            age=$((NOW - oldest))
            [ "$age" -gt "$REST_MAILBOX_AGE" ] \
                && anomalies+=("casella:un messaggio aspetta in to-$role/ da $((age / 60)) minuti, più di ${REST_MAILBOX_AGE}s (REST_MAILBOX_AGE)")
        fi
    fi
    if [ ${#anomalies[@]} -eq 0 ]; then
        BEAT_STATE=riposo
        BEAT_KEY="riposo@$start"
        BEAT_WHY="a riposo dal $(utc "$start"), $queue"
        return 0
    fi
    BEAT_STATE=anomalia
    BEAT_KEY="anomalia@$start"
    BEAT_WHY="a riposo dal $(utc "$start"), ma"
    for text in "${anomalies[@]}"; do
        BEAT_KEY="$BEAT_KEY:${text%%:*}"
        BEAT_WHY="$BEAT_WHY ${text#*:};"
    done
    BEAT_WHY=${BEAT_WHY%;}
}

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

# state ROLE: lo stato di ROLE, il riposo prima del battito: in BEAT_STATE attivo (ok), riposo,
# anomalia o fermo, in BEAT_KEY la chiave dello stato, in BEAT_WHY il perché; torna 1 se il
# battito non si legge.
state() {
    if rest "$1"; then
        return 0
    fi
    beat "$1" || return 1
    BEAT_KEY=$BEAT_STATE
}

state supervisor || exit 1
SUPERVISOR_STATE=$BEAT_STATE SUPERVISOR_KEY=$BEAT_KEY SUPERVISOR_WHY=$BEAT_WHY
state coordinator || exit 1
COORDINATOR_STATE=$BEAT_STATE COORDINATOR_KEY=$BEAT_KEY COORDINATOR_WHY=$BEAT_WHY
rm -f "$STATE/failed"

NEW="supervisor=$SUPERVISOR_KEY coordinator=$COORDINATOR_KEY"
OLD=$(cat "$STATE/last" 2>/dev/null || printf 'supervisor=ok coordinator=ok')
say "$WHO: supervisore $SUPERVISOR_STATE ($SUPERVISOR_WHY), coordinatore $COORDINATOR_STATE ($COORDINATOR_WHY)"
if [ "$NEW" = "$OLD" ]; then
    say "stato invariato: nessun avviso nuovo"
    exit 0
fi

# bad KEY: lo stato di chiave KEY è da segnalare (fermo, o a riposo con un'anomalia).
bad() { [[ $1 == fermo || $1 == anomalia@* ]]; }
# alarms STATO: la parte da segnalare di uno stato salvato ("supervisor=… coordinator=…"): la
# chiave di ogni ruolo fermo o in anomalia, "ok" per gli altri. Un ruolo che entra in riposo, o
# ne esce, non ripete l'avviso dell'altro.
alarms() {
    local part out='' role key
    for part in $1; do
        role=${part%%=*}
        key=${part#*=}
        bad "$key" || key=ok
        out="$out $role=$key"
    done
    printf '%s' "${out# }"
}

# L'informazione del riposo (#684): nel journal, non un allarme, una volta per ingresso.
for role in supervisor coordinator; do
    if [ "$role" = supervisor ]; then
        key=$SUPERVISOR_KEY why=$SUPERVISOR_WHY name=supervisore
    else
        key=$COORDINATOR_KEY why=$COORDINATOR_WHY name=coordinatore
    fi
    if [[ $key == riposo@* ]] && [[ " $OLD " != *" $role=$key "* ]]; then
        say "informazione: $WHO: il $name è $why"
    fi
done

# L'avviso all'operatore: `devloop notify` del progetto quando ci sarà (#593); finché non c'è,
# il journal (questa riga) e le caselle.
operator() {
    say "avviso all'operatore: $1 (devloop notify non c'è ancora, #593: solo il journal e le caselle)"
}

# describe RUOLO STATO PERCHÉ: il ruolo fermo o in anomalia, per gli avvisi.
describe() {
    if [ "$2" = fermo ]; then
        printf 'il %s è fermo (%s)' "$1" "$3"
    else
        printf 'il %s è %s' "$1" "$3"
    fi
}

NEW_ALARMS=$(alarms "$NEW")
if [ "$NEW_ALARMS" != "$(alarms "$OLD")" ]; then
    sup=0 coo=0
    bad "$SUPERVISOR_KEY" && sup=1
    bad "$COORDINATOR_KEY" && coo=1
    case $sup$coo in
        10)
            send to-coordinator "watchdog dei ruoli, $WHO: $(describe supervisore "$SUPERVISOR_STATE" "$SUPERVISOR_WHY"). Vedere il runbook, \"Stopped roles\"." \
                || exit 1
            ;;
        01)
            text="watchdog dei ruoli, $WHO: $(describe coordinatore "$COORDINATOR_STATE" "$COORDINATOR_WHY")."
            send to-supervisor "$text Vedere il runbook, \"Stopped roles\"." || exit 1
            operator "$text"
            ;;
        11)
            if [ "$SUPERVISOR_STATE $COORDINATOR_STATE" = 'fermo fermo' ]; then
                text="watchdog dei ruoli, $WHO: supervisore e coordinatore sono fermi (supervisore: $SUPERVISOR_WHY; coordinatore: $COORDINATOR_WHY)."
            else
                text="watchdog dei ruoli, $WHO: supervisore e coordinatore non sono attivi ($(describe supervisore "$SUPERVISOR_STATE" "$SUPERVISOR_WHY"); $(describe coordinatore "$COORDINATOR_STATE" "$COORDINATOR_WHY"))."
            fi
            operator "$text"
            send to-coordinator "$text" && send to-supervisor "$text" || exit 1
            ;;
        *)
            say "$WHO: supervisore e coordinatore di nuovo attivi"
            ;;
    esac
fi
printf '%s\n' "$NEW" >"$STATE/last.tmp" && mv -f "$STATE/last.tmp" "$STATE/last" \
    || { say "errore: non salvo lo stato in $STATE" >&2; exit 1; }
exit 0
