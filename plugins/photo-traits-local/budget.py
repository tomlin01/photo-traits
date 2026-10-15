"""Atomic project budget contract and an offline SQLite reference implementation.

The reference survives local process restarts, NOT loss of an ephemeral disk.
It deliberately refuses live readiness. A deployment must supply a reviewed
durable binding; no tool or startup path creates, resets or funds a ledger.
Amounts are integer nano-USD, never binary floats. No photos or credentials belong
here. Unsettled reservations have no expiry and cannot be refunded by cancellation.
"""
from contextlib import closing, contextmanager
from decimal import Decimal
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Protocol
import uuid

PROJECT = 'phototraits-new-spend-2026-10-14'
SCALE = 1_000_000_000
TOTAL = 5 * SCALE
PER_CALL = SCALE // 4


class BudgetError(ValueError):
    """Constant messages only; never expose backend errors or paths."""


def nanos(value):
    if not isinstance(value, (str, Decimal)):
        raise BudgetError('Money must be an exact decimal string.')
    try:
        amount = Decimal(value) * SCALE
        if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
            raise ValueError
        return int(amount)
    except (ValueError, ArithmeticError):
        raise BudgetError('Invalid money amount.') from None


def usd(amount):
    return str(Decimal(amount) / SCALE)


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise BudgetError('A bounded evidence digest is required.')
    return value


class BudgetStore(Protocol):
    """All callers share ONE externally pinned ledger identity for this project.

    Implementations must atomically reserve against settled upper charges plus
    all pending reservations, durably commit before acknowledging, and refuse
    replayed attempt IDs (even after settlement). Uncertain commits are errors,
    never permission to send. Settlement is idempotent only for identical evidence.
    A missing/unavailable/replaced ledger fails closed; it is never initialized
    by these methods. Storage failures must expose only constant safe errors.
    """
    def ensure_ready(self, *, for_live: bool) -> None: ...
    def snapshot(self) -> dict: ...
    def reserve(self, attempt_id: str, request_sha256: str, price_sha256: str,
                maximum_usd: str) -> dict: ...
    def settle(self, attempt_id: str, *, cost_upper_usd: str, input_tokens: int,
               response_sha256: str, price_sha256: str) -> dict: ...


