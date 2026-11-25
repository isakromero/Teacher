#!/bin/sh
# The container's entrypoint (Dockerfile). Started as root, it gives $TEACHER_HOME (the data
# volume, owned by root when the host creates it) to the user "teacher", then runs the command
# as that user, so the server never runs as root. Started as another user, it just runs the
# command.
set -eu

if [ "$(id -u)" = "0" ]; then
    : "${TEACHER_HOME:?TEACHER_HOME must be set (the Dockerfile sets it to /data)}"
    mkdir -p "$TEACHER_HOME"
    chown teacher:teacher "$TEACHER_HOME"
    exec setpriv --reuid=teacher --regid=teacher --init-groups -- "$@"
fi
exec "$@"
