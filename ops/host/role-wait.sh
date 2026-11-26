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
# - un messaggio nella casella del ruolo (to-supervisor/ o to-coordinator/ di COORDINATION_DIR),
#   letta ogni secondo: lo segnala senza consumarlo; il ruolo lo legge, poi lo sposta in done/ o
#   lo rimuove;
# - un evento del flusso degli eventi del progetto (#683): la directory events/ dello state_dir,
#   che devloop run scrive a ogni lettura di GitHub, con le righe di devloop watch, e che il
#   ruolo legge come sé stesso, con l'ACL di sola lettura di setup-supervisor.sh o
#   setup-coordinator.sh, ogni STREAM_POLL secondi. Il cursore, il numero dell'ultimo evento
#   letto, è nello stato del ruolo, uno per progetto: un ruolo non consuma gli eventi dell'altro,
#   né un progetto quelli di un altro progetto dello stesso utente. Il supervisore si sveglia su
#   ogni evento. Il coordinatore solo sui suoi: la label agent:needs-operator aggiunta, la label
#   agent:needs-coordinator aggiunta (un'issue che aspetta il suo /devloop authorize, #607), "il
#   merge spetta all'operatore", un commento sull'issue degli allarmi ([run].alert_issue
#   dell'overlay), una risposta dell'operatore ("operator reply", #676: un suo commento di
#   persona la cui prima riga, dopo l'intestazione, è un comando /devloop, o su un'issue o una
#   PR in agent:needs-operator; lo distingue devloop run, che conosce le label), tre letture di
#   GitHub fallite di fila ("watch error:"). Non sugli altri commenti dell'operatore ("operator
#   comment"); nessuno dei due ruoli, poi, sulla routine (#682), che devloop run riconosce per
#   l'autore che verifica, mai per il testo: l'archivio del riepilogo giornaliero ("report by",
#   il record report di devloop report scritto dall'identità del dispatcher; l'aggiornamento del
#   corpo dell'issue del riepilogo non è un evento), i commenti del ruolo stesso ("supervisor
#   comment" o "coordinator comment") e il diario dell'altro ruolo, cioè un suo commento
#   sull'issue degli allarmi, l'issue del diario (un diario tenuto su un'altra issue sveglia il
#   supervisore come prima). Un ruolo è riconosciuto solo con la sua app: il coordinatore con
#   [roles.coordinator], il supervisore con [roles.supervisor] come tabella; senza, il
#   supervisore firma con l'app del suo agent e i suoi commenti restano "comment", che svegliano
#   come prima. Gli altri commenti del dispatcher sull'issue degli allarmi (i suoi avvisi)
#   svegliano il coordinatore;
# - il flusso che non si può seguire senza salti: sostituito (un'altra identità, il file id), o
#   con eventi dopo il cursore non più conservati (la rotazione a 10 MiB ne tiene due segmenti).
#   La riga lo dice e la sveglia riprende dal primo evento ancora conservato: nessun evento si
#   perde in silenzio. Senza cursore (il primo avvio per il progetto) la sveglia crea la base
#   alla fine del flusso e lo dice in una riga, senza svegliarsi;
# - solo per il coordinatore, una riga nuova di run.log (tail -n 200 come utente devloop, dal suo
#   sudoers) con un Traceback, un allarme (ALERT:), un 401 o "Bad credentials", o il rifiuto
#   dell'avvio ("devloop: ...", l'uscita del comando);
# - solo per il coordinatore, il flusso fermo: con il servizio attivo, il segno di vita del
#   flusso (events/alive, l'ora dell'ultima lettura di GitHub riuscita) più vecchio di
#   EVENTS_STALE_AFTER secondi (300), contati dal più recente fra il segno di vita e l'avvio del
#   servizio, oppure assente o illeggibile. Una volta per episodio: l'avviso si riarma quando il
#   segno di vita torna recente;
# - il servizio non attivo (systemctl is-active), una volta per ogni cambio di stato;
# - la fine del periodo (--period; per default COORDINATOR_WAIT_PERIOD per il coordinatore e
#   ROLE_WAIT_PERIOD per il supervisore, un'ora), senza eventi: il controllo periodico.
#
# Ogni riga dice perché si è svegliata: "casella:", "evento:", "flusso:", "log:", "eventi
# fermi:", "servizio:", "periodo:". Esce con 2 su un'opzione sbagliata. Non esegue nulla come
# utente devloop, fuori dal tail di run.log del coordinatore, e non scrive nulla fuori dal battito
# e dal suo stato, in ~/.local/state/devloop-<ruolo>-wait/<progetto>/ dell'utente del ruolo
# (_default senza PROJECT): il cursore del flusso, le righe di run.log già viste, l'ultimo stato
# del servizio e l'episodio del flusso fermo. Va eseguito come l'utente del ruolo, dalla copia di
# root che setup-supervisor.sh (SUPERVISOR_OPS_DIR) o setup-coordinator.sh (COORDINATOR_OPS_DIR)
# installa, con i suoi parametri.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"
set +e  # un comando che fallisce si gestisce qui, non interrompe la sveglia

