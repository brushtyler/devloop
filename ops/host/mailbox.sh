#!/usr/bin/env bash
# Le caselle fra i ruoli di devloop (#707): scrive, elenca, mostra e sposta i messaggi di
# COORDINATION_DIR (to-supervisor/, to-coordinator/, to-operator/), e pulisce le loro done/.
#
#   mailbox.sh [--params FILE] send --to supervisor|coordinator|operator [--file FILE]
#   mailbox.sh [--params FILE] list [--box supervisor|coordinator|operator]
#   mailbox.sh [--params FILE] show [--box ...] ID
#   mailbox.sh [--params FILE] done [--box ...] ID
#   mailbox.sh [--params FILE] read          (solo root: to-operator/)
#   mailbox.sh [--params FILE] prune         (solo root: le done/ delle tre caselle)
#
# Chi lo esegue si riconosce dall'utente: SUPERVISOR_USER è il supervisore, COORDINATOR_USER il
# coordinatore, root l'operatore o l'agent dell'host; nessun altro.
#
# - send: il testo da stdin o da --file, pubblicato come lo pubblicavano i runbook a mano: un file
#   temporaneo che comincia con "." nella casella, 640 del gruppo COORDINATION_GROUP, poi una
#   rinomina atomica nel nome "<data e ora UTC>-<suffisso casuale>.txt", che stampa (l'id). In
#   testa al messaggio una riga con chi scrive (dall'utente che lo esegue) e il progetto. Rifiuta,
#   senza pubblicare nulla e senza ripeterlo, un testo che contiene una chiave privata, un token o
#   il valore di un nome da segreto, con i rilevatori _PEM, _CREDENTIAL e _ASSIGNED di
#   devloop/agents.py (copiati qui: la copia di root ha solo i file di ops/). Un ruolo non scrive
#   nella propria casella.
# - list, show, done: un ruolo sulla propria casella (list anche su quella dell'altro ruolo, per
#   vedere se un suo messaggio è ancora da leggere: solo nomi, mittenti e ore). La regola resta
#   «leggi, agisci, poi sposta»: show non sposta, così una sessione caduta a metà lascia il
#   messaggio dov'era, e il segnale «non ancora gestito» per l'altro ruolo e per il watchdog; done
#   lo sposta in done/. root su qualunque casella.
# - read: solo root, su to-operator/: stampa i messaggi non letti e li sposta subito in done/. Un
#   ruolo non legge to-operator/ (#663): list, show e read di quella casella sono rifiutati a chi
#   non è root.
# - La provenienza: list, show e read dicono il proprietario del file (stat), cioè chi l'ha
#   scritto davvero; la riga di testa è solo testo, e non se ne fidano.
# - prune: solo root, toglie dalle done/ delle tre caselle i messaggi più vecchi di
#   MAILBOX_DONE_DAYS giorni (30 per default), per data di modifica; mai un messaggio non letto,
#   che sta fuori da done/. Lo esegue il timer devloop-mailbox-prune@<progetto>, una volta al
#   giorno, come root.
#
# I file si aprono senza seguire i link, e solo in directory di root o di chi lo esegue: un ruolo
# che mette un link al posto di un messaggio o di done/ non fa leggere né spostare a root un
# file fuori dalla casella. Esce con 0, con 1 su un rifiuto o un errore, con 2 su un'opzione
# sbagliata. Si esegue dalla copia di root di ops/ dei ruoli (SUPERVISOR_OPS_DIR,
# COORDINATOR_OPS_DIR), con i suoi parametri, o da un checkout con --params.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"

usage() {
    say "Uso: bash $0 [--params FILE] COMANDO"
    say
    say '  send --to supervisor|coordinator|operator [--file FILE]'
    say '                    pubblica un messaggio, dal testo di stdin o di FILE; stampa il suo id'
    say '  list [--box RUOLO]  i messaggi da leggere: id, proprietario del file, ora UTC'
    say '  show [--box RUOLO] ID  un messaggio, con il proprietario del file; non lo sposta'
    say '  done [--box RUOLO] ID  sposta il messaggio in done/, dopo averlo gestito'
    say "  read              solo root: stampa i messaggi di to-operator/ e li sposta in done/"
    say "  prune             solo root: toglie dalle done/ i messaggi più vecchi di MAILBOX_DONE_DAYS giorni"
    say
    say '  --params FILE     i parametri (default: params.env accanto a questa copia)'
    say '  --box RUOLO       la casella (default: la propria; per root, operator)'
}

