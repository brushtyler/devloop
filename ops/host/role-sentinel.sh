#!/usr/bin/env bash
# La sentinella dei ruoli a riposo di devloop (#684; ADR 0002, precisazione del 2026-11-26): il
# servizio devloop-role-sentinel@<progetto>, senza modello, senza chiavi GitHub e senza battiti
# per nessuno. Un ruolo che a fine turno non ha niente da fare entra in riposo (role-wait.sh
# --rest): scrive il suo stato di riposo COORDINATION_DIR/rest/<ruolo> e aspetta solo il segnale di
# questa sentinella. Mentre almeno un ruolo è a riposo, la sentinella guarda ciò che lo
# sveglierebbe e sveglia **solo il ruolo interessato** (scelta R3 (a)), scrivendo il segnale
# COORDINATION_DIR/rest/<ruolo>.wake, "<ora> <motivo>", una volta per riposo:
#
# - un messaggio nella sua casella (to-supervisor/ o to-coordinator/), senza leggerlo;
# - un evento del flusso degli eventi del progetto (#683) scritto dopo l'inizio del riposo, con il
#   filtro della sveglia del ruolo (ops_role_event di lib.sh): ogni evento per il supervisore, i
#   suoi per il coordinatore. L'issue degli allarmi del filtro è quella che devloop run scrive nel
#   flusso (ops_alert_issue): la sentinella non legge l'overlay. La sentinella ha un cursore suo; il
#   ruolo, svegliato, legge poi gli eventi dal flusso con il suo cursore (role-wait.sh);
# - solo per il coordinatore, una riga nuova di run.log con un errore (ops_log_error: un
#   Traceback, un allarme, un 401, il rifiuto dell'avvio), come la sua sveglia;
# - il servizio di devloop non attivo, a ogni cambio di stato, per entrambi i ruoli, come la sveglia;
# - il lavoro con un ruolo a riposo (#684: il file hold nella directory del flusso, che devloop
#   run scrive con i controlli di prontezza e le implementazioni che trattiene o con una coda non
#   vuota): sveglia ogni ruolo a riposo, e devloop run aspetta che lo stato di riposo sia tolto
#   prima di avviare un controllo di prontezza o un'implementazione;
# - il controllo giornaliero (scelta R2), all'ora fissa UTC REST_DAILY_CHECK (default 00:30, dopo il
#   riepilogo giornaliero, ma senza dipenderne): sveglia ogni ruolo il cui riposo è cominciato prima
#   dell'ultima occorrenza di quell'ora, il coordinatore per il controllo dei testi pubblicati e del
#   lavoro fermo, il supervisore per tornare a riposo se non ha niente da fare. Così l'attesa a
#   riposo di un ruolo dura al più un giorno.
#
# Durante la pausa dell'operatore (#685: COORDINATION_DIR/rest/pause, di root, ops_pause_start)
# si ferma tutto, e la sentinella sveglia un ruolo in attesa solo per un messaggio nella sua
# casella o per un suo allarme: per il coordinatore una riga nuova di run.log con un errore, e per
# entrambi il servizio di devloop che torna attivo durante la pausa (non lo ha riavviato il resume
# dell'operatore). Non per gli eventi del flusso, il servizio fermo (lo ha fermato l'operatore), il
# lavoro trattenuto né il controllo giornaliero. La fine della pausa la vede role-wait.sh da sé.
#
# Toglie il proprio segnale quando il ruolo non è più a riposo. A ogni giro, ogni POLL secondi, dà
# a systemd il segno di vita (systemd-notify WATCHDOG=1, WatchdogSec= dell'unità): una sentinella
# bloccata la riavvia systemd (Restart=on-failure), e il suo fallimento avvisa l'operatore con
# devloop notify (OnFailure=, devloop-role-sentinel-failed@<progetto>). Il watchdog dei ruoli
# (role-watchdog.sh) segnala la sentinella non attiva con un ruolo a riposo.
#
# Va eseguita come SENTINEL_USER, un utente di sistema del solo progetto, fuori dal gruppo dei
# ruoli, con le ACL di setup-supervisor.sh e setup-coordinator.sh (sentinel_acls ed
# ensure_state_reader di lib.sh): sola lettura del flusso, di run.log e delle caselle; la sola
# scrittura è il segnale di risveglio in rest/. Dalla copia di root WATCHDOG_OPS_DIR, con i
# parametri del progetto; il suo stato (i cursori del flusso e di run.log, l'ultimo stato del
# servizio) in STATE_DIRECTORY (StateDirectory= dell'unità).
#
#   role-sentinel.sh [--params FILE] [--once]
#
# --once fa un solo giro ed esce (le prove). Esce con 1 su un errore dell'installazione, con 2 su
# un'opzione sbagliata.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e  # un comando che fallisce si gestisce qui, non ferma la sentinella