LOG_LINES=200
NEEDS_OPERATOR=agent:needs-operator
NEEDS_COORDINATOR=agent:needs-coordinator
# Ogni quanti secondi si leggono la casella (POLL), il flusso (STREAM_POLL) e il log, il servizio
# e il segno di vita del flusso (LOCAL_POLL).
POLL=1
STREAM_POLL=5
LOCAL_POLL=60

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
    supervisor) INBOX=to-supervisor ROLE_NAME=supervisore OTHER_ROLE=coordinator ;;
    coordinator) INBOX=to-coordinator ROLE_NAME=coordinatore OTHER_ROLE=supervisor ;;
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
# Il flusso degli eventi del progetto (#683), che devloop run scrive e il ruolo legge.
STREAM=$STATE_PATH/$EVENTS_DIR
# Lo stato per progetto: OPS_INSTANCE, il progetto o _default senza PROJECT, così il ruolo di più
# progetti (o un progetto "default") non mescola il suo stato, né il cursore del flusso, fra
# installazioni.
MINE=${HOME:?}/.local/state/devloop-$ROLE-wait/$OPS_INSTANCE
mkdir -p -m 700 "$MINE" || die "non creo $MINE"
SEEN=$MINE/run.log.seen
SERVICE_SEEN=$MINE/service
CURSOR=$MINE/events.cursor
STALE=$MINE/events.stale

# L'issue degli allarmi ([run].alert_issue dell'overlay), se il ruolo la legge.
ALERT_ISSUE=$(python3 -I -B - "$STATE_PATH/config.local.toml" 2>/dev/null <<'EOF'
import sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    value = (tomllib.load(fh).get('run') or {}).get('alert_issue')
if isinstance(value, int) and not isinstance(value, bool) and value > 0:
    print(value)
EOF
)

