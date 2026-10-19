#!/bin/sh
# preseed/late_command: copy the unit bundle onto the new system and have
# provision.sh run on its first boot (ember-firstboot.service).
set -e
T=/target
mkdir -p $T/opt/ember-setup
cp -a /cdrom/ember/bundle/. $T/opt/ember-setup/
cp -a /cdrom/ember/debs $T/opt/ember-setup/debs
mv $T/opt/ember-setup/debs/ember-index.gz $T/opt/ember-setup/debs/Packages.gz
cp /cdrom/ember/ember-image.tar.gz $T/opt/ember-setup/
# apt reads the package folder as its unprivileged _apt user; the secrets stay root-only.
chmod 711 $T/opt/ember-setup
chmod -R a+rX $T/opt/ember-setup/debs
chmod 600 $T/opt/ember-setup/secrets.env $T/opt/ember-setup/hotspot.env
cp /cdrom/ember/ember-firstboot.service $T/etc/systemd/system/
ln -sf /etc/systemd/system/ember-firstboot.service \
    $T/etc/systemd/system/multi-user.target.wants/ember-firstboot.service
