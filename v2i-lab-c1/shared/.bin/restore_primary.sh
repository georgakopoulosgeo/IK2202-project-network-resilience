#!/bin/bash
tc filter del dev eth1 parent 1: prio 1 2>/dev/null
tc qdisc del dev eth1 ingress 2>/dev/null
tc filter show dev eth1 parent 1: | grep -q matchall && exit 1
echo RESTORED
