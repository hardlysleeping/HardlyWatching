# Using it from a phone

The app runs on one computer. A phone uses the same app by opening one address
in its browser, and can then install it to the home screen so it opens like an
app rather than a web page.

## Turning it on

On the computer: **Settings → Open on your phone → Reachable from your phone**.

That card shows the address to type on the phone, for example
`http://192.168.1.42:8765`, and a button to copy it. Open that on the phone,
then use the browser's *Add to Home Screen*.

The switch is stored in the app's state, so it survives a restart.

## What "reachable" means here

There is **no password anywhere in this app**. While the switch is on, anyone
on that network — the whole coffee-shop Wi-Fi, if that is where you are — can
open the app, change settings, queue downloads and export a backup (which
contains the Instagram login unless you exclude it). Turn it off when you are
not on a network you trust.

## Why the app listens even when the switch is off

The server binds every interface at startup, and the switch is enforced per
request:

- requests from this machine (loopback) are always served;
- requests from anywhere else get a 403 — an API error for `/api/*`, and a
  plain page pointing at the switch for everything else.

Binding per-setting instead would mean restarting the server whenever the
switch moved, which a phone cannot do from the outside. The cost of listening
with the gate closed is that the port is visible to a port scan; the cost of
rebinding would be a switch that needs a reboot to work.

`INSTA_HOST` still works and is still honoured for the address shown in the
card: `INSTA_HOST=127.0.0.1` turns the app back into a loopback-only server
whose switch cannot help, which is a sensible setting for a machine that should
never be reachable.

## Ports and firewalls

The port is 8765 unless `INSTA_PORT` says otherwise. Nothing in the app opens a
firewall: if the phone cannot connect while the switch is on, the host firewall
or the Wi-Fi network (guest networks and "AP isolation" both do this) is
refusing the connection, not the app. Serving the archive over a tailnet or a
reverse proxy works too — the app cannot tell the difference, and the switch
still applies.
