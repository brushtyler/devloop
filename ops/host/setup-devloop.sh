#!/usr/bin/env bash
# Prepara l'host per devloop: pacchetti, utente dedicato, clone, state_dir, overlay d'esempio,
# key_dir 700, impostazioni di Claude Code senza aggiornamenti automatici, unità systemd. Con
# SOURCE_REPO anche il clone della sorgente dell'installazione e l'avviatore dal suo tag.
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
# nomina un altro; con la sorgente a parte, l'overlay dice dove è il suo clone.
if [ -n "$LAUNCHER_ARGS" ]; then
    for line in "state_dir = \"$STATE_PATH\"" ${SOURCE_REPO:+"source = \"$SOURCE_CLONE\""}; do
        if [ -f "$STATE/config.local.toml" ] && grep -qxF "$line" "$STATE/config.local.toml"; then
            ok "l'overlay ha $line"
        else
            todo "scrivere $line in $STATE/config.local.toml"
        fi
    done
fi

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
UNIT="$SYSTEMD_DIR/$SERVICE_NAME.service"
if put_file "$UNIT" 644 root "$(render "$OPS_DIR/host/devloop.service")"; then
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
