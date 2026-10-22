#!/usr/bin/env bash
# Install the apps directory page (https://apps.badita.com/) and the nginx config
# that serves it. Re-runnable.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"

sudo install -d -m 0755 /srv/apps-portal
# SELinux: nginx (httpd_t) may read httpd_sys_content_t only.
if ! sudo semanage fcontext -l | grep -q '^/srv/apps-portal(/.\*)?'; then
  sudo semanage fcontext -a -t httpd_sys_content_t '/srv/apps-portal(/.*)?'
fi
sudo install -m 0644 "$root/deploy/portal/index.html" "$root/deploy/portal/portal.css" /srv/apps-portal/
sudo restorecon -R /srv/apps-portal

sudo install -m 0644 "$root/deploy/nginx/apps.badita.com.conf" /etc/nginx/conf.d/apps.badita.com.conf
sudo nginx -t
sudo systemctl reload nginx
