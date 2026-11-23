#!/usr/bin/env bash
# Avvia la sessione di un ruolo di devloop (ADR 0002, precisazione del 2026-11-22; #531): il
# supervisore, il coordinatore o l'agent dell'host, ciascuno in una sessione tmux, con l'utente
# del ruolo, la CLI e il modello del ruolo, e il suo runbook (docs/devloop/roles/<ruolo>.md di
# questo checkout) come istruzioni iniziali; per supervisore e coordinatore, prima del runbook, i
# valori dell'installazione per il canale fra i due ruoli e la sveglia del coordinatore (#543) e,
# per il coordinatore, la key_dir della sua app e il suo clone (#565).
#
# - supervisor, coordinator: CLI (kind) e modello dalla tabella [roles.<ruolo>] della
#   configurazione di devloop: devloop/config.toml di questo checkout con sopra l'overlay
#   <state_dir>/config.local.toml, come devloop li compone. Senza la tabella si ferma.
# - host: CLI e modello li sceglie l'operatore al lancio (--cli, --model); la sessione gira con
#   l'utente dell'operatore che lancia lo script con sudo, quello che ha sudo sull'host.
#
# Non scrive file e non legge segreti: legge i parametri, la configurazione e il runbook, e avvia
# tmux. Va lanciato dall'operatore, da un suo checkout aggiornato del branch principale.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ROOT_DIR=$(cd "$OPS_DIR/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"

# Le CLI che un ruolo può usare: i kind degli agent di devloop (AGENT_KINDS di devloop/config.py).
ROLE_CLIS='claude codex agy'

usage() {
    say "Uso: sudo bash $0 [opzioni] supervisor|coordinator"
    say "     sudo bash $0 [opzioni] host --cli claude|codex|agy --model MODELLO"
    say
    say 'Avvia la sessione tmux del ruolo, con il suo utente, la sua CLI e il suo modello, e il'
    say 'runbook docs/devloop/roles/<ruolo>.md come istruzioni iniziali.'
    say
    say '  --cli CLI        solo per host: claude, codex o agy'
    say '  --model MODELLO  solo per host: il modello della CLI'
    say
    say "$OPS_COMMON_HELP"
}

ROLE=''
CLI=''
MODEL=''
while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --cli)
            [ $# -ge 2 ] || die '--cli vuole claude, codex o agy'
            CLI=$2
            shift 2
            ;;
        --model)
            [ $# -ge 2 ] || die '--model vuole il nome del modello'
            MODEL=$2
            shift 2
            ;;
        supervisor | coordinator | host)
            [ -z "$ROLE" ] || die "un solo ruolo per volta: $ROLE e $1"
            ROLE=$1
            shift
            ;;
        -*) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
        *) die "ruolo sconosciuto: '$1' (supervisor, coordinator o host)" ;;
    esac
done
[ -n "$ROLE" ] || { usage >&2; exit 2; }

ops_load_params
ops_check_platform

# role_table ROLE: "kind model" della tabella [roles.ROLE], da devloop/config.toml di questo
# checkout con sopra l'overlay dello state_dir, tabella su tabella come devloop/config.py
# (_merge). Solo la libreria standard, con Python isolato: nessun file della directory corrente
# entra.
role_table() {
    python3 -I - "$ROOT_DIR/devloop/config.toml" "$STATE_PATH/config.local.toml" "$1" <<'PY'
import sys
import tomllib
from pathlib import Path

installation, overlay, role = sys.argv[1:4]
table = {}
for path in (installation, overlay):
    where = Path(path)
    if path == overlay and not where.exists():
        continue
    try:
        with where.open('rb') as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.exit(f'non leggo {where}: {exc}')
    value = (data.get('roles') or {}).get(role)
    if value is None:
        continue
    if not isinstance(value, dict):
        sys.exit(f'{where}: roles.{role} non è una tabella: start-role.sh legge la CLI e il '
                 f'modello solo dalla tabella [roles.{role}] (kind, model)')
    table.update(value)
if not table:
    sys.exit(f'manca la tabella [roles.{role}] (in {overlay} o in {installation}): il ruolo '
             'non è assegnato, e la sua CLI e il suo modello non sono configurati')
kind, model = table.get('kind'), table.get('model')
if not isinstance(kind, str) or not isinstance(model, str):
    sys.exit(f'[roles.{role}]: servono kind e model, come stringhe')
print(kind, model)
PY
}

