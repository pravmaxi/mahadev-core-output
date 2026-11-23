"""Local, atomic storage primitives for future Paper Trading profiles.

This module is deliberately not wired into the running engine yet. A profile
contains a single coherent snapshot; it does not start scans or execute trades.
"""
import json
import os
import re
import tempfile
import threading
from pathlib import Path

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_LOCK = threading.RLock()
VERSION = 1


class ProfileError(ValueError):
    pass


def _path(directory, name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ProfileError("Profile name must be 1–64 letters, digits, underscores or hyphens")
    return Path(directory) / (name + '.json')


def _validate(data):
    if not isinstance(data, dict) or data.get('version') != VERSION:
        raise ProfileError('Invalid or unsupported profile version')
    for key, typ in (('config', dict), ('state', dict), ('trades', list), ('ui', dict)):
        if not isinstance(data.get(key), typ):
            raise ProfileError('Invalid profile field: ' + key)
    return data


def load(directory, name):
    path = _path(directory, name)
    with _LOCK:
        try:
            with path.open('r', encoding='utf-8') as f:
                return _validate(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            raise ProfileError('Cannot read profile: ' + str(e)) from e


def _atomic_write(path, encoded):
    fd, temp_name = tempfile.mkstemp(prefix='.profile-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
        try:
            dirfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save(directory, name, snapshot, *, create_only=False):
    path = _path(directory, name)
    _validate(snapshot)
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as e:
        raise ProfileError('Cannot serialize profile: ' + str(e)) from e
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if create_only and path.exists():
            raise ProfileError('Profile already exists')
        if path.exists():
            # Refuse to overwrite corrupt data, and preserve the validated prior
            # generation as a separate recovery file before replacing the main.
            old = load(directory, name)
            old_bytes = json.dumps(old, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
            _atomic_write(path.with_suffix('.backup'), old_bytes)
        _atomic_write(path, encoded)


def load_backup(directory, name):
    """Read the previous validated generation explicitly; never silently roll back."""
    path = _path(directory, name).with_suffix('.backup')
    with _LOCK:
        try:
            with path.open('r', encoding='utf-8') as f:
                return _validate(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            raise ProfileError('Cannot read profile backup: ' + str(e)) from e


def list_names(directory):
    root = Path(directory)
    if not root.exists():
        return []
    with _LOCK:
        return sorted(p.stem for p in root.glob('*.json') if _NAME.fullmatch(p.stem))


def delete(directory, name):
    """Permanently remove one inactive profile and its backup, never other profiles."""
    path = _path(directory, name)
    with _LOCK:
        if not path.is_file():
            raise ProfileError('Profile not found')
        try:
            # Keep the primary if removing its backup fails.
            backup = path.with_suffix('.backup')
            if backup.exists():
                backup.unlink()
            path.unlink()
        except OSError as e:
            raise ProfileError('Cannot delete profile: ' + str(e)) from e