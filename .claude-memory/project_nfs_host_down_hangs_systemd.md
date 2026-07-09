---
name: project_nfs_host_down_hangs_systemd
description: When the shared CoW/NFS host node is down, hard NFS mounts on the OTHER cluster nodes hang systemctl daemon-reload and cascade into sudo/pam_systemd — every sudo blocks while non-sudo commands stay instant
metadata:
  type: project
---

In a dev cluster the golden-repos mount is a `hard` NFS mount (vers=3, nolock) to the CoW/NFS host node. When that host crashes or is unreachable, ANY access to the mount path blocks indefinitely (that is what `hard` means). `systemctl daemon-reload` walks mount units, so it hangs; a PID 1 stuck mid-reload leaves dbus/logind unresponsive, so `pam_systemd` (invoked by every `sudo`) blocks too. Net symptom: the node's `sudo` and `systemctl` operations appear wedged (timing out 30-60s+), while plain non-sudo commands run instantly.

This surfaced as a sudo/daemon-reload timeout during an auto-update deploy; it was misdiagnosed as CPU contention from the heavy build until the real cause emerged — the CoW/NFS host had crashed. It recovers on its own the instant the host returns (the `hard` mount unblocks). A longer command timeout does NOT save a truly-down `hard` mount (it blocks forever) — only host recovery does.

Diagnose WITHOUT sudo, wrapping each probe in `timeout`: `cat /proc/mounts | grep nfs`, `systemctl is-system-running`, `systemctl list-jobs`, `stat` the mount dir. The CoW/NFS host is a deliberate single point of failure in dev clusters (accepted). See [[project_cluster_auto_updater_service]], [[reference_cow_daemon_architecture]], [[feedback_study_anomalies_deeply]].
