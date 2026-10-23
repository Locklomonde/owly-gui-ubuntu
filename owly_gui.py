#!/usr/bin/python3
"""User-session GTK frontend for the installed Owly CLI."""
import codecs
import concurrent.futures
import http.client
import ipaddress
import plistlib
import socket
import ssl
import sys
import errno
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import urllib.error
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk


RELEASES_URL = 'https://github.com/t4t5/owly/releases'
RELEASE_API = 'https://api.github.com/repos/t4t5/owly/releases/latest'


def version_key(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value.strip())
    if not match:
        raise ValueError('Unrecognized version format')
    return tuple(int(part) for part in match.groups())


def check_owly_release(binary, fetch=None):
    if not binary:
        raise ValueError('Owly is not installed or could not be found.')
    result = subprocess.run([binary, '--version'], capture_output=True, text=True,
                            timeout=5, check=True)
    match = re.search(r'\bowly\s+(v?\d+\.\d+\.\d+)(?=\s|$)', result.stdout.strip())
    if not match:
        raise ValueError('Could not determine the installed Owly version.')
    installed = match.group(1)
    if fetch is None:
        def fetch():
            request = urllib.request.Request(RELEASE_API, headers={
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'Owly-Native-Update-Check',
                'X-GitHub-Api-Version': '2022-11-28'})
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError('The update server returned an unexpected response.')
            return json.loads(raw)
    release = fetch()
    if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
        raise ValueError('No stable release information was returned.')
    latest = release.get('tag_name')
    if not isinstance(latest, str):
        raise ValueError('The update server did not provide a version.')
    current_key, latest_key = version_key(installed), version_key(latest)
    if latest_key > current_key:
        return f'Owly {latest.lstrip("v")} is available. You have {installed.lstrip("v")}.', True
    if latest_key == current_key:
        return f'Owly {installed.lstrip("v")} is up to date with the latest stable release.', False
    return f'Owly {installed.lstrip("v")} is newer than the latest stable release ({latest.lstrip("v")}).', False


def peer_ip(mac):
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
        raise ValueError('Invalid peer address')
    parts = bytearray.fromhex(mac.replace(':', ''))
    parts[0] ^= 2
    interface_id = bytes(parts[:3]) + b'\xff\xfe' + bytes(parts[3:])
    return str(ipaddress.IPv6Address(int(ipaddress.IPv6Address('fe80::')) | int.from_bytes(interface_id, 'big')))


