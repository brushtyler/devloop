#!/usr/bin/env bash
# Prepara l'host per devloop: pacchetti, utente dedicato, directory dei lock per repository,
# clone, state_dir, overlay d'esempio, key_dir 700, impostazioni di Claude Code senza
# aggiornamenti automatici, unità systemd. Con SOURCE_REPO anche il clone della sorgente
# dell'installazione e l'avviatore dal suo tag. Con PROJECT (ADR 0006, R8) prepara uno dei
# progetti dell'host: l'unità template devloop@.service e la drop-in della sua istanza.
# Idempotente: si può rieseguire, fa solo ciò che manca. Non scrive segreti e non avvia il
# servizio. L'utente degli agent e le sue CLI li prepara setup-agent-user.sh (#415); i login
# delle CLI, le chiavi delle app e il pin restano passi manuali (ops/README.md).

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"

usage() {
    say "Uso: sudo bash $0 [--params FILE] [--dry-run] [--yes]"
    say
    say "$OPS_COMMON_HELP"
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

ops_load_params
ops_check_platform
require_root

PACKAGES='python3 git gh openssl acl nodejs npm ca-certificates curl sudo'

step 'Pacchetti di sistema'
missing=()
for pkg in $PACKAGES; do
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
        ok "$pkg installato"
    else
        missing+=("$pkg")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    run apt-get update
    run apt-get install -y --no-install-recommends "${missing[@]}"
fi

step "Utente $DEVLOOP_USER"
if user_exists "$DEVLOOP_USER"; then
    ok "l'utente $DEVLOOP_USER esiste"
else
    run useradd --create-home --home-dir "$DEVLOOP_HOME" --shell /bin/bash "$DEVLOOP_USER"
fi

step "Lock per repository: $REPO_LOCK_DIR, gruppo $REPO_LOCK_GROUP (ADR 0006, R8)"
# Una directory comune a tutti gli utenti devloop dell'host, e a nessun altro: il dispatcher vi
# prende il lock del suo repository, così due servizi sullo stesso repository non partono insieme.
if getent group "$REPO_LOCK_GROUP" >/dev/null; then
    ok "il gruppo $REPO_LOCK_GROUP esiste"
else
    run groupadd --system "$REPO_LOCK_GROUP"
fi
if id -nG "$DEVLOOP_USER" 2>/dev/null | tr ' ' '\n' | grep -qxF "$REPO_LOCK_GROUP"; then
    ok "$DEVLOOP_USER è nel gruppo $REPO_LOCK_GROUP"
else
    # Vale dal prossimo avvio del servizio.
    run usermod -aG "$REPO_LOCK_GROUP" "$DEVLOOP_USER"
fi
if [ -d "$REPO_LOCK_DIR" ] && [ "$(stat -c '%U %G' "$REPO_LOCK_DIR")" != "root $REPO_LOCK_GROUP" ] \
    && [ -n "$(find "$REPO_LOCK_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    # systemd-tmpfiles ne cambierebbe modo e proprietario: non si tocca una directory d'altri.
    die "$REPO_LOCK_DIR esiste già, non è vuota e non è di root:$REPO_LOCK_GROUP: scegliere un altro REPO_LOCK_DIR"
fi
if put_file "$TMPFILES_FILE" 644 root "$(render "$OPS_DIR/host/tmpfiles-devloop-repos.conf")" \
    || [ ! -d "$REPO_LOCK_DIR" ]; then
    run systemd-tmpfiles --create "$TMPFILES_FILE"
    [ "$DRY_RUN" = 1 ] || [ -d "$REPO_LOCK_DIR" ] \
        || die "systemd-tmpfiles non ha creato $REPO_LOCK_DIR: crearne a mano le directory superiori"
else
    ok "$REPO_LOCK_DIR esiste"
fi

step "Clone di $REPO"
if [ -d "$DEVLOOP_CLONE/.git" ]; then
    ok "$DEVLOOP_CLONE è già un clone (non lo aggiorna: devloop fissa le copie da origin)"
else
    as_user "$DEVLOOP_USER" git clone "https://github.com/$REPO.git" "$DEVLOOP_CLONE"
fi

if [ -n "$SOURCE_REPO" ]; then
    step "Sorgente dell'installazione: $SOURCE_REPO, avviatore dal tag $SOURCE_REF"
    if [ -d "$SOURCE_CLONE/.git" ]; then
        ok "$SOURCE_CLONE è già un clone (devloop pin lo aggiorna da origin)"
    else
        as_user "$DEVLOOP_USER" git clone "https://github.com/$SOURCE_REPO.git" "$SOURCE_CLONE"
    fi
    # L'avviatore sta fuori dalla copia fissata e dal repository gestito (ADR 0006, sezione 3.5):
    # di root, non scrivibile dall'utente devloop né dagli agent.
    ensure_dir "$LAUNCHER_DIR" 755 root
    if [ "$DRY_RUN" = 1 ]; then
        say "  [dry-run] installerebbe $LAUNCHER (mode 755, proprietario root): scripts/devloop-pinned del tag $SOURCE_REF di origin, in $SOURCE_CLONE"
    else
        # Il tag come è su origin, anche se il clone ne aveva uno locale con lo stesso nome.
        as_user "$DEVLOOP_USER" git -C "$SOURCE_CLONE" fetch --quiet --no-tags origin \
            "+refs/tags/$SOURCE_REF:refs/tags/$SOURCE_REF"
        if [ "$(id -un)" = "$DEVLOOP_USER" ]; then
            launcher=$(git -C "$SOURCE_CLONE" show "refs/tags/$SOURCE_REF:scripts/devloop-pinned") \
                || die "scripts/devloop-pinned non è nel tag $SOURCE_REF di $SOURCE_REPO"
        else
            launcher=$(sudo -u "$DEVLOOP_USER" -H -- git -C "$SOURCE_CLONE" show \
                "refs/tags/$SOURCE_REF:scripts/devloop-pinned") \
                || die "scripts/devloop-pinned non è nel tag $SOURCE_REF di $SOURCE_REPO"
        fi
        put_file "$LAUNCHER" 755 root "$launcher" || true
    fi
fi

step 'state_dir, log e overlay'
STATE=$STATE_PATH
if [ -d "$STATE" ]; then
    ok "$STATE esiste"
elif [[ $STATE_DIR == /* ]]; then
    # Fuori dal clone: le directory intermedie restano di root, lo state_dir è di devloop.
    ensure_dir "$STATE" 755 "$DEVLOOP_USER"
else
    # mkdir -p come utente devloop: anche le directory intermedie sono sue.
    as_user "$DEVLOOP_USER" mkdir -p "$STATE"
fi
if [ -f "$STATE/run.log" ]; then
    ok "$STATE/run.log esiste"
else
    # Lo crea l'utente devloop: systemd lo aprirebbe in append come root.
    as_user "$DEVLOOP_USER" touch "$STATE/run.log"
fi
if [ -f "$STATE/config.local.toml" ]; then
    ok "$STATE/config.local.toml esiste: è dell'operatore, non lo tocca"
else
    put_file "$STATE/config.local.toml" 644 "$DEVLOOP_USER" "$(<"$OPS_DIR/host/config.local.toml.example")" || true
fi
# Con l'avviatore che riceve lo state_dir (LAUNCHER_ARGS), devloop si ferma se l'overlay ne
# nomina un altro; con la sorgente a parte, l'overlay dice dove è il suo clone. Sempre, l'overlay
# nomina la directory dei lock per repository (ADR 0006, R8) e il repository gestito, che
# devloop/config.toml non ha più (ADR 0006, R6).
overlay_lines=("repo = \"$REPO\"" "repo_lock_dir = \"$REPO_LOCK_DIR\"")
if [ -n "$LAUNCHER_ARGS" ]; then
    overlay_lines+=("state_dir = \"$STATE_PATH\"" ${SOURCE_REPO:+"source = \"$SOURCE_CLONE\""})
fi
for line in "${overlay_lines[@]}"; do
    if [ -f "$STATE/config.local.toml" ] && grep -qxF "$line" "$STATE/config.local.toml"; then
        ok "l'overlay ha $line"
    else
        todo "scrivere $line in $STATE/config.local.toml"
    fi
done

step 'Directory private (700)'
ensure_dir "$DEVLOOP_KEY_DIR" 700 "$DEVLOOP_USER"
ensure_dir "$DEVLOOP_HOME/.claude" 700 "$DEVLOOP_USER"
ensure_dir "$DEVLOOP_HOME/.codex" 700 "$DEVLOOP_USER"
ensure_dir "$DEVLOOP_HOME/.local" 755 "$DEVLOOP_USER"
ensure_dir "$DEVLOOP_HOME/.local/bin" 755 "$DEVLOOP_USER"

step 'Claude Code senza aggiornamenti automatici'
SETTINGS="$DEVLOOP_HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    if python3 - "$SETTINGS" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
sys.exit(0 if data.get('autoUpdates') is False
         and str(data.get('env', {}).get('DISABLE_AUTOUPDATER')) == '1' else 1)
EOF
    then
        ok "$SETTINGS spegne gli aggiornamenti automatici"
    else
        warn "$SETTINGS esiste ma non ha autoUpdates: false e env.DISABLE_AUTOUPDATER=\"1\": aggiungerli a mano"
    fi
else
    put_file "$SETTINGS" 600 "$DEVLOOP_USER" '{
  "autoUpdates": false,
  "env": {
    "DISABLE_AUTOUPDATER": "1"
  }
}' || true
fi

step 'CLI degli agent'
say "  le CLI e i loro login sono dell'utente $AGENT_USER (#415): setup-agent-user.sh"

step "Servizio $SERVICE_NAME"
# Con PROJECT (ADR 0006, R8) l'unità template, comune ai progetti, e la drop-in del progetto.
unit_changed=0
put_file "$UNIT_FILE" 644 root "$(render "$OPS_DIR/host/$UNIT_TEMPLATE")" && unit_changed=1
if [ -n "$DROPIN_FILE" ]; then
    ensure_dir "$(dirname "$DROPIN_FILE")" 755 root
    put_file "$DROPIN_FILE" 644 root "$(render "$OPS_DIR/host/devloop-project.conf")" \
        && unit_changed=1
fi
if [ "$unit_changed" = 1 ]; then
    run systemctl daemon-reload
fi
say "  il servizio non viene avviato né abilitato: lo fa l'operatore (ops/README.md)"

step 'Chiavi delle GitHub App'
for role in $DEVLOOP_KEYS; do
    if compgen -G "$DEVLOOP_KEY_DIR/$APP_PREFIX$role.*.private-key.pem" >/dev/null 2>&1; then
        ok "chiave di $APP_PREFIX$role presente"
    else
        todo "copiare la chiave di $APP_PREFIX$role in $DEVLOOP_KEY_DIR (mode 600, proprietario $DEVLOOP_USER)"
    fi
done

step 'Fatto'
if [ -n "$SOURCE_REPO" ]; then
    say "Poi: chiavi, setup-agent-user.sh e login delle CLI, primo pin del tag $SOURCE_REF, ops/host/check-host.sh, avvio del servizio (ops/README.md)."
else
    say "Poi: chiavi, setup-agent-user.sh e login delle CLI, primo pin, ops/host/check-host.sh, avvio del servizio (ops/README.md)."
fi