class SQLiteBudgetStore:
    """Single-host reference. SQLite alone does not establish cloud durability.

    Keep this database outside session/photo cleanup. Reopening requires the same
    non-secret ledger_id, pinned separately by the operator. Tests use temporary
    directories and explicit synthetic transports, never a live-readiness bypass.
    """
    def __init__(self, path, *, ledger_id):
        self.path = Path(path).absolute()
        if not isinstance(ledger_id, str) or not re.fullmatch(r'[a-f0-9]{32}', ledger_id):
            raise BudgetError('The existing ledger identity is required.')
        self.ledger_id = ledger_id

    @staticmethod
    def provision(path, *, opening_spent_upper_usd, opening_evidence_sha256):
        """Explicit offline provisioning primitive; never called by app startup.

        The operator must audit ALL new-project spend first. Zero is not a default.
        Preserve the returned identity separately. An existing file is never reset.
        Tests supply a synthetic opening balance and synthetic evidence digest.
        """
        opening = nanos(opening_spent_upper_usd)
        evidence = digest(opening_evidence_sha256)
        if opening > TOTAL:
            raise BudgetError('Opening spend exceeds the USD 5 project limit.')
        path = Path(path).absolute()
        if path.resolve() != path or not path.parent.is_dir():
            raise BudgetError('Ledger must have an existing non-symlink parent.')
        ledger_id = uuid.uuid4().hex
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA synchronous=EXTRA')
                db.execute('PRAGMA fullfsync=ON')
                db.executescript('''
                    BEGIN IMMEDIATE;
                    CREATE TABLE project (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        project_id TEXT NOT NULL, ledger_id TEXT NOT NULL,
                        limit_nanos INTEGER NOT NULL CHECK(limit_nanos=5000000000),
                        opening_nanos INTEGER NOT NULL CHECK(opening_nanos BETWEEN 0 AND 5000000000),
                        evidence_sha256 TEXT NOT NULL, version INTEGER NOT NULL CHECK(version=1));
                    CREATE TABLE attempts (
                        attempt_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                        price_sha256 TEXT NOT NULL,
                        reserved_nanos INTEGER NOT NULL CHECK(reserved_nanos=250000000),
                        charged_nanos INTEGER CHECK(charged_nanos BETWEEN 1 AND reserved_nanos),
                        input_tokens INTEGER, response_sha256 TEXT,
                        CHECK ((charged_nanos IS NULL AND input_tokens IS NULL AND response_sha256 IS NULL)
                            OR (charged_nanos IS NOT NULL AND input_tokens > 0 AND response_sha256 IS NOT NULL)));
                ''')
                db.execute('INSERT INTO project VALUES (1, ?, ?, ?, ?, ?, 1)',
                           (PROJECT, ledger_id, TOTAL, opening, evidence))
                db.commit()
            # Persist directory entry as well as SQLite's file/transaction.
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, sqlite3.Error):
            # A partial file remains closed to execution; never recreate on error.
            raise BudgetError('Ledger provisioning failed; inspect locally, never reset automatically.') from None
        return SQLiteBudgetStore(path, ledger_id=ledger_id)

    @contextmanager
    def _transaction(self):
        db = None
        try:
            if self.path.resolve() != self.path:
                raise BudgetError('Ledger path was redirected.')
            info = self.path.stat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()):
                raise BudgetError('Ledger must be an owner-only regular file.')
            # mode=rw prevents silent creation on a missing mount/file.
            db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=2,
                                 isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=EXTRA')
            db.execute('PRAGMA fullfsync=ON')
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise BudgetError('Unexpected ledger journal configuration.')
            db.execute('BEGIN IMMEDIATE')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise BudgetError('Ledger integrity check failed.')
            rows = db.execute('SELECT * FROM project').fetchall()
            if (len(rows) != 1 or rows[0]['project_id'] != PROJECT
                    or rows[0]['ledger_id'] != self.ledger_id or rows[0]['limit_nanos'] != TOTAL
                    or rows[0]['version'] != 1):
                raise BudgetError('Missing or mismatched project budget state.')
            digest(rows[0]['evidence_sha256'])
            self._summary(db)  # Validate before either reads or writes.
            yield db
            db.commit()  # No successful acknowledgment before the durable commit.
        except (OSError, sqlite3.Error):
            raise BudgetError('Budget storage unavailable or corrupt; no new request permitted.') from None
        finally:
            if db is not None:
                db.close()  # Rolls back any uncommitted transaction.

    @staticmethod
    def _summary(db):
        opening = db.execute('SELECT opening_nanos FROM project').fetchone()[0]
        row = db.execute('''SELECT COALESCE(SUM(charged_nanos), 0),
                          COALESCE(SUM(CASE WHEN charged_nanos IS NULL THEN reserved_nanos ELSE 0 END), 0),
                          COUNT(*), COALESCE(SUM(charged_nanos IS NULL), 0) FROM attempts''').fetchone()
        settled, pending, count, pending_count = row
        used = opening + settled + pending
        if any(type(v) is not int or v < 0 for v in (opening, settled, pending, used)) or used > TOTAL:
            raise BudgetError('Budget invariant failed; execution stopped.')
        return {'project_id': PROJECT, 'limit_usd': '5', 'opening_spent_upper_usd': usd(opening),
                'settled_upper_usd': usd(settled), 'pending_reserved_usd': usd(pending),
                'committed_upper_usd': usd(used), 'available_usd': usd(TOTAL-used),
                'attempts': count, 'pending_attempts': pending_count}

    def ensure_ready(self, *, for_live):
        if for_live:
            raise BudgetError('Live blocked: this local reference has no verified durable deployment binding.')
        self.snapshot()

    def snapshot(self):
        with self._transaction() as db:
            return self._summary(db)

    def reserve(self, attempt_id, request_sha256, price_sha256, maximum_usd):
        for value in (attempt_id, request_sha256, price_sha256):
            digest(value)
        if nanos(maximum_usd) != PER_CALL:
            raise BudgetError('The reviewed per-call maximum is USD 0.25.')
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone():
                raise BudgetError('Attempt already reserved; never resend or automatically retry.')
            if nanos(self._summary(db)['available_usd']) < PER_CALL:
                raise BudgetError('The total USD 5 project budget cannot cover another reservation.')
            db.execute('INSERT INTO attempts (attempt_id, request_sha256, price_sha256, reserved_nanos) VALUES (?, ?, ?, ?)',
                       (attempt_id, request_sha256, price_sha256, PER_CALL))
            return {'attempt_id': attempt_id, 'reserved_usd': usd(PER_CALL),
                    'price_sha256': price_sha256, 'request_sha256': request_sha256}

    def settle(self, attempt_id, *, cost_upper_usd, input_tokens, response_sha256, price_sha256):
        for value in (attempt_id, response_sha256, price_sha256):
            digest(value)
        amount = nanos(cost_upper_usd)
        if not 0 < amount <= PER_CALL or type(input_tokens) is not int or not 0 < input_tokens <= 922_000:
            raise BudgetError('Invalid usage evidence; keep the full reservation.')
        # This price revision uses 220 nano-USD per input token, covering 2x and 1.1x.
        if amount != input_tokens * 220:
            raise BudgetError('Usage charge disagrees with the reviewed upper rate.')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if row is None or row['price_sha256'] != price_sha256:
                raise BudgetError('Reservation or price evidence is missing or mismatched.')
            if row['charged_nanos'] is not None:
                if (row['charged_nanos'], row['input_tokens'], row['response_sha256']) != (amount, input_tokens, response_sha256):
                    raise BudgetError('Conflicting settlement evidence; do not revise charges.')
            else:
                db.execute('UPDATE attempts SET charged_nanos=?, input_tokens=?, response_sha256=? WHERE attempt_id=?',
                           (amount, input_tokens, response_sha256, attempt_id))
            return self._summary(db)
