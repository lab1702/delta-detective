"""Resolve explicit snapshots and root-relative Hive partitions without glob reads."""
import os
from pathlib import Path
from urllib.parse import unquote

from .config import InvestigationError, identifier_key


def linked(path):
    # Python 3.11 on Windows has no isjunction(); reparse points include junctions.
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def snapshot_files(value):
    if isinstance(value, list):
        return [(str(Path(p).resolve()), {}) for p in value]
    root = Path(value)
    if not root.is_dir():
        return [(str(root.resolve()), {})]
    files = []
    def walk_error(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs:
            if linked(Path(directory, name)):
                raise InvestigationError('Snapshot directories must not contain directory links or junctions')
        for name in names:
            path = Path(directory, name)
            if path.suffix.lower() not in ('.csv', '.parquet'):
                continue
            if linked(path) or not path.resolve().is_relative_to(root.resolve()):
                raise InvestigationError('Snapshot files must stay within the snapshot directory without links')
            if any(c in str(path) for c in '*?[]'):
                raise InvestigationError('Snapshot file paths must not contain glob characters')
            partitions = {}
            for part in path.relative_to(root).parts[:-1]:
                if '=' not in part:
                    continue
                key, raw = part.split('=', 1)
                try:
                    key, decoded = unquote(key, errors='strict'), unquote(raw, errors='strict')
                except UnicodeError:
                    raise InvestigationError('Invalid UTF-8 Hive partition encoding') from None
                if not key or '\x00' in key or '\x00' in decoded or identifier_key(key) in {identifier_key(k) for k in partitions}:
                    raise InvestigationError('Hive partition names must be nonempty and unique within each path')
                partitions[key] = None if raw == '__HIVE_DEFAULT_PARTITION__' else decoded
            files.append((str(path.resolve()), partitions))
    files.sort(key=lambda item: item[0])
    if not files:
        raise InvestigationError('Snapshot directory contains no CSV or Parquet files')
    expected = set(files[0][1])
    if any(set(partitions) != expected for _, partitions in files):
        raise InvestigationError('All files in a directory snapshot must have the same Hive partition columns')
    return files


def has_csv(value):
    return any(Path(path).suffix.lower() == '.csv' for path, _ in snapshot_files(value))
