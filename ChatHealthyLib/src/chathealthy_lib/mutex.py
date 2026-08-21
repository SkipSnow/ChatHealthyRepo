"""The mutex: one holder at a time.

A service that must not run twice takes the mutex before it does
anything and gives it back when it is done. The collection holds one record
or none: a record means the service is running.

    look        a record is there    -> abend, and say who holds it
                no record            -> write one
    write       it lands             -> this job holds the service
                it is refused        -> abend, another job holds it

The write is what decides. The record's id is the mutex type, so a
second one cannot exist: the database refuses it, and that refusal is the
arbitration -- not the look above it, which only has a window between
reading and writing and cannot settle anything two jobs do at once. The look
is kept because it can say who holds the service and since when, which a
duplicate-key error cannot.

Left unconverted that refusal leaves as a driver error rather than one of
ours, and a service that recognises only its own exceptions as a refusal
dies without ever reporting that it was refused for being second.

This lives in the library rather than in the service that takes it, because
admitting a job and performing one are different concerns. The service asks
whether it may run; what it does once it may is no business of this module.
"""
from __future__ import annotations

import datetime

from pymongo.errors import DuplicateKeyError

from .exceptions import ChatHealthyException
from .mongo_utilities import ChatHealthyMongoUtilities

MUTEX_DATABASE = "pipelineAdmin"
MUTEX_COLLECTION = "DataMigrationServiceMutex"


def _collection(identity: str, cluster: str):
    return ChatHealthyMongoUtilities().getConnection(
        identity, cluster)[MUTEX_DATABASE][MUTEX_COLLECTION]


def _raise_held(mutex_name: str, holder: str, about: str,
                held: dict) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="mutex_held",
        component="mutexs",
        message=(f"the {mutex_name!r} mutex is held by {held.get('holder')!r} "
                 f"since {held.get('taken_at')} for {held.get('about')!r}; "
                 f"{holder!r} was refused"),
        mutex_name=mutex_name,
        about=about)


def _raise_contended(mutex_name: str, holder: str, about: str,
                     found: int) -> None:
    raise ChatHealthyException(
        mode="mutex_contended",
        component="mutexs",
        message=(f"{found} holders of the {mutex_name!r} mutex exist at "
                 f"once: another invocation wrote one at the same moment. "
                 f"{holder!r} withdrew its own and refused; no holder is "
                 f"assumed"),
        mutex_name=mutex_name,
        about=about)


def take(identity: str, cluster: str, mutex_name: str,
            holder: str, about: str = "") -> None:
    """Take the mutex, or raise.

    There is one record or none. The record's id is the mutex's name, so there is
    nothing to hand back and forth and nothing to look up.
    """
    collection = _collection(identity, cluster)

    # Any holder at all, not one of this mutex. The collection holds one
    # record or none, and a record means something is running.
    held = collection.find_one({})
    if held is not None:
        _raise_held(mutex_name, holder, about, held)

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        collection.insert_one({
            "_id": mutex_name,
            "mutex_name": mutex_name,
            "holder": holder,
            "about": about,
            "taken_at": now,
            "day": now.date().isoformat(),
        })
    except DuplicateKeyError:
        # Another holder wrote between the look above and this write. The id
        # is the mutex type, so the database refuses the second write
        # on its own -- that refusal is the same fact the look was after, and
        # it arrives as a driver error rather than as one of ours. Left
        # unconverted it leaves the process as a DuplicateKeyError, which the
        # service does not recognise as a refusal: the job dies without ever
        # reporting that it was refused for being second.
        held = collection.find_one({}) or {}
        _raise_held(mutex_name, holder, about, held)

    # The whole collection, not this record. Counting by id proves nothing --
    # an id is unique, so that count is one by definition. More than one
    # mutex of any kind means the service's state is not what this
    # invocation believes, and it abends rather than acting on it.
    found = collection.count_documents({})
    if found != 1:
        collection.delete_one({"_id": mutex_name})
        _raise_contended(mutex_name, holder, about, found)


def give_back(identity: str, cluster: str, mutex_name: str) -> None:
    """Give the mutex back, so the next holder can take it."""
    _collection(identity, cluster).delete_one({"_id": mutex_name})
