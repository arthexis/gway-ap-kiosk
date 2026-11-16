# Uplink recovery

The `gway-uplink-recovery.timer` periodically checks active NetworkManager IPv4
client uplinks with a default route. It excludes Wi-Fi AP mode and any gateway
which is also a local address. Checks bind HTTPS requests to each interface and
include a DNS-independent TLS endpoint. A successful external probe establishes
internet reachability for that check, not Watchtower or charger availability.

After three failed checks on the normally preferred route and a successful check
on another route, the service adds a temporary default route (metric 5, protocol
186) through the healthy alternative. It keeps NetworkManager profiles, IP
addresses and connected routes unchanged. It removes only its own route after four
successful preferred-path checks. If the selected alternate fails, it can replace
its own route with another proven uplink. When no alternative is proven, the
service does not change routes. A foreign default route of metric 5 or lower
prevents an override. Samples are about one minute apart; probe time adds delay.

This monitors IPv4 only. It does not change Wi-Fi credentials, restart NetworkManager,
restart WireGuard, correct a gateway/address conflict, attest Watchtower, or prove
physical failover. A recent WireGuard handshake is logged separately when the
`gway` interface exists; it is not an end-to-end MCP check. Probe endpoints are
external services and should not be treated as proof that all destinations work.

Inspect with `systemctl status gway-uplink-recovery.timer` and
`journalctl -u gway-uplink-recovery.service`. To stop recovery, disable the timer
and, if present, inspect and remove only the controller-owned default route:

```sh
sudo systemctl disable --now gway-uplink-recovery.timer
ip -4 route show default proto 186
# After checking the exact gateway and interface shown above:
sudo ip -4 route del default via <gateway> dev <interface> metric 5 proto 186
```

The state in `/run/gway-uplink-recovery` is temporary and contains only counters.
Port moves do not affect candidate selection: active NetworkManager client mode
is checked rather than a fixed Wi-Fi interface name. Saved profiles must still
match the adapter and autoconnect appropriately.