# Ogni quanti secondi fa un giro: la casella, il flusso, run.log, il servizio, il lavoro
# trattenuto e il controllo giornaliero.
POLL=5
ROLES='supervisor coordinator'

usage() {
    say "Uso: bash $0 [--params FILE] [--once]"
    say
    say '  --params FILE  i parametri del progetto (default: params.env accanto a questa copia)'
    say '  --once         un solo giro, poi esce'
    say
    say "STATE_DIRECTORY: dove tiene il suo stato (systemd lo imposta)."
}

ONCE=0
while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --once) ONCE=1; shift ;;
        --params)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            PARAMS_FILE=$2
            shift 2
            ;;
        *) printf '%s: opzione sconosciuta: %s (vedi --help)\n' "$(basename "$0")" "$1" >&2; exit 2 ;;
    esac
done

ops_load_params
[ "$(id -un)" = "$SENTINEL_USER" ] || die "va eseguito come $SENTINEL_USER, la sentinella dei ruoli a riposo"
STATE=${STATE_DIRECTORY:-}
[ -n "$STATE" ] && [ -d "$STATE" ] && [ -w "$STATE" ] \
    || die "manca STATE_DIRECTORY, la directory del suo stato (StateDirectory= dell'unità $SENTINEL_UNIT)"
cd / || exit 1

REST_DIR=$COORDINATION_DIR/$COORDINATION_REST
STREAM=$STATE_PATH/$EVENTS_DIR
HOLD=$STREAM/$EVENTS_HOLD
LOG=$STATE_PATH/run.log
# L'issue degli allarmi, riletta a ogni giro (ops_alert_issue): quella che devloop run ha scritto nel
# flusso al suo avvio, senza leggere l'overlay.
ALERT_ISSUE=''

# notify ARGOMENTI: systemd-notify, quando systemd lo aspetta (NOTIFY_SOCKET, Type=notify).
notify() {
    [ -n "${NOTIFY_SOCKET:-}" ] || return 0
    systemd-notify "$@" 2>/dev/null
}

