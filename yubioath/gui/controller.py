# Copyright (c) 2014 Yubico AB
# All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# Additional permission under GNU GPL version 3 section 7
#
# If you modify this program, or any covered work, by linking or
# combining it with the OpenSSL project's OpenSSL library (or a
# modified version of that library), containing parts covered by the
# terms of the OpenSSL or SSLeay licenses, We grant you additional
# permission to convey the resulting work. Corresponding Source for a
# non-source form of such a combination shall include the source code
# for the parts of OpenSSL used as well as that of the covered work.

from ..core.standard import YubiOathCcid
from ..core.controller import Controller
from ..core.exc import CardError, DeviceLockedError
from .ccid import CardStatus
from .view.get_password import GetPasswordDialog
from .keystore import get_keystore
from . import messages as m
from yubioath.yubicommon.qt import get_active_window, MutexLocker
from PySide import QtCore, QtGui
from time import time
from collections import namedtuple

import sys
if sys.platform == 'win32':  # Windows has issues with the high level API.
    from .ccid_poll import observe_reader
else:
    from .ccid import observe_reader


class CredentialType:
    AUTO, HOTP, TOUCH, INVALID = range(4)


Code = namedtuple('Code', 'code timestamp')
UNINITIALIZED = Code('', 0)

TIME_PERIOD = 30


class Credential(QtCore.QObject):
    changed = QtCore.Signal()

    def __init__(self, name, cred_type):
        super(Credential, self).__init__()
        self.name = name
        self.cred_type = cred_type
        self._code = UNINITIALIZED

    @property
    def code(self):
        return self._code

    @code.setter
    def code(self, value):
        self._code = value
        self.changed.emit()


class BoundCredential(Credential):

    def __init__(self, controller, name, cred_type):
        super(BoundCredential, self).__init__(name, cred_type)
        self._controller = controller

    def delete(self):
        self._controller.delete_cred(self.name)


class AutoCredential(BoundCredential):

    def __init__(self, controller, name, code):
        super(AutoCredential, self).__init__(
            controller, name, CredentialType.AUTO)
        self._code = code


class TouchCredential(BoundCredential):

    def __init__(self, controller, name, slot, digits):
        super(TouchCredential, self).__init__(
            controller, name, CredentialType.TOUCH)

        self._slot = slot
        self._digits = digits

    def calculate(self):
        dialog = QtGui.QMessageBox(get_active_window())
        dialog.setWindowTitle(m.touch_title)
        dialog.setStandardButtons(QtGui.QMessageBox.NoButton)
        dialog.setIcon(QtGui.QMessageBox.Information)
        dialog.setText(m.touch_desc)

        def cb(code):
            self.code = code
            dialog.accept()
        self._controller._app.worker.post_bg(
            (self._controller._calculate_touch, self._slot, self._digits),
            cb)
        dialog.exec_()


class HotpCredential(BoundCredential):

    def __init__(self, controller, cred, name):
        super(HotpCredential, self).__init__(
            controller, name, CredentialType.HOTP)
        self._cred = cred

    def calculate(self):
        def cb(code):
            self.code = code
        self._controller._app.worker.post_bg(
            (self._controller._calculate_hotp, self._cred),
            cb)


def names(creds):
    return set(map(lambda c: c.name, creds))


class Timer(QtCore.QObject):
    time_changed = QtCore.Signal(int)

    def __init__(self):
        super(Timer, self).__init__()

        now = time()
        rem = now % TIME_PERIOD
        QtCore.QTimer.singleShot((TIME_PERIOD - rem) * 1000, self.start_timer)
        self._time = int(now - rem)

    def start_timer(self):
        self.startTimer(TIME_PERIOD * 1000)
        self.timerEvent(QtCore.QEvent(QtCore.QEvent.None))

    def timerEvent(self, event):
        self._time += TIME_PERIOD
        self.time_changed.emit(self._time)
        event.accept()

    @property
    def time(self):
        return self._time