bad_usage() {
    printf '%s: %s (vedi --help)\n' "$(basename "$0")" "$1" >&2
    exit 2
}

COMMAND=''
TO=''
BOX=''
FILE=''
ID=''
while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --params | --to | --box | --file)
            [ $# -ge 2 ] || bad_usage "$1 vuole un valore"
            case $1 in
                --params) PARAMS_FILE=$2 ;;
                --to) TO=$2 ;;
                --box) BOX=$2 ;;
                --file) FILE=$2 ;;
            esac
            shift 2
            ;;
        -*) bad_usage "opzione sconosciuta: $1" ;;
        *)
            if [ -z "$COMMAND" ]; then
                COMMAND=$1
            elif [ -z "$ID" ]; then
                ID=$1
            else
                bad_usage "argomento in più: $1"
            fi
            shift
            ;;
    esac
done

case $COMMAND in
    send)
        [ -n "$TO" ] || bad_usage 'send vuole --to supervisor|coordinator|operator'
        [ -z "$BOX" ] && [ -z "$ID" ] || bad_usage 'send vuole solo --to e, se serve, --file'
        ;;
    list) [ -z "$TO" ] && [ -z "$FILE" ] && [ -z "$ID" ] || bad_usage 'list vuole solo --box' ;;
    show | done)
        [ -n "$ID" ] || bad_usage "$COMMAND vuole l'id del messaggio"
        [ -z "$TO" ] && [ -z "$FILE" ] || bad_usage "$COMMAND vuole solo --box e l'id"
        ;;
    read | prune)
        [ -z "$TO" ] && [ -z "$BOX" ] && [ -z "$FILE" ] && [ -z "$ID" ] \
            || bad_usage "$COMMAND non vuole argomenti"
        ;;
    '') bad_usage 'manca il comando: send, list, show, done, read o prune' ;;
    *) bad_usage "comando sconosciuto: $COMMAND" ;;
esac
for value in "$TO" "$BOX"; do
    case $value in
        '' | supervisor | coordinator | operator) ;;
        *) bad_usage "la casella è supervisor, coordinator o operator: '$value'" ;;
    esac
done

ops_load_params

# Chi lo esegue, dall'utente: il ruolo, la sua casella e il nome nella riga di testa.
if [ "$(id -u)" = 0 ]; then
    ME=root
    ME_NAME="root (l'operatore o l'agent dell'host)"
else
    case $(id -un) in
        "$SUPERVISOR_USER") ME=supervisor ME_NAME=supervisore ;;
        "$COORDINATOR_USER") ME=coordinator ME_NAME=coordinatore ;;
        *) die "va eseguito come $SUPERVISOR_USER (il supervisore), $COORDINATOR_USER (il coordinatore) o root" ;;
    esac
fi

if [ -n "$PROJECT" ]; then
    WHO="progetto $PROJECT ($REPO)"
else
    WHO="progetto $REPO"
fi

# Le regole di chi scrive e legge quale casella (i runbook dei ruoli; #663 per to-operator/).
case $COMMAND in
    send)
        [ "$TO" != "$ME" ] || die "un ruolo non scrive nella propria casella: to-$TO/ è la tua"
        ;;
    list | show | done)
        if [ -z "$BOX" ]; then
            [ "$ME" = root ] && BOX=operator || BOX=$ME
        fi
        if [ "$ME" != root ]; then
            [ "$BOX" != operator ] \
                || die "to-operator/ la legge solo l'operatore, come root: un ruolo non la legge (#663)"
            [ "$COMMAND" = list ] || [ "$BOX" = "$ME" ] \
                || die "$COMMAND solo sulla propria casella, to-$ME/: to-$BOX/ è dell'altro ruolo"
        fi
        ;;
    read)
        [ "$ME" = root ] \
            || die "read è dell'operatore, come root, su to-operator/: un ruolo non la legge (#663)"
        BOX=operator
        ;;
    prune)
        [ "$ME" = root ] || die 'prune va eseguito come root (il timer devloop-mailbox-prune@<progetto>)'
        ;;
esac
if [ -n "$FILE" ]; then
    [ -f "$FILE" ] && [ -r "$FILE" ] || die "--file: $FILE non esiste o non si legge"
fi