step "Ruolo $ROLE"
case $ROLE in
    supervisor | coordinator)
        [ -z "$CLI" ] && [ -z "$MODEL" ] \
            || die "--cli e --model valgono solo per host: CLI e modello di $ROLE sono quelli di [roles.$ROLE]"
        table=$(role_table "$ROLE") || die "niente avviato"
        read -r CLI MODEL <<<"$table"
        ok "[roles.$ROLE]: CLI $CLI, modello $MODEL"
        if [ "$ROLE" = supervisor ]; then
            ROLE_USER=$SUPERVISOR_USER
            ROLE_HOME=$SUPERVISOR_HOME
            ROLE_DIR=$SUPERVISOR_CLONE
        else
            ROLE_USER=$COORDINATOR_USER
            ROLE_HOME=$COORDINATOR_HOME
            ROLE_DIR=$COORDINATOR_HOME
            # Il clone del coordinatore, da cui lancia scripts/coordinator-token.py (#565): nella
            # sua home, con il nome del repository, come i clone di devloop e del supervisore
            # senza PROJECT (/home/coordinator/devloop su questo host, ADR 0007).
            TOKEN_CLONE=$COORDINATOR_HOME/${REPO#*/}
        fi
        ;;
    host)
        [ -n "$CLI" ] && [ -n "$MODEL" ] \
            || die "per host CLI e modello li sceglie l'operatore: --cli claude|codex|agy --model MODELLO"
        # L'utente dell'operatore che ha lanciato lo script con sudo: quello che ha sudo
        # sull'host. Mai root, mai l'utente di un altro ruolo.
        ROLE_USER=${SUDO_USER:-$(id -un)}
        [[ $ROLE_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "utente dell'operatore non valido: '$ROLE_USER'"
        [ "$ROLE_USER" != root ] \
            || die "l'agent dell'host gira con l'utente dell'operatore, non come root: lanciare con sudo dal proprio utente"
        for var in DEVLOOP_USER AGENT_USER SUPERVISOR_USER COORDINATOR_USER; do
            [ "$ROLE_USER" != "${!var}" ] \
                || die "l'agent dell'host non gira come $ROLE_USER ($var): lanciare con sudo dall'utente dell'operatore"
        done
        ROLE_HOME=''
        ROLE_DIR=$ROOT_DIR
        ok "CLI $CLI, modello $MODEL, scelti dall'operatore"
        ;;
esac

# CLI e modello finiscono nella riga di comando della sessione: solo forme note.
case " $ROLE_CLIS " in
    *" $CLI "*) ;;
    *) die "CLI sconosciuta: '$CLI' (claude, codex o agy)" ;;
esac
[[ $MODEL =~ ^[A-Za-z0-9][A-Za-z0-9._:/@-]*$ ]] || die "modello non valido: '$MODEL'"

RUNBOOK=docs/devloop/roles/$ROLE.md
[ -f "$ROOT_DIR/$RUNBOOK" ] || die "manca il runbook $ROOT_DIR/$RUNBOOK"

step "Utente $ROLE_USER"
user_exists "$ROLE_USER" || die "l'utente $ROLE_USER non esiste: prima il setup del ruolo (ops/README.md)"
if [ -z "$ROLE_HOME" ]; then
    ROLE_HOME=$(getent passwd "$ROLE_USER" | cut -d: -f6) || true
    [[ $ROLE_HOME =~ ^/[A-Za-z0-9._/-]*$ ]] || die "non trovo la home di $ROLE_USER: '$ROLE_HOME'"
fi
[ -d "$ROLE_DIR" ] || die "manca la directory di lavoro $ROLE_DIR"
ok "$ROLE_USER, directory di lavoro $ROLE_DIR"
if [ "$ROLE" = coordinator ] && [ ! -d "$TOKEN_CLONE/.git" ]; then
    warn "manca il clone del coordinatore $TOKEN_CLONE: senza, non conia i token della sua app (ops/README.md, passo 10)"
