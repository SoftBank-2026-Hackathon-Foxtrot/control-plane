#!/bin/sh
# Based on redis-migrator/onprem/start.sh. Persist the migration fence in users.acl.
set -eu
if [ ! -f /data/users.acl ]; then
    umask 077
    admin_hash=$(sha256sum /run/foxtrot/valkey_admin | cut -d' ' -f1)
    app_hash=$(sha256sum /run/foxtrot/valkey_app | cut -d' ' -f1)
    {
        printf 'user default on #%s ~* &* +@all\n' "$admin_hash"
        printf 'user app on #%s ~* &* +@all\n' "$app_hash"
    } > /data/users.acl
fi
exec docker-entrypoint.sh valkey-server --appendonly yes --appendfsync everysec \
    --notify-keyspace-events KA --aclfile /data/users.acl --maxmemory-policy noeviction --maxmemory 1gb