def discover_peer(mac, timeout=3):
    """Use AirDrop's /Discover only. Never ask for approval or upload data."""
    address = peer_ip(mac)
    scope = socket.if_nametoindex('awdl0')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # AirDrop receivers use self-signed certificates; match Owly's TLS behavior.
    # This context is used only for link-local peers on awdl0, never for updates.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    body = plistlib.dumps({}, fmt=plistlib.FMT_BINARY)
    request = (f'POST /Discover HTTP/1.1\r\nHost: [{address}%awdl0]:8770\r\n'
               'User-Agent: AirDrop/1.0\r\nContent-Type: application/octet-stream\r\n'
               f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as raw:
        raw.settimeout(timeout)
        raw.connect((address, 8770, 0, scope))
        with context.wrap_socket(raw, server_hostname='airdrop.local') as stream:
            stream.settimeout(max(.1, deadline - time.monotonic()))
            stream.sendall(request)
            response = http.client.HTTPResponse(stream)
            response.begin()
            if response.status != 200:
                return None
            payload = bytearray()
            while len(payload) <= 65536:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Discovery timed out')
                stream.settimeout(remaining)
                chunk = response.read1(min(8192, 65537 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > 65536:
                raise ValueError('Discovery reply too large')
            info = plistlib.loads(payload)
            name = info.get('ReceiverComputerName') if isinstance(info, dict) else None
            if not isinstance(name, str) or not name.strip():
                return None
            return name, f'[{address}%{scope}]:8770'


def find_devices(binary):
    """Run inside an owned process group so Stop covers all discovery work."""
    # Browse holds Owly's BLE advertisement while direct discovery retries.
    # Some iPhones need around 15 seconds before opening the AirDrop port.
    browse = subprocess.Popen([binary, 'browse', '--seconds', '30'])
    found = set()
    deadline = time.monotonic() + 30
    last_error = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            while time.monotonic() < deadline:
                peers = subprocess.run([binary, 'radio', 'peers'], capture_output=True,
                                       text=True, timeout=3)
                if peers.returncode:
                    print('Could not read radio peers: ' + peers.stderr.strip(), flush=True)
                    return 1
                macs = list(dict.fromkeys(re.findall(r'(?m)^((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\s', peers.stdout)))
                pending = {pool.submit(discover_peer, mac): mac for mac in macs[:16] if mac not in found}
                for future in concurrent.futures.as_completed(pending):
                    mac = pending[future]
                    try:
                        receiver = future.result()
                        if receiver:
                            name, address = receiver
                            print(f'mac:{mac} {json.dumps(name)} ({address})', flush=True)
                            found.add(mac)
                    except (OSError, ValueError, http.client.HTTPException) as exc:
                        last_error = str(exc)
                # Keep scanning for other devices until the window ends.
                time.sleep(.7)
        if not found:
            print('Direct discovery: radio peers did not return receiver names.' if last_error else
                  'Direct discovery: no named AirDrop receiver found.', flush=True)
        return 0
    finally:
        if browse.poll() is None:
            browse.send_signal(signal.SIGINT)
            try:
                browse.wait(timeout=2)
            except subprocess.TimeoutExpired:
                browse.kill()
                browse.wait()


class DeviceParser:
    """Recognize complete CLI discovery rows, independently of PTY read sizes."""
    ROW = re.compile(r'^(\S+)\s+("(?:\\.|[^"\\])*") \(([^\r\n]+)\)$')

    def __init__(self, found):
        self.buffer = ''
        self.found = found

    def feed(self, text):
        self.buffer += text.replace('\r', '')
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            match = self.ROW.fullmatch(line)
            if match:
                ident, name, address = match.groups()
                try:
                    name = json.loads(name)
                except ValueError:
                    name = name[1:-1]  # Keep Rust debug escapes visible if JSON cannot decode them.
                self.found(ident, name or 'Unnamed device', address)
        if len(self.buffer) > 65536:
            self.buffer = ''


class ApprovalParser:
    """Parse only Owly's receive prompt; tolerate arbitrary PTY chunk boundaries."""
    PROMPT = 'accept? [y/N] '

    def __init__(self, show, clear):
        self.buffer = ''
        self.offer = None
        self.show, self.clear = show, clear

    def feed(self, text, received_at=None):
        self.buffer += text.replace('\r', '')
        while self.buffer:
            end = self.buffer.find('\n')
            prompt = self.buffer.find(self.PROMPT)
            if prompt >= 0 and (end < 0 or prompt < end):
                self.buffer = self.buffer[prompt + len(self.PROMPT):]
                if self.offer:
                    self.show(self.offer, received_at or time.monotonic())
                self.offer = None
            elif end >= 0:
                line, self.buffer = self.buffer[:end], self.buffer[end + 1:]
                if line.endswith(' wants to send:') and not line.startswith(' '):
                    self.clear()
                    self.offer = [line[:-len(' wants to send:')]]
                elif self.offer is not None and line.startswith('  '):
                    self.offer.append(line.strip())
                elif line.strip() in ('timed out', 'the request was withdrawn'):
                    self.offer = None
                    self.clear()
            else:
                # Never retain an unbounded line or interpret truncated text as a prompt.
                if len(self.buffer) > 65536:
                    self.buffer = ''
                    self.offer = None
                break


class Job:
    def __init__(self, argv, output, finished):
        master, slave = pty.openpty()
        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                         start_new_session=True, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd = master
        self.stopping = False
        self.done = threading.Event()
        self.output, self.finished = output, finished
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        ended_at = None
        try:
            while True:
                if select.select([self.fd], [], [], .15)[0]:
                    try:
                        chunk = os.read(self.fd, 8192)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    GLib.idle_add(self.output, decoder.decode(chunk), time.monotonic())
                if self.proc.poll() is not None:
                    ended_at = ended_at or time.monotonic()
                    if time.monotonic() - ended_at > .5:
                        break
        finally:
            # Clean up descendants that inherited the terminal, even if the leader exited.
            self.kill(signal.SIGKILL)
            code = self.proc.wait()
            os.close(self.fd)
            self.done.set()
            GLib.idle_add(self.finished, self, code)

    def kill(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def stop(self):
        if self.stopping or self.done.is_set():
            return
        self.stopping = True
        self.kill(signal.SIGINT)
        def escalate():
            if not self.done.wait(3):
                self.kill(signal.SIGTERM)
                if not self.done.wait(2):
                    self.kill(signal.SIGKILL)
        threading.Thread(target=escalate, daemon=True).start()

    def reply(self, text):
        if not self.stopping and not self.done.is_set():
            try:
                os.write(self.fd, (text + '\n').encode())
            except OSError:
                pass


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id='local.owly.Native', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.jobs = {}
        self.closing = False
        self.devices = {}
        self.device_parser = DeviceParser(self.device_found)
        self.approval_job = None
        self.approval_deadline = 0
        self.approval_timer = None
        self.parser = ApprovalParser(self.show_approval, self.clear_approval)
        self.owly = shutil.which('owly')
        self.connect('activate', self.activate)

    def activate(self, *_):
        if self.get_active_window():
            self.get_active_window().present()
            return
        self.win = Adw.ApplicationWindow(application=self, title='Owly AirDrop', default_width=780, default_height=700)
        self.win.connect('close-request', self.close)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_start=20, margin_end=20, margin_bottom=20)
        box.append(Adw.HeaderBar())
        title = Gtk.Label(label='AirDrop for Ubuntu', xalign=0)
        title.add_css_class('title-1')
        box.append(title)
        self.status = Gtk.Label(label='Radio off', xalign=0)
        box.append(self.status)
        row = Gtk.Box(spacing=12)
        row.append(Gtk.Label(label='AirDrop radio', hexpand=True, xalign=0))
        self.radio = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.radio.connect('notify::active', self.toggle_radio)
        row.append(self.radio)
        box.append(row)
        self.folder = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or str(Path.home() / 'Downloads'))
        self.folder_button = Gtk.Button(label=f'Receive folder: {self.folder}')
        self.folder_button.connect('clicked', lambda *_: self.pick(True))
        box.append(self.folder_button)
        box.append(Gtk.Label(label='Send to', xalign=0))
        self.target = Gtk.ComboBoxText()
        self.target.set_tooltip_text('Choose a device found by Find devices')
        self.target.connect('changed', lambda *_: self.refresh())
        box.append(self.target)
        self.device_status = Gtk.Label(label='Turn on the radio, then click Find devices.', xalign=0, wrap=True)
        box.append(self.device_status)
        row = Gtk.Box(spacing=8, homogeneous=True)
        self.buttons = {}
        for name, callback in [('Receive', lambda *_: self.receive()), ('Send files…', lambda *_: self.pick(False)),
                               ('Find devices', lambda *_: self.start('browse', ['browse'])),
                               ('Diagnostics', lambda *_: self.start('doctor', ['doctor'])),
                               ('Stop', lambda *_: self.stop_all())]:
            button = Gtk.Button(label=name)
            button.connect('clicked', callback)
            self.buttons[name] = button
            row.append(button)
        box.append(row)
        self.incoming_status = Gtk.Label(label='Turn on the radio, then choose Receive.', xalign=0)
        box.append(self.incoming_status)
        self.approval_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                                    margin_top=8, margin_bottom=8)
        self.approval_card.add_css_class('card')
        self.sender_label = Gtk.Label(xalign=0, wrap=True, selectable=True,
                                      margin_start=16, margin_end=16, margin_top=16)
        self.sender_label.add_css_class('title-3')
        self.approval_card.append(self.sender_label)
        self.offer_label = Gtk.Label(xalign=0, wrap=True, selectable=True, margin_start=16, margin_end=16)
        self.approval_card.append(self.offer_label)
        self.countdown = Gtk.Label(xalign=0, margin_start=16, margin_end=16)
        self.approval_card.append(self.countdown)
        row = Gtk.Box(spacing=10, halign=Gtk.Align.END, margin_start=16, margin_end=16, margin_bottom=16)
        for label, accept in [('Deny', False), ('Accept', True)]:
            button = Gtk.Button(label=label)
            button.add_css_class('suggested-action' if accept else 'destructive-action')
            button.connect('clicked', lambda _, value=accept: self.decide(value))
            row.append(button)
        self.approval_card.append(row)
        self.approval_card.set_visible(False)
        box.append(self.approval_card)
        self.log = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=220)
        scroll.set_child(self.log)
        self.details = Gtk.Expander(label='Activity and diagnostics', vexpand=True)
        self.details.set_child(scroll)
        box.append(self.details)
        button = Gtk.Button(label='Open receive folder')
        button.connect('clicked', lambda *_: Gio.AppInfo.launch_default_for_uri(self.folder.as_uri(), None))
        box.append(button)
        self.update_button = Gtk.Button(label='Check for Owly updates')
        self.update_button.connect('clicked', self.check_updates)
        box.append(self.update_button)
        self.update_status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.append(self.update_status)
        self.release_link = Gtk.LinkButton(uri=RELEASES_URL, label='Open official Owly releases')
        self.release_link.set_visible(False)
        box.append(self.release_link)
        self.win.set_content(box)
        self.refresh()
        if not self.owly:
            self.write('Owly was not found. Install/configure the CLI before using this app.\n')
        self.win.present()

    def check_updates(self, *_):
        if not self.update_button.get_sensitive():
            return
        self.update_button.set_sensitive(False)
        self.release_link.set_visible(False)
        self.update_status.set_text('Checking the creator’s latest stable Owly release…')
        def worker():
            try:
                message, available = check_owly_release(self.owly)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    message = 'GitHub’s request limit was reached. Try again later or open releases.'
                elif exc.code == 404:
                    message = 'No latest stable release was found. You can check the releases page.'
                else:
                    message = 'The update service is unavailable. Try again later.'
                available = True
            except (urllib.error.URLError, TimeoutError, OSError):
                message, available = 'Could not reach the update service. Check your connection and try again.', True
            except (ValueError, subprocess.SubprocessError):
                message, available = 'Could not compare Owly versions. You can check the official releases page.', True
            GLib.idle_add(self.update_result, message, available)
        threading.Thread(target=worker, daemon=True).start()

    def update_result(self, message, show_link):
        if not self.closing:
            self.update_status.set_text(message)
            self.release_link.set_visible(show_link)
            self.update_button.set_sensitive(True)
        return False

    def write(self, text):
        # Keep the live log bounded. Strip terminal controls that GTK does not interpret.
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text).replace('\r', '')
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        if buf.get_char_count() > 160000:
            buf.delete(buf.get_start_iter(), buf.get_iter_at_offset(buf.get_char_count() - 120000))
        self.log.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0)
        return False

    def refresh(self):
        running = 'radio' in self.jobs and not self.jobs['radio'].stopping
        if 'receive' in self.jobs and not self.jobs['receive'].stopping:
            self.incoming_status.set_text('Waiting for incoming transfers…')
        elif running:
            self.incoming_status.set_text('Choose Receive to allow incoming requests.')
        else:
            self.incoming_status.set_text('Turn on the radio, then choose Receive.')
        self.status.set_text('Active: ' + ', '.join(self.jobs) if self.jobs else 'Radio off · Ready')
        for label, key in [('Receive', 'receive'), ('Send files…', 'send'), ('Find devices', 'browse')]:
            self.buttons[label].set_sensitive(bool(running and key not in self.jobs and not self.closing))
        self.buttons['Send files…'].set_sensitive(bool(running and 'send' not in self.jobs and self.target.get_active_id() and not self.closing))
        self.target.set_sensitive(bool(running and 'send' not in self.jobs and not self.closing))
        self.buttons['Diagnostics'].set_sensitive(bool(self.owly and 'doctor' not in self.jobs and not self.closing))
        self.buttons['Stop'].set_sensitive(bool(self.jobs))
        self.radio.set_sensitive(bool(self.owly and not self.closing and not ('radio' in self.jobs and self.jobs['radio'].stopping)))
        self.folder_button.set_sensitive('receive' not in self.jobs and not self.closing)

    def start(self, key, args):
        if not self.owly or self.closing or key in self.jobs:
            return
        if key == 'browse':
            self.clear_devices()
            self.device_status.set_text('Looking for devices (about 30 seconds)… Keep the receiving device unlocked with AirDrop set to Everyone.')
        try:
            self.write(f'\n[{key}] Starting\n')
            command = ([sys.executable, str(Path(__file__).resolve()), '--find-devices', self.owly]
                       if key == 'browse' else [self.owly] + args)
            job = Job(command,
                      lambda text, stamp: self.output(key, text, stamp),
                      lambda job, code: self.finished(key, job, code))
            self.jobs[key] = job
            if key == 'receive':
                self.parser = ApprovalParser(self.show_approval, self.clear_approval)
            if key == 'doctor':
                self.details.set_expanded(True)
        except OSError as exc:
            self.write(f'[{key}] Could not start: {exc}\n')
            if key == 'radio':
                self.radio.set_active(False)
        self.refresh()

    def finished(self, key, job, code):
        if key == 'browse':
            self.device_parser.feed('\n')
            if job.stopping:
                self.device_status.set_text('Device search stopped.')
            elif code != 0:
                self.device_status.set_text('Device search failed. Open Activity and diagnostics for details, then try again.')
                self.details.set_expanded(True)
            elif self.devices:
                self.device_status.set_text(f'{len(self.devices)} device(s) found. Select one above, then choose Send files…')
            else:
                self.device_status.set_text('No devices found. Unlock the receiving device, set AirDrop to Everyone, then try Find devices again.')
        if key == 'receive':
            self.clear_approval()
        if self.jobs.get(key) is job:
            del self.jobs[key]
        self.write(f'\n[{key}] ' + ('Stopped' if job.stopping else f'Exited ({code})') + '\n')
        if key == 'radio':
            self.radio.set_active(False)
            for k, j in list(self.jobs.items()):
                if k != 'doctor':
                    j.stop()
        self.refresh()
        if self.closing and not self.jobs:
            self.win.destroy()
            self.quit()
        return False

    def toggle_radio(self, switch, *_):
        if switch.get_active():
            self.start('radio', ['radio', 'up'])
        else:
            self.stop_all()

    def stop_all(self):
        self.clear_devices()
        self.clear_approval()
        for job in list(self.jobs.values()):
            job.stop()
        self.radio.set_active(False)
        self.refresh()

    def receive(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.start('receive', ['receive', '--dir', str(self.folder)])
        except OSError as exc:
            self.write(f'Cannot open receive folder: {exc}\n')

    def pick(self, folder):
        target = self.target.get_active_id()
        if not folder and not target:
            self.device_status.set_text('Click Find devices, then select the device you want to send to.')
            self.target.grab_focus()
            return
        dialog = Gtk.FileChooserNative.new('Choose receive folder' if folder else 'Choose files to send', self.win,
            Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN, 'Choose', 'Cancel')
        dialog.set_select_multiple(not folder)
        def selected(d, response):
            if response == Gtk.ResponseType.ACCEPT and not self.closing:
                if folder and 'receive' not in self.jobs:
                    path = d.get_file().get_path()
                    if path:
                        self.folder = Path(path)
                        self.folder_button.set_label(f'Receive folder: {self.folder}')
                elif not folder and 'radio' in self.jobs and not self.jobs['radio'].stopping:
                    files = d.get_files()
                    paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
                    if paths and all(paths) and target and target in self.devices:
                        option, destination = ('--mac', target[4:]) if target.startswith('mac:') else ('--to', target)
                        self.start('send', ['send', option, destination, '--'] + paths)
            d.destroy()
        dialog.connect('response', selected)
        dialog.show()

    def clear_devices(self):
        self.devices.clear()
        self.target.remove_all()
        self.device_parser = DeviceParser(self.device_found)
        self.device_status.set_text('Click Find devices to look for receivers.')

    def device_found(self, ident, name, address):
        if ident in self.devices:
            return
        def host(value):
            return value.split(']')[0].lstrip('[').split('%')[0]
        if any(host(existing[1]) == host(address) for existing in self.devices.values()):
            return
        self.devices[ident] = (name, address)
        # Keep the CLI service ID as the hidden value, so duplicate display names
        # still target the exact discovered receiver.
        self.target.append(ident, f'{name} · {address}')
        self.device_status.set_text(f'{len(self.devices)} device(s) found. Select a device to send files.')
        self.refresh()

    def output(self, key, text, stamp):
        clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
        if key == 'receive':
            self.parser.feed(clean, stamp)
        elif key == 'browse':
            self.device_parser.feed(clean)
        self.write(f'[{key}] {clean}')
        if 'transfer failed:' in clean:
            self.details.set_expanded(True)
        return False

    def show_approval(self, offer, stamp):
        job = self.jobs.get('receive')
        # Leave a margin before the CLI's 15-second timeout, avoiding a stale response
        # in stdin that could approve the next request.
        deadline = stamp + 13
        if not job or job.stopping or deadline <= time.monotonic():
            return
        self.clear_approval()
        self.approval_job = job
        self.approval_deadline = deadline
        name = offer[0]
        match = re.fullmatch(r'("(?:\\.|[^"\\])*") \(("(?:\\.|[^"\\])*")\)', name)
        if match:
            try:
                sender, model = (json.loads(value) for value in match.groups())
                name = (sender or 'Unknown device') + (f' ({model})' if model else '')
            except ValueError:
                pass
        self.sender_label.set_text(name + ' wants to send')
        self.offer_label.set_text('\n'.join(offer[1:]) or 'Incoming transfer')
        self.approval_card.set_visible(True)
        self.details.set_expanded(False)
        self.tick_approval()
        self.approval_timer = GLib.timeout_add(200, self.tick_approval)

    def tick_approval(self):
        remaining = self.approval_deadline - time.monotonic()
        if not self.approval_job or remaining <= 0:
            self.approval_timer = None
            self.clear_approval()
            return False
        self.countdown.set_text(f'Respond within {max(1, int(remaining))} seconds · otherwise declined')
        return True

    def clear_approval(self):
        if self.approval_timer is not None:
            GLib.source_remove(self.approval_timer)
            self.approval_timer = None
        self.approval_job = None
        if hasattr(self, 'approval_card'):
            self.approval_card.set_visible(False)

    def decide(self, accept):
        job = self.approval_job
        valid = (job is not None and job is self.jobs.get('receive') and
                 not job.stopping and not job.done.is_set() and
                 time.monotonic() < self.approval_deadline)
        self.clear_approval()
        if valid:
            job.reply('y' if accept else 'n')
            self.write('Incoming transfer accepted.\n' if accept else 'Incoming transfer denied.\n')

    def close(self, *_):
        if not self.jobs:
            return False
        self.closing = True
        self.write('Closing: waiting for owned processes to stop…\n')
        self.stop_all()
        return True


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--find-devices':
        try:
            sys.exit(find_devices(sys.argv[2]))
        except KeyboardInterrupt:
            sys.exit(130)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f'Device search failed: {exc}', flush=True)
            sys.exit(1)
    else:
        App().run()
