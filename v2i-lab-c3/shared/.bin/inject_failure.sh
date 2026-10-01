#!/bin/bash
# Silent failure of the primary link (runs inside r1). Every frame on eth1 is
# dropped in both directions while the interface stays UP, so OSPF only notices
# when hellos stop arriving - like a failed microwave hop or intermediate switch.
# ('ip link set eth1 down' would be detected instantly by r1 via carrier loss,
#  making C1 look like C2.)
out=$1
t0=$(date +%s%N)
tc filter add dev eth1 parent 1: protocol all prio 1 matchall action drop || exit 1
t1=$(date +%s%N)          # from here on nothing leaves r1 through eth1
tc qdisc add dev eth1 handle ffff: ingress
tc filter add dev eth1 parent ffff: protocol all prio 1 matchall action drop || exit 1
printf '{"t_fail_start_ns": %s, "t_fail_end_ns": %s, "method": "tc matchall drop on r1 eth1 egress+ingress, link stays up"}\n' "$t0" "$t1" > "$out"
echo FAIL_INJECTED