# Gli eventi del flusso dopo il cursore della sentinella (STATE/stream.cursor, "<id> <numero>
# <inode> <posizione>": l'identità del flusso, l'ultimo evento letto, e dove riprendere nel
# segmento corrente se non è ruotato), una riga ciascuno: "<ora in secondi> <HH:MM> <riga>". Come
# la sveglia dei ruoli (role-wait.sh, #689): il segmento corrente si legge da dove era rimasto
# solo se il file aperto è ancora quello del cursore e gli eventi continuano senza salti;
# altrimenti i due segmenti si leggono insieme e si controllano (gli inode dei file aperti contro
# quelli dei loro nomi, i numeri senza salti), e una rotazione che attraversa la lettura la fa
# ritentare. Il cursore non avanza mai oltre eventi ancora conservati che non ha dato; senza una
# lettura coerente non si tocca, e il giro dopo rilegge. Senza cursore la base alla fine, senza
# eventi; con il flusso sostituito, gli eventi conservati (il filtro sull'inizio del riposo scarta
# i vecchi). Solo righe intere. Esce con 2 se il flusso non si legge.
read_events() {
    python3 -I -B - "$STREAM" "$STATE/stream.cursor" <<'EOF'
import calendar, contextlib, os, re, sys, time
directory, cursor_path = sys.argv[1], sys.argv[2]
LINE = re.compile(rb'(\d+) (\d{4}-\d\d-\d\dT(\d\d:\d\d):\d\dZ) (.*)')
# Le letture dei due segmenti tentate in un giro, se una rotazione le attraversa.
ATTEMPTS = 5

def opened(name):
    """Il segmento NAME aperto in lettura, o None se manca."""
    try:
        return open(os.path.join(directory, name), 'rb')
    except FileNotFoundError:
        return None

def segment(fh, start=0):
    """(inode, posizione dopo l'ultima riga intera, righe intere) del segmento aperto FH dal
    byte START; (0, 0, b'') se manca."""
    if fh is None:
        return 0, 0, b''
    inode = os.fstat(fh.fileno()).st_ino
    fh.seek(start)
    data = fh.read()
    end = data.rfind(b'\n') + 1
    return inode, start + end, data[:end]

def path_inode(name):
    try:
        return os.stat(os.path.join(directory, name)).st_ino
    except FileNotFoundError:
        return 0

def parse(data):
    found = []
    for raw in data.split(b'\n')[:-1]:
        match = LINE.fullmatch(raw)
        if match:
            found.append((int(match.group(1)), match.group(2).decode(), match.group(3).decode(),
                          match.group(4).decode('utf-8', 'replace')))
    return found

def gapless(seq, found):
    """Gli eventi FOUND continuano SEQ senza salti."""
    for event in found:
        if event[0] != seq + 1:
            return False
        seq = event[0]
    return True

def read_kept():
    """Gli eventi conservati, il segmento precedente poi il corrente, letti insieme: (eventi,
    inode e posizione del corrente), o None se una rotazione ha attraversato la lettura (i
    segmenti aperti non sono più quelli dei loro nomi). I due restano aperti fino al controllo,
    così nessuno dei loro inode si riusa nel frattempo."""
    with contextlib.ExitStack() as stack:
        handles = [opened(name) for name in ('events.log.1', 'events.log')]
        for fh in handles:
            if fh is not None:
                stack.enter_context(fh)
        previous, current = (segment(fh) for fh in handles)
        if (path_inode('events.log.1') != previous[0]
                or path_inode('events.log') != current[0]):
            return None
    return parse(previous[2]) + parse(current[2]), current[0], current[1]

def read_coherent():
    """read_kept, ritentata anche quando gli eventi letti hanno un salto fra loro (devloop run
    non ne scrive: è una rotazione che la lettura non ha visto). Senza una lettura coerente esce
    senza toccare il cursore."""
    for _ in range(ATTEMPTS):
        read = read_kept()
        if read is not None and (not read[0] or gapless(read[0][0][0] - 1, read[0])):
            return read
    sys.exit(0)

try:
    with open(os.path.join(directory, 'id'), encoding='utf-8') as fh:
        identity = fh.read().strip()
except OSError:
    sys.exit(2)
if not re.fullmatch(r'[0-9a-f]{1,64}', identity):
    sys.exit(2)
cursor = None
try:
    with open(cursor_path, encoding='utf-8') as fh:
        parts = fh.read().split()
    if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
        cursor = (parts[0], int(parts[1]), int(parts[2]), int(parts[3]))
except OSError:
    pass

def save(seq, inode, offset):
    tmp = cursor_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(f'{identity} {seq} {inode} {offset}\n')
    os.replace(tmp, cursor_path)

try:
    if cursor is None:
        kept, inode, offset = read_coherent()
        save(kept[-1][0] if kept else 0, inode, offset)
        sys.exit(0)
    old_identity, seq, old_inode, old_offset = cursor
    found = None
    if old_identity == identity and old_inode:
        # Il segmento corrente non è ruotato dall'ultima lettura: si legge da dove era rimasta,
        # se il file aperto è quello del cursore e gli eventi nuovi continuano quelli già letti.
        fh = opened('events.log')
        if fh is not None:
            with fh:
                if (os.fstat(fh.fileno()).st_ino == old_inode
                        and os.fstat(fh.fileno()).st_size >= old_offset):
                    inode, offset, data = segment(fh, old_offset)
                    found = [event for event in parse(data) if event[0] > seq]
                    if not gapless(seq, found):
                        found = None  # un salto: si rilegge tutto
    if found is None:
        kept, inode, offset = read_coherent()
        if old_identity != identity or (kept and kept[-1][0] < seq):
            found = kept  # il flusso è stato sostituito
            seq = 0
        else:
            found = [event for event in kept if event[0] > seq]
        if found:
            seq = found[-1][0]
    elif found:
        seq = found[-1][0]
    save(seq, inode, offset)
except OSError:
    sys.exit(2)
for _, stamp, hhmm, text in found:
    at = calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ'))
    print(at, hhmm, text)
EOF
}