class GuiController(QtCore.QObject, Controller):
    refreshed = QtCore.Signal()

    def __init__(self, app, settings):
        super(GuiController, self).__init__()
        self._app = app
        self._settings = settings
        self._needs_read = False
        self._reader = None
        self._creds = None
        self._lock = QtCore.QMutex()
        self._keystore = get_keystore()
        self.timer = Timer()

        self.watcher = observe_reader(self.reader_name, self._on_reader)

        self.startTimer(2000)
        self.timer.time_changed.connect(self.refresh_codes)

    @property
    def reader_name(self):
        return self._settings.get('reader', 'Yubikey')

    @property
    def slot1(self):
        return self._settings.get('slot1', 0)

    @property
    def slot2(self):
        return self._settings.get('slot2', 0)

    def unlock(self, dev):
        if dev.locked:
            key = self._keystore.get(dev.id)
            if not key:
                self._app.worker.post_fg((self._init_dev, dev))
                return False
            dev.unlock(key)
        return True

    def grab_lock(self, lock=None, try_lock=False):
        return lock or MutexLocker(self._lock, False).lock(try_lock)

    def read_slot_otp_touch(self, cred, timestamp):
        return (cred, 'TIMEOUT')

    @property
    def otp_enabled(self):
        return self.otp_supported and bool(self.slot1 or self.slot2)

    @property
    def credentials(self):
        return self._creds

    def _on_reader(self, watcher, reader):
        if reader:
            if self._reader is None:
                self._reader = reader
                self._creds = []
                if not self._app.window.isVisible():
                    self._needs_read = True
                else:
                    ccid_dev = watcher.open()
                    if ccid_dev:
                        dev = YubiOathCcid(ccid_dev)
                        self._app.worker.post_fg((self._init_dev, dev))
                    else:
                        self._needs_read = True
            elif self._needs_read:
                self.refresh_codes(self.timer.time)
        else:
            self._reader = None
            self._creds = None
            self._expires = 0
            self.refreshed.emit()

    def _init_dev(self, dev):
        _lock = self.grab_lock()
        while dev.locked:
            if self._keystore.get(dev.id) is None:
                dialog = GetPasswordDialog(get_active_window())
                if dialog.exec_():
                    self._keystore.put(dev.id,
                                       dev.calculate_key(dialog.password),
                                       dialog.remember)
                else:
                    return
            try:
                dev.unlock(self._keystore.get(dev.id))
            except CardError:
                self._keystore.delete(dev.id)
        self.refresh_codes(self.timer.time, _lock)

    def _await(self):
        self._creds = None

    def wrap_credential(self, tup):
        (cred, code) = tup
        if code == 'INVALID':
            return Credential(cred.name, CredentialType.INVALID)
        if code == 'TIMEOUT':
            return TouchCredential(self, cred.name, cred._slot, cred._digits)
        if code is None:
            return HotpCredential(self, cred, cred.name)
        else:
            return AutoCredential(self, cred.name, Code(code, self.timer.time))

    def _set_creds(self, creds):
        if creds:
            creds = map(self.wrap_credential, creds)
            if self._creds and names(creds) == names(self._creds):
                creds = dict((c.name, c) for c in creds)
                for cred in self._creds:
                    if cred.cred_type == CredentialType.AUTO:
                        cred.code = creds[cred.name].code
                return
            elif self._reader and self._needs_read and self._creds:
                return
        self._creds = creds
        self.refreshed.emit()

    def _calculate_touch(self, slot, digits):
        _lock = self.grab_lock()
        legacy = self.open_otp()
        if not legacy:
            raise ValueError('YubiKey removed!')

        now = time()
        timestamp = self.timer.time
        if timestamp + TIME_PERIOD - now < 10:
            timestamp += TIME_PERIOD
        cred = self.read_slot_otp(legacy, slot, digits, timestamp, True)
        cred, code = super(GuiController, self).read_slot_otp_touch(cred[0],
                                                                    timestamp)
        return Code(code, timestamp)

    def _calculate_hotp(self, cred):
        _lock = self.grab_lock()
        ccid_dev = self.watcher.open()
        if not ccid_dev:
            if self.watcher.status != CardStatus.Present:
                self._set_creds(None)
            return
        dev = YubiOathCcid(ccid_dev)
        if self.unlock(dev):
            return Code(dev.calculate(cred.name, cred.oath_type), float('inf'))

    def refresh_codes(self, timestamp=None, lock=None):
        if not self._reader:
            return self._on_reader(self.watcher, self.watcher.reader)
        elif not self._app.window.isVisible():
            self._needs_read = True
            return
        lock = self.grab_lock(lock, True)
        if not lock:
            return
        device = self.watcher.open()
        self._needs_read = bool(self._reader and device is None)
        timestamp = timestamp or self.timer.time
        try:
            creds = self.read_creds(device, self.slot1, self.slot2, timestamp)
        except DeviceLockedError:
            creds = []
        self._set_creds(creds)

    def timerEvent(self, event):
        if self._app.window.isVisible():
            if self._reader and self._needs_read:
                self._app.worker.post_bg(self.refresh_codes)
            elif self._reader is None and self._creds is None \
                    and self.otp_enabled:
                _lock = self.grab_lock()
                timestamp = self.timer.time
                read = self.read_creds(None, self.slot1, self.slot2, timestamp)
                if read is not None and self._reader is None:
                    self._set_creds(read)
        event.accept()

    def add_cred(self, *args, **kwargs):
        lock = self.grab_lock()
        dev = YubiOathCcid(self.watcher.open())
        if self.unlock(dev):
            dev.put(*args, **kwargs)
            self._creds = None
            self.refresh_codes(lock=lock)

    def delete_cred(self, name):
        if name in ['YubiKey slot 1', 'YubiKey slot 2']:
            raise NotImplementedError('Deleting YubiKey slots not implemented')
        lock = self.grab_lock()
        dev = YubiOathCcid(self.watcher.open())
        if dev.locked:
            self.unlock(dev)
        dev.delete(name)
        self._creds = None
        self.refresh_codes(lock=lock)

    def set_password(self, password, remember=False):
        _lock = self.grab_lock()
        dev = YubiOathCcid(self.watcher.open())
        if self.unlock(dev):
            key = dev.calculate_key(password)
            dev.set_key(key)
            self._keystore.put(dev.id, key, remember)