fi
command -v tmux >/dev/null 2>&1 || die 'manca tmux: installarlo (apt-get install tmux)'

# Il runbook, intero, è il primo messaggio della sessione: le istruzioni del ruolo, valide per
# qualunque CLI. Per supervisore e coordinatore, prima del runbook, i valori di questa
# installazione per il canale fra i due ruoli e la sveglia del coordinatore (#543), e l'avvio dalla
# sezione "At startup": una sessione nuova li segue senza istruzioni dell'operatore.
CHANNEL=''
case $ROLE in
    supervisor) INBOX=to-supervisor OUTBOX=to-coordinator ;;
    coordinator) INBOX=to-coordinator OUTBOX=to-supervisor ;;
    host) INBOX='' ;;
esac
# Per il coordinatore anche i valori dei token della sua app (#565): la sua key_dir e il clone da
# cui lancia scripts/coordinator-token.py, così il runbook non contiene percorsi di un host.
TOKENS=''
[ "$ROLE" != coordinator ] \
    || TOKENS=" For the section \"GitHub\": the key_dir of your app (DEVLOOP_APP_KEY_DIR) is $COORDINATOR_KEY_DIR, and your clone, from which you run scripts/coordinator-token.py, is $TOKEN_CLONE."
if [ -n "$INBOX" ]; then
    CHANNEL="Values of this installation (ops/params.env), for the sections \"At startup\", \"Channel\" and \"Watching\" of the runbook: the state_dir of devloop is $STATE_PATH; the mailbox between supervisor and coordinator is $COORDINATION_DIR (group $COORDINATION_GROUP): your messages are in $COORDINATION_DIR/$INBOX/, yours to the other role go in $COORDINATION_DIR/$OUTBOX/; the coordinator's wake-up is bash $COORDINATOR_OPS_DIR/host/coordinator-wait.sh.$TOKENS Start now with the section \"At startup\".

"
fi
PROMPT="You hold the role \"$ROLE\" of devloop for $REPO. Your instructions are the runbook below ($RUNBOOK of the checkout $ROOT_DIR, started by ops/host/start-role.sh): follow it for the whole session.

$CHANNEL$(<"$ROOT_DIR/$RUNBOOK")"

# La riga di comando della CLI, interattiva, con il modello e il primo messaggio.
case $CLI in
    claude) COMMAND=(claude --model "$MODEL" "$PROMPT") ;;
    codex) COMMAND=(codex --model "$MODEL" "$PROMPT") ;;
    agy) COMMAND=(agy --model "$MODEL" -i "$PROMPT") ;;
esac

SESSION=devloop-$ROLE
# Come l'utente del ruolo (sudo -u … -H), o direttamente se lo script gira già come lui.
AS_ROLE=()
[ "$(id -un)" = "$ROLE_USER" ] || AS_ROLE=(sudo -u "$ROLE_USER" -H --)
step "Sessione tmux $SESSION"
if [ "$DRY_RUN" != 1 ] && "${AS_ROLE[@]}" tmux has-session -t "=$SESSION" >/dev/null 2>&1; then
    die "la sessione $SESSION di $ROLE_USER esiste già: sudo -u $ROLE_USER -H tmux attach -t $SESSION"
fi
# La CLI dalla home dell'utente del ruolo (~/.local/bin), poi dal sistema.
LAUNCH=("${AS_ROLE[@]}" tmux new-session -d -s "$SESSION" -c "$ROLE_DIR"
        env "PATH=$ROLE_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" "${COMMAND[@]}")
# Il comando si stampa senza il primo messaggio, che è l'ultimo argomento: il runbook intero.
if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run]'; else printf '  +'; fi
printf ' %q' "${LAUNCH[@]:0:${#LAUNCH[@]}-1}"
printf ' <istruzioni: %s>\n' "$RUNBOOK"
[ "$DRY_RUN" = 1 ] && exit 0
"${LAUNCH[@]}" || die "tmux non ha avviato la sessione $SESSION"
say "Sessione avviata. Per entrarci: sudo -u $ROLE_USER -H tmux attach -t $SESSION"