# Le righe aggiunte a run.log dopo il cursore della sentinella (STATE/log.cursor, "<inode>
# <posizione>"), solo intere; senza cursore la base alla fine. Un log ruotato si legge da capo.
read_log() {
    python3 -I -B - "$LOG" "$STATE/log.cursor" <<'EOF'
import os, sys
path, cursor_path = sys.argv[1], sys.argv[2]
try:
    with open(cursor_path, encoding='utf-8') as fh:
        old = [int(part) for part in fh.read().split()]
except (OSError, ValueError):
    old = None
try:
    fh = open(path, 'rb')
except OSError:
    sys.exit(2)
with fh:
    inode, size = os.fstat(fh.fileno()).st_ino, os.fstat(fh.fileno()).st_size
    if old is None or len(old) != 2:
        start, data = size, b''
    else:
        start = old[1] if old[0] == inode and old[1] <= size else 0
        # Al più l'ultimo MiB: le righe di un log che cresce in fretta non fermano il giro.
        start = max(start, size - (1 << 20))
        fh.seek(start)
        data = fh.read(size - start)
end = data.rfind(b'\n') + 1
tmp = cursor_path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as out:
    out.write(f'{inode} {start + end}\n')
os.replace(tmp, cursor_path)
sys.stdout.buffer.write(data[:end])
EOF
}

# L'ultima occorrenza dell'ora del controllo giornaliero (REST_DAILY_CHECK, UTC) non dopo NOW.
daily_check() {
    local now=$1 at
    at=$(date -u -d "$(date -u -d "@$now" +%F) $REST_DAILY_CHECK" +%s) || return 1
    [ "$at" -le "$now" ] || at=$((at - 86400))
    printf '%s\n' "$at"
}

# wake ROLE MOTIVO: il segnale di risveglio di ROLE, atomico, del proprietario la sentinella.
wake() {
    local role=$1 why=$2 tmp
    tmp=$(mktemp "$REST_DIR/.tmp-XXXXXX" 2>/dev/null) \
        || { say "errore: non scrivo in $REST_DIR (ACL di $SENTINEL_USER: setup-supervisor.sh, setup-coordinator.sh)" >&2; return 1; }
    if printf '%s %s\n' "$NOW" "$why" >"$tmp" && chmod 644 "$tmp" \
        && mv -f "$tmp" "$REST_DIR/$role.wake"; then
        say "sveglio il $role: $why"
        return 0
    fi
    rm -f "$tmp"
    say "errore: non scrivo il segnale $REST_DIR/$role.wake" >&2
    return 1
}

