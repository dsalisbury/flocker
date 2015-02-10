#!/bin/sh

# Pre-cache downloads.

set -e

# We indirectly ask Docker to write a very large file to its temporary
# directory.  /tmp is a small tmpfs mount which can't hold the file.  Convince
# Docker to write somewhere else instead.
echo "# Flocker-defined alternate temporary path to provide more temporary space." >> /etc/sysconfig/docker
echo "TMPDIR=/var/tmp" >> /etc/sysconfig/docker

# Restart docker to ensure that it picks up the new tmpdir configuration.
systemctl restart docker

for image in busybox clusterhq/mongodb dockerfile/redis clusterhq/flask; do
    docker pull "${image}"
done