# Una riga del flusso che sveglia il ruolo (docs/devloop/guide.md, "Waking the supervising
# agent"), con l'ora ("HH:MM ") davanti, come la stampa devloop watch: i filtri la saltano. La
# riga della base di devloop watch non è un evento per nessuno.
role_event() {
    local line=$1
    [[ $line =~ ^[0-9]{2}:[0-9]{2}\ (.*)$ ]] && line=${BASH_REMATCH[1]}
    [ -n "$line" ] || return 1
    [[ $line == 'watch baseline:'* ]] && return 1
    # La routine (#682), riconosciuta da devloop run per l'autore, mai per il testo: l'archivio
    # del riepilogo giornaliero ("report by"), i commenti del ruolo stesso, il diario dell'altro
    # ruolo (un suo commento sull'issue degli allarmi).
    [[ $line =~ ^(PR|issue)\ \#[0-9]+\ (report|$ROLE\ comment)\ by\  ]] && return 1
    [ -n "$ALERT_ISSUE" ] && [[ $line =~ ^issue\ \#$ALERT_ISSUE\ $OTHER_ROLE\ comment\ by\  ]] \
        && return 1
    [ "$ROLE" = supervisor ] && return 0
    # Una delle due label aggiunta, o fra quelle di un'issue o di una PR nuova ("new PR #N [a,b]
    # titolo").
    [[ $line == *" +$NEEDS_OPERATOR"* || $line == *" +$NEEDS_COORDINATOR"* ]] && return 0
    if [[ $line =~ ^new\ (PR|issue)\ \#[0-9]+\ \[([^]]*)\] ]]; then
        case ",${BASH_REMATCH[2]}," in
            *",$NEEDS_OPERATOR,"* | *",$NEEDS_COORDINATOR,"*) return 0 ;;
        esac
    fi
    [[ ${line,,} == *"merge spetta all'operatore"* ]] && return 0
    [[ $line == 'watch error:'* ]] && return 0
    # Una risposta dell'operatore (#676): devloop run la distingue da un commento qualunque.
    [[ $line =~ ^(PR|issue)\ \#[0-9]+\ operator\ reply\ by\  ]] && return 0
    [ -n "$ALERT_ISSUE" ] && [[ $line =~ ^issue\ \#$ALERT_ISSUE\ (operator\ )?comment\ by\  ]] \
        && return 0
    return 1
}

# Il contenuto atomico di FILE: TESTO.
save() {
    local file=$1 text=$2
    printf '%s' "$text" >"$file.tmp" && mv -f "$file.tmp" "$file"
}

# Gli eventi del flusso dopo il cursore (#683), una riga ciascuno: "E HH:MM <riga>". Prima delle
# righe, il cursore salvato, atomico, dopo l'ultimo evento letto (anche quelli che non svegliano
# il ruolo: il filtro è qui, non nel flusso). "N <testo>" è la base del primo avvio, che non
# sveglia; "W <testo>" un salto del flusso (sostituito, o eventi non più conservati), che
# sveglia. Il cursore è "<id> <numero> <inode> <posizione>": l'identità del flusso, l'ultimo
# evento letto, e dove riprendere nel segmento corrente se non è ruotato. Si leggono solo le
# righe intere (con il ritorno a capo): una riga che devloop sta scrivendo si legge al giro dopo.
# Esce con 2 se il flusso non si legge (manca, o manca l'ACL).
read_stream() {
    python3 -I -B - "$STREAM" "$CURSOR" <<'EOF'
import os, re, sys
directory, cursor_path = sys.argv[1], sys.argv[2]
LINE = re.compile(rb'(\d+) \d{4}-\d\d-\d\dT(\d\d:\d\d):\d\dZ (.*)')

def segment(name, start=0):
    """(inode, posizione dopo l'ultima riga intera, righe intere) di un segmento dal byte start."""
    try:
        with open(os.path.join(directory, name), 'rb') as fh:
            inode = os.fstat(fh.fileno()).st_ino
            fh.seek(start)
            data = fh.read()
    except FileNotFoundError:
        return 0, 0, b''
    end = data.rfind(b'\n') + 1
    return inode, start + end, data[:end]

def parse(data):
    found = []
    for raw in data.split(b'\n')[:-1]:
        match = LINE.fullmatch(raw)
        if match:
            found.append((int(match.group(1)), match.group(2).decode(),
                          match.group(3).decode('utf-8', 'replace')))
    return found

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

out = []
try:
    if cursor is None:
        inode, offset, data = segment('events.log')
        found = parse(data) or parse(segment('events.log.1')[2])
        last = found[-1][0] if found else 0
        save(last, inode, offset)
        print(f'N base alla fine del flusso {directory}, dopo l\'evento {last}: gli eventi già '
              'scritti non svegliano')
        sys.exit(0)
    old_identity, seq, old_inode, old_offset = cursor
    found = None
    if old_identity == identity and old_inode:
        # Il segmento corrente non è ruotato dall'ultima lettura: si legge da dove era rimasta.
        try:
            current = os.stat(os.path.join(directory, 'events.log'))
        except FileNotFoundError:
            current = None
        if (current is not None and current.st_ino == old_inode
                and current.st_size >= old_offset):
            inode, offset, data = segment('events.log', old_offset)
            found = [event for event in parse(data) if event[0] > seq]
            if inode != old_inode or (found and found[0][0] != seq + 1):
                found = None  # ruotato nel frattempo, o un salto: si rilegge tutto
    if found is None:
        kept = parse(segment('events.log.1')[2])
        inode, offset, data = segment('events.log')
        kept += parse(data)
        if old_identity != identity or (kept and kept[-1][0] < seq):
            found = kept
            first = found[0][0] if found else None
            out.append('W il flusso degli eventi è stato sostituito: riprendo dal primo evento '
                       'conservato' + (f' ({first})' if first else ', nessuno per ora'))
            seq = 0
        else:
            found = [event for event in kept if event[0] > seq]
            if found and found[0][0] > seq + 1:
                out.append(f'W gli eventi da {seq + 1} a {found[0][0] - 1} non sono più '
                           f'conservati (rotazione del flusso): riprendo dal primo conservato '
                           f'({found[0][0]})')
    save(found[-1][0] if found else seq, inode, offset)
except OSError:
    sys.exit(2)
out += [f'E {at} {text}' for _, at, text in found]
if out:
    print('\n'.join(out))
EOF
}

STREAM_FAILED=0
# In EVENTS le righe del flusso che svegliano il ruolo; la base del primo avvio la stampa subito.
stream_events() {
    local out line
    out=$(read_stream)
    if [ $? -ne 0 ]; then
        if [ "$STREAM_FAILED" = 0 ]; then
            STREAM_FAILED=1
            printf 'role-wait: non leggo il flusso degli eventi %s: devloop run non l'"'"'ha ancora scritto, o manca l'"'"'ACL di sola lettura (setup-%s.sh)\n' \
                "$STREAM" "$ROLE" >&2
        fi
        return
    fi
    STREAM_FAILED=0
    while IFS= read -r line; do
        case $line in
            'N '*) say "flusso: ${line#N }" ;;
            'W '*) EVENTS+="flusso: ${line#W }"$'\n' ;;
            'E '*) role_event "${line#E }" && EVENTS+="evento: ${line#E }"$'\n' ;;
        esac
    done <<<"$out"
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

# In EVENTS, aggiunta, la riga dei messaggi della casella, se ce ne sono.
inbox_events() {
    local found=() name names=''
    while IFS= read -r -d '' name; do
        found+=("$name")
    done < <(find "$BOX" -mindepth 1 -maxdepth 1 -type f ! -name '.*' -printf '%f\0' 2>/dev/null \
                 | sort -z)
    if [ ${#found[@]} -gt 0 ]; then
        names=$(printf ' %s' "${found[@]}")
        EVENTS+="casella: ${#found[@]} messaggi in $BOX:$names"$'\n'
    fi
}

# Il flusso fermo (#683), solo per il coordinatore, con il servizio attivo: in EVENTS la riga
# "eventi fermi:" una volta per episodio (STALE), e l'episodio chiuso quando il segno di vita
# torna recente. L'età si conta dal più recente fra il segno di vita e l'avvio del servizio,
# così un servizio appena riavviato ha il tempo della prima lettura.
stale_events() {
    local now alive since ref reason=''
    now=$(date +%s)
    alive=$(head -c 32 "$STREAM/$EVENTS_ALIVE" 2>/dev/null | tr -d '[:space:]')
    [[ $alive =~ ^[0-9]{1,12}$ ]] || alive=''
    since=$(systemctl show --property=ActiveEnterTimestamp --value "$SERVICE_NAME" 2>/dev/null)
    if [ -n "$since" ] && [ "$since" != n/a ]; then
        since=$(date -d "$since" +%s 2>/dev/null) || since=''
    else
        since=''
    fi
    ref=$alive
    [ -n "$since" ] && { [ -z "$ref" ] || [ "$since" -gt "$ref" ]; } && ref=$since
    if [ -z "$alive" ] && { [ -z "$ref" ] || [ $((now - ref)) -gt "$EVENTS_STALE_AFTER" ]; }; then
        reason="il segno di vita $STREAM/$EVENTS_ALIVE è assente o illeggibile (devloop run non lo scrive, o manca l'ACL di setup-coordinator.sh)"
    elif [ -n "$alive" ] && [ $((now - ref)) -gt "$EVENTS_STALE_AFTER" ]; then
        reason="l'ultima lettura di GitHub riuscita di devloop run è di $((now - alive))s fa (soglia EVENTS_STALE_AFTER, ${EVENTS_STALE_AFTER}s), con $SERVICE_NAME attivo"
    fi
    if [ -z "$reason" ]; then
        rm -f "$STALE"
    elif ! [ -f "$STALE" ]; then
        EVENTS+="eventi fermi: $reason"$'\n'
        save "$STALE" "$now"$'\n'
    fi
}

# In EVENTS le righe del log, del servizio e del flusso fermo, una per evento.
local_events() {
    local state last
    [ "$ROLE" = coordinator ] && log_events
    state=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null)
    state=${state:-sconosciuto}
    last=$(cat "$SERVICE_SEEN" 2>/dev/null || printf active)
    if [ "$state" != active ] && [ "$state" != "$last" ]; then
        EVENTS+="servizio: $SERVICE_NAME è $state"$'\n'
    fi
    [ "$state" = "$last" ] || save "$SERVICE_SEEN" "$state"
    # Il servizio fermo resta "servizio:": il flusso fermo conta solo con il servizio attivo.
    [ "$ROLE" = coordinator ] && [ "$state" = active ] && stale_events
}

SECONDS=0
next_local=0
next_stream=0
while :; do
    EVENTS=''
    inbox_events
    if [ "$SECONDS" -ge "$next_local" ]; then
        local_events
        next_local=$((SECONDS + LOCAL_POLL))
    fi
    # Con un messaggio o una riga locale la sveglia esce subito: il flusso resta dopo il cursore,
    # e lo legge il risveglio successivo.
    if [ -z "$EVENTS" ] && [ "$SECONDS" -ge "$next_stream" ]; then
        stream_events
        next_stream=$((SECONDS + STREAM_POLL))
    fi
    if [ -n "$EVENTS" ]; then
        printf '%s' "$EVENTS"
        exit 0
    fi
    if [ "$SECONDS" -ge "$PERIOD" ]; then
        say "periodo: ${PERIOD}s senza eventi del $ROLE_NAME: il controllo periodico"
        exit 0
    fi
    sleep "$POLL"
done
