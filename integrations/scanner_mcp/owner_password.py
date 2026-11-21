"""Owner approval password hashing for the remote MCP services.

Standard library only, so the deploy installers (run with the app venv, which
has no MCP SDK) can create hashes with the exact format the services verify.
"""
import base64
import binascii
import hashlib
import secrets


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# Owner approval password hash. New installs store salted scrypt
# ("scrypt$N$r$p$salt$key", base64 without padding). Existing unsalted SHA-256
# hex hashes keep working because they cannot be converted without the
# plaintext; rotate the password to move them to scrypt.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
_SCRYPT_MAX_N, _SCRYPT_KEY_LEN, _SCRYPT_MAXMEM = 2**16, 32, 128 * 1024 * 1024


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def hash_owner_password(password, *, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                         maxmem=_SCRYPT_MAXMEM, dklen=_SCRYPT_KEY_LEN)
    return f'scrypt${n}${r}${p}${_b64(salt)}${_b64(key)}'


def _is_legacy_hash(stored):
    return len(stored) == 64 and all(c in '0123456789abcdef' for c in stored)


def _parse_scrypt(stored):
    parts = stored.split('$')
    if len(parts) != 6 or parts[0] != 'scrypt':
        raise ValueError('Owner credential hash format is not supported')
    try:
        n, r, p = (int(x) for x in parts[1:4])
        salt, key = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Owner credential hash is malformed') from exc
    if not (2 <= n <= _SCRYPT_MAX_N and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
        raise ValueError('Owner credential hash cost is out of range')
    if len(salt) < 16 or len(key) != _SCRYPT_KEY_LEN:
        raise ValueError('Owner credential hash is malformed')
    return n, r, p, salt, key


def validate_owner_hash(stored):
    if not isinstance(stored, str) or not (_is_legacy_hash(stored) or _parse_scrypt(stored)):
        raise ValueError('Owner credential hash required')


def verify_owner_password(password, stored):
    if _is_legacy_hash(stored):
        return secrets.compare_digest(digest(password), stored)
    n, r, p, salt, key = _parse_scrypt(stored)
    candidate = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                               maxmem=_SCRYPT_MAXMEM, dklen=len(key))
    return secrets.compare_digest(candidate, key)
