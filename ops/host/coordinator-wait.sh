#!/usr/bin/env bash
# La sveglia del coordinatore di devloop (#543), ora un involucro compatibile di role-wait.sh
# (#602): `coordinator-wait.sh [--params FILE] [--period SECONDI]` è
# `role-wait.sh --role coordinator` con gli stessi argomenti, dalla stessa copia. Eventi, uscite e
# stato sono quelli di role-wait.sh; il periodo di default è COORDINATOR_WAIT_PERIOD (un'ora, come
# ROLE_WAIT_PERIOD, se non è scritto in params.env).
exec bash "$(dirname "${BASH_SOURCE[0]}")/role-wait.sh" --role coordinator "$@"