# Tutto il lavoro sui file, con i descrittori delle directory e senza seguire i link.
MAILBOX_PY=$(cat <<'EOF'
import datetime
import grp
import os
import pwd
import re
import secrets
import stat
import string
import sys

# I rilevatori di segreti di devloop/agents.py (#707), uguali carattere per carattere:
# tests/test_ops.py li confronta.
_PEM = re.compile(r'-----BEGIN [^-]+-----.*?(?:-----END [^-]+-----|\Z)', re.S)
_CREDENTIAL = re.compile(r'(?<![A-Za-z0-9_])(?:sk-[A-Za-z0-9_-]{20,}'
                         r'|(?:ghp_|ghs_|gho_|ghu_|ghr_|github_pat_)[A-Za-z0-9_]{20,}'
                         r'|AKIA[A-Z0-9]{16}|Bearer\s+[A-Za-z0-9._~+/-]{20,}=*)(?![A-Za-z0-9_])')
_ASSIGNED = re.compile(
    r'(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_.-])*(?:key|token|secret|password|passwd|credentials?)'
    r'(?:[_.-][A-Za-z0-9]+)*(?![A-Za-z0-9])\\?["\']?\s*[=:]\s*)'
    r'(?:(\\")(?:\\\\(?:\\.|[^\\"\n])|\\[^"\\\n]|[^"\\\n])*(\\"|$)'  # inside a JSON string
    r'|(")(?:\\.|[^"\\\n])*("|$)'
    r"|(')[^'\n]*('|$)"
    r'|[^\s"\'`\\,;&|]+)', re.I | re.M)
# Fine dei rilevatori.

DETECTORS = ((_PEM, 'una chiave privata (PEM)'), (_CREDENTIAL, 'un token o una credenziale'),
             (_ASSIGNED, 'il valore di un nome da segreto (key, token, secret, password, …)'))
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
LIMIT = 256 * 1024
ALPHABET = string.ascii_letters + string.digits
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC

env = os.environ
ROOT = env['MAILBOX_DIR']
BOXES = env['MAILBOX_BOXES'].split()


class Refused(Exception):
    """Un rifiuto o un errore, con il motivo da dire."""


def fail(message):
    raise Refused(message)


def opendir(name, parent=None, label=None):
    """La directory NAME (in PARENT), senza seguire un link, di root o di chi esegue."""
    label = label or name
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        fail(f'{label} non si apre come directory ({exc.strerror})')
    owner = os.fstat(fd).st_uid
    if owner not in (0, os.geteuid()):
        os.close(fd)
        fail(f'{label} è di {user(owner)}, non di root: niente fatto')
    return fd


def user(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def when(st):
    return datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def box_fd(box):
    root = opendir(ROOT, label=ROOT)
    fd = opendir(box, root, f'{ROOT}/{box}')
    os.close(root)
    return fd


def messages(fd):
    """I messaggi da leggere della casella FD: i file regolari, non i temporanei (".")."""
    found = []
    for name in sorted(os.listdir(fd)):
        if name.startswith('.') or not NAME.fullmatch(name):
            continue
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            found.append((name, st))
    return found


def check_id(name):
    if not NAME.fullmatch(name) or name.startswith('.'):
        fail(f"'{name}' non è l'id di un messaggio (il nome del file, come lo stampano send e list)")


def read_message(fd, name, box):
    try:
        mfd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        fail(f'{box}/{name} non c\'è (già spostato in done/?)')
    except OSError as exc:
        fail(f'{box}/{name} non si apre ({exc.strerror}): un link non è un messaggio')
    with os.fdopen(mfd, 'rb') as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            fail(f'{box}/{name} non è un file')
        data = fh.read(LIMIT + 1)
    text = data[:LIMIT].decode('utf-8', errors='replace')
    if len(data) > LIMIT:
        text += f'\n[… troncato a {LIMIT} byte]\n'
    return st, text


def move_done(fd, name, box):
    done = opendir('done', fd, f'{box}/done')
    try:
        os.stat(name, dir_fd=done, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        fail(f'{box}/done/{name} esiste già: niente spostato')
    try:
        os.rename(name, name, src_dir_fd=fd, dst_dir_fd=done)
    except OSError as exc:
        fail(f'{box}/{name} non si sposta in done/ ({exc.strerror})')
    finally:
        os.close(done)


def header(name, st):
    return f'messaggio {name}, proprietario del file {user(st.st_uid)}, scritto {when(st)}'


def show(box, name, move):
    check_id(name)
    fd = box_fd(box)
    st, text = read_message(fd, name, box)
    print(f'== {header(name, st)}')
    print(text, end='' if text.endswith('\n') else '\n')
    if move:
        move_done(fd, name, box)


def send(box, path, first_line, group):
    if path:
        try:
            with open(path, 'rb') as fh:
                data = fh.read(LIMIT + 1)
        except OSError as exc:
            fail(f'--file: {path} non si legge ({exc.strerror})')
    else:
        data = sys.stdin.buffer.read(LIMIT + 1)
    if len(data) > LIMIT:
        fail(f'il testo supera {LIMIT} byte: niente pubblicato')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        fail('il testo non è UTF-8: niente pubblicato')
    if not text.strip():
        fail('il testo è vuoto: niente pubblicato')
    for detector, what in DETECTORS:
        if detector.search(text):
            fail(f'il testo contiene {what}: niente pubblicato. Un messaggio non porta token, '
                 'chiavi né segreti; toglierlo e riscrivere')
    body = f'{first_line}\n\n{text}' + ('' if text.endswith('\n') else '\n')
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        fail(f'il gruppo {group} non esiste (setup-supervisor.sh, setup-coordinator.sh)')
    fd = box_fd(box)
    for _ in range(20):
        suffix = ''.join(secrets.choice(ALPHABET) for _ in range(6))
        tmp = f'.tmp-{suffix}'
        try:
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            continue
        try:
            os.fchown(out, -1, gid)
            os.fchmod(out, 0o640)
            with os.fdopen(out, 'wb') as fh:
                fh.write(body.encode('utf-8'))
                fh.flush()
                os.fsync(fh.fileno())
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            name = f'{stamp}-{suffix}.txt'
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
                print(name)
                return
            os.unlink(tmp, dir_fd=fd)
        except OSError as exc:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            fail(f'non scrivo in {ROOT}/{box} ({exc.strerror}): niente pubblicato')
    fail(f'nessun nome libero in {ROOT}/{box}: niente pubblicato')


def prune(days):
    cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - days * 86400
    errors = 0
    for box in BOXES:
        try:
            fd = box_fd(box)
            done = opendir('done', fd, f'{ROOT}/{box}/done')
        except Refused as exc:
            print(f'mailbox.sh: {exc}', file=sys.stderr)
            errors += 1
            continue
        removed = 0
        for entry in os.scandir(done):
            if entry.name.startswith('.') or not entry.is_file(follow_symlinks=False):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.name, dir_fd=done)
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                print(f'mailbox.sh: {box}/done/{entry.name} non si toglie ({exc.strerror})',
                      file=sys.stderr)
                errors += 1
        os.close(done)
        os.close(fd)
        print(f'{box}/done/: tolti {removed} messaggi più vecchi di {days} giorni')
    return 1 if errors else 0


def main(command, args):
    if command == 'list':
        box, = args
        found = messages(box_fd(box))
        if not found:
            print(f'nessun messaggio da leggere in {box}/')
        for name, st in found:
            print(f'{name}  proprietario {user(st.st_uid)}  {when(st)}')
    elif command == 'show':
        show(*args, move=False)
    elif command == 'done':
        box, name = args
        check_id(name)
        fd = box_fd(box)
        read_message(fd, name, box)
        move_done(fd, name, box)
        print(f'{box}/{name} spostato in {box}/done/')
    elif command == 'read':
        box, = args
        found = messages(box_fd(box))
        if not found:
            print(f'nessun messaggio da leggere in {box}/')
        for name, _ in found:
            show(box, name, move=True)
    elif command == 'send':
        send(*args)
    elif command == 'prune':
        return prune(int(args[0]))
    return 0


try:
    sys.exit(main(sys.argv[1], sys.argv[2:]))
except Refused as exc:
    print(f'mailbox.sh: {exc}', file=sys.stderr)
    sys.exit(1)
EOF
)

boxes=''
for box in $COORDINATION_BOXES $COORDINATION_OPERATOR_BOX; do
    boxes="$boxes $box"
done
export MAILBOX_DIR=$COORDINATION_DIR MAILBOX_BOXES=$boxes

case $COMMAND in
    send)
        exec python3 -I -B -c "$MAILBOX_PY" send "to-$TO" "$FILE" "Da: $ME_NAME, $WHO" \
            "$COORDINATION_GROUP"
        ;;
    list | show | done | read)
        exec python3 -I -B -c "$MAILBOX_PY" "$COMMAND" "to-$BOX" ${ID:+"$ID"}
        ;;
    prune)
        exec python3 -I -B -c "$MAILBOX_PY" prune "$MAILBOX_DONE_DAYS"
        ;;
esac
