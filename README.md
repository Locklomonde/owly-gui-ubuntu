# Owly Native

An experimental GTK4/Libadwaita companion interface for [Owly](https://github.com/t4t5/owly), an AirDrop implementation for Linux. This independent interface is not an official Owly application and does not bundle or replace Owly. Created with AI assistance and tested on Ubuntu with Owly 0.4.0.
## Credits

Powered by [Owly](https://github.com/t4t5/owly), created by [t4t5](https://github.com/t4t5) and its contributors.

This is an independent, experimental Ubuntu interface built with AI assistance. It is not an official Owly project.

## Features

- Radio toggle with shutdown of child processes owned by the application.
- Receive folder selection and incoming sender/file cards with Accept and Deny.
- Named receiver selection and a multi-file picker for sending.
- Expandable activity output, diagnostics and receive-folder shortcut.
- On-demand check for newer stable Owly releases. No automatic installs.

## Requirements and installation

Install Owly using its upstream instructions first. You also need system Python 3, PyGObject, GTK4 and Libadwaita. This installer checks these dependencies; it does not install packages or modify services, firewall rules or drivers.

From this folder:

```sh
sh install.sh
```

Launch **Owly AirDrop** from Applications. Do not use sudo. The installer places the Python app under `~/.local/share/owly-native/` and adds a desktop launcher under your user applications directory. Reinstallation backs up the installed Python file. Owly's existing authorization and hardware requirements still apply.

To run a development copy without installing:

```sh
/usr/bin/python3 owly_gui.py
```

Close other copies first: the application uses a single-instance application ID.

## Use

Enable the radio and allow startup to complete. Click **Receive** for incoming requests. Unanswered requests expire; automatic acceptance is not enabled.

For sending, unlock the receiving device, enable Wi-Fi and Bluetooth, set AirDrop to **Everyone for 10 Minutes**, and click **Find devices**. Select a receiver, then **Send files…**. Device names come from Owly's browse output; the app retains the corresponding service ID for sending. Addresses help distinguish identical names.

**Stop** and closing the application interrupt its child process groups, with terminate/kill fallback. Other Owly sessions are not managed by this app. An interrupted transfer may be incomplete.

## Current limitations

- Device discovery is experimental. Owly's `browse` path depends on mDNS advertisements; its send command also has a direct AWDL-peer discovery fallback that Browse does not use. An empty list can therefore occur even when a radio peer is visible. Radio peers alone do not establish a named, available receiver.
- VPN/local-network filters may interfere with discovery; investigate rather than assuming a VPN is the cause.
- The UI parses human-readable Owly 0.4.0 CLI output. Upstream formatting changes may require updates. There is no structured event API integration yet.
- Approval cards close shortly before Owly's 15-second timeout. Names are sender-reported, not verified identity.
- Routine logs stay in memory and can contain device names, addresses and filenames. Review and redact logs before sharing them.
- UI behavior has been checked on a GTK Broadway display; live discovery remains under investigation. There is no automated hardware transfer test suite.
- The update button checks upstream Owly, not this companion UI.

## Contributing

Issues and improvements are welcome. For discovery problems, report the Owly version, Ubuntu version, Wi-Fi hardware, whether `owly browse --seconds 20` finds the receiver with the radio active, and relevant redacted activity output. Never attach private keys or Owly's state directory.

Before submitting a change, run:

```sh
/usr/bin/python3 -m py_compile owly_gui.py
sh -n install.sh
```

Useful manual checks: receive Accept/Deny and timeout, split CLI output, duplicate device names, exact selected recipient, Stop/close cleanup, and offline update checks.

## Sharing and licensing

This source bundle contains only the companion interface and installer. Owly is a separate project; consult its own license for its code. Choose a license for this interface before inviting others to reuse or modify it. MIT is a common permissive option. No license has been selected on the owner's behalf in this bundle.

To uninstall, close the app and remove its user application folder and `owly-native.desktop` launcher. This does not uninstall Owly.
