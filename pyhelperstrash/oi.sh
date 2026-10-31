#!/bin/sh
DIR=$(dirname "$0")
SSH_ASKPASS="$DIR/askpass.sh" SSH_ASKPASS_REQUIRE=force setsid ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 kvmuser@192.168.1.166 "$@" </dev/null