STREAM_FAILED=0
LOG_FAILED=0
# Un giro: legge ciò che è nuovo, e sveglia i ruoli a riposo che lo riguarda.
round() {
    local role start resting='' events='' log='' service last why wake_at at hhmm text line
    local box count daily pause
    NOW=$(date +%s)
    pause=$(ops_pause_start) || pause=''
    ALERT_ISSUE=$(ops_alert_issue)
    # I cursori del flusso e di run.log avanzano a ogni giro: nuovo è ciò che è arrivato dal giro
    # prima.
    events=$(read_events)
    if [ $? -ne 0 ]; then
        [ "$STREAM_FAILED" = 1 ] \
            || say "avviso: non leggo il flusso degli eventi $STREAM (ACL di setup-supervisor.sh o setup-coordinator.sh)" >&2
        STREAM_FAILED=1
        events=''
    else
        STREAM_FAILED=0
    fi
    log=$(read_log)
    if [ $? -ne 0 ]; then
        [ "$LOG_FAILED" = 1 ] || say "avviso: non leggo $LOG" >&2
        LOG_FAILED=1
        log=''
    else
        LOG_FAILED=0
    fi
    service=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)
    service=${service:-sconosciuto}
    last=$(cat "$STATE/service" 2>/dev/null || printf active)
    [ "$service" = "$last" ] || printf '%s\n' "$service" >"$STATE/service"
    daily=$(daily_check "$NOW")
    for role in $ROLES; do
        if ! start=$(ops_rest_start "$role"); then
            # Il ruolo non è a riposo: il segnale di prima, se c'è, non serve più.
            [ -e "$REST_DIR/$role.wake" ] && rm -f "$REST_DIR/$role.wake" 2>/dev/null
            continue
        fi
        resting="$resting $role"
        # Una volta per riposo: un segnale scritto dopo l'inizio c'è già.
        wake_at=$(ops_rest_wake "$role")
        [ -n "$wake_at" ] && [ "${wake_at%% *}" -ge "$start" ] && continue
        why=''
        box=$COORDINATION_DIR/to-$role
        count=$(find "$box" -mindepth 1 -maxdepth 1 -type f ! -name '.*' 2>/dev/null | wc -l)
        [ "$count" -gt 0 ] && why="casella: $count messaggi in $box"
        if [ -n "$pause" ]; then
            # La pausa dell'operatore (#685): solo la casella e gli allarmi.
            if [ -z "$why" ] && [ "$role" = coordinator ] && [ -n "$log" ]; then
                while IFS= read -r line; do
                    [ -n "$line" ] && ops_log_error "$line" && { why="log: ${line:0:300}"; break; }
                done <<<"$log"
            fi
            if [ -z "$why" ] && [ "$service" = active ] && [ "$service" != "$last" ]; then
                why="allarme: $SERVICE_NAME è di nuovo attivo durante la pausa dell'operatore, cominciata alle $(date -u -d "@$pause" +%Y-%m-%dT%H:%M:%SZ): non l'ha riavviato il resume dell'operatore"
            fi
            [ -n "$why" ] && wake "$role" "$why"
            continue
        fi
        if [ -z "$why" ] && [ -n "$events" ]; then
            while read -r at hhmm text; do
                [ "$at" -ge $((start - 1)) ] || continue
                if ops_role_event "$role" "$text"; then
                    why="evento: $hhmm $text"
                    break
                fi
            done <<<"$events"
        fi
        if [ -z "$why" ] && [ "$role" = coordinator ] && [ -n "$log" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] && ops_log_error "$line" && { why="log: ${line:0:300}"; break; }
            done <<<"$log"
        fi
        if [ -z "$why" ] && [ "$service" != active ] && [ "$service" != "$last" ]; then
            why="servizio: $SERVICE_NAME è $service"
        fi
        if [ -z "$why" ] && [ -f "$HOLD" ]; then
            text=$(head -c 300 "$HOLD" 2>/dev/null | head -n 1)
            why="lavoro: c'è lavoro con un ruolo a riposo, e devloop run non avvia un controllo di prontezza né un'implementazione finché i ruoli non escono dal riposo (${text:-lavoro})"
        fi
        if [ -z "$why" ] && [ -n "$daily" ] && [ "$start" -lt "$daily" ]; then
            why="controllo giornaliero delle $REST_DAILY_CHECK UTC"
        fi
        [ -n "$why" ] && wake "$role" "$why"
    done
}

notify --ready
say "sentinella dei ruoli a riposo del progetto $OPS_INSTANCE ($REPO): un giro ogni ${POLL}s"
while :; do
    notify WATCHDOG=1
    round
    [ "$ONCE" = 1 ] && exit 0
    sleep "$POLL"
done
