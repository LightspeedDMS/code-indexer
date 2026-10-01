---
name: reference_staging_nfs_wedge_recovery
description: "How to recover a wedged cow-storage NFS mount on a staging cluster node (NFSv4.2 hang, reboot-hangs-on-unmount, vers=4.1 workaround)"
metadata: 
  node_type: memory
  type: reference
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

Large manual reads/writes against the staging cluster's shared cow-storage NFS export can wedge a client node's mount (a stray write hitting the pre-existing NFS permission-recovery failure -- see bug #1479 -- back-pressures all node I/O via the dirty-page cache; `statfs`/`ls` on the mount hang, load spikes, tasks stick in state `D`). Avoid multi-GB manual `cp`/`mv` on the NFS from a client node; the fleet-migration workload itself is fine.

Recovery procedure (proven 2026-07-26):
1. `systemctl reboot` on a node with a wedged NFS mount HANGS in shutdown (systemd blocks trying to unmount the dead NFS). The reboot may still complete after a long timeout, or you must reconnect and `reboot -f`.
2. After reboot the mount can STILL hang if the server holds stale NFSv4 client state. On the NFS-server node, per-client state lives in `/proc/fs/nfsd/clients/<id>/` (`info` shows the client address); force-expire the stuck client with `echo expire > /proc/fs/nfsd/clients/<id>/ctl`. Stuck `nfsd` worker threads (state `D`) clear once its state is released.
3. Diagnose client-vs-server by trying different NFS versions: `mount -t nfs -o vers=4.1` and `-o vers=3` succeeded while the default `vers=4.2` persistently hung on the recovered node -- an NFSv4.2 state-recovery hang specific to that node<->server pair. `showmount -e` / `rpcinfo` were clean (not RPC/mountd level).
4. Durable fix used: pin `vers=4.1` in the node's `/etc/fstab` cow-storage line (options `nfs4 vers=4.1,_netdev,soft,timeo=30,retrans=3`), `systemctl daemon-reload`, force-unmount the wedged mount (`umount -f`, then `umount -l` if busy -- stop cidx-server first to release handles), remount, verify a real golden-repo read, restart cidx-server.
5. The NFS-export host node accesses cow-storage via its LOCAL filesystem path (not an NFS loopback mount), so restarting `nfs-server` on it is safe for its own cidx-server -- but it briefly disrupts other clients' mounts and their in-flight NFS-touching jobs, so prefer the surgical per-client expire when another node is mid-migration.

This whole failure mode is staging-cluster-only. Production is solo/local-disk (no NFS), so it can never hit this. See [[project_nfs_host_down_hangs_systemd]] and [[project_local_server_solo_sqlite]].
