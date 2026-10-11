#!/bin/sh
# Lädt Docker-Secrets in die Umgebung und startet danach den eigentlichen Befehl.
#
# Für jede Variable NAME_FILE, deren Wert unter /run/secrets/ liegt, wird NAME
# mit dem Dateiinhalt gesetzt (ohne abschließenden Zeilenumbruch) und NAME_FILE
# entfernt. Andere *_FILE-Variablen bleiben unberührt. Ohne *_FILE ändert sich
# nichts, das Image läuft dann wie bisher mit Werten aus der Umgebung.
set -e
for name in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)_FILE=\/run\/secrets\/.*/\1/p'); do
  datei=$(printenv "${name}_FILE")
  if [ ! -r "$datei" ]; then
    echo "env-secrets: Datei für $name nicht lesbar: $datei" >&2
    exit 1
  fi
  export "$name=$(cat "$datei")"
  unset "${name}_FILE"
done
exec "$@"
