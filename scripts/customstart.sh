#!/bin/bash
# © 2026 CubeCoders Limited. All Rights Reserved.
#
# customstart.sh — runs as root inside the container before AMP drops to
# the amp user. Single purpose: pre-create the in-cluster ServiceAccount
# mount dir owned by amp so mock-k8s-go can populate token/namespace/ca.crt
# at the standard path director's IGWO subsystem reads at startup.
#
# Installed at <instance>/customstart.sh by install.sh on every update;
# AMP picks it up on the next container start.
set -e
SA_DIR=/var/run/secrets/kubernetes.io/serviceaccount
mkdir -p "$SA_DIR"
chown -R "${AMPUSERID:-1001}:${AMPGROUPID:-1001}" /var/run/secrets
chmod 0755 "$SA_DIR"
