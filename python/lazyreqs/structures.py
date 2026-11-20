"""Data structures used by ``lazyreqs``.

``CaseInsensitiveDict`` mirrors ``requests.structures.CaseInsensitiveDict``
(implemented on ``MutableMapping``, as upstream does).  ``LazyDict`` is a real
``dict`` subclass whose contents are materialised on first access.
"""

import threading
from collections.abc import MutableMapping

__all__ = ["CaseInsensitiveDict", "LazyDict"]


class CaseInsensitiveDict(MutableMapping):
    """A case-insensitive ``dict``-like object.

    Implements all methods and operations of ``MutableMapping`` as well as
    ``dict``'s ``copy``.  All keys are expected to be strings and the last
    case a key was set with is remembered, which is convenient for
    round-tripping HTTP headers.

    Example::

        >>> d = CaseInsensitiveDict()
        >>> d["Accept"] = "application/json"
        >>> d["accept"] == "application/json"
        True
        >>> list(d.keys())
        ['Accept']
    """

    def __init__(self, data=None, **kwargs):
        # _store maps lower-cased key -> (original_key, value)
        self._store = {}
        if data is None:
            data = {}
        self.update(data, **kwargs)

    def __setitem__(self, key, value):
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key):
        return self._store[key.lower()][1]

    def __delitem__(self, key):
        del self._store[key.lower()]

    def __iter__(self):
        return (origkey for origkey, _ in self._store.values())

    def __len__(self):
        return len(self._store)

    def __contains__(self, key):
        return key.lower() in self._store

    def __eq__(self, other):
        if isinstance(other, CaseInsensitiveDict):
            return dict(self.lower_items()) == dict(other.lower_items())
        return dict(self.lower_items()) == {
            k.lower(): v for k, v in dict(other).items()
        }

    def lower_items(self):
        """Like ``items()``, but with lower-cased keys."""
        return ((lower_key, value) for lower_key, (_, value) in self._store.items())

    def copy(self):
        return CaseInsensitiveDict(self._store.values())

    def __repr__(self):
        return str(dict(self.items()))


class LazyDict(dict):
    """A ``dict`` subclass that resolves its contents on first access.

    Instances are constructed with a zero-argument *loader* that returns a
    mapping.  Every read (``[]``, ``in``, ``len``, iteration, ``==`` …) and
    every mutation triggers the loader exactly once; afterwards the object is
    an ordinary ``dict``.

    ``LazyDict`` deliberately subclasses ``dict`` so that ``isinstance(value,
    dict)`` is true, which keeps JSON-shaped data interchangeable with plain
    dictionaries.
    """

    __slots__ = ("_lazy_done", "_lazy_error", "_lazy_loader", "_lazy_lock")

    def __init__(self, loader):
        dict.__init__(self)
        object.__setattr__(self, "_lazy_loader", loader)
        object.__setattr__(self, "_lazy_done", False)
        object.__setattr__(self, "_lazy_error", None)
        object.__setattr__(self, "_lazy_lock", threading.RLock())

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def _lazy_load(self):
        with object.__getattribute__(self, "_lazy_lock"):
            if not object.__getattribute__(self, "_lazy_done"):
                loader = object.__getattribute__(self, "_lazy_loader")
                object.__setattr__(self, "_lazy_loader", None)
                try:
                    if loader is None:
                        data = {}
                    else:
                        data = loader()
                        if not isinstance(data, dict):
                            data = dict(data)
                except BaseException as exc:
                    object.__setattr__(self, "_lazy_done", True)
                    object.__setattr__(self, "_lazy_error", exc)
                    raise
                object.__setattr__(self, "_lazy_done", True)
                dict.update(self, data)
            error = object.__getattribute__(self, "_lazy_error")
            if error is not None:
                raise error
        return self

    # ``loads`` is a friendlier, explicit spelling of ``_lazy_load``.
    def loads(self):
        """Force resolution and return ``self`` (now a plain dict)."""
        return self._lazy_load()

    @property
    def resolved(self):
        """``True`` once the underlying mapping has been materialised."""
        return object.__getattribute__(self, "_lazy_done")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    def __getitem__(self, key):
        return dict.__getitem__(self._lazy_load(), key)

    def get(self, key, default=None):
        return dict.get(self._lazy_load(), key, default)

    def __contains__(self, key):
        return dict.__contains__(self._lazy_load(), key)

    def __iter__(self):
        return dict.__iter__(self._lazy_load())

    def __len__(self):
        return dict.__len__(self._lazy_load())

    def __bool__(self):
        return dict.__bool__(self._lazy_load())

    def keys(self):
        return dict.keys(self._lazy_load())

    def values(self):
        return dict.values(self._lazy_load())

    def items(self):
        return dict.items(self._lazy_load())

    def __eq__(self, other):
        return dict.__eq__(self._lazy_load(), other)

    def __ne__(self, other):
        return dict.__ne__(self._lazy_load(), other)

    def __or__(self, other):
        return dict.__or__(self._lazy_load(), other)

    def __ror__(self, other):
        return dict.__ror__(self._lazy_load(), other)

    def __reversed__(self):
        return dict.__reversed__(self._lazy_load())

    def copy(self):
        return dict(self._lazy_load())

    def __reduce__(self):
        return (dict, (dict(self._lazy_load()),))

    def __reduce_ex__(self, protocol):
        return self.__reduce__()

    def __repr__(self):
        if not object.__getattribute__(self, "_lazy_done"):
            return "<LazyDict pending>"
        return repr(dict(self))

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def __setitem__(self, key, value):
        dict.__setitem__(self._lazy_load(), key, value)

    def __delitem__(self, key):
        dict.__delitem__(self._lazy_load(), key)

    def clear(self):
        dict.clear(self._lazy_load())

    def pop(self, key, *default):
        return dict.pop(self._lazy_load(), key, *default)

    def popitem(self):
        return dict.popitem(self._lazy_load())

    def setdefault(self, key, default=None):
        return dict.setdefault(self._lazy_load(), key, default)

    def update(self, *args, **kwargs):
        return dict.update(self._lazy_load(), *args, **kwargs)

    def fromkeys(self, iterable, value=None):
        # ``dict.fromkeys`` is a classmethod-ish constructor; keep it plain.
        return dict.fromkeys(iterable, value